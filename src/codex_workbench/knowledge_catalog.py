"""已审核个人知识的只读视图；Scope 和读取语义由既有知识工具维护。"""
from pathlib import Path
import json
import subprocess


class KnowledgeCatalog:
    """仅开放已存在 Scope 的目录，不创建或审核知识。"""

    def __init__(self, command=None, run=None):
        self.command = Path(command or Path.home() / ".codex/skills/manage-personal-knowledge/scripts/personal-knowledge")
        self.run = run or subprocess.run

    def _read(self, args):
        try:
            result = self.run([str(self.command), *args], text=True, capture_output=True, timeout=20)
            if result.returncode or len(result.stdout.encode()) > 2 * 1024 * 1024:
                raise ValueError("知识来源暂时不可用")
            records = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
            if any(not isinstance(record, dict) for record in records):
                raise ValueError("知识目录格式无效")
            return records
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            raise ValueError("知识来源暂时不可用") from None

    def scopes(self):
        return [
            {"id": item["scope_key"], "name": item.get("display_name") or item["scope_key"], "kind": item.get("kind")}
            for item in self._read(["scopes"])
            if item.get("is_active") and isinstance(item.get("scope_key"), str)
        ][:500]

    def snapshot(self):
        scopes = self.scopes()
        records, limited = [], []
        keys = ("knowledge_key", "scope_key", "title", "summary", "tags", "scope_priority")
        for scope in scopes:
            items = self._read(["catalog", scope["id"], "1000"])
            own = [item for item in items if item.get("scope_key") == scope["id"]]
            records.extend({key: item[key] for key in keys if key in item} for item in own)
            if len(own) >= 1000:
                limited.append(scope["id"])
        return {"scopes": scopes, "knowledge": records, "limited_scopes": limited}

    def detail(self, scope, key):
        if scope not in {item["id"] for item in self.scopes()}:
            raise ValueError("知识范围不存在或不可访问")
        if not isinstance(key, str) or not key or len(key) > 200:
            raise ValueError("知识标识无效")
        records = self._read(["lookup", scope, key])
        if len(records) != 1:
            raise ValueError("知识条目不存在或已失效")
        record = records[0]
        if record.get("scope_key") != scope or record.get("knowledge_key") != key:
            raise ValueError("知识条目来源不匹配")
        revision = record.get("revision")
        if not isinstance(revision, dict) or not isinstance(revision.get("title"), str) or not isinstance(revision.get("content"), str):
            raise ValueError("知识版本格式无效，未展示不完整内容")
        source = record.get("source") or {}
        return {"knowledge": {
            "knowledge_key": record.get("knowledge_key"), "scope_key": record.get("scope_key"),
            "title": revision["title"], "content": revision["content"], "summary": revision.get("summary"),
            "tags": revision.get("tags", []), "updated_at": revision.get("approved_at") or revision.get("created_at"),
            "source_native_id": source.get("native_id"),
        }}
