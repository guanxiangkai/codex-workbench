#!/usr/bin/env python3
"""Build the standalone, synthetic Codex Workbench demonstration."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def replace_between(source: str, start: str, end: str, replacement: str) -> str:
    """Replace one source span and fail when the maintained UI shape changes."""
    left = source.find(start)
    right = source.find(end, left + len(start))
    if left < 0 or right < 0:
        raise RuntimeError(f"UI source marker changed: {start!r} .. {end!r}")
    return source[:left] + replacement + source[right:]


def replace_exact(source: str, old: str, new: str = "") -> str:
    if source.count(old) != 1:
        raise RuntimeError(f"Expected one UI source fragment: {old[:48]!r}")
    return source.replace(old, new)


def build_app() -> str:
    ui = ROOT / "ui"
    source = (ui / "readonly.js").read_text(encoding="utf-8")
    adapter = (ROOT / "demo" / "demo-adapter.js").read_text(encoding="utf-8")

    source = replace_between(source, "async function fetchView", "const s={", adapter + "\n")
    source = replace_between(
        source,
        "function usageRefreshButton()",
        "\nfunction resetAnalysisView",
        "function usageRefreshButton(){return '<span class=\"muted\">合成快照</span>';}\n",
    )
    source, count = re.subn(r"(?m)^  const accountActions=.*;\n", "  const accountActions='';\n", source)
    if count != 1:
        raise RuntimeError("Expected one account action fragment")
    source, count = re.subn(r'<button type="button" id="account-create".*?</button>', "", source, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError("Expected one account creation button")
    source, count = re.subn(r"(?m)^async function (?:loginAccount|createAccount|checkAccount)\(.*\n", "", source)
    if count != 3:
        raise RuntimeError("Expected three account write functions")
    source = replace_between(
        source,
        "function defaultAccountButton",
        "async function load",
        "function defaultAccountButton(){return '';}\n",
    )
    source = replace_between(
        source,
        "async function readSecret",
        "\nfunction bind(){",
        "",
    )
    source = replace_between(
        source,
        "function secretPanel",
        "\n\nfunction navigation",
        "function secretPanel(d){const entry=d.entry||d.value?.credential;if(!entry)return '';const rows=[['名称',entry.label||entry.name||'公开演示元数据'],['类型',entry.service_type_label||entry.service_type||'演示配置'],['说明',entry.description||'仅展示合成的公开元数据。']];return `<section class=\"card config-field-panel\"><h2>公开演示元数据</h2><dl>${rows.map(([label,value])=>`<div><dt>${E(label)}</dt><dd>${E(value)}</dd></div>`).join('')}</dl></section>`;}\n",
    )
    source = replace_exact(source, ";if(entry&&!document.hidden)await readSecret('fields');", ";")
    for handler in (
        " document.getElementById('refresh-usage')?.addEventListener('click',()=>load({refresh:true}));\n",
        " document.getElementById('account-create')?.addEventListener('click',()=>createAccount());\n",
        " document.querySelectorAll('[data-account-login]').forEach(button=>button.onclick=()=>loginAccount(button.dataset.accountLogin));\n",
        " document.querySelectorAll('[data-account-status]').forEach(button=>button.onclick=()=>checkAccount(button.dataset.accountStatus));\n",
        " document.querySelectorAll('[data-account-default]').forEach(button=>button.onclick=()=>setDefaultAccount(button.dataset.accountDefault));\n",
        "document.getElementById('fields')?.addEventListener('click',()=>readSecret('fields'));",
        "document.querySelectorAll('[data-reveal]').forEach(b=>b.onclick=()=>{const n=Number(b.dataset.reveal);if(s.revealed.has(n)){s.revealed.delete(n);render();}else readSecret('view',n);});",
        "document.querySelectorAll('[data-copy]').forEach(b=>b.onclick=()=>readSecret('copy',Number(b.dataset.copy)));",
    ):
        source = replace_exact(source, handler)

    icons = (ui / "assets" / "readonly-icons.json").read_text(encoding="utf-8")
    primitives = (ui / "readonly-primitives.js").read_text(encoding="utf-8")
    prefix = "\n".join((
        "const WORKBENCH_MODULES=globalThis.DEMO_FIXTURES.modules;",
        "const INITIAL_PAGE='accounts';",
        f"const READONLY_ICONS={icons};",
        "const RESOURCE_URI='';",
        "const UI_REVISION='static-demo';",
        "const WORKBENCH_BOOTSTRAP={context:'static-demo-v1',views:Object.entries(globalThis.DEMO_FIXTURES.views).map(([view,data])=>({args:{view},revision:`demo-${view}`,data}))};",
        "",
    ))
    app = prefix + primitives + "\n" + source
    forbidden = ("fetch(", "/rpc", "postMessage", "credential_details", "ConfigurationCrypto", "account_create", "account_login", "account_status", "account_default")
    found = [token for token in forbidden if token in app]
    if found:
        raise RuntimeError(f"Generated static app retained prohibited bridge code: {', '.join(found)}")
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="directory for the publishable static site")
    args = parser.parse_args()
    output = args.output.resolve()
    if output.is_symlink():
        raise RuntimeError("Refusing to write build output through a symbolic link")
    assets = output / "assets"
    assets.mkdir(parents=True, exist_ok=True)

    css = (ROOT / "ui" / "readonly.css").read_text(encoding="utf-8")
    css += "\n.demo-banner{padding:.7rem 1rem;background:#13233d;color:#e6f1ff;text-align:center;font-size:.9rem}.demo-banner strong{margin-right:.5rem;color:#8bd3ff}\n"
    (assets / "app.css").write_text(css, encoding="utf-8")
    (assets / "fixtures.js").write_text((ROOT / "demo" / "fixtures.js").read_text(encoding="utf-8"), encoding="utf-8")
    (assets / "app.js").write_text(build_app(), encoding="utf-8")
    csp = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    page = f"""<!doctype html>
<html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><meta http-equiv=\"Content-Security-Policy\" content=\"{csp}\"><title>Codex 工作台（静态演示）</title><link rel=\"stylesheet\" href=\"assets/app.css\"></head>
<body><div class=\"demo-banner\" role=\"status\"><strong>静态演示数据</strong><span>全部账户、模型、配置与知识均为合成数据。</span></div><div id=\"app\"></div><script src=\"assets/fixtures.js\"></script><script src=\"assets/app.js\"></script></body></html>
"""
    (output / "index.html").write_text(page, encoding="utf-8")
    print(f"Built static demo: {output}")


if __name__ == "__main__":
    main()
