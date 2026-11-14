"""冲突检测：把「同 target 同优先级给出不同值」显式列出，并给出可执行的修复建议。"""
from __future__ import annotations

SUGGEST_STEP = 10


def detect(targets: list[dict]) -> list[dict]:
    """返回冲突清单（含候选规则、值、可选项）。"""
    out: list[dict] = []
    for entry in targets:
        conflict = entry.get("conflict")
        if not conflict:
            continue
        item = dict(conflict)
        item["summary"] = describe(item)
        item["fix"] = suggest_fix(item)
        out.append(item)
    return out


def describe(conflict: dict) -> str:
    pairs = "、".join(f'{c["rule_id"]}={c["value_display"]}' for c in conflict["candidates"])
    return (
        f'目标字段 {conflict["label"]}（{conflict["target"]}）在优先级 {conflict["priority"]} '
        f"上有 {len(conflict['candidates'])} 条规则给出不同值：{pairs}"
    )


def suggest_fix(conflict: dict) -> dict:
    """建议：把候选里 rule_id 最小的一条提到 top+10，其余保持不变。"""
    candidates = sorted(conflict["candidates"], key=lambda c: c["rule_id"])
    keep = candidates[0]
    target_priority = conflict["priority"] + SUGGEST_STEP
    return {
        "rule_id": keep["rule_id"],
        "target": conflict["target"],
        "label": conflict["label"],
        "from_priority": keep["priority"],
        "to_priority": target_priority,
        "kept_value": keep["value_display"],
        "suppressed": [c["rule_id"] for c in candidates[1:]],
        "why": (
            f"把 {keep['rule_id']} 的优先级从 {keep['priority']} 提到 {target_priority}，"
            f"保留值 {keep['value_display']}；其余同优先级规则自动降为被覆盖，冲突消除。"
        ),
    }


def apply_fix(rules_priority_map: dict[str, int], fix: dict) -> dict[str, int]:
    """把一条建议写回优先级表（供页面「一键采纳」使用）。"""
    updated = dict(rules_priority_map)
    updated[fix["rule_id"]] = fix["to_priority"]
    return updated
