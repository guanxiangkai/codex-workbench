"""运行核心持有的 UI/工具一致快照，只接受完整发布的版本。"""
from __future__ import annotations
import copy
import hashlib
import json
import re
import threading
from pathlib import Path
from .catalog import VERSION, PAGES, UI_URIS, tools_for_page
from .ui_resources import ui_html, page_icons

ROOT=Path(__file__).resolve().parents[2]
RELEASE_FILE=ROOT/'ui/release.json'

def build_release() -> dict:
    """构建无运行数据的页面与工具快照；接口版本必须和源码声明相同。"""
    raw=ui_html()
    declared=re.search(r"appInfo:\{name:'codex-workbench-ui',version:'([^']+)'",raw)
    if not declared or declared.group(1)!=VERSION or not raw.rstrip().endswith('</html>'):
        raise ValueError('页面与运行核心版本未匹配')
    pages={page:{'page':page,'entry':PAGES[page][1],'title':PAGES[page][2],'version':VERSION,
                 'resource_uri':UI_URIS[PAGES[page][1]],'tools':tools_for_page(page),
                 'html':ui_html(),'icons':page_icons(page)} for page in PAGES}
    revision=hashlib.sha256(json.dumps(pages,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    for entry in pages.values():
        entry['revision']=revision
        entry['html']=entry['html'].replace('__WORKBENCH_UI_REVISION__',revision)
    return {'version':VERSION,'revision':revision,'pages':pages}

class UiRelease:
    """生产读取原子发布文件；隔离开发可显式读取源码，失败时保留上一份可用版本。"""
    def __init__(self, source_mode: bool = False, release_file: Path | None = None):
        self.source_mode=source_mode;self.file=release_file or RELEASE_FILE
        self._signature=None;self._snapshot={};self._revision=None;self._lock=threading.RLock();self.refresh()
    def refresh(self):
        with self._lock:
            try:
                paths=[ROOT/'ui/app.html',ROOT/'ui/readonly.js',ROOT/'ui/readonly.css',ROOT/'ui/configuration-crypto.js',ROOT/'ui/assets/readonly-icons.json',ROOT/'ui/icons/board.svg'] if self.source_mode else [self.file]
                signature=tuple((p.stat().st_mtime_ns,p.stat().st_size) for p in paths)
                if signature==self._signature:return
                published=build_release() if self.source_mode else json.loads(self.file.read_text(encoding='utf-8'))
                if published.get('version')!=VERSION or not isinstance(published.get('revision'),str) or set(published.get('pages',{}))!=set(PAGES):
                    raise ValueError('页面与运行核心版本未匹配')
                for page,value in published['pages'].items():
                    if value.get('page')!=page or value.get('version')!=VERSION or value.get('revision')!=published['revision'] or not isinstance(value.get('html'),str):
                        raise ValueError('发布内容不完整')
                self._snapshot,self._revision=published['pages'],published['revision'];self._signature=signature
            except (OSError,ValueError,TypeError):
                if not self._snapshot:raise ValueError('工作台发布版本未就绪，请先发布匹配的界面') from None
    @property
    def revision(self):
        self.refresh();return self._revision
    def manifest(self,page:str,known_revision:str|None=None)->dict:
        """未改变时仅返回摘要，减少入口轮询开销。"""
        if page not in PAGES:raise ValueError('页面不存在')
        self.refresh()
        with self._lock:
            if known_revision==self._revision:return {'revision':self._revision,'unchanged':True}
            return copy.deepcopy(self._snapshot[page])
