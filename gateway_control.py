#!/usr/bin/env python3
"""准备、启动、接入及撤回本机模型网关；原生认证材料永不进入部署制品。"""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tomllib
import time

sys.dont_write_bytecode=True
SOURCE=Path(__file__).resolve().parent
sys.path.insert(0,str(SOURCE/'src'))
from codex_workbench.gateway_daemon import atomic_json
from codex_workbench.gateway_routes import RouteStore
from codex_workbench.runtime import secure_directory

DATA=Path.home()/'Library/Application Support/CodexWorkbench'
ROOT=DATA/'model-gateway'
CONFIG=Path.home()/'.codex/config.toml'
LABEL='org.codexworkbench.model-gateway'
PLIST=Path.home()/'Library/LaunchAgents'/f'{LABEL}.plist'
CLI='/Applications/ChatGPT.app/Contents/Resources/codex'
_ACCOUNT_ID = re.compile(r'[A-Za-z0-9_.:@-]{1,160}\Z')


def _authorized_account_ids() -> tuple[str, ...]:
    """读取显式网关账户白名单；配置无效时拒绝准备网关。"""
    value=json.loads(os.environ.get('WORKBENCH_GATEWAY_ACCOUNT_IDS', '["current"]'))
    if (not isinstance(value,list) or not 1 <= len(value) <= 8 or 'current' not in value
            or len(value)!=len(set(value)) or any(not isinstance(item,str) or not _ACCOUNT_ID.fullmatch(item) for item in value)):
        raise ValueError('网关账户白名单无效')
    return tuple(value)


ALLOWED=_authorized_account_ids()
TOP='# BEGIN CODEX WORKBENCH GATEWAY\nmodel_provider = "workbench_gateway"\n# END CODEX WORKBENCH GATEWAY\n'
BOTTOM='''\n# BEGIN CODEX WORKBENCH PROVIDER
[model_providers.workbench_gateway]
name = "Codex Workbench Gateway"
base_url = "http://127.0.0.1:18742/v1"
wire_api = "responses"
requires_openai_auth = true
supports_websockets = false
request_max_retries = 4
stream_max_retries = 5
stream_idle_timeout_ms = 300000
# END CODEX WORKBENCH PROVIDER
'''


