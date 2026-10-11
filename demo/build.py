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

    # Rename private bridge names in the UI source before injecting the adapter;
    # the adapter itself must keep the original names so its rejection tests
    # exercise the real contract.
    for private_name, static_name in {
        "account_create": "static_create",
        "account_login": "static_login",
        "account_status": "static_status",
        "account_default": "static_default",
    }.items():
        source = source.replace(private_name, static_name)

    source = replace_between(
        source,
        "async function fetchView",
        "const s={",
        adapter + "\nclass Bridge{constructor(){this.embedded=false;}async initialize(){}async tool(name,args={}){return WORKBENCH_ADAPTER.call(name,args);}}\n",
    )
    source = replace_between(source, "function profileUpdateButton", "function usageRefreshButton", "function profileUpdateButton(){return '';}\n")
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
    # The read-only source still contains the private bridge name in a dead
    # handler branch. Rename it in the static artifact so the demo contract
    # cannot be mistaken for a credential-capable build.
    source = "function resetCountdown(){}\nfunction updateResetCountdown(){}\n" + source
    source, _ = re.subn(r"window\.__workbenchReadonlyTest=\{.*?\};\n?", "", source, count=1, flags=re.S)
    for handler in (
        "document.querySelectorAll('[data-profile-id]').forEach(b=>b.onclick=()=>updateProfile(b.dataset.profileView,b.dataset.profileId));\n",
        " document.getElementById('refresh-usage')?.addEventListener('click',()=>load({refresh:true}));\n",
        " document.getElementById('account-create')?.addEventListener('click',()=>createAccount());\n",
        " document.querySelectorAll('[data-account-login]').forEach(button=>button.onclick=()=>loginAccount(button.dataset.accountLogin));\n",
        " document.querySelectorAll('[data-account-status]').forEach(button=>button.onclick=()=>checkAccount(button.dataset.accountStatus));\n",
        " document.querySelectorAll('[data-account-default]').forEach(button=>button.onclick=()=>setDefaultAccount(button.dataset.accountDefault));\n",
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
    forbidden = ("fetch(", "/rpc", "postMessage", "credential_details", "ConfigurationCrypto", "account_profile_update", "account_profile_capture", "account_profile_import", "data-profile-id", "account_create", "account_login", "account_status", "account_default")
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
<body><div class=\"demo-banner\" role=\"status\"><strong>静态演示数据</strong><span>全部账户和模型均为合成数据。</span></div><div id=\"app\"></div><script src=\"assets/fixtures.js\"></script><script src=\"assets/app.js\"></script></body></html>
"""
    (output / "index.html").write_text(page, encoding="utf-8")
    print(f"Built static demo: {output}")


if __name__ == "__main__":
    main()
