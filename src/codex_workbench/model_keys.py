"""模型 API Key 的浏览器加密信封和受控保险库写入边界。"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


_CONTEXT_FIELDS = frozenset({"name", "model_type", "base_url", "model_id", "version"})
_KEY_RE = re.compile(r"[\x21-\x7e]{1,8192}\Z")
_ENTRY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_TTL_SECONDS = 300
_MAX_SESSIONS = 32


class ModelKeyError(ValueError):
    """向调用方提供固定、无秘密的模型密钥处理失败原因。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> None:
    raise ModelKeyError(code, message)


def _context(value: dict[str, Any]) -> dict[str, Any]:
    """验证并复制会话绑定上下文，禁止额外字段绕过目标匹配。"""
    if not isinstance(value, dict) or set(value) != _CONTEXT_FIELDS:
        _fail("context_invalid", "模型密钥绑定上下文无效")
    name, model_type, base_url = value.get("name"), value.get("model_type"), value.get("base_url")
    model_id, version = value.get("model_id"), value.get("version")
    if not all(isinstance(item, str) and item.strip() for item in (name, model_type, base_url)):
        _fail("context_invalid", "模型密钥绑定上下文无效")
    if model_id is not None and (not isinstance(model_id, str) or not model_id.strip()):
        _fail("context_invalid", "模型密钥绑定上下文无效")
    if version is not None and (not isinstance(version, int) or isinstance(version, bool) or version < 0):
        _fail("context_invalid", "模型密钥绑定上下文无效")
    return {field: value[field] for field in sorted(_CONTEXT_FIELDS)}


def _b64decode(value: Any, code: str, maximum: int) -> bytes:
    if not isinstance(value, str) or not value or len(value) > maximum * 2 + 16:
        _fail(code, "模型密钥加密信封无效")
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError):
        _fail(code, "模型密钥加密信封无效")
    if not decoded or len(decoded) > maximum:
        _fail(code, "模型密钥加密信封无效")
    return decoded


def _key_bytes(value: str) -> bytes:
    if not isinstance(value, str):
        _fail("key_invalid", "模型密钥格式无效")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        _fail("key_invalid", "模型密钥格式无效")
    if len(encoded) > 8192 or not _KEY_RE.fullmatch(value):
        _fail("key_invalid", "模型密钥格式无效")
    return encoded


@dataclass
class _Session:
    """仅存在于服务进程内存中的一次性私钥和目标绑定。"""

    private_key: rsa.RSAPrivateKey
    context: dict[str, Any]
    expires_at: int


