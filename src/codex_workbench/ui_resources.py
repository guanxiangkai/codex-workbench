"""本地 UI HTML 与原生入口图标的纯加载函数。"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from .catalog import UI_VIEWS, MODULES, DEFAULT_VIEW


_VIEWS = UI_VIEWS


def ui_html(page: str = DEFAULT_VIEW) -> str:
    """读取受控入口页面，并嵌入本机图标和表情选择目录。"""
    if page not in _VIEWS:
        raise ValueError("页面不存在")
    root = Path(__file__).resolve().parents[2] / "ui"
    icons=json.dumps(json.loads((root/'assets/readonly-icons.json').read_text()),ensure_ascii=False).replace('<','\\u003c')
    return ((root / "app.html").read_text(encoding="utf-8")
            .replace("__WORKBENCH_MODULES__",json.dumps(MODULES,ensure_ascii=False).replace("<","\\u003c"))
            .replace("__WORKBENCH_INITIAL_PAGE__",page)
            .replace("__WORKBENCH_READONLY_ICONS__",icons)
            .replace("__WORKBENCH_READONLY_CSS__",(root/'readonly.css').read_text())
            .replace("__WORKBENCH_CONFIG_CRYPTO__",(root/'configuration-crypto.js').read_text())
            .replace("__WORKBENCH_READONLY_JS__",(root/'readonly.js').read_text()))



def page_icons(page: str = "workbench") -> list[dict]:
    """返回单一工作台入口的浅色和深色 MCP 图标数据 URL。"""
    if page != "workbench":
        raise ValueError("页面不存在")
    source = (Path(__file__).resolve().parents[2] / "ui" / "icons" / "board.svg").read_text(encoding="utf-8")
    return [{"src": "data:image/svg+xml;base64," + base64.b64encode(source.replace("currentColor", color).encode()).decode(),
             "mimeType": "image/svg+xml", "sizes": ["any"], "theme": theme}
            for theme, color in (("light", "#505866"), ("dark", "#d0d6df"))]
