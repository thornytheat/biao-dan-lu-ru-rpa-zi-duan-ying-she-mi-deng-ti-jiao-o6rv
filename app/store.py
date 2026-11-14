"""SQLite 访问：源单据 seed、规则优先级覆盖、dry-run 预览、提交与回执、审计流水。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from . import settings

DDL = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_docs (
  doc_no TEXT PRIMARY KEY,
  version INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  seeded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rule_overrides (
  rule_id TEXT PRIMARY KEY,
  priority INTEGER,
  enabled INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS previews (
  run_id TEXT PRIMARY KEY,
  doc_no TEXT NOT NULL,
  version INTEGER NOT NULL,
  rules_digest TEXT NOT NULL,
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submissions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  receipt_no TEXT NOT NULL UNIQUE,
  idem_key TEXT NOT NULL UNIQUE,
  doc_no TEXT NOT NULL,
  version INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  payload_sha TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (doc_no, version)
);
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  submission_id INTEGER,
  receipt_no TEXT,
  doc_no TEXT NOT NULL,
  version INTEGER NOT NULL,
  action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submission_attempts (
  attempt_no TEXT PRIMARY KEY,
  doc_no TEXT NOT NULL,
  version INTEGER NOT NULL,
  status TEXT NOT NULL,
  idem_key TEXT NOT NULL,
  receipt_no TEXT NOT NULL,
  run_id TEXT NOT NULL,
  rules_digest TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  payload_sha TEXT NOT NULL,
  retries INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  submission_id INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_doc ON submission_attempts (doc_no, version);
CREATE INDEX IF NOT EXISTS idx_attempts_status ON submission_attempts (status);
CREATE TABLE IF NOT EXISTS remote_receipts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  receipt_no TEXT NOT NULL UNIQUE,
  idem_key TEXT NOT NULL UNIQUE,
  doc_no TEXT NOT NULL,
  version INTEGER NOT NULL,
  payload_sha TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS doc_locks (
  doc_no TEXT PRIMARY KEY,
  attempt_no TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""

# 尝试状态机：in_flight（进行中）→ unknown（待核对，锁单）/ failed（明确失败）/ confirmed（已确认入账）
ATTEMPT_STATUSES = ("in_flight", "unknown", "failed", "confirmed")
# 处于这两个状态的尝试会锁住对应单据：结果不明或仍在途，禁止再开新尝试
OPEN_STATUSES = ("in_flight", "unknown")


def now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def connect() -> sqlite3.Connection:
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> dict:
    with connect() as conn:
        conn.executescript(DDL)
    return seed_docs()


def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def seed_docs() -> dict:
    """幂等 seed：重复启动 / 重复调用都不会重复插入单据。"""
    with open(settings.SOURCE_DOCS_FILE, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    docs = payload["docs"]
    inserted, skipped = 0, 0
    with connect() as conn:
        conn.executescript(DDL)
        already = meta_get(conn, "docs_seed_version")
        if already == settings.SEED_VERSION:
            skipped = len(docs)
            return {"seed_version": already, "inserted": 0, "skipped": skipped,
                    "total": conn.execute("SELECT COUNT(*) AS c FROM source_docs").fetchone()["c"]}
        for doc in docs:
            exists = conn.execute("SELECT 1 FROM source_docs WHERE doc_no = ?", (doc["doc_no"],)).fetchone()
            if exists:
                skipped += 1
                continue
            conn.execute(
                "INSERT INTO source_docs (doc_no, version, payload_json, seeded_at) VALUES (?,?,?,?)",
                (doc["doc_no"], int(doc.get("version", 1)), json.dumps(doc, ensure_ascii=False), now()),
            )
            inserted += 1
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('docs_seed_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (settings.SEED_VERSION,),
        )
        conn.commit()
        total = conn.execute("SELECT COUNT(*) AS c FROM source_docs").fetchone()["c"]
    return {"seed_version": settings.SEED_VERSION, "inserted": inserted, "skipped": skipped, "total": total}


def list_docs() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM source_docs ORDER BY doc_no").fetchall()
    return [
        {"doc_no": r["doc_no"], "version": r["version"], "doc": json.loads(r["payload_json"]), "seeded_at": r["seeded_at"]}
        for r in rows
    ]


def get_doc(doc_no: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM source_docs WHERE doc_no = ?", (doc_no,)).fetchone()
    if not row:
        return None
    return {"doc_no": row["doc_no"], "version": row["version"], "doc": json.loads(row["payload_json"])}


def count_docs() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM source_docs").fetchone()["c"]


def list_overrides() -> dict[str, dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM rule_overrides ORDER BY rule_id").fetchall()
    return {r["rule_id"]: {"priority": r["priority"], "enabled": bool(r["enabled"])} for r in rows}


def set_override(rule_id: str, priority: int | None, enabled: bool) -> dict:
    with connect() as conn:
        conn.execute(
            "INSERT INTO rule_overrides (rule_id, priority, enabled, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(rule_id) DO UPDATE SET priority = excluded.priority, "
            "enabled = excluded.enabled, updated_at = excluded.updated_at",
            (rule_id, priority, 1 if enabled else 0, now()),
        )
        conn.commit()
    return {"rule_id": rule_id, "priority": priority, "enabled": enabled}


def reset_overrides() -> int:
    with connect() as conn:
        cur = conn.execute("DELETE FROM rule_overrides")
        conn.commit()
        return cur.rowcount


def save_preview(run: dict) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO previews (run_id, doc_no, version, rules_digest, result_json, created_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET result_json = excluded.result_json, "
            "created_at = excluded.created_at",
            (run["run_id"], run["doc_no"], run["version"], run["rules_digest"],
             json.dumps(run, ensure_ascii=False), now()),
        )
        conn.commit()


def get_preview(run_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM previews WHERE run_id = ?", (run_id,)).fetchone()
    if not row:
        return None
    run = json.loads(row["result_json"])
    run["preview_created_at"] = row["created_at"]
    return run


def find_by_key(idem_key: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM submissions WHERE idem_key = ?", (idem_key,)).fetchone()
    return _submission(row) if row else None


def find_doc_version(doc_no: str, version: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM submissions WHERE doc_no = ? AND version = ?", (doc_no, int(version))
        ).fetchone()
    return _submission(row) if row else None


def latest_version(doc_no: str) -> int | None:
    with connect() as conn:
        row = conn.execute("SELECT MAX(version) AS v FROM submissions WHERE doc_no = ?", (doc_no,)).fetchone()
    return row["v"] if row and row["v"] is not None else None


def insert_submission(receipt_no: str, idem_key: str, doc_no: str, version: int, payload_json: str, payload_sha: str) -> dict:
    created = now()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO submissions (receipt_no, idem_key, doc_no, version, payload_json, payload_sha, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (receipt_no, idem_key, doc_no, int(version), payload_json, payload_sha, created),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM submissions WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _submission(row)


def add_audit(action: str, doc_no: str, version: int, detail: str = "", submission: dict | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO audit_events (submission_id, receipt_no, doc_no, version, action, detail, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                submission["id"] if submission else None,
                submission["receipt_no"] if submission else None,
                doc_no,
                int(version),
                action,
                detail,
                now(),
            ),
        )
        conn.commit()


def _submission(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "receipt_no": row["receipt_no"],
        "idem_key": row["idem_key"],
        "doc_no": row["doc_no"],
        "version": row["version"],
        "payload": json.loads(row["payload_json"]),
        "payload_sha": row["payload_sha"],
        "created_at": row["created_at"],
    }


def list_submissions() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM submissions ORDER BY doc_no, version").fetchall()
    return [_submission(r) for r in rows]


def get_submission(receipt_no: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM submissions WHERE receipt_no = ?", (receipt_no,)).fetchone()
    return _submission(row)


def list_audits(limit: int = 40) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()
    return [dict(r) for r in rows]


def count_submissions() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM submissions").fetchone()["c"]


def replay_counts() -> dict[str, int]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT receipt_no, COUNT(*) AS c FROM audit_events WHERE action = 'replay' GROUP BY receipt_no"
        ).fetchall()
    return {r["receipt_no"]: r["c"] for r in rows}


def delete_override(rule_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM rule_overrides WHERE rule_id = ?", (rule_id,))
        conn.commit()


# ---------------------------------------------------------------------------
# 可恢复尝试（submission_attempts）+ 远端账本（remote_receipts）
# ---------------------------------------------------------------------------

def _attempt(row: sqlite3.Row) -> dict:
    return {
        "attempt_no": row["attempt_no"],
        "doc_no": row["doc_no"],
        "version": row["version"],
        "status": row["status"],
        "idem_key": row["idem_key"],
        "receipt_no": row["receipt_no"],
        "run_id": row["run_id"],
        "rules_digest": row["rules_digest"],
        "payload": json.loads(row["payload_json"]),
        "payload_sha": row["payload_sha"],
        "retries": row["retries"],
        "last_error": row["last_error"],
        "submission_id": row["submission_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def next_attempt_no(conn: sqlite3.Connection, doc_no: str) -> str:
    """同一单据的尝试序号递增：ATT-CG-2026-0001-0001。"""
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM submission_attempts WHERE doc_no = ?", (doc_no,)
    ).fetchone()
    return f"ATT-{doc_no}-{int(row['c']) + 1:04d}"


class DocAlreadyLocked(sqlite3.Error):
    """并发提交时单据锁已被另一个尝试持有。"""


def acquire_doc_lock(conn: sqlite3.Connection, doc_no: str, attempt_no: str, status: str) -> None:
    """在调用方事务内占锁：doc_no 主键冲突说明已有在途/待核对尝试。"""
    ts = now()
    conn.execute(
        "INSERT INTO doc_locks (doc_no, attempt_no, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (doc_no, attempt_no, status, ts, ts),
    )


def release_doc_lock(doc_no: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM doc_locks WHERE doc_no = ?", (doc_no,))
        conn.commit()


def touch_doc_lock(doc_no: str, status: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE doc_locks SET status = ?, updated_at = ? WHERE doc_no = ?",
            (status, now(), doc_no),
        )
        conn.commit()


def get_doc_lock(doc_no: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM doc_locks WHERE doc_no = ?", (doc_no,)).fetchone()
    return dict(row) if row else None


def insert_attempt(
    doc_no: str,
    version: int,
    idem_key: str,
    receipt_no: str,
    run_id: str,
    rules_digest: str,
    payload: dict,
    payload_sha: str,
) -> dict:
    """事务化创建尝试并占单据锁：并发下同一单据只会有一个尝试成功。"""
    created = now()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        attempt_no = next_attempt_no(conn, doc_no)
        try:
            acquire_doc_lock(conn, doc_no, attempt_no, "in_flight")
        except sqlite3.IntegrityError:
            conn.rollback()
            raise DocAlreadyLocked(f"单据 {doc_no} 的提交锁已被其他尝试持有")
        conn.execute(
            "INSERT INTO submission_attempts (attempt_no, doc_no, version, status, idem_key, "
            "receipt_no, run_id, rules_digest, payload_json, payload_sha, retries, last_error, "
            "submission_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,0,'',NULL,?,?)",
            (
                attempt_no, doc_no, int(version), "in_flight", idem_key, receipt_no, run_id,
                rules_digest, json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                         separators=(",", ":")), payload_sha, created, created,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM submission_attempts WHERE attempt_no = ?", (attempt_no,)
        ).fetchone()
    return _attempt(row)


def get_attempt(attempt_no: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM submission_attempts WHERE attempt_no = ?", (attempt_no,)
        ).fetchone()
    return _attempt(row) if row else None


def find_attempt_by_key(idem_key: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM submission_attempts WHERE idem_key = ? ORDER BY rowid DESC LIMIT 1",
            (idem_key,),
        ).fetchone()
    return _attempt(row) if row else None


def list_attempts(limit: int = 50) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM submission_attempts ORDER BY rowid DESC LIMIT ?", (int(limit),)
        ).fetchall()
    return [_attempt(r) for r in rows]


def list_doc_attempts(doc_no: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM submission_attempts WHERE doc_no = ? ORDER BY rowid", (doc_no,)
        ).fetchall()
    return [_attempt(r) for r in rows]


def open_attempt_for_doc(doc_no: str) -> dict | None:
    """返回锁住该单据的在途/待核对尝试（其他单据不受影响）。"""
    placeholders = ",".join("?" for _ in OPEN_STATUSES)
    with connect() as conn:
        row = conn.execute(
            f"SELECT * FROM submission_attempts WHERE doc_no = ? AND status IN ({placeholders}) "
            "ORDER BY rowid DESC LIMIT 1",
            (doc_no, *OPEN_STATUSES),
        ).fetchone()
    return _attempt(row) if row else None


def update_attempt_status(
    attempt_no: str,
    status: str,
    *,
    last_error: str | None = None,
    increment_retry: bool = False,
) -> None:
    if status not in ATTEMPT_STATUSES:
        raise ValueError(f"非法尝试状态 {status!r}")
    with connect() as conn:
        if increment_retry:
            conn.execute(
                "UPDATE submission_attempts SET status = ?, retries = retries + 1, "
                "last_error = COALESCE(?, last_error), updated_at = ? WHERE attempt_no = ?",
                (status, last_error, now(), attempt_no),
            )
        else:
            conn.execute(
                "UPDATE submission_attempts SET status = ?, "
                "last_error = COALESCE(?, last_error), updated_at = ? WHERE attempt_no = ?",
                (status, last_error, now(), attempt_no),
            )
        row = conn.execute(
            "SELECT doc_no FROM submission_attempts WHERE attempt_no = ?", (attempt_no,)
        ).fetchone()
        if row is not None:
            if status in OPEN_STATUSES:
                # 在途/待核对 → 持锁；已存在则更新状态
                conn.execute(
                    "INSERT INTO doc_locks (doc_no, attempt_no, status, created_at, updated_at) "
                    "VALUES (?,?,?,?,?) ON CONFLICT(doc_no) DO UPDATE SET status = excluded.status, "
                    "attempt_no = excluded.attempt_no, updated_at = excluded.updated_at",
                    (row["doc_no"], attempt_no, status, now(), now()),
                )
            else:
                # 终态（confirmed/failed 不再需要拦截新提交）→ 释放锁
                conn.execute("DELETE FROM doc_locks WHERE doc_no = ?", (row["doc_no"],))
        conn.commit()


def count_attempts_by_status() -> dict[str, int]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS c FROM submission_attempts GROUP BY status"
        ).fetchall()
    counts = {status: 0 for status in ATTEMPT_STATUSES}
    counts.update({r["status"]: r["c"] for r in rows})
    return counts


def recover_stale_attempts(timeout_seconds: int) -> list[str]:
    """崩溃恢复：in_flight 超过 timeout_seconds 仍未确认的尝试转 unknown 并锁单。"""
    if timeout_seconds <= 0:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)).astimezone()
    cutoff_text = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    recovered: list[str] = []
    with connect() as conn:
        rows = conn.execute(
            "SELECT attempt_no FROM submission_attempts WHERE status = 'in_flight' AND updated_at <= ?",
            (cutoff_text,),
        ).fetchall()
        for row in rows:
            ts = now()
            conn.execute(
                "UPDATE submission_attempts SET status = 'unknown', "
                "last_error = '进程中断后自动回收：在途尝试超过锁定时限，转待核对', updated_at = ? "
                "WHERE attempt_no = ?",
                (ts, row["attempt_no"]),
            )
            attempt = conn.execute(
                "SELECT doc_no FROM submission_attempts WHERE attempt_no = ?", (row["attempt_no"],)
            ).fetchone()
            conn.execute(
                "INSERT INTO doc_locks (doc_no, attempt_no, status, created_at, updated_at) "
                "VALUES (?,?, 'unknown', ?, ?) ON CONFLICT(doc_no) DO UPDATE SET "
                "status = 'unknown', attempt_no = excluded.attempt_no, updated_at = excluded.updated_at",
                (attempt["doc_no"], row["attempt_no"], ts, ts),
            )
            recovered.append(row["attempt_no"])
        conn.commit()
    return recovered


def confirm_attempt(attempt: dict, audit_detail: str) -> tuple[dict, dict]:
    """单事务确认：插入正式回执 + 尝试置 confirmed + 写 submit 审计。

    任何一步失败整体回滚——旧的已入账回执原样保留，绝不出现「覆盖后无法恢复」。
    返回 (submission, attempt)。重复确认（已 confirmed）直接返回既有数据。
    """
    attempt_no = attempt["attempt_no"]
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM submission_attempts WHERE attempt_no = ?", (attempt_no,)
        ).fetchone()
        if row is None:
            raise LookupError(f"尝试 {attempt_no} 不存在")
        if row["status"] == "confirmed" and row["submission_id"] is not None:
            existing = conn.execute(
                "SELECT * FROM submissions WHERE id = ?", (row["submission_id"],)
            ).fetchone()
            conn.commit()
            return _submission(existing), _attempt(row)

        created = now()
        cur = conn.execute(
            "INSERT INTO submissions (receipt_no, idem_key, doc_no, version, payload_json, "
            "payload_sha, created_at) VALUES (?,?,?,?,?,?,?)",
            (
                row["receipt_no"], row["idem_key"], row["doc_no"], row["version"],
                row["payload_json"], row["payload_sha"], created,
            ),
        )
        submission_id = cur.lastrowid
        conn.execute(
            "UPDATE submission_attempts SET status = 'confirmed', submission_id = ?, "
            "last_error = '', updated_at = ? WHERE attempt_no = ?",
            (submission_id, created, attempt_no),
        )
        conn.execute("DELETE FROM doc_locks WHERE doc_no = ?", (row["doc_no"],))
        conn.execute(
            "INSERT INTO audit_events (submission_id, receipt_no, doc_no, version, action, "
            "detail, created_at) VALUES (?,?,?,?,?,?,?)",
            (
                submission_id, row["receipt_no"], row["doc_no"], row["version"],
                "submit", audit_detail, created,
            ),
        )
        submission = conn.execute(
            "SELECT * FROM submissions WHERE id = ?", (submission_id,)
        ).fetchone()
        updated = conn.execute(
            "SELECT * FROM submission_attempts WHERE attempt_no = ?", (attempt_no,)
        ).fetchone()
        conn.commit()
    return _submission(submission), _attempt(updated)


def add_attempt_audit(attempt: dict, action: str, detail: str) -> None:
    add_audit(
        action, attempt["doc_no"], attempt["version"], detail,
        {"id": attempt.get("submission_id"), "receipt_no": attempt["receipt_no"]},
    )


def upsert_remote_receipt(
    receipt_no: str, idem_key: str, doc_no: str, version: int, payload_sha: str
) -> dict:
    created = now()
    with connect() as conn:
        # 远端账本按回执号/幂等键双重去重：重复送达同一笔永远只保留一行
        existing = conn.execute(
            "SELECT * FROM remote_receipts WHERE receipt_no = ? OR idem_key = ?",
            (receipt_no, idem_key),
        ).fetchone()
        if existing is not None:
            conn.execute(
                "UPDATE remote_receipts SET receipt_no = ?, idem_key = ?, doc_no = ?, "
                "version = ?, payload_sha = ? WHERE id = ?",
                (receipt_no, idem_key, doc_no, int(version), payload_sha, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO remote_receipts (receipt_no, idem_key, doc_no, version, payload_sha, "
                "created_at) VALUES (?,?,?,?,?,?)",
                (receipt_no, idem_key, doc_no, int(version), payload_sha, created),
            )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM remote_receipts WHERE idem_key = ?", (idem_key,)
        ).fetchone()
    return {
        "receipt_no": row["receipt_no"],
        "idem_key": row["idem_key"],
        "doc_no": row["doc_no"],
        "version": row["version"],
        "payload_sha": row["payload_sha"],
    }


def find_remote_receipt(idem_key: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM remote_receipts WHERE idem_key = ?", (idem_key,)
        ).fetchone()
    if not row:
        return None
    return {
        "receipt_no": row["receipt_no"],
        "idem_key": row["idem_key"],
        "doc_no": row["doc_no"],
        "version": row["version"],
        "payload_sha": row["payload_sha"],
    }


def get_remote_receipt(receipt_no: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM remote_receipts WHERE receipt_no = ?", (receipt_no,)
        ).fetchone()
    if not row:
        return None
    return {
        "receipt_no": row["receipt_no"],
        "idem_key": row["idem_key"],
        "doc_no": row["doc_no"],
        "version": row["version"],
        "payload_sha": row["payload_sha"],
    }


def count_remote_receipts() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM remote_receipts").fetchone()["c"]
