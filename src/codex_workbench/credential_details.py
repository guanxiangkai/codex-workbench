"""单条凭证详情的端到端加密读取；父进程从不接触明文。"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .credential_schema import CredentialSchemaError, parse_payload

if TYPE_CHECKING:
    from .credentials import CredentialCatalog

_ENTRY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_REQUEST_RE = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
MAX_DETAIL_INPUT_BYTES = 1024 * 1024
MAX_DETAIL_REPLY_BYTES = 2 * 1024 * 1024
_PUBLIC_KEY_MAX_BYTES = 2048


class CredentialDetailsError(ValueError):
    """详情读取的固定错误；异常中不保留命令输出、输入或解析异常。"""

    _MESSAGES = {"details_invalid": "凭证详情请求无效", "details_failed": "凭证详情读取失败",
                 "details_timeout": "凭证详情读取超时", "details_stale": "凭证修订已变化，请重新查看",
                 "details_reply_invalid": "凭证详情加密结果无效"}

    def __init__(self, code: str) -> None:
        self.code = code if code in self._MESSAGES else "details_failed"
        super().__init__(self._MESSAGES[self.code])


def _public_key(value: Any) -> tuple[str, rsa.RSAPublicKey, bytes, str]:
    """校验浏览器临时 SPKI 公钥，返回规范 DER 及其摘要。"""
    if not isinstance(value, str) or not value or len(value) > _PUBLIC_KEY_MAX_BYTES * 2 + 16:
        raise CredentialDetailsError("details_invalid")
    try:
        encoded = value.encode("ascii")
        der = base64.b64decode(encoded, validate=True)
        key = serialization.load_der_public_key(der)
    except (UnicodeEncodeError, ValueError, TypeError):
        raise CredentialDetailsError("details_invalid") from None
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048 or key.key_size > 4096:
        raise CredentialDetailsError("details_invalid")
    canonical = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    if canonical != der:
        raise CredentialDetailsError("details_invalid")
    return value, key, der, hashlib.sha256(der).hexdigest()


def _aad(entry_id: str, revision: int, request_id: str, public_key_sha256: str) -> bytes:
    """构造跨进程稳定 AAD，把密文绑定到条目、版本、请求和浏览器公钥。"""
    return json.dumps({"entry_id": entry_id, "public_key_sha256": public_key_sha256,
                       "request_id": request_id, "revision": revision},
                      ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")


def _entry(catalog: CredentialCatalog, entry_id: str) -> dict[str, Any] | None:
    snapshot = catalog._snapshot(force=True)
    return next((item for item in catalog._entry_views(snapshot) if item["id"] == entry_id), None)


def _validate_reply(value: Any, entry_id: str, revision: int, generation: int,
                    request_id: str, key_digest: str) -> dict[str, Any]:
    required = {"entry_id", "revision", "generation", "request_id", "public_key_sha256", "wrapped_key", "iv", "ciphertext"}
    if not isinstance(value, dict) or set(value) != required:
        raise CredentialDetailsError("details_reply_invalid")
    if (value["entry_id"], value["revision"], value["generation"], value["request_id"], value["public_key_sha256"]) != (entry_id, revision, generation, request_id, key_digest):
        raise CredentialDetailsError("details_reply_invalid")
    for field, maximum in (("wrapped_key", 1024), ("iv", 64), ("ciphertext", MAX_DETAIL_REPLY_BYTES)):
        if not isinstance(value[field], str):
            raise CredentialDetailsError("details_reply_invalid")
        try:
            decoded = base64.b64decode(value[field].encode("ascii"), validate=True)
        except (UnicodeEncodeError, ValueError):
            raise CredentialDetailsError("details_reply_invalid") from None
        if not decoded or len(decoded) > maximum or (field == "wrapped_key" and len(decoded) not in {256, 384, 512}) or (field == "iv" and len(decoded) != 12) or (field == "ciphertext" and len(decoded) < 16):
            raise CredentialDetailsError("details_reply_invalid")
    return dict(value)


def read_details(catalog: CredentialCatalog, entry_id: str, public_key: str, request_id: str,
                 *, timeout: float = 30.0) -> dict[str, Any]:
    """返回仅请求浏览器私钥可解开的详情信封，父进程绝不接触保险库明文。"""
    if not isinstance(entry_id, str) or not _ENTRY_RE.fullmatch(entry_id) or not isinstance(request_id, str) or not _REQUEST_RE.fullmatch(request_id):
        raise CredentialDetailsError("details_invalid")
    if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise CredentialDetailsError("details_invalid")
    public_text, _, _, key_digest = _public_key(public_key)
    before = _entry(catalog, entry_id)
    if before is None:
        raise CredentialDetailsError("details_invalid")
    revision, generation = before.get("revision"), before.get("structure_generation")
    if type(revision) is not int or revision < 1 or type(generation) is not int or generation < 0:
        raise CredentialDetailsError("details_invalid")
    package_root = Path(__file__).resolve().parents[1]
    command = [str(catalog.command), "exec-stdin", entry_id, sys.executable, "-B", "-m",
               "codex_workbench.credential_details", entry_id, str(revision), str(generation), request_id, public_text]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                cwd=package_root.parent, env={**os.environ, "PYTHONPATH": str(package_root)},
                                timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise CredentialDetailsError("details_timeout") from None
    except (OSError, ValueError):
        raise CredentialDetailsError("details_failed") from None
    if result.returncode != 0 or result.stderr or len(result.stdout) > MAX_DETAIL_REPLY_BYTES:
        raise CredentialDetailsError("details_failed")
    try:
        reply = _validate_reply(json.loads(result.stdout.decode("utf-8")), entry_id, revision, generation, request_id, key_digest)
    except (UnicodeDecodeError, json.JSONDecodeError, CredentialDetailsError):
        raise CredentialDetailsError("details_reply_invalid") from None
    after = _entry(catalog, entry_id)
    if after is None or after.get("revision") != revision or after.get("structure_generation") != generation:
        raise CredentialDetailsError("details_stale")
    return reply


def _format_payload(raw: bytes) -> dict[str, Any]:
    """保留详情原值和原字段；仅标注包装格式，绝不按名称改写。"""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError from None
    try:
        # 与结构探针相同地拒绝重复键和 NaN；否则将原始文本完整交给浏览器。
        value = parse_payload(text)
    except CredentialSchemaError:
        return {"format": "text", "text": text}
    if isinstance(value, dict) and value.get("schema") == "codex-workbench.credential" and value.get("version") == 1 and isinstance(value.get("credential"), dict):
        format_name = "workbench_credential_v1"
    elif isinstance(value, dict) and isinstance(value.get("credential"), dict):
        format_name = "workbench_credential"
    elif isinstance(value, dict) and "api_key" in value and isinstance(value.get("target"), dict):
        format_name = "workbench_model_key"
    else:
        format_name = "json"
    return {"format": format_name, "fields": value}


def _consumer(arguments: list[str]) -> int:
    """固定消费者：stdin 明文只留在此进程，stdout 永远是加密 JSON。"""
    try:
        if len(arguments) != 5:
            raise ValueError
        entry_id, revision_text, generation_text, request_id, public_text = arguments
        if not _ENTRY_RE.fullmatch(entry_id) or not _REQUEST_RE.fullmatch(request_id) or not revision_text.isascii() or not generation_text.isascii():
            raise ValueError
        revision, generation = int(revision_text), int(generation_text)
        if revision < 1 or generation < 0:
            raise ValueError
        _, public, der, digest = _public_key(public_text)
        raw = sys.stdin.buffer.read(MAX_DETAIL_INPUT_BYTES + 1)
        if len(raw) > MAX_DETAIL_INPUT_BYTES:
            raise ValueError
        plaintext = json.dumps(_format_payload(raw), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(plaintext) > MAX_DETAIL_INPUT_BYTES + 1024:
            raise ValueError
        aes_key, iv = os.urandom(32), os.urandom(12)
        ciphertext = AESGCM(aes_key).encrypt(iv, plaintext, _aad(entry_id, revision, request_id, digest))
        wrapped = public.encrypt(aes_key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
        reply = {"entry_id": entry_id, "revision": revision, "generation": generation, "request_id": request_id,
                 "public_key_sha256": hashlib.sha256(der).hexdigest(), "wrapped_key": base64.b64encode(wrapped).decode("ascii"),
                 "iv": base64.b64encode(iv).decode("ascii"), "ciphertext": base64.b64encode(ciphertext).decode("ascii")}
        encoded = json.dumps(reply, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        if len(encoded) > MAX_DETAIL_REPLY_BYTES:
            raise ValueError
        sys.stdout.buffer.write(encoded + b"\n")
        return 0
    except Exception:
        sys.stdout.write('{"error":"detail_failed"}\n')
        return 1


def main(argv: list[str] | None = None) -> int:
    """模块入口仅供 ``exec-stdin`` 的固定消费者调用。"""
    return _consumer(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())
