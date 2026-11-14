"""幂等键与回执号：幂等键 = sha256(doc_no + version + 规范化 payload)。"""
from __future__ import annotations

import hashlib
import json


def canonical_json(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def payload_sha(payload: dict) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def key(doc_no: str, version: int, payload: dict) -> str:
    material = f"{doc_no}|{int(version)}|{canonical_json(payload)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def rules_digest(rules: list[dict]) -> str:
    material = canonical_json(
        [
            {
                "id": r["id"],
                "target": r["target"],
                "expr": r.get("expr", ""),
                "priority": int(r.get("priority", 0)),
                "transform": list(r.get("transform") or []),
            }
            for r in sorted(rules, key=lambda r: r["id"])
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def run_id(doc_no: str, version: int, digest: str) -> str:
    material = f"{doc_no}|{int(version)}|{digest}"
    return "run_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def receipt_no(doc_no: str, version: int) -> str:
    return f"RCPT-{doc_no}-V{int(version)}"


def short(value: str, length: int = 12) -> str:
    return (value or "")[:length]
