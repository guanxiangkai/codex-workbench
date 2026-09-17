"""隔离的专业模型消费者；凭据只从 stdin 进入本次 HTTP 请求。"""

from __future__ import annotations

import base64
import json
import math
import re
import sys
import uuid
from pathlib import Path
from typing import Any


def _fail(code: str) -> dict[str, Any]:
    return {"success": False, "code": code}


def _decode_credential(payload: bytes, expected_base_url: str | None = None) -> str | None:
    """与模型探测相同地校验保险库 Key 及其绑定的接口地址。"""
    if expected_base_url is not None and not isinstance(expected_base_url, str) or len(payload) > 16384:
        raise ValueError("credential_format")
    value = payload.decode("utf-8").strip()
    if not value:
        return None
    if value.startswith("{"):
        record = json.loads(value)
        if not isinstance(record, dict):
            raise ValueError("credential_format")
        target = record.get("target")
        if isinstance(target, dict) and isinstance(target.get("base_url"), str) and expected_base_url is not None:
            if target["base_url"].strip().rstrip("/") != expected_base_url.strip().rstrip("/"):
                raise ValueError("credential_target")
        candidates = {record[key] for key in ("api_key", "apiKey", "API_KEY", "APP_KEY", "token", "access_token", "value")
                      if isinstance(record.get(key), str) and record[key]}
        if len(candidates) != 1:
            raise ValueError("credential_format")
        value = candidates.pop().strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    if not re.fullmatch(r"[\x21-\x7e]{1,8192}", value) or value.startswith("-----BEGIN"):
        raise ValueError("credential_format")
    return value


def _bounded_text(value: Any, limit: int = 32768) -> str | None:
    return value if isinstance(value, str) and value.strip() and len(value.encode("utf-8")) <= limit else None


def _messages(value: Any) -> list[dict[str, str]] | None:
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        return None
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"role", "content"} or item.get("role") not in {"system", "user", "assistant"}:
            return None
        content = _bounded_text(item.get("content"))
        if content is None:
            return None
        result.append({"role": item["role"], "content": content})
    return result


def _multipart(model_id: str, audio: bytes, suffix: str, mime: str) -> tuple[bytes, str]:
    boundary = "----capability-" + uuid.uuid4().hex
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{model_id}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"input{suffix}\"\r\n"
            f"Content-Type: {mime}\r\n\r\n").encode()
    return head + audio + f"\r\n--{boundary}--\r\n".encode(), "multipart/form-data; boundary=" + boundary


def _output_text(value: dict[str, Any], protocol: str) -> str | None:
    if protocol == "openai-chat":
        choices = value.get("choices")
        first = choices[0] if isinstance(choices, list) and choices else None
        message = first.get("message") if isinstance(first, dict) else None
        text = message.get("content") if isinstance(message, dict) else None
        return _bounded_text(text)
    text = value.get("output_text")
    if _bounded_text(text):
        return text
    output = value.get("output")
    if isinstance(output, list):
        for item in output:
            content = item.get("content") if isinstance(item, dict) else None
            if isinstance(content, list):
                for part in content:
                    text = part.get("text") if isinstance(part, dict) else None
                    if _bounded_text(text):
                        return text
    return None


def _usage(value: dict[str, Any]) -> dict[str, int]:
    usage = value.get("usage")
    if not isinstance(usage, dict):
        return {}
    result = {}
    for source, target in (("input_tokens", "input_tokens"), ("prompt_tokens", "input_tokens"),
                           ("output_tokens", "output_tokens"), ("completion_tokens", "output_tokens")):
        candidate = usage.get(source)
        if target not in result and isinstance(candidate, int) and not isinstance(candidate, bool) and 0 <= candidate <= 10**9:
            result[target] = candidate
    return result


