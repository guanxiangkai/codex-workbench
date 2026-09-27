from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import threading
import uuid
import json
from urllib.parse import quote
from pathlib import Path


class PlanningNotes:
    """Project planning details into an Obsidian-readable, user-editable vault."""

    def __init__(self, library_root, db: sqlite3.Connection, lock=None):
        self.lock = lock or threading.RLock()
        self.db = db
        self.root = Path(library_root).expanduser()
        if self.root.exists() and self.root.is_symlink():
            raise ValueError("symlink_root")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.tasks_root = self.root / "Tasks"
        self.drafts_root = self.root / "Drafts"
        self.assets_root = self.root / "Assets"
        for path in (self.tasks_root, self.drafts_root):
            if path.exists() and path.is_symlink():
                raise ValueError("symlink_directory")
            path.mkdir(mode=0o700, exist_ok=True)
            os.chmod(path, 0o700)
        if self.assets_root.exists() and self.assets_root.is_symlink():
            raise ValueError("symlink_directory")
        self.assets_root.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.assets_root, 0o700)
        with self.lock:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS planning_note_exports "
                "(task_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL)"
            )
            self.db.execute("CREATE TABLE IF NOT EXISTS planning_note_projections "
                            "(path TEXT PRIMARY KEY, sha256 TEXT NOT NULL)")
            self.db.commit()

    @staticmethod
    def _task_id(value):
        try:
            return str(uuid.UUID(str(value)))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("invalid_task_id") from None

    @staticmethod
    def _line(value):
        return str(value if value is not None else "").replace("\r", " ").replace("\n", " ")

    @staticmethod
    def _markdown(value):
        return str(value if value is not None else "").replace("\r\n", "\n").replace("\r", "\n")

    def _asset_links(self, detail):
        result = []
        for asset in detail.get("assets", []) or []:
            aid = asset.get("id")
            if not aid:
                continue
            cur = self.db.execute("SELECT * FROM assets WHERE id=?", (aid,))
            raw = cur.fetchone()
            row = dict(raw) if hasattr(raw, "keys") else (dict(zip([d[0] for d in cur.description], raw)) if raw else None)
            if row:
                name = row.get("name") or aid
                stored = Path(row["path"])
                if stored.is_absolute() or len(stored.parts) != 1 or stored.name in (".", ".."):
                    raise ValueError("asset_path_invalid")
                label = self._line(name).replace("[", "\\[").replace("]", "\\]")
                result.append(f"- [{label} v{row.get('version', 1)}](../files/{quote(stored.name)}) · [资产元数据](../Assets/{quote(aid)}.md)")
        return result

    def _render(self, detail):
        assets = detail.get("assets", []) or []
        meta = {"task_id": detail.get("id"), "title": detail.get("title") or detail.get("id"),
                "status": detail.get("status") or detail.get("state"), "period": detail.get("period"),
                "asset_count": len(assets), "updated_at": detail.get("updated_at")}
        front = "---\n" + "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in meta.items()) + "\n---\n"
        lines = [front, f"# {self._line(detail.get('title') or detail.get('id'))}", "",
                 "> [!info] 自动生成\n> 手写草稿请放入 `Drafts/`；检测到手改时不会覆盖。", "",
                 f"- task_id: `{detail.get('id')}`",
                 f"- period: `{self._line(detail.get('period'))}`",
                 f"- status: `{self._line(detail.get('status') or detail.get('state'))}`", "",
                 "## Prompt", "", self._markdown(detail.get("prompt")), "", "## Runs", ""]
        for run in detail.get("runs", []) or []:
            lines.extend([f"### {self._line(run.get('id'))} · {self._line(run.get('state'))}",
                          f"- created_at: {self._line(run.get('created_at'))}",
                          "- result:", self._markdown(run.get("result")),
                          f"- error: {self._line(run.get('error'))}", ""])
        lines.extend(["## Assets", ""])
        lines.extend(self._asset_links(detail) or ["- none"])
        lines.extend(["", "## Knowledge references", ""])
        refs = detail.get("knowledge_refs", []) or detail.get("knowledge", []) or []
        for ref in refs:
            if isinstance(ref, dict):
                lines.append(f"- scope: `{self._line(ref.get('scope'))}` · key: `{self._line(ref.get('key'))}`")
        if not refs:
            lines.append("- none")
        return ("\n".join(lines) + "\n").encode("utf-8")

    def _projection(self, path, content, conflicts):
        digest = hashlib.sha256(content).hexdigest()
        if path.is_symlink():
            conflicts.append(str(path)); return
        if path.exists():
            row = self.db.execute("SELECT sha256 FROM planning_note_projections WHERE path=?", (str(path),)).fetchone()
            if row is None or hashlib.sha256(path.read_bytes()).hexdigest() != row[0]:
                conflicts.append(str(path)); return
        fd, tmp = tempfile.mkstemp(prefix=".projection.", dir=path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream: stream.write(content)
            os.replace(tmp, path)
            self.db.execute("INSERT INTO planning_note_projections(path,sha256) VALUES(?,?) ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256", (str(path), digest))
        finally:
            try: os.unlink(tmp)
            except OSError: pass

    def export(self, task_detail):
        if not isinstance(task_detail, dict):
            raise ValueError("invalid_task_detail")
        task_id = self._task_id(task_detail.get("id") or task_detail.get("task_id"))
        target = self.tasks_root / f"{task_id}.md"
        if self.tasks_root.is_symlink():
            raise ValueError("symlink_directory")
        content = self._render(task_detail)
        digest = hashlib.sha256(content).hexdigest()
        with self.lock:
            previous = self.db.execute(
                "SELECT sha256 FROM planning_note_exports WHERE task_id=?", (task_id,)
            ).fetchone()
            if target.is_symlink():
                raise ValueError("symlink_target")
            if target.exists():
                existing = target.read_bytes()
                if previous is None or hashlib.sha256(existing).hexdigest() != previous[0]:
                    return {"path": str(target), "status": "conflict"}
            conflicts = []
            fd, temp_name = tempfile.mkstemp(prefix=f".{task_id}.", suffix=".tmp", dir=self.tasks_root)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                os.replace(temp_name, target)
                self.db.execute(
                    "INSERT INTO planning_note_exports(task_id,sha256) VALUES(?,?) "
                    "ON CONFLICT(task_id) DO UPDATE SET sha256=excluded.sha256",
                    (task_id, digest),
                )
                self.db.commit()
            except Exception:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
                raise
            for asset in task_detail.get("assets", []) or []:
                row = self.db.execute("SELECT * FROM assets WHERE id=?", (asset.get("id"),)).fetchone()
                if not row: continue
                cur = self.db.execute("SELECT * FROM assets WHERE id=?", (asset.get("id"),))
                data = dict(row) if hasattr(row, "keys") else dict(zip([d[0] for d in cur.description], row))
                asset_path = self.assets_root / f"{data['id']}.md"
                try:
                    refs = self.db.execute("SELECT task_id,run_id,source_kind,source_id,created_at FROM asset_refs WHERE asset_id=? ORDER BY created_at", (data['id'],)).fetchall()
                except sqlite3.OperationalError:
                    refs = []
                body = "---\n" + "\n".join(f"{k}: {json.dumps(data.get(k), ensure_ascii=False)}" for k in ('id','name','mime','version','sha256','bytes')) + "\n---\n\n"
                body += f"# {self._line(data['name'])}\n\n[文件](../files/{data['path']})\n"
                body += "\n## 关联引用\n\n" + "\n".join(f"- task_id: `{r[0]}` · run_id: `{r[1]}` · source: `{r[2]}` · source_id: `{r[3]}` · created_at: `{r[4]}`" for r in refs)
                if str(data.get('mime', '')).startswith('image/') and data.get('mime') in {'image/png','image/jpeg','image/gif','image/webp'}:
                    body += f"\n![](../files/{data['path']})\n"
                self._projection(asset_path, body.encode(), conflicts)
            for path, body in ((self.root / "工作台任务.base", "filters:\n  and:\n    - file.inFolder(\"Tasks\")\nviews:\n  - type: table\n    name: 任务\n    order: [title, status, period, updated_at]\n"), (self.root / "资料库索引.base", "filters:\n  and:\n    - file.inFolder(\"Assets\")\nviews:\n  - type: table\n    name: 资料库\n    order: [name, mime, bytes, version]\n")):
                self._projection(path, body.encode(), conflicts)
            self.db.commit()
        return {"path": str(target), "status": "updated" if previous else "created", "projections": [str(target)], "conflicts": conflicts}
