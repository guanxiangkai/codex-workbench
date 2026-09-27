"""本地模型网关的请求账户记录；不存储请求、响应或认证材料。"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable
from uuid import UUID

from .readonly_sources import connection
from .runtime import secure_directory


class GatewayError(ValueError):
    """只包含预定义错误，不把上游异常或请求正文暴露给调用方。"""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


@dataclass(frozen=True)
class AccountTarget:
    """账户目录中的非秘密身份；subject 用于检测同一账户引用下的换号。"""

    account_id: str
    subject_id: str

    def __post_init__(self):
        if not all(isinstance(v, str) and re.fullmatch(r'[A-Za-z0-9_.:@-]{1,160}', v)
                   for v in (self.account_id, self.subject_id)):
            raise GatewayError('account_invalid', '账户身份尚未确认', 409)


class WorkbenchAccounts:
    """只读工作台原有账户及默认偏好；没有默认时拒绝，不猜测桌面账户。"""

    def __init__(self, path: Path):
        self.path = path

    def __call__(self, account_id: str | None = None) -> AccountTarget:
        try:
            with connection(self.path) as db:
                if account_id is None:
                    row = db.execute('SELECT default_execution_account_id FROM preferences WHERE singleton=1').fetchone()
                    account_id = row[0] if row else None
                row = db.execute('SELECT id,subject_id FROM execution_accounts WHERE id=?', (account_id,)).fetchone()
        except (OSError, sqlite3.Error):
            raise GatewayError('account_catalog_unavailable', '无法读取账户配置', 503) from None
        if not row or not row['subject_id']:
            raise GatewayError('account_unconfirmed', '默认或绑定账户尚未确认身份', 409)
        return AccountTarget(row['id'], row['subject_id'])


def thread_key(value) -> str:
    """仅接受规范 UUID；缺少线程元数据时禁止以 header 或默认值猜归属。"""
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError()
    except ValueError:
        raise GatewayError('thread_metadata_required', '请求缺少有效的会话标识', 409) from None
    return value


class RouteStore:
    """每次请求读取当前默认账户；SQLite 只记录最近使用账户及会话根标识。"""

    def __init__(self, directory: Path):
        self.directory = secure_directory(directory)
        self.path = self.directory / 'routes.sqlite3'
        if self.path.is_symlink():
            raise ValueError('路由库不能是符号链接')
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS routes(thread_id TEXT PRIMARY KEY, root_id TEXT NOT NULL, '
                       'account_id TEXT NOT NULL, subject_id TEXT NOT NULL, created_at REAL NOT NULL)')
        self.path.chmod(0o600)
        self._lock = threading.Lock()
        self._active: set[str] = set()

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA trusted_schema=OFF')
        try:
            with db:
                yield db
        finally:
            db.close()

    def lookup(self, thread_id: str) -> dict | None:
        """按精确会话读取归属，不提供全量会话枚举接口。"""
        with self._db() as db:
            row = db.execute('SELECT * FROM routes WHERE thread_id=?', (thread_key(thread_id),)).fetchone()
            return dict(row) if row else None

    def bind(self, body: dict, accounts: Callable, verify: Callable, *, compact=False) -> AccountTarget:
        """每次推理及压缩使用当前默认账户；已发出的请求保持其认证快照。"""
        metadata = body.get('client_metadata')
        if not isinstance(metadata, dict):
            raise GatewayError('thread_metadata_required', '请求缺少会话元数据', 409)
        thread_id = thread_key(metadata.get('thread_id'))
        root_id = thread_key(metadata.get('session_id', thread_id))
        if body.get('generate') is False:
            raise GatewayError('prewarm_unsupported', '此网关使用 HTTP 模式，不接收模型预热请求', 409)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            own = db.execute('SELECT * FROM routes WHERE thread_id=?', (thread_id,)).fetchone()
            if own and own['root_id'] != root_id:
                raise GatewayError('thread_owner_mismatch', '会话根标识与已有记录不一致', 409)
            chosen = accounts()
            # 服务器响应引用属于原账户，不能借切换默认账户静默丢弃上下文。
            if body.get('previous_response_id') and (not own or
                    (own['account_id'], own['subject_id']) != (chosen.account_id, chosen.subject_id)):
                raise GatewayError('account_context_required', '账户已切换，请使用完整会话上下文重试', 409)
            if verify(chosen) != chosen.subject_id:
                raise GatewayError('upstream_identity_mismatch', '上游执行身份与当前默认账户不符', 409)
            db.execute('INSERT INTO routes VALUES(?,?,?,?,?) '
                       'ON CONFLICT(thread_id) DO UPDATE SET account_id=excluded.account_id, '
                       'subject_id=excluded.subject_id',
                       (thread_id, root_id, chosen.account_id, chosen.subject_id, time.time()))
        return chosen

    @contextmanager
    def executing(self, thread_id: str):
        """同一会话同时只允许一个模型请求；结束、失败、取消均释放执行占用。"""
        key = thread_key(thread_id)
        with self._lock:
            if key in self._active:
                raise GatewayError('thread_busy', '此会话已有模型请求正在执行', 409)
            self._active.add(key)
        try:
            yield
        finally:
            with self._lock:
                self._active.discard(key)
