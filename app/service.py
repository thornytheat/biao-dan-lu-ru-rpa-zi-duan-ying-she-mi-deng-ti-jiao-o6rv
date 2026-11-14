"""业务编排：dry-run 预览与可恢复幂等提交。路由只做参数解析与渲染。"""
from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

from . import conflicts as conflicts_mod
from . import contract as contract_mod
from . import gateway, idempotency, mapping, settings, store, validation

# 尝试状态的统一展示口径：页面、统计、API 全部从这里取，避免各端各写一套文案。
STATUS_LABELS = {
    "in_flight": "进行中",
    "unknown": "待核对",
    "failed": "明确失败",
    "confirmed": "已确认入账",
}
STATUS_TAGS = {
    "in_flight": "warn",
    "unknown": "err",
    "failed": "err",
    "confirmed": "ok",
}


class DocNotFound(LookupError):
    def __init__(self, doc_no: str):
        super().__init__(f"源单据 {doc_no} 不存在（样例数据只有 3 张单据）")
        self.doc_no = doc_no


def _json_safe(obj):
    if isinstance(obj, Decimal):
        return format(obj, "f")
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def load_ruleset() -> dict:
    with open(settings.RULESET_FILE, "r", encoding="utf-8") as fh:
        return json.load(fh)


def effective_rules() -> list[dict]:
    base = load_ruleset()["rules"]
    return mapping.effective_rules(base, store.list_overrides())


def _inject_identity(targets: list[dict], doc_no: str, version: int) -> None:
    for entry in targets:
        if entry["name"] == "doc_no":
            value, note = doc_no, "单据身份字段：源单据 doc_no，直接注入"
        elif entry["name"] == "version":
            value, note = version, "单据身份字段：本次提交的 version，直接注入"
        else:
            continue
        synthetic = {
            "rule_id": "ID",
            "target": entry["name"],
            "expr": note,
            "priority": 0,
            "base_priority": 0,
            "transform": [],
            "note": note,
            "status": "ok",
            "value": value,
            "value_display": mapping.format_value(value),
            "code": "",
            "reason": "",
        }
        entry.update(
            rules=[synthetic],
            value=value,
            value_display=mapping.format_value(value),
            status="ok",
            conflict=None,
            chosen={"rule_id": "ID", "expr": note, "priority": 0, "overridden": []},
        )


def build_run(doc_no: str, version: int | None = None) -> dict:
    """纯计算：源单据 + 规则 + 契约 → 映射/冲突/校验/幂等键。不写任何表。"""
    record = store.get_doc(doc_no)
    if record is None:
        raise DocNotFound(doc_no)
    doc = record["doc"]
    req_version = int(version if version is not None else doc.get("version", 1))

    contract = contract_mod.load_contract()
    ruleset = load_ruleset()
    rules = effective_rules()
    digest = idempotency.rules_digest(rules)

    rule_results = mapping.evaluate_rules(rules, doc)
    targets = mapping.resolve_targets(rule_results, contract)
    _inject_identity(targets, doc_no, req_version)

    conflict_list = conflicts_mod.detect(targets)
    check = validation.validate(targets, contract)
    payload = check["payload"]
    psha = idempotency.payload_sha(payload)
    idem_key = idempotency.key(doc_no, req_version, payload)

    run = {
        "run_id": idempotency.run_id(doc_no, req_version, digest + ":" + psha),
        "rules_digest": digest,
        "doc_no": doc_no,
        "version": req_version,
        "doc_version": int(doc.get("version", 1)),
        "doc": doc,
        "doc_fields": [
            {"name": key, "value_display": mapping.format_value(value)} for key, value in doc.items()
        ],
        "ruleset": ruleset.get("ruleset", ""),
        "contract_title": contract.get("form_title", ""),
        "contract_version": contract.get("contract_version", ""),
        "rule_results": rule_results,
        "targets": targets,
        "conflicts": conflict_list,
        "errors": check["errors"],
        "error_groups": validation.group_by_code(check["errors"]),
        "optional_empty": check["optional_empty"],
        "payload": payload,
        "payload_sha": psha,
        "idempotency_key": idem_key,
        "idempotency_key_short": idempotency.short(idem_key, 16),
        "stats": mapping.stats(rule_results),
        "submittable": not conflict_list and not check["errors"],
        "created_at": store.now(),
    }
    run["stats"].update(
        {
            "conflict_count": len(conflict_list),
            "error_count": len(check["errors"]),
            "target_count": len(targets),
            "mapped_count": sum(1 for t in targets if t["status"] == "ok"),
        }
    )
    return _json_safe(run)


