"""工作台的只读数据适配器；不初始化、迁移或写入任何业务数据库。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
import hashlib
import json
import re
import sqlite3
import threading
from urllib.parse import quote, urlsplit

from .account_runtime import current_account_home, validate_account_home
from .accounts import normalize_account
from .codex_rpc import AccountRpc, RpcError
from .credentials import CredentialCatalog
from .native_catalog import NativeCatalog
from .titles import safe_display_title
from .turn_metadata import TurnMetadata
from .execution_titles import execution_title


def timestamp():
    """返回 UTC 观察时间，和业务更新时间保持区分。"""
    return datetime.now(UTC).isoformat()


@contextmanager
def connection(path):
    """仅打开已存在的数据库；禁止 SQL 写入及隐式建库。"""
    db = sqlite3.connect('file:' + quote(str(Path(path).resolve()), safe='/') + '?mode=ro', uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    db.execute('PRAGMA trusted_schema=OFF')
    try:
        yield db
    finally:
        db.close()


def rows(path, table, fields):
    """从代码声明的白名单列读取配置；缺失表示未登记，损坏明确失败。"""
    if not Path(path).is_file():
        return []
    if not re.fullmatch(r'[a-z_]+', table) or any(not re.fullmatch(r'[a-z_]+', x) for x in fields):
        raise ValueError('无效的配置查询')
    with connection(path) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            return []
        available = {r[1] for r in db.execute(f'PRAGMA table_info({table})')}
        selected = [f for f in fields if f in available]
        if not selected:
            return []
        return [dict(r) for r in db.execute(f"SELECT {','.join(selected)} FROM {table} LIMIT 5000")]


class ReadonlyCredentials(CredentialCatalog):
    """复用目录解析逻辑，连接强制只读；不调用父类数据库初始化。"""
    def __init__(self, path, vault_command=None):
        self.db_path = Path(path)
        self.command = vault_command or Path.home() / '.codex/scripts/key-vault/key-vault.sh'
        self.poll_seconds = 0  # 仅用户读取时刷新；不启动轮询器。
        self._cache = None
        self._lock = threading.RLock()

    @contextmanager
    def _connection(self):
        with connection(self.db_path) as db:
            yield db

    def _folders(self):
        return rows(self.db_path, 'credential_folders', ['id','name','parent_id','color'])

    def _entry_views(self, snapshot):
        """保留和当前密文版本匹配的字段索引，绝不按标题猜测类型。"""
        saved = {r['entry_id']: r for r in rows(self.db_path, 'credential_organizer',
                 ['entry_id','label','folder_id','tags_json','color','organization_source'])}
        structures = {r['entry_id']: r for r in rows(self.db_path, 'credential_structure',
                      ['entry_id','revision','generation','source','structure_json'])}
        result = []
        for entry in snapshot['entries']:
            key = entry['entry_id']; meta = saved.get(key, {})
            value = {'id':key, 'reference':'vault:'+key, 'label':meta.get('label') or key,
                     'folder_id':meta.get('folder_id'), 'color':meta.get('color'),
                     'tags':self._decode_list(meta.get('tags_json','[]'),12,'标签'),
                     'organization_source':meta.get('organization_source') or 'unorganized'}
            value.update(self._structure_view(structures.get(key),entry.get('revision')))
            value.update({k:v for k,v in entry.items() if k!='entry_id'})
            result.append(value)
        return result


class ReadRpc(AccountRpc):
    """受限官方接口：无登录、执行、配置写入或通用方法转发。"""
    METHODS = CURRENT_METHODS = frozenset({'account/read','account/rateLimits/read','skills/list',
                                         'thread/read','thread/turns/list','thread/items/list','plugin/list'})


def number(value):
    """只接受非负有限数字；未知不能变为零。"""
    import math
    return value if type(value) in (int,float) and math.isfinite(value) and value >= 0 else None


class NativeRead:
    """原生身份、技能、会话和执行轮次的只读投影。"""
    def __init__(self, path, codex, lease_fd=None, rpc_factory=ReadRpc, catalog=None, cwd=None):
        self.path, self.codex, self.lease_fd = Path(path), codex, lease_fd
        self.rpc_factory = rpc_factory
        self.catalog = catalog or NativeCatalog(Path(current_account_home()), include_archived=True)
        self.cwd = str(Path(cwd or Path.cwd()).resolve())
        self.turn_metadata=TurnMetadata(current_account_home())

    def rpc(self, home=None, current=True):
        """固定账户引用由本机登记决定，不接受浏览器提供的目录。"""
        return self.rpc_factory(self.codex, home or current_account_home(), self.lease_fd,
                                current=current, timeout=15)

    def identity(self, rpc):
        """官方返回的账户身份；不读取认证文件。"""
        value = rpc.request('account/read', {'refreshToken':False}) or {}
        return value.get('account')

    def accounts(self):
        """读取已登记账户，并在身份变化时丢弃不匹配的额度。"""
        saved = rows(self.path,'execution_accounts', ['id','name','display_name','expected_email','codex_home','kind','subject_id'])
        preferences=rows(self.path,'preferences',['singleton','default_execution_account_id'])
        preference=next((p for p in preferences if p.get('singleton')==1),None)
        default_id=preference.get('default_execution_account_id') if preference is not None else next((a['id'] for a in saved if a.get('id')=='current'),None)
        current = next((a for a in saved if a.get('id')=='current'), {'id':'current','name':'Codex','kind':'current'})
        records = [current] + [a for a in saved if a.get('id')!='current']
        result=[]
        for a in records:
            is_current = a['id']=='current'
            item={'id':a['id'],'name':a.get('display_name') or a.get('name') or 'Codex', 'is_current':is_current,'is_default':a['id']==default_id,
                  'email':None,'plan':None,'login_status':'unavailable','remaining_percent':None,
                  'resets_at':None,'reset_cards':None,'observed_at':timestamp()}
            try:
                home=current_account_home() if is_current else validate_account_home(a.get('codex_home'))
                with self.rpc(home,is_current) as rpc:
                    identity=self.identity(rpc)
                    if not isinstance(identity,dict) or identity.get('type')!='chatgpt':
                        item['login_status']='not_logged_in';result.append(item);continue
                    if a.get('expected_email') and str(identity.get('email') or '').casefold()!=a['expected_email'].casefold():
                        item['login_status']='identity_mismatch';result.append(item);continue
                    usage=rpc.request('account/rateLimits/read') or {}
                    if identity!=self.identity(rpc):
                        item['login_status']='identity_mismatch';result.append(item);continue
                    subject=usage.get('accountId')
                    if not isinstance(subject,str) or not subject:
                        raise RpcError('无法确认账户身份')
                    if not is_current and a.get('subject_id') and subject!=a['subject_id']:
                        item['login_status']='identity_mismatch';result.append(item);continue
                    now=datetime.now(UTC); normalized=normalize_account(usage,observed_at=now,current_account_id=subject,now=now)
                    # 周窗口从官方桶中选择，只有明确 Codex 主桶时展示，不借用 Spark 的额度。
                    limits=usage.get('rateLimitsByLimitId') or {}
                    bucket=limits.get('codex') if isinstance(limits,dict) else None
                    if bucket is None:
                        legacy=usage.get('rateLimits')
                        if isinstance(legacy,dict) and legacy.get('limitId') in (None,'codex'):
                            bucket=legacy
                    window=next((v for k,v in (bucket or {}).items() if k in ('primary','secondary') and isinstance(v,dict) and v.get('windowDurationMins')==10080),None)
                    percent=number(window.get('usedPercent')) if window else None
                    item.update(email=identity.get('email'),plan=identity.get('planType'),login_status='ready',
                                remaining_percent=max(0,min(100,100-percent)) if percent is not None else None,
                                resets_at=number(window.get('resetsAt')) if window else None,reset_cards=normalized.get('resetCredits'))
                    official=next((identity[k] for k in ('username','displayName','name') if isinstance(identity.get(k),str) and identity[k].strip()),None)
                    item['name']=a.get('display_name') or official or a.get('name') or 'Codex'
            except (OSError,ValueError,sqlite3.Error):
                item['login_status']='unavailable'
            result.append(item)
        return result

    def _skill_records(self):
        """官方技能发现结果；不把工作台自建角色冒充原生技能。"""
        with self.rpc() as rpc:
            response=rpc.request('skills/list',{'cwds':[self.cwd],'forceReload':True}) or {}
        result=[];seen=set()
        for group in response.get('data',[]):
            for skill in group.get('skills',[]):
                if not isinstance(skill,dict) or not isinstance(skill.get('path'),str):continue
                identity=hashlib.sha256(skill['path'].encode()).hexdigest()[:32]
                if identity in seen:continue
                seen.add(identity);interface=skill.get('interface') or {}
                result.append({'id':identity,'name':skill.get('name'),'display_name':interface.get('displayName') or skill.get('name'),
                               'description':skill.get('description') or '', 'short_description':interface.get('shortDescription') or skill.get('shortDescription'),
                               'updated_at':self._skill_modified(skill['path']),'enabled':skill.get('enabled') is True,'scope':skill.get('scope'),'plugin_id':skill.get('pluginId'),'source':'codex','path':skill['path']})
        return result

    @staticmethod
    def _skill_modified(path):
        try:return Path(path).stat().st_mtime
        except OSError:return None

    def skills(self):
        """展示全部原生发现的技能；不公开配置仓库绝对路径。"""
        return [{k:v for k,v in x.items() if k!='path'} for x in self._skill_records()]

    def skill_sources(self):
        """仅供资源适配器定位技能内静态资产，不能由客户端传入路径。"""
        return [{'id':x['id'],'name':x['display_name'] or x['name'],'path':x['path']} for x in self._skill_records()]

    def plugins(self):
        """只读本地已安装插件目录，不强制刷新远端市场。"""
        with self.rpc() as rpc:
            result=rpc.request('plugin/list',{'cwds':[self.cwd],'forceRefetch':False,'marketplaceKinds':['local']}) or {}
        values=[];seen=set()
        for market in result.get('marketplaces',[]):
            for item in market.get('plugins',[]):
                if not item.get('installed') or item.get('id') in seen:continue
                seen.add(item['id']);interface=item.get('interface') or {}
                values.append({'id':'plugin:'+item['id'],'name':interface.get('displayName') or item.get('name'),
                               'kind':'plugin','enabled':item.get('enabled') is True,'source':'codex_plugin_catalog',
                               'version':item.get('localVersion') or item.get('version'),'availability':'unverified'})
        return values

    def snapshot(self):
        """当前主体明确后才选取账户侧栏；不在身份不明时推断账户。"""
        with self.rpc() as rpc:
            before=self.identity(rpc)
            if not isinstance(before,dict):raise RpcError('当前账户信息不可用')
            limits=rpc.request('account/rateLimits/read') or {}
            if before!=self.identity(rpc) or not isinstance(limits.get('accountId'),str):raise RpcError('账户身份发生变化')
        value=self.catalog.snapshot(limits['accountId'])
        if value.get('status',{}).get('state')!='ok':raise RpcError('原生目录暂时无法读取')
        return value

    def turns(self, session, cursor=None):
        """分页读取原生执行摘要；不下载完整历史或扫描原始日志。"""
        with self.rpc() as rpc:
            identity=self.identity(rpc)
            response=rpc.request('thread/turns/list',{'threadId':session['native_id'],'limit':50,'cursor':cursor,
                                                     'itemsView':'summary','sortDirection':'desc'}) or {}
            thread=rpc.request('thread/read',{'threadId':session['native_id'],'includeTurns':False}) or {}
            metadata={}
            path=(thread.get('thread') or {}).get('path')
            if isinstance(path,str):
                try:metadata=self.turn_metadata.read(path,session['native_id'])
                except OSError:pass
            if identity!=self.identity(rpc):raise RpcError('读取时账户身份发生变化')
        return [{**self.turn_view(t,session),**metadata.get(t['id'],{})} for t in response.get('data',[])], response.get('nextCursor')

    @staticmethod
    def turn_view(turn, session):
        """状态严格来自原生轮次；会话 idle、未加载及聚合 Token 不作为轮次指标。"""
        if not isinstance(turn,dict) or not isinstance(turn.get('id'),str):raise RpcError('原生执行记录格式无效')
        status=turn.get('status');state={'inProgress':'running','completed':'done','failed':'done','interrupted':'done','cancelled':'done'}.get(status)
        if session.get('native_status')=='archived':state='archived'
        summary=''
        for item in turn.get('items',[]):
            if item.get('type')=='userMessage':
                summary=' '.join(c.get('text','') for c in item.get('content',[]) if isinstance(c,dict) and c.get('type')=='text')
                break
        # 仅传出经过净化的短摘要，不把包含附件、工具输出或秘密的原始消息传给页面。
        if '## My request:' in summary:
            summary=summary.split('## My request:',1)[1]
        summary=re.sub(r'<in-app-browser-context\b[^>]*>[\s\S]*?</in-app-browser-context>', '', summary)
        summary=re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----', '私钥（已隐藏）', summary)
        summary=re.sub(r'(^|\s)#{1,6}\s*', ' ', summary).replace('**','').replace('`','').strip()
        title=execution_title(session['native_id'],turn['id'],summary)
        duration=number(turn.get('durationMs'));start=number(turn.get('startedAt'));end=number(turn.get('completedAt'))
        if duration is None and start is not None and end is not None and end>=start:duration=(end-start)*1000
        return {'id':session['native_id']+':'+turn['id'],'turn_id':turn['id'],'session_id':session['id'],
                'title':title,'state':state,'result':status,'started_at':start,'completed_at':end,'duration_ms':duration,
                'input_tokens':None,'output_tokens':None,'total_tokens':None,'cached_input_tokens':None,'reasoning_output_tokens':None,
                'model':None,'reasoning_effort':None,'account':None,'source':'codex','source_kind':'execution_turn',
                'session_title':session['title'],'project_id':session.get('project_id'),'section_id':session.get('section_id'),
                'native_url':'codex://threads/'+session['native_id']}
