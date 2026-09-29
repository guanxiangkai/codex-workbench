"""工作台目录与本机计划服务；仅计划功能按需初始化执行与资料存储。"""
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
from .knowledge_catalog import KnowledgeCatalog
from .connection_catalog import ConnectionCatalog
from .service_directory import services
from .model_catalog import configured_models
from .model_observations import ModelObservations
from .other_accounts import other_accounts
from .account_usage import AccountUsage
from .account_preferences import set_default_account
from .account_onboarding import AccountRegistry
from .account_service import AccountService
from .account_runtime import current_account_home
from .view_cache import ViewCache
from .source_versions import SourceVersions, stamp, tree
from .snapshot_store import SnapshotStore, SnapshotScheduler
from .account_profiles import apply_profiles

LOCAL_PLANNING_VIEWS=frozenset(('planning',))

MODEL_FIELDS=['id','name','model_type','base_url','model','protocol','credential_ref','validation_status',
              'last_checked_at','last_verified_at','last_error_code','created_at','updated_at']


def _model_search_text(model):
    """供应商别名只用于模型检索，保留目录原始字段和其他模块的搜索语义。"""
    values=[model.get(key,'') for key in ('id','name','service_name','model_type','model','provider_id','provider_name')]
    normalized=' '.join(str(value) for value in values).casefold()
    return normalized+(' minmax' if 'minimax' in normalized else '')


def _page_view(value, query='', kind='', provider='', tag='', folder='', scope='global', **_ignored):
    """在完整公开快照上筛选并计算分类；列表一次返回全部匹配项。"""
    key=next((k for k in ('accounts','skills','models','entries','knowledge','services','connections','projects') if isinstance(value.get(k),list)),None)
    if key is None:return value
    items=[x for x in value[key] if isinstance(x,dict)]
    if key=='knowledge':
        if 'scopes' in value and scope not in {x['id'] for x in value['scopes']}:raise ValueError('知识范围不存在或不可访问')
        items=[x for x in items if x.get('scope_key')==scope]
        value={**value,'selected_scope':scope,'limit_reached':scope in value.get('limited_scopes',[])}
        value.pop('limited_scopes',None)
    directory={str(x['id']):x for x in value.get('folders',[]) if isinstance(x,dict) and x.get('id')}
    def ancestors(identifier):
        seen=set()
        while identifier and identifier not in seen:
            seen.add(identifier);yield identifier
            identifier=str(directory.get(identifier,{}).get('parent_id') or '')
    def item_kind(x):
        return str((x.get('model_type') if key=='models' else x.get('service_type') if key=='entries' else None) or x.get('kind') or '')
    def item_providers(x):
        p=x.get('account_provider_ids',[]) if key=='entries' else x.get('provider_id') or x.get('provider') or []
        return [str(y) for y in (p if isinstance(p,list) else [p]) if y]
    def search_text(x):
        parts=[str(v) for v in x.values() if isinstance(v,(str,int,float,bool))]
        parts.extend(str(v) for v in x.get('tags',[]) or [])
        parts.extend(str(directory[f].get('name','')) for f in ancestors(str(x.get('folder_id') or '')) if f in directory)
        text=' '.join(parts).casefold()
        return text+(' minmax' if key=='models' and 'minimax' in text else '')
    q=str(query or '').casefold()
    selected=[x for x in items if (not q or q in search_text(x)) and (not kind or kind==item_kind(x)) and (not provider or provider in item_providers(x)) and (not tag or tag in (x.get('tags') or [])) and (not folder or folder in set(ancestors(str(x.get('folder_id') or ''))))]
    # 没有时间戳时保持源顺序，有时间戳时用标识消除并列顺序的不确定性。
    if any(x.get('updated_at') or x.get('created_at') for x in selected):
        selected.sort(key=lambda x:(str(x.get('updated_at') or x.get('created_at') or ''),str(x.get('id') or x.get('knowledge_key') or '')),reverse=True)
    if key=='accounts':
        selected.sort(key=lambda x:(not bool(x.get('is_current')),not bool(x.get('is_default'))))
    # 平台汇总只保留元数据，避免通过嵌套账户泄露额外列表。
    if key=='accounts' and isinstance(value.get('providers'),list):
        value={**value,'providers':[{k:v for k,v in p.items() if k!='accounts'} for p in value['providers']]}
    providers={};kinds={};tags={};folders={}
    for x in items:
        for p in item_providers(x):
            entry=providers.setdefault(p,{'id':p,'name':x.get('provider_name') or p,'count':0});entry['count']+=1
        k=item_kind(x)
        if k:
            entry=kinds.setdefault(k,{'id':k,'name':x.get('service_type_label') or k,'count':0});entry['count']+=1
        for t in x.get('tags') or []:tags[str(t)]=tags.get(str(t),0)+1
        for f in ancestors(str(x.get('folder_id') or '')):folders[f]=folders.get(f,0)+1
    return {**value,key:selected,
            'facets':{'providers':[providers[k] for k in sorted(providers)],'kinds':[kinds[k] for k in sorted(kinds)],'tags':[{'id':k,'name':k,'count':v} for k,v in sorted(tags.items())],'folder_counts':folders}}