def dry_run(doc_no: str, version: int | None = None, persist: bool = True) -> dict:
    """dry-run：不写 submissions / audit_events，只缓存预览快照供 /result/{run_id} 使用。"""
    run = build_run(doc_no, version)
    if persist:
        store.save_preview(run)
    return run


def _first_problem(run: dict) -> str:
    if run["conflicts"]:
        return run["conflicts"][0]["summary"]
    if run["errors"]:
        return run["errors"][0]["message"]
    return ""


def _receipt(submission: dict, replayed: bool, attempt_no: str | None = None) -> dict:
    receipt = dict(submission)
    receipt["idem_key_short"] = idempotency.short(submission["idem_key"], 16)
    receipt["replayed"] = replayed
    receipt["attempt_no"] = attempt_no
    return receipt


def _attempt_view(attempt: dict) -> dict:
    """尝试的对外视图：状态文案与标签与页面/统计保持同一口径。"""
    view = dict(attempt)
    view["status_label"] = STATUS_LABELS[attempt["status"]]
    view["status_tag"] = STATUS_TAGS[attempt["status"]]
    view["idem_key_short"] = idempotency.short(attempt["idem_key"], 16)
    view["payload_sha_short"] = idempotency.short(attempt["payload_sha"], 12)
    view["rules_digest_short"] = idempotency.short(attempt["rules_digest"], 12)
    return view


class DocLocked(RuntimeError):
    def __init__(self, attempt: dict):
        super().__init__(
            f"单据 {attempt['doc_no']} 存在{STATUS_LABELS[attempt['status']]}尝试 {attempt['attempt_no']}，"
            "已锁定：请先核对并处理该尝试（重试或认领回执），其他单据不受影响"
        )
        self.attempt = attempt


class AttemptNotFound(LookupError):
    pass


def _stale_check(doc_no: str, req_version: int, sha: str) -> tuple[int, dict] | None:
    """版本链守卫：版本回退 / 同版本 payload 变化未升版 → 409。返回 None 表示放行。"""
    latest = store.latest_version(doc_no)
    if latest is None:
        return None
    if req_version < latest:
        store.add_audit(
            "stale_rejected", doc_no, req_version,
            f"版本回退：最新已提交版本 v{latest}，收到 v{req_version}",
        )
        return 409, {
            "code": "stale_version",
            "detail": f"单据 {doc_no} 最新已提交版本为 v{latest}，本次提交 v{req_version} 不是递增版本，已拒绝",
            "latest_version": latest,
        }
    if req_version == latest:
        old = store.find_doc_version(doc_no, req_version) or {}
        store.add_audit(
            "stale_rejected", doc_no, req_version,
            f"同版本 payload 变化但未升版：已存 {old.get('payload_sha', '')[:12]}，本次 {sha[:12]}",
        )
        return 409, {
            "code": "stale_version",
            "detail": (
                f"单据 {doc_no} 的 v{req_version} 已提交且 payload 内容不同；"
                "payload 变化时必须显式升版（version+1）后再提交，避免覆盖已入账数据"
            ),
            "latest_version": latest,
            "latest_payload_sha": old.get("payload_sha", ""),
            "payload_sha": sha,
        }
    return None


def _confirmed_response(attempt: dict, submission: dict, *, replayed: bool, detail: str,
                        run_id: str, extra_status: int | None = None) -> tuple[int, dict]:
    return extra_status or (200 if replayed else 201), {
        "replayed": replayed,
        "detail": detail,
        "receipt": _receipt(submission, replayed, attempt["attempt_no"]),
        "attempt": _attempt_view(attempt),
        "run_id": run_id,
    }


