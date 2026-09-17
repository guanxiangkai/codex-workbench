"""凭证载荷与非秘密结构索引的单一契约；不依据名称、引用或服务可用性分类。"""

from __future__ import annotations

import json
import re
from typing import Any

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization

SCHEMA_NAME = "codex-workbench.credential"
SCHEMA_VERSION = 1
MAX_PROBE_BYTES = 1048576
MAX_PAYLOAD_BYTES = 65536
# 扩展诊断与默认八键索引分离；受控消费者的单次输出仍不超过 16 KiB。
MAX_SHAPE_BYTES = 16384
MAX_SHAPE_GROUPS = 16
# v1 归档只扩展可选字段，旧条目的字段和版本字面量保持可读。所有新建字段
# port 与 database_index 保持整数；其余保持原始字符串，不按名称改写或拆分值。
PAYLOAD_FIELDS = frozenset({"description", "account", "password", "ip", "remote_path", "port", "endpoint", "key",
                            "host", "token", "private_key", "public_key", "passphrase", "certificate",
                            "database_type", "database", "namespace", "group", "access_key", "secret_key", "bucket", "region", "app_id",
                            "database_index", "connection_mode", "endpoints", "sentinel_master", "auth_database", "replica_set", "connection_uri",
                            "brokers", "security_protocol", "sasl_mechanism", "virtual_host", "name_servers", "kubeconfig", "transport_security", "extra_config"})
INTEGER_FIELDS = {"port": (1, 65535), "database_index": (0, 2147483647)}
FIELD_NAMES = PAYLOAD_FIELDS | frozenset({"recovery_code", "secret"})
KINDS = frozenset({"api_key", "password", "private_key", "certificate", "credential", "unknown", "token", "secret"})
VALUE_TYPES = frozenset({"string", "integer", "number", "boolean", "null", "object", "array"})
FORMATS = frozenset({"workbench_credential_v1", "workbench_credential", "workbench_model_key", "json_object", "json_value", "text", "invalid", "pem_private_key", "pem_certificate"})
ERROR_CODES = frozenset({"invalid_utf8", "invalid_json", "payload_too_large", "structure_limit", "invalid_schema", "invalid_field_type", "duplicate_field", "unrecognized_format"})
# 只识别可审查的固定字段名；未知键名绝不进入返回值或错误。
_ALIASES = {name: name for name in FIELD_NAMES} | {"username": "account", "user": "account", "login": "account", "passwd": "password", "pwd": "password", "api_key": "key", "apiKey": "key", "API_KEY": "key", "base_url": "endpoint", "url": "endpoint", "api_url": "endpoint", "access_token": "token", "accessToken": "token", "hostname": "host", "access_key_id": "access_key", "secret_access_key": "secret_key", "AWS_ACCESS_KEY_ID": "access_key", "AWS_SECRET_ACCESS_KEY": "secret_key"}


class CredentialSchemaError(ValueError):
    """固定安全错误，不携带原始输入、任意键名或解析器异常。"""

    def __init__(self) -> None:
        super().__init__("凭证结构索引无效")


def normalize_payload(value: Any) -> dict[str, Any]:
    """校验新建凭证允许字段及紧凑 UTF-8 JSON 整包 65536 字节上限，保持端口整数。"""
    if not isinstance(value, dict) or set(value) - PAYLOAD_FIELDS:
        raise CredentialSchemaError()
    for name, item in value.items():
        if name in INTEGER_FIELDS:
            low, high = INTEGER_FIELDS[name]
            if type(item) is not int or not low <= item <= high:
                raise CredentialSchemaError()
        elif not isinstance(item, str):
            raise CredentialSchemaError()
        else:
            try:
                if len(item.encode("utf-8")) > MAX_PAYLOAD_BYTES:
                    raise CredentialSchemaError()
            except UnicodeEncodeError:
                raise CredentialSchemaError() from None
    if len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise CredentialSchemaError()
    return {name: item for name, item in value.items() if item != ""}


