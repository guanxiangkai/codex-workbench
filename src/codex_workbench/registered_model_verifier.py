"""一次性验证已登记的 MiniMax/智谱模型，凭据只由调用方通过 stdin 提供。"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import time
import zlib
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request

from .model_observations import ModelObservations
from .model_probe import (_PNG, _PROBE_AUDIO, _audio_bytes, _json, _json_body, _multipart,
                          _opener_for, _post, _provider_error_diagnostic, _read_response)
from .media_validation import valid_media


# 登记目录只允许这两个官方生产主机；不能通过 CLI 改写，也不会跟随重定向。
_HOSTS = {
    "minimax": {"api.minimax.cn"},
    "bigmodel": {"open.bigmodel.cn"},
}
_REVIEW_PROMPT = (
    "请独立审查这项账户资料规则，并用三条编号边界条件回答："
    "首次官网资料固定本地；Python 定时任务只写用量快照；"
    "名称和头像只能由按账户主体绑定的手动更新改变。"
)
_MEDIA_PROMPT = "一张纯红色方形图标，白色字母 OK，简洁清晰。"


def _ocr_probe_png() -> str:
    """Create a local high-contrast ``OK`` image so OCR has real glyphs to parse."""
    glyphs = {
        "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
        "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    }
    scale, width, height = 16, 224, 144
    pixels = bytearray(b"\xff\xff\xff" * width * height)
    for letter_index, letter in enumerate("OK"):
        for row, bits in enumerate(glyphs[letter]):
            for column, bit in enumerate(bits):
                if bit != "1":
                    continue
                for y in range(24 + row * scale, 24 + (row + 1) * scale):
                    for x in range(24 + letter_index * 96 + column * scale, 24 + letter_index * 96 + (column + 1) * scale):
                        offset = (y * width + x) * 3
                        pixels[offset:offset + 3] = b"\x00\x00\x00"
    raw = b"".join(b"\x00" + pixels[row * width * 3:(row + 1) * width * 3] for row in range(height))
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    return base64.b64encode(png).decode("ascii")


_OCR_PNG = _ocr_probe_png()


def safe_result(model_id: str, success: bool, status: str, error_code: str) -> dict:
    """CLI 的唯一 stdout 合同，永不含服务响应、请求内容或凭据。"""
    return {"model_id": model_id, "success": success, "status": status, "error_code": error_code}


def configuration_fingerprint(model: dict, token: str) -> str:
    """One-way binding for resume: config identity plus the current credential value."""
    identity = {key: str(model.get(key) or "") for key in ("id", "provider_id", "base_url", "model", "credential_id")}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    digest.update(b"\0")
    digest.update(token.encode("utf-8"))
    return digest.hexdigest()


def _response_failure(payload: bytes | None, fallback: str = "response_invalid") -> dict:
    """Turn an in-band provider error into a persistable, message-free result."""
    diagnostic = _provider_error_diagnostic(payload, 200)
    provider_code = diagnostic.get("provider_error_code")
    if provider_code is None:
        return {"success": False, "code": fallback}
    return {"success": False, "code": str(diagnostic["diagnosis"]), "evidence": diagnostic}


def _diagnostics(result: dict) -> dict:
    """Whitelist the only failure details allowed in local verification metadata."""
    evidence = result.get("evidence")
    if not isinstance(evidence, dict):
        return {}
    return {key: evidence[key] for key in ("http_status", "provider_error_code", "diagnosis", "video_task_id")
            if key in evidence and isinstance(evidence[key], (int, str))}


def _official_endpoint(model: dict) -> bool:
    provider = model.get("provider_id")
    try:
        parsed = urlsplit(str(model.get("base_url") or ""))
    except ValueError:
        return False
    return (provider in _HOSTS and parsed.scheme == "https" and parsed.hostname in _HOSTS[provider]
            and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment)


def _native_probe(model: dict, token: str, *, timeout: float) -> dict:
    """探测已由官方 OpenAI 兼容接口定义的能力；响应必须包含真实可用结果。"""
    from .model_probe import probe_model
    capability = model["model_type"]
    config = {"model_type": capability, "base_url": model["base_url"], "model": model["model"],
              "protocol": "openai-chat", "voice": "tongtong"}
    if model.get("provider_id") == "bigmodel" and str(model.get("model", "")).startswith("glm-5.3"):
        config["reasoning_effort"] = "low"
    if capability in {"reasoning", "multimodal"}:
        # 真实文本任务，避免把 /models 或 HTTP 200 当成能力验证。
        config["probe_prompt"] = _REVIEW_PROMPT
    return probe_model(config, token=token, timeout=timeout, response_timeout=timeout)


def _image_output(value: dict | None) -> bool:
    if not isinstance(value, dict):
        return False
    data = value.get("data")
    if not isinstance(data, list) or not data:
        return False
    first = data[0]
    if not isinstance(first, dict):
        return False
    url = first.get("url")
    if _downloadable_https_url(url):
        return True
    encoded = first.get("b64_json")
    return _valid_image_base64(encoded)


def _valid_image_base64(encoded: object) -> bool:
    """只接受可识别的 PNG、JPEG 或 WebP 容器，避免把任意 Base64 当作图片成功。"""
    if not isinstance(encoded, str) or len(encoded) > 2 * 1024 * 1024:
        return False
    try:
        image = base64.b64decode(encoded, validate=True)
    except ValueError:
        return False
    return any(valid_media(image, suffix, "image") for suffix in (".png", ".jpeg", ".webp"))


def _minimax_image_output(value: dict | None) -> bool:
    """MiniMax image response uses ``data.image_urls/image_base64`` rather than OpenAI ``data``."""
    base = value.get("base_resp") if isinstance(value, dict) else None
    if not isinstance(base, dict) or base.get("status_code") != 0:
        return False
    data = value.get("data")
    if not isinstance(data, dict):
        return False
    urls = data.get("image_urls")
    if isinstance(urls, list) and any(_downloadable_https_url(url) for url in urls):
        return True
    encoded = data.get("image_base64")
    if not isinstance(encoded, list):
        return False
    for image in encoded:
        if _valid_image_base64(image):
            return True
    return False


def _native_image(model: dict, token: str, timeout: float) -> dict:
    """Use each provider's documented image payload and require an image result."""
    deadline = time.monotonic() + timeout
    if model.get("provider_id") == "minimax":
        body = {"model": model["model"], "prompt": _MEDIA_PROMPT, "aspect_ratio": "1:1",
                "response_format": "base64", "n": 1}
    else:
        body = {"model": model["model"], "prompt": _MEDIA_PROMPT, "size": "1024x1024"}
    payload, _, failure = _post(model["base_url"], _json_body(body), "application/json", token, None, deadline,
                                response_timeout=timeout, error_diagnostics=True)
    if failure:
        return failure
    parsed = _json(payload)
    valid = _minimax_image_output(parsed) if model.get("provider_id") == "minimax" else _image_output(parsed)
    return {"success": True, "code": "verified"} if valid else _response_failure(payload)


