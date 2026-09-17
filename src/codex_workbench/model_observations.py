"""本机模型调用结果；只保存配置指纹和结果元数据，不保存会话正文或凭据。"""
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def configuration_fingerprint(model):
    """绑定实际调用配置；展示名称变化不会抹去结果，连接或凭据变化会失效。"""
    values={key:model.get(key) or '' for key in (
        'id','base_url','model','model_source','model_type','api_profile','protocol','voice','provider_id')}
    values['credential_id']=model.get('credential_id') or str(model.get('credential_ref') or '').removeprefix('vault:')
    return hashlib.sha256(json.dumps(values,sort_keys=True,separators=(',',':')).encode()).hexdigest()


class ModelObservations:
    """写入由实际调用方负责；目录只读投影，不触发探测或创建数据库。"""

    def __init__(self, data_dir):
        self.path=Path(data_dir)/'model-observations.sqlite3'

    def record(self, model, *, success, source, observed_at=None):
        """记录调用终态；调用方须确认生成完成，不能把 Key 查询或任务受理当成功。"""
        if not isinstance(model.get('id'),str) or not model['id'] or len(model['id'])>512:
            raise ValueError('记录调用结果需要明确的目录模型标识')
        if type(success) is not bool or source not in ('conversation','probe'):
            raise ValueError('模型调用结果或来源无效')
        now=datetime.now(timezone.utc)
        observed=now if observed_at is None else datetime.fromisoformat(observed_at.replace('Z','+00:00'))
        if observed.tzinfo is None or observed>now:
            raise ValueError('调用时间必须含时区且不能在未来')
        checked=observed.astimezone(timezone.utc).isoformat()
        fingerprint=configuration_fingerprint(model)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        # 先以私有权限创建，避免把本机模型标识暴露给其他系统用户。
        self.path.touch(mode=0o600,exist_ok=True)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS observations(
                model_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                status TEXT NOT NULL, checked_at TEXT NOT NULL, verified_at TEXT,
                source TEXT NOT NULL, PRIMARY KEY(model_id,fingerprint))''')
            conn.execute('''INSERT INTO observations VALUES(?,?,?,?,?,?)
                ON CONFLICT(model_id,fingerprint) DO UPDATE SET
                status=CASE WHEN excluded.checked_at>=checked_at THEN excluded.status ELSE status END,
                source=CASE WHEN excluded.checked_at>=checked_at THEN excluded.source ELSE source END,
                checked_at=MAX(checked_at,excluded.checked_at),
                verified_at=CASE WHEN excluded.verified_at IS NULL THEN verified_at
                    WHEN verified_at IS NULL THEN excluded.verified_at
                    ELSE MAX(verified_at,excluded.verified_at) END''',
                (model['id'],fingerprint,'verified' if success else 'failed',checked,checked if success else None,source))

    def project(self, models):
        """投影匹配配置的最新结果；迟到的历史成功不会覆盖较新的失败。"""
        if not self.path.exists():return models
        with closing(sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True)) as conn:
            for model in models:
                row=conn.execute('''SELECT status,checked_at,verified_at,source FROM observations
                    WHERE model_id=? AND fingerprint=?''',
                    (model['id'],configuration_fingerprint(model))).fetchone()
                if row and (not model.get('last_checked_at') or _seconds(row[1])>=_seconds(model['last_checked_at'])):
                    model.update(validation_status=row[0],last_checked_at=row[1],last_verified_at=row[2],
                                 validation_source=row[3],last_error_code=None if row[0]=='verified' else 'call_failed')
        return models


def _seconds(value):
    if isinstance(value,(int,float)):return value/1000 if value>1e12 else value
    return datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
