"""FastAPI 路由（薄）：只做参数解析、调用 service、渲染模板。"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import contract as contract_mod
from . import service, settings, store

app = FastAPI(title=settings.APP_NAME)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["pretty_json"] = lambda value: json.dumps(
    value, ensure_ascii=False, indent=2, sort_keys=True
)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

SEED_INFO = store.init_db()


def _base(request: Request, **extra) -> dict:
    context = {
        "request": request,
        "app_name": settings.APP_NAME,
        "seed_info": SEED_INFO,
        "submission_count": store.count_submissions(),
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
    }


@app.get("/health")
def health():
    return JSONResponse(
        {
            "status": "ok",
            "app": settings.APP_SHORT,
            "docs": store.count_docs(),
            "submissions": store.count_submissions(),
            "seed_version": SEED_INFO["seed_version"],
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
    context = _workbench_context(doc_no, version, True)
    notice = f"提交被拒绝（HTTP {status_code}）：{body.get('detail', '')}"
    return templates.TemplateResponse(
        request,
        "workbench.html",
        _base(request, page="workbench", **context, error=notice),
        status_code=status_code,
    )


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