def _native_tts(model: dict, token: str, timeout: float) -> dict:
    """MiniMax synchronous TTS returns encoded audio under ``data.audio``."""
    body = {
        "model": model["model"], "text": "Model capability verification.", "stream": False,
        "language_boost": "English",
        "voice_setting": {"voice_id": "English_Graceful_Lady", "speed": 1.0, "vol": 1.0, "pitch": 0},
        "audio_setting": {"format": "mp3", "sample_rate": 32000, "bitrate": 128000, "channel": 1},
    }
    payload, _, failure = _post(model["base_url"], _json_body(body), "application/json", token, None, time.monotonic() + timeout,
                                response_timeout=timeout, error_diagnostics=True)
    if failure:
        return failure
    parsed = _json(payload)
    audio = parsed.get("data", {}).get("audio") if isinstance(parsed, dict) and isinstance(parsed.get("data"), dict) else None
    base = parsed.get("base_resp") if isinstance(parsed, dict) else None
    if not isinstance(base, dict) or base.get("status_code") != 0 or not isinstance(audio, str):
        return _response_failure(payload)
    decoded_candidates: list[bytes] = []
    try:
        decoded_candidates.append(bytes.fromhex(audio))
    except ValueError:
        pass
    try:
        decoded_candidates.append(base64.b64decode(audio, validate=True))
    except ValueError:
        pass
    return ({"success": True, "code": "verified"}
            if any(_audio_bytes(decoded) for decoded in decoded_candidates) else _response_failure(payload))


