"""路径与常量。数据目录可用 FORMFORGE_DATA_DIR 覆盖（容器内为 /app/data 命名卷）。"""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "formforge · 表单录入 RPA 字段映射与幂等提交"
APP_SHORT = "formforge"

DATA_DIR = Path(os.environ.get("FORMFORGE_DATA_DIR", "data"))
DB_PATH = DATA_DIR / "formforge.db"
SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample_data"

SEED_VERSION = "2026-03-01"
RULESET_FILE = SAMPLE_DIR / "mapping_rules.json"
CONTRACT_FILE = SAMPLE_DIR / "target_contract.json"
SOURCE_DOCS_FILE = SAMPLE_DIR / "source_docs.json"
