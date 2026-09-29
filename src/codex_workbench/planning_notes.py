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

    def __init__(self, library_root, db: sqlite3.Connection, lock=None, asset_files=None):
        self.lock = lock or threading.RLock()
        self.db = db
        self.root = Path(library_root).expanduser()
        if self.root.exists() and self.root.is_symlink():
            raise ValueError("symlink_root")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Existing output directories may contain other user deliverables.
        # Never alter their permissions or link the whole library into Documents.
        database_path = next((row[2] for row in db.execute('PRAGMA database_list') if row[1] == 'main'), '')
        self.asset_files = Path(asset_files) if asset_files else (Path(database_path).parent / 'library' / 'files' if database_path else self.root / 'files')
        self.tasks_root = self.root / "Tasks"
        self.drafts_root = self.root / "Drafts"
        self.assets_root = self.root / "Assets"
        for path in (self.tasks_root, self.drafts_root):
            if path.exists() and path.is_symlink():
                raise ValueError("symlink_directory")
            path.mkdir(mode=0o700, exist_ok=True)
        if self.assets_root.exists() and self.assets_root.is_symlink():
            raise ValueError("symlink_directory")
        self.assets_root.mkdir(mode=0o700, exist_ok=True)
        with self.lock:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS planning_note_exports "
                "(task_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, base_version TEXT)"
            )
            self.db.execute("CREATE TABLE IF NOT EXISTS planning_note_projections "
                            "(path TEXT PRIMARY KEY, sha256 TEXT NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS planning_note_patches "
                            "(task_id TEXT NOT NULL, base_version TEXT, base_sha256 TEXT NOT NULL, "
                            "content_sha256 TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL, "
                            "PRIMARY KEY(task_id, content_sha256))")
            self.db.execute('CREATE TABLE IF NOT EXISTS planning_knowledge_exports (id TEXT PRIMARY KEY, scope TEXT, key TEXT, base_revision TEXT, sha256 TEXT, path TEXT)')
            self.db.execute('CREATE TABLE IF NOT EXISTS planning_knowledge_patches (id TEXT, base_revision TEXT, sha256 TEXT, content TEXT, PRIMARY KEY(id,sha256))')
            try:
                self.db.execute("ALTER TABLE planning_note_exports ADD COLUMN base_version TEXT")
            except sqlite3.OperationalError:
                pass
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

    def _file_link(self, name, folder):
        name = Path(name)
        if name.is_absolute() or len(name.parts) != 1 or name.name in ('.','..'):
            raise ValueError('asset_path_invalid')
        return quote(os.path.relpath(self.asset_files / name, folder), safe='/')

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
                result.append(f"- [{label} v{row.get('version', 1)}]({self._file_link(stored.name,self.tasks_root)}) · [资产元数据](../Assets/{quote(aid)}.md)")
        return result

    def _render(self, detail):
        assets = detail.get("assets", []) or []
        meta = {"task_id": detail.get("id"), "base_version": detail.get("version"),
                "title": detail.get("title") or detail.get("id"),
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
            if row[0] == digest:
                return
        fd, tmp = tempfile.mkstemp(prefix=".projection.", dir=path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream: stream.write(content)
            os.replace(tmp, path)
            self.db.execute("INSERT INTO planning_note_projections(path,sha256) VALUES(?,?) ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256", (str(path), digest))
        finally:
            try: os.unlink(tmp)
            except OSError: pass

    def _patch_candidate(self, task_id, path, exported):
        """Record a hand edit for review; this never changes the task itself."""
        content = path.read_bytes()
        content_hash = hashlib.sha256(content).hexdigest()
        if content_hash == exported["sha256"]:
            return None
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            return {"task_id": task_id, "status": "invalid_encoding", "path": str(path)}
        base_version = exported.get("base_version")
        candidate = {"task_id": task_id, "base_version": base_version,
                     "base_sha256": exported["sha256"], "content_sha256": content_hash,
                     "content": text, "path": str(path), "status": "candidate"}
        self.db.execute(
            "INSERT OR IGNORE INTO planning_note_patches "
            "(task_id,base_version,base_sha256,content_sha256,content,created_at) "
            "VALUES(?,?,?,?,?,datetime('now'))",
            (task_id, base_version, exported["sha256"], content_hash, text),
        )
        self.db.commit()
        return candidate

    def local_edits(self, task_id=None):
        """Collect hand edits as review candidates, without applying them to SQLite tasks."""
        with self.lock:
            params = () if task_id is None else (self._task_id(task_id),)
            sql = "SELECT task_id,sha256,base_version FROM planning_note_exports"
            if task_id is not None:
                sql += " WHERE task_id=?"
            candidates = []
            for row in self.db.execute(sql, params):
                task = row[0]
                path = self.tasks_root / f"{task}.md"
                if path.exists() and not path.is_symlink():
                    candidate = self._patch_candidate(task, path, {"sha256": row[1], "base_version": row[2]})
                    if candidate:
                        candidates.append(candidate)
            return candidates

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
            row = self.db.execute(
                "SELECT sha256,base_version FROM planning_note_exports WHERE task_id=?", (task_id,)
            ).fetchone()
            previous = {"sha256": row[0], "base_version": row[1]} if row else None
            if target.is_symlink():
                raise ValueError("symlink_target")
            if target.exists():
                existing = target.read_bytes()
                if previous is None or hashlib.sha256(existing).hexdigest() != previous["sha256"]:
                    candidate = self._patch_candidate(task_id, target, previous or {"sha256": "", "base_version": None}) if previous else None
                    return {"path": str(target), "status": "conflict", "candidate": candidate}
            conflicts = []
            fd, temp_name = tempfile.mkstemp(prefix=f".{task_id}.", suffix=".tmp", dir=self.tasks_root)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                os.replace(temp_name, target)
                self.db.execute(
                    "INSERT INTO planning_note_exports(task_id,sha256,base_version) VALUES(?,?,?) "
                    "ON CONFLICT(task_id) DO UPDATE SET sha256=excluded.sha256,base_version=excluded.base_version",
                    (task_id, digest, str(task_detail.get("version")) if task_detail.get("version") is not None else None),
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
                link = self._file_link(data['path'],self.assets_root)
                body += f"# {self._line(data['name'])}\n\n[文件]({link})\n"
                body += "\n## 关联引用\n\n" + "\n".join(f"- task_id: `{r[0]}` · run_id: `{r[1]}` · source: `{r[2]}` · source_id: `{r[3]}` · created_at: `{r[4]}`" for r in refs)
                if str(data.get('mime', '')).startswith('image/') and data.get('mime') in {'image/png','image/jpeg','image/gif','image/webp'}:
                    body += f"\n![]({link})\n"
                self._projection(asset_path, body.encode(), conflicts)
            for path, body in ((self.root / "工作台任务.base", "filters:\n  and:\n    - file.inFolder(\"Tasks\")\nviews:\n  - type: table\n    name: 任务\n    order: [title, status, period, updated_at]\n"), (self.root / "资料库索引.base", "filters:\n  and:\n    - file.inFolder(\"Assets\")\nviews:\n  - type: table\n    name: 资料库\n    order: [name, mime, bytes, version]\n"), (self.root / "工作台导航.md", "# 工作台导航\n\n- [任务视图](工作台任务.base)\n- [资料库视图](资料库索引.base)\n- [[Tasks]]\n- [[Assets]]\n- [[Drafts]]\n")):
                self._projection(path, body.encode(), conflicts)
            self.db.commit()
        return {"path": str(target), "status": "updated" if previous else "created", "projections": [str(target)], "conflicts": conflicts}

    def export_knowledge(self, records):
        """Export only catalog-resolved selections; edits remain versioned proposals."""
        directory = self.root / 'Knowledge'
        if directory.is_symlink():
            raise ValueError('symlink_directory')
        directory.mkdir(mode=0o700, exist_ok=True)
        results=[]
        with self.lock:
            for record in records:
                scope,key=record.get('scope_key'),record.get('knowledge_key')
                if not scope or not key or not isinstance(record.get('content'),str):
                    raise ValueError('knowledge_record_invalid')
                identifier=hashlib.sha256((scope+'\0'+key).encode()).hexdigest()
                path=directory / (identifier+'.md')
                revision=hashlib.sha256(json.dumps({k:record.get(k) for k in ('title','content','updated_at')},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
                row=self.db.execute('SELECT base_revision,sha256 FROM planning_knowledge_exports WHERE id=?',(identifier,)).fetchone()
                if path.is_symlink():
                    raise ValueError('symlink_target')
                if path.exists() and (not row or hashlib.sha256(path.read_bytes()).hexdigest()!=row[1]):
                    text=path.read_text(encoding='utf-8')
                    digest=hashlib.sha256(text.encode()).hexdigest()
                    if row:
                        self.db.execute('INSERT OR IGNORE INTO planning_knowledge_patches VALUES(?,?,?,?)',(identifier,row[0],digest,text))
                    results.append({'scope':scope,'key':key,'path':str(path),'status':'conflict','candidate':{'base_revision':row[0] if row else None,'current_revision':revision,'base_stale':bool(row and row[0]!=revision),'sha256':digest,'content':text,'reviewed':False}})
                    continue
                meta={'scope':scope,'knowledge_key':key,'base_revision':revision,'title':record.get('title') or key,'updated_at':record.get('updated_at'),'source_id':record.get('source_native_id'),'status':'reviewed_projection','para_kind':'areas' if scope.startswith('domain:') else 'projects' if scope.startswith('project:') else 'resources'}
                body='---\n'+'\n'.join(f'{k}: {json.dumps(v,ensure_ascii=False)}' for k,v in meta.items())+'\n---\n\n'
                body+='> 本地视图；编辑形成修订候选，不直接改变已审核知识。\n\n'+record['content']+'\n'
                conflicts=[]
                self._projection(path,body.encode(),conflicts)
                if conflicts:
                    results.append({'scope':scope,'key':key,'path':str(path),'status':'conflict'});continue
                self.db.execute('INSERT OR REPLACE INTO planning_knowledge_exports VALUES(?,?,?,?,?,?)',(identifier,scope,key,revision,hashlib.sha256(body.encode()).hexdigest(),str(path)))
                results.append({'scope':scope,'key':key,'path':str(path),'status':'updated' if row else 'created'})
            conflicts=[]
            bases='filters:\n  and:\n    - file.inFolder("Knowledge")\nviews:\n  - type: table\n    name: 已选知识\n    order: [title, scope, para_kind, updated_at, source_id]\n'
            self._projection(self.root/'已选知识.base',bases.encode(),conflicts)
            all_rows=self.db.execute('SELECT scope,key,path FROM planning_knowledge_exports ORDER BY scope,key').fetchall()
            groups={'projects':[],'areas':[],'resources':[]}
            for scope,key,path in all_rows:
                group='projects' if scope.startswith('project:') else 'areas' if scope.startswith('domain:') else 'resources'
                groups[group].append(f'- [{self._line(key).replace("[", "").replace("]", "")}]({quote(os.path.relpath(path,self.root),safe="/")}) · {scope}')
            moc='# 知识地图\n\n[已选知识视图](已选知识.base)\n\n'
            for group,title in [('projects','项目'),('areas','持续领域'),('resources','可复用知识')]:
                moc+='## '+title+'\n\n'+('\n'.join(groups[group]) or '暂无已选条目。')+'\n\n'
            self._projection(self.root/'知识地图.md',moc.encode(),conflicts)
            self.db.commit()
        return {'items':results,'conflicts':conflicts}