def _native_asr(model: dict, token: str, timeout: float) -> dict:
    """MiniMax ASR requires multipart ``model`` and a containerized audio file."""
    try:
        audio = Path(_PROBE_AUDIO).read_bytes()
    except OSError:
        return {"success": False, "code": "validation_fixture_missing"}
    body, content_type = _multipart(model["model"], audio)
    payload, _, failure = _post(model["base_url"], body, content_type, token, None, time.monotonic() + timeout,
                                response_timeout=timeout, error_diagnostics=True)
    if failure:
        return failure
    parsed = _json(payload)
    return ({"success": True, "code": "verified"}
            if isinstance(parsed, dict) and isinstance(parsed.get("text"), str) and parsed["text"].strip()
            else _response_failure(payload))


def _native_ocr(model: dict, token: str, timeout: float) -> dict:
    """GLM-OCR accepts an image/PDF URL or base64; use a generated red PNG, never a remote fixture."""
    body = {"model": model["model"], "file": "data:image/png;base64," + _OCR_PNG, "return_crop_images": False,
            "need_layout_visualization": False}
    payload, _, failure = _post(model["base_url"], _json_body(body), "application/json", token, None, time.monotonic() + timeout,
                                response_timeout=timeout, error_diagnostics=True)
    if failure:
        return failure
    parsed = _json(payload)
    markdown = parsed.get("md_results") if isinstance(parsed, dict) else None
    return ({"success": True, "code": "verified"}
            if isinstance(markdown, str) and markdown.strip() else _response_failure(payload))


def _native_bigmodel_tts(model: dict, token: str, timeout: float) -> dict:
    """GLM-TTS only documents WAV/PCM output; MP3 is not a supported probe format."""
    body = {"model": model["model"], "input": "模型能力验证。", "voice": "tongtong", "response_format": "wav"}
    payload, content_type, failure = _post(model["base_url"], _json_body(body), "application/json", token, None,
                                           time.monotonic() + timeout, accept="audio/wav, application/json",
                                           response_timeout=timeout, error_diagnostics=True)
    if failure:
        return failure
    if content_type and content_type.startswith("audio/") and valid_media(payload, ".wav", "audio"):
        return {"success": True, "code": "verified"}
    return _response_failure(payload)


