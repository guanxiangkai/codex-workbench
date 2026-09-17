"""只读工作台服务；不初始化执行器、验证器、业务数据库或资源目录。"""
from __future__ import annotations
import os
import json
from pathlib import Path
import threading
import sqlite3
from urllib.parse import urlsplit

from .catalog import VERSION, validate, MODULES, DEFAULT_VIEW
from .configuration_schema import configuration_schema
from .configuration_center import configuration_entries
from .credential_details import read_details
from .model_contracts import contract_for
from .readonly_sources import NativeRead, ReadonlyCredentials, rows, timestamp
from .ui_release import UiRelease
from .asset_catalog import AssetCatalog
from .knowledge_catalog import KnowledgeCatalog
from .connection_catalog import ConnectionCatalog
from .service_directory import services
from .model_catalog import configured_models
from .other_accounts import other_accounts
from .account_usage import AccountUsage
from .view_cache import ViewCache
from .execution_pages import ExecutionPages
from .source_versions import SourceVersions

MODEL_FIELDS=['id','name','model_type','base_url','model','protocol','credential_ref','validation_status',
              'last_checked_at','last_verified_at','last_error_code','created_at','updated_at']


def _model_search_text(model):
    """供应商别名只用于模型检索，保留目录原始字段和其他模块的搜索语义。"""
    values=[model.get(key,'') for key in ('id','name','service_name','model_type','model','provider_id','provider_name')]
    normalized=' '.join(str(value) for value in values).casefold()
    return normalized+(' minmax' if 'minimax' in normalized else '')