def submit(doc_no: str, version: int | None = None) -> tuple[int, dict]:
    """可恢复提交：固定规则版本与表单内容 → 建尝试 → 送远端 → 单事务确认。

    返回 (HTTP 状态码, 响应体)：
    201 新建确认 / 200 幂等回放 / 202 结果不明待核对（锁单）/ 409 stale_version /
    422 校验未通过 / 423 单据被在途/待核对尝试锁住 / 502 远端明确失败（可重试）。
    """
    record = store.get_doc(doc_no)
    if record is None:
        raise DocNotFound(doc_no)
    doc = record["doc"]
    req_version = int(version if version is not None else doc.get("version", 1))

    # 锁单检查：以 doc_locks 为准（与建尝试的临界区同一张表，避免检查窗口不一致）。
    lock_row = store.get_doc_lock(doc_no)
    if lock_row is not None:
        locked = store.get_attempt(lock_row["attempt_no"]) or store.open_attempt_for_doc(doc_no)
        if locked is not None:
            return 423, {
                "code": "doc_locked",
                "detail": str(DocLocked(locked)),
                "attempt": _attempt_view(locked),
                "retry_url": f"/api/attempts/{locked['attempt_no']}/retry",
                "claim_url": f"/api/attempts/{locked['attempt_no']}/claim",
            }
        # 锁行残留但尝试缺失（异常态）：清理孤儿锁后放行，避免单据被永久锁死
        store.release_doc_lock(doc_no)

    # 纯计算：此刻固定规则快照（rules_digest）与表单内容（payload/idem_key）。
    run = build_run(doc_no, req_version)
    if run["conflicts"] or run["errors"]:
        return 422, {
            "code": "dry_run_blocked",
            "detail": f"映射/校验未通过，未写入任何记录：{_first_problem(run)}",
            "conflicts": run["conflicts"],
            "errors": run["errors"],
            "run_id": run["run_id"],
        }

    payload = run["payload"]
    idem_key = run["idempotency_key"]
    sha = run["payload_sha"]
    receipt_no = idempotency.receipt_no(doc_no, req_version)

    # 幂等命中：正式回执已存在 → 回放，不新增任何业务记录。
    existing = store.find_by_key(idem_key)
    if existing:
        store.add_audit(
            "replay", doc_no, req_version,
            f"命中既有幂等键 {idempotency.short(idem_key, 16)}，返回回执 {existing['receipt_no']}，不新增记录",
            existing,
        )
        return _confirmed_response(
            _attested_attempt(existing), existing, replayed=True,
            detail=f"幂等命中：单据 {doc_no} v{req_version} 与回执 {existing['receipt_no']} 内容一致，未新增记录",
            run_id=run["run_id"], extra_status=200,
        )

    # 版本链守卫（回退 / 同版本改内容未升版）。
    stale = _stale_check(doc_no, req_version, sha)
    if stale is not None:
        code, body = stale
        body["run_id"] = run["run_id"]
        return code, body

    # 同一幂等键已有尝试（比如失败后再次点击）：直接走恢复路径，绝不新建第二张回执。
    prior = store.find_attempt_by_key(idem_key)
    if prior is not None and prior["status"] in ("failed", "unknown"):
        return retry_attempt(prior["attempt_no"])

    # ① 固定快照并创建尝试（in_flight）。回执号此刻确定，重试多少次都不变。
    try:
        attempt = store.insert_attempt(
            doc_no=doc_no, version=req_version, idem_key=idem_key, receipt_no=receipt_no,
            run_id=run["run_id"], rules_digest=run["rules_digest"], payload=payload, payload_sha=sha,
        )
    except store.DocAlreadyLocked:
        # 并发提交：另一个请求已持锁，转为标准 423 锁单响应
        locked = store.open_attempt_for_doc(doc_no)
        return 423, {
            "code": "doc_locked",
            "detail": str(DocLocked(locked)) if locked else f"单据 {doc_no} 正被另一个提交尝试锁定",
            "attempt": _attempt_view(locked) if locked else None,
            "retry_url": f"/api/attempts/{locked['attempt_no']}/retry" if locked else None,
            "claim_url": f"/api/attempts/{locked['attempt_no']}/claim" if locked else None,
        }
    store.add_attempt_audit(
        attempt, "attempt_started",
        f"创建尝试 {attempt['attempt_no']}，固定规则快照 {idempotency.short(run['rules_digest'], 12)} "
        f"与表单内容 {sha[:12]}，回执号预占 {receipt_no}",
    )
    return _drive_attempt(attempt, run_id=run["run_id"])


