"""可恢复尝试：固定快照、超时锁单、认领回执、重试不新增回执、崩溃恢复、确认失败回滚。"""
from __future__ import annotations

import json
import threading

from fastapi.testclient import TestClient

from app import gateway, service, store
from app.main import app

client = TestClient(app)

DOC = "CG-2026-0001"
OTHER = "CG-2026-0002"


def fix_conflicts() -> None:
    store.set_override("R01", 20, True)
    store.set_override("R03", 25, True)


def open_attempts(doc_no: str = DOC) -> list[dict]:
    return [a for a in store.list_attempts() if a["doc_no"] == doc_no]


def test_attempt_pins_rules_digest_and_payload_snapshot():
    fix_conflicts()
    code, body = service.submit(DOC, 1)
    assert code == 201
    attempt = store.list_attempts()[0]
    run = service.build_run(DOC, 1)
    # 尝试行固定了规则快照、payload、幂等键：之后规则再变，重试仍发送这一份
    assert attempt["rules_digest"] == run["rules_digest"]
    assert attempt["idem_key"] == run["idempotency_key"]
    assert attempt["payload_sha"] == run["payload_sha"]
    assert attempt["receipt_no"] == "RCPT-CG-2026-0001-V1"
    assert attempt["status"] == "confirmed"


def test_timeout_marks_unknown_locks_doc_but_keeps_other_docs_working():
    fix_conflicts()
    gateway.configure_fault("timeout")
    code, body = service.submit(DOC, 1)
    assert code == 202
    assert body["code"] == "result_unknown"
    attempt = body["attempt"]
    assert attempt["status"] == "unknown"
    assert store.count_submissions() == 0  # 结果不明：不写正式回执

    # 同一单据被锁住：再点提交直接 423，不会生成第二张回执/第二次尝试
    code2, body2 = service.submit(DOC, 1)
    assert code2 == 423
    assert body2["code"] == "doc_locked"
    assert body2["attempt"]["attempt_no"] == attempt["attempt_no"]
    assert len(open_attempts()) == 1

    # HTTP 层同样锁单
    res = client.post("/api/submit", json={"doc_no": DOC, "version": 1})
    assert res.status_code == 423

    # 其他单据不受影响，照常可提交（先让其通过自身校验：CG-2026-0002 金额超限，改成合规金额）
    record = store.get_doc(OTHER)
    with store.connect() as conn:
        conn.execute(
            "UPDATE source_docs SET payload_json = ? WHERE doc_no = ?",
            (json.dumps({**record["doc"], "amount_total": 800.0}, ensure_ascii=False), OTHER),
        )
        conn.commit()
    ok = client.post("/api/submit", json={"doc_no": OTHER, "version": 1})
    assert ok.status_code == 201
    assert ok.json()["receipt"]["receipt_no"] == f"RCPT-{OTHER}-V1"


def test_unknown_retry_discovers_remote_receipt_and_confirms_without_duplicate():
    fix_conflicts()
    gateway.configure_fault("crash")  # 远端已写回执、本地确认前中断
    code, body = service.submit(DOC, 1)
    assert code == 202 and body["code"] == "result_unknown"
    attempt_no = body["attempt"]["attempt_no"]
    assert store.count_remote_receipts() == 1
    assert store.count_submissions() == 0

    # 重试：先按幂等键核对远端，认领到既有回执后确认；不新增第二张业务回执
    code2, body2 = service.retry_attempt(attempt_no)
    assert code2 == 200
    assert body2["receipt"]["receipt_no"] == "RCPT-CG-2026-0001-V1"
    assert store.count_submissions() == 1
    assert store.count_remote_receipts() == 1
    refreshed = store.get_attempt(attempt_no)
    assert refreshed["status"] == "confirmed"
    assert refreshed["retries"] >= 1

    # 再重试/再提交都只回放，不新增
    code3, body3 = service.retry_attempt(attempt_no)
    assert code3 == 200 and body3["replayed"] is True
    code4, body4 = service.submit(DOC, 1)
    assert code4 == 200 and body4["replayed"] is True
    assert store.count_submissions() == 1


def test_manual_claim_validates_idempotency_key():
    fix_conflicts()
    gateway.configure_fault("crash")
    code, body = service.submit(DOC, 1)
    attempt_no = body["attempt"]["attempt_no"]

    # 查无此回执 → 404，尝试保持待核对
    missing = service.claim_attempt(attempt_no, "RCPT-NOPE-9999")
    assert missing[0] == 404
    assert missing[1]["code"] == "receipt_not_found"
    assert store.get_attempt(attempt_no)["status"] == "unknown"

    # 正确回执号 → 认领确认
    ok = service.claim_attempt(attempt_no, "RCPT-CG-2026-0001-V1")
    assert ok[0] == 200
    assert ok[1]["receipt"]["receipt_no"] == "RCPT-CG-2026-0001-V1"
    assert store.count_submissions() == 1

    # 幂等键不一致的回执不能挂到本尝试上（构造一张别的单据的远端回执）
    assert service.claim_attempt(attempt_no, "RCPT-CG-2026-0001-V1")[1]["replayed"] is True


