"""HTTP 层：页面可访问、错误输入 4xx、dry-run → 提交 → replay → 409 → 升版 端到端。"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import service, store
from app.main import app

client = TestClient(app)


def test_health_and_pages():
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["docs"] == 3

    index = client.get("/")
    assert index.status_code == 200
    assert "CG-2026-0001" in index.text
    assert "演示步骤" in index.text

    workbench = client.get("/workbench", params={"doc": "CG-2026-0001", "run": 1})
    assert workbench.status_code == 200
    assert "R01" in workbench.text and "同优先级冲突" in workbench.text

    review = client.get("/review")
    assert review.status_code == 200


def test_dry_run_returns_conflicts_and_errors():
    res = client.post("/api/dry-run", json={"doc_no": "CG-2026-0001"})
    assert res.status_code == 200
    body = res.json()
    assert body["conflict_count"] == 2
    assert body["error_count"] == 2
    assert body["submittable"] is False
    assert body["result_url"].startswith("/result/run_")
    assert len(body["idempotency_key"]) == 64
    assert {e["code"] for e in body["errors"]} == {"conflict_unresolved"}


def test_dry_run_bad_inputs():
    assert client.post("/api/dry-run", json={}).status_code == 400
    assert client.post("/api/dry-run", json={"doc_no": "CG-NOPE"}).status_code == 404
    assert client.post("/api/dry-run", json={"doc_no": "CG-2026-0001", "version": "abc"}).status_code == 422
    assert client.post("/api/dry-run", json={"doc_no": "CG-2026-0001", "version": 0}).status_code == 422


def test_result_page_404_for_unknown_run():
    res = client.get("/result/run_deadbeef0000")
    assert res.status_code == 404
    assert "找不到这个预览" in res.text


def test_blocked_submit_is_422_and_writes_nothing():
    res = client.post("/api/submit", json={"doc_no": "CG-2026-0001", "version": 1})
    assert res.status_code == 422
    body = res.json()
    assert body["code"] == "dry_run_blocked"
    assert len(body["conflicts"]) == 2
    assert store.count_submissions() == 0


def test_doc_with_amount_and_enum_problems():
    amount_res = client.post("/api/dry-run", json={"doc_no": "CG-2026-0002"}).json()
    assert [e["code"] for e in amount_res["errors"]] == ["amount_exceeds_max"]
    enum_res = client.post("/api/dry-run", json={"doc_no": "CG-2026-0003"}).json()
    assert [e["code"] for e in enum_res["errors"]] == ["enum_invalid"]
    assert "travel" in enum_res["errors"][0]["options"]


def test_rule_priority_api_validates_rule():
    assert client.post("/api/rules/priority", json={"rule_id": "R99", "priority": 10}).status_code == 404
    assert client.post("/api/rules/priority", json={"rule_id": "R01", "priority": "x"}).status_code == 422
    ok = client.post("/api/rules/priority", json={"rule_id": "R01", "priority": 20})
    assert ok.status_code == 200
    assert ok.json()["overrides"]["R01"]["priority"] == 20
    reset = client.post("/api/rules/reset")
    assert reset.status_code == 200 and reset.json()["removed"] >= 1


def test_end_to_end_dry_run_submit_replay_409_bump():
    # 1) 初始 dry-run：2 冲突 + 2 校验错误
    first = client.post("/api/dry-run", json={"doc_no": "CG-2026-0001"}).json()
    assert first["conflict_count"] == 2 and first["submittable"] is False

    # 2) 按建议提高优先级（R01 → 20，R03 → 25）
    assert client.post("/api/rules/priority", json={"rule_id": "R01", "priority": 20}).status_code == 200
    assert client.post("/api/rules/priority", json={"rule_id": "R03", "priority": 25}).status_code == 200

    # 3) 重跑：冲突消除，可提交
    second = client.post("/api/dry-run", json={"doc_no": "CG-2026-0001"}).json()
    assert second["conflict_count"] == 0 and second["error_count"] == 0
    assert second["submittable"] is True
    assert second["payload"]["amount"] == "1280.50"
    assert second["payload"]["expense_type"] == "travel"
    run_id = second["run_id"]
    result_page = client.get(f"/result/{run_id}")
    assert result_page.status_code == 200
    assert "幂等键" in result_page.text

    # 4) 提交 → 201；重复提交 → 200 且 replayed=true
    submitted = client.post("/api/submit", json={"doc_no": "CG-2026-0001", "version": 1})
    assert submitted.status_code == 201
    assert submitted.json()["replayed"] is False
    receipt_no = submitted.json()["receipt"]["receipt_no"]
    assert receipt_no == "RCPT-CG-2026-0001-V1"
    assert store.count_submissions() == 1

    replayed = client.post("/api/submit", json={"doc_no": "CG-2026-0001", "version": 1})
    assert replayed.status_code == 200
    assert replayed.json()["replayed"] is True
    assert replayed.json()["receipt"]["receipt_no"] == receipt_no
    assert store.count_submissions() == 1

    # 5) 同号改金额不升版 → 409 stale_version
    record = store.get_doc("CG-2026-0001")
    with store.connect() as conn:
        conn.execute(
            "UPDATE source_docs SET payload_json = ? WHERE doc_no = ?",
            (json.dumps({**record["doc"], "amount_total": 1500.0}, ensure_ascii=False), "CG-2026-0001"),
        )
        conn.commit()
    stale = client.post("/api/submit", json={"doc_no": "CG-2026-0001", "version": 1})
    assert stale.status_code == 409
    assert stale.json()["code"] == "stale_version"
    assert store.count_submissions() == 1

    # 6) 显式升版 → 201，并保留旧版本
    bumped = client.post("/api/submit", json={"doc_no": "CG-2026-0001", "version": 2})
    assert bumped.status_code == 201
    assert bumped.json()["receipt"]["receipt_no"] == "RCPT-CG-2026-0001-V2"
    assert store.count_submissions() == 2

    review = client.get("/review", params={"receipt": receipt_no})
    assert review.status_code == 200
    assert "RCPT-CG-2026-0001-V1" in review.text
    assert "RCPT-CG-2026-0001-V2" in review.text
    assert "replayed" in review.text
    chain_page = client.get("/review").text
    assert "版本链" in chain_page


def test_form_submit_path_works_without_javascript():
    """无 JS 兜底：表单 POST 成功 303 跳转；失败回到工作台并显示原因。"""
    client.post("/api/rules/priority", json={"rule_id": "R01", "priority": 20})
    client.post("/api/rules/priority", json={"rule_id": "R03", "priority": 25})
    ok = client.post(
        "/workbench/submit", data={"doc_no": "CG-2026-0001", "version": "1"}, follow_redirects=False
    )
    assert ok.status_code == 303
    assert ok.headers["location"].startswith("/review?receipt=RCPT-CG-2026-0001-V1")

    client.post("/api/rules/reset")
    blocked = client.post(
        "/workbench/submit", data={"doc_no": "CG-2026-0001", "version": "1"}, follow_redirects=False
    )
    assert blocked.status_code == 422
    assert "提交被拒绝" in blocked.text


def test_rules_page_shows_missed_rule():
    page = client.get("/workbench", params={"doc": "CG-2026-0001", "run": 1}).text
    assert "未命中" in page
    assert "remark_text" in page
    api = client.get("/api/rules").json()
    assert len(api["rules"]) == 18
    assert any(r["target"] == "amount" and r["priority"] == 10 for r in api["rules"])
