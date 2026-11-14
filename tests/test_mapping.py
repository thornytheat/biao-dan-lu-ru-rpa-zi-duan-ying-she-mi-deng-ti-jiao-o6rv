"""映射规则求值：优先级、冲突、表达式、变换、未命中（核心规则一）。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app import conflicts, contract, mapping


def rule(rule_id: str, target: str, expr: str, priority: int, transform=None, note="") -> dict:
    return {
        "id": rule_id,
        "target": target,
        "expr": expr,
        "priority": priority,
        "base_priority": priority,
        "transform": transform or [],
        "note": note,
    }


def resolve(rules, doc):
    contract_doc = contract.load_contract()
    results = mapping.evaluate_rules(rules, doc)
    targets = mapping.resolve_targets(results, contract_doc)
    return results, {t["name"]: t for t in targets}, targets


DOC = {"amount_total": 100, "tax_rate_factor": 1.13, "applicant": " 张伟 ", "cost_center": "CC-3301-01", "urgent": "是"}


def test_highest_priority_wins():
    rules = [
        rule("R01", "applicant", "trim({{applicant}})", 10),
        rule("R02", "applicant", "upper({{applicant}})", 30),
    ]
    _, targets, _ = resolve(rules, DOC)
    assert targets["applicant"]["value"] == " 张伟 "          # p30 的 upper 获胜
    assert targets["applicant"]["chosen"]["rule_id"] == "R02"
    assert targets["applicant"]["chosen"]["overridden"][0]["rule_id"] == "R01"


def test_same_priority_same_value_is_not_conflict():
    rules = [
        rule("R01", "applicant", "trim({{applicant}})", 20),
        rule("R02", "applicant", "trim({{applicant}})", 20),
    ]
    _, targets, all_targets = resolve(rules, DOC)
    assert targets["applicant"]["status"] == "ok"
    assert targets["applicant"]["value"] == "张伟"
    assert conflicts.detect(all_targets) == []


def test_same_priority_different_values_is_conflict():
    rules = [
        rule("R01", "applicant", "trim({{applicant}})", 20),
        rule("R02", "applicant", "upper({{applicant}})", 20),
    ]
    _, targets, all_targets = resolve(rules, DOC)
    entry = targets["applicant"]
    assert entry["status"] == "conflict"
    assert entry["value"] is None
    found = conflicts.detect(all_targets)
    assert len(found) == 1
    ids = [c["rule_id"] for c in found[0]["candidates"]]
    assert ids == ["R01", "R02"]
    assert {c["value_display"] for c in found[0]["candidates"]} == {"张伟", " 张伟 "}
    assert found[0]["fix"]["rule_id"] == "R01"
    assert found[0]["fix"]["to_priority"] == 30


def test_raising_priority_resolves_conflict():
    base = [
        rule("R01", "applicant", "trim({{applicant}})", 20),
        rule("R02", "applicant", "upper({{applicant}})", 20),
    ]
    overrides = {"R01": {"priority": 40, "enabled": True}}
    effective = mapping.effective_rules(base, overrides)
    _, targets, all_targets = resolve(effective, DOC)
    assert targets["applicant"]["status"] == "ok"
    assert targets["applicant"]["chosen"]["rule_id"] == "R01"
    assert targets["applicant"]["value"] == "张伟"
    assert conflicts.detect(all_targets) == []


def test_expression_arithmetic_and_round2():
    rules = [rule("R01", "amount", "{{amount_total}}*{{tax_rate_factor}}", 10, ["round2"])]
    _, targets, _ = resolve(rules, DOC)
    assert targets["amount"]["value"] == Decimal("113.00")
    assert targets["amount"]["value_display"] == "113.00"


def test_expression_supports_src_prefix():
    rules = [rule("R01", "amount", "{{src.amount_total}}*2", 10, ["round2"])]
    _, targets, _ = resolve(rules, DOC)
    assert targets["amount"]["value"] == Decimal("200.00")


def test_transforms_upper_lower_trim():
    rules = [
        rule("R01", "applicant", "{{applicant}}", 10, ["trim"]),
        rule("R02", "currency", "cny", 10, ["upper"]),
    ]
    _, targets, _ = resolve(rules, DOC)
    assert targets["applicant"]["value"] == "张伟"
    assert targets["currency"]["value"] == "CNY"


def test_substr_transform():
    rules = [rule("R01", "cost_center", "substr({{cost_center}}, 0, 7)", 10)]
    _, targets, _ = resolve(rules, DOC)
    assert targets["cost_center"]["value"] == "CC-3301"


def test_constant_rule():
    rules = [rule("R01", "currency", "CNY", 10)]
    _, targets, _ = resolve(rules, DOC)
    assert targets["currency"]["value"] == "CNY"
    assert targets["currency"]["chosen"]["rule_id"] == "R01"


def test_optional_target_with_missing_source_is_untouched():
    rules = [rule("R08", "remark", "{{remark_text}}", 20)]
    results, targets, _ = resolve(rules, DOC)
    assert results[0]["status"] == "miss"
    assert "remark_text" in results[0]["reason"]
    assert targets["remark"]["status"] == "miss"
    assert mapping.stats(results)["rules_miss"] == 1


def test_unknown_function_is_reported_not_silently_used():
    rules = [rule("R01", "applicant", "cast({{applicant}})", 10)]
    results, targets, _ = resolve(rules, DOC)
    assert results[0]["status"] == "error"
    assert results[0]["code"] == "unknown_function"
    assert targets["applicant"]["status"] == "miss"


def test_required_target_without_rule_is_unmapped():
    _, targets, _ = resolve([], DOC)
    assert targets["supplier_name"]["status"] == "unmapped"
    assert targets["supplier_name"]["required"] is True


def test_sample_ruleset_has_two_conflict_groups():
    import json

    from app import settings

    base = json.loads(settings.RULESET_FILE.read_text(encoding="utf-8"))["rules"]
    results = mapping.evaluate_rules(base, {"amount_total": 1280.5, "tax_rate_factor": 1.13,
                                            "expense_class": "TRAVEL_FEE", "expense_type": "travel",
                                            "applicant": " 张伟 ", "dept_name": " 费用组 ",
                                            "occur_date": "2026-03-05", "cost_center": "CC-3301-01",
                                            "supplier_name": "云海", "urgent": True, "currency": "CNY",
                                            "remark": "备注"})
    targets = mapping.resolve_targets(results, contract.load_contract())
    found = conflicts.detect(targets)
    assert len(found) == 2
    assert {c["target"] for c in found} == {"amount", "expense_type"}
    assert mapping.stats(results)["rules_miss"] == 1   # R08 引用 remark_text，样例单据没有该字段


def test_tied_values_compared_numerically():
    rules = [
        rule("R01", "amount", "100", 10, ["round2"]),
        rule("R02", "amount", "100.0", 10, ["round2"]),
    ]
    _, targets, _ = resolve(rules, DOC)
    assert targets["amount"]["status"] == "ok"
    assert mapping.value_key(Decimal("1.0")) == mapping.value_key(1)