def test_claim_mismatch_is_rejected():
    fix_conflicts()
    gateway.configure_fault("timeout")
    code, body = service.submit(DOC, 1)
    attempt_no = body["attempt"]["attempt_no"]
    # 手工伪造一张幂等键不同的远端回执
    store.upsert_remote_receipt(
        receipt_no="RCPT-FAKE-V1", idem_key="x" * 64, doc_no=DOC, version=1, payload_sha="y" * 64
    )
    code2, body2 = service.claim_attempt(attempt_no, "RCPT-FAKE-V1")
    assert code2 == 409
    assert body2["code"] == "claim_mismatch"
    assert store.count_submissions() == 0
    assert store.get_attempt(attempt_no)["status"] == "unknown"  # 状态不被污染


def test_remote_error_is_failed_and_retry_uses_same_receipt_no():
    fix_conflicts()
    gateway.configure_fault("remote_error")
    code, body = service.submit(DOC, 1)
    assert code == 502
    assert body["code"] == "remote_rejected"
    attempt_no = body["attempt"]["attempt_no"]
    assert store.get_attempt(attempt_no)["status"] == "failed"
    assert store.count_submissions() == 0
    assert store.count_remote_receipts() == 0  # 明确失败：远端没有回执

    # 失败不锁单：再次提交应复用同一尝试走重试，而不是新建
    code2, body2 = service.submit(DOC, 1)  # 故障已一次性消耗，这次成功
    assert code2 == 201
    assert body2["receipt"]["receipt_no"] == "RCPT-CG-2026-0001-V1"
    assert len(open_attempts()) == 1  # 始终只有一个尝试、一张回执
    assert store.count_submissions() == 1
    actions = [a["action"] for a in store.list_audits()]
    assert "attempt_failed" in actions and "submit" in actions


def test_audit_trail_covers_every_transition():
    fix_conflicts()
    gateway.configure_fault("timeout")
    service.submit(DOC, 1)
    attempt_no = store.list_attempts()[0]["attempt_no"]
    # 两次重试都超时，第三次故障清除后成功
    gateway.configure_fault("timeout")
    service.retry_attempt(attempt_no)
    gateway.configure_fault("none")
    service.retry_attempt(attempt_no)
    actions = [a["action"] for a in store.list_audits()]
    assert "attempt_started" in actions
    assert actions.count("attempt_unknown") == 2
    assert "attempt_retry" in actions
    assert "attempt_confirmed" in actions
    assert "submit" in actions  # 确认时的正式审计
    # 每次尝试迁移都有审计，没有任何静默状态变化
    assert len(actions) >= 5


def test_stale_in_flight_attempt_is_recovered_to_unknown_on_startup():
    fix_conflicts()
    gateway.configure_fault("timeout")
    service.submit(DOC, 1)
    # 模拟进程崩溃：把 updated_at 改到很久以前，状态留在 in_flight
    with store.connect() as conn:
        conn.execute(
            "UPDATE submission_attempts SET status = 'in_flight', updated_at = '2000-01-01 00:00:00'"
        )
        conn.commit()
    recovered = service.recover_on_startup()
    assert len(recovered) == 1
    attempt = store.get_attempt(recovered[0])
    assert attempt["status"] == "unknown"
    assert store.open_attempt_for_doc(DOC)["attempt_no"] == attempt["attempt_no"]
    assert "attempt_recovered" in [a["action"] for a in store.list_audits()]


