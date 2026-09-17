"""按原生轮次读取历史配置与用量；缓存追加部分，不持久化原始会话。"""
from collections import OrderedDict
from pathlib import Path
import json
import threading

FIELDS=('input_tokens','cached_input_tokens','output_tokens','reasoning_output_tokens','total_tokens')

class TurnMetadata:
    """只消费 session_meta、turn_context 和按 turn_id 归属的用量白名单。"""
    def __init__(self,home):
        self.home=Path(home);self.cache=OrderedDict();self.lock=threading.Lock()

    def read(self,path,thread_id):
        """路径须由官方 thread/read 返回，限制在该 Codex Home 会话根内。"""
        path=Path(path).resolve()
        roots=[(self.home/name).resolve() for name in ('sessions','archived_sessions')]
        if not any(path.is_relative_to(root) for root in roots) or path.suffix!='.jsonl':return {}
        with self.lock:
            stat=path.stat();key=(str(path),thread_id);entry=self.cache.get(key)
            signature=(stat.st_dev,stat.st_ino)
            if not entry or entry['signature']!=signature or stat.st_size<entry['offset'] or (stat.st_size==entry['offset'] and stat.st_mtime_ns!=entry['mtime']):
                entry={'signature':signature,'offset':0,'mtime':0,'verified':False,'turns':{}}
            with path.open('rb') as stream:
                stream.seek(entry['offset']);budget=256*1024*1024
                while stream.tell()<stat.st_size and budget>0:
                    offset=stream.tell();line=stream.readline(8*1024*1024+1);budget-=len(line)
                    if not line.endswith(b'\n'):
                        if len(line)>8*1024*1024:return {}
                        stream.seek(offset);break
                    entry['offset']=stream.tell()
                    # 先筛除模型正文、工具输出及用户消息，避免解析并保留无关材料。
                    if not any(marker in line[:160] for marker in (b'"session_meta"',b'"turn_context"',b'"token_usage_record"')):continue
                    try:event=json.loads(line)
                    except (ValueError,UnicodeError):continue
                    kind=event.get('type');payload=event.get('payload',{})
                    if not isinstance(payload,dict):continue
                    if kind=='session_meta':
                        if payload.get('id')!=thread_id:return {}
                        entry['verified']=True
                    if not entry['verified']:continue
                    turn_id=payload.get('turn_id')
                    if not isinstance(turn_id,str):continue
                    if kind=='turn_context':
                        target=entry['turns'].setdefault(turn_id,{})
                        for source,dest in [('model','model'),('effort','reasoning_effort')]:
                            value=payload.get(source)
                            if isinstance(value,str) and len(value)<160:target[dest]=value
                    elif kind=='token_usage_record' and payload.get('thread_id')==thread_id:
                        usage=payload.get('turn_token_usage')
                        if not isinstance(usage,dict):continue
                        target=entry['turns'].setdefault(turn_id,{})
                        for field in FIELDS:
                            value=usage.get(field)
                            if type(value) is int and value>=0:target[field]=value
                    if len(entry['turns'])>10000:return {}
            entry['mtime']=stat.st_mtime_ns;self.cache[key]=entry;self.cache.move_to_end(key)
            while len(self.cache)>16:self.cache.popitem(last=False)
            return {key:dict(value) for key,value in entry['turns'].items()} if entry['verified'] else {}
