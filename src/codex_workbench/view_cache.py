"""有界进程内目录快照及按条目增量协议；不持久化、不缓存秘密详情。"""
from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
import threading
import time


def encoded(value):
    """稳定编码仅用于公开视图的修订与容量，不写文件或日志。"""
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()


def stable(value):
    """观察时间变化不等于业务条目变化；保留其他字段原义。"""
    if isinstance(value,dict):
        # 受管其他账户的来源与观测时间是一条登记事实；同一数值的新快照也应同步给卡片。
        if value.get('source')=='other_account_catalog':return {k:stable(v) for k,v in value.items()}
        return {k:stable(v) for k,v in value.items() if k not in ('observed_at','checked_at')}
    if isinstance(value,list):return [stable(v) for v in value]
    return value


def collection(items):
    """仅有稳定、唯一身份的集合使用条目差异；其他列表按字段替换。"""
    if not isinstance(items,list):return None
    if not items:return ('id',{})
    key=next((k for k in ('id','knowledge_key') if all(isinstance(x,dict) and isinstance(x.get(k),str) for x in items)),None)
    if key is None:return None
    result={x[key]:x for x in items}
    return (key,result) if len(result)==len(items) else None


def delta(previous,current):
    """输出顶层字段变化及有身份集合的 upsert/remove/order；不发回未变条目。"""
    patch={'set':{},'remove':[k for k in previous if k not in current],'collections':{}}
    for field,value in current.items():
        old=previous.get(field)
        if field in previous and stable(old)==stable(value):continue
        before,after=collection(old),collection(value)
        if before is not None and after is not None and (before[0]==after[0] or not before[1] or not after[1]):
            key=after[0] if after[1] else before[0];a,b=before[1],after[1]
            patch['collections'][field]={'key':key,'upsert':[v for k,v in b.items() if k not in a or stable(a[k])!=stable(v)],
                                         'remove':[k for k in a if k not in b],**({'order':list(b)} if list(a)!=list(b) else {})}
        else:patch['set'][field]=value
    return patch


class ViewCache:
    """按查询隔离快照、合并相同在途读取；错误不覆盖最后成功版本。"""
    def __init__(self,clock=time.monotonic,max_entries=48,max_bytes=16*1024*1024):
        self.clock,self.max_entries,self.max_bytes=clock,max_entries,max_bytes
        self.entries=OrderedDict();self.context=None;self.lock=threading.RLock();self.inflight={};self.closed=False

    def clear(self):
        with self.lock:self.closed=True;self.entries.clear()

    def snapshots(self,context):
        """已验证相同账户下的最后成功快照用于首屏；不触发来源查询。"""
        epoch=context()
        with self.lock:
            if self.closed or self.context!=epoch:return []
            return [(key,{'revision':entry['revision'],'data':deepcopy(entry['data'])}) for key,entry in self.entries.items()]

    def sync(self,key,revision,read,context,signature,ttl=60,force=False):
        """每次先核验账户边界和源标记；过期或变化时才调用来源适配器。"""
        epoch=context()
        # 同键并发共享一个结果，其他页面不受慢来源阻塞。
        with self.lock:
            if self.closed:raise ValueError('工作台正在关闭')
            if self.context!=epoch:self.entries.clear();self.context=epoch
            slot=self.inflight.setdefault((epoch,key),[threading.Lock(),0]);slot[1]+=1
            gate=slot[0]
        try:
            with gate:
                if self.closed or context()!=epoch:raise ValueError('账户环境已变化，请重新读取')
                stamp=signature()
                with self.lock:
                    entry=self.entries.get(key) if self.context==epoch else None
                    due=entry is None or entry['signature']!=stamp or self.clock()-entry['checked']>=ttl
                if due or force:
                    value=read()
                    if value.get('status',{}).get('state')=='error' or value.get('source_errors'):
                        raise ValueError('来源暂时不可用，请重新读取')
                    if context()!=epoch:
                        with self.lock:self.entries.clear();self.context=None
                        raise ValueError('账户环境已变化，请重新读取')
                    end_stamp=signature()
                    if stamp!=end_stamp:
                        # 来源读取期间发生变化：本次结果可展示，但下次必须重新核对。
                        stamp=None
                    digest=hashlib.sha256(encoded(stable(value))).hexdigest()
                    with self.lock:
                        if self.closed or self.context!=epoch:raise ValueError('读取环境已变化，请重新读取')
                        versions=OrderedDict(entry['versions']) if entry else OrderedDict()
                        versions[digest]=deepcopy(value)
                        while len(versions)>3:versions.popitem(last=False)
                        entry={'revision':digest,'data':versions[digest],'versions':versions,'signature':stamp,'checked':self.clock()}
                        entry['bytes']=sum(len(encoded(v)) for v in versions.values())
                        self.entries[key]=entry;self.entries.move_to_end(key)
                        while len(self.entries)>self.max_entries or sum(e['bytes'] for e in self.entries.values())>self.max_bytes:
                            self.entries.popitem(last=False)
                with self.lock:
                    if self.context!=epoch or context()!=epoch:raise ValueError('账户环境已变化，请重新读取')
                    if key in self.entries:self.entries.move_to_end(key)
                    result={'context':epoch,'revision':entry['revision'],'base_revision':revision,'unchanged':revision==entry['revision'],
                            'checked_at_age_seconds':max(0,round(self.clock()-entry['checked']))}
                    if result['unchanged']:return result
                    previous=entry['versions'].get(revision)
                    if previous is None:result.update(reset=True,data=deepcopy(entry['data']))
                    else:result.update(reset=False,patch=deepcopy(delta(previous,entry['data'])))
                    return result
        finally:
            # 计数包含等待者，最后一个请求释放后才能删除同键锁。
            with self.lock:
                slot[1]-=1
                if slot[1]==0:self.inflight.pop((epoch,key),None)
