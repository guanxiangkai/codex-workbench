"""Bounded, server-selected rule discovery. Discovery is not proof of loading."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

MAX_RULE_BYTES = 64_000


def collect_rules(task, project, card, *, run_id=None):
    rules, contents, unavailable = [], {}, []
    value = card or {"goal": task["title"], "acceptance": []}
    body = json.dumps(value, ensure_ascii=False, sort_keys=True)
    records = [("task-card", "task", "workbench:task-card", body,
                "injected" if run_id else "available")]
    candidates = [("global-entry", "global", Path.home() / ".codex" / "AGENTS.md")]
    cwd = project.get("cwd")
    if cwd:
        candidates.append(("project-entry", "project", Path(cwd) / "AGENTS.md"))
    for rule_id, scope, path in candidates:
        try:
            # Paths come from the registered project and fixed app location, never an API path.
            if path.is_symlink():
                unavailable.append({'id': rule_id, 'reason': 'symlink_not_read'})
                continue
            if not path.is_file():
                continue
            with path.open("rb") as stream:
                raw = stream.read(MAX_RULE_BYTES + 1)
            if len(raw) > MAX_RULE_BYTES:
                unavailable.append({"id": rule_id, "reason": "size_limit"})
                continue
            records.append((rule_id, scope, str(path), raw.decode("utf-8"), "discovered"))
        except (OSError, UnicodeError):
            unavailable.append({"id": rule_id, "reason": "unreadable"})
    for rule_id, scope, source, content, load_state in records:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        rules.append({"id": rule_id, "scope": scope, "source": source,
                      "kind": "task_constraint" if rule_id == "task-card" else "instruction",
                      "content_hash": digest, "byte_count": len(content.encode("utf-8")),
                      "load_state": load_state,
                      "evidence_ref": "run:" + run_id if rule_id == "task-card" and run_id else None})
        contents[rule_id] = content
    return {"rules": rules, "collector": "workbench-rule-manifest-v1",
            "runtime": {"schema_version": 1, "skill_loading": "unknown", "unavailable": unavailable,
                        "boundary": "discovered is not loaded; injected is not proof of compliance"}}, contents
