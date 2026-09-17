#!/usr/bin/env python3
"""隔离认证进程内的双账户最小验收；只打印状态和用量，不输出请求、响应正文或令牌。"""
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import tempfile
import threading
from uuid import uuid4

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
from codex_workbench.gateway_auth import OfficialAuth, UPSTREAM
from codex_workbench.gateway_routes import AccountTarget, RouteStore
from codex_workbench.model_gateway import ModelGateway, Upstream

APP = '/Applications/ChatGPT.app/Contents/Resources/codex'
_ACCOUNT_ID = re.compile(r'[A-Za-z0-9_.:@-]{1,160}\Z')


def _authorized_account_ids() -> tuple[str, ...]:
    """读取显式订阅验收账户，不从本机账户目录推断范围。"""
    value=json.loads(os.environ.get('WORKBENCH_GATEWAY_ACCOUNT_IDS', '["current"]'))
    if (not isinstance(value,list) or not 1 <= len(value) <= 8 or 'current' not in value
            or len(value)!=len(set(value)) or any(not isinstance(item,str) or not _ACCOUNT_ID.fullmatch(item) for item in value)):
        raise ValueError('网关账户白名单无效')
    return tuple(value)


ALLOWED = _authorized_account_ids()


def main():
    """显式运行时检查用户配置的账户；会发送真实请求，运行前需自行授权。"""
    db_path = Path.home()/'Library/Application Support/CodexWorkbench/workbench.sqlite3'
    with sqlite3.connect('file:'+str(db_path)+'?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        saved = {r['id']: dict(r) for r in db.execute('SELECT id,codex_home,subject_id FROM execution_accounts') if r['id'] in ALLOWED}
    auth = {key: OfficialAuth(Path.home()/'.codex' if key=='current' else Path(saved[key]['codex_home']),
                  saved[key]['subject_id'], APP, current=key=='current') for key in ALLOWED}
    selected = ['current']
    accounts = lambda key=None: AccountTarget(key or selected[0], saved[key or selected[0]]['subject_id'])
    results = []
    with tempfile.TemporaryDirectory(prefix='workbench-subscription-probe-') as temporary:
        gateway = ModelGateway(RouteStore(Path(temporary)/'routes'), accounts,
                   {key: Upstream(UPSTREAM, item.authorize) for key,item in auth.items()}, secrets.token_urlsafe(32),
                   authenticate=auth['current'].accepts)
        serving = threading.Thread(target=gateway.serve_forever, daemon=True); serving.start()
        try:
            for key in ALLOWED:
                selected[0] = key; thread_id = str(uuid4())
                body = {'model':'gpt-6-astra', 'instructions':'Connectivity verification. Reply exactly OK. Do not use tools.',
                        'input':[{'role':'user','content':[{'type':'input_text','text':'Reply OK.'}]}],
                        'reasoning':{'effort':'low'}, 'stream':True,'store':False,
                        'client_metadata':{'thread_id':thread_id,'session_id':thread_id}}
                conn = http.client.HTTPConnection('127.0.0.1', gateway.server_port, timeout=75)
                try:
                    headers = {'Authorization': auth['current'].authorize().headers['Authorization'], 'Content-Type':'application/json'}
                    conn.request('POST','/v1/responses',json.dumps(body),headers)
                    response = conn.getresponse()
                    record = {'account_id':key,'http_status':response.status,'completed':False}
                    if response.status == 200:
                        for line in response:
                            if not line.startswith(b'data: '):continue
                            try:event=json.loads(line[6:])
                            except ValueError:continue
                            if event.get('type')=='response.completed':
                                record['completed']=True
                                record['usage']={k:v for k,v in event.get('response',{}).get('usage',{}).items()
                                                 if k in ('input_tokens','output_tokens','total_tokens') and type(v) is int}
                    else:
                        try: record['error_code']=json.loads(response.read()).get('error',{}).get('code')
                        except ValueError: record['error_code']='unclassified'
                    results.append(record)
                finally: conn.close()
        finally:
            gateway.shutdown(); gateway.server_close(); serving.join(2)
    print(json.dumps({'checks':results,'passed':all(r['completed'] for r in results)},ensure_ascii=False))


if __name__ == '__main__':
    try: main()
    except Exception:
        print(json.dumps({'passed':False,'error':'subscription_probe_unavailable'}))
        raise SystemExit(1)
