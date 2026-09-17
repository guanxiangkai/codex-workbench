"""专业配置可引用的模型服务；这里只保存端点与受管凭据引用。"""

from urllib.parse import urlsplit
import re


def validate_services(value: object) -> list[dict]:
    """校验非秘密模型服务列表，不读取凭据或发起网络调用。"""
    if not isinstance(value, list) or len(value) > 10:
        raise ValueError("最多配置 10 个模型服务")
    fields = {"name", "base_url", "model", "protocol", "credential_ref"}
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) - fields:
            raise ValueError("模型服务只接受名称、接口地址、模型、协议和凭据引用")
        if any(not isinstance(v, str) or len(v) > 4096 for v in item.values()):
            raise ValueError("模型服务字段必须是有界文本")
        data = {key: item.get(key, "").strip() for key in fields}
        if not data["name"] or len(data["name"]) > 160:
            raise ValueError("请填写模型服务名称")
        try:
            url = urlsplit(data["base_url"])
            if url.port is not None and not 1 <= url.port <= 65535:
                raise ValueError()
        except ValueError:
            raise ValueError("接口地址或端口无效") from None
        if url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("接口地址需为 HTTP(S) 地址，不能带账号、密码、查询参数或片段")
        if data["protocol"] not in {"", "openai-compatible"}:
            raise ValueError("当前支持 OpenAI 兼容模型服务配置")
        reference = data["credential_ref"]
        if reference and (not re.fullmatch(r"vault:[A-Za-z0-9][A-Za-z0-9_.:-]{0,180}", reference) or reference[6:].lower().startswith(("sk-", "bearer", "eyj"))):
            raise ValueError("凭据只能填写 vault:<entry-ref> 保险库引用，不能填写 API Key 明文")
        data["protocol"] = "openai-compatible"
        result.append(data)
    return result
