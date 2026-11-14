# formforge · 表单录入 RPA 字段映射与幂等提交

把采购申请单据自动录入报销/资产系统 Web 表单的**映射 · 校验 · 幂等提交**层。

- **使用者**：集团财务共享中心 RPA 运维（提交前看依据）、财务复核岗（看历史与版本链）。
- **痛点**：① 同一单据被提交两次导致重复入账；② 源字段与目标表单字段错配（差旅费录成办公费）；
  ③ 改了金额却没升版，覆盖了已入账数据。
- **本系统做**：源单据 → 映射规则求值（优先级/冲突）→ 目标表单契约校验 → dry-run 预览 → 幂等提交与回执。
  不驱动浏览器，只保证「该录什么值、能不能录、会不会重复录」。

## 核心规则（后端真算，纯函数模块）

1. **规则求值**（`app/expr.py` + `app/mapping.py`）
   - 规则形如 `{"id":"R01","target":"amount","expr":"{{amount_total}}*1.0","priority":10,"transform":["round2"]}`。
   - 表达式支持 `{{字段}}` / `{{src.字段}}`、常量（`CNY`、`1.13`）、`upper/trim/lower/substr(v,a[,b])/round2`、
     `+ - * / ( )`（Decimal 递归下降求值，无 `eval`）。
   - 同一 `target` 取**最高 priority**；同 priority 多值不同 → **conflict**（列出每条规则 id 与值）；
     必填 target 全部未命中/无规则 → **missing_required**。
   - 值比较按类型口径：`1` 与 `1.0` 同值；文本按原样比较（`"张伟"` 与 `" 张伟 "` 是不同提交值）。
2. **类型与业务校验**（`app/contract.py` + `app/validation.py`）
   - 必填、`string` 长度、`integer`、`amount`（两位小数、> 0、单笔上限 50000）、
     `date`（ISO `YYYY-MM-DD` 且必须是真实日历日，`2026-02-30` 非法、`2024-02-29` 合法）、
     `enum`（越界报错并给出可选项）、`boolean`（`true/false`、`是/否`、`1/0`）。
3. **幂等提交**（`app/idempotency.py` + `app/service.py`）
   - 幂等键 = `sha256(doc_no + version + 规范化 payload)`，payload 规范化 = `sort_keys + 紧凑分隔符`。
   - 重复键 → 返回既有回执 `replayed=true`，**不新增记录**。
   - 同 `doc_no` 但 payload 变化且 `version` 未递增（含版本回退）→ **HTTP 409 `stale_version`**。
   - 显式升版 → 新版本落库，旧版本保留可查（版本链）。
4. **dry-run 与提交分离**
   - dry-run 返回映射结果 / 冲突 / 校验错误 / 幂等键，**不写 `submissions`、`audit_events`**；
     只写 `previews` 预览快照，供 `/result/{run_id}` 打开（run_id = 单据+版本+规则快照+payload 的哈希前 12 位，
     规则或数据一变旧链接就失效，属预期行为）。
   - 提交才写 `submissions` 与 `audit_events`（`submit` / `replay` / `stale_rejected`）。

## 启动

```bash
docker compose up -d --build
docker compose ps                      # 等 healthy
curl -s http://127.0.0.1:18211/health
```

- 端口：`18211`（`.env` 里的 `APP_PORT` 可改）。
- SQLite 命名卷：`formforge-data:/app/data`（`formforge.db`）。
- 重建数据库：`docker compose down -v` 后重新 `up`（源单据 seed 幂等，重复启动不会重复插单据）。
- 容器内跑测试：`docker compose run --rm app python -m pytest -q`。

## 演示步骤（页面可点）

1. 打开 http://127.0.0.1:18211/ → 看场景、样例单据、目标契约。
2. 进 `/workbench?doc=CG-2026-0001&run=1` → 看到 **2 条同优先级冲突**（`amount` 的 R01/R02 =
   1280.50/1446.97；`expense_type` 的 R03/R04 = travel/travel_fee）与 **2 条校验错误**
   （`conflict_unresolved`：冲突未决无法定值）。
