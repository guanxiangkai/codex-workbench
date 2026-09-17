"""通用凭证敏感字段的加密信封与受控保险库存储。"""
from __future__ import annotations
import json, secrets, subprocess
from pathlib import Path
from .model_keys import KeyEnvelopeSessions, ModelKeyError, _context, _fail
from .credential_schema import MAX_PAYLOAD_BYTES, SCHEMA_NAME, SCHEMA_VERSION, CredentialSchemaError, normalize_payload, parse_payload

# AES-GCM 追加 16 字节认证标签；Base64 按三字节分组向上取整。
MAX_PAYLOAD_CIPHERTEXT_CHARS = 4 * ((MAX_PAYLOAD_BYTES + 16 + 2) // 3)

class CredentialPayloadSessions(KeyEnvelopeSessions):
    """复用一次性 RSA/AES 会话，整包敏感字段只在内存中出现。"""
    def consume_payload(self,envelope,context):
        """解封并依统一字段契约校验，不回显载荷或 JSON 解析错误。"""
        try:
            if not isinstance(envelope, dict) or not isinstance(envelope.get("ciphertext"), str) or len(envelope["ciphertext"]) > MAX_PAYLOAD_CIPHERTEXT_CHARS:
                raise CredentialSchemaError()
            text = super().consume_text(envelope,context)
            if len(text.encode("utf-8")) > MAX_PAYLOAD_BYTES:
                raise CredentialSchemaError()
            return normalize_payload(parse_payload(text))
        except (CredentialSchemaError, ModelKeyError):
            _fail("payload_invalid","凭证加密内容无效")

class CredentialPayloadVault:
    """仅经 stdin 写入整包敏感数据，随后以 verify 确认。"""
    def __init__(self,command=None,timeout=20): self.command=Path(command) if command else Path.home()/'.codex/scripts/key-vault/key-vault.sh'; self.timeout=timeout
    def store(self,payload,context):
        """写入版本化整包载荷；标题与目标信息仍封装在密文内。"""
        if not self.command.is_file() or self.command.is_symlink(): _fail("vault_unavailable","保险库入口不可用")
        try:
            normalized = normalize_payload(payload)
        except CredentialSchemaError:
            _fail("payload_invalid","凭证加密内容无效")
        body=json.dumps({"schema":SCHEMA_NAME,"version":SCHEMA_VERSION,"credential":normalized,"target":_context(context)},ensure_ascii=True,separators=(",",":")).encode()
        entry='credential-'+secrets.token_urlsafe(24).replace('_','-')
        try:
            if subprocess.run([str(self.command),'put-stdin',entry],input=body,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=self.timeout).returncode: _fail("vault_store_failed","无法安全保存凭证")
            if subprocess.run([str(self.command),'verify',entry],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=self.timeout).returncode: _fail("vault_verify_failed","凭证已加密写入但校验失败，排查条目："+entry)
        except subprocess.TimeoutExpired: _fail("vault_timeout","保险库操作超时，凭证可能已写入，排查条目："+entry)
        except OSError: _fail("vault_unavailable","保险库入口不可用")
        return 'vault:'+entry
