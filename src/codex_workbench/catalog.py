"""工作台的 MCP 契约。"""
from __future__ import annotations

from typing import Any

VERSION = "0.10.5"
UI_URIS = {"open_workbench": "ui://codex-workbench/v7/workbench.html"}
PAGES = {"workbench": ("codex-workbench", "open_workbench", "工作台")}
MODULES = [
    {"id": "accounts", "name": "Codex 账户", "group": "账户与配置"},
    {"id": "other_accounts", "name": "其他账户", "group": "账户与配置"},
    {"id": "config", "name": "配置中心", "group": "账户与配置"},
    {"id": "knowledge", "name": "知识中心", "group": "能力与知识"},
    {"id": "models", "name": "模型目录", "group": "能力与知识"},
]
DEFAULT_VIEW = "accounts"
UI_VIEWS = frozenset(module["id"] for module in MODULES)


def text(maximum=300, nullable=False):
    return {"type": ["string", "null"] if nullable else "string", "maxLength": maximum}


ID = text(255)


def definition(name, title, description, properties=None, required=None, *, read_only=True, idempotent=True):
    result = {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False},
        "annotations": {"readOnlyHint": read_only, "destructiveHint": False, "idempotentHint": idempotent, "openWorldHint": name in ("workbench_state", "workbench_sync")},
    }
    if name in UI_URIS:
        result["_meta"] = {"ui": {"resourceUri": UI_URIS[name]}, "openai/ui": {"entrypoints": [{"type": "global", "header": {"enabled": False}}]}}
    if name in ("credential_details", "knowledge_detail", "account_create", "account_login", "account_status", "account_default", "account_profile_update", "account_profile_capture"):
        result["_meta"] = {"ui": {"visibility": ["app"]}}
    if name in ("account_create", "account_login", "account_status", "account_default", "account_profile_update", "account_profile_capture", "account_profile_import"):
        result["annotations"]["readOnlyHint"] = False
    if name in ("account_create", "account_login"):
        result["annotations"]["idempotentHint"] = False
    return result


FILTERS = {"query": text(300), "kind": text(100), "provider": text(200), "tag": text(200), "folder": text(200)}
TOOLS = [
    definition("account_create", "添加 Codex 账户", "准备临时官方登录目录；成功核验后才登记账户，不自动设为默认。", {"name": text(160)}, ["name"]),
    definition("account_login", "登录 Codex 账户", "打开官方 OpenAI 登录页面，登录完成后自动核验账户状态。", {"id": ID}, ["id"]),
    definition("account_status", "核验 Codex 账户", "读取官方登录状态和额度，并登记已确认的账户主体。", {"id": ID}, ["id"]),
    definition("account_default", "设置默认账户", "设置已登记且身份核验通过的默认账户；所有会话下一次请求使用该账户。", {"id": ID, "expected_default_id": text(255, True)}, ["id", "expected_default_id"]),
    definition("account_profile_update", "更新账户资料", "仅在用户手动更新时读取 Codex 官方资料；不代表已采集网页头像。", {"id": ID}, ["id"]),
    definition("account_profile_capture", "授权官网资料采集", "仅供工作台首次登录或用户点击更新资料后申请十分钟内有效的单次保存授权。", {"view": {"type": "string", "enum": ["accounts", "other_accounts"]}, "id": ID}, ["view", "id"], idempotent=False),
    definition("account_profile_import", "保存官网账户资料", "需要工作台发起的有效单次 capture_id；仅保存已在官方可见页面核对为同一账户的真实昵称和本地头像；不读取或保存 Cookie、Token。", {"view": {"type": "string", "enum": ["accounts", "other_accounts"]}, "id": ID, "capture_id": text(128), "display_name": text(200), "account_identifier": text(200), "avatar_data_uri": text(1048576), "missing_fields": {"type": "array", "items": {"type": "string", "enum": ["display_name", "avatar_data_uri", "account_identifier"]}, "uniqueItems": True, "maxItems": 3}, "observed_at": text(100)}, ["view", "id", "capture_id"]),
    definition("open_workbench", "工作台", "打开账户、配置、知识和模型工作台。"),
    definition("workbench_state", "读取工作台", "按视图读取后台更新的公开 JSON 快照，不在页面请求中访问来源。", {"view": {"type": "string", "enum": sorted(UI_VIEWS)}, **FILTERS}),
    definition("workbench_sync", "同步工作台变化", "按修订返回后台快照差异；刷新仅排入后台采集，失败保留最后成功数据，不返回密钥。", {"view": {"type": "string", "enum": sorted(UI_VIEWS)}, **FILTERS, "revision": text(64), "refresh": {"type": "boolean"}}, ["view"]),
    definition("module_list", "读取工作台模块", "读取用户可见的工作台模块目录。"),
    definition("knowledge_list", "读取知识目录", "读取已审核知识的范围和目录，不创建、审核或修改知识。", {"scope": text(200), "query": text(300)}),
    definition("knowledge_detail", "阅读知识", "读取指定范围内已审核且有效的知识，不创建或修改知识。", {"scope": text(200), "key": text(200)}, ["scope", "key"]),
    definition("model_list", "读取模型目录", "读取已经登记的模型与验证快照，不发起验证。"),
    definition("model_detail", "读取模型详情", "读取指定模型的公开配置与保险库条目引用。", {"id": ID}, ["id"]),
    definition("credential_list", "读取配置目录", "仅展示非秘密目录与匹配当前修订的结构索引。"),
    definition("credential_details", "读取单条配置", "返回由请求浏览器临时私钥解密的单条信封，不返回明文。", {"id": ID, "public_key": text(5500), "request_id": text(128)}, ["id", "public_key", "request_id"]),
    definition("service_list", "读取服务目录", "从已登记模型或当前凭证结构生成只读引用，不解密、不重新登记服务。"),
    definition("service_detail", "读取服务引用", "读取服务来源和其关联配置引用。", {"id": ID}, ["id"]),
    definition("connection_list", "读取工具连接", "读取 MCP 配置及本地已安装插件，不进行探测、登录或安装。"),
]

