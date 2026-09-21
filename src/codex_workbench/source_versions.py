"""仅用本机文件元数据检测目录变化；未知远端变更由惰性过期核验兜底。"""
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from .account_runtime import current_account_home
from .readonly_sources import rows
from .native_catalog import NativeCatalog


def stamp(path):
    """检查替换、删除、权限和内容修改信号，不读取认证或秘密文件内容。"""
    path=Path(path)
    try:
        info=path.stat()
        return (str(path.resolve()),info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns,info.st_mode)
    except FileNotFoundError:return (str(path),'missing')


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,default=str).encode()).hexdigest()


def tree(path,depth=3,limit=3000):
    """有界元数据遍历，覆盖技能文件与新增/删除；不解析文件内容。"""
    found=[];pending=[(Path(path),0)];seen=set()
    while pending and len(found)<limit:
        current,level=pending.pop();value=stamp(current);found.append(value)
        if value in seen:continue
        seen.add(value)
        if level>=depth or not current.is_dir():continue
        for child in sorted(current.iterdir(),reverse=True):
            if len(pending)+len(found)>=limit:break
            pending.append((child,level+1))
    return found


class SourceVersions:
    """缓存账户边界与视图来源信号；只返回不可逆摘要。"""
    def __init__(self,db,cwd,resources_dir=None):
        self.db,self.cwd,self.resources_dir=Path(db),Path(cwd),Path(resources_dir) if resources_dir else None
        self._trees={}
        self._tree_lock=threading.Lock()

    def invalidate(self):
        with self._tree_lock:self._trees.clear()

    def _tree(self,path,depth=3,limit=3000):
        """两秒内复用目录元数据；身份检查及单文件标记始终实时读取。"""
        key=(str(path),depth,limit);now=time.monotonic()
        with self._tree_lock:
            entry=self._trees.get(key)
            if entry and now-entry[0]<2:return entry[1]
        value=tree(path,depth,limit)
        with self._tree_lock:
            self._trees={k:v for k,v in self._trees.items() if now-v[0]<2}
            self._trees[key]=(now,value)
        return value

    def _local_source_dir(self, environment_name):
        """返回公开来源目录；未配置时只检查本机数据目录。"""
        configured=os.environ.get(environment_name)
        if configured:return Path(configured).expanduser()
        return self.db.parent / environment_name.removeprefix("WORKBENCH_").removesuffix("_DIR").lower()

    def context(self):
        home=Path(current_account_home())
        accounts=rows(self.db,'execution_accounts',['id','codex_home','subject_id','expected_email'])
        homes=[home]+[Path(a['codex_home']) for a in accounts if a.get('codex_home') and Path(a['codex_home']).is_absolute()]
        return digest([accounts,[stamp(p/'auth.json') for p in homes],stamp(Path.home()/'Library/Keychains/login.keychain-db')])

    def signature(self,view):
        home=Path(current_account_home());signals=[stamp(home/'config.toml')]
        if view in ('models','accounts','config','services'):
            signals.extend(stamp(p) for p in [self.db,Path(str(self.db)+'-wal')])
        if view=='projects':
            signals.append(NativeCatalog(home,include_archived=True).revision())
        if view in ('agents','connections'):
            signals.extend(self._tree(home/'skills',3))
            signals.extend(self._tree(home/'plugins',2,500))
            signals.extend(self._tree(self.cwd/'.agents/skills',3,300))
            signals.extend(self._tree(self.cwd/'.codex/skills',3,300))
        if view=='knowledge':
            knowledge=self._local_source_dir('WORKBENCH_KNOWLEDGE_DIR')
            signals.extend(self._tree(knowledge,2,3000))
        if view in ('config','services'):
            signals.extend(self._tree(self._local_source_dir('WORKBENCH_VAULT_DIR'),2,1000))
        if view in ('other_accounts','config') and self.resources_dir:
            signals.append(stamp(self.resources_dir/'accounts/catalog.json'))
        return digest(signals)
