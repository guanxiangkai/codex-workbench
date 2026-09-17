"""当前已分析执行记录的语义标题；不改写原生会话或执行数据。"""
from functools import lru_cache
from pathlib import Path
import json
from .titles import safe_display_title, _display_title

@lru_cache(maxsize=1)
def _labels():
    path = Path(__file__).with_name('execution_titles.json')
    return json.loads(path.read_text()) if path.is_file() else {}

def execution_title(thread_id,turn_id,description):
    """已归纳记录使用精炼标题；未归纳记录仅抽取请求，不声称模型总结。"""
    label=_labels().get(thread_id,{}).get(turn_id)
    if label:return safe_display_title(label,40)
    if not description:return '未提供任务描述'
    return _display_title(description,40,extract_action=True)
