"""凭证结构的受控消费者与显式探测入口；普通凭证列表从不调用此模块。"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import TYPE_CHECKING, Any

from .credential_schema import CredentialSchemaError, MAX_PROBE_BYTES, parse_payload, probe_shapes, probe_structure, validate_structure

if TYPE_CHECKING:
    from .credentials import CredentialCatalog

_ENTRY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_MAX_REPLY_BYTES = 16384


class CredentialProbeError(ValueError):
    """只携带固定代码与提示，不保存子进程、stdout、stderr 或原始异常。"""

    def __init__(self, code: str) -> None:
        messages = {"probe_invalid": "凭证结构探测参数无效", "probe_failed": "凭证结构探测失败",
                    "probe_timeout": "凭证结构探测超时", "probe_reply_invalid": "凭证结构探测结果无效"}
        self.code = code if code in messages else "probe_failed"
        super().__init__(messages[self.code])


def probe_entry(catalog: CredentialCatalog, entry_id: str, *, timeout: float = 30.0) -> dict[str, Any]:
    """显式探测一个条目，返回安全 ticket，不写索引或组织资料。

    主代理仅在获准探测后手动调用。先固定清单 revision 和本机 generation，
    再让保险库通过 exec-stdin 直接把明文交给固定消费者。父进程只能收到
    消费者 stdout；保险库和消费者 stderr 始终捕获且不回显。执行时限最多
    60 秒；成功响应最多 16 KiB，且必须通过严格白名单结构校验。
    """
    if not isinstance(entry_id, str) or not _ENTRY_RE.fullmatch(entry_id):
        raise CredentialProbeError("probe_invalid")
    if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise CredentialProbeError("probe_invalid")
    snapshot = catalog._snapshot(force=True)
    entry = next((item for item in catalog._entry_views(snapshot) if item["id"] == entry_id), None)
    if entry is None:
        raise CredentialProbeError("probe_invalid")
    revision, generation = entry.get("revision"), entry["structure_generation"]
    if type(revision) is not int or revision < 1 or type(generation) is not int or generation < 0:
        raise CredentialProbeError("probe_invalid")
    package_root = Path(__file__).resolve().parents[1]
    command = [str(catalog.command), "exec-stdin", entry_id, sys.executable, "-B", "-m", "codex_workbench.credential_probe"]
    environment = {**os.environ, "PYTHONPATH": str(package_root)}
    error_code = None
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                cwd=package_root.parent, env=environment, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        error_code = "probe_timeout"
    except (OSError, ValueError):
        error_code = "probe_failed"
    if error_code:
        # 在 except 之外抛固定错误，不保留含子进程输出的异常上下文。
        raise CredentialProbeError(error_code)
    if result.returncode != 0 or result.stderr:
        raise CredentialProbeError("probe_failed")
    if len(result.stdout) > _MAX_REPLY_BYTES:
        raise CredentialProbeError("probe_reply_invalid")
    try:
        structure = validate_structure(parse_payload(result.stdout.decode("utf-8")))
    except (UnicodeDecodeError, CredentialSchemaError):
        raise CredentialProbeError("probe_reply_invalid") from None
    del result
    return {"entry_id": entry_id, "revision": revision, "expected_generation": generation, "structure": structure}


def apply_probe(catalog: CredentialCatalog, ticket: dict[str, Any]) -> dict[str, Any]:
    """显式应用安全探测 ticket；再次核对修订号并以 generation CAS 防止覆盖。"""
    if not isinstance(ticket, dict) or set(ticket) != {"entry_id", "revision", "expected_generation", "structure"}:
        raise CredentialProbeError("probe_invalid")
    try:
        structure = validate_structure(ticket["structure"])
    except CredentialSchemaError:
        raise CredentialProbeError("probe_reply_invalid") from None
    return catalog.index_structure(ticket["entry_id"], ticket["revision"], structure,
                                   expected_generation=ticket["expected_generation"], source="probe")


def main(argv: list[str] | None = None) -> int:
    """默认返回原索引；显式 --shapes 返回独立诊断，均只消费有界 stdin。

    --shapes 只供已获授权的结构统计使用，不经过 probe_entry/apply_probe。
    未知参数在读取 stdin 前拒绝，错误不回显参数或输入。
    """
    try:
        arguments = sys.argv[1:] if argv is None else argv
        if arguments not in ([], ["--shapes"]):
            raise CredentialSchemaError()
        raw = sys.stdin.buffer.read(MAX_PROBE_BYTES + 1)
        result = probe_shapes(raw) if arguments else probe_structure(raw)
        del raw
        sys.stdout.write(json.dumps(result, ensure_ascii=True, separators=(",", ":")) + "\n")
        return 0
    except Exception:
        # 绝不打印 traceback、原始解析异常或异常对象 repr。
        sys.stdout.write('{"error":"structure_probe_failed"}\n')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