def _attested_attempt(submission: dict) -> dict:
    """正式回执对应的 confirmed 尝试（兼容旧数据：没有尝试行时合成一个只读视图）。"""
    attempt = store.find_attempt_by_key(submission["idem_key"])
    if attempt is not None:
        return attempt
    return {
        "attempt_no": f"ATT-{submission['receipt_no']}",
        "doc_no": submission["doc_no"],
        "version": submission["version"],
        "status": "confirmed",
        "idem_key": submission["idem_key"],
        "receipt_no": submission["receipt_no"],
        "run_id": "",
        "rules_digest": "",
        "payload": submission["payload"],
        "payload_sha": submission["payload_sha"],
        "retries": 0,
        "last_error": "",
        "submission_id": submission["id"],
        "created_at": submission["created_at"],
        "updated_at": submission["created_at"],
    }


def _earlier_open_peer(attempt: dict) -> dict | None:
    """找同幂等键、更早创建、仍处于在途/待核对或已确认的其他尝试（并发收敛用）。"""
    for other in store.list_doc_attempts(attempt["doc_no"]):
        if other["attempt_no"] == attempt["attempt_no"]:
            continue
        if other["idem_key"] != attempt["idem_key"]:
            continue
        if other["status"] in ("in_flight", "unknown", "confirmed"):
            return other
    return None


