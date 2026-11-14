"""SQLite 访问：源单据 seed、规则优先级覆盖、dry-run 预览、提交与回执、审计流水。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

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
"""


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
