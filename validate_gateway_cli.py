#!/usr/bin/env python3
"""以官方 CLI 和原有官方登录完成最小网关验收；不读认证、不持久保存会话。"""
import json
import argparse
import os
import re
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parent/'src'))
from codex_workbench.account_runtime import account_environment,current_account_home
from codex_workbench.gateway_daemon import atomic_json


_ACCOUNT_ID = re.compile(r'[A-Za-z0-9_.:@-]{1,160}\Z')


def _expected_account_id() -> str:
    """读取显式期望路由账户；默认仅验证当前账户。"""
    value=os.environ.get('WORKBENCH_GATEWAY_EXPECTED_ACCOUNT','current')
    if not isinstance(value,str) or not _ACCOUNT_ID.fullmatch(value):
        raise ValueError('网关期望账户无效')
    return value


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--tools',action='store_true');args=parser.parse_args()
    expected_account_id=_expected_account_id()
    root=Path.home()/'Library/Application Support/CodexWorkbench/model-gateway'
    with tempfile.TemporaryDirectory(prefix='workbench-native-gateway-') as temp:
        temporary=Path(temp).resolve()
        prompt='Reply WORKBENCH_GATEWAY_OK.'
        instructions='For this connectivity test return exactly WORKBENCH_GATEWAY_OK. Never use tools.'
        if args.tools:
            for index in range(1,4):
                (temporary/f'part-{index}.txt').write_text((f'synthetic-network-check-{index} '+('abcdef0123456789 '*48)+'\n')*24+f'END_PART_{index}\n')
            instructions='This is a bounded network continuity acceptance test. Use only local read-only shell commands in the working directory. No external services, no file changes. Execute exactly three sequential shell tool calls, one per part, and wait for each tool result before the next. Each command is: sleep 2; cat part-N.txt (N is 1, then 2, then 3). Once all three results arrived, reply exactly WORKBENCH_GATEWAY_OK. Do not combine the three calls.'
            prompt='Run the three sequential read-only tool calls specified in your instructions and finish with the exact marker.'
        (temporary/'instructions.md').write_text(instructions)
        settings={'model_provider':'workbench_gateway','model':'gpt-6-astra','model_reasoning_effort':'high' if args.tools else 'low',
                  'model_providers.workbench_gateway.name':'Codex Workbench Gateway',
                  'model_providers.workbench_gateway.base_url':'http://127.0.0.1:18742/v1',
                  'model_providers.workbench_gateway.wire_api':'responses',
                  'model_providers.workbench_gateway.requires_openai_auth':True,
                  'model_providers.workbench_gateway.supports_websockets':False,
                  'model_providers.workbench_gateway.request_max_retries':4,
                  'model_providers.workbench_gateway.stream_max_retries':5,
                  'sqlite_home':str(temporary/'state'),'log_dir':str(temporary/'logs'),
                  'model_instructions_file':str(temporary/'instructions.md'),'project_doc_max_bytes':0,
                  'features.apps':False,'analytics.enabled':False}
        command=['/Applications/ChatGPT.app/Contents/Resources/codex','exec','--ephemeral','--ignore-user-config',
                 '--skip-git-repo-check','--json','-s','read-only','-C',str(temporary)]
        for key,value in settings.items():command.extend(['-c',key+'='+json.dumps(value)])
        command.append(prompt)
        result=subprocess.run(command,env=account_environment(current_account_home(),use_current=True),
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=240 if args.tools else 90)
        record={'exit_code':result.returncode,'completed':False,'expected_reply':False,'thread_id':None,'tool_calls':0}
        combined=(result.stdout+result.stderr).decode(errors='replace')
        record['failure_codes']=[code for code in ('upstream_rejected','gateway_unavailable','gateway_auth_required','encoding_unsupported',
            'upstream_invalid_max_output_tokens','upstream_invalid_temperature','upstream_invalid_store','upstream_invalid_stream',
            'upstream_invalid_instructions','upstream_invalid_service_tier','upstream_invalid_prompt_cache_retention',
            'upstream_invalid_context_management','upstream_invalid_reasoning','upstream_invalid_input','upstream_invalid_tools','upstream_invalid_model') if code in combined]
        tool_ids=set();completed_commands=set()
        for line in result.stdout.splitlines():
            try:event=json.loads(line)
            except ValueError:continue
            if event.get('type')=='thread.started':record['thread_id']=event.get('thread_id')
            if event.get('type')=='turn.completed':record['completed']=True;record['usage']=event.get('usage')
            if event.get('type') in {'error','turn.failed'}:
                failure=event.get('error',event)
                text=str(failure.get('message','')) if isinstance(failure,dict) else ''
                # 此探针仅发送固定的公开测试输入；仍移除认证模式、长字符串、URL 和标识。
                text=re.sub(r'(?i)Bearer\s+\S+|https?://\S+|[\w./+=@:-]{18,}', '[redacted]', text)
                record['diagnostic']=text[:400]
            item=event.get('item',{})
            if item.get('type')=='agent_message' and item.get('text','').strip()=='WORKBENCH_GATEWAY_OK':record['expected_reply']=True
            if item.get('type') in {'command_execution','mcp_tool_call','web_search'}:tool_ids.add(item.get('id'))
            if item.get('type')=='command_execution' and event.get('type')=='item.completed' and item.get('exit_code')==0:completed_commands.add(item.get('id'))
        record['tool_calls']=len(tool_ids);record['successful_commands']=len(completed_commands)
        with sqlite3.connect('file:'+str(root/'routes.sqlite3')+'?mode=ro',uri=True) as db:
            row=db.execute('SELECT account_id FROM routes WHERE thread_id=?',(record['thread_id'],)).fetchone()
        record['routed_account_id']=row[0] if row else None
        record['passed']=all((record['exit_code']==0,record['completed'],record['expected_reply'],(record['successful_commands']==3 if args.tools else record['tool_calls']==0),
                              record['routed_account_id']==expected_account_id))
        record['native_desktop_button_verified']=False
        atomic_json(root/('official-tools-validation.json' if args.tools else 'official-cli-validation.json'),record)
        print(json.dumps(record))
        raise SystemExit(0 if record['passed'] else 1)


if __name__=='__main__':
    try:main()
    except Exception:
        print(json.dumps({'passed':False,'error':'official_cli_probe_failed'}));raise SystemExit(1)