def prepare():
    """复制无秘密源码为独立制品；仅持久化显式授权的账户清单。"""
    secure_directory(ROOT)
    files=sorted((SOURCE/'src/codex_workbench').glob('*.py'))
    included=files+[SOURCE/'validate_model_gateway.py',SOURCE/'tests/native_protocol_probe.py']
    digest=hashlib.sha256(b''.join(p.name.encode()+p.read_bytes() for p in included)).hexdigest()
    release=ROOT/'releases'/digest
    if not release.exists():
        (release/'src/codex_workbench').mkdir(parents=True,mode=0o700)
        for source in files:shutil.copyfile(source,release/'src/codex_workbench'/source.name)
        (release/'tests').mkdir()
        shutil.copyfile(SOURCE/'tests/native_protocol_probe.py',release/'tests/native_protocol_probe.py')
        shutil.copyfile(SOURCE/'validate_model_gateway.py',release/'validate_model_gateway.py')
        (release/'run.py').write_text('import sys\nfrom pathlib import Path\nsys.dont_write_bytecode=True\nsys.path.insert(0,str(Path(__file__).parent/"src"))\nfrom codex_workbench.gateway_daemon import main\nmain()\n')
    with sqlite3.connect('file:'+str(DATA/'workbench.sqlite3')+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        accounts=[{'id':r['id'],'home':str(Path.home()/'.codex') if r['id']=='current' else r['codex_home'],
                   'subject_id':r['subject_id']} for r in db.execute('SELECT id,codex_home,subject_id FROM execution_accounts') if r['id'] in ALLOWED]
    if {a['id'] for a in accounts}!=set(ALLOWED) or any(not a['subject_id'] for a in accounts):raise ValueError('授权账户未完整登记')
    atomic_json(ROOT/'settings.json',{'authorized_account_ids':list(ALLOWED),'accounts':accounts,'cli':CLI,'port':18742,'release':str(release),'probe':str(release/'validate_model_gateway.py')})
    RouteStore(ROOT)
    payload={'Label':LABEL,'ProgramArguments':[sys.executable,str(release/'run.py'),'--root',str(ROOT)],
             'RunAtLoad':True,'KeepAlive':{'SuccessfulExit':False},'ThrottleInterval':30,
             'WorkingDirectory':str(ROOT),'StandardOutPath':'/dev/null','StandardErrorPath':'/dev/null',
             'EnvironmentVariables':{'PYTHONDONTWRITEBYTECODE':'1'}}
    if PLIST.exists():
        old=plistlib.loads(PLIST.read_bytes())
        if old.get('Label')!=LABEL:raise ValueError('启动入口冲突')
    PLIST.write_bytes(plistlib.dumps(payload));PLIST.chmod(0o600)
    print(json.dumps({'prepared':True,'release':digest,'native_provider_changed':False}))


def start():
    """只启动本工具拥有的 LaunchAgent，不重启或退出 Codex。"""
    subprocess.run(['launchctl','bootout',f'gui/{os.getuid()}/{LABEL}'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    for attempt in range(4):
        result=subprocess.run(['launchctl','bootstrap',f'gui/{os.getuid()}',str(PLIST)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if result.returncode==0:break
        if attempt==3:raise ValueError('网关启动入口未加载')
        time.sleep(.4)
    print(json.dumps({'start_requested':True}))


def replace_config(before: bytes, after: bytes):
    """仅替换已核验的同一配置；临时文件留在官方本机目录，权限保持 0600。"""
    if CONFIG.is_symlink() or CONFIG.read_bytes()!=before:raise ValueError('原生配置已变化，未覆盖')
    tomllib.loads(after.decode())
    temporary=CONFIG.with_name('config.gateway-'+secrets.token_hex(6)+'.tmp')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(after);stream.flush();os.fsync(stream.fileno())
        if CONFIG.read_bytes()!=before:raise ValueError('原生配置发生并发修改，未覆盖')
        os.replace(temporary,CONFIG)
    finally:temporary.unlink(missing_ok=True)


def activate():
    """服务验收后接入固定 provider；先确认既有会话归主账户，不修改原生记录。"""
    status=json.loads((ROOT/'status.json').read_text())
    if status.get('ready') is not True:raise ValueError('网关尚未就绪')
    client=http.client.HTTPConnection('127.0.0.1',18742,timeout=3)
    try:
        client.request('GET','/health');response=client.getresponse()
        if response.status!=200 or json.loads(response.read()).get('ready') is not True:raise ValueError('网关未响应')
    finally:client.close()
    evidence=json.loads((ROOT/'subscription-validation.json').read_text())
    if evidence.get('passed') is not True:raise ValueError('缺少真实账户验收')
    if json.loads((ROOT/'official-cli-validation.json').read_text()).get('passed') is not True:raise ValueError('完整 CLI 链路尚未通过')
    settings=json.loads((ROOT/'settings.json').read_text())
    main=next(a for a in settings['accounts'] if a['id']=='current')
    before=CONFIG.read_bytes();parsed=tomllib.loads(before.decode())
    if parsed.get('model_provider')=='workbench_gateway':
        print(json.dumps({'already_active':True}));return
    if parsed.get('model_provider') or 'workbench_gateway' in parsed.get('model_providers',{}):raise ValueError('已有其他 provider，未覆盖')
    store=RouteStore(ROOT)
    with sqlite3.connect('file:'+str(Path.home()/'.codex/state_5.sqlite')+'?mode=ro',uri=True) as native:
        existing=dict(native.execute('SELECT id,source FROM threads WHERE model_provider="openai"'))
    def root_of(key):
        seen=set();cursor=key
        while cursor in existing:
            if cursor in seen:raise ValueError('原生子代理归属出现循环')
            seen.add(cursor);source=existing[cursor]
            if not isinstance(source,str) or not source.startswith('{'):return cursor
            parent=(json.loads(source).get('subagent') or {}).get('thread_spawn',{}).get('parent_thread_id')
            if not parent:return cursor
            cursor=parent
        raise ValueError('原生子代理缺少主会话归属')
    # 只建立网关路由归属，原生会话、目录和数据库完全保留。
    with store._db() as db:
        import time
        for key in existing:
            db.execute('INSERT OR IGNORE INTO routes VALUES(?,?,?,?,?)',(key,root_of(key),'current',main['subject_id'],time.time()))
    backup=ROOT/'config.before-gateway.toml'
    if not backup.exists():
        fd=os.open(backup,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as stream:stream.write(before)
    replace_config(before,(TOP+before.decode()+BOTTOM).encode())
    atomic_json(ROOT/'activation.json',{'enabled':True,'existing_routes_preserved':len(existing),'native_ui_verified':False})
    print(json.dumps({'enabled':True,'existing_routes_preserved':len(existing),'native_ui_verified':False}))


def recovery():
    """只恢复受管 provider 的官方重试值；不变更账户、模型或其他配置。"""
    before=CONFIG.read_bytes();text=before.decode()
    previous=BOTTOM.replace('request_max_retries = 4','request_max_retries = 0').replace('stream_max_retries = 5','stream_max_retries = 0')
    if BOTTOM in text:
        print(json.dumps({'recovery_enabled':True,'already_configured':True}));return
    if text.count(previous)!=1 or TOP not in text:raise ValueError('受管配置已变化，未覆盖')
    evidence=json.loads((ROOT/'official-tools-validation.json').read_text())
    if evidence.get('passed') is not True:raise ValueError('缺少多次工具调用验收')
    backup=ROOT/'config.before-recovery.toml'
    if not backup.exists():
        fd=os.open(backup,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as stream:stream.write(before)
    replace_config(before,text.replace(previous,BOTTOM,1).encode())
    print(json.dumps({'recovery_enabled':True,'request_retries':4,'stream_retries':5,'other_configuration_preserved':True}))


def deactivate():
    """仅移除本工具原样写入的配置块；其他编辑保留，修改过的块拒绝自动覆盖。"""
    before=CONFIG.read_bytes();text=before.decode()
    if TOP not in text or BOTTOM not in text:raise ValueError('接入块已变化，未自动修改')
    replace_config(before,text.replace(TOP,'',1).replace(BOTTOM,'',1).encode())
    atomic_json(ROOT/'activation.json',{'enabled':False})
    print(json.dumps({'enabled':False,'other_configuration_preserved':True}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['prepare','start','activate','recovery','deactivate','status'])
    args=parser.parse_args()
    try:
        if args.action=='status':print((ROOT/'status.json').read_text())
        else:globals()[args.action]()
    except Exception:
        print(json.dumps({'success':False,'error':'gateway_control_failed'}));raise SystemExit(1)