_OUTPUT_KEYS = {
    "account_profile_update": ["account"], "account_profile_capture": ["capture_id", "expires_in_seconds"], "account_profile_import": ["profile"],
    "account_create": ["account"], "account_login": ["login"], "account_status": ["account", "login"],
    "account_default": ["default_account_id", "applies_to"], "open_workbench": ["view", "read_only", "status"],
    "workbench_state": ["view", "read_only", "status"], "workbench_sync": ["context", "revision", "base_revision", "unchanged"],
    "module_list": ["modules", "read_only"], "knowledge_list": ["scopes", "knowledge", "selected_scope"], "knowledge_detail": ["knowledge"], "model_list": ["models"], "model_detail": ["model", "credential"],
    "credential_list": ["entries", "folders", "status"],
    "credential_details": ["entry_id", "revision", "generation", "request_id", "public_key_sha256", "wrapped_key", "iv", "ciphertext"],
    "service_list": ["services"], "service_detail": ["service"], "connection_list": ["connections", "source_errors"],
}
for _tool in TOOLS:
    _tool["outputSchema"] = {"type": "object", "required": _OUTPUT_KEYS[_tool["name"]], "additionalProperties": True}
BY_NAME = {tool["name"]: tool for tool in TOOLS}


def tools_for_page(page):
    if page not in PAGES:
        raise ValueError("页面不存在")
    return list(TOOLS)


def validate(name: str, arguments: Any) -> dict:
    if name not in BY_NAME or not isinstance(arguments, dict):
        raise ValueError("工具或参数无效")
    schema = BY_NAME[name]["inputSchema"]
    if set(arguments) - set(schema["properties"]) or set(schema["required"]) - set(arguments):
        raise ValueError("请求包含未知字段或缺少必填字段")
    for key, value in arguments.items():
        _value(value, schema["properties"][key])
    return dict(arguments)


def _value(value, schema):
    types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
    if value is None and "null" in types:
        return
    if "string" in types and isinstance(value, str):
        if len(value) > schema.get("maxLength", 1000000):
            raise ValueError("字段文本过长")
    elif "boolean" in types and type(value) is bool:
        pass
    elif "integer" in types and type(value) is int:
        if not schema.get("minimum", value) <= value <= schema.get("maximum", value):
            raise ValueError("数值超出范围")
    elif "array" in types and isinstance(value, list):
        if len(value) > schema.get("maxItems", 1000):
            raise ValueError("列表过长")
        for item in value:
            _value(item, schema["items"])
    elif "object" in types and isinstance(value, dict):
        if len(value) > schema.get("maxProperties", 1000):
            raise ValueError("对象字段过多")
        properties = schema.get("properties")
        if properties is None:
            if schema.get("additionalProperties") is True:
                return
            properties = {}
        if set(value) - set(properties) or set(schema.get("required", [])) - set(value):
            raise ValueError("对象包含未知字段或缺少必填字段")
        for key, child in value.items():
            _value(child, properties[key])
    else:
        raise ValueError("参数类型无效")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("参数选项无效")
