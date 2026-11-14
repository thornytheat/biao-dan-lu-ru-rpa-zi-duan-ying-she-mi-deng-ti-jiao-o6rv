"""映射规则求值：逐条规则求值 → 按 target 收敛到最高优先级 → 冲突 / 未命中。

纯函数，不碰数据库；路由与 service 只做编排。
"""
from __future__ import annotations

from decimal import Decimal

from . import expr

OK = "ok"
MISS = "miss"
ERROR = "error"

STATUS_LABEL = {
    "ok": "已映射",
    "conflict": "同优先级冲突",
    "miss": "未命中",
    "unmapped": "无规则",
    "empty": "可空未映射",
}


def effective_rules(base_rules: list[dict], overrides: dict[str, dict]) -> list[dict]:
    """把库里的优先级/启停覆盖叠加到样例规则上；被禁用的规则直接剔除。"""
    out: list[dict] = []
    for rule in base_rules:
        item = dict(rule)
        item["transform"] = list(rule.get("transform") or [])
        item["base_priority"] = int(rule.get("priority", 0))
        item["enabled"] = True
        override = overrides.get(item["id"]) or {}
        if "priority" in override and override["priority"] is not None:
            item["priority"] = int(override["priority"])
        else:
            item["priority"] = item["base_priority"]
        if override.get("enabled") is False:
            item["enabled"] = False
            continue
        out.append(item)
    return out


def evaluate_rule(rule: dict, doc: dict) -> dict:
    """单条规则求值，永远返回结果字典（不抛异常）。"""
    result = {
        "rule_id": rule["id"],
        "target": rule["target"],
        "expr": rule.get("expr", ""),
        "priority": int(rule.get("priority", 0)),
        "base_priority": int(rule.get("base_priority", rule.get("priority", 0))),
        "transform": list(rule.get("transform") or []),
        "note": rule.get("note", ""),
        "status": OK,
        "value": None,
        "value_display": "—",
        "code": "",
        "reason": "",
    }
    try:
        raw = expr.evaluate(rule.get("expr", ""), doc)
        value = expr.apply_transforms(raw, rule.get("transform") or [])
    except expr.ExprError as exc:
        result["status"] = MISS if exc.code in {"source_field_missing", "source_doc_invalid"} else ERROR
        result["code"] = exc.code
        result["reason"] = exc.message
        return result
    result["value"] = value
    result["value_display"] = format_value(value)
    return result


def evaluate_rules(rules: list[dict], doc: dict) -> list[dict]:
    return [evaluate_rule(rule, doc) for rule in rules]


def format_value(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def value_key(value) -> str:
    """冲突比较口径：1 与 1.0 视为同值；文本按原样比较（"张伟" 与 " 张伟 " 是不同提交值）。"""
    if value is None:
        return "none:"
    if isinstance(value, bool):
        return "bool:" + ("true" if value else "false")
    if isinstance(value, Decimal):
        return "num:" + format(value.normalize(), "f")
    if isinstance(value, (int, float)):
        return "num:" + format(Decimal(str(value)).normalize(), "f")
    return "str:" + str(value)


def resolve_targets(rule_results: list[dict], contract: dict) -> list[dict]:
    """按契约字段顺序收敛：最高优先级获胜，同优先级多值不同 → 冲突。"""
    grouped: dict[str, list[dict]] = {}
    for result in rule_results:
        grouped.setdefault(result["target"], []).append(result)

    targets: list[dict] = []
    for field in contract["fields"]:
        name = field["name"]
        results = grouped.get(name, [])
        entry = {
            "name": name,
            "label": field.get("label") or name,
            "type": field.get("type"),
            "required": bool(field.get("required")),
            "spec": field,
            "rules": results,
            "value": None,
            "value_display": "—",
            "status": "unmapped" if not results else "miss",
            "chosen": None,
            "conflict": None,
        }
        ok_results = [r for r in results if r["status"] == OK]
        if not results:
            targets.append(entry)
            continue
        if not ok_results:
            entry["status"] = "miss"
            targets.append(entry)
            continue

        top_priority = max(r["priority"] for r in ok_results)
        top = [r for r in ok_results if r["priority"] == top_priority]
        buckets: dict[str, list[dict]] = {}
        for result in top:
            buckets.setdefault(value_key(result["value"]), []).append(result)

        if len(buckets) > 1:
            entry["status"] = "conflict"
            entry["conflict"] = {
                "target": name,
                "label": entry["label"],
                "priority": top_priority,
                "candidates": [
                    {
                        "rule_id": r["rule_id"],
                        "expr": r["expr"],
                        "priority": r["priority"],
                        "value": r["value"],
                        "value_display": r["value_display"],
                    }
                    for r in sorted(top, key=lambda r: r["rule_id"])
                ],
                "lower_priority": [
                    {
                        "rule_id": r["rule_id"],
                        "priority": r["priority"],
                        "value_display": r["value_display"],
                    }
                    for r in sorted(ok_results, key=lambda r: (-r["priority"], r["rule_id"]))
                    if r["priority"] < top_priority
                ],
            }
            targets.append(entry)
            continue

        winner = sorted(top, key=lambda r: r["rule_id"])[0]
        entry["status"] = OK
        entry["value"] = winner["value"]
        entry["value_display"] = winner["value_display"]
        entry["chosen"] = {
            "rule_id": winner["rule_id"],
            "expr": winner["expr"],
            "priority": winner["priority"],
            "overridden": [
                {"rule_id": r["rule_id"], "priority": r["priority"], "value_display": r["value_display"]}
                for r in ok_results
                if r["priority"] < winner["priority"]
            ],
        }
        targets.append(entry)
    return targets


def stats(rule_results: list[dict]) -> dict:
    total = len(rule_results)
    misses = [r for r in rule_results if r["status"] == MISS]
    errors = [r for r in rule_results if r["status"] == ERROR]
    return {
        "rules_total": total,
        "rules_ok": total - len(misses) - len(errors),
        "rules_miss": len(misses),
        "rules_error": len(errors),
        "missed_rules": [
            {"rule_id": r["rule_id"], "target": r["target"], "reason": r["reason"]} for r in misses + errors
        ],
    }