class Workbench:
    """MCP 和 HTTP 共用工具白名单，错误时不返回另一主体的缓存。"""
    def __init__(self, data_dir:Path, resources_dir:Path, codex='codex', *, lease_fd=None,
                 native_reader=None, credential_catalog=None, credential_reader=None, ui_source_mode=False, knowledge_catalog=None, connection_catalog=None, view_cache=None, source_versions=None, account_usage=None, snapshot_store=None, reset_analyzer=None, output_root=None):
        self.data_dir=Path(data_dir);self.resources_dir=Path(resources_dir)
        self.codex=codex;self.lease_fd=lease_fd
        self.output_root=Path(output_root) if output_root else Path.home()/'Documents'/'输出'
        # 登录服务只在用户发起账户操作时构造。这样打开只读页面不会创建或迁移业务库。
        self._account_service=None
        self.db=self.data_dir/'workbench.sqlite3'
        self.ui_release=UiRelease(source_mode=ui_source_mode)
        self.native=native_reader or NativeRead(self.db,codex,lease_fd)
        self.credentials=credential_catalog or ReadonlyCredentials(self.db)
        self.credential_reader=credential_reader or read_details
        self.knowledge=knowledge_catalog or KnowledgeCatalog()
        self.connections=connection_catalog or ConnectionCatalog(codex,self.native)
        self.view_cache=view_cache or ViewCache()
        self.source_versions=source_versions or SourceVersions(self.db,getattr(self.native,'cwd',Path.cwd()),self.resources_dir)
        self.account_usage=account_usage or AccountUsage(data_dir=self.data_dir)
        # 仅注入已登记的 GLM/MiniMax 受控分析器；缺省不发起外部抓取。
        self.reset_analyzer=reset_analyzer
        self.model_observations=ModelObservations(self.data_dir)
        self.snapshots=snapshot_store or SnapshotStore(self.data_dir)
        self.snapshot_scheduler=None
        self.snapshot_errors={}
        self._scheduled_accounts = None
        self._planning=None
        self._closing=False;self.lock=threading.RLock()

    def _planning_service(self):
        """计划与资料仅在首次使用时打开本机业务库。"""
        with self.lock:
            if self._planning is None:
                from .planning import Planning
                from .executor import CodexExecutor
                from .planning_models import PlanningModels
                from .planning_context import PlanningContext
                router=PlanningModels(self._models, self._other_accounts)
                context=PlanningContext(cache_path=self.data_dir/'planning-context-cache.sqlite3')
                def embed(texts):
                    response=router.call('embedding',{'input':texts},external_allowed=True,purpose='planning_context')
                    vectors=response.get('vectors')
                    if not isinstance(vectors,list) or not vectors or not vectors[0]:
                        raise ValueError('embedding_response_invalid')
                    dimensions=len(vectors[0])
                    if any(len(vector)!=dimensions for vector in vectors):raise ValueError('embedding_dimension_mismatch')
                    # Establish a binding only from the registered adapter's actual response.
                    binding={'model_id':response['model_id'],'dimensions':dimensions}
                    if context.embedding_binding and context.embedding_binding!=binding:
                        raise ValueError('embedding_binding_changed')
                    context.embedding_binding=binding
                    return {**response,'dimensions':dimensions}
                def rerank(query,documents):
                    response=router.call('rerank',{'query':query,'documents':[item['content'] for item in documents]},external_allowed=True,purpose='planning_context')
                    binding={'model_id':response['model_id']}
                    if context.rerank_binding and context.rerank_binding!=binding:raise ValueError('rerank_binding_changed')
                    context.rerank_binding=binding
                    return response
                context.embedding=embed;context.rerank=rerank
                self._planning=Planning(self.data_dir,executor=CodexExecutor((self.codex,),lease_fd=self.lease_fd),
                    knowledge_reader=self.knowledge.detail,output_root=self.output_root,context_engine=context,model_router=router)
            return self._planning

    def _planning_state(self,view):
        data=self._planning_service().snapshot(view=view)
        if not isinstance(data,dict):raise ValueError('本机计划状态不可用')
        return {'view':view,'read_only':False,'status':{'state':'ok','observed_at':timestamp()},**data}

    def manifest(self,page,known_revision=None):
        """读取 UI/工具发布快照，不访问账户和业务对象。"""
        return self.ui_release.manifest(page,known_revision)

    def page(self,view=DEFAULT_VIEW,native=False):
        """把公开缓存带入首屏；发布文件本身不保存任何运行数据或临时许可。"""
        if view not in {m['id'] for m in MODULES}:raise ValueError('页面不存在')
        if self._closing:raise ValueError('工作台正在关闭')
        local_view=view in LOCAL_PLANNING_VIEWS
        epoch='local-planning' if local_view else self.source_versions.context()
        # 冷首屏立即返回页面，由现有异步加载读取账户和目录。
        if not local_view and epoch!=self.source_versions.context():raise ValueError('账户环境已变化，请重新打开工作台')
        views=[];size=0
        snapshot=(self._planning_state(view) if self._planning is not None else None) if local_view else self._snapshot_state(view)
        if snapshot is not None and (local_view or snapshot['status']['snapshot']['state'] != 'pending'):
            value={'args':{'view':view},'revision':self._snapshot_revision(snapshot),'data':snapshot}
            length=len(json.dumps(value,ensure_ascii=False).encode())
            if length <= 700_000:
                views.append(value);size+=length
        bootstrap={'views':views,'context':epoch}
        csp={'connectDomains':[],'resourceDomains':[]}
        manifest=self.ui_release.manifest('workbench')
        html=manifest['html'].replace(f"const INITIAL_PAGE='{DEFAULT_VIEW}';",f"const INITIAL_PAGE='{view}';")
        encoded=json.dumps(bootstrap,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
        html=html.replace('const WORKBENCH_BOOTSTRAP=null;', 'const WORKBENCH_BOOTSTRAP='+encoded+';')
        return {'html':html.replace('__WORKBENCH_RESOURCE_URI__',manifest['resource_uri']),'csp':csp}

    def close(self):
        """关闭后台采集、账户操作和计划执行器。"""
        with self.lock:
            self._closing=True;self.view_cache.clear();planning=self._planning
        if self.snapshot_scheduler is not None:self.snapshot_scheduler.close()
        if self._account_service is not None:self._account_service.close()
        if planning is not None:planning.close()

    def start_snapshots(self):
        """仅由 runtime 启动后台采集，测试构造服务不创建线程。"""
        with self.lock:
            if self.snapshot_scheduler is None:
                packaged=Path(__file__).with_name('refresh-schedule.json')
                configured=self.resources_dir/'refresh-schedule.json'
                self.snapshot_scheduler=SnapshotScheduler(self, configured if configured.exists() else packaged)
            scheduler=self.snapshot_scheduler
        scheduler.start()

    def refresh_provider_usage(self, provider):
        """Scheduler callback: refresh each registered account for one provider once.

        The page projection only reads AccountUsage's cache afterwards, so this
        path is the sole periodic network entry and does not trigger a second
        refresh while collecting ``other_accounts``.
        """
        if provider not in {'bigmodel', 'minimax', 'codex', 'manual_reset'} or self._closing:
            return {'provider': provider, 'refreshed': 0}
        if provider == 'manual_reset':
            # Manual reset is an explicit public-source/model job. It must use
            # the cached account projection and never call the official quota RPC.
            cached_accounts = self.native.accounts(cached_only=True)
            if self.reset_analyzer is None:
                raise ValueError('manual_reset_analysis_unavailable')
            seed = cached_accounts[0] if cached_accounts else {}
            analysis = self.reset_analyzer.force_refresh(seed)
            self._scheduled_accounts = [dict(account, reset_analysis=analysis) for account in cached_accounts]
            published = self.collect_snapshot('accounts')
            state = 'error' if analysis.get('error') and not analysis.get('last_success_at') else ('stale' if analysis.get('error') else 'ready')
            if analysis.get('error') or not published:
                raise ValueError('manual_reset_analysis_unavailable')
            return {'provider': provider, 'state': state, 'refreshed': len(cached_accounts), 'error': analysis.get('error')}
        if provider == 'codex':
            # NativeRead is the only owner of the official Codex RPC and its
            # account snapshot persistence. Refresh once, then publish the
            # resulting accounts view for the scheduled page/API readers.
            accounts = self.native.accounts()
            self._scheduled_accounts = accounts
            published = self.collect_snapshot('accounts')
            if not published or not accounts or any(a.get('usage_refresh', {}).get('state') != 'ready' for a in accounts):
                raise ValueError('provider_usage_unavailable')
            return {'provider': provider, 'refreshed': len(accounts)}
        directory = other_accounts(self.resources_dir/'accounts/catalog.json')
        refreshed = 0
        failures = 0
        for account in directory.get('accounts', []):
            if account.get('provider_id') != provider:
                continue
            usage = self.account_usage.refresh(account)
            failures += int(usage.get('usage_refresh', {}).get('state') == 'failed')
            refreshed += 1
        if refreshed:
            self.collect_snapshot('other_accounts')
        if failures:raise ValueError('provider_usage_unavailable')
        if not refreshed:raise ValueError('provider_account_unavailable')
        return {'provider': provider, 'refreshed': refreshed}

    def has_snapshot(self,view):
        if view in LOCAL_PLANNING_VIEWS:return False
        return self.snapshots.get(self.source_versions.context(),view) is not None

    def _snapshot_revision(self,value):
        from .view_cache import encoded, stable
        import hashlib
        return hashlib.sha256(encoded(stable(value))).hexdigest()

    def _snapshot_state(self,view,context=None):
        context=context if context is not None else self.source_versions.context()
        entry=self.snapshots.get(context,view)
        if view=='accounts' and isinstance(self.native,NativeRead):
            cached=self.native.accounts(cached_only=True)
            if any(a.get('email') for a in cached) and (entry is None or not any(a.get('email') for a in entry.get('data',{}).get('accounts',[]))):
                value=apply_profiles({'view':view,'read_only':True,'accounts':cached,'status':{'state':'ok'}},view,self.data_dir/'account-profiles.json')
                entry={'data':value,'updated_at':max((a.get('observed_at') or '' for a in cached),default='') or None}
        refreshing=False
        if self.snapshot_scheduler is not None:
            with self.snapshot_scheduler.lock:
                refreshing=(context,view) in self.snapshot_scheduler.requested or (context,view) in self.snapshot_scheduler.running
        if entry is None:
            if self.snapshot_scheduler is not None:self.snapshot_scheduler.touch(view)
            error=self.snapshot_errors.get((context,view))
            snapshot={'state':'error' if error else 'pending','updated_at':None,'refreshing':True}
            if error:snapshot['error']=error
            return {'view':view,'read_only':True,'status':{'state':'ok','observed_at':timestamp(),
                    'snapshot':snapshot}}
        value=entry['data']
        error=self.snapshot_errors.get((context,view))
        account_failure=view=='accounts' and any(a.get('usage_refresh',{}).get('state') in ('failed','cached') for a in value.get('accounts',[]))
        state='stale' if error or account_failure else 'ready'
        snapshot={'state':state,'updated_at':entry.get('updated_at'),'refreshing':refreshing}
        if error:snapshot['error']=error
        status={**value.get('status',{}),'snapshot':snapshot}
        return {**value,'status':status}

    @staticmethod
    def _account_identity(view, account):
        if view=='accounts':
            # 未提供邮箱时不能把旧主体资料迁移给可能已切换的账户。
            return ('codex',account.get('id'),account.get('email')) if account.get('email') else None
        return ('provider',account.get('provider_id'),account.get('id'),account.get('usage_credential_id'))

    def _merge_account_fields(self, view, value, previous):
        """来源缺字段或用量失败时保留已确认资料，绝不跨账户合并。"""
        if view not in ('accounts','other_accounts') or not isinstance(previous,dict):
            return value
        old={self._account_identity(view,item):item for item in previous.get('accounts',[]) if isinstance(item,dict) and self._account_identity(view,item)}
        preserved=('display_name','avatar','avatar_url','image','avatar_data_uri','profile_observed_at',
                   'remaining_percent','reset_cards','usage','usage_windows','resets_at','expires_at',
                   'reset_analysis')
        merged=[]
        for item in value.get('accounts',[]):
            if not isinstance(item,dict):
                merged.append(item);continue
            prior=old.get(self._account_identity(view,item))
            if not prior:
                merged.append(item);continue
            retained={field:prior[field] for field in preserved if field in prior and item.get(field) in (None,'',[],{})}
            # “用户名未提供”不是官方资料，短暂的官方来源缺失不能抹掉已确认姓名。
            if (item.get('name_source') == 'unavailable' or item.get('name') in (None,'')) and prior.get('name_source') in ('official','official_page'):
                retained.update(name=prior.get('name'),name_source=prior.get('name_source'))
            merged.append({**item,**retained})
        return {**value,'accounts':merged}

    def collect_snapshot(self,view):
        """后台唯一来源读取入口；失败绝不覆盖最后成功快照。"""
        if self._closing:return False
        context=self.source_versions.context()
        try:
            value=self._collect_state(view)
            if self.source_versions.context()!=context:return False
            existing=self.snapshots.get(context,view)
            value=self._merge_account_fields(view,value,existing.get('data') if existing else None)
            value={**value,'status':{k:v for k,v in value.get('status',{}).items() if k!='snapshot'}}
            self.snapshots.put(context,view,value)
            self.snapshot_errors.pop((context,view),None)
            return True
        except (ValueError,OSError,sqlite3.Error):
            self.snapshot_errors[(context,view)]='来源暂时不可用'
            return False

    def _model_catalog_revision(self):
        """模型目录或任一分片变化时使模型及服务投影失效，不启动轮询。"""
        return tuple(tree(self.resources_dir/'models', 1, 1100))+tuple(
            stamp(path) for path in (self.model_observations.path,Path(str(self.model_observations.path)+'-journal')))

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
        return self.model_observations.project(result)

    def _other_accounts(self, refresh=False):
        """目录默认只读；其他账户页按显式密钥绑定刷新用量，不改写配置目录。"""
        result=other_accounts(self.resources_dir/'accounts/catalog.json')
        if refresh:
            for account in result['accounts']:
                account.update(self.account_usage.refresh(account))
        else:
            for account in result['accounts']:
                account.update(self.account_usage.cached(account))
        return result

    def _catalog(self):
        snapshot=self.native.snapshot()
        for session in snapshot.get('sessions',[]):
            session.pop('name_token',None)
        return snapshot

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
        sources=[('agents',lambda:self._snapshot_state('agents').get('skills',[]),'id','display_name','description'),
                 ('models',lambda:self._snapshot_state('models').get('models',[]),'id','name','provider_name'),
                 ('config',lambda:self._snapshot_state('config').get('entries',[]),'id','label','kind'),
                 ('other_accounts',lambda:self._snapshot_state('other_accounts').get('accounts',[]),'id','label','provider_name'),
                 ('knowledge',lambda:self.state('knowledge',scope=scope).get('knowledge',[]),'knowledge_key','title','summary')]
        for module,read,key,title,summary in sources:
            if module not in {m['id'] for m in MODULES}:continue
            try:
                count=0
                for item in read():
                    name=item.get(title) or item.get('name') or item[key];snippet=item.get(summary) or ''
                    searchable=_model_search_text(item) if module=='models' else (str(name)+' '+str(snippet)).casefold()
                    if query.casefold() not in searchable:continue
                    result.append({'module':module,'id':item[key],'title':name,'summary':str(snippet)[:300],
                                   **({'scope':item.get('scope_key',scope)} if module=='knowledge' else {})});count+=1
                    if count>=10:break
            except (ValueError,OSError,sqlite3.Error):errors.append(module)
        return {'results':result,'source_errors':errors}

    def _collect_state(self,view):
        """仅供后台采集调用；页面请求不得经过此方法。"""
        if view in LOCAL_PLANNING_VIEWS:raise ValueError('本机计划视图不参与快照采集')
        result={'view':view,'read_only':True,'status':{'state':'ok','observed_at':timestamp()},
                'runtime':{'version':VERSION,'ui_revision':self.ui_release.revision}}
        if view=='accounts':
            if self._scheduled_accounts is not None:
                result['accounts'] = self._scheduled_accounts
            else:
                cached_accounts = self.native.accounts(cached_only=True)
                result['accounts'] = cached_accounts
                previous = self.snapshots.get(self.source_versions.context(), 'accounts')
                known = {self._account_identity('accounts', a): a for a in (previous or {}).get('data', {}).get('accounts', [])}
                for account in result['accounts']:
                    identity = self._account_identity('accounts', account)
                    prior = known.get(identity) if identity else None
                    if prior and account.get('usage_refresh', {}).get('state') == 'cached':
                        # A cache projection is not a new authentication/usage attempt.
                        # Preserve the last result only for the same bound identity.
                        account['login_status'] = prior.get('login_status', account.get('login_status'))
                        account['usage_refresh'] = prior.get('usage_refresh', account['usage_refresh'])
            self._scheduled_accounts = None
            if self.reset_analyzer is not None:
                analysis = self.reset_analyzer.cached()
                for account in result['accounts']:
                    if isinstance(analysis, dict) and analysis.get('scope') == 'official_manual_reset':
                        account['reset_analysis'] = dict(analysis)
                    else:
                        account.pop('reset_analysis', None)
        elif view=='agents':result['skills']=self.native.skills()
        elif view=='models':result['models']=self._models()
        elif view=='other_accounts':result.update(self._other_accounts(refresh=False))
        elif view=='knowledge':result.update(self.knowledge.snapshot())
        elif view=='config':
            catalog=self.credentials.list();result.update(catalog);result['status']={**catalog['status'],'state':'ok','observed_at':timestamp()}
            result['configuration_schema']=configuration_schema()
            models=self._models();other=self._other_accounts(refresh=False)
            result['entries']=configuration_entries(catalog['entries'],models,other['accounts'])
            result['other_account_providers']=[{'id':provider['id'],'name':provider['name']} for provider in other['providers']]
        else:raise ValueError('页面不存在')
        # 官网首次确认的公开资料仅补齐当前来源未提供的字段，身份键由适配器严格校验。
        return apply_profiles(result,view,self.data_dir/'account-profiles.json')

    def _accounts(self):
        """按需启用新增账户登记与官方登录，不复用 Store 的全库迁移。"""
        with self.lock:
            if self._account_service is None:
                registry=AccountRegistry(self.db)
                self._account_service=AccountService(registry,self.data_dir/'accounts',self.codex,
                                                     Path(current_account_home()),self.lease_fd)
            return self._account_service

    def state(self,view=DEFAULT_VIEW,*,snapshot_context=None,**filters):
        """页面状态只投影本机快照；首次读取排入后台采集。"""
        if view not in {m['id'] for m in MODULES}:raise ValueError('页面不存在')
        if view in LOCAL_PLANNING_VIEWS:
            if filters:raise ValueError('当前视图不接受这些筛选参数')
            return self._planning_state(view)
        result=self._snapshot_state(view,snapshot_context)
        return _page_view(result,**filters)

    def sync(self,view,revision=None,refresh=False,**filters):
        """以快照差异返回筛选后的完整列表；刷新仅通知后台线程。"""
        allowed={'query','kind','provider','tag','folder'}|({'scope'} if view=='knowledge' else set())
        if set(filters)-allowed:raise ValueError('当前视图不接受这些筛选参数')
        if view not in {m['id'] for m in MODULES}:raise ValueError('页面不存在')
        if view in LOCAL_PLANNING_VIEWS:
            if filters:raise ValueError('当前视图不接受这些筛选参数')
            value=self.state(view)
            current=self._snapshot_revision(value)
            result={'context':'local-planning','revision':current,'base_revision':revision,'unchanged':revision==current,'checked_at_age_seconds':0}
            if not result['unchanged']:result.update(reset=True,data=value)
            return result
        context=self.source_versions.context()
        requested_provider = filters.get('provider') if refresh else None
        if requested_provider is not None:
            expected = 'accounts' if requested_provider in {'codex', 'manual_reset'} else 'other_accounts'
            if requested_provider not in {'codex', 'bigmodel', 'minimax', 'manual_reset'} or view != expected:
                raise ValueError('额度刷新视图与供应商不匹配')
            if self.snapshot_scheduler is not None:self.snapshot_scheduler.request_provider(requested_provider)
        if self.snapshot_scheduler is not None:self.snapshot_scheduler.touch(view,refresh=refresh and requested_provider is None)
        value=self.state(view,snapshot_context=context,**filters)
        if self.source_versions.context()!=context:raise ValueError('账户环境已变化，请重新读取')
        current=self._snapshot_revision(value)
        result={'context':context,'revision':current,'base_revision':revision,'unchanged':revision==current,'checked_at_age_seconds':0}
        if requested_provider:result['provider_refresh']={'provider':requested_provider,'queued':True}
        if self.snapshot_scheduler is not None:
            result['provider_tasks'] = self.snapshot_scheduler.provider_tasks()
        if not result['unchanged']:
            result.update(reset=True,data=value)
        return result

    def call(self,name,arguments):
        """先验证固定工具与字段；仅账户新增、官方登录、核验和默认选择允许写入。"""
        args=validate(name,arguments)
        with self.lock:
            if self._closing:raise ValueError('工作台正在关闭')
        if name=='account_default':
            result=set_default_account(self.db,self.native,args['id'],args['expected_default_id'])
            if self.snapshot_scheduler is not None:self.snapshot_scheduler.touch('accounts',refresh=True)
            return result
        if name=='account_create':
            return {'account':self._accounts().create(args['name'])}
        if name=='account_login':
            return {'login':self._accounts().login(args['id'])}
        if name=='account_status':
            result=self._accounts().status(args['id'],refresh=True)
            if result.get('login',{}).get('status')=='ready' and self.snapshot_scheduler is not None:
                self.snapshot_scheduler.touch('accounts',refresh=True)
            return result
        if name=='planning_create':return {'item':self._planning_service().create(**args)}
        if name=='planning_update':return {'item':self._planning_service().update(**args)}
        if name=='planning_start':return {'item':self._planning_service().start(**args)}
        if name=='planning_stop':return {'item':self._planning_service().stop(**args)}
        if name=='planning_archive':return {'item':self._planning_service().archive(**args)}
        if name=='planning_delete':return {'item':self._planning_service().remove(**args)}
        if name=='planning_followup':return {'item':self._planning_service().followup(**args)}
        if name=='planning_intake':return {'draft':self._planning_service().intake(models_provider=self._models, **args)}
        if name=='planning_intake_save':return self._planning_service().intake_save(**args)
        if name=='planning_knowledge_link':return {'item':self._planning_service().link_knowledge(**args)}
        if name=='planning_export':
            plan=self._planning_service()
            result=plan.export(task_id=args['task_id'])
            if args.get('include_knowledge',True):
                from .planning_notes import PlanningNotes
                records=[self.knowledge.detail(ref['scope'],ref['key'])['knowledge'] for ref in plan.detail(task_id=args['task_id']).get('knowledge_refs',[])]
                if records:
                    result['knowledge']=PlanningNotes(self.output_root,plan.db,plan.lock).export_knowledge(records)
            return {'item':result}
        if name=='planning_detail':return {'item':self._planning_service().detail(**args)}
        if name=='planning_delivery':return {'item':self._planning_service().delivery_action(**args)}
        if name=='planning_context':return {'item':self._planning_service().context(**args)}
        if name=='planning_knowledge_candidates':return {'items':self._planning_service().knowledge_candidates(**args)}
        if name in {'planning_output_filter','planning_output_source'}:
            from .planning_output import PlanningOutput
            output=PlanningOutput(self.data_dir/'planning-output-cache')
            return {'item':output.filter(**args) if name=='planning_output_filter' else output.read(**args)}
        if name=='planning_draft':
            from .planning_draft import PlanningDraft
            return {'draft':PlanningDraft(self._models).generate(args['text'])}
        if name=='library_upload':return {'item':self._planning_service().upload(**args)}
        if name=='library_upload_begin':return {'item':self._planning_service().upload_begin(**args)}
        if name=='library_upload_chunk':return {'item':self._planning_service().upload_chunk(**args)}
        if name=='library_upload_commit':return {'item':self._planning_service().upload_commit(**args)}
        if name=='library_link':return {'item':self._planning_service().link(**args)}
        if name=='library_list':return {'item':self._planning_service().library_list(**args)}
        if name=='library_content':return {'item':self._planning_service().asset_content(**args)}
        if name=='workbench_sync':return self.sync(**args)
        if name=='open_workbench':
            # 冷入口只打开页面；计划存储由页面的异步同步初始化。
            if DEFAULT_VIEW in LOCAL_PLANNING_VIEWS and self._planning is None:
                return {'view':DEFAULT_VIEW,'_bootstrap_pending':True}
            initial=self.sync(DEFAULT_VIEW)
            return {**initial['data'],'_sync':{'context':initial['context'],'revision':initial['revision']}}
        if name=='workbench_state':return self.state(args.get('view',DEFAULT_VIEW),**{k:v for k,v in args.items() if k!='view'})
        if name=='module_list':return {'modules':MODULES,'read_only':False}
        if name=='project_list':return self.project_state()
        if name=='project_detail':
            data=self.project_state();item=next((x for x in data['projects'] if x['id']==args['id']),None)
            if item is None:raise ValueError('项目不存在或不可访问')
            return {'project':item,'sessions':[x for x in data['sessions'] if x.get('project_id')==item['id']]}
        if name=='knowledge_list':return self.state('knowledge',scope=args.get('scope','global'),query=args.get('query',''))
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
        raise ValueError('只读工具不存在')