def _drive_attempt(attempt: dict, *, run_id: str | None = None) -> tuple[int, dict]:
    """把一次尝试送达远端并按结果迁移状态。全程只使用尝试创建时固定的快照。"""
    run_id = run_id or attempt["run_id"]
    idem_key = attempt["idem_key"]
    short_key = idempotency.short(idem_key, 16)

    # 重发前先核对：本地/远端若已有同幂等键回执，直接认领确认，不产生第二张业务回执。
    local = store.find_by_key(idem_key)
    if local is not None:
        store.add_attempt_audit(
            attempt, "replay",
            f"重试前核对发现本地已有回执 {local['receipt_no']}（幂等键 {short_key}），直接回放不新增记录",
        )
        confirmed = _sync_confirmed_attempt(attempt, local)
        return _confirmed_response(
            confirmed, local, replayed=True,
            detail=f"重试前核对命中既有回执 {local['receipt_no']}，未新增记录", run_id=run_id,
            extra_status=200,
        )
    remote = gateway.find_remote_receipt(idem_key)
    if remote is not None:
        return _claim_remote(attempt, remote.as_dict(), run_id, via="retry_reconcile", replayed=True)

    # 并发同伴核对：同幂等键的更早尝试若已确认则回放；若仍在途/待核对则随它锁定，不重复送达。
    peer = _earlier_open_peer(attempt)
    if peer is not None:
        if peer["status"] == "confirmed":
            submission = store.get_submission(peer["receipt_no"])
            if submission is not None:
                store.add_attempt_audit(
                    attempt, "replay",
                    f"并发核对：更早的尝试 {peer['attempt_no']} 已确认回执 {peer['receipt_no']}，本尝试直接回放",
                )
                synced = _sync_confirmed_attempt(attempt, submission)
                return _confirmed_response(
                    synced, submission, replayed=True,
                    detail=f"并发请求已由尝试 {peer['attempt_no']} 完成，回放回执 {peer['receipt_no']}，未新增记录",
                    run_id=run_id, extra_status=200,
                )
        store.update_attempt_status(
            attempt["attempt_no"], "unknown",
            last_error=f"并发请求：等待更早的尝试 {peer['attempt_no']} 核对结果，本尝试随单据锁定",
        )
        refreshed = store.get_attempt(attempt["attempt_no"])
        store.add_attempt_audit(
            refreshed, "attempt_unknown",
            f"并发收敛：{refreshed['attempt_no']} 跟随在途尝试 {peer['attempt_no']}，不重复送达远端",
        )
        return 202, {
            "code": "result_unknown",
            "replayed": False,
            "detail": (
                f"同一单据存在并发的在途尝试 {peer['attempt_no']}，本请求已并入待核对，"
                "请通过该尝试重试或认领，不会产生第二张业务回执。"
            ),
            "attempt": _attempt_view(refreshed),
            "receipt": None,
            "run_id": run_id,
            "retry_url": f"/api/attempts/{refreshed['attempt_no']}/retry",
            "claim_url": f"/api/attempts/{refreshed['attempt_no']}/claim",
        }
    try:
        gateway.submit(
            receipt_no=attempt["receipt_no"], idem_key=idem_key, doc_no=attempt["doc_no"],
            version=attempt["version"], payload_sha=attempt["payload_sha"],
        )
    except gateway.RemoteAmbiguous as exc:
        # 结果不明：锁单待核对，不猜成功也不猜失败。
        store.update_attempt_status(attempt["attempt_no"], "unknown", last_error=str(exc),
                                    increment_retry=True)
        refreshed = store.get_attempt(attempt["attempt_no"])
        store.add_attempt_audit(
            refreshed, "attempt_unknown",
            f"尝试 {refreshed['attempt_no']} 结果不明（第 {refreshed['retries']} 次送达）：{exc}；单据已锁定待核对",
        )
        return 202, {
            "code": "result_unknown",
            "replayed": False,
            "detail": (
                f"{exc}。单据 {attempt['doc_no']} 已锁定，可在复核页/尝试页核对后"
                "「重试」（自动认领远端回执）或「认领回执」，重试不会新增第二张业务回执。"
            ),
            "attempt": _attempt_view(refreshed),
            "receipt": None,
            "run_id": run_id,
            "retry_url": f"/api/attempts/{attempt['attempt_no']}/retry",
            "claim_url": f"/api/attempts/{attempt['attempt_no']}/claim",
        }
    except gateway.RemoteRejected as exc:
        # 明确失败：不留锁，可直接重试；尝试行保留，审计不丢。
        store.update_attempt_status(attempt["attempt_no"], "failed", last_error=str(exc),
                                    increment_retry=True)
        refreshed = store.get_attempt(attempt["attempt_no"])
        store.add_attempt_audit(
            refreshed, "attempt_failed",
            f"尝试 {refreshed['attempt_no']} 明确失败（第 {refreshed['retries']} 次送达）：{exc}",
        )
        return 502, {
            "code": "remote_rejected",
            "replayed": False,
            "detail": f"{exc}。本次确定未入账，可直接重试（同一尝试、同一回执号，不新增业务回执）。",
            "attempt": _attempt_view(refreshed),
            "receipt": None,
            "run_id": run_id,
            "retry_url": f"/api/attempts/{attempt['attempt_no']}/retry",
        }

    # ② 远端明确成功 → ③ 单事务确认，一次性切换为正式结果。
    try:
        submission, confirmed = store.confirm_attempt(
            attempt,
            f"尝试 {attempt['attempt_no']} 确认入账，回执 {attempt['receipt_no']}，幂等键 {short_key}",
        )
    except sqlite3.IntegrityError as exc:
        # 确认失败（如同版本已有另一张正式回执）：整体回滚，上一张已入账回执原样保留。
        store.update_attempt_status(
            attempt["attempt_no"], "unknown",
            last_error=f"确认切换失败并已回滚，旧回执保留：{exc}", increment_retry=True,
        )
        refreshed = store.get_attempt(attempt["attempt_no"])
        store.add_attempt_audit(
            refreshed, "confirm_rolled_back",
            f"尝试 {refreshed['attempt_no']} 确认失败，事务回滚，未覆盖任何既有回执：{exc}",
        )
        return 409, {
            "code": "confirm_conflict",
            "detail": (
                f"确认回执 {attempt['receipt_no']} 时与既有正式回执冲突，事务已整体回滚，"
                "上一张已入账回执完整保留；请核对后认领或升版再提交。"
            ),
            "attempt": _attempt_view(refreshed),
            "run_id": run_id,
        }
    # 能走到这里，说明送达前本地与远端都没有该幂等键的回执（上面两道核对都已提前 return），
    # 因此这是首次真正入账 → 201；真正的回放由前面的核对/认领路径按 200 返回。
    store.add_attempt_audit(
        confirmed, "attempt_confirmed",
        f"尝试 {confirmed['attempt_no']} 状态切换为已确认入账（共送达 {max(confirmed['retries'], 1)} 次）",
    )
    retry_note = (
        "" if confirmed["retries"] == 0
        else f"（经历 {confirmed['retries']} 次送达后确认，沿用同一尝试与回执号，未新增第二张业务回执）"
    )
    return _confirmed_response(
        confirmed, submission, replayed=False,
        detail=(
            f"提交成功，回执 {submission['receipt_no']}"
            f"（单据 {attempt['doc_no']} v{attempt['version']}）{retry_note}"
        ),
        run_id=run_id, extra_status=201,
    )