def _downloadable_https_url(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > 4096 or any(character.isspace() for character in value):
        return False
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password


def _video_result(value: dict | None) -> tuple[str | None, bool]:
    """返回 (task_id, terminal-success)，仅有可下载视频才算终态成功。"""
    if not isinstance(value, dict):
        return None, False
    task = value.get("task")
    if isinstance(task, dict):
        task_id = task.get("id")
        status = str(task.get("status") or "").lower()
        content = task.get("content")
        has_video = isinstance(content, dict) and _downloadable_https_url(content.get("url"))
        return (task_id, status == "succeeded" and has_video) if isinstance(task_id, str) and task_id else (None, False)
    task_id = value.get("id") or value.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        return None, False
    status = str(value.get("task_status") or value.get("status") or "").lower()
    output = value.get("video_result") or value.get("result") or value.get("data")
    has_video = (isinstance(output, dict) and _downloadable_https_url(output.get("url") or output.get("video_url"))) or (
        isinstance(output, list) and any(isinstance(item, dict) and _downloadable_https_url(item.get("url") or item.get("video_url")) for item in output))
    return task_id, status in {"success", "succeeded", "completed"} and has_video


def _video_terminal_failure(value: dict | None) -> bool:
    """确认任务已经结束却没有有效视频时终止续查，防止下次运行重复恢复同一失败任务。"""
    if not isinstance(value, dict):
        return False
    task = value.get("task")
    if isinstance(task, dict):
        status = str(task.get("status") or "").lower()
        content = task.get("content")
        has_video = isinstance(content, dict) and _downloadable_https_url(content.get("url"))
    else:
        status = str(value.get("task_status") or value.get("status") or "").lower()
        output = value.get("video_result") or value.get("result") or value.get("data")
        has_video = (isinstance(output, dict) and _downloadable_https_url(output.get("url") or output.get("video_url"))) or (
            isinstance(output, list) and any(isinstance(item, dict) and _downloadable_https_url(item.get("url") or item.get("video_url")) for item in output))
    failed = {"failed", "failure", "error", "cancelled", "canceled", "rejected", "expired"}
    completed = {"success", "succeeded", "completed"}
    return status in failed or (status in completed and not has_video)


def _native_video(model: dict, token: str, timeout: float) -> dict:
    """视频生成受理不等于成功；两家官方异步协议都必须轮询得到 URL。"""
    deadline = time.monotonic() + timeout
    if model.get("provider_id") == "minimax":
        request = {"model": model["model"], "content": [{"type": "text", "text": _MEDIA_PROMPT}],
                   "resolution": "768P", "duration": 4, "ratio": "1:1"}
    else:
        request = {"model": model["model"], "prompt": _MEDIA_PROMPT}
    existing_task_id = model.get("_verification_task_id")
    if isinstance(existing_task_id, str) and existing_task_id and len(existing_task_id) <= 256 and existing_task_id.replace("-", "").replace("_", "").isalnum():
        task_id = existing_task_id
    else:
        payload, _, failure = _post(model["base_url"], _json_body(request), "application/json", token, None, deadline,
                                    response_timeout=timeout, error_diagnostics=True)
        if failure:
            return failure
        response = _json(payload)
        task_id, done = _video_result(response)
        if done:
            return {"success": True, "code": "verified"}
        if _video_terminal_failure(response):
            return {"success": False, "code": "task_failed"}
        if not task_id:
            return _response_failure(payload)
    task_path = quote(task_id, safe="")
    if model.get("provider_id") == "minimax":
        poll_url = model["base_url"].rsplit("/v2/video_generation", 1)[0] + "/v2/query/video_generation/" + task_path
    else:
        poll_url = model["base_url"].rsplit("/videos/generations", 1)[0] + "/async-result/" + task_path
    # 只提交一次；在总 deadline 内逐步放宽轮询间隔，任务受理绝不算 verified。
    interval = 1.0
    polls = 0
    # This bound only protects deterministic test clocks and a broken sleep implementation;
    # normal execution stops on the deadline after increasingly spaced polls.
    max_polls = max(1, int(timeout) + 1)
    while polls < max_polls:
        if time.monotonic() >= deadline:
            break
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
        if time.monotonic() >= deadline:
            break
        payload, failure = _get(poll_url, token, deadline, error_diagnostics=True)
        polls += 1
        if failure:
            evidence = failure.get("evidence") if isinstance(failure.get("evidence"), dict) else {}
            failure["evidence"] = {**evidence, "video_task_id": task_id}
            return failure
        response = _json(payload)
        _, done = _video_result(response)
        if done:
            return {"success": True, "code": "verified"}
        if _video_terminal_failure(response):
            return {"success": False, "code": "task_failed"}
        interval = min(interval * 1.5, 5.0)
    return {"success": False, "code": "task_pending", "evidence": {"video_task_id": task_id}}


def _get(url: str, token: str, deadline: float, *, error_diagnostics: bool = False) -> tuple[bytes | None, dict | None]:
    """受限视频轮询：GET、禁止重定向、复用响应大小和超时上限。"""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None, {"success": False, "code": "timeout"}
    try:
        response = _opener_for(url).open(Request(url, headers={"Authorization": "Bearer " + token, "Accept": "application/json"}, method="GET"),
                                         timeout=min(remaining, 15))
    except HTTPError as error:
        try:
            code = "redirect_blocked" if error.code in {301, 302, 303, 307, 308} else "server_error"
            result = {"success": False, "code": code}
            if error_diagnostics:
                try:
                    result["evidence"] = _provider_error_diagnostic(error.read(8192), error.code)
                except OSError:
                    result["evidence"] = _provider_error_diagnostic(None, error.code)
            return None, result
        finally:
            error.close()
    except (URLError, OSError, ValueError):
        return None, {"success": False, "code": "network_error"}
    try:
        return _read_response(response, None, deadline)
    finally:
        response.close()


def _verify_model(model: dict, token: str, *, timeout: float = 30) -> tuple[dict, dict]:
    """验证一个登记模型；每个成功结果都必须有对应能力的有效输出。"""
    model_id = model.get("id") if isinstance(model.get("id"), str) else ""
    if not model_id or not _official_endpoint(model):
        return safe_result(model_id, False, "skipped", "unregistered_https_endpoint"), {}
    if not isinstance(token, str) or not token:
        return safe_result(model_id, False, "failed", "credential_format"), {}
    capability = model.get("model_type")
    path = urlsplit(model["base_url"]).path.rstrip("/")
    try:
        if (model.get("provider_id"), path) == ("bigmodel", "/api/paas/v4/layout_parsing"):
            result = _native_ocr(model, token, timeout)
        elif (model.get("provider_id"), path) == ("minimax", "/v1/speech_to_text"):
            result = _native_asr(model, token, timeout)
        elif (model.get("provider_id"), path) == ("minimax", "/v1/t2a_v2"):
            result = _native_tts(model, token, timeout)
        elif (model.get("provider_id"), path) == ("bigmodel", "/api/paas/v4/audio/speech"):
            result = _native_bigmodel_tts(model, token, timeout)
        elif capability == "image_generation":
            result = _native_image(model, token, timeout)
        elif capability == "video_generation":
            result = _native_video(model, token, timeout)
        elif capability in {"reasoning", "multimodal", "speech_to_text", "text_to_speech", "embedding", "rerank"}:
            if capability == "speech_to_text" and not Path(_PROBE_AUDIO).is_file():
                return safe_result(model_id, False, "skipped", "validation_fixture_missing"), {}
            result = _native_probe(model, token, timeout=timeout)
        else:
            return safe_result(model_id, False, "skipped", "unsupported_model_type"), {}
    except (OSError, ValueError, TypeError):
        result = {"success": False, "code": "probe_error"}
    success = result.get("success") is True
    return (safe_result(model_id, success, "verified" if success else "failed", str(result.get("code") or "probe_error")),
            _diagnostics(result))


def verify_model(model: dict, token: str, *, timeout: float = 30) -> dict:
    """Public verifier contract, intentionally restricted to the four CLI fields."""
    return _verify_model(model, token, timeout=timeout)[0]


def _review_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        text = "\n".join(parts).strip()
        return text or None
    return None


def verify_model_with_review(model: dict, token: str, *, timeout: float = 30,
                             include_diagnostics: bool = False):
    """对聊天模型一次请求同时验证能力并保留可采纳的文字审查，绝不写入 stdout。"""
    model_id = model.get("id") if isinstance(model.get("id"), str) else ""
    capability = model.get("model_type")
    path = urlsplit(str(model.get("base_url") or "")).path.rstrip("/")
    if (not _official_endpoint(model) or not isinstance(token, str) or not token or capability not in {"reasoning", "multimodal"}
            or not path.endswith("/chat/completions")):
        safe, diagnostic = _verify_model(model, token, timeout=timeout)
        return (safe, None, diagnostic) if include_diagnostics else (safe, None)
    request: dict = {"model": model["model"], "max_completion_tokens": 2048}
    if model.get("provider_id") == "bigmodel" and str(model.get("model", "")).startswith("glm-5.3"):
        # GLM-5.3 always thinks; its documented lowest supported effort is low.
        request["reasoning_effort"] = "low"
    if capability == "multimodal":
        request["messages"] = [{"role": "user", "content": [
            {"type": "text", "text": _REVIEW_PROMPT + " 同时确认随附纯红图片可见。"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + _PNG}},
        ]}]
    else:
        request["messages"] = [{"role": "user", "content": _REVIEW_PROMPT}]
    try:
        payload, _, failure = _post(model["base_url"], _json_body(request), "application/json", token, None,
                                    time.monotonic() + timeout, response_timeout=timeout, error_diagnostics=True)
    except (OSError, ValueError, TypeError):
        safe = safe_result(model_id, False, "failed", "probe_error")
        return (safe, None, {}) if include_diagnostics else (safe, None)
    if failure:
        safe = safe_result(model_id, False, "failed", str(failure.get("code") or "probe_error"))
        diagnostic = _diagnostics(failure)
        return (safe, None, diagnostic) if include_diagnostics else (safe, None)
    parsed = _json(payload)
    choices = parsed.get("choices") if isinstance(parsed, dict) else None
    first = choices[0] if isinstance(choices, list) and choices else None
    message = first.get("message") if isinstance(first, dict) else None
    review = _review_text(message.get("content")) if isinstance(message, dict) else None
    if not review:
        safe = safe_result(model_id, False, "failed", "response_invalid")
        return (safe, None, {}) if include_diagnostics else (safe, None)
    # Cap one provider response before it crosses the verifier's private-file boundary.
    safe = safe_result(model_id, True, "verified", "verified")
    return (safe, review[:12000], {}) if include_diagnostics else (safe, review[:12000])


