"""字段校验：必填 / 日期真实性 / 枚举 / 金额 / 布尔 / 长度（核心规则二）。"""
from __future__ import annotations

import pytest

from app import contract, validation

CONTRACT = contract.load_contract()

VALID = {
    "doc_no": "CG-2026-9001",
    "version": 1,
    "applicant": "张伟",
    "dept_name": "财务共享中心-费用组",
    "expense_type": "travel",
    "amount": "1280.50",
    "occur_date": "2026-03-05",
    "cost_center": "CC-3301",
    "supplier_name": "北京云海商旅服务有限公司",
    "urgent": True,
    "currency": "CNY",
    "remark": "差旅报销",
}


def targets_for(values: dict, skip: tuple = (), status: dict | None = None):
    """按契约顺序构造 targets；未给值的字段状态为 unmapped，可显式指定状态。"""
    status = status or {}
    out = []
    for field in CONTRACT["fields"]:
        name = field["name"]
        present = name in values and name not in skip
        out.append(
            {
                "name": name,
                "label": field.get("label") or name,
                "type": field["type"],
                "required": bool(field.get("required")),
                "spec": field,
                "rules": [],
                "value": values.get(name) if present else None,
                "value_display": str(values.get(name)) if present else "—",
                "status": status.get(name, "ok" if present else "unmapped"),
                "chosen": None,
                "conflict": None,
            }
        )
    return out


def run(values, skip=(), status=None):
    return validation.validate(targets_for(values, skip, status), CONTRACT)


def codes(result):
    return [e["code"] for e in result["errors"]]


def test_valid_payload_passes_and_is_canonicalised():
    result = run(VALID)
    assert result["ok"] is True
    assert result["errors"] == []
    assert result["payload"]["amount"] == "1280.50"
    assert result["payload"]["version"] == 1
    assert result["payload"]["urgent"] is True


def test_required_field_missing_is_reported():
    result = run(VALID, skip=("supplier_name",))
    assert result["ok"] is False
    err = result["errors"][0]
    assert err["code"] == "missing_required"
    assert err["target"] == "supplier_name"
    assert err["label"] == "供应商"


def test_required_field_without_any_rule_is_reported():
    result = run(VALID, skip=("cost_center",), status={"cost_center": "unmapped"})
    assert codes(result) == ["missing_required"]
    assert "没有任何映射规则" in result["errors"][0]["message"]


def test_fake_calendar_date_is_rejected():
    result = run({**VALID, "occur_date": "2026-02-30"})
    assert codes(result) == ["invalid_date"]
    assert "2026-02-30" in result["errors"][0]["message"]


def test_leap_day_is_accepted():
    result = run({**VALID, "occur_date": "2024-02-29"})
    assert result["ok"] is True
    assert result["payload"]["occur_date"] == "2024-02-29"


def test_non_iso_date_format_is_rejected():
    result = run({**VALID, "occur_date": "2026/03/05"})
    assert codes(result) == ["invalid_date_format"]


def test_enum_out_of_range_lists_options():
    result = run({**VALID, "expense_type": "meal"})
    err = result["errors"][0]
    assert err["code"] == "enum_invalid"
    assert "meal" in err["message"]
    assert set(err["options"]) == {"traffic", "office", "travel", "entertain", "training"}


def test_amount_with_three_decimals_is_rejected():
    result = run({**VALID, "amount": "12.345"})
    assert codes(result) == ["amount_decimals"]


def test_amount_must_be_positive():
    assert codes(run({**VALID, "amount": "0"})) == ["amount_not_positive"]
    assert codes(run({**VALID, "amount": "-8.00"})) == ["amount_not_positive"]


def test_amount_over_limit_is_rejected():
    result = run({**VALID, "amount": "128000"})
    err = result["errors"][0]
    assert err["code"] == "amount_exceeds_max"
    assert err["max"] == "50000"


def test_amount_is_rounded_to_two_decimals_string():
    result = run({**VALID, "amount": 12.5})
    assert result["payload"]["amount"] == "12.50"


@pytest.mark.parametrize(
    "raw,expected",
    [(True, True), ("是", True), ("否", False), ("no", False), (1, True), (0, False), ("TRUE", True)],
)
def test_boolean_coercion(raw, expected):
    result = run({**VALID, "urgent": raw})
    assert result["payload"]["urgent"] is expected


def test_unrecognised_boolean_is_rejected():
    assert codes(run({**VALID, "urgent": "也许"})) == ["type_boolean"]


def test_max_length_is_enforced():
    result = run({**VALID, "applicant": "张" * 21})
    assert codes(result) == ["max_length"]


def test_version_must_be_integer():
    assert run({**VALID, "version": "2"})["payload"]["version"] == 2
    assert codes(run({**VALID, "version": "v2"})) == ["type_integer"]


def test_conflicted_target_blocks_with_candidates():
    targets = targets_for(VALID, skip=("amount",))
    for entry in targets:
        if entry["name"] == "amount":
            entry["status"] = "conflict"
            entry["conflict"] = {
                "target": "amount",
                "label": "金额（元）",
                "priority": 10,
                "candidates": [
                    {"rule_id": "R01", "expr": "{{amount_total}}*1.0", "priority": 10,
                     "value": 1280.5, "value_display": "1280.50"},
                    {"rule_id": "R02", "expr": "{{amount_total}}*1.13", "priority": 10,
                     "value": 1446.97, "value_display": "1446.97"},
                ],
            }
    result = validation.validate(targets, CONTRACT)
    assert codes(result) == ["conflict_unresolved"]
    assert len(result["errors"][0]["candidates"]) == 2
    assert "amount" not in result["payload"]


def test_optional_empty_field_is_not_an_error():
    result = run(VALID, skip=("remark", "currency"))
    assert result["ok"] is True
    assert set(result["optional_empty"]) == {"remark", "currency"}
