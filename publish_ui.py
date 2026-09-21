#!/usr/bin/env python3
"""检查界面后原子发布；不会改动账户、任务或官方 Codex 配置。"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
root=Path(__file__).resolve().parent
sys.dont_write_bytecode=True
sys.path.insert(0,str(root/'src'))
from codex_workbench.ui_release import build_release,RELEASE_FILE

def main():
    node=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node'
    executable=str(node) if node.is_file() else shutil.which('node')
    if not executable:raise SystemExit('未找到现有 Node，未发布界面')
    from codex_workbench.ui_resources import ui_html
    raw=ui_html()
    script=re.search(r'<script>([\s\S]*)</script>',raw).group(1)
    subprocess.run([executable,'--check'],input=script,text=True,check=True,cwd=root)
    for check in ('ui-primitives.mjs','ui-readonly.mjs','ui-other-accounts.mjs','ui-loading.mjs','configuration-crypto.mjs'):
        subprocess.run([executable,str(root/'tests'/check)],check=True,cwd=root)
    release=build_release()
    temporary=None
    try:
        with tempfile.NamedTemporaryFile('w',encoding='utf-8',dir=RELEASE_FILE.parent,prefix='.release-',delete=False) as stream:
            temporary=Path(stream.name);json.dump(release,stream,ensure_ascii=False,separators=(',',':'));stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,RELEASE_FILE)
    finally:
        if temporary and temporary.exists():temporary.unlink()
    print(json.dumps({'version':release['version'],'revision':release['revision'],'published':True}))
if __name__=='__main__':main()