def _valid_field_types(format_name: str, fields: dict[str, str]) -> bool:
    """历史格式仅放宽已知账户空值、端口标量；探测与入库共用且不转换原值。"""
    if format_name in {"pem_private_key", "pem_certificate"}:
        return fields == {"private_key" if format_name == "pem_private_key" else "certificate": "string"}
    for name, value_type in fields.items():
        allowed = {"integer"} if name in INTEGER_FIELDS else {"string"}
        if format_name in {"json_object", "workbench_credential"}:
            if name == "account":
                allowed = {"string", "null"}
            elif name == "port":
                allowed = {"integer", "number", "string"}
        if value_type not in allowed:
            return False
    return True


def _probe_pem(raw: bytes) -> dict[str, Any] | None:
    """只接受完整单块规范 PEM；解析器对象和异常不出内存，不判断证书可信度。"""
    block = raw.strip(b" \t\r\n")
    match = re.fullmatch(rb"-----BEGIN (PRIVATE KEY|RSA PRIVATE KEY|EC PRIVATE KEY|DSA PRIVATE KEY|OPENSSH PRIVATE KEY|CERTIFICATE)-----\r?\n(?:[A-Za-z0-9+/=]+\r?\n)+-----END \1-----", block)
    if match is None:
        return None
    try:
        if match.group(1) == b"CERTIFICATE":
            x509.load_pem_x509_certificate(block)
            return _index("pem_certificate", {"certificate": "string"})
        if match.group(1) == b"OPENSSH PRIVATE KEY":
            serialization.load_ssh_private_key(block, password=None)
        else:
            serialization.load_pem_private_key(block, password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        return None
    return _index("pem_private_key", {"private_key": "string"})


def _value_type(value: Any) -> str:
    if value is None:
        return "null"
    return {str: "string", int: "integer", float: "number", bool: "boolean", dict: "object", list: "array"}[type(value)]


def _kind(fields: dict[str, str]) -> str:
    if {"access_key", "secret_key"} <= set(fields):
        return "credential"
    candidates = [kind for field, kind in (("key", "api_key"), ("token", "token"), ("secret", "secret"), ("secret_key", "secret"), ("password", "password"), ("private_key", "private_key"), ("certificate", "certificate")) if field in fields]
    return candidates[0] if len(candidates) == 1 else "credential"


def _index(format_name: str, fields: dict[str, str] | None = None, *, unknown: bool = False,
           error: str | None = None, recognized: bool = True) -> dict[str, Any]:
    fields = dict(sorted((fields or {}).items()))
    status = "error" if error and error != "unrecognized_format" else ("indexed" if recognized else "unrecognized")
    return {"schema_version": SCHEMA_VERSION, "kind": _kind(fields) if status == "indexed" else "unknown",
            "has_fields": list(fields), "field_types": fields, "has_unknown_fields": unknown,
            "structure_status": status, "structure_format": format_name, "structure_error": error}


def build_payload_index(payload: dict[str, Any]) -> dict[str, Any]:
    """从已校验的新建载荷生成无值索引；调用方须在清理载荷前调用。"""
    normalized = normalize_payload(payload)
    return _index("workbench_credential_v1", {name: _value_type(value) for name, value in normalized.items()})


def build_model_key_index() -> dict[str, Any]:
    """仅供已完成 ModelKeyVault.store 的创建路径记录其固定载荷契约。"""
    return _index("workbench_model_key", {"key": "string", "endpoint": "string"})


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for name, value in pairs:
        if name in result:
            raise CredentialSchemaError()
        result[name] = value
    return result


def parse_payload(text: str) -> Any:
    """严格解析 JSON，拒绝重复键和非标准数值，错误不回显输入。"""
    def invalid_constant(_: str) -> None:
        raise CredentialSchemaError()
    try:
        return json.loads(text, object_pairs_hook=_pairs, parse_constant=invalid_constant)
    except (json.JSONDecodeError, RecursionError, ValueError):
        raise CredentialSchemaError() from None


def probe_structure(raw: bytes) -> dict[str, Any]:
    """受控消费者：输入只在内存解析，仅返回白名单字段存在性、值类型和固定状态。"""
    if not isinstance(raw, bytes) or len(raw) > MAX_PROBE_BYTES:
        return _index("invalid", error="payload_too_large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return _index("invalid", error="invalid_utf8")
    try:
        value = parse_payload(text)
    except CredentialSchemaError:
        pem = _probe_pem(raw)
        if pem is not None:
            return pem
        if text.lstrip().startswith(("{", "[", '"')):
            return _index("invalid", error="invalid_json")
        return _index("text", recognized=False, error="unrecognized_format")
    pending, nodes = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > 32 or nodes > 4096:
            return _index("invalid", error="structure_limit")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    if not isinstance(value, dict):
        return _index("json_value", recognized=False, error="unrecognized_format")
    format_name, fields, unknown, empty_allowed = "json_object", value, False, False
    if "schema" in value:
        if value.get("schema") != SCHEMA_NAME or type(value.get("version")) is not int or value["version"] != SCHEMA_VERSION or not isinstance(value.get("credential"), dict):
            return _index("invalid", error="invalid_schema")
        format_name, fields, empty_allowed = "workbench_credential_v1", value["credential"], True
        unknown = bool(set(value) - {"schema", "version", "credential", "target"})
        try:
            fields = normalize_payload(fields)
        except CredentialSchemaError:
            return _index(format_name, error="invalid_schema")
    elif "credential" in value:
        if not isinstance(value["credential"], dict):
            return _index("invalid", error="invalid_schema")
        format_name, fields, empty_allowed = "workbench_credential", value["credential"], True
        unknown = bool(set(value) - {"credential", "target"})
    elif "api_key" in value and isinstance(value.get("target"), dict):
        format_name = "workbench_model_key"
        fields = {"api_key": value["api_key"]}
        if "base_url" in value["target"]:
            fields["base_url"] = value["target"]["base_url"]
        unknown = bool(set(value) - {"api_key", "target"})
    types = {}
    for name, item in fields.items():
        canonical = _ALIASES.get(name)
        if canonical is None:
            unknown = True
            continue
        if canonical in types:
            return _index(format_name, unknown=unknown, error="duplicate_field")
        types[canonical] = _value_type(item)
    if not _valid_field_types(format_name, types):
        return _index(format_name, types, unknown=unknown, error="invalid_field_type")
    if not types and (not empty_allowed or fields):
        return _index(format_name, unknown=unknown, recognized=False, error="unrecognized_format")
    return _index(format_name, types, unknown=unknown)


def validate_structure(value: Any) -> dict[str, Any]:
    """在落库前严格重建索引；拒绝额外键、任意字段名和伪造的类型推断。"""
    expected = {"schema_version", "kind", "has_fields", "field_types", "has_unknown_fields", "structure_status", "structure_format", "structure_error"}
    if not isinstance(value, dict) or set(value) != expected or type(value.get("schema_version")) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise CredentialSchemaError()
    fields = value["field_types"]
    if not isinstance(fields, dict) or set(fields) - FIELD_NAMES or any(not isinstance(item, str) or item not in VALUE_TYPES for item in fields.values()):
        raise CredentialSchemaError()
    if not isinstance(value["structure_format"], str) or value["structure_format"] not in FORMATS or type(value["has_unknown_fields"]) is not bool:
        raise CredentialSchemaError()
    error = value["structure_error"]
    if error is not None and (not isinstance(error, str) or error not in ERROR_CODES):
        raise CredentialSchemaError()
    rebuilt = _index(value["structure_format"], fields, unknown=value["has_unknown_fields"], error=error,
                     recognized=value["structure_status"] == "indexed")
    if rebuilt != value:
        raise CredentialSchemaError()
    if value["structure_status"] == "indexed" and not _valid_field_types(value["structure_format"], fields):
        raise CredentialSchemaError()
    if value["structure_status"] == "indexed":
        if value["structure_format"] in {"text", "json_value", "invalid"}:
            raise CredentialSchemaError()
        if value["structure_format"] == "workbench_credential_v1" and set(fields) - PAYLOAD_FIELDS:
            raise CredentialSchemaError()
        if value["structure_format"] == "workbench_model_key" and ("key" not in fields or set(fields) - {"key", "endpoint"}):
            raise CredentialSchemaError()
    return rebuilt


def describe_structure(value: Any) -> dict[str, str]:
    """从严格索引派生 UI 状态，不修改索引、版本或凭证分类。

    格式可知不等于字段已映射；mapped/partial 只描述固定字段映射，不能证明
    账户完整或认证有效。empty 表示允许的空载荷。调用方须先处理目录视图的
    unindexed/stale 状态，不能用旧修订的索引生成当前状态。
    """
    index = validate_structure(value)
    if index["structure_status"] == "error":
        mapping = "error"
    elif index["structure_status"] != "indexed":
        mapping = "unmapped"
    elif not index["has_fields"]:
        mapping = "unmapped" if index["has_unknown_fields"] else "empty"
    else:
        mapping = "partial" if index["has_unknown_fields"] else "mapped"
    return {"format_status": "unknown" if index["structure_format"] == "invalid" else "known",
            "mapping_status": mapping}


def _shape_group(value: dict[str, Any], object_id: int, parent_id: int | None,
                 depth: int, container: str) -> dict[str, Any]:
    """逐对象记录固定字段候选，别名冲突仅在本对象内判定；不输出输入路径。"""
    fields: dict[str, list[str]] = {}
    unknown = 0
    for name, item in value.items():
        canonical = _ALIASES.get(name)
        if canonical is not None:
            fields.setdefault(canonical, []).append(_value_type(item))
        elif name not in {"schema", "version", "credential", "credentials", "target"}:
            unknown += 1
    error = None
    if any(len(types) > 1 for types in fields.values()):
        error = "duplicate_field"
    elif not _valid_field_types("json_object", {name: types[0] for name, types in fields.items()}):
        error = "invalid_field_type"
    return {"object_id": object_id, "parent_id": parent_id, "depth": depth,
            "container": container, "known_fields": {name: sorted(types) for name, types in sorted(fields.items())},
            "unknown_key_count": unknown, "mapping_status": "error" if error else ("candidate" if fields else "unmapped"),
            "structure_error": error}


def probe_shapes(raw: bytes) -> dict[str, Any]:
    """返回独立的无值结构诊断，供主代理在明确授权后调用固定消费者。

    structure 保持原八键协议，只有它可交给 validate_structure/索引登记。
    groups 的编号只在本次摘要中有效；每组对应一个 JSON 对象，parent_id 是
    最近的父对象。容器名仅允许 root/credential/credentials/target/array_item/
    other；任意键名、数组下标和载荷值都不输出。所有字段组都是候选，不能
    合并为账户、推断父子字段归属或自动提升顶层 kind/has_fields。

    最多保留 16 个对象且整包最多 16 KiB，截断时仍报告全量有界结构计数。
    非 JSON 或不通过原探测错误门禁时 shape_status=unavailable，不诊断错误载荷。
    """
    index = probe_structure(raw)
    result: dict[str, Any] = {"structure": index, **describe_structure(index),
                              "shape_status": "unavailable", "object_count": 0, "array_count": 0,
                              "unknown_key_count": 0, "max_depth": 0, "groups": []}
    if index["structure_status"] == "error" or index["structure_format"] not in {
        "json_object", "json_value", "workbench_credential", "workbench_credential_v1", "workbench_model_key"
    }:
        return result
    # 原探测已完成严格 JSON、1 MiB、深度和节点门禁；二次解析不放宽这些约束。
    value = parse_payload(raw.decode("utf-8"))
    pending = [(value, 0, None, "root")]
    result["shape_status"] = "complete"
    while pending:
        item, depth, parent_id, container = pending.pop()
        result["max_depth"] = max(result["max_depth"], depth)
        if isinstance(item, dict):
            result["object_count"] += 1
            object_id = result["object_count"]
            group = _shape_group(item, object_id, parent_id, depth, container)
            result["unknown_key_count"] += group["unknown_key_count"]
            if len(result["groups"]) < MAX_SHAPE_GROUPS:
                result["groups"].append(group)
            else:
                result["shape_status"] = "truncated"
            for name, child in reversed(list(item.items())):
                route = name if name in {"credential", "credentials", "target"} else "other"
                pending.append((child, depth + 1, object_id, route))
        elif isinstance(item, list):
            result["array_count"] += 1
            pending.extend((child, depth + 1, parent_id, "array_item") for child in reversed(item))
    # 字段别名数和将来白名单扩展也不能突破消费者输出上限，换行计入字节预算。
    while len(json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode("utf-8")) + 1 > MAX_SHAPE_BYTES:
        result["groups"].pop()
        result["shape_status"] = "truncated"
    return result
