"""Opt-in output selection with expiring, content-addressed local originals.

An optional Jev-style selector returns top-level field names, never replacement
facts. Controls are retained by this module regardless of the selector.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import time
import tempfile
from pathlib import Path


class PlanningOutput:
    critical=('error','exception','control','status','path','uri','url','evidence','reference',
              'source','constraint','acceptance','permission','warning','failed','exit_code',
              'signal','limit','cursor','revision','version','must','blocked','preserve')

    def __init__(self, root, selector=None):
        self.root=Path(root)
        self.selector=selector

    def read(self, source_id, pointer=''):
        if not isinstance(source_id,str) or not re.fullmatch('[0-9a-f]{64}',source_id):
            raise ValueError('output_source_invalid')
        path=self.root/(source_id+'.json')
        if self.root.is_symlink() or path.is_symlink() or not path.is_file() or time.time()-path.stat().st_mtime>3600:
            raise ValueError('output_source_expired')
        raw=path.read_bytes()
        if len(raw)>2*1024*1024 or hashlib.sha256(raw).hexdigest()!=source_id:
            raise ValueError('output_source_invalid')
        value=json.loads(raw)
        if pointer:
            if not isinstance(pointer,str) or not pointer.startswith('/'):
                raise ValueError('output_pointer_invalid')
            try:
                for token in pointer[1:].split('/'):
                    token=token.replace('~1','/').replace('~0','~')
                    value=value[int(token)] if isinstance(value,list) else value[token]
            except (IndexError,KeyError,ValueError,TypeError):
                raise ValueError('output_pointer_invalid') from None
        return {'value':value,'source_id':source_id,'pointer':pointer}

    def filter(self, value, enabled=False):
        started=time.perf_counter()
        raw=json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
        if len(raw)>2*1024*1024:
            raise ValueError('output_too_large')
        if not enabled:
            return {'value':value,'enabled':False,'raw_bytes':len(raw),'retained_bytes':len(raw),'duration_ms':0,'usage':None}
        if self.root.is_symlink():
            raise ValueError('output_cache_invalid')
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        digest=hashlib.sha256(raw).hexdigest()
        path=self.root/(digest+'.json')
        fd,temporary=tempfile.mkstemp(prefix='.output-',dir=self.root)
        try:
            os.fchmod(fd,0o600)
            with os.fdopen(fd,'wb') as stream:stream.write(raw)
            os.replace(temporary,path)
        finally:
            if os.path.exists(temporary):os.unlink(temporary)
        # Cache lifecycle never visits the task asset store or formal output root.
        for cached in self.root.glob('*.json'):
            if re.fullmatch(r'[0-9a-f]{64}\.json',cached.name) and not cached.is_symlink() and time.time()-cached.stat().st_mtime>3600:
                cached.unlink()
        omitted=[]
        def important(key):return any(token in str(key).casefold() for token in self.critical)
        def contains_control(item):
            if isinstance(item,dict):return any(important(key) or contains_control(child) for key,child in item.items())
            if isinstance(item,list):return any(contains_control(child) for child in item)
            if isinstance(item,str):return bool(re.search(r'error|failed|forbidden|must|不得|禁止|失败|错误|验收|保留',item,re.I))
            return False
        selected=None
        if self.selector and isinstance(value,dict):
            try:
                keys=self.selector(value)
                if isinstance(keys,list) and all(isinstance(key,str) and key in value for key in keys):selected=set(keys)
            except Exception:
                pass
        def trim(item,pointer='',protected=False):
            if protected:return item
            if isinstance(item,dict):
                result={}
                for index,(key,child) in enumerate(item.items()):
                    child_pointer=pointer+'/'+key.replace('~','~0').replace('/','~1')
                    if important(key) or contains_control(child) or (key in selected if selected is not None and not pointer else index<32):
                        result[key]=trim(child,child_pointer,important(key))
                    else:omitted.append(child_pointer)
                return result
            if isinstance(item,list):
                result=[]
                for index,child in enumerate(item):
                    if index<32 or contains_control(child):result.append(trim(child,pointer+'/'+str(index)))
                    else:omitted.append(pointer+'/'+str(index))
                return result
            if isinstance(item,str) and len(item)>1024:
                omitted.append(pointer)
                lines=item.splitlines()
                important_lines=[f'{i+1}: {line}' for i,line in enumerate(lines) if contains_control(line)]
                return item[:1024]+'\n[…已省略，原文可按 source_id/pointer 回取…]\n'+'\n'.join(important_lines)
            return item
        retained=trim(value)
        return {'value':retained,'enabled':True,'source_id':digest,'omitted_pointers':omitted,
                'raw_bytes':len(raw),'retained_bytes':len(json.dumps(retained,ensure_ascii=False).encode()),
                'duration_ms':round((time.perf_counter()-started)*1000,3),'usage':None,'expires_in_seconds':3600}