class Workbench:
    """MCP 和 HTTP 共用同一只读白名单，错误时不返回另一主体的缓存。"""
    def __init__(self, data_dir:Path, resources_dir:Path, codex='codex', *, lease_fd=None,
                 native_reader=None, credential_catalog=None, credential_reader=None, ui_source_mode=False, asset_catalog=None, knowledge_catalog=None, connection_catalog=None, view_cache=None, source_versions=None, account_usage=None):
        self.data_dir=Path(data_dir);self.resources_dir=Path(resources_dir)
        self.db=self.data_dir/'workbench.sqlite3'
        self.ui_release=UiRelease(source_mode=ui_source_mode)
        self.native=native_reader or NativeRead(self.db,codex,lease_fd)
        self.credentials=credential_catalog or ReadonlyCredentials(self.db)
        self.credential_reader=credential_reader or read_details
        self.assets=asset_catalog or AssetCatalog(lambda:self.native.skill_sources())
        self.knowledge=knowledge_catalog or KnowledgeCatalog()
        self.connections=connection_catalog or ConnectionCatalog(codex,self.native)
        self.view_cache=view_cache or ViewCache()
        self.source_versions=source_versions or SourceVersions(self.db,getattr(self.native,'cwd',Path.cwd()),self.resources_dir)
        self.execution_pages=ExecutionPages()
        self.account_usage=account_usage or AccountUsage()
        self._closing=False;self.lock=threading.RLock();self._turn_cursors={}

    def manifest(self,page,known_revision=None):
        """读取 UI/工具发布快照，不访问账户和业务对象。"""
        return self.ui_release.manifest(page,known_revision)

    def page(self,view=DEFAULT_VIEW,native=False):
        """把公开缓存带入首屏；发布文件本身不保存任何运行数据或临时许可。"""
        if view not in {m['id'] for m in MODULES}:raise ValueError('页面不存在')
        if self._closing:raise ValueError('工作台正在关闭')
        epoch=self.source_versions.context();initial_error=None
        snapshots=self.view_cache.snapshots(self.source_versions.context)
        def default(key):return key==(view,()) or view=='knowledge' and key==('knowledge',(('query',''),('scope','global')))
        if not any(default(key) for key,_ in snapshots):
            try:
                self.sync(view)
                snapshots=self.view_cache.snapshots(self.source_versions.context)
            except (ValueError,OSError,sqlite3.Error,RuntimeError):initial_error='首屏数据暂时不可用，请重试'
        if epoch!=self.source_versions.context():raise ValueError('账户环境已变化，请重新打开工作台')
        views=[];size=0
        for (name,filters),entry in sorted(snapshots,key=lambda pair:pair[0][0]!=view):
            value={'args':{'view':name,**dict(filters)},**entry}
            length=len(json.dumps(value,ensure_ascii=False).encode())
            if size+length>700_000:continue
            views.append(value);size+=length
        bootstrap={'views':views,'context':epoch}
        if initial_error:bootstrap['error']=initial_error
        csp={'connectDomains':[],'resourceDomains':[]}
        manifest=self.ui_release.manifest('workbench')
        html=manifest['html'].replace(f"const INITIAL_PAGE='{DEFAULT_VIEW}';",f"const INITIAL_PAGE='{view}';")
        encoded=json.dumps(bootstrap,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
        html=html.replace('const WORKBENCH_BOOTSTRAP=null;', 'const WORKBENCH_BOOTSTRAP='+encoded+';')
        return {'html':html.replace('__WORKBENCH_RESOURCE_URI__',manifest['resource_uri']),'csp':csp}

    def close(self):
        """清理进程内读取状态；没有业务运行需要取消或补偿。"""
        with self.lock:
            self._closing=True;self._turn_cursors.clear();self.view_cache.clear();self.execution_pages.clear()

    def _model_catalog_revision(self):
        """模型配置文件变化时使模型及服务投影失效，不启动轮询。"""
        path=self.resources_dir/'models/catalog.json'
        try:stat=path.stat();return (stat.st_mtime_ns,stat.st_size)
        except FileNotFoundError:return None

    def _models(self):
        result=configured_models(self.resources_dir/'models/catalog.json')
        known={model['id'] for model in result}
        for model in rows(self.db,'provider_models',MODEL_FIELDS):
            if model['id'] in known:raise ValueError('模型目录与既有记录标识重复')
            ref=model.pop('credential_ref','')
            model['credential_id']=ref[6:] if isinstance(ref,str) and ref.startswith('vault:') else None
            # URL 的 query/userinfo 不属于公开目录；即使旧数据异常也不能把 Key 带入列表。
            try:
                url=urlsplit(model.get('base_url',''))
                if url.scheme not in ('http','https') or not url.hostname or url.username or url.password or url.query or url.fragment:
                    model['base_url']='';model['configuration_status']='invalid_endpoint'
            except ValueError:model['base_url']='';model['configuration_status']='invalid_endpoint'
            result.append(model)
        return result

    def _other_accounts(self, refresh=False):
        """目录默认只读；其他账户页按显式密钥绑定刷新用量，不改写配置目录。"""
        result=other_accounts(self.resources_dir/'accounts/catalog.json')
        if refresh:
            for account in result['accounts']:
                account.update(self.account_usage.refresh(account))
        return result

    def _catalog(self):
        snapshot=self.native.snapshot()
        for session in snapshot.get('sessions',[]):
            session.pop('name_token',None)
        return snapshot

    def board_state(self,session_id=None,cursor=None,section_id=None,project_id=None):
        """默认汇总全部可访问会话；筛选、分页和统计使用同一数据范围。"""
        snapshot=self._catalog();sessions=snapshot['sessions']
        filtered=[s for s in sessions if
                  (not section_id or (not s.get('section_id') if section_id=='__none__' else s.get('section_id')==section_id))
                  and (not project_id or s.get('project_id')==project_id)]
        selected=next((s for s in filtered if s['id']==session_id),None) if session_id else None
        if session_id and selected is None:raise ValueError('所选会话不存在或不属于当前筛选')
        def read(session,native_cursor):
            records,next_cursor=self.native.turns(session,native_cursor)
            with self.lock:
                for task in records:self._turn_cursors[(session['id'],task['turn_id'])]=native_cursor
                while len(self._turn_cursors)>5000:self._turn_cursors.pop(next(iter(self._turn_cursors)))
            return records,next_cursor
        if selected:
            tasks,next_cursor=read(selected,cursor)
        else:
            tasks,next_cursor=self.execution_pages.read(filtered,cursor,(section_id,project_id),
                                                        self.source_versions.context(),read)
        tasks.sort(key=lambda t:(t.get('started_at') or t.get('completed_at') or 0,t['id']),reverse=True)
        return {'read_only':True,'view':'board','sections':snapshot['sections'],'projects':snapshot['projects'],
                'sessions':sessions,'tasks':tasks,'selected_session_id':selected['id'] if selected else None,
                'next_cursor':next_cursor,'status':{'state':'ok','observed_at':timestamp(),'source':'codex',
                'scope':'selected_session' if selected else 'all_sessions','task_unit':'execution_turn',
                'truncated':snapshot.get('status',{}).get('truncated',False)}}

    def project_state(self):
        """原生项目只做关联聚合，不创建独立项目记录。"""
        catalog=self._catalog()
        return {'projects':[{**p,'session_count':sum(x.get('project_id')==p['id'] for x in catalog['sessions'])} for p in catalog['projects']],
                'sections':catalog['sections'],'sessions':catalog['sessions'],'truncated':catalog.get('status',{}).get('truncated',False)}

    def service_state(self):
        """服务和秘密共享原引用，避免重复登记和解密列表。"""
        return {'services':services(self._models(),self.credentials.list()['entries'])}

    def search(self,query,scope='global'):
        """显式提交后搜索非秘密摘要；逐源错误不会伪装成空结果。"""
        query=query.strip()
        if not query:return {'results':[],'source_errors':[]}
        result=[{'module':m['id'],'id':m['id'],'title':m['name'],'summary':m['group'],'entity_type':'module'} for m in MODULES if query.casefold() in m['name'].casefold()];errors=[]
        sources=[('projects',lambda:self.project_state()['projects'],'id','name',''),
                 ('agents',self.native.skills,'id','display_name','description'),
                 ('assets',lambda:self.assets.list()['assets'],'id','name','skill_name'),
                 ('models',self._models,'id','name','provider_name'),
                 ('config',lambda:self.state('config')['entries'],'id','label','kind'),
                 ('other_accounts',lambda:self._other_accounts()['accounts'],'id','label','provider_name'),
                 ('knowledge',lambda:self.knowledge.listing(scope,query)['knowledge'],'knowledge_key','title','summary')]
        for module,read,key,title,summary in sources:
            if module not in {m['id'] for m in MODULES}:continue
            try:
                count=0
                for item in read():
                    name=item.get(title) or item.get('name') or item[key];snippet=item.get(summary) or ''
                    searchable=_model_search_text(item) if module=='models' else (str(name)+' '+str(snippet)).casefold()
                    if module!='knowledge' and query.casefold() not in searchable:continue
                    result.append({'module':module,'id':item[key],'title':name,'summary':str(snippet)[:300],
                                   **({'scope':item.get('scope_key',scope)} if module=='knowledge' else {})});count+=1
                    if count>=10:break
            except (ValueError,OSError,sqlite3.Error):errors.append(module)
        return {'results':result,'source_errors':errors}

    def state(self,view=DEFAULT_VIEW):
        """按页惰性读取，配置页不会扫描会话，打开页面不会创建任何资源。"""
        result={'view':view,'read_only':True,'status':{'state':'ok','observed_at':timestamp()},
                'runtime':{'version':VERSION,'ui_revision':self.ui_release.revision}}
        if view=='board':return {**result,**self.board_state()}
        if view=='accounts':result['accounts']=self.native.accounts()
        elif view=='agents':result['skills']=self.native.skills()
        elif view=='models':result['models']=self._models()
        elif view=='other_accounts':result.update(self._other_accounts(refresh=True))
        elif view=='projects':result.update(self.project_state())
        elif view=='assets':result.update(self.assets.list())
        elif view=='knowledge':result.update(self.knowledge.listing())
        elif view=='services':result.update(self.service_state())
        elif view=='connections':result.update(self.connections.listing())
        elif view=='config':
            catalog=self.credentials.list();result.update(catalog);result['status']={**catalog['status'],'state':'ok','observed_at':timestamp()}
            result['configuration_schema']=configuration_schema()
            models=self._models();other=self._other_accounts()
            result['entries']=configuration_entries(catalog['entries'],models,other['accounts'])
            result['other_account_providers']=[{'id':provider['id'],'name':provider['name']} for provider in other['providers']]
        else:raise ValueError('页面不存在')
        return result

    def sync(self,view,revision=None,refresh=False,**filters):
        """只缓存展示页公开投影，查询键隔离视图、范围、会话和分页。"""
        allowed={'session_id','cursor','section_id','project_id'} if view=='board' else {'scope','query'} if view=='knowledge' else set()
        if set(filters)-allowed:raise ValueError('当前视图不接受这些筛选参数')
        if view=='knowledge':filters={'scope':filters.get('scope','global'),'query':filters.get('query','')}
        key=(view,tuple(sorted(filters.items())))
        def read():
            if view=='board':return self.board_state(**filters)
            if view=='knowledge':return self.knowledge.listing(**filters)
            return self.state(view)
        ttl={'board':10,'accounts':60,'other_accounts':60,'projects':60,'agents':120,'assets':120,'connections':120,'knowledge':30,'config':60,'services':60,'models':120}[view]
        return self.view_cache.sync(key,revision,read,self.source_versions.context,lambda:(self.source_versions.signature(view), self._model_catalog_revision() if view in ('models','services','config') else None),ttl,refresh)

    def call(self,name,arguments):
        """先验证固定工具与字段，再进入只读处理；不存在可转发的写入通道。"""
        args=validate(name,arguments)
        with self.lock:
            if self._closing:raise ValueError('工作台正在关闭')
        if name=='workbench_sync':return self.sync(**args)
        if name=='open_workbench':
            initial=self.sync(DEFAULT_VIEW)
            return {**initial['data'],'_sync':{'context':initial['context'],'revision':initial['revision']}}
        if name=='workbench_state':return self.state(args.get('view',DEFAULT_VIEW))
        if name=='board_state':return self.board_state(**args)
        if name=='module_list':return {'modules':MODULES,'read_only':True}
        if name=='project_list':return self.project_state()
        if name=='project_detail':
            data=self.project_state();item=next((x for x in data['projects'] if x['id']==args['id']),None)
            if item is None:raise ValueError('项目不存在或不可访问')
            return {'project':item,'sessions':[x for x in data['sessions'] if x.get('project_id')==item['id']]}
        if name=='asset_list':return self.assets.list()
        if name=='asset_detail':return self.assets.detail(args['id'])
        if name=='knowledge_list':return self.knowledge.listing(args.get('scope','global'),args.get('query',''))
        if name=='knowledge_detail':return self.knowledge.detail(args['scope'],args['key'])
        if name=='service_list':return self.service_state()
        if name=='service_detail':
            item=next((x for x in self.service_state()['services'] if x['id']==args['id']),None)
            if item is None:raise ValueError('服务引用不存在或已变化')
            return {'service':item}
        if name=='connection_list':return self.connections.listing()
        if name=='global_search':return self.search(args['query'],args.get('scope','global'))
        if name=='model_list':return {'models':self._models()}
        if name=='credential_list':return self.credentials.list()
        if name=='credential_details':return self.credential_reader(self.credentials,args['id'],args['public_key'],args['request_id'])
        if name=='skill_detail':
            item=next((s for s in self.native.skills() if s['id']==args['id']),None)
            if item is None:raise ValueError('技能不存在或不可访问')
            return {'skill':item}
        if name=='model_detail':
            item=next((m for m in self._models() if m['id']==args['id']),None)
            if item is None:raise ValueError('模型不存在')
            credential=None
            if item.get('credential_id'):
                credential=next((e for e in self.credentials.list()['entries'] if e['id']==item['credential_id']),None)
            return {'model':item,'credential':credential,'api_contract':contract_for(item)}
        if name=='task_detail':
            snapshot=self._catalog();session=next((s for s in snapshot['sessions'] if s['id']==args['session_id']),None)
            if session is None:raise ValueError('会话不存在或不可访问')
            with self.lock:
                cursor=self._turn_cursors.get((session['id'],args['turn_id']))
            tasks,_=self.native.turns(session,cursor)
            task=next((t for t in tasks if t['turn_id']==args['turn_id']),None)
            if task is None:raise ValueError('执行记录不在当前页，请刷新任务列表')
            return {'task':task,'session':session,'metrics':{k:task[k] for k in ('duration_ms','input_tokens','output_tokens','cached_input_tokens','reasoning_output_tokens','total_tokens')},
                    'native_url':task['native_url'],'description':task.get('description') or task['title']}
        raise ValueError('只读工具不存在')
