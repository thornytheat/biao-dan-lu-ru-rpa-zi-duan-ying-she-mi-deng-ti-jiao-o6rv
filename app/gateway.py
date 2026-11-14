"""目标报销系统网关（本地模拟）。

真实环境里这一层是远程表单/入账接口：调用可能超时、进程可能在「已入账但响应丢失」的
半路上崩掉。这里用 SQLite 表 ``remote_receipts`` 模拟远端账本，并提供一次性故障注入，
让提交路径可以稳定复现 *明确失败* 与 *结果不明* 两种异常，验证可恢复尝试机制。

故障模式（仅在 settings.ENABLE_FAULTS 时可注入）：
- ``none``           正常
- ``remote_error``   远端明确拒绝（确定没入账）→ 尝试 failed
- ``timeout``        请求发出后超时，远端可能已入账也可能没入账 → 尝试 unknown（待核对）
- ``crash``          远端已写入回执，但本地在确认前崩溃 → 重启回收后 unknown，可认领
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from . import settings, store

VALID_MODES = ("none", "remote_error", "timeout", "crash")


class RemoteRejected(RuntimeError):
    """远端明确拒绝：可以确定没有入账。"""


class RemoteAmbiguous(RuntimeError):
    """结果不明：远端可能已入账，也可能没有，必须先核对再重试。"""


@dataclass
class RemoteReceipt:
    receipt_no: str
    idem_key: str
    doc_no: str
    version: int
    payload_sha: str

    def as_dict(self) -> dict:
        return {
            "receipt_no": self.receipt_no,
            "idem_key": self.idem_key,
            "doc_no": self.doc_no,
            "version": int(self.version),
            "payload_sha": self.payload_sha,
        }


class _FaultPolicy:
    """进程内一次性故障策略：触发一次后自动恢复 none（只影响下一次提交）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mode = "none"

    def get(self) -> str:
        with self._lock:
            return self._mode

    def set(self, mode: str) -> None:
        if mode not in VALID_MODES:
            raise ValueError(f"未知故障模式 {mode!r}，可选：{', '.join(VALID_MODES)}")
        with self._lock:
            self._mode = mode

    def consume(self) -> str:
        with self._lock:
            mode = self._mode
            self._mode = "none"
            return mode


policy = _FaultPolicy()


def configure_fault(mode: str) -> dict:
    """演示/测试入口：设置下一次提交的故障模式。生产关闭故障注入后路由层不会放行。"""
    policy.set(mode)
    return {"next_fault": policy.get(), "enabled": settings.ENABLE_FAULTS}


def current_fault() -> str:
    return policy.get()


def reset_fault() -> None:
    policy.set("none")


def find_remote_receipt(idem_key: str) -> RemoteReceipt | None:
    """按幂等键核对远端账本：模拟「查询这笔到底入账没有」。"""
    row = store.find_remote_receipt(idem_key)
    return RemoteReceipt(**row) if row else None


def submit(
    receipt_no: str, idem_key: str, doc_no: str, version: int, payload_sha: str
) -> RemoteReceipt:
    """把一张回执提交到远端。

    远端账本自身按 idem_key 去重：同一幂等键的重复送达直接返回既有回执，
    这是「重试不新增第二张业务回执」的最后一道防线。
    """
    mode = policy.consume()

    if mode == "remote_error":
        raise RemoteRejected("远端报销系统明确拒绝（502 业务错误）：本次确定未入账，可修正后重试")

    # timeout：调用方拿不到任何确定结论，不能假装成功也不能假装没入账。
    if mode == "timeout":
        raise RemoteAmbiguous("请求发出后超时：远端是否已入账无法确认，单据已锁定待核对")

    # crash / none 都先把回执落到远端账本；crash 模式下在本地确认前抛出，模拟进程中断。
    row = store.upsert_remote_receipt(
        receipt_no=receipt_no,
        idem_key=idem_key,
        doc_no=doc_no,
        version=int(version),
        payload_sha=payload_sha,
    )
    if mode == "crash":
        raise RemoteAmbiguous(
            f"远端已生成回执 {receipt_no}，但本地在确认前中断：需按幂等键核对并认领该回执"
        )
    return RemoteReceipt(**row)
