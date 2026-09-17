"""生成不会把任务正文或凭据带入列表视图的简短标题。"""

import re
from urllib.parse import urlsplit


_ACTION_PATTERN = re.compile(
    r"修复|实现|创建|优化|设计|整理|生成|分析|检查|部署|更新|迁移|编写|配置|搭建|导出|排查|制作|调整|完善|验证|总结|开发"
)
_MARKDOWN_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_URL = re.compile(r"(?:https?|ftp)://[^\s<>(){}]+", re.IGNORECASE)
_ABSOLUTE_PATH = re.compile(r"(?:~?/|[A-Za-z]:[\\/])[^\s`'\"<>|，,。；;、!?！？]*")
_ASSIGNED_SECRET = re.compile(
    r"(?P<field>\b(?:app[_-]?key|api[_-]?key|password|passwd|token|authorization)\b|密码|密钥|令牌)"
    r"\s*(?:=|:|：|\s+)\s*(?:Bearer\s+[^\s,;，；]+|\"[^\"]*\"|'[^']*'|[^\s,;，；]+)",
    re.IGNORECASE,
)
_BEARER_SECRET = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_API_SECRET = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")


def safe_display_title(text: str, limit: int = 50) -> str:
    """将不可信任务文本压缩为可安全展示的标题。

    标题只保留行动摘要；链接、路径和常见凭据值不会随原生目录或工作台
    状态返回。完整任务描述仍由调用方按原有业务字段保存。
    """
    if not isinstance(text, str):
        raise ValueError("标题必须是文本")
    if limit < 2:
        raise ValueError("标题长度上限至少为 2")
    return _display_title(text, limit, extract_action=False)


def _display_title(text: str, limit: int, *, extract_action: bool) -> str:
    """净化展示文本；仅默认标题或风险输入提取行动短句。"""
    value = text.strip()
    if not value:
        return "未命名任务"
    value = re.sub(r"\\([!\"#$%&'()*+,\-./:;<=>?@[\\\]^_`{|}~])", r"\1", value)
    original = value
    value = _MARKDOWN_IMAGE.sub(lambda match: match.group(1) or "图片", value)
    value = _MARKDOWN_LINK.sub(lambda match: match.group(1), value)
    value = _URL.sub(_safe_url, value)
    value = _ABSOLUTE_PATH.sub(_safe_path, value)
    value = _ASSIGNED_SECRET.sub(_hidden_assignment, value)
    value = _BEARER_SECRET.sub("Bearer <已隐藏>", value)
    value = _API_SECRET.sub("<已隐藏>", value)
    value = _JWT.sub("<已隐藏>", value)
    value = re.sub(r"\s+", " ", value).strip()
    if not extract_action and value == original and len(value) <= limit:
        return value
    parts = [part.strip(" ，,：:；;。.!！?？-—") for part in re.split(r"[。！!？?；;\n]", value) if part.strip()]
    candidates = []
    for index, part in enumerate(parts):
        part = re.sub(r"^(?:请|帮我|帮忙|麻烦|你|希望|我想|我需要|需要|可以|能不能|一下|把|将)+", "", part).strip()
        match = _ACTION_PATTERN.search(part)
        if match and not re.search(r"(?:不要|不用|无需|禁止|别).{0,4}$", part[:match.start()]):
            candidates.append((match.start() == 0, -index, part[match.start():]))
    chosen = max(candidates)[2] if candidates else (parts[0] if parts else value)
    chosen = re.sub(r"[，,].*$", "", chosen).strip()
    if not chosen:
        chosen = "未命名任务"
    return chosen if len(chosen) <= limit else chosen[:limit - 1].rstrip(" ，,：:") + "…"


def _safe_url(match: re.Match[str]) -> str:
    """以 URL 的 host 代替可能包含查询参数或访问令牌的原始链接。"""
    try:
        host = urlsplit(match.group(0)).hostname
    except ValueError:
        host = None
    return f"链接({host})" if host else "链接"


def _safe_path(match: re.Match[str]) -> str:
    """以路径末段代替可能暴露用户目录的绝对路径。"""
    path = match.group(0).rstrip("/\\")
    name = re.split(r"[\\/]", path)[-1]
    return f"文件({name})" if name else "文件"


def _hidden_assignment(match: re.Match[str]) -> str:
    """保留字段名，隐藏其后的值。"""
    return f"{match.group('field')}=<已隐藏>"


def suggest_title(description: str, limit: int = 28) -> str:
    """优先提取描述中的行动句；完整描述仅保存到任务正文。"""
    if not isinstance(description, str) or not description.strip():
        raise ValueError("请填写任务描述")
    return _display_title(description, limit, extract_action=True)
