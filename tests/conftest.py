"""测试前置：独立数据目录 + 每个用例清空业务表（源单据 seed 幂等保留）。"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("FORMFORGE_DATA_DIR", tempfile.mkdtemp(prefix="formforge-test-"))


@pytest.fixture(autouse=True)
def clean_state():
    from app import store

    store.init_db()
    with store.connect() as conn:
        # 每个用例都从 sample_data 重新 seed，避免上一个用例改过源单据影响下一个
        for table in ("submissions", "audit_events", "previews", "rule_overrides", "source_docs", "meta"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    store.seed_docs()
    yield