def _sync_confirmed_attempt(attempt: dict, submission: dict) -> dict:
    """本地回执已存在时把尝试标记为 confirmed（不重复插 submissions）。"""
    with store.connect() as conn:
        conn.execute(
            "UPDATE submission_attempts SET status = 'confirmed', submission_id = ?, "
            "updated_at = ? WHERE attempt_no = ?",
            (submission["id"], store.now(), attempt["attempt_no"]),
        )
        conn.execute("DELETE FROM doc_locks WHERE doc_no = ?", (attempt["doc_no"],))
        conn.commit()
    return store.get_attempt(attempt["attempt_no"])


def _claim_remote(attempt: dict, remote: dict, run_id: str, *, via: str,
                  replayed: bool) -> tuple[int, dict]:
    """认领到远端回执：校验幂等键一致后，单事务确认成本地正式回执。"""
    if remote["idem_key"] != attempt["idem_key"]:
        store.add_attempt_audit(
            attempt, "claim_rejected",
            f"认领被拒：远端回执 {remote['receipt_no']} 幂等键与尝试 {attempt['attempt_no']} 不一致",
        )
        return 409, {
            "code": "claim_mismatch",
            "detail": (
                f"回执 {remote['receipt_no']} 的幂等键与尝试 {attempt['attempt_no']} 固定的表单内容不一致，"
                "不能认领（防止把别的单据的回执挂到本次尝试上）。"
            ),
            "attempt": _attempt_view(attempt),
            "run_id": run_id,
        }
    try:
        submission, confirmed = store.confirm_attempt(
            attempt,
            f"认领远端回执 {remote['receipt_no']}（动作 {via}），幂等键 {idempotency.short(attempt['idem_key'], 16)}",
        )
    except sqlite3.IntegrityError as exc:
        store.update_attempt_status(
            attempt["attempt_no"], "unknown", last_error=f"认领确认失败并回滚：{exc}",
        )
        refreshed = store.get_attempt(attempt["attempt_no"])
        store.add_attempt_audit(refreshed, "confirm_rolled_back", f"认领确认事务回滚：{exc}")
        return 409, {
            "code": "confirm_conflict",
            "detail": "认领确认与既有正式回执冲突，事务已回滚，旧回执完整保留。",
            "attempt": _attempt_view(refreshed),
            "run_id": run_id,
        }
    store.add_attempt_audit(
        confirmed, "receipt_claimed",
        f"尝试 {confirmed['attempt_no']} 认领回执 {remote['receipt_no']} 并确认入账（{via}）",
    )
    return _confirmed_response(
        confirmed, submission, replayed=replayed,
        detail=f"已认领远端回执 {remote['receipt_no']} 并确认入账，未新增第二张业务回执。",
        run_id=run_id, extra_status=200,
    )


