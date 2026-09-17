"""工作台分区和项目外观的受限校验。"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ElementTree
from typing import Any


_COLOR = re.compile(r"#[0-9A-Fa-f]{6}")
_SVG_TAGS = frozenset({"svg", "g", "path", "circle", "rect", "ellipse", "line", "polyline", "polygon", "title", "desc"})
_SVG_ATTRIBUTES = frozenset({
    "viewBox", "width", "height", "fill", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin",
    "d", "cx", "cy", "r", "rx", "ry", "x", "y", "x1", "x2", "y1", "y2", "points", "transform",
    "opacity", "fill-opacity", "stroke-opacity", "xmlns",
    "fill-rule", "clip-rule", "stroke-dasharray", "stroke-dashoffset", "stroke-miterlimit", "vector-effect",
})


class AppearanceError(ValueError):
    """外观数据不满足安全契约。"""


def validate_color(value: Any) -> str | None:
    """校验可选的 ``#RRGGBB`` 颜色，并规范为大写。"""
    if value is None:
        return None
    if not isinstance(value, str) or _COLOR.fullmatch(value) is None:
        raise AppearanceError("颜色必须是 #RRGGBB")
    return value.upper()


def validate_icon(value: Any, *, allow_symbol: bool = False) -> str | None:
    """校验并规范化图标 JSON；自建实体仅接受 emoji 或安全 SVG。"""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"kind", "value"}:
        raise AppearanceError("图标必须是含 kind 和 value 的对象")
    kind, icon_value = value["kind"], value["value"]
    allowed = {"emoji", "svg"} | ({"symbol"} if allow_symbol else set())
    if not isinstance(kind, str) or kind not in allowed or not isinstance(icon_value, str) or not icon_value:
        raise AppearanceError("图标类型或内容无效")
    if kind == "emoji":
        if len(icon_value) > 32 or any(ord(character) < 32 for character in icon_value):
            raise AppearanceError("emoji 图标无效")
    elif kind == "svg":
        _validate_svg(icon_value)
    elif len(icon_value) > 80:
        raise AppearanceError("symbol 图标过长")
    return json.dumps({"kind": kind, "value": icon_value}, ensure_ascii=False, separators=(",", ":"))


def icon_value(value: Any) -> dict[str, str] | None:
    """将数据库中经过校验的图标 JSON 投影为 API 对象。"""
    if value is None:
        return None
    return json.loads(value)


def _validate_svg(value: str) -> None:
    if len(value.encode("utf-8")) > 64 * 1024:
        raise AppearanceError("SVG 图标不能超过 64KiB")
    lowered = value.lower()
    if "<!doctype" in lowered or "<!entity" in lowered or "<script" in lowered or "<foreignobject" in lowered:
        raise AppearanceError("SVG 含不安全元素")
    try:
        root = ElementTree.fromstring(value)
    except ElementTree.ParseError as exc:
        raise AppearanceError("SVG 格式无效") from exc
    if root.tag.rsplit("}", 1)[-1] != "svg":
        raise AppearanceError("图标必须以 SVG 元素为根节点")
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1] if isinstance(element.tag, str) else ""
        if tag not in _SVG_TAGS:
            raise AppearanceError("SVG 仅允许基础形状")
        for attribute, attribute_value in element.attrib.items():
            name = attribute.rsplit("}", 1)[-1]
            if name not in _SVG_ATTRIBUTES or name.lower().startswith("on") or name in {"href", "style"}:
                raise AppearanceError("SVG 含不安全属性")
            if "url(" in attribute_value.lower() or "http:" in attribute_value.lower() or "https:" in attribute_value.lower():
                raise AppearanceError("SVG 不允许外部引用")
