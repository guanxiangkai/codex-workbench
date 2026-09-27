from __future__ import annotations
import json,re,os,subprocess,sys,tempfile
from datetime import date
from pathlib import Path
from typing import Any

LIMITS={'tasks':12,'title':300,'prompt':12000,'project_name':160,'section_name':80,'tags':6}
class PlanningDraft:
 def __init__(self,models_provider,invoke=None): self.models_provider=models_provider; self.invoke=invoke or self._invoke_default
 def _models(self):
  try: raw=self.models_provider() or []
  except Exception: return []
  chosen={}
  def key(m):
   nums=tuple(int(x) for x in re.findall(r"\d+",str(m.get("version") or m.get("id") or "0"))); return nums or (0,)
  for m in raw:
   if not isinstance(m,dict) or m.get("model_type")!="reasoning" or m.get("provider_id") not in {"minimax","bigmodel"} or m.get("validation_status")!="verified": continue
   p=m["provider_id"]
   if p not in chosen or key(m)>key(chosen[p]): chosen[p]=m
  return [chosen[p] for p in ("minimax","bigmodel") if p in chosen]
 def _invoke_default(self,model,text,timeout,instruction=None):
  worker=Path(__file__).with_name("capability_worker.py")
  if not worker.is_file(): raise ValueError("model_unavailable")
  credential=str(model.get("credential_id") or model.get("credential_ref") or "")
  reference=credential[6:] if credential.startswith("vault:") else credential
  config={"model_type":"reasoning","base_url":model.get("base_url",""),"model":model.get("model") or model.get("id",""),"protocol":"openai-chat","credential_ref":("vault:"+reference if reference else "")}
  system=instruction or '把用户文字按项目维度拆解为可独立执行的工作项草案，不执行任何动作。只输出JSON对象：{"project":null,"section":null,"tasks":[{"title":"简短工作项名称","prompt":"完整要求和验收标准","tags":[],"start_date":null,"due_date":null}]}。project和section可以是建议名称或null；tasks须有1到12项，title最多300字，prompt最多12000字，tags最多6个字符串。只有用户明确给出的绝对日期才填写start_date或due_date，格式必须为YYYY-MM-DD；未明确给出时填null，不推测日期、项目事实或已完成结果。不要输出解释、Markdown或其他字段。'
  request={"tool":"reasoning_chat","model_id":config["model"],"messages":[{"role":"system","content":system},{"role":"user","content":text}],"max_tokens":4096}
  with tempfile.TemporaryDirectory(prefix="planning-draft-") as d:
   root=Path(d); cp=root/'model.json'; rp=root/'request.json'; cp.write_text(json.dumps(config,ensure_ascii=False)); rp.write_text(json.dumps(request,ensure_ascii=False))
   cmd=[sys.executable,'-I',str(worker),str(cp),str(rp)]
   vault=Path.home()/'.codex/scripts/key-vault/key-vault.sh'
   if reference:
    if not vault.is_file():raise ValueError('credential_unavailable')
    cmd=[str(vault),'exec-stdin',reference,*cmd]
   env={k:v for k,v in os.environ.items() if k not in {'OPENAI_API_KEY','CODEX_API_KEY','CODEX_ACCESS_TOKEN','PYTHONPATH','PYTHONHOME'}}
   p=subprocess.run(cmd,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env,timeout=min(float(timeout),60),check=False)
   if p.returncode:raise ValueError('model_unavailable')
   try: value=json.loads(p.stdout.decode())
   except Exception:raise ValueError('response_invalid') from None
   if not isinstance(value,dict) or value.get('success') is not True or not isinstance(value.get('text'),str):raise ValueError('response_invalid')
   return {'text':value['text']}
 def _manual(self,text,warning): return {'source':'manual','warning':warning,'project':None,'section':None,'tasks':[{'title':text[:LIMITS['title']],'prompt':text[:LIMITS['prompt']],'tags':[],'start_date':None,'due_date':None,'state':'backlog'}]}
 def _draft_date(self,value):
  if value in (None,''): return None
  if not isinstance(value,str) or len(value)!=10: raise ValueError('draft_schema')
  try: parsed=date.fromisoformat(value)
  except ValueError: raise ValueError('draft_schema')
  if parsed.isoformat()!=value: raise ValueError('draft_schema')
  return value
 def _valid(self,value):
  if not isinstance(value,dict) or not isinstance(value.get('tasks'),list) or len(value['tasks'])>12:raise ValueError('draft_schema')
  out={'source':'model','warning':None,'project':None,'section':None,'tasks':[]}
  for key,lim in [('project',160),('section',80)]:
   x=value.get(key); out[key]=x[:lim] if isinstance(x,str) and x.strip() else None
  for item in value['tasks']:
   if not isinstance(item,dict):raise ValueError('draft_schema')
   title=item.get('title'); prompt=item.get('prompt',title)
   if not isinstance(title,str) or not title.strip() or not isinstance(prompt,str):raise ValueError('draft_schema')
   tags=item.get('tags',[])
   if not isinstance(tags,list) or len(tags)>6 or any(not isinstance(x,str) or len(x)>80 for x in tags):raise ValueError('draft_schema')
   start_date=self._draft_date(item.get('start_date'))
   due_date=self._draft_date(item.get('due_date'))
   if start_date and due_date and start_date>due_date: raise ValueError('draft_schema')
   out['tasks'].append({'title':title[:300],'prompt':prompt[:12000],'tags':tags[:6],
                        'start_date':start_date,'due_date':due_date,'state':'backlog'})
  if not out['tasks']:raise ValueError('draft_schema')
  return out
 def _parse(self,text):
  text=text.strip()
  # Some compatible reasoning endpoints put a completed thinking block before JSON.
  # It is never part of the editable task projection.
  text=re.sub(r'^<think>.*?</think>\s*', '', text, flags=re.S|re.I)
  if text.startswith('```'):
   text=re.sub(r'^```(?:json)?\s*|\s*```$','',text,flags=re.I|re.S).strip()
  return self._valid(json.loads(text))
 def generate(self,text):
  if not isinstance(text,str) or not text.strip():return self._manual('', 'empty_input')
  models=self._models()
  if not models or self.invoke is None:return self._manual(text,'model_unavailable')
  for model in models:
   try:
    result=self.invoke(model, text, 60)
    raw=result.get('text') if isinstance(result,dict) else result
    if isinstance(raw,str):return self._parse(raw)
   except Exception:continue
  return self._manual(text,'model_failed')
