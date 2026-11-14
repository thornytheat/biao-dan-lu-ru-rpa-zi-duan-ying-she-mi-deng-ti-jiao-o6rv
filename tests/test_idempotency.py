"""幂等提交：键的规范化、replay、stale_version 409、升版落库（核心规则三）。"""
from __future__ import annotations

import json

import pytest

from app import idempotency, service, store

PAYLOAD_A = {"doc_no": "CG-2026-0001", "version": 1, "amount": "1280.50", "urgent": True}
PAYLOAD_B = {"urgent": True, "amount": "1280.50", "version": 1, "doc_no": "CG-2026-0001"}


def tweak_doc(doc_no: str, **changes) -> None:
    record = store.get_doc(doc_no)
    doc = {**record["doc"], **changes}
    with store.connect() as conn:
        conn.execute(
            "UPDATE source_docs SET payload_json = ? WHERE doc_no = ?",
            (json.dumps(doc, ensure_ascii=False), doc_no),
        )
        conn.commit()


def fix_conflicts() -> None:
    store.set_override("R01", 20, True)
    store.set_override("R03", 25, True)


def test_canonical_json_is_key_order_independent():
    assert idempotency.canonical_json(PAYLOAD_A) == idempotency.canonical_json(PAYLOAD_B)
    assert idempotency.payload_sha(PAYLOAD_A) == idempotency.payload_sha(PAYLOAD_B)
    assert idempotency.key("CG-2026-0001", 1, PAYLOAD_A) == idempotency.key("CG-2026-0001", 1, PAYLOAD_B)


def test_idempotency_key_changes_with_version_and_doc():
    base = idempotency.key("CG-2026-0001", 1, PAYLOAD_A)
    assert base != idempotency.key("CG-2026-0001", 2, PAYLOAD_A)
    assert base != idempotency.key("CG-2026-0002", 1, PAYLOAD_A)
    assert base != idempotency.key("CG-2026-0001", 1, {**PAYLOAD_A, "amount": "1280.51"})
    assert len(base) == 64


def test_rules_digest_tracks_priority():
    rules = service.load_ruleset()["rules"]
    digest = idempotency.rules_digest(rules)
    changed = [dict(r) for r in rules]
    changed[0]["priority"] = 99
    assert idempotency.rules_digest(changed) != digest


def test_dry_run_writes_no_business_records():
    run = service.dry_run("CG-2026-0001")
    assert run["submittable"] is False
    assert store.count_submissions() == 0
    assert store.list_audits() == []


def test_submit_then_replay_returns_existing_receipt():
    fix_conflicts()
    code, body = service.submit("CG-2026-0001", 1)
    assert code == 201
    assert body["replayed"] is False
    receipt_no = body["receipt"]["receipt_no"]
    assert receipt_no == "RCPT-CG-2026-0001-V1"
    assert store.count_submissions() == 1

    code2, body2 = service.submit("CG-2026-0001", 1)
    assert code2 == 200
    assert body2["replayed"] is True
    assert body2["receipt"]["receipt_no"] == receipt_no
    assert store.count_submissions() == 1
    actions = [a["action"] for a in store.list_audits()]
    assert actions[0] == "replay" and "submit" in actions


def test_payload_change_without_version_bump_is_409():
    fix_conflicts()
    assert service.submit("CG-2026-0001", 1)[0] == 201
    tweak_doc("CG-2026-0001", amount_total=1500.0)
    code, body = service.submit("CG-2026-0001", 1)
    assert code == 409
    assert body["code"] == "stale_version"
    assert body["latest_version"] == 1
    assert store.count_submissions() == 1
    assert store.list_audits()[0]["action"] == "stale_rejected"


def test_explicit_version_bump_creates_new_row_and_keeps_old():
    fix_conflicts()
    assert service.submit("CG-2026-0001", 1)[0] == 201
    tweak_doc("CG-2026-0001", amount_total=1500.0)
    code, body = service.submit("CG-2026-0001", 2)
    assert code == 201
    assert body["receipt"]["receipt_no"] == "RCPT-CG-2026-0001-V2"
    assert store.count_submissions() == 2
    versions = [s["version"] for s in store.list_submissions()]
    assert versions == [1, 2]
    old = store.find_doc_version("CG-2026-0001", 1)
    new = store.find_doc_version("CG-2026-0001", 2)
    assert old["payload"]["amount"] == "1280.50"
    assert new["payload"]["amount"] == "1500.00"
    chains = service.review_data()["chains"]
    assert [v["version"] for v in chains[0]["versions"]] == [1, 2]
    assert chains[0]["latest_version"] == 2


def test_version_regression_is_409():
    fix_conflicts()
    assert service.submit("CG-2026-0001", 1)[0] == 201
    tweak_doc("CG-2026-0001", amount_total=1500.0)
    assert service.submit("CG-2026-0001", 2)[0] == 201
    code, body = service.submit("CG-2026-0001", 1)
    assert code == 409
    assert "不是递增版本" in body["detail"]


def test_blocked_document_writes_nothing():
    code, body = service.submit("CG-2026-0001", 1)
    assert code == 422
    assert body["code"] == "dry_run_blocked"
    assert store.count_submissions() == 0
    assert store.list_audits() == []


def test_unknown_document_raises():
    with pytest.raises(service.DocNotFound):
        service.submit("CG-9999", 1)


def test_seed_is_idempotent():
    first = store.seed_docs()
    second = store.seed_docs()
    assert store.count_docs() == 3
    assert second["inserted"] == 0
    assert second["total"] == 3