def retry_attempt(attempt_no: str) -> tuple[int, dict]:
    """恢复路径：沿用尝试固定快照重发。多少次重试都共用同一回执号，不新增业务回执。"""
    attempt = store.get_attempt(attempt_no)
    if attempt is None:
        raise AttemptNotFound(attempt_no)
    if attempt["status"] == "confirmed":
        submission = store.get_submission(attempt["receipt_no"])
        if submission is not None:
            store.add_attempt_audit(attempt, "replay", "对已确认尝试再次点击，回放既有回执")
            return _confirmed_response(
                attempt, submission, replayed=True,
                detail=f"尝试 {attempt_no} 已确认，回放回执 {attempt['receipt_no']}，未新增记录。",
                run_id=attempt["run_id"], extra_status=200,
            )
    if attempt["status"] == "in_flight":
        return 423, {
            "code": "attempt_in_flight",
            "detail": f"尝试 {attempt_no} 仍在途，请等待结果或在锁定超时后按待核对处理。",
            "attempt": _attempt_view(attempt),
            "run_id": attempt["run_id"],
        }
    store.add_attempt_audit(
        attempt, "attempt_retry",
        f"人工/自动恢复尝试 {attempt_no}（当前状态 {STATUS_LABELS[attempt['status']]}），沿用固定快照重发",
    )
    store.update_attempt_status(attempt_no, "in_flight")
    refreshed = store.get_attempt(attempt_no)
    return _drive_attempt(refreshed, run_id=refreshed["run_id"])


def claim_attempt(attempt_no: str, receipt_no: str | None = None) -> tuple[int, dict]:
    """人工认领：按回执号（或自动按幂等键）核对远端账本，确认后切换为正式结果。"""
    attempt = store.get_attempt(attempt_no)
    if attempt is None:
        raise AttemptNotFound(attempt_no)
    if attempt["status"] == "confirmed":
        submission = store.get_submission(attempt["receipt_no"])
        if submission is not None:
            return _confirmed_response(
                attempt, submission, replayed=True,
                detail=f"尝试 {attempt_no} 已确认，回执 {attempt['receipt_no']}。",
                run_id=attempt["run_id"], extra_status=200,
            )
    target = receipt_no.strip() if receipt_no else ""
    remote = store.get_remote_receipt(target) if target else gateway.find_remote_receipt(attempt["idem_key"])
    if remote is None:
        store.add_attempt_audit(
            attempt, "claim_rejected",
            f"认领失败：远端账本查不到回执 {target or '（按幂等键自动查询）'}，保持{STATUS_LABELS[attempt['status']]}",
        )
        return 404, {
            "code": "receipt_not_found",
            "detail": (
                f"远端账本中找不到回执 {target or ''}。请确认回执号，或先点「重试」让系统自动核对。"
            ),
            "attempt": _attempt_view(attempt),
            "run_id": attempt["run_id"],
        }
    return _claim_remote(attempt, remote, attempt["run_id"], via="manual_claim", replayed=False)


def attempts_data(attempt_no: str | None = None) -> dict:
    """尝试监控页/统计的统一数据源：状态口径与页面完全一致。"""
    attempts = [_attempt_view(a) for a in store.list_attempts(100)]
    counts = store.count_attempts_by_status()
    status_cards = [
        {
            "status": status,
            "label": STATUS_LABELS[status],
            "tag": STATUS_TAGS[status],
            "count": counts.get(status, 0),
        }
        for status in store.ATTEMPT_STATUSES
    ]
    current = None
    if attempt_no:
        raw = store.get_attempt(attempt_no)
        if raw is not None:
            current = _attempt_view(raw)
            if raw["status"] == "confirmed":
                current["submission"] = store.get_submission(raw["receipt_no"])
    return {
        "attempts": attempts,
        "attempt_count": len(attempts),
        "status_cards": status_cards,
        "open_count": counts.get("in_flight", 0) + counts.get("unknown", 0),
        "current": current,
        "fault_mode": gateway.current_fault(),
        "fault_enabled": settings.ENABLE_FAULTS,
        "remote_receipt_count": store.count_remote_receipts(),
    }


