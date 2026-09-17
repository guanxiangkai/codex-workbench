"""OpenAI 兼容模型的最小能力探测，避免把服务目录当作可用性证据。"""

from __future__ import annotations

import base64
import json
import math
import socket
import struct
import time
import uuid
import zlib
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from .model_endpoints import resolve_endpoint
from .media_validation import valid_media


_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_AUDIO_FIXTURE_BYTES = 512 * 1024
_PROBE_AUDIO = Path(__file__).with_name("fixtures") / "probe.wav"


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    """构造带 CRC 的 PNG 数据块。"""
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def _probe_png() -> bytes:
    """生成 32×32 的纯红 PNG，避免极小图片被视觉模型拒绝。"""
    width = height = 32
    scanlines = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header) + _png_chunk(b"IDAT", zlib.compress(scanlines)) + _png_chunk(b"IEND", b"")


_PNG = base64.b64encode(_probe_png()).decode("ascii")


class _NoRedirect(HTTPRedirectHandler):
    """禁止重定向，避免凭据被转发到另一个主机。"""

    def redirect_request(self, request, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _result(success: bool, code: str, message: str, **evidence: int) -> dict:
    value = {"success": success, "code": code, "message": message}
    if evidence:
        value["evidence"] = evidence
    return value


def _cancelled(cancel_event: Any) -> bool:
    return bool(cancel_event is not None and cancel_event.is_set())


def _validated_model(model: dict) -> tuple[str, str, str, str, str] | None:
    if not isinstance(model, dict):
        return None
    model_type = model.get("model_type")
    base_url = model.get("base_url")
    model_id = model.get("model")
    protocol = model.get("protocol")
    voice = model.get("voice", "alloy")
    if not isinstance(model_type, str) or model_type not in {"reasoning", "multimodal", "speech_to_text", "text_to_speech", "embedding", "rerank"}:
        return None
    if not all(isinstance(value, str) and value.strip() for value in (base_url, model_id, protocol, voice)):
        return None
    try:
        parsed = urlsplit(base_url)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    if parsed.query or parsed.fragment:
        return None
    if protocol not in {"openai-chat", "openai-responses"}:
        return None
    if model_type in {"speech_to_text", "text_to_speech", "embedding", "rerank"} and protocol != "openai-chat":
        return None
    return model_type, base_url.rstrip("/"), model_id.strip(), protocol, voice.strip()


def _opener_for(url: str):
    """保留系统代理；本机回环测试显式绕过代理。"""
    host = urlsplit(url).hostname
    handlers = [_NoRedirect()]
    if host in {"127.0.0.1", "::1", "localhost"}:
        handlers.insert(0, ProxyHandler({}))
    return build_opener(*handlers)


def _read_response(response, cancel_event: Any, deadline: float) -> tuple[bytes | None, dict | None]:
    chunks: list[bytes] = []
    total = 0
    reader = getattr(response, "read1", response.read)
    while True:
        if _cancelled(cancel_event):
            return None, _result(False, "cancelled", "探测已取消")
        if time.monotonic() >= deadline:
            return None, _result(False, "timeout", "模型服务响应超时")
        try:
            chunk = reader(64 * 1024)
        except (socket.timeout, TimeoutError):
            return None, _result(False, "timeout", "模型服务响应超时")
        if not chunk:
            return b"".join(chunks), None
        total += len(chunk)
        if total > _MAX_RESPONSE_BYTES:
            return None, _result(False, "response_oversize", "模型服务响应超过安全上限")
        chunks.append(chunk)


def _post(url: str, body: bytes, content_type: str, token: str | None, cancel_event: Any, deadline: float,
          accept: str = "application/json"):
    if _cancelled(cancel_event):
        return None, None, _result(False, "cancelled", "探测已取消")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None, None, _result(False, "timeout", "模型服务响应超时")
    headers = {"Content-Type": content_type, "Accept": accept}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request(url, data=body, headers=headers, method="POST")
    try:
        response = _opener_for(url).open(request, timeout=min(remaining, 15))
    except HTTPError as error:
        try:
            status = error.code
            if status in {301, 302, 303, 307, 308}:
                return None, None, _result(False, "redirect_blocked", "模型服务重定向已被安全拒绝")
            mapping = {
                401: ("unauthorized", "模型服务认证失败"),
                403: ("forbidden", "模型服务拒绝访问"),
                404: ("not_found", "模型或能力接口不存在"),
                429: ("rate_limited", "模型服务限流"),
            }
            code, message = mapping.get(status, ("server_error", "模型服务发生错误"))
            return None, None, _result(False, code, message, status=status)
        finally:
            error.close()
    except (socket.timeout, TimeoutError):
        return None, None, _result(False, "timeout", "模型服务连接超时")
    except (URLError, OSError, ValueError):
        return None, None, _result(False, "network_error", "无法连接模型服务")
    try:
        payload, failure = _read_response(response, cancel_event, deadline)
        if failure:
            return None, None, failure
        return payload, response.headers.get_content_type(), None
    finally:
        response.close()


def _json(payload: bytes) -> dict | None:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _readable(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(_readable(item.get("text") if isinstance(item, dict) else item) for item in value)
    return False


def _text_output(value: dict, protocol: str) -> bool:
    if protocol == "openai-chat":
        choices = value.get("choices")
        if not isinstance(choices, list):
            return False
        for choice in choices:
            message = choice.get("message") if isinstance(choice, dict) else None
            if isinstance(message, dict) and (_readable(message.get("content")) or _readable(message.get("reasoning_content"))):
                return True
        return False
    if _readable(value.get("output_text")):
        return True
    output = value.get("output")
    if not isinstance(output, list):
        return False
    for item in output:
        content = item.get("content") if isinstance(item, dict) else None
        if _readable(content):
            return True
    return False


def _positive_reasoning_tokens(value: Any) -> int | None:
    """只接受有限的正数推理 token 计数，避免将任意成功响应当作能力证据。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
        return int(value)
    return None


def _reasoning_budget_exhausted(value: dict, protocol: str) -> int | None:
    """识别官方推理模型因输出预算截断、但已产生 reasoning token 的明确状态。"""
    usage = value.get("usage")
    if not isinstance(usage, dict):
        return None
    if protocol == "openai-responses":
        details = usage.get("output_tokens_details")
        incomplete = value.get("incomplete_details")
        if value.get("status") != "incomplete" or not isinstance(incomplete, dict) or incomplete.get("reason") != "max_output_tokens":
            return None
        return _positive_reasoning_tokens(details.get("reasoning_tokens") if isinstance(details, dict) else None)
    choices = value.get("choices")
    details = usage.get("completion_tokens_details")
    if not isinstance(choices, list) or not any(isinstance(choice, dict) and choice.get("finish_reason") == "length" for choice in choices):
        return None
    return _positive_reasoning_tokens(details.get("reasoning_tokens") if isinstance(details, dict) else None)


def _json_body(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _multipart(model_id: str, audio: bytes) -> tuple[bytes, str]:
    boundary = "----codex-probe-" + uuid.uuid4().hex
    head = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{model_id}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"probe.wav\"\r\n"
        "Content-Type: audio/wav\r\n\r\n"
    ).encode("utf-8")
    return head + audio + f"\r\n--{boundary}--\r\n".encode("ascii"), f"multipart/form-data; boundary={boundary}"


def _audio_bytes(value: bytes) -> bool:
    """TTS 请求及落盘契约都是 MP3；只确认帧结构，不证明可播放或语音质量。"""
    return valid_media(value, ".mp3", "audio")


def probe_model(model: dict, token: str | None = None, cancel_event=None, timeout: float = 15) -> dict:
    """对一个模型发起最小真实能力请求，返回脱敏且稳定的探测结论。

    参数 ``token`` 仅在本次请求的 Authorization 头中使用，函数不会读取环境变量、
    存储或输出它。取消信号可在网络读取之间尽快终止探测。
    """
    if _cancelled(cancel_event):
        return _result(False, "cancelled", "探测已取消")
    settings = _validated_model(model)
    if (settings is None or not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or not math.isfinite(timeout) or timeout <= 0 or (token is not None and not isinstance(token, str))):
        return _result(False, "validation_invalid_model", "模型配置无效")
    model_type, base_url, model_id, protocol, voice = settings
    try:
        endpoint = resolve_endpoint(base_url, model_type, protocol)
    except ValueError:
        return _result(False, "validation_invalid_model", "模型配置无效")
    deadline = time.monotonic() + min(float(timeout), 15.0)
    if model_type == "reasoning":
        path = "/chat/completions" if protocol == "openai-chat" else "/responses"
        request = ({"model": model_id, "messages": [{"role": "user", "content": "回复OK"}], "max_completion_tokens": 256}
                   if protocol == "openai-chat" else {"model": model_id, "input": "回复OK", "max_output_tokens": 256})
        payload, _, failure = _post(endpoint, _json_body(request), "application/json", token, cancel_event, deadline)
        if failure:
            return failure
        parsed = _json(payload)
        if parsed and _text_output(parsed, protocol):
            return _result(True, "verified", "文本推理能力已验证", output_items=1)
        reasoning_tokens = _reasoning_budget_exhausted(parsed, protocol) if parsed else None
        if reasoning_tokens is not None:
            return _result(True, "verified", "推理接口已响应", reasoning_tokens=reasoning_tokens)
        return _result(False, "response_invalid", "模型未返回可读文本输出")
    if model_type == "multimodal":
        path = "/chat/completions" if protocol == "openai-chat" else "/responses"
        image = "data:image/png;base64," + _PNG
        request = ({"model": model_id, "messages": [{"role": "user", "content": [{"type": "text", "text": "识别图片颜色并回复OK"}, {"type": "image_url", "image_url": {"url": image}}]}], "max_completion_tokens": 32}
                   if protocol == "openai-chat" else {"model": model_id, "input": [{"role": "user", "content": [{"type": "input_text", "text": "识别图片颜色并回复OK"}, {"type": "input_image", "image_url": image}]}], "max_output_tokens": 32})
        payload, _, failure = _post(endpoint, _json_body(request), "application/json", token, cancel_event, deadline)
        if failure:
            return failure
        parsed = _json(payload)
        return (_result(True, "verified", "图像理解能力已验证", image_inputs=1) if parsed and _text_output(parsed, protocol)
                else _result(False, "response_invalid", "模型未确认图像输入或未返回可读文本"))
    if model_type == "rerank":
        documents = ["模型验证使用的第一段合成文本", "第二段合成文本用于排序验证"]
        payload, _, failure = _post(endpoint, _json_body({"model": model_id, "query": "排序验证", "documents": documents, "top_n": 2}),
                                    "application/json", token, cancel_event, deadline)
        if failure:
            return failure
        parsed = _json(payload)
        results = parsed.get("results") if isinstance(parsed, dict) else None
        if not isinstance(results, list) or not results:
            return _result(False, "response_invalid", "重排序模型未返回结果")
        indexes: set[int] = set()
        for item in results:
            index = item.get("index") if isinstance(item, dict) else None
            score = item.get("relevance_score") if isinstance(item, dict) else None
            if (isinstance(index, bool) or not isinstance(index, int) or index < 0 or index >= len(documents)
                    or index in indexes or isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score)):
                return _result(False, "response_invalid", "重排序模型未返回有效索引或分数")
            indexes.add(index)
        return _result(True, "verified", "重排序能力已验证", result_items=len(results))
    if model_type == "embedding":
        payload, _, failure = _post(endpoint, _json_body({"model": model_id, "input": "test"}), "application/json", token, cancel_event, deadline)
        if failure:
            return failure
        parsed = _json(payload)
        data = parsed.get("data") if isinstance(parsed, dict) else None
        first = data[0] if isinstance(data, list) and data else None
        vector = first.get("embedding") if isinstance(first, dict) else None
        if isinstance(vector, list) and vector and all(isinstance(number, (int, float)) and not isinstance(number, bool) and math.isfinite(number) for number in vector):
            return _result(True, "verified", "向量能力已验证", dimensions=len(vector))
        return _result(False, "response_invalid", "模型未返回有效向量")
    if model_type == "speech_to_text":
        try:
            audio = _PROBE_AUDIO.read_bytes()
        except OSError:
            return _result(False, "validation_fixture_missing", "缺少探测音频素材")
        if not audio or len(audio) > _MAX_AUDIO_FIXTURE_BYTES:
            return _result(False, "validation_fixture_missing", "探测音频素材无效")
        body, content_type = _multipart(model_id, audio)
        payload, _, failure = _post(endpoint, body, content_type, token, cancel_event, deadline)
        if failure:
            return failure
        parsed = _json(payload)
        return (_result(True, "verified", "语音转写能力已验证", transcript_items=1) if parsed and _readable(parsed.get("text"))
                else _result(False, "response_invalid", "模型未返回有效转写文本"))
    payload, content_type, failure = _post(endpoint, _json_body({"model": model_id, "input": "Test.", "voice": voice, "response_format": "mp3"}), "application/json", token, cancel_event, deadline, accept="audio/*, application/json")
    if failure:
        return failure
    if content_type and content_type.startswith("audio/") and _audio_bytes(payload):
        return _result(True, "verified", "语音合成能力已验证", audio_bytes=len(payload))
    return _result(False, "response_invalid", "模型未返回可识别音频")
