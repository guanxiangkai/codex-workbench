"""随单次 Codex 运行启动的 stdio MCP 专业能力入口。"""

from __future__ import annotations

import json
import hashlib
import base64
import os
import selectors
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

if __package__:
    from .capability_manifest import CapabilityManifestError, append_capability_event, load_active_manifest, listed_capability_artifact, register_capability_artifact, _sha256
    from .media_validation import valid_media
else:
    # Codex 使用清单中的绝对脚本路径启动 stdio 进程，不能依赖外部 PYTHONPATH。
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from codex_workbench.capability_manifest import CapabilityManifestError, append_capability_event, load_active_manifest, listed_capability_artifact, register_capability_artifact, _sha256
    from codex_workbench.media_validation import valid_media


_TOOLS = {
    "reasoning_chat": "reasoning", "multimodal_image": "multimodal", "speech_to_text": "speech_to_text",
    "text_to_speech": "text_to_speech", "embedding": "embedding", "rerank": "rerank",
}
_MAX_CALLS = 32
_MAX_REQUEST = 64 * 1024
_MAX_FILE = 20 * 1024 * 1024
_EVENT_ERRORS = frozenset({"request_invalid", "capability_error", "model_not_allowed", "manifest_invalid", "credential_unavailable",
                           "credential_format", "credential_target", "timeout", "response_oversize", "response_invalid",
                           "network_error", "unauthorized", "forbidden", "not_found", "rate_limited", "server_error", "read_only_tts_unavailable"})


