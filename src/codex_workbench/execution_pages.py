"""跨会话执行记录分页；只保留短期、不含正文的续页状态。"""
from collections import OrderedDict
import secrets
import threading
import time


class ExecutionPages:
    """每页最多读取四个原生页，优先覆盖各会话，再读取更早轮次。"""
    def __init__(self, clock=time.monotonic):
        self.clock=clock
        self.pages=OrderedDict()
        self.lock=threading.RLock()

    def clear(self):
        with self.lock:self.pages.clear()

    def read(self, sessions, cursor, scope, context, read_turns):
        """续页绑定账户与筛选，重新校验会话存在性；失败不推进游标。"""
        available={s['id']:s for s in sessions}
        with self.lock:
            now=self.clock()
            for key in list(self.pages):
                if now-self.pages[key][0]>900:del self.pages[key]
            if cursor:
                saved=self.pages.get(cursor)
                if not saved or saved[1:3]!=(context,scope):
                    raise ValueError('分页已失效，请重新打开执行记录')
                queue=list(saved[3])
            else:
                queue=[(s['id'],None) for s in sessions]
        tasks=[]
        for _ in range(min(4,len(queue))):
            sid,native_cursor=queue.pop(0)
            if sid not in available:continue
            records,next_cursor=read_turns(available[sid],native_cursor)
            tasks.extend(records)
            if next_cursor:
                if next_cursor==native_cursor:raise ValueError('原生分页未推进')
                queue.append((sid,next_cursor))
        next_token=None
        if queue:
            with self.lock:
                next_token=secrets.token_urlsafe(24)
                self.pages[next_token]=(self.clock(),context,scope,tuple(queue))
                while len(self.pages)>128:self.pages.popitem(last=False)
        return tasks,next_token