def write_metadata(data_dir: Path, results: list[dict]) -> None:
    """供人工验收的脱敏结果，不保存模型输出、响应正文或凭据。"""
    target = Path(data_dir) / "registered-model-verifications.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    previous: dict[str, dict] = {}
    try:
        saved = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(saved, dict) and isinstance(saved.get("results"), list):
            previous = {item["model_id"]: item for item in saved["results"]
                        if isinstance(item, dict) and isinstance(item.get("model_id"), str)}
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    previous.update({item["model_id"]: item for item in results})
    target.write_text(json.dumps({"review_task": "account_profile_freeze", "results": list(previous.values())}, ensure_ascii=False), encoding="utf-8")
    target.chmod(0o600)


def write_reviews(data_dir: Path, reviews: dict[str, str], selected_model_ids: set[str] | None = None) -> None:
    """更新本次选中模型的审查正文；失败模型清除旧值，未选模型保留。"""
    target = Path(data_dir) / "registered-model-reviews.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    previous: dict[str, dict] = {}
    try:
        saved = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(saved, dict) and isinstance(saved.get("reviews"), list):
            previous = {item["model_id"]: item for item in saved["reviews"]
                        if isinstance(item, dict) and isinstance(item.get("model_id"), str)}
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    if selected_model_ids is not None:
        previous = {model_id: item for model_id, item in previous.items() if model_id not in selected_model_ids}
    observed_at = int(time.time())
    for model_id, review in reviews.items():
        if isinstance(model_id, str) and isinstance(review, str) and review.strip():
            previous[model_id] = {"model_id": model_id, "observed_at": observed_at, "review": review[:12000]}
    target.write_text(json.dumps({"review_task": "account_profile_freeze", "reviews": list(previous.values())}, ensure_ascii=False), encoding="utf-8")
    target.chmod(0o600)


def record_results(data_dir: Path, selected: list[dict], results: list[dict]) -> None:
    store = ModelObservations(data_dir)
    by_id = {item["model_id"]: item for item in results}
    for model in selected:
        result = by_id[model["id"]]
        if result["status"] != "skipped":
            store.record(model, success=result["success"], source="probe")
