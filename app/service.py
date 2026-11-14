"""业务编排：dry-run 预览与幂等提交。路由只做参数解析与渲染。"""
from __future__ import annotations

import json
from decimal import Decimal

from . import conflicts as conflicts_mod
from . import contract as contract_mod
from . import idempotency, mapping, settings, store, validation


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


def _receipt(submission: dict, replayed: bool) -> dict:
    receipt = dict(submission)
    receipt["idem_key_short"] = idempotency.short(submission["idem_key"], 16)
    receipt["replayed"] = replayed
    return receipt


def submit(doc_no: str, version: int | None = None) -> tuple[int, dict]:
    """提交：返回 (HTTP 状态码, 响应体)。201 新建 / 200 replayed / 409 stale_version / 422 校验未通过。"""
    record = store.get_doc(doc_no)
    if record is None:
        raise DocNotFound(doc_no)
    doc = record["doc"]
    req_version = int(version if version is not None else doc.get("version", 1))

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

    existing = store.find_by_key(idem_key)
    if existing:
        store.add_audit(
            "replay", doc_no, req_version,
            f"命中既有幂等键 {idempotency.short(idem_key, 16)}，返回回执 {existing['receipt_no']}，不新增记录",
            existing,
        )
        return 200, {
            "replayed": True,
            "detail": f"幂等命中：单据 {doc_no} v{req_version} 与回执 {existing['receipt_no']} 内容一致，未新增记录",
            "receipt": _receipt(existing, True),
            "run_id": run["run_id"],
        }

    latest = store.latest_version(doc_no)
    if latest is not None:
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

    receipt_no = idempotency.receipt_no(doc_no, req_version)
    created = store.insert_submission(
        receipt_no=receipt_no,
        idem_key=idem_key,
        doc_no=doc_no,
        version=req_version,
        payload_json=idempotency.canonical_json(payload),
        payload_sha=sha,
    )
    store.add_audit(
        "submit", doc_no, req_version,
        f"新建回执 {receipt_no}，幂等键 {idempotency.short(idem_key, 16)}", created,
    )
    return 201, {
        "replayed": False,
        "detail": f"提交成功，回执 {receipt_no}（单据 {doc_no} v{req_version}）",
        "receipt": _receipt(created, False),
        "run_id": run["run_id"],
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
    return {
        "submissions": submissions,
        "chains": sorted(chains.values(), key=lambda c: c["doc_no"]),
        "audits": store.list_audits(40),
        "overrides": [{"rule_id": k, **v} for k, v in sorted(store.list_overrides().items())],
        "receipt": receipt,
        "submission_count": len(submissions),
        "override_count": len(store.list_overrides()),
    }
