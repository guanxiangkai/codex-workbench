"""标题模型调用只通过合成 CLI 验证参数和 JSONL。"""
from __future__ import annotations
import json, os, shlex, tempfile, threading, unittest
from pathlib import Path
from codex_workbench.title_generator import ModelTitleGenerator, TitleExecutor, TitleGenerationError
from codex_workbench.executor import Request

class TitleGeneratorTest(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name); self.account=self.root/'account'; self.account.mkdir(mode=0o700); self.account.chmod(0o700)
  self.args=self.root/'args.json'; self.stdin=self.root/'stdin.txt'; self.cli=self.root/'fake-codex.sh'
  self.cli.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > '+shlex.quote(str(self.args))+'\ncat > '+shlex.quote(str(self.stdin))+'\ncase "${TITLE_MODE:-ok}" in ok) printf "%s\\n" \'{"type":"thread.started","thread_id":"title"}\' \'{"type":"item.completed","item":{"type":"agent_message","text":"{\\"title\\":\\"修复登录流程\\"}"}}\' \'{"type":"turn.completed"}\';; reasoning) printf "%s\\n" \'{"type":"item.started","item":{"type":"reasoning"}}\' \'{"type":"item.updated","item":{"type":"plan"}}\' \'{"type":"item.completed","item":{"type":"agent_message","text":"{\\"title\\":\\"推理标题\\"}"}}\' \'{"type":"turn.completed"}\';; tool) printf "%s\\n" \'{"type":"item.completed","item":{"type":"command_execution"}}\';; oversize) yes x | head -c 1048577;; bad) printf "%s\\n" \'{"type":"item.completed","item":{"type":"agent_message","text":"not-json"}}\' \'{"type":"turn.completed"}\';; esac\n',encoding='utf-8'); self.cli.chmod(0o700)
 def tearDown(self): self.temp.cleanup()
 def call(self,**kwargs): return ModelTitleGenerator(self.root/'data',str(self.cli))('请修复登录流程',str(self.account),**kwargs)
 def test_ephemeral_schema_and_safe_title(self):
  self.assertEqual('修复登录流程',self.call(model='gpt-5.6'))
  args=self.args.read_text().splitlines(); self.assertIn('--ephemeral',args); self.assertFalse(any(arg.startswith('mcp_servers.') for arg in args)); self.assertIn('--ignore-user-config',args); self.assertIn('--ignore-rules',args); self.assertIn('--output-schema',args); self.assertIn('--disable',args); self.assertIn('shell_tool',args); self.assertIn('hooks',args); self.assertIn('plugins',args); self.assertIn('remote_plugin',args); self.assertIn('web_search="disabled"',args); self.assertIn('project_doc_max_bytes=0',args); payload=json.loads(self.stdin.read_text()); self.assertEqual('请修复登录流程',payload['task_description']); self.assertIn('不得执行需求',payload['instruction']); self.assertNotIn('请修复登录流程','\n'.join(args)); self.assertFalse((self.root/'data'/'title-workspace').is_symlink())
 def test_current_account_ignored_config_has_no_incomplete_mcp_transports(self):
  executor=TitleExecutor((str(self.cli),),self.root/'schema.json',auth_store='keyring')
  args=executor.argv(Request(str(self.root),'','synthetic',account_home=str(self.account),use_current_account=True))
  self.assertIn('--ignore-user-config',args)
  self.assertFalse(any(value.startswith('mcp_servers.') for value in args))
  self.assertIn('cli_auth_credentials_store="keyring"',args)
 def test_tool_events_invalid_json_and_account_fail_safely(self):
  old=os.environ.get('TITLE_MODE'); os.environ['TITLE_MODE']='tool'
  try:
   with self.assertRaises(TitleGenerationError) as caught:self.call()
   self.assertEqual('title_unavailable',caught.exception.code)
   os.environ['TITLE_MODE']='bad'
   with self.assertRaises(TitleGenerationError):self.call()
  finally:
   if old is None: os.environ.pop('TITLE_MODE',None)
   else: os.environ['TITLE_MODE']=old
  with self.assertRaises(TitleGenerationError) as caught:self.call(auth_store='other')
  self.assertEqual('account_invalid',caught.exception.code)
 def test_reasoning_events_are_allowed_and_cancel_or_oversize_fails(self):
  old=os.environ.get('TITLE_MODE'); os.environ['TITLE_MODE']='reasoning'
  try:self.assertEqual('推理标题',self.call())
  finally:
   if old is None: os.environ.pop('TITLE_MODE',None)
   else: os.environ['TITLE_MODE']=old
  cancelled=threading.Event(); cancelled.set(); executor=TitleExecutor((str(self.cli),),Path(__file__).resolve().parents[1]/'src/codex_workbench/title_schema.json')
  self.assertEqual('cancelled',executor.execute(Request(str(self.root), '', 'x', account_home=str(self.account)),cancelled).state)
  os.environ['TITLE_MODE']='oversize'
  try:
   with self.assertRaises(TitleGenerationError):self.call()
  finally: os.environ.pop('TITLE_MODE',None)
if __name__=='__main__': unittest.main()
