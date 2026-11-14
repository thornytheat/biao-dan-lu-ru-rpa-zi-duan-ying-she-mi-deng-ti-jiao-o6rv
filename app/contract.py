"""目标表单契约：字段类型、必填、枚举、长度，以及类型/业务校验原语。

校验是纯函数：coerce(spec, raw) -> (ok, canonical_value, code, message)。
canonical_value 是可直接 JSON 序列化的目标值（金额为两位小数字符串、日期为 ISO）。
"""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import lru_cache
from typing import Any

from . import settings

BOOL_TRUE = {"1", "true", "t", "yes", "y", "是", "对", "on"}
BOOL_FALSE = {"0", "false", "f", "no", "n", "否", "不", "off"}


class ContractError(RuntimeError):
    pass


def _normalize_options(raw: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in raw or []:
        if isinstance(item, dict):
            value = str(item.get("value", "")).strip()
            out.append({"value": value, "label": str(item.get("label") or value)})
        else:
            text = str(item).strip()
            out.append({"value": text, "label": text})
    return out


@lru_cache(maxsize=4)
def _load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    fields = []
    for spec in data.get("fields", []):
        item = dict(spec)
        item["options"] = _normalize_options(item.get("options"))
        fields.append(item)
    data["fields"] = fields
    data["by_name"] = {f["name"]: f for f in fields}
    return data


def load_contract() -> dict:
    return _load(str(settings.CONTRACT_FILE))


def field_specs(contract: dict | None = None) -> list[dict]:
    return list((contract or load_contract())["fields"])


def spec(contract: dict, name: str) -> dict:
    return contract["by_name"][name]


def option_values(field: dict) -> list[str]:
    return [o["value"] for o in field.get("options") or []]


def option_labels(field: dict) -> str:
    return "、".join(f'{o["label"]}({o["value"]})' for o in field.get("options") or [])


def _err(code: str, message: str, **extra) -> tuple:
    return (False, None, code, message, extra)


def _ok(value) -> tuple:
    return (True, value, "", "", {})


def _to_decimal(raw: Any) -> Decimal | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, int):
        return Decimal(raw)
    if isinstance(raw, float):
        return Decimal(str(raw))
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            return Decimal(text)
        except InvalidOperation:
            return None
    return None


def coerce(field: dict, raw: Any) -> tuple:
    """把源值按目标契约转成规范化值；返回 (ok, value, code, message, extra)。"""
    kind = field.get("type")
    label = field.get("label") or field.get("name")

    if kind == "string":
        if isinstance(raw, str):
            text = raw
        elif isinstance(raw, (int, float, Decimal)) and not isinstance(raw, bool):
            text = str(raw)
        else:
            return _err("type_string", f"{label} 需要文本，实际是 {type(raw).__name__}")
        limit = field.get("max_length")
        if limit and len(text) > int(limit):
            return _err(
                "max_length",
                f"{label} 长度 {len(text)} 超过上限 {limit}",
                length=len(text), max_length=int(limit),
            )
        return _ok(text)

    if kind == "integer":
        if isinstance(raw, bool):
            return _err("type_integer", f"{label} 需要整数，收到布尔值")
        if isinstance(raw, int):
            value = raw
        elif isinstance(raw, float) and float(raw).is_integer():
            value = int(raw)
        elif isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
            value = int(raw.strip())
        else:
            return _err("type_integer", f"{label} 需要整数，收到 {raw!r}")
        low = field.get("min")
        if low is not None and value < int(low):
            return _err("out_of_range", f"{label} 不能小于 {low}，收到 {value}")
        return _ok(value)

    if kind == "amount":
        dec = _to_decimal(raw)
        if dec is None:
            return _err("type_amount", f"{label} 需要数字，收到 {raw!r}")
        if dec.as_tuple().exponent < -int(field.get("decimals", 2)):
            return _err(
                "amount_decimals",
                f"{label} 最多两位小数，收到 {raw!r}",
                got=str(dec),
            )
        if dec <= 0:
            return _err("amount_not_positive", f"{label} 必须大于 0，收到 {dec}")
        upper = field.get("max")
        if upper is not None and dec > Decimal(str(upper)):
            return _err(
                "amount_exceeds_max",
                f"{label} {dec} 超过单笔上限 {upper}",
                got=str(dec), max=str(upper),
            )
        return _ok(str(dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)))

    if kind == "date":
        if not isinstance(raw, str):
            return _err("type_date", f"{label} 需要 ISO 日期文本（YYYY-MM-DD），收到 {raw!r}")
        text = raw.strip()
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            if len(text) == 10 and text[4] == "-" and text[7] == "-":
                return _err("invalid_date", f"{label} 的日期 {text} 不存在（不是真实日历日期）")
            return _err("invalid_date_format", f"{label} 需要 YYYY-MM-DD 格式，收到 {text!r}")
        return _ok(parsed.strftime("%Y-%m-%d"))

    if kind == "enum":
        if not isinstance(raw, str):
            return _err("type_enum", f"{label} 需要枚举文本，收到 {raw!r}", options=option_values(field))
        text = raw.strip()
        allowed = option_values(field)
        if text not in allowed:
            return _err(
                "enum_invalid",
                f"{label} 的值 “{text}” 不在可选范围内",
                got=text, options=allowed, option_labels=option_labels(field),
            )
        return _ok(text)

    if kind == "boolean":
        if isinstance(raw, bool):
            return _ok(raw)
        if isinstance(raw, (int, float, Decimal)) and not isinstance(raw, bool):
            if Decimal(str(raw)) == 1:
                return _ok(True)
            if Decimal(str(raw)) == 0:
                return _ok(False)
            return _err("type_boolean", f"{label} 需要布尔值，收到 {raw!r}")
        if isinstance(raw, str):
            text = raw.strip().lower()
            if text in BOOL_TRUE:
                return _ok(True)
            if text in BOOL_FALSE:
                return _ok(False)
            return _err("type_boolean", f"{label} 无法识别为布尔值：{raw!r}")
        return _err("type_boolean", f"{label} 需要布尔值，收到 {raw!r}")

    raise ContractError(f"目标契约里出现未知类型 {kind!r}（字段 {field.get('name')}）")


def display(field: dict, value: Any) -> str:
    """给页面用的展示值。"""
    if value is None:
        return "—"
    if field.get("type") == "boolean":
        return "是" if value else "否"
    if field.get("type") == "enum":
        for opt in field.get("options") or []:
            if opt["value"] == value:
                return f'{opt["label"]}（{value}）'
    return str(value)