def rules_table(run: dict | None = None) -> list[dict]:
    """工作台的规则表：基础优先级、覆盖后优先级、启停、以及本次求值命中情况。"""
    base = load_ruleset()["rules"]
    overrides = store.list_overrides()
    hits = {r["rule_id"]: r for r in (run or {}).get("rule_results", [])}
    rows: list[dict] = []
    for rule in base:
        override = overrides.get(rule["id"]) or {}
        item = {
            "id": rule["id"],
            "target": rule["target"],
            "expr": rule.get("expr", ""),
            "transform": list(rule.get("transform") or []),
            "note": rule.get("note", ""),
            "base_priority": int(rule.get("priority", 0)),
            "enabled": override.get("enabled", True) is not False,
            "overridden": override.get("priority") is not None,
            "hit": hits.get(rule["id"]),
        }
        item["priority"] = (
            int(override["priority"]) if override.get("priority") is not None else item["base_priority"]
        )
        rows.append(item)
    return rows


def review_data(receipt_no: str | None = None) -> dict:
    submissions = store.list_submissions()
    replays = store.replay_counts()
    for item in submissions:
        item["idem_key_short"] = idempotency.short(item["idem_key"], 16)
        item["payload_sha_short"] = idempotency.short(item["payload_sha"], 12)
        item["replayed_times"] = replays.get(item["receipt_no"], 0)
    chains: dict[str, dict] = {}
    for item in submissions:
        chain = chains.setdefault(
            item["doc_no"],
            {"doc_no": item["doc_no"], "versions": [], "latest_version": item["version"]},
        )
        chain["versions"].append(item)
        chain["latest_version"] = max(chain["latest_version"], item["version"])
    for chain in chains.values():
        chain["versions"].sort(key=lambda v: v["version"])
        for item in chain["versions"]:
            item["is_latest"] = item["version"] == chain["latest_version"]
    receipt = store.get_submission(receipt_no) if receipt_no else (submissions[-1] if submissions else None)
    if receipt:
        order = [f["name"] for f in contract_mod.load_contract()["fields"]]
        receipt = dict(receipt)
        receipt["payload"] = {k: receipt["payload"][k] for k in order if k in receipt["payload"]}
        receipt["idem_key_short"] = idempotency.short(receipt["idem_key"], 16)
        receipt["replayed_times"] = replays.get(receipt["receipt_no"], 0)
    # 可恢复尝试：与 /attempts 页面共用同一状态口径与统计
    attempts = [_attempt_view(a) for a in store.list_attempts(100)]
    attempt_counts = store.count_attempts_by_status()
    open_attempts = [a for a in attempts if a["status"] in store.OPEN_STATUSES]
    locked_docs = sorted({a["doc_no"] for a in open_attempts})
    return {
        "submissions": submissions,
        "chains": sorted(chains.values(), key=lambda c: c["doc_no"]),
        "audits": store.list_audits(40),
        "overrides": [{"rule_id": k, **v} for k, v in sorted(store.list_overrides().items())],
        "receipt": receipt,
        "submission_count": len(submissions),
        "override_count": len(store.list_overrides()),
        "attempts": attempts,
        "attempt_count": len(attempts),
        "attempt_counts": attempt_counts,
        "status_cards": [
            {"status": s, "label": STATUS_LABELS[s], "tag": STATUS_TAGS[s],
             "count": attempt_counts.get(s, 0)}
            for s in store.ATTEMPT_STATUSES
        ],
        "open_attempts": open_attempts,
        "open_attempt_count": len(open_attempts),
        "locked_docs": locked_docs,
        "remote_receipt_count": store.count_remote_receipts(),
    }


def recover_on_startup() -> list[str]:
    """启动/访问时的崩溃恢复：超时仍在途的尝试转 unknown 并锁单，等待人工核对。"""
    recovered = store.recover_stale_attempts(settings.ATTEMPT_LOCK_TIMEOUT_SECONDS)
    for attempt_no in recovered:
        attempt = store.get_attempt(attempt_no)
        if attempt:
            store.add_attempt_audit(attempt, "attempt_recovered",
                                    f"崩溃恢复：{attempt_no} 由进行中转待核对并锁定单据 {attempt['doc_no']}")
    return recovered
