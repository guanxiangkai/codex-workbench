"""仅修改新会话的默认账户偏好，不初始化数据库或切换官方登录。"""
import sqlite3
from pathlib import Path
from urllib.parse import quote

from .account_runtime import current_account_home, validate_account_home
from .readonly_sources import connection


def set_default_account(path, native, account_id, expected_default_id):
    """核验登记与官方身份后原子保存；拒绝过期页面覆盖其他窗口的选择。"""
    path = Path(path)
    if not account_id or path.is_symlink() or not path.is_file():
        raise ValueError("账户登记不可用，请刷新后重试")
    with connection(path) as db:
        row = db.execute(
            "SELECT id,codex_home,subject_id,expected_email FROM execution_accounts WHERE id=?",
            (account_id,),
        ).fetchone()
    if row is None or not row["subject_id"]:
        raise ValueError("账户不存在或身份尚未确认，请刷新后重试")
    saved = dict(row)
    current = account_id == "current"
    home = current_account_home() if current else validate_account_home(saved["codex_home"])
    with native.rpc(home, current) as rpc:
        identity = native.identity(rpc)
        if not isinstance(identity, dict) or identity.get("type") != "chatgpt":
            raise ValueError("此账户尚未登录，不能设为默认")
        expected_email = saved["expected_email"]
        if expected_email and str(identity.get("email") or "").casefold() != expected_email.casefold():
            raise ValueError("账户登录身份已变化，请刷新后重试")
        usage = rpc.request("account/rateLimits/read") or {}
        if usage.get("accountId") != saved["subject_id"] or native.identity(rpc) != identity:
            raise ValueError("账户登录身份已变化，请刷新后重试")

    # mode=rw 禁止创建数据库；事务只更新已有 preferences，不执行 Store 的迁移。
    db = sqlite3.connect("file:" + quote(str(path.resolve()), safe="/") + "?mode=rw", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA trusted_schema=OFF")
        with db:
            db.execute("BEGIN IMMEDIATE")
            latest = db.execute(
                "SELECT id,codex_home,subject_id,expected_email FROM execution_accounts WHERE id=?",
                (account_id,),
            ).fetchone()
            if latest is None or dict(latest) != saved:
                raise ValueError("账户登记已变化，请刷新后重试")
            preference = db.execute("SELECT default_execution_account_id FROM preferences WHERE singleton=1").fetchone()
            previous = preference[0] if preference else (
                "current" if db.execute("SELECT 1 FROM execution_accounts WHERE id='current'").fetchone() else None
            )
            if previous != account_id:
                if previous != expected_default_id:
                    raise ValueError("默认账户已在其他窗口更改，请刷新后重试")
                db.execute(
                    "INSERT INTO preferences(singleton,default_execution_account_id) VALUES(1,?) "
                    "ON CONFLICT(singleton) DO UPDATE SET default_execution_account_id=excluded.default_execution_account_id",
                    (account_id,),
                )
    finally:
        db.close()
    return {"default_account_id": account_id, "applies_to": "new_sessions"}