def test_confirm_failure_rolls_back_and_keeps_previous_receipt():
    fix_conflicts()
    # 先正常入账 V1
    assert service.submit(DOC, 1)[0] == 201
    first = store.get_submission("RCPT-CG-2026-0001-V1")
    assert first is not None
    submissions_before = store.count_submissions()

    # 构造一个待核对尝试：预占回执号与既有正式回执撞 UNIQUE（模拟确认切换时的冲突）
    conflicting_no = "ATT-CG-2026-0001-9999"
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO submission_attempts (attempt_no, doc_no, version, status, idem_key, "
            "receipt_no, run_id, rules_digest, payload_json, payload_sha, retries, last_error, "
            "submission_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,0,'',NULL,?,?)",
            (
                conflicting_no, DOC, 1, "unknown", "z" * 64,
                "RCPT-CG-2026-0001-V1", "run_x", "d" * 64,
                json.dumps({"doc_no": DOC, "version": 1}, ensure_ascii=False),
                "p" * 64, store.now(), store.now(),
            ),
        )
        conn.commit()

    # 远端没有该幂等键的回执 → 网关会实际送一次；确认时撞 receipt_no UNIQUE → 回滚
    code, body = service.retry_attempt(conflicting_no)
    assert code == 409
    assert body["code"] == "confirm_conflict"
    # 上一张已入账回执原样保留，没有被覆盖
    kept = store.get_submission("RCPT-CG-2026-0001-V1")
    assert kept["idem_key"] == first["idem_key"]
    assert store.count_submissions() == submissions_before
    # 尝试回退到待核对，仍可继续处理（不会卡死也不会丢审计）
    assert store.get_attempt(conflicting_no)["status"] == "unknown"
    assert "confirm_rolled_back" in [a["action"] for a in store.list_audits()]


def test_retry_never_creates_second_business_receipt_under_repeated_timeouts():
    fix_conflicts()
    gateway.configure_fault("timeout")
    code, body = service.submit(DOC, 1)
    attempt_no = body["attempt"]["attempt_no"]
    fixed_receipt = body["attempt"]["receipt_no"]
    for _ in range(3):
        gateway.configure_fault("timeout")
        code, body = service.retry_attempt(attempt_no)
        assert code == 202
        assert body["attempt"]["receipt_no"] == fixed_receipt
    gateway.configure_fault("none")
    code, body = service.retry_attempt(attempt_no)
    assert code in (200, 201)
    assert body["receipt"]["receipt_no"] == fixed_receipt
    assert store.count_submissions() == 1
    assert store.count_remote_receipts() == 1
    assert len(open_attempts()) == 1


def test_attempts_api_and_pages_share_one_status_source():
    fix_conflicts()
    gateway.configure_fault("timeout")
    client.post("/api/submit", json={"doc_no": DOC, "version": 1})

    api = client.get("/api/attempts").json()
    assert api["status_cards"][1]["status"] == "unknown"
    assert api["open_count"] == 1
    assert sum(c["count"] for c in api["status_cards"]) == 1

    page = client.get("/attempts").text
    assert "待核对" in page and "ATT-CG-2026-0001-0001" in page
    review = client.get("/review").text
    assert "待处理尝试" in review and "ATT-CG-2026-0001-0001" in review
    workbench = client.get("/workbench", params={"doc": DOC, "run": 1}).text
    assert "单据已锁定" in workbench

    health = client.get("/health").json()
    assert health["attempts"]["unknown"] == 1
    assert health["locked_docs"] == [DOC]


def test_fault_injection_endpoint_is_one_shot_and_validated():
    res = client.post("/api/dev/fault", json={"mode": "bogus"})
    assert res.status_code == 422
    client.post("/api/dev/fault", json={"mode": "timeout"})
    assert client.get("/api/dev/fault").json()["next_fault"] == "timeout"
    fix_conflicts()
    client.post("/api/submit", json={"doc_no": DOC, "version": 1})
    # 一次性：触发后自动复位
    assert client.get("/api/dev/fault").json()["next_fault"] == "none"


def test_form_retry_and_claim_paths_work_without_javascript():
    fix_conflicts()
    gateway.configure_fault("timeout")
    res = client.post(
        "/workbench/submit", data={"doc_no": DOC, "version": "1"}, follow_redirects=False
    )
    assert res.status_code == 303
    assert "/attempts?attempt=ATT-CG-2026-0001-0001" in res.headers["location"]

    # 无 JS 表单重试（先让远端能成功；timeout 模式下远端未写，故重置故障）
    gateway.reset_fault()
    retry = client.post(
        "/attempts/ATT-CG-2026-0001-0001/retry", follow_redirects=False
    )
    assert retry.status_code == 303
    assert retry.headers["location"].startswith("/review?receipt=RCPT-CG-2026-0001-V1")


def test_concurrent_submits_collapse_to_one_attempt_and_one_receipt():
    fix_conflicts()
    results = []

    def fire() -> None:
        local = TestClient(app)
        res = local.post("/api/submit", json={"doc_no": DOC, "version": 1})
        results.append(res.status_code)

    threads = [threading.Thread(target=fire) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 一个 201 新建；其余为 200 回放 / 423 锁单 / 202 并入待核对 / 409 版本守卫（时序相关，均安全）。
    # 关键不变量：只有一张正式回执，绝不出现第二张业务回执，也没有残留锁。
    assert sorted(results).count(201) == 1
    assert all(code in (200, 201, 202, 409, 423) for code in results)
    assert store.count_submissions() == 1
    assert store.get_doc_lock(DOC) is None