class KeyEnvelopeSessions:
    """为浏览器提交 API Key 创建短时 RSA-OAEP + AES-GCM 加密信封。"""

    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.RLock()

    def _expire(self, now: int) -> None:
        for session_id, session in list(self._sessions.items()):
            if session.expires_at <= now:
                del self._sessions[session_id]

    def prepare(self, context: dict[str, Any]) -> dict[str, Any]:
        """创建五分钟的一次性公钥会话，私钥绝不写入日志、文件或响应。"""
        bound = _context(context)
        now = int(time.time())
        with self._lock:
            self._expire(now)
            if len(self._sessions) >= _MAX_SESSIONS:
                _fail("session_limit", "加密会话数量已达上限，请稍后重试")
            session_id = secrets.token_urlsafe(24)
            private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            expires_at = now + _TTL_SECONDS
            self._sessions[session_id] = _Session(private_key, bound, expires_at)
        public_key = private_key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        return {"id": session_id, "public_key": base64.b64encode(public_key).decode("ascii"), "expires_at": expires_at}

    def consume(self, envelope: dict[str, Any], context: dict[str, Any], _allow_utf8: bool = False) -> str:
        """严格匹配目标后一次性解封 API Key；篡改、过期和重放均以安全错误拒绝。"""
        bound = _context(context)
        if not isinstance(envelope, dict) or set(envelope) != {"session_id", "wrapped_key", "iv", "ciphertext"}:
            _fail("envelope_invalid", "模型密钥加密信封无效")
        session_id = envelope.get("session_id")
        if not isinstance(session_id, str) or not session_id or len(session_id) > 128:
            _fail("envelope_invalid", "模型密钥加密信封无效")
        now = int(time.time())
        with self._lock:
            self._expire(now)
            # 找到会话后即销毁，使错误信封也无法被反复尝试。
            session = self._sessions.pop(session_id, None)
        if session is None:
            _fail("session_invalid", "加密会话已过期或已使用")
        if session.context != bound:
            _fail("context_mismatch", "模型目标已变化，请重新输入密钥")
        wrapped_key = _b64decode(envelope.get("wrapped_key"), "envelope_invalid", 256)
        iv = _b64decode(envelope.get("iv"), "envelope_invalid", 12)
        ciphertext = _b64decode(envelope.get("ciphertext"), "envelope_invalid", 65552 if _allow_utf8 else 8208)
        if len(wrapped_key) != 256 or len(iv) != 12 or len(ciphertext) < 16:
            _fail("envelope_invalid", "模型密钥加密信封无效")
        try:
            aes_key = session.private_key.decrypt(
                wrapped_key,
                padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
            )
            plaintext = AESGCM(aes_key).decrypt(iv, ciphertext, session_id.encode("utf-8"))
        except (ValueError, InvalidTag):
            _fail("envelope_invalid", "模型密钥加密信封无效")
        if len(aes_key) != 32:
            _fail("envelope_invalid", "模型密钥加密信封无效")
        try:
            key = plaintext.decode("utf-8" if _allow_utf8 else "ascii")
        except UnicodeDecodeError:
            _fail("key_invalid", "模型密钥格式无效")
        if _allow_utf8:
            if not key or len(plaintext) > 65536:
                _fail("payload_invalid", "凭证加密内容无效")
        else:
            _key_bytes(key)
        return key

    def consume_text(self, envelope: dict[str, Any], context: dict[str, Any]) -> str:
        """解封有界 UTF-8 文本供通用凭证 payload 使用；API Key 仍使用 ASCII consume。"""
        return self.consume(envelope, context, _allow_utf8=True)


class ModelKeyVault:
    """把已解封密钥经 stdin 写入既有保险库，并以 verify 完成受控确认。"""

    def __init__(self, command: Path | None = None, timeout: float = 20) -> None:
        self.command = command or Path.home() / ".codex/scripts/key-vault/key-vault.sh"
        self.timeout = timeout

    def store(self, key: str, context: dict[str, Any]) -> str:
        """存储带目标绑定的 JSON 负载，返回唯一且非秘密的 ``vault:`` 引用。"""
        _key_bytes(key)
        bound = _context(context)
        if not self.command.is_file() or self.command.is_symlink():
            _fail("vault_unavailable", "保险库入口不可用")
        entry_id = "model-" + secrets.token_urlsafe(24).replace("_", "-")
        if not _ENTRY_RE.fullmatch(entry_id):
            _fail("vault_unavailable", "保险库入口不可用")
        payload = json.dumps({"api_key": key, "target": bound}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            stored = subprocess.run(
                [str(self.command), "put-stdin", entry_id], input=payload,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False, timeout=self.timeout,
            )
            if stored.returncode:
                _fail("vault_store_failed", "无法安全保存模型密钥")
            checked = subprocess.run(
                [str(self.command), "verify", entry_id],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False, timeout=self.timeout,
            )
            if checked.returncode:
                _fail("vault_verify_failed", "Key 已加密写入但校验失败，模型尚未保存。排查条目：" + entry_id)
        except subprocess.TimeoutExpired:
            _fail("vault_timeout", "保险库操作超时，Key 可能已写入，模型尚未保存。排查条目：" + entry_id)
        except OSError:
            _fail("vault_unavailable", "保险库入口不可用")
        return "vault:" + entry_id