class CapabilityRuntime:
    """只从不可变运行清单取模型和路径，拒绝任何运行时端点或凭据输入。"""

    def __init__(self, manifest_path: str, vault_command: Path | None = None) -> None:
        self.manifest_path = manifest_path
        self.vault_command = vault_command or Path.home() / ".codex/scripts/key-vault/key-vault.sh"
        self._calls = 0
        self._lock = threading.Lock()

    def tools(self) -> list[dict[str, Any]]:
        """按本次已选模型生成严格的、可由 Codex 直接调用的工具契约。"""
        manifest = load_active_manifest(self.manifest_path)
        identifiers = {kind: [model["id"] for model in manifest["models"] if model["model_type"] == kind] for kind in set(_TOOLS.values())}
        return [_tool_schema(name, identifiers[kind], manifest["sandbox"]) for name, kind in _TOOLS.items() if identifiers[kind]]

    def call(self, name: str, arguments: object) -> dict[str, Any]:
        """校验调用目标后在独立受控消费者中执行一次请求。"""
        started = int(time.time() * 1000)
        model_id = arguments.get("model_id") if isinstance(arguments, dict) else None
        event = {"tool": name if name in _TOOLS else "invalid", "model_id": "",
                 "started_at": started, "finished_at": started, "success": False, "error_code": "request_invalid",
                 "input_tokens": None, "output_tokens": None}
        try:
            manifest = load_active_manifest(self.manifest_path)
            if name not in _TOOLS or not isinstance(arguments, dict) or set(arguments) - _argument_keys(name):
                raise CapabilityManifestError("请求参数无效")
            if name == "text_to_speech" and manifest["sandbox"] == "read-only":
                raise CapabilityManifestError("read_only_tts_unavailable")
            encoded = json.dumps(arguments, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(encoded) > _MAX_REQUEST:
                raise CapabilityManifestError("请求超过大小限制")
            if not isinstance(model_id, str):
                raise CapabilityManifestError("模型标识无效")
            model = next((item for item in manifest["models"] if item["id"] == model_id), None)
            if model is None or model["model_type"] != _TOOLS[name]:
                raise CapabilityManifestError("模型不属于本次已授权能力")
            # 仅在模型已与本次不可变清单匹配后记入其规范 ID，拒绝输入永不进入账本。
            event["tool"], event["model_id"] = name, model["id"]
            with self._lock:
                if self._calls >= _MAX_CALLS:
                    raise CapabilityManifestError("本次运行的专业能力调用已达上限")
                self._calls += 1
            request = self._prepare_request(name, arguments, manifest)
            result = self._invoke(manifest, model, request)
            if not result.get("success"):
                raise CapabilityManifestError(_safe_code(result.get("code")))
            if name == "text_to_speech":
                result = self._store_speech_artifact(manifest, result)
            event.update(success=True, error_code=None, input_tokens=result.get("input_tokens"), output_tokens=result.get("output_tokens"))
            return {key: value for key, value in result.items() if key not in {"success", "input_tokens", "output_tokens"}}
        except CapabilityManifestError as exc:
            event["error_code"] = _safe_code(str(exc))
            return {"isError": True, "content": [{"type": "text", "text": "专业能力调用被拒绝：" + event["error_code"]}]}
        finally:
            event["finished_at"] = int(time.time() * 1000)
            try:
                # 清单被撤销后不再持久化，因为运行目录的生命周期已经结束。
                append_capability_event(load_active_manifest(self.manifest_path), event)
            except CapabilityManifestError:
                pass

    def _prepare_request(self, name: str, arguments: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
        request = {"tool": name}
        for key in ("messages", "max_tokens", "prompt", "input", "query", "documents", "voice"):
            if key in arguments:
                request[key] = arguments[key]
        if name in {"multimodal_image", "speech_to_text"}:
            source = arguments.get("image_path" if name == "multimodal_image" else "audio_path")
            path = self._input_path(source, manifest)
            raw, mime, suffix = _read_regular(path, _MAX_FILE, "image" if name == "multimodal_image" else "audio")
            # 对实际交给接口的这份内存字节校验，不能仅信任较早的路径检查。
            digest = hashlib.sha256(raw).hexdigest()
            selected = next((item for item in manifest["resource_snapshots"] if item["path"] == str(path)), None)
            if selected is not None:
                if digest != selected["sha256"]:
                    raise CapabilityManifestError("选定资源快照已变化")
            elif not listed_capability_artifact(manifest, path, content_sha256=digest):
                raise CapabilityManifestError("能力产物已变化")
            request["image_bytes" if name == "multimodal_image" else "audio_bytes"] = raw
            request["image_mime" if name == "multimodal_image" else "audio_mime"] = mime
            request["audio_suffix"] = suffix if name == "speech_to_text" else request.get("audio_suffix")
        return request

    def _store_speech_artifact(self, manifest: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        """TTS 只把音频写入本运行产物目录，并登记摘要供后续 STT 使用。"""
        try:
            audio = base64.b64decode(result.pop("audio"), validate=True)
        except (KeyError, TypeError, ValueError) as exc:
            raise CapabilityManifestError("response_invalid") from exc
        if not _valid_media(Path("speech.mp3"), audio, "audio"):
            raise CapabilityManifestError("response_invalid")
        path = Path(manifest["artifacts_dir"]) / ("speech-" + uuid.uuid4().hex + ".mp3")
        _write_private(path, audio)
        register_capability_artifact(manifest, path)
        return {"success": True, "artifact_path": str(path), **{key: value for key, value in result.items() if key != "success"}}

    def _input_path(self, source: object, manifest: dict[str, Any]) -> Path:
        if not isinstance(source, str) or not source:
            raise CapabilityManifestError("输入文件无效")
        candidate = Path(source)
        if not candidate.is_absolute():
            raise CapabilityManifestError("媒体输入必须使用本次已选择的资源快照绝对路径")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise CapabilityManifestError("输入文件不可用") from exc
        selected = next((item for item in manifest["resource_snapshots"] if item["path"] == str(resolved)), None)
        if selected is not None:
            if _sha256(resolved) != selected["sha256"]:
                raise CapabilityManifestError("选定资源快照已变化")
        elif not listed_capability_artifact(manifest, resolved):
            raise CapabilityManifestError("媒体输入不是本次已选择资源或能力产物")
        return resolved

    def _invoke(self, manifest: dict[str, Any], model: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        run_dir = Path(self.manifest_path).parent
        with tempfile.TemporaryDirectory(prefix="capability-", dir=run_dir) as directory:
            work = Path(directory)
            config_path, request_path = work / "model.json", work / "request.json"
            _write_private(config_path, json.dumps(model, ensure_ascii=False).encode())
            # Bytes from selected files never enter the request file. The isolated worker gets them via a pipe.
            file_key = "image_bytes" if "image_bytes" in request else "audio_bytes" if "audio_bytes" in request else None
            file_payload = request.pop(file_key, None) if file_key else None
            request_value = request
            if file_key:
                request_value = {**request, file_key + "_path": str(work / "input.bin")}
                _write_private(work / "input.bin", file_payload)
            _write_private(request_path, json.dumps(request_value, ensure_ascii=False).encode())
            command = [sys.executable, "-I", str(Path(__file__).with_name("capability_worker.py")), str(config_path), str(request_path)]
            reference = model.get("credential_ref", "")
            if reference:
                if not self.vault_command.is_file():
                    raise CapabilityManifestError("credential_unavailable")
                command = [str(self.vault_command), "exec-stdin", reference[6:], *command]
            # File values are not part of provider credentials. For binary capabilities the worker reads a private side file.
            environment = {key: value for key, value in os.environ.items() if key not in {"OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "PYTHONPATH", "PYTHONHOME", "CODEX_HOME"}}
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       env=environment, start_new_session=True)
            try:
                output = _collect(process, self.manifest_path, 120)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
                if process.stdout is not None:
                    process.stdout.close()
            try:
                value = json.loads(output)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CapabilityManifestError("capability_error") from exc
            if not isinstance(value, dict) or not isinstance(value.get("success"), bool):
                raise CapabilityManifestError("capability_error")
            return value


def _argument_keys(name: str) -> set[str]:
    keys = {"model_id"}
    return keys | {
        "reasoning_chat": {"messages", "max_tokens"}, "multimodal_image": {"prompt", "image_path"},
        "speech_to_text": {"audio_path"}, "text_to_speech": {"input", "voice"}, "embedding": {"input"},
        "rerank": {"query", "documents"},
    }[name]


def _tool_schema(name: str, model_ids: list[str], sandbox: str) -> dict[str, Any]:
    """生成和服务端白名单一致的 JSON Schema，不提供 URL 或 Key 字段。"""
    model = {"type": "string", "enum": model_ids, "description": "本次运行已验证且已授权的模型 ID。"}
    path = {"type": "string", "minLength": 1, "maxLength": 4096,
            "pattern": "^(?![A-Za-z][A-Za-z0-9+.-]*://).+$",
            "description": "本次已选择资源快照或本运行生成产物的绝对路径，不能是 URL；扩展名和文件魔数都会验证。"}
    text = {"type": "string", "minLength": 1, "maxLength": 32768}
    base = {"type": "object", "additionalProperties": False, "properties": {"model_id": model}, "required": ["model_id"]}
    if name == "reasoning_chat":
        base.update(description="使用已验证的推理/对话模型完成文本任务；返回模型文本与可用 token 计数。",
                    properties={"model_id": model, "messages": {"type": "array", "minItems": 1, "maxItems": 32,
                    "items": {"type": "object", "additionalProperties": False, "required": ["role", "content"],
                    "properties": {"role": {"type": "string", "enum": ["system", "user", "assistant"]}, "content": text}}},
                    "max_tokens": {"type": "integer", "minimum": 1, "maximum": 8192}}, required=["model_id", "messages"])
    elif name == "multimodal_image":
        base.update(description="让已验证的图像理解模型分析一张已选择素材快照图片；只支持 PNG、JPEG、GIF、WebP，返回文本分析结果。",
                    properties={"model_id": model, "prompt": text, "image_path": path}, required=["model_id", "prompt", "image_path"])
    elif name == "speech_to_text":
        base.update(description="将已选择素材快照或本运行产物中的音频转写为文本；只支持 WAV、MP3、M4A、FLAC、Ogg，返回转写结果。",
                    properties={"model_id": model, "audio_path": path}, required=["model_id", "audio_path"])
    elif name == "text_to_speech":
        base.update(description="将短文本合成为音频；返回 Base64 编码音频，单次输出最多 512 KiB 原始音频。",
                    properties={"model_id": model, "input": {"type": "string", "minLength": 1, "maxLength": 16000},
                                "voice": {"type": "string", "minLength": 1, "maxLength": 100}}, required=["model_id", "input"])
    elif name == "embedding":
        base.update(description="为一条或多条文本生成向量；返回有序浮点向量列表。",
                    properties={"model_id": model, "input": {"oneOf": [{"type": "string", "minLength": 1, "maxLength": 8192},
                    {"type": "array", "minItems": 1, "maxItems": 64, "items": {"type": "string", "minLength": 1, "maxLength": 8192}}]}}, required=["model_id", "input"])
    else:
        base.update(description="按查询意图重排序候选文本；返回候选索引与相关性分数。",
                    properties={"model_id": model, "query": {"type": "string", "minLength": 1, "maxLength": 32768},
                    "documents": {"type": "array", "minItems": 1, "maxItems": 64, "items": {"type": "string", "minLength": 1, "maxLength": 8192}}}, required=["model_id", "query", "documents"])
    annotations = {"readOnlyHint": name != "text_to_speech", "destructiveHint": False, "openWorldHint": True}
    if name == "text_to_speech" and sandbox == "read-only":
        base["description"] += " 当前运行是 read-only，服务端会拒绝该调用；请改用允许写入的任务沙箱。"
    return {"name": name, "description": base.pop("description"), "inputSchema": base, "annotations": annotations}


def _read_regular(path: Path, maximum: int, media: str) -> tuple[bytes, str, str]:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError()
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            value = os.read(fd, maximum + 1)
        finally:
            os.close(fd)
        if len(value) > maximum:
            raise ValueError()
        descriptor = _media_descriptor(path, value, media)
        if descriptor is None:
            raise ValueError()
        return value, *descriptor
    except OSError as exc:
        raise CapabilityManifestError("输入文件不可用") from exc
    except ValueError as exc:
        raise CapabilityManifestError("输入文件超过大小限制") from exc


def _valid_media(path: Path, value: bytes, media: str) -> bool:
    return _media_descriptor(path, value, media) is not None


def _media_descriptor(path: Path, value: bytes, media: str) -> tuple[str, str] | None:
    """扩展名与最小容器结构同时匹配；摘要仍由调用方对这份内存字节校验。"""
    suffix = path.suffix.lower()
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
            ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".flac": "audio/flac", ".ogg": "audio/ogg"}.get(suffix)
    return (mime, suffix) if mime and valid_media(value, suffix, media) else None


def _collect(process: subprocess.Popen, manifest_path: str, timeout: float) -> bytes:
    selector = selectors.DefaultSelector(); output = bytearray(); deadline = time.monotonic() + timeout
    try:
        os.set_blocking(process.stdout.fileno(), False); selector.register(process.stdout, selectors.EVENT_READ)
        while selector.get_map():
            load_active_manifest(manifest_path)
            if time.monotonic() >= deadline:
                raise CapabilityManifestError("timeout")
            for key, _ in selector.select(0.1):
                block = os.read(key.fd, 65536)
                if not block: selector.unregister(key.fileobj)
                else:
                    output.extend(block)
                    if len(output) > 1024 * 1024: raise CapabilityManifestError("response_oversize")
        process.wait(timeout=2)
        return bytes(output)
    finally:
        selector.close()


def _write_private(path: Path, payload: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try: os.write(fd, payload)
    finally: os.close(fd)


def _safe_code(value: object) -> str:
    return value if value in _EVENT_ERRORS else "capability_error"


def serve_stdio(manifest_path: str) -> None:
    """最小 JSON-RPC stdio MCP，实现工具枚举和调用两种必需操作。"""
    runtime = CapabilityRuntime(manifest_path)
    for line in sys.stdin:
        try:
            message = json.loads(line); method = message.get("method"); ident = message.get("id")
            if method == "initialize": result = {"protocolVersion": message.get("params", {}).get("protocolVersion", "2024-11-05"), "capabilities": {"tools": {}}, "serverInfo": {"name": "workbench-run-capabilities", "version": "1"}}
            elif method == "tools/list": result = {"tools": runtime.tools()}
            elif method == "tools/call":
                params = message.get("params", {}); result = runtime.call(params.get("name"), params.get("arguments", {}))
                if "content" not in result: result = {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
            elif method == "notifications/initialized": continue
            else: raise ValueError("unsupported")
            if ident is not None: print(json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}, ensure_ascii=False), flush=True)
        except Exception:
            if 'ident' in locals() and ident is not None: print(json.dumps({"jsonrpc": "2.0", "id": ident, "error": {"code": -32600, "message": "Invalid Request"}}), flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 2: raise SystemExit(64)
    serve_stdio(sys.argv[1])
