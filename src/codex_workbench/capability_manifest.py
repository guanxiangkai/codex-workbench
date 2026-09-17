"""专业能力 MCP 的一次性运行清单与安全调用账本。"""

from __future__ import annotations

import json
import os
import re
import hashlib
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_TYPES = frozenset({"reasoning", "multimodal", "speech_to_text", "text_to_speech", "embedding", "rerank"})
_MODEL_FIELDS = ("id", "model_type", "base_url", "model", "protocol", "voice", "credential_ref", "last_verified_at")
_EVENT_TOOLS = frozenset({"reasoning_chat", "multimodal_image", "speech_to_text", "text_to_speech", "embedding", "rerank", "invalid"})
_EVENT_ERRORS = frozenset({"request_invalid", "capability_error", "model_not_allowed", "manifest_invalid", "credential_unavailable",
                           "credential_format", "credential_target", "timeout", "response_oversize", "response_invalid",
                           "network_error", "unauthorized", "forbidden", "not_found", "rate_limited", "server_error", "read_only_tts_unavailable"})


class CapabilityManifestError(ValueError):
    """运行清单不满足隔离执行边界时抛出。"""


@dataclass(frozen=True)
class CapabilityBinding:
    """交给执行器的受限 MCP 配置来源。"""

    manifest_path: str
    server_name: str = "workbench-run-capabilities"


