"""FastAPI 路由（薄）：只做参数解析、调用 service、渲染模板。"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import contract as contract_mod
from . import gateway, service, settings, store

app = FastAPI(title=settings.APP_NAME)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["pretty_json"] = lambda value: json.dumps(
    value, ensure_ascii=False, indent=2, sort_keys=True
)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

SEED_INFO = store.init_db()
# 崩溃恢复：上次进程中断留下的在途尝试转待核对并锁单。
RECOVERED = service.recover_on_startup()


def _base(request: Request, **extra) -> dict:
    attempt_counts = store.count_attempts_by_status()
    context = {
        "request": request,
        "app_name": settings.APP_NAME,
        "seed_info": SEED_INFO,
        "submission_count": store.count_submissions(),
        "open_attempt_count": attempt_counts.get("in_flight", 0) + attempt_counts.get("unknown", 0),
        "status_labels": service.STATUS_LABELS,
    }
    context.update(extra)
    return context


def _workbench_context(doc_no: str | None, version: int | None, run_flag: bool) -> dict:
    docs = store.list_docs()
    names = [d["doc_no"] for d in docs]
    selected = doc_no if doc_no in names else (names[0] if names else None)
    run = None
    if selected:
        run = service.build_run(selected, version)
        if run_flag:
            store.save_preview(run)
    contract = contract_mod.load_contract()
    overrides = store.list_overrides()
    # 同一状态源：工作台展示的锁定尝试与 /attempts、/review 完全一致
    lock = store.open_attempt_for_doc(selected) if selected else None
    doc_attempts = service.attempts_data()["attempts"] if selected else []
    doc_attempts = [a for a in doc_attempts if a["doc_no"] == selected][:6]
    return {
        "docs": docs,
        "selected": selected,
        "version": version,
        "run": run,
        "show_run": run_flag and run is not None,
        "rules": service.rules_table(run),
        "contract": contract,
        "fields": contract["fields"],
        "overrides": overrides,
        "override_count": len(overrides),
        "suggestions": [c["fix"] for c in (run["conflicts"] if run else [])],
        "lock_attempt": service._attempt_view(lock) if lock else None,
        "doc_attempts": doc_attempts,
    }


@app.get("/health")
def health():
    attempt_counts = store.count_attempts_by_status()
    open_docs = sorted(
        {a["doc_no"] for a in store.list_attempts(500) if a["status"] in store.OPEN_STATUSES}
    )
    return JSONResponse(
        {
            "status": "ok",
            "app": settings.APP_SHORT,
            "docs": store.count_docs(),
            "submissions": store.count_submissions(),
            "seed_version": SEED_INFO["seed_version"],
            "attempts": attempt_counts,
            "locked_docs": open_docs,
            "remote_receipts": store.count_remote_receipts(),
        }
    )


@app.get("/")
def index(request: Request):
    contract = contract_mod.load_contract()
    rules = service.rules_table(None)
    docs = store.list_docs()
    recent_submissions = sorted(
        store.list_submissions(), key=lambda item: item.get("created_at") or "", reverse=True
    )[:6]
    attempt_cards = service.attempts_data()
    return templates.TemplateResponse(
        request,
        "index.html",
        _base(
            request,
            page="index",
            recent_submissions=recent_submissions,
            docs=docs,
            contract=contract,
            fields=contract["fields"],
            rules=rules,
            ruleset=service.load_ruleset().get("ruleset", ""),
            conflict_groups=[
                {"target": "amount", "rules": ["R01", "R02"], "priority": 10, "values": ["1280.50", "1446.97"]},
                {"target": "expense_type", "rules": ["R03", "R04"], "priority": 15, "values": ["travel", "travel_fee"]},
            ],
            attempt_cards=attempt_cards["status_cards"],
        ),
    )


@app.get("/workbench")
def workbench(request: Request, doc: str | None = None, version: int | None = None, run: int = 0):
    context = _workbench_context(doc, version, bool(run))
    return templates.TemplateResponse(
        request, "workbench.html", _base(request, page="workbench", **context)
    )


@app.post("/workbench/rules")
async def workbench_rules(request: Request):
    form = await request.form()
    doc = form.get("doc") or None
    version = form.get("version")
    base_rules = service.load_ruleset()["rules"]
    changed = 0
    for rule in base_rules:
        rule_id = rule["id"]
        raw_priority = form.get(f"priority_{rule_id}")
        enabled = f"enabled_{rule_id}" in form
        if raw_priority is None:
            continue
        try:
            priority = int(str(raw_priority).strip())
        except ValueError:
            continue
        base_priority = int(rule.get("priority", 0))
        if priority == base_priority and enabled:
            store.delete_override(rule_id)
        else:
            store.set_override(rule_id, priority, enabled)
            changed += 1
    target_version = int(version) if version else None
    url = f"/workbench?doc={doc or ''}&run=1"
    if target_version:
        url += f"&version={target_version}"
    return RedirectResponse(url=url, status_code=303)


@app.post("/workbench/submit")
def workbench_submit(
    request: Request,
    doc_no: str = Form(...),
    version: int | None = Form(None),
):
    try:
        status_code, body = service.submit(doc_no, version)
    except service.DocNotFound as exc:
        context = _workbench_context(None, None, False)
        return templates.TemplateResponse(
            request,
            "workbench.html",
            _base(request, page="workbench", **context, error=str(exc)),
            status_code=404,
        )
    if status_code in (200, 201):
        receipt_no = body["receipt"]["receipt_no"]
        suffix = "&replayed=1" if body.get("replayed") else ""
        return RedirectResponse(url=f"/review?receipt={receipt_no}{suffix}", status_code=303)
    if status_code == 202 and body.get("attempt"):
        # 结果不明：跳尝试页，引导核对/认领，而不是让用户再点一次提交
        return RedirectResponse(
            url=f"/attempts?attempt={body['attempt']['attempt_no']}&unknown=1", status_code=303
        )
    if status_code == 502 and body.get("attempt"):
        return RedirectResponse(
            url=f"/attempts?attempt={body['attempt']['attempt_no']}&failed=1", status_code=303
        )
    context = _workbench_context(doc_no, version, True)
    notice = f"提交被拒绝（HTTP {status_code}）：{body.get('detail', '')}"
    return templates.TemplateResponse(
        request,
        "workbench.html",
        _base(request, page="workbench", **context, error=notice),
        status_code=status_code if status_code < 500 else 502,
    )


@app.get("/attempts")
def attempts_page(request: Request, attempt: str | None = None, unknown: int = 0, failed: int = 0):
    data = service.attempts_data(attempt)
    notice = None
    if unknown:
        notice = "提交结果不明：单据已锁定待核对。可「重试」让系统自动核对远端回执，或人工「认领回执」，不会新增第二张业务回执。"
    elif failed:
        notice = "远端明确拒绝且确定未入账：可直接「重试」，沿用同一尝试与回执号。"
    return templates.TemplateResponse(
        request,
        "attempts.html",
        _base(request, page="attempts", **data, notice=notice),
    )


@app.post("/attempts/{attempt_no}/retry")
def attempt_retry_form(request: Request, attempt_no: str):
    try:
        status_code, body = service.retry_attempt(attempt_no)
    except service.AttemptNotFound:
        return templates.TemplateResponse(
            request, "missing.html",
            _base(request, page="attempts", error=f"尝试 {attempt_no} 不存在", back_url="/attempts"),
            status_code=404,
        )
    if status_code in (200, 201) and body.get("receipt"):
        suffix = "&replayed=1" if body.get("replayed") else ""
        return RedirectResponse(
            url=f"/review?receipt={body['receipt']['receipt_no']}{suffix}", status_code=303
        )
    return RedirectResponse(url=f"/attempts?attempt={attempt_no}", status_code=303)


@app.post("/attempts/{attempt_no}/claim")
async def attempt_claim_form(request: Request, attempt_no: str):
    form = await request.form()
    receipt_no = (form.get("receipt_no") or "").strip() or None
    try:
        status_code, body = service.claim_attempt(attempt_no, receipt_no)
    except service.AttemptNotFound:
        return templates.TemplateResponse(
            request, "missing.html",
            _base(request, page="attempts", error=f"尝试 {attempt_no} 不存在", back_url="/attempts"),
            status_code=404,
        )
    if status_code in (200, 201) and body.get("receipt"):
        return RedirectResponse(
            url=f"/review?receipt={body['receipt']['receipt_no']}", status_code=303
        )
    return RedirectResponse(url=f"/attempts?attempt={attempt_no}", status_code=303)


@app.post("/workbench/reset")
def workbench_reset(request: Request, doc: str = Form("")):
    store.reset_overrides()
    return RedirectResponse(url=f"/workbench?doc={doc}&run=1", status_code=303)


@app.get("/result/{run_id}")
def result(request: Request, run_id: str):
    run = store.get_preview(run_id)
    if run is None:
        return templates.TemplateResponse(
            request,
            "missing.html",
            _base(
                request,
                page="missing",
                error=f"预览 {run_id} 不存在（预览按单据+版本+规则快照缓存，规则改动后 run_id 会变化）",
                back_url="/workbench",
            ),
            status_code=404,
        )
    return templates.TemplateResponse(
        request,
        "result.html",
        _base(request, page="result", run=run, contract=contract_mod.load_contract()),
    )


@app.get("/review")
def review(request: Request, receipt: str | None = None, replayed: int = 0):
    data = service.review_data(receipt)
    notice = "本次提交命中幂等键，返回既有回执，未新增记录（replayed=true）" if replayed else None
    contract = contract_mod.load_contract()
    return templates.TemplateResponse(
        request,
        "review.html",
        _base(request, page="review", **data, contract=contract, notice=notice, submitted=receipt or None),
    )


@app.get("/api/source-docs")
def api_source_docs():
    return {"docs": store.list_docs(), "count": store.count_docs()}


@app.get("/api/rules")
def api_rules():
    run = None
    docs = store.list_docs()
    if docs:
        run = service.build_run(docs[0]["doc_no"])
    return {
        "ruleset": service.load_ruleset().get("ruleset", ""),
        "overrides": store.list_overrides(),
        "rules": service.rules_table(run),
    }


@app.post("/api/rules/priority")
async def api_rules_priority(payload: dict):
    rule_id = payload.get("rule_id")
    known = {r["id"] for r in service.load_ruleset()["rules"]}
    if not rule_id or rule_id not in known:
        return JSONResponse(
            {"detail": f"规则 {rule_id!r} 不存在，可用规则：{', '.join(sorted(known))}"}, status_code=404
        )
    priority = payload.get("priority")
    enabled = payload.get("enabled", True)
    if priority is not None:
        try:
            priority = int(priority)
        except (TypeError, ValueError):
            return JSONResponse({"detail": "priority 必须是整数"}, status_code=422)
        if priority < 0 or priority > 999:
            return JSONResponse({"detail": "priority 需在 0..999 之间"}, status_code=422)
    if not isinstance(enabled, bool):
        return JSONResponse({"detail": "enabled 必须是布尔值"}, status_code=422)
    base = {r["id"]: int(r.get("priority", 0)) for r in service.load_ruleset()["rules"]}
    if priority in (None, base[rule_id]) and enabled:
        store.delete_override(rule_id)
    else:
        store.set_override(rule_id, priority, enabled)
    return {
        "rule_id": rule_id,
        "priority": priority,
        "enabled": enabled,
        "overrides": store.list_overrides(),
        "detail": f"已更新规则 {rule_id}",
    }


@app.post("/api/rules/reset")
def api_rules_reset():
    removed = store.reset_overrides()
    return {"removed": removed, "detail": f"已清除 {removed} 条规则覆盖，恢复样例默认优先级"}


@app.post("/api/dry-run")
async def api_dry_run(payload: dict | None = None):
    payload = payload or {}
    doc_no = payload.get("doc_no")
    version = payload.get("version")
    if not doc_no:
        return JSONResponse({"detail": "缺少 doc_no 参数"}, status_code=400)
    try:
        version = int(version) if version is not None else None
    except (TypeError, ValueError):
        return JSONResponse({"detail": "version 必须是整数"}, status_code=422)
    if version is not None and version < 1:
        return JSONResponse({"detail": "version 必须大于等于 1"}, status_code=422)
    try:
        run = service.dry_run(doc_no, version)
    except service.DocNotFound as exc:
        return JSONResponse({"detail": str(exc)}, status_code=404)
    return {
        "run_id": run["run_id"],
        "doc_no": run["doc_no"],
        "version": run["version"],
        "submittable": run["submittable"],
        "conflict_count": len(run["conflicts"]),
        "error_count": len(run["errors"]),
        "idempotency_key": run["idempotency_key"],
        "payload": run["payload"],
        "conflicts": run["conflicts"],
        "errors": run["errors"],
        "result_url": f"/result/{run['run_id']}",
    }


@app.post("/api/submit")
async def api_submit(payload: dict | None = None):
    payload = payload or {}
    doc_no = payload.get("doc_no")
    version = payload.get("version")
    if not doc_no:
        return JSONResponse({"detail": "缺少 doc_no 参数"}, status_code=400)
    try:
        version = int(version) if version is not None else None
    except (TypeError, ValueError):
        return JSONResponse({"detail": "version 必须是整数"}, status_code=422)
    try:
        status_code, body = service.submit(doc_no, version)
    except service.DocNotFound as exc:
        return JSONResponse({"detail": str(exc)}, status_code=404)
    return JSONResponse(body, status_code=status_code)


@app.get("/api/attempts")
def api_attempts():
    return service.attempts_data()


@app.get("/api/attempts/{attempt_no}")
def api_attempt_detail(attempt_no: str):
    data = service.attempts_data(attempt_no)
    if data["current"] is None:
        return JSONResponse({"detail": f"尝试 {attempt_no} 不存在"}, status_code=404)
    return data["current"]


@app.post("/api/attempts/{attempt_no}/retry")
def api_attempt_retry(attempt_no: str):
    try:
        status_code, body = service.retry_attempt(attempt_no)
    except service.AttemptNotFound:
        return JSONResponse({"detail": f"尝试 {attempt_no} 不存在"}, status_code=404)
    return JSONResponse(body, status_code=status_code)


@app.post("/api/attempts/{attempt_no}/claim")
async def api_attempt_claim(attempt_no: str, payload: dict | None = None):
    payload = payload or {}
    receipt_no = payload.get("receipt_no")
    try:
        status_code, body = service.claim_attempt(
            attempt_no, str(receipt_no).strip() if receipt_no else None
        )
    except service.AttemptNotFound:
        return JSONResponse({"detail": f"尝试 {attempt_no} 不存在"}, status_code=404)
    return JSONResponse(body, status_code=status_code)


@app.post("/api/dev/fault")
async def api_dev_fault(payload: dict | None = None):
    """演示/测试用故障注入。生产环境（FORMFORGE_ENABLE_FAULTS=0）直接 404。"""
    if not settings.ENABLE_FAULTS:
        return JSONResponse({"detail": "故障注入已关闭（FORMFORGE_ENABLE_FAULTS=0）"}, status_code=404)
    mode = (payload or {}).get("mode", "none")
    try:
        result = gateway.configure_fault(mode)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=422)
    return result


@app.get("/api/dev/fault")
def api_dev_fault_get():
    if not settings.ENABLE_FAULTS:
        return JSONResponse({"detail": "故障注入已关闭"}, status_code=404)
    return {"next_fault": gateway.current_fault(), "enabled": True}
