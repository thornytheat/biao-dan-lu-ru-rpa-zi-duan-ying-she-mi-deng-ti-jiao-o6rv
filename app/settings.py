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

# 演示/测试用故障注入开关：开启后允许通过 API 让「远端报销系统」在提交时失败或超时。
# 生产环境置为 0 后注入接口直接 404，正常提交流程不受影响。
ENABLE_FAULTS = os.environ.get("FORMFORGE_ENABLE_FAULTS", "1") not in ("0", "false", "False", "")
# in_flight 尝试超过该秒数仍未确认，启动/访问时自动转 unknown（崩溃恢复），0 表示不自动回收。
ATTEMPT_LOCK_TIMEOUT_SECONDS = int(os.environ.get("FORMFORGE_ATTEMPT_TIMEOUT", "300"))