3. 点冲突行的「采纳建议」或手动把 `R01` 优先级改成 20、`R03` 改成 25 → 「保存并重跑」→ 冲突 0、错误 0、可提交。
4. 提交 → 回执 `RCPT-CG-2026-0001-V1`；再提交一次 → 200 且 `replayed=true`，记录数不变。
5. 用同一单据改金额（把样例 `amount_total` 改掉）后不升版提交 → `409 stale_version`；
   把 version 改成 2 提交 → `RCPT-CG-2026-0001-V2`，`/review` 可见 V1→V2 版本链与 replay 审计。
6. `/result/{run_id}` 看逐字段来源（哪条规则、什么表达式、优先级多少、被谁覆盖）、冲突与错误依据、幂等键。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查（含单据数/提交数/seed 版本） |
| GET | `/api/source-docs` | 3 张样例单据 |
| GET | `/api/rules` | 18 条规则 + 覆盖情况 + 当前求值命中 |
| POST | `/api/rules/priority` | `{"rule_id":"R01","priority":20,"enabled":true}` |
| POST | `/api/rules/reset` | 清除规则覆盖 |
| POST | `/api/dry-run` | `{"doc_no":"CG-2026-0001","version":1}` → 冲突/错误/幂等键/result_url |
| POST | `/api/submit` | 201 新建 / 200 replayed / 409 stale_version / 422 未通过 / 404 单据不存在 |
| GET | `/`、`/workbench`、`/result/{run_id}`、`/review` | 4 个页面 |

```bash
curl -s -X POST http://127.0.0.1:18211/api/dry-run -H 'Content-Type: application/json' \
  -d '{"doc_no":"CG-2026-0001"}'
curl -s -X POST http://127.0.0.1:18211/api/rules/priority -H 'Content-Type: application/json' \
  -d '{"rule_id":"R01","priority":20}'
curl -s -X POST http://127.0.0.1:18211/api/submit -H 'Content-Type: application/json' \
  -d '{"doc_no":"CG-2026-0001","version":1}'
```

## 目录

```text
app/
  main.py          # FastAPI 路由（薄：参数解析 + 渲染）
  service.py       # 编排：build_run/dry_run、submit、review_data、rules_table
  mapping.py       # 规则求值 + 按 target 收敛到最高优先级（核心规则一）
  expr.py          # 表达式解析与求值（占位/常量/函数/四则，Decimal，无 eval）
  conflicts.py     # 冲突检测与修复建议
  validation.py    # 表单级校验编排，产出规范化 payload（核心规则二）
  contract.py      # 目标契约：类型/必填/枚举/长度 + 类型与业务校验原语
  idempotency.py   # 幂等键、payload 规范化、回执号（核心规则三）
  store.py         # SQLite：seed/规则覆盖/预览/提交/审计
  settings.py      # 路径与常量
  templates/       # base/index/workbench/result/review/missing
  static/          # app.js（原生 fetch）、style.css
sample_data/       # source_docs.json(3) / mapping_rules.json(18) / target_contract.json(12)
tests/             # test_mapping / test_validation / test_idempotency / test_api
```

## 已知限制

- 只做映射/校验/幂等，不真正驱动浏览器或调用报销系统的接口；「提交」= 在本地入库并给出回执。
- 目标契约与映射规则来自 `sample_data/` 的 JSON（规则覆盖存 SQLite）；页面不提供新增规则的编辑器。
- 幂等键依赖 payload 完全一致；若目标契约枚举值调整，同一单据会被视为新 payload（需升版）。
- `previews` 表是 dry-run 预览缓存，不是业务记录；规则或源数据变化后旧 `run_id` 失效。
- 单进程 SQLite，无并发写优化；仅面向单机演示与小型运维场景。
