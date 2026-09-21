"""其他平台账户的受管目录投影；不读取、解密或探测账户凭据。"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any


_ROOT_FIELDS = frozenset({"version", "providers"})
_PROVIDER_FIELDS = frozenset({"id", "name", "accounts"})
_FRAGMENT_FIELDS = frozenset({"version", "provider", "account"})
_PROVIDER_REFERENCE_FIELDS = frozenset({"id", "name"})
_ACCOUNT_FIELDS = frozenset({"id", "label", "vault_id", "usage", "usage_windows", "api_auth", "field_notes", "is_used", "status_source", "observed_at", "expires_at", "resets_at", "last_used_at", "updated_at", "usage_credential_id"})
_USAGE_FIELDS = frozenset({"used", "limit", "remaining", "unit", "observed_at", "source"})
_API_AUTH_FIELDS = frozenset({"status", "source", "observed_at"})
_USAGE_WINDOW_FIELDS = frozenset({"id", "label", "usage", "resets_at"})
_FIELD_NOTES = frozenset({"usage", "resets_at", "expires_at", "last_used_at"})
_ID = re.compile(r"[a-z][a-z0-9_-]{0,127}")
_VAULT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def other_accounts(path: Path) -> dict[str, list[dict[str, Any]]]:
    """读取账户目录及每账户分片，缺失目录返回空投影，格式错误明确失败。

    `vault_id` 只是配置中心条目引用，函数不访问保险库或任何网络来源。
    """
    path = Path(path)
    directory = path.parent
    sources = []
    if path.exists():
        sources.extend(_legacy_providers(path))
    if directory.exists():
        for fragment in sorted(directory.glob("*.json")):
            if fragment == path:
                continue
            sources.append(_fragment_provider(fragment))
    if not sources:
        return {"providers": [], "accounts": []}

    providers: list[dict[str, Any]] = []
    accounts: list[dict[str, Any]] = []
    account_ids: set[str] = set()
    grouped = {}
    for provider in sources:
        if not isinstance(provider, dict) or set(provider) != _PROVIDER_FIELDS:
            raise ValueError("其他账户平台字段无效")
        provider_id = _identifier(provider.get("id"), "平台标识")
        provider_name = _text(provider.get("name"), "平台名称")
        provider_accounts = provider.get("accounts")
        if not isinstance(provider_accounts, list) or len(provider_accounts) > 500:
            raise ValueError("其他账户平台标识或账户列表无效")
        if provider_id in grouped and grouped[provider_id]["name"] != provider_name:
            raise ValueError("其他账户平台标识或账户列表无效")
        grouped.setdefault(provider_id, {"name": provider_name, "accounts": []})["accounts"].extend(provider_accounts)
    if len(grouped) > 200:
        raise ValueError("其他账户目录格式无效")
    for provider_id, provider in grouped.items():
        provider_name = provider["name"]
        if len(provider["accounts"]) > 500:
            raise ValueError("其他账户平台标识或账户列表无效")
        public_accounts = []
        for account in provider["accounts"]:
            item = _account(account, provider_id, provider_name)
            if item["id"] in account_ids:
                raise ValueError("其他账户标识重复")
            account_ids.add(item["id"])
            public_accounts.append(item)
            accounts.append(item)
        # 没有账户的平台不进入投影，避免把可用平台误作已登记账户。
        if public_accounts:
            providers.append({"id": provider_id, "name": provider_name, "accounts": public_accounts})
    return {"providers": providers, "accounts": accounts}


def _load(path: Path, message: str) -> Any:
    if path.stat().st_size > 1024 * 1024:
        raise ValueError(message + "超过大小限制")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(message + "不是有效 JSON") from error


def _legacy_providers(path: Path) -> list[dict[str, Any]]:
    raw = _load(path, "其他账户目录")
    if not isinstance(raw, dict) or set(raw) != _ROOT_FIELDS or type(raw.get("version")) is not int or raw["version"] != 1 or not isinstance(raw.get("providers"), list) or len(raw["providers"]) > 200:
        raise ValueError("其他账户目录格式无效")
    return raw["providers"]


def _fragment_provider(path: Path) -> dict[str, Any]:
    raw = _load(path, "其他账户分片")
    if not isinstance(raw, dict) or set(raw) != _FRAGMENT_FIELDS or type(raw.get("version")) is not int or raw["version"] != 1:
        raise ValueError("其他账户分片格式无效")
    provider = raw.get("provider")
    if not isinstance(provider, dict) or set(provider) != _PROVIDER_REFERENCE_FIELDS:
        raise ValueError("其他账户分片平台字段无效")
    return {**provider, "accounts": [raw.get("account")]}


def _account(value: Any, provider_id: str, provider_name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - _ACCOUNT_FIELDS:
        raise ValueError("其他账户字段无效")
    account_id = _identifier(value.get("id"), "账户标识")
    label = _text(value.get("label"), "账户名称")
    vault_id = value.get("vault_id")
    if vault_id is not None:
        vault_id = _vault_id(vault_id)
    usage_credential_id = value.get("usage_credential_id")
    if usage_credential_id is not None:
        usage_credential_id = _vault_id(usage_credential_id)
    usage = _usage(value.get("usage")) if "usage" in value else None
    usage_windows = _usage_windows(value.get("usage_windows")) if "usage_windows" in value else []
    api_auth = _api_auth(value.get("api_auth")) if "api_auth" in value else None
    field_notes = _field_notes(value.get("field_notes")) if "field_notes" in value else {}
    status_fields = {key: value.get(key) for key in ("is_used", "status_source", "observed_at")}
    if all(item is None for item in status_fields.values()):
        is_used = status_source = observed_at = None
    elif any(item is None for item in status_fields.values()) or type(status_fields["is_used"]) is not bool:
        raise ValueError("其他账户使用状态必须同时登记来源和时间")
    else:
        is_used = status_fields["is_used"]
        status_source = _text(status_fields["status_source"], "使用状态来源")
        observed_at = _timestamp(status_fields["observed_at"], "使用状态时间")
    result = {"id": account_id, "label": label, "provider_id": provider_id, "provider_name": provider_name,
              "source": "other_account_catalog",
              "vault_id": vault_id, "usage": usage, "is_used": is_used, "status_source": status_source,
              "observed_at": observed_at, "usage_windows": usage_windows, "api_auth": api_auth, "field_notes": field_notes,
              "usage_credential_id": usage_credential_id}
    for key in ("expires_at", "resets_at", "last_used_at", "updated_at"):
        result[key] = _timestamp(value[key], key) if key in value and value[key] is not None else None
    return result


def _api_auth(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != _API_AUTH_FIELDS or not isinstance(value.get("status"), str) or value["status"] not in {"accepted", "rejected"}:
        raise ValueError("API 验证信息无效")
    return {"status": value["status"], "source": _text(value["source"], "API 验证来源"), "observed_at": _timestamp(value["observed_at"], "API 验证时间")}


def _usage_windows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 16:
        raise ValueError("用量窗口无效")
    result, ids = [], set()
    for window in value:
        if not isinstance(window, dict) or set(window) != _USAGE_WINDOW_FIELDS:
            raise ValueError("用量窗口字段无效")
        identifier = _identifier(window.get("id"), "用量窗口标识")
        if identifier in ids:
            raise ValueError("用量窗口标识重复")
        ids.add(identifier)
        usage = _usage(window.get("usage"))
        if usage is None:
            raise ValueError("用量窗口必须提供用量")
        result.append({"id": identifier, "label": _text(window.get("label"), "用量窗口名称"), "usage": usage, "resets_at": _timestamp(window["resets_at"], "用量窗口重置时间") if window.get("resets_at") is not None else None})
    return result


def _field_notes(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) - _FIELD_NOTES:
        raise ValueError("字段说明无效")
    return {key: _text(note, "字段说明") for key, note in value.items()}


def _usage(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _USAGE_FIELDS:
        raise ValueError("其他账户用量字段无效")
    amounts = {key: _amount(value[key], key) if value[key] is not None else None for key in ("used", "limit", "remaining")}
    if all(item is None for item in amounts.values()):
        raise ValueError("其他账户用量至少需要一个数值")
    return {**amounts, "unit": _text(value["unit"], "用量单位"), "observed_at": _timestamp(value["observed_at"], "用量时间"),
            "source": _text(value["source"], "用量来源")}


def _amount(value: Any, label: str) -> int | float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(f"其他账户{label}无效")
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{label}无效")
    return value


def _vault_id(value: Any) -> str:
    """沿用凭证详情入口的公开条目引用格式，不把保险库标识当作账户标识。"""
    if not isinstance(value, str) or not _VAULT_ID.fullmatch(value):
        raise ValueError("保险库引用无效")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError(f"{label}无效")
    return value.strip()


def _timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError(f"{label}无效")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{label}必须是 ISO 带时区时间") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label}必须是 ISO 带时区时间")
    return value
