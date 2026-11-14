"""表单级字段校验：必填 / 未命中 / 冲突未决 / 类型与业务规则 → 规范化 payload。"""
from __future__ import annotations

from . import contract as contract_mod


def _error(entry: dict, code: str, message: str, hint: str = "", **extra) -> dict:
    item = {
        "target": entry["name"],
        "label": entry["label"],
        "type": entry["type"],
        "code": code,
        "message": message,
        "hint": hint,
    }
    item.update(extra)
    return item


def validate(targets: list[dict], contract: dict) -> dict:
    """返回 {"ok", "errors", "payload", "optional_empty"}。payload 是可直接提交的规范化值。"""
    errors: list[dict] = []
    payload: dict = {}
    optional_empty: list[str] = []
    by_name = {entry["name"]: entry for entry in targets}

    for field in contract["fields"]:
        name = field["name"]
        entry = by_name.get(name) or {
            "name": name,
            "label": field.get("label") or name,
            "type": field.get("type"),
            "required": bool(field.get("required")),
            "status": "unmapped",
            "value": None,
            "rules": [],
        }
        required = bool(field.get("required"))

        if entry["status"] == "conflict" and entry.get("conflict"):
            conflict = entry["conflict"]
            errors.append(
                _error(
                    entry,
                    "conflict_unresolved",
                    f'该字段存在未决冲突（优先级 {conflict["priority"]}，'
                    f'{len(conflict["candidates"])} 条规则值不同），无法确定提交值',
                    hint="在工作台调整规则优先级后重跑 dry-run",
                    candidates=conflict["candidates"],
                )
            )
            continue

        value = entry.get("value")
        if value is None:
            if required:
                if entry["status"] == "unmapped":
                    message = "必填字段没有任何映射规则命中"
                    hint = "为该字段新增一条映射规则"
                elif entry["status"] == "miss":
                    reasons = "；".join(
                        f'{r["rule_id"]}: {r["reason"]}' for r in entry.get("rules") or [] if r.get("reason")
                    )
                    message = f"必填字段的映射规则全部未命中（{reasons or '无可用值'}）"
                    hint = "检查源单据字段名，或补充一条可命中的规则"
                else:
                    message = "必填字段没有可用的映射结果"
                    hint = "检查规则表达式与源单据字段"
                errors.append(_error(entry, "missing_required", message, hint=hint))
            else:
                optional_empty.append(name)
            continue

        ok, coerced, code, message, extra = contract_mod.coerce(field, value)
        if not ok:
            errors.append(_error(entry, code, message, hint=_hint_for(code), **extra))
            continue
        payload[name] = coerced

    return {
        "ok": not errors,
        "errors": errors,
        "payload": payload,
        "optional_empty": optional_empty,
        "error_count": len(errors),
    }


def _hint_for(code: str) -> str:
    return {
        "invalid_date": "按 YYYY-MM-DD 填写真实日历日期（如 2026-02-30 不存在）",
        "invalid_date_format": "日期需为 ISO 格式 YYYY-MM-DD",
        "enum_invalid": "从契约给出的可选项中选择",
        "amount_decimals": "金额保留两位小数",
        "amount_not_positive": "金额必须大于 0",
        "amount_exceeds_max": "超过单笔上限，需拆分单据或走大额审批通道",
        "max_length": "截断或改用规则 substr() 变换",
        "type_boolean": "加急字段接受 true/false、是/否、1/0",
        "type_integer": "版本号必须是整数",
        "type_string": "目标字段需要文本",
        "type_amount": "金额字段需要数字",
        "type_date": "日期字段需要文本",
        "type_enum": "费用类型需要文本",
        "out_of_range": "数值超出契约允许范围",
    }.get(code, "")


def group_by_code(errors: list[dict]) -> list[dict]:
    """按错误码聚合，供结果页/复核页展示。"""
    buckets: dict[str, dict] = {}
    for err in errors:
        bucket = buckets.setdefault(err["code"], {"code": err["code"], "count": 0, "fields": [], "message": err["message"]})
        bucket["count"] += 1
        bucket["fields"].append(err["label"])
    return sorted(buckets.values(), key=lambda b: (-b["count"], b["code"]))