def execute(model: dict[str, Any], request: dict[str, Any], token: str | None) -> dict[str, Any]:
    """执行固定六类协议；错误只透出稳定代码，绝不回显响应体或 headers。"""
    from codex_workbench.model_endpoints import resolve_endpoint
    from codex_workbench.model_probe import _json, _json_body, _post
    model_type = model.get("model_type")
    protocol = model.get("protocol")
    model_id = model.get("model")
    tool = request.get("tool")
    if not isinstance(model_id, str) or model_type not in {"reasoning", "multimodal", "speech_to_text", "text_to_speech", "embedding", "rerank"}:
        return _fail("manifest_invalid")
    expected = {"reasoning_chat": "reasoning", "multimodal_image": "multimodal", "speech_to_text": "speech_to_text",
                "text_to_speech": "text_to_speech", "embedding": "embedding", "rerank": "rerank"}
    if expected.get(tool) != model_type:
        return _fail("model_not_allowed")
    try:
        endpoint = resolve_endpoint(model["base_url"], model_type, protocol)
    except (KeyError, TypeError, ValueError):
        return _fail("manifest_invalid")
    timeout = 120
    if tool == "reasoning_chat":
        messages = _messages(request.get("messages"))
        maximum = request.get("max_tokens", 1024)
        if messages is None or not isinstance(maximum, int) or isinstance(maximum, bool) or not 1 <= maximum <= 8192:
            return _fail("request_invalid")
        body = ({"model": model_id, "messages": messages, "max_completion_tokens": maximum} if protocol == "openai-chat"
                else {"model": model_id, "input": messages[-1]["content"], "max_output_tokens": maximum})
        payload, _, failure = _post(endpoint, _json_body(body), "application/json", token, None, __import__("time").monotonic() + timeout)
        if failure:
            return _fail(failure["code"])
        parsed = _json(payload)
        text = _output_text(parsed, protocol) if parsed else None
        return {"success": True, "text": text, **_usage(parsed)} if text else _fail("response_invalid")
    if tool == "multimodal_image":
        prompt = _bounded_text(request.get("prompt"))
        image = _private_bytes(request.get("image_bytes_path"))
        if prompt is None or not isinstance(image, bytes) or not 1 <= len(image) <= 20 * 1024 * 1024:
            return _fail("request_invalid")
        encoded = base64.b64encode(image).decode("ascii")
        image_mime = request.get("image_mime")
        if image_mime not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
            return _fail("request_invalid")
        image_url = "data:" + image_mime + ";base64," + encoded
        body = ({"model": model_id, "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": image_url}}]}], "max_completion_tokens": 2048}
                if protocol == "openai-chat" else {"model": model_id, "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}, {"type": "input_image", "image_url": image_url}]}], "max_output_tokens": 2048})
        payload, _, failure = _post(endpoint, _json_body(body), "application/json", token, None, __import__("time").monotonic() + timeout)
        if failure:
            return _fail(failure["code"])
        parsed = _json(payload)
        text = _output_text(parsed, protocol) if parsed else None
        return {"success": True, "text": text, **_usage(parsed)} if text else _fail("response_invalid")
    if tool == "speech_to_text":
        audio = _private_bytes(request.get("audio_bytes_path"))
        if not isinstance(audio, bytes) or not 1 <= len(audio) <= 20 * 1024 * 1024:
            return _fail("request_invalid")
        audio_mime, audio_suffix = request.get("audio_mime"), request.get("audio_suffix")
        if audio_mime not in {"audio/wav", "audio/mpeg", "audio/mp4", "audio/flac", "audio/ogg"} or audio_suffix not in {".wav", ".mp3", ".m4a", ".flac", ".ogg"}:
            return _fail("request_invalid")
        body, content_type = _multipart(model_id, audio, audio_suffix, audio_mime)
        payload, _, failure = _post(endpoint, body, content_type, token, None, __import__("time").monotonic() + timeout)
        if failure:
            return _fail(failure["code"])
        parsed = _json(payload)
        text = _bounded_text(parsed.get("text")) if parsed else None
        return {"success": True, "text": text, **_usage(parsed)} if text else _fail("response_invalid")
    if tool == "text_to_speech":
        text = _bounded_text(request.get("input"), 16000)
        voice = request.get("voice", model.get("voice"))
        if text is None or not isinstance(voice, str) or not 1 <= len(voice) <= 100:
            return _fail("request_invalid")
        payload, content_type, failure = _post(endpoint, _json_body({"model": model_id, "input": text, "voice": voice, "response_format": "mp3"}), "application/json", token, None, __import__("time").monotonic() + timeout, accept="audio/*, application/json")
        if failure:
            return _fail(failure["code"])
        # MCP 文本响应保持在运行时 1 MiB 输出上限内；较大音频由提供方缩短输入后重试。
        if not payload or not content_type or not content_type.startswith("audio/") or len(payload) > 512 * 1024:
            return _fail("response_invalid")
        return {"success": True, "audio": base64.b64encode(payload).decode("ascii")}
    if tool == "embedding":
        value = request.get("input")
        values = [value] if isinstance(value, str) else value
        if not isinstance(values, list) or not 1 <= len(values) <= 64 or any(_bounded_text(item, 8192) is None for item in values):
            return _fail("request_invalid")
        payload, _, failure = _post(endpoint, _json_body({"model": model_id, "input": values}), "application/json", token, None, __import__("time").monotonic() + timeout)
        if failure:
            return _fail(failure["code"])
        parsed = _json(payload); data = parsed.get("data") if parsed else None
        vectors = [item.get("embedding") for item in data] if isinstance(data, list) else None
        if not isinstance(vectors, list) or not vectors or any(not isinstance(vector, list) or not vector or any(not isinstance(number, (int, float)) or isinstance(number, bool) or not math.isfinite(number) for number in vector) for vector in vectors):
            return _fail("response_invalid")
        return {"success": True, "vectors": vectors, **_usage(parsed)}
    query, documents = _bounded_text(request.get("query")), request.get("documents")
    if query is None or not isinstance(documents, list) or not 1 <= len(documents) <= 64 or any(_bounded_text(item, 8192) is None for item in documents):
        return _fail("request_invalid")
    payload, _, failure = _post(endpoint, _json_body({"model": model_id, "query": query, "documents": documents, "top_n": len(documents)}), "application/json", token, None, __import__("time").monotonic() + timeout)
    if failure:
        return _fail(failure["code"])
    parsed = _json(payload); results = parsed.get("results") if parsed else None
    if not isinstance(results, list) or not results:
        return _fail("response_invalid")
    safe = []
    for item in results:
        index, score = (item.get("index"), item.get("relevance_score")) if isinstance(item, dict) else (None, None)
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(documents) or not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(score):
            return _fail("response_invalid")
        safe.append({"index": index, "relevance_score": score})
    return {"success": True, "results": safe, **_usage(parsed)}


def _private_bytes(value: Any) -> bytes | None:
    """读取运行时私有临时文件，不接受调用方指定的任意路径。"""
    if not isinstance(value, str):
        return None
    path = Path(value)
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
            return None
        return path.read_bytes()
    except OSError:
        return None


def main() -> None:
    """从受控文件和 stdin 读取一次输入，stdout 只写有界 JSON 响应。"""
    try:
        config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        request = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        token = _decode_credential(sys.stdin.buffer.read(16385), expected_base_url=config.get("base_url"))
        if config.get("credential_ref") and token is None:
            raise ValueError("credential_format")
        result = execute(config, request, token)
    except Exception:
        result = _fail("capability_error")
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write(encoded[:1_100_000] + "\n")


if __name__ == "__main__":
    main()
