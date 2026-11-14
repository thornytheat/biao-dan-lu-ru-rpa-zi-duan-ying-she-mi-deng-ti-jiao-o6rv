"""规则表达式求值（纯函数，无 eval）。

支持：`{{field}}` / `{{src.field}}` 占位、常量（`CNY`、`1.13`）、
函数 `upper/trim/lower/substr(v,start[,end])/round2`、
`+ - * / ( )` 四则运算（Decimal 实现，避免浮点误差）。
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

TOKEN = re.compile(r"\{\{\s*(?:src\.)?([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
VAR = re.compile(r"__V(\d+)__")
FUNC_HEAD = re.compile(r"^([a-z_][a-z0-9_]*)\((.*)\)$", re.S)
NUMERIC = re.compile(r"^-?\d+(?:\.\d+)?$")
ARITH = set("0123456789.+-*/() \t")
FUNCS = {"upper", "lower", "trim", "substr", "round2"}
TRANSFORMS = {"round2", "upper", "lower", "trim"}


class ExprError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _lookup(doc: dict, name: str):
    if not isinstance(doc, dict):
        raise ExprError("source_doc_invalid", f"源单据不是对象，无法取字段 {name}")
    if name not in doc or doc[name] is None:
        raise ExprError("source_field_missing", f"源单据没有字段 {name}")
    return doc[name]


def _decimal(value, name: str = "表达式") -> Decimal:
    if isinstance(value, bool):
        raise ExprError("not_numeric", f"{name} 得到布尔值，不能参与算术")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except InvalidOperation:
            raise ExprError("not_numeric", f"{name} 的值 “{value}” 不是数字，不能参与算术")
    raise ExprError("not_numeric", f"{name} 的值 {value!r} 不是数字，不能参与算术")


def _text(value) -> str:
    if isinstance(value, Decimal):
        text = format(value.normalize(), "f")
        return text
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class _Arithmetic:
    """递归下降：expr := term ((+|-) term)* ; term := factor ((*|/) factor)*。"""

    def __init__(self, source: str, values: list):
        self.src = source
        self.pos = 0
        self.values = values

    def run(self) -> Decimal:
        value = self.expr()
        self._skip()
        if self.pos < len(self.src):
            raise ExprError("expr_syntax", f"表达式在 {self.src[self.pos:]!r} 处无法解析")
        return value

    def _skip(self) -> None:
        while self.pos < len(self.src) and self.src[self.pos] in " \t":
            self.pos += 1

    def _peek(self) -> str:
        self._skip()
        return self.src[self.pos] if self.pos < len(self.src) else ""

    def expr(self) -> Decimal:
        value = self.term()
        while True:
            ch = self._peek()
            if not ch or ch not in "+-":
                return value
            self.pos += 1
            right = self.term()
            value = value + right if ch == "+" else value - right

    def term(self) -> Decimal:
        value = self.factor()
        while True:
            ch = self._peek()
            if not ch or ch not in "*/":
                return value
            self.pos += 1
            right = self.factor()
            if ch == "*":
                value = value * right
            else:
                if right == 0:
                    raise ExprError("expr_div_zero", "表达式中出现除以 0")
                value = value / right

    def factor(self) -> Decimal:
        ch = self._peek()
        if ch and ch in "+-":
            self.pos += 1
            inner = self.factor()
            return -inner if ch == "-" else inner
        if ch == "(":
            self.pos += 1
            value = self.expr()
            if self._peek() != ")":
                raise ExprError("expr_syntax", "表达式括号不匹配")
            self.pos += 1
            return value
        match = VAR.match(self.src, self.pos)
        if match:
            self.pos = match.end()
            return _decimal(self.values[int(match.group(1))], "占位字段")
        return self._number()

    def _number(self) -> Decimal:
        start = self.pos
        while self.pos < len(self.src) and self.src[self.pos] in "0123456789.":
            self.pos += 1
        text = self.src[start:self.pos]
        if not NUMERIC.match(text):
            raise ExprError("expr_syntax", f"表达式中的 {text!r} 不是合法数字")
        return Decimal(text)


def _split_args(raw: str) -> list[str]:
    out, buf, depth = [], "", 0
    for ch in raw:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(buf)
            buf = ""
        else:
            buf += ch
    out.append(buf)
    return [item.strip() for item in out]


def _arg_value(raw: str, values: list):
    raw = raw.strip()
    match = VAR.fullmatch(raw)
    if match:
        return values[int(match.group(1))]
    if NUMERIC.match(raw):
        return Decimal(raw)
    inner = FUNC_HEAD.match(raw)
    if inner:
        return _call(inner.group(1), inner.group(2), values)
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    if raw.startswith("{{") or raw.endswith("}}"):
        raise ExprError("expr_syntax", f"函数参数 {raw!r} 不完整")
    return raw


def _int_arg(raw: str, values: list, name: str) -> int:
    value = _arg_value(raw, values)
    dec = _decimal(value, name)
    if dec != dec.to_integral_value():
        raise ExprError("expr_syntax", f"{name} 需要整数，收到 {value!r}")
    return int(dec)


def _call(name: str, argtext: str, values: list):
    if name not in FUNCS:
        raise ExprError("unknown_function", f"不支持的函数 {name}()，可用：{', '.join(sorted(FUNCS))}")
    args = _split_args(argtext)
    if name in {"upper", "lower", "trim", "round2"}:
        if len(args) != 1:
            raise ExprError("expr_syntax", f"{name}() 需要 1 个参数，收到 {len(args)} 个")
        value = _arg_value(args[0], values)
        if name == "upper":
            return _text(value).upper()
        if name == "lower":
            return _text(value).lower()
        if name == "trim":
            return _text(value).strip()
        dec = _decimal(value, "round2()")
        return dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if len(args) not in (2, 3):
        raise ExprError("expr_syntax", f"substr() 需要 2 或 3 个参数，收到 {len(args)} 个")
    text = _text(_arg_value(args[0], values))
    start = _int_arg(args[1], values, "substr() 起始位置")
    if len(args) == 3:
        end = _int_arg(args[2], values, "substr() 结束位置")
        return text[start:end]
    return text[start:]


def _constant(text: str):
    raw = text.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    if NUMERIC.match(raw):
        return Decimal(raw)
    return raw


def evaluate(expr: str, doc: dict):
    """求值单条规则表达式；失败抛 ExprError（带可读原因）。"""
    source = (expr or "").strip()
    if not source:
        raise ExprError("expr_empty", "规则表达式为空")

    values: list = []

    def repl(match: re.Match) -> str:
        values.append(_lookup(doc, match.group(1)))
        return f"__V{len(values) - 1}__"

    text = TOKEN.sub(repl, source)
    if "{{" in text or "}}" in text:
        raise ExprError(
            "expr_syntax",
            f"占位写法无法识别：{source}（只支持 {{{{字段}}}} 或 {{{{src.字段}}}}）",
        )
    single = VAR.fullmatch(text)
    if single:
        return values[int(single.group(1))]

    if text.count("(") != text.count(")"):
        raise ExprError("expr_syntax", f"表达式括号不匹配：{source}")

    head = FUNC_HEAD.match(text)
    if head and head.group(1) in FUNCS:
        return _call(head.group(1), head.group(2), values)
    if head and head.group(1).isidentifier() and FUNC_HEAD.fullmatch(text):
        raise ExprError("unknown_function", f"不支持的函数 {head.group(1)}()，可用：{', '.join(sorted(FUNCS))}")

    residue = VAR.sub("", text)
    if residue and all(ch in ARITH for ch in residue):
        return _Arithmetic(text, values).run()
    if values:
        return VAR.sub(lambda m: _text(values[int(m.group(1))]), text)
    return _constant(source)


def apply_transforms(value, names) -> object:
    """按 transform 列表顺序加工（round2/upper/lower/trim）。"""
    out = value
    for name in names or []:
        if name not in TRANSFORMS:
            raise ExprError("unknown_transform", f"不支持的 transform {name!r}，可用：{', '.join(sorted(TRANSFORMS))}")
        if name == "round2":
            out = _decimal(out, "round2 变换").quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        elif name == "upper":
            out = _text(out).upper()
        elif name == "lower":
            out = _text(out).lower()
        else:
            out = _text(out).strip()
    return out
