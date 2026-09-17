"""已审核个人知识的只读视图；Scope 和读取语义由既有知识工具维护。"""
from pathlib import Path
import json
import subprocess


class KnowledgeCatalog:
    """仅开放已存在 Scope 的目录、搜索和详情，不创建或审核知识。"""
    def __init__(self, command=None, run=None):
        self.command=Path(command or Path.home()/'.codex/skills/manage-personal-knowledge/scripts/personal-knowledge')
        self.run=run or subprocess.run

    def _read(self,args,query=None):
        if args[0] not in ('scopes','catalog','search','lookup'):raise ValueError('不支持的知识读取')
        try:
            r=self.run([str(self.command),*args],input=query or '',text=True,capture_output=True,timeout=20)
            if r.returncode or len(r.stdout.encode())>2*1024*1024:raise ValueError('知识来源暂时不可用')
            values=[json.loads(line) for line in r.stdout.splitlines() if line.strip()]
            if any(not isinstance(x,dict) for x in values):raise ValueError('知识目录格式无效')
            return values
        except (OSError,subprocess.TimeoutExpired,json.JSONDecodeError):
            raise ValueError('知识来源暂时不可用') from None

    def scopes(self):
        """返回既有活动 Scope 的非秘密名称，不按项目名创建关系。"""
        return [{'id':x['scope_key'],'name':x.get('display_name') or x['scope_key'],'kind':x.get('kind')}
                for x in self._read(['scopes']) if x.get('is_active') and isinstance(x.get('scope_key'),str)][:500]

    def listing(self,scope='global',query=''):
        """只搜索用户选定的 Scope；目录和摘要不含知识正文。"""
        scopes=self.scopes()
        if scope not in {x['id'] for x in scopes}:raise ValueError('知识范围不存在或不可访问')
        if not isinstance(query,str) or len(query)>300:raise ValueError('知识查询过长')
        values=self._read(['search',scope,'50'],query) if query.strip() else self._read(['catalog',scope,'100'])
        keys=('knowledge_key','scope_key','title','summary','tags','scope_priority')
        return {'scopes':scopes,'selected_scope':scope,'knowledge':[{k:x[k] for k in keys if k in x} for x in values],
                'limit_reached':len(values)>=(50 if query.strip() else 100)}

    def detail(self,scope,key):
        """返回指定已审核条目；同名键仍按 Scope 解析。"""
        if scope not in {x['id'] for x in self.scopes()}:raise ValueError('知识范围不存在或不可访问')
        if not isinstance(key,str) or not key or len(key)>200:raise ValueError('知识标识无效')
        values=self._read(['lookup',scope,key])
        if len(values)!=1:raise ValueError('知识条目不存在或已失效')
        record=values[0];revision=record.get('revision')
        if not isinstance(revision,dict) or not isinstance(revision.get('title'),str) or not isinstance(revision.get('content'),str):
            raise ValueError('知识版本格式无效，未展示不完整内容')
        source=record.get('source') or {};source_revision=record.get('source_revision') or {}
        return {'knowledge':{'knowledge_key':record.get('knowledge_key'),'scope_key':record.get('scope_key'),
                'title':revision['title'],'content':revision['content'],'summary':revision.get('summary'),'tags':revision.get('tags',[]),
                'updated_at':revision.get('approved_at') or revision.get('created_at'),'source_native_id':source.get('native_id'),
                'source_revision_hash':source_revision.get('revision_hash'),'sensitivity':source.get('sensitivity')}}
