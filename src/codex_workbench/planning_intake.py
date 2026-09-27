"""Bounded text-intake drafting for the local planning service."""
from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from .planning_draft import PlanningDraft


class PlanningIntake:
    """Use only verified MiniMax/GLM models and only supplied catalog candidates."""

    def __init__(self, models_provider, projects, sections, tasks, invoke=None):
        self.projects = projects
        self.sections = sections
        self.tasks = tasks
        self.worker = PlanningDraft(models_provider, invoke)

    def _manual(self, text, project_id, task_id, start_date, due_date, period, warning):
        intent = 'question' if task_id else 'create_task'
        return {'source': 'manual', 'warning': warning, 'items': [{
            'intent': intent, 'target_task_id': task_id, 'expected_version': None,
            'title': text[:300] or '未命名工作项', 'prompt': text[:12000], 'project_id': project_id,
            'project_name': None, 'section_id': None, 'section_name': None,
            'start_date': start_date, 'due_date': due_date, 'period': period, 'tags': [],
            'execution_account_id': None, 'model': None, 'effort': None, 'sandbox': None,
        }]}

    def generate(self, text, *, project_id=None, task_id=None, start_date=None, due_date=None, period=None, today=None):
        if not isinstance(text, str) or not text.strip():
            return self._manual('', project_id, task_id, start_date, due_date, period, 'empty_input')
        sections = {section['id']: section['name'] for section in self.sections()}
        candidates = [{'id': project['id'], 'name': project['name'], 'section_id': project.get('section_id'),
                       'section_name': sections.get(project.get('section_id'))} for project in self.projects()[:100]]
        task_candidates = [{'id': task['id'], 'title': task['title'], 'state': task['state'], 'project_id': task['project_id'],
                            'section_id': task.get('section_id'), 'prompt': task.get('prompt', '')[:500], 'version': task['version']}
                           for task in self.tasks()[:100]]
        instruction = ('把用户文本拆成一个或多个独立项目工作项，不执行。只输出 JSON：'
            '{"items":[{"intent":"create_task|supplement|question","target_task_id":null,'
            '"expected_version":null,"title":"","prompt":"","project_id":null,"start_date":null,'
            '"due_date":null,"period":null,"tags":[],"project_name":null,"section_id":null,"section_name":null}]}. '
            'project_id 只能从以下候选中选择：' + json.dumps(candidates, ensure_ascii=False) +
            '；target_task_id 只能从以下候选中选择：' + json.dumps(task_candidates, ensure_ascii=False) +
            '。当前输入上下文为 project_id=' + str(project_id) + ', task_id=' + str(task_id) +
            ', start_date=' + str(start_date) + ', due_date=' + str(due_date) + ', period=' + str(period) +
            ', today=' + str(today or date.today().isoformat()) + '。上述上下文是未明确时的默认值；'
            '不得编造候选、版本、账户、模型或执行权限；'
            '可保留用户明确的项目/分区名称建议。无法判断时填 null。不要输出 Markdown 或解释。')
        for model in self.worker._models():
            try:
                if getattr(self.worker.invoke, '__func__', None) is PlanningDraft._invoke_default:
                    raw = self.worker._invoke_default(model, text, 60, instruction)
                else:
                    raw = self.worker.invoke(model, text, 60)
                payload = raw.get('text') if isinstance(raw, dict) else raw
                payload = re.sub(r'^<think>.*?</think>\s*', '', payload.strip(), flags=re.S | re.I)
                if payload.startswith('```'):
                    payload = re.sub(r'^```(?:json)?\s*|\s*```$', '', payload, flags=re.S | re.I).strip()
                value = json.loads(payload)
                items = self._validate(value, candidates, task_candidates, selected_task_id=task_id,
                                       default_project_id=project_id, default_start_date=start_date,
                                       default_due_date=due_date, default_period=period)
                return {'source': 'model', 'warning': None, 'items': items}
            except Exception:
                continue
        return self._manual(text, project_id, task_id, start_date, due_date, period, 'model_failed')

    @staticmethod
    def _validate(value: Any, projects: list[dict], tasks: list[dict], *, selected_task_id: str | None,
                  default_project_id: str | None, default_start_date: str | None,
                  default_due_date: str | None, default_period: str | None):
        if not isinstance(value, dict) or not isinstance(value.get('items'), list) or not 1 <= len(value['items']) <= 12:
            raise ValueError('draft_schema')
        ids = {item['id'] for item in projects}
        project_sections = {item['id']: item.get('section_id') for item in projects}
        task_ids = {item['id'] for item in tasks}
        versions = {item['id']: item['version'] for item in tasks}
        result = []
        for item in value['items']:
            if not isinstance(item, dict) or item.get('intent') not in {'create_task', 'supplement', 'question'}:
                raise ValueError('draft_schema')
            target = item.get('target_task_id')
            if target is not None and target not in task_ids:
                raise ValueError('draft_schema')
            if selected_task_id is not None and target is not None and target != selected_task_id:
                raise ValueError('draft_schema')
            if item['intent'] == 'create_task' and target is not None:
                raise ValueError('draft_schema')
            if item['intent'] in {'supplement', 'question'} and target is None:
                raise ValueError('draft_schema')
            project_id = item.get('project_id') or default_project_id
            if project_id is not None and project_id not in ids:
                raise ValueError('draft_schema')
            title, prompt = item.get('title'), item.get('prompt')
            if not isinstance(title, str) or not title.strip() or len(title) > 300 or not isinstance(prompt, str) or len(prompt) > 12000:
                raise ValueError('draft_schema')
            tags = item.get('tags', [])
            if not isinstance(tags, list) or len(tags) > 6 or any(not isinstance(x, str) or len(x) > 80 for x in tags):
                raise ValueError('draft_schema')
            expected_version = item.get('expected_version')
            if target is not None and expected_version != versions[target]:
                raise ValueError('draft_schema')
            project_name = item.get('project_name')
            section_name = item.get('section_name')
            if project_name is not None and (not isinstance(project_name, str) or not project_name.strip() or len(project_name) > 160):
                raise ValueError('draft_schema')
            if section_name is not None and (not isinstance(section_name, str) or not section_name.strip() or len(section_name) > 80):
                raise ValueError('draft_schema')
            section_id = item.get('section_id')
            if section_id is not None and (not isinstance(section_id, str) or project_id is None or project_sections[project_id] != section_id):
                raise ValueError('draft_schema')
            start_date = item.get('start_date') or default_start_date
            due_date = item.get('due_date') or default_due_date
            for value_date in (start_date, due_date):
                if value_date is not None:
                    if not isinstance(value_date, str) or len(value_date) != 10:
                        raise ValueError('draft_schema')
                    try:
                        if date.fromisoformat(value_date).isoformat() != value_date:
                            raise ValueError('draft_schema')
                    except ValueError:
                        raise ValueError('draft_schema')
            if start_date and due_date and start_date > due_date:
                raise ValueError('draft_schema')
            item_period = item.get('period') or default_period
            if item_period is not None and (not isinstance(item_period, str) or len(item_period) > 80):
                raise ValueError('draft_schema')
            result.append({'intent': item['intent'], 'target_task_id': target, 'expected_version': expected_version,
                'title': title.strip(), 'prompt': prompt, 'project_id': project_id, 'project_name': project_name.strip() if project_name else None,
                'section_id': section_id, 'section_name': section_name.strip() if section_name else None, 'start_date': start_date, 'due_date': due_date,
                'period': item_period.strip() if item_period else None, 'tags': tags, 'execution_account_id': None, 'model': None, 'effort': None, 'sandbox': None})
        return result