def create_capability_manifest(data_dir: str | Path, run_id: str, cwd: str, sandbox: str,
                               models: list[dict[str, Any]], resources: list[dict[str, Any]] | None = None,
                               expires_in_seconds: int = 7200) -> CapabilityBinding:
    """创建仅服务一个运行的不可变模型快照及有效期标记。"""
    try:
        normalized_run_id = str(uuid.UUID(run_id))
    except (ValueError, TypeError) as exc:
        raise CapabilityManifestError("运行标识无效") from exc
    if sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
        raise CapabilityManifestError("运行沙箱无效")
    workspace = Path(cwd)
    if not workspace.is_absolute() or not workspace.is_dir():
        raise CapabilityManifestError("运行工作目录无效")
    if not isinstance(expires_in_seconds, int) or not 60 <= expires_in_seconds <= 86400:
        raise CapabilityManifestError("能力清单有效期无效")
    if not isinstance(models, list) or not 1 <= len(models) <= 20:
        raise CapabilityManifestError("已选择模型必须为 1 至 20 项")
    selected = [_model_view(model) for model in models]
    if len({model["id"] for model in selected}) != len(selected):
        raise CapabilityManifestError("已选择模型存在重复")
    snapshots = _resource_views(resources or [], workspace.resolve())
    run_dir = Path(data_dir).resolve() / "runs" / normalized_run_id
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if run_dir.is_symlink() or stat.S_IMODE(run_dir.stat().st_mode) & 0o077:
        raise CapabilityManifestError("运行目录权限无效")
    manifest = run_dir / "capability-manifest.json"
    active = run_dir / ".capability-active"
    if manifest.exists() or active.exists():
        raise CapabilityManifestError("运行能力清单已存在")
    value = {"schema_version": 1, "run_id": normalized_run_id, "cwd": str(workspace.resolve()), "sandbox": sandbox,
             "created_at": int(time.time()), "expires_at": int(time.time()) + expires_in_seconds,
             "models": selected, "resource_snapshots": snapshots, "active_marker": str(active),
             "events_path": str(run_dir / "capability-events.jsonl"), "artifacts_dir": str(run_dir / "capability-artifacts"),
             "artifacts_path": str(run_dir / "capability-artifacts.jsonl")}
    _write_new(active, b"active\n")
    try:
        _write_new(manifest, json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        Path(value["artifacts_dir"]).mkdir(mode=0o700)
    except BaseException:
        for path in (manifest, active):
            try:
                path.unlink()
            except OSError:
                pass
        raise
    return CapabilityBinding(str(manifest))


def revoke_capability_manifest(manifest_path: str | Path) -> None:
    """撤销一个运行的能力入口；已启动消费者也会在下一检查点停止。"""
    manifest = _safe_manifest_path(manifest_path)
    value = _load_manifest(manifest)
    marker = Path(value["active_marker"])
    try:
        marker.unlink()
    except FileNotFoundError:
        pass


def load_active_manifest(manifest_path: str | Path) -> dict[str, Any]:
    """读取并核验清单、生命周期标记、有效期和受限本机路径。"""
    manifest = _safe_manifest_path(manifest_path)
    value = _load_manifest(manifest)
    if time.time() >= value["expires_at"] or not Path(value["active_marker"]).is_file():
        raise CapabilityManifestError("本次运行的专业能力入口已失效")
    return value


def read_capability_events(manifest_path: str | Path) -> list[dict[str, Any]]:
    """返回可展示的安全调用记录，不包含原始输入、输出或服务端错误体。"""
    value = _load_manifest(_safe_manifest_path(manifest_path))
    path = Path(value["events_path"])
    if not path.exists():
        return []
    if path.is_symlink() or path.stat().st_size > 128 * 1024:
        raise CapabilityManifestError("能力调用记录无效")
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CapabilityManifestError("能力调用记录无效") from exc
        if not _valid_event(item):
            raise CapabilityManifestError("能力调用记录无效")
        records.append(item)
    return records


def append_capability_event(manifest: dict[str, Any], event: dict[str, Any]) -> None:
    """追加严格投影后的调用结果，避免将模型内容或异常持久化。"""
    allowed = {"tool", "model_id", "started_at", "finished_at", "success", "error_code", "input_tokens", "output_tokens"}
    if set(event) - allowed or not _valid_event(event):
        raise CapabilityManifestError("能力调用记录无效")
    path = Path(manifest["events_path"])
    if path.exists() and (path.is_symlink() or path.stat().st_size > 128 * 1024):
        raise CapabilityManifestError("能力调用记录已达上限")
    encoded = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(fd, encoded)
    finally:
        os.close(fd)


def register_capability_artifact(manifest: dict[str, Any], path: Path) -> None:
    """登记本运行生成的媒体产物，后续上传时同时核对名称、位置和摘要。"""
    root = Path(manifest["artifacts_dir"])
    if path.parent.resolve() != root.resolve() or not path.is_file() or path.is_symlink():
        raise CapabilityManifestError("能力产物无效")
    digest = _sha256(path)
    record = {"path": str(path.resolve()), "sha256": digest}
    _append_private(Path(manifest["artifacts_path"]), (json.dumps(record, separators=(",", ":")) + "\n").encode())


def listed_capability_artifact(manifest: dict[str, Any], path: Path, *, content_sha256: str | None = None) -> bool:
    """校验本次产物；可对即将发送的内存字节摘要核验，避免二次读文件竞态。"""
    index = Path(manifest["artifacts_path"])
    if not index.exists() or index.is_symlink() or index.stat().st_size > 64 * 1024:
        return False
    try:
        for line in index.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            if isinstance(item, dict) and set(item) == {"path", "sha256"} and item["path"] == str(path.resolve()) and item["sha256"] == (content_sha256 if content_sha256 is not None else _sha256(path)):
                return True
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return False


def _model_view(model: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(model, dict) or model.get("validation_status", "verified") != "verified":
        raise CapabilityManifestError("只能挂载已验证模型")
    value = {key: model.get(key) for key in _MODEL_FIELDS}
    if (not isinstance(value["id"], str) or not value["id"] or value["model_type"] not in _TYPES
            or any(not isinstance(value[key], str) for key in ("base_url", "model", "protocol", "voice", "credential_ref"))):
        raise CapabilityManifestError("模型快照无效")
    return value


def _resource_views(resources: list[dict[str, Any]], workspace: Path) -> list[dict[str, str]]:
    if not isinstance(resources, list) or len(resources) > 20:
        raise CapabilityManifestError("资源快照无效")
    values: list[dict[str, str]] = []
    for item in resources:
        path = item.get("snapshotPath") if isinstance(item, dict) else None
        digest = item.get("sha256") if isinstance(item, dict) else None
        if not isinstance(path, str) or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise CapabilityManifestError("资源快照无效")
        resolved = Path(path).resolve(strict=True)
        if resolved.is_symlink() or not resolved.is_file() or str(resolved).startswith(str(workspace) + os.sep):
            raise CapabilityManifestError("资源快照无效")
        if _sha256(resolved) != digest:
            raise CapabilityManifestError("资源快照已变化")
        values.append({"path": str(resolved), "sha256": digest})
    return values


def _safe_manifest_path(path: str | Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or candidate.name != "capability-manifest.json" or candidate.is_symlink():
        raise CapabilityManifestError("能力清单路径无效")
    return candidate


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        info = path.stat()
        if not path.is_file() or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 64 * 1024:
            raise ValueError()
        value = json.loads(path.read_text(encoding="utf-8"))
        required = {"schema_version", "run_id", "cwd", "sandbox", "created_at", "expires_at", "models", "resource_snapshots", "active_marker", "events_path", "artifacts_dir", "artifacts_path"}
        if not isinstance(value, dict) or set(value) != required or value["schema_version"] != 1:
            raise ValueError()
        str(uuid.UUID(value["run_id"]))
        if not Path(value["cwd"]).is_dir() or value["sandbox"] not in {"read-only", "workspace-write", "danger-full-access"}:
            raise ValueError()
        if not isinstance(value["expires_at"], int) or not isinstance(value["created_at"], int) or value["expires_at"] <= value["created_at"]:
            raise ValueError()
        if not isinstance(value["models"], list) or not 1 <= len(value["models"]) <= 20:
            raise ValueError()
        for model in value["models"]:
            _model_view({**model, "validation_status": "verified"})
        if not isinstance(value["resource_snapshots"], list) or any(not isinstance(item, dict) or set(item) != {"path", "sha256"} or not isinstance(item["path"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) for item in value["resource_snapshots"]):
            raise ValueError()
        run_dir = path.parent.resolve()
        for key in ("active_marker", "events_path", "artifacts_dir", "artifacts_path"):
            if Path(value[key]).parent.resolve() != run_dir:
                raise ValueError()
        return value
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise CapabilityManifestError("能力清单无效") from exc


def _write_new(path: Path, payload: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def _append_private(path: Path, payload: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try: os.write(fd, payload)
    finally: os.close(fd)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as reader:
        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_event(event: object) -> bool:
    """事件只保留可枚举的执行事实，避免未验证输入进入持久化账本。"""
    if not isinstance(event, dict) or set(event) != {"tool", "model_id", "started_at", "finished_at", "success", "error_code", "input_tokens", "output_tokens"}:
        return False
    if event.get("tool") not in _EVENT_TOOLS or not isinstance(event.get("model_id"), str) or len(event["model_id"]) > 80:
        return False
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}", event["model_id"]) and event["model_id"] != "":
        return False
    if not all(isinstance(event.get(key), int) and not isinstance(event[key], bool) and 0 <= event[key] <= 10**15 for key in ("started_at", "finished_at")):
        return False
    if event["finished_at"] < event["started_at"] or not isinstance(event["success"], bool):
        return False
    if event["success"] != (event["error_code"] is None):
        return False
    if not event["success"] and event["error_code"] not in _EVENT_ERRORS:
        return False
    return all(value is None or (isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10**12)
               for value in (event["input_tokens"], event["output_tokens"]))
