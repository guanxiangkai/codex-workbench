from __future__ import annotations
import base64,binascii,hashlib,mimetypes,os,sqlite3,threading,uuid,stat,re
from pathlib import Path
from datetime import datetime,timezone
MAX_BYTES=32*1024*1024
class PlanningAssets:
 def __init__(self,root,db,lock=None):
  self.lock=lock or threading.RLock(); self.db=db; self.root=Path(root).expanduser()
  if self.root.exists() and self.root.is_symlink():raise ValueError('symlink_root')
  self.root.mkdir(parents=True,exist_ok=True,mode=0o700); os.chmod(self.root,0o700); self.root=self.root.resolve()
  with self.lock:
   self.db.executescript('''CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY,sha256 TEXT NOT NULL,mime TEXT NOT NULL,bytes INTEGER NOT NULL,name TEXT NOT NULL,version INTEGER NOT NULL,source_kind TEXT NOT NULL,source_id TEXT,task_id TEXT,run_id TEXT,created_at TEXT NOT NULL,path TEXT NOT NULL);CREATE TABLE IF NOT EXISTS asset_tasks(asset_id TEXT NOT NULL,task_id TEXT NOT NULL,PRIMARY KEY(asset_id,task_id));CREATE TABLE IF NOT EXISTS asset_refs(asset_id TEXT NOT NULL,task_id TEXT,run_id TEXT,source_kind TEXT,source_id TEXT,created_at TEXT NOT NULL,PRIMARY KEY(asset_id,task_id,run_id,source_kind,source_id));''');self.db.commit()
 def _name(self,n):
  if not isinstance(n,str) or not n or len(n)>255 or any(ord(c)<32 for c in n) or '\\' in n or Path(n).name!=n or '..' in Path(n).parts:raise ValueError('invalid_asset_name')
  return n
 def _storage_name(self,aid,name):
  suffix=Path(name).suffix.lower()
  if not re.fullmatch(r'\.[a-z0-9]{1,16}',suffix): suffix='.bin'
  return aid+suffix
 def _reject_symlink_components(self,path):
  cur=Path(path.anchor or os.sep)
  for part in path.parts[1:] if path.is_absolute() else path.parts:
   cur=cur/part
   try:
    if os.path.islink(cur): raise ValueError('symlink_forbidden')
   except OSError: pass
 def add_bytes(self,data,*,name,mime=None,source_kind='input',source_id=None,task_id=None,run_id=None):
  if not isinstance(data,(bytes,bytearray)) or len(data)>MAX_BYTES:raise ValueError('asset_too_large')
  name=self._name(name); digest=hashlib.sha256(data).hexdigest()
  with self.lock:
   row=self.db.execute('SELECT id,version FROM assets WHERE sha256=? AND name=? ORDER BY version DESC LIMIT 1',(digest,name)).fetchone(); now=datetime.now(timezone.utc).isoformat()
   if row:
    aid,ver=row;self.db.execute('INSERT OR IGNORE INTO asset_tasks VALUES(?,?)',(aid,task_id)) if task_id else None;self._ref(aid,task_id,run_id,source_kind,source_id,now);self.db.commit();return self.get(aid)
   aid='asset_'+uuid.uuid4().hex; target=self.root/self._storage_name(aid,name);tmp=self.root/f'.{aid}.tmp'; version=(self.db.execute('SELECT COALESCE(MAX(version),0)+1 FROM assets WHERE name=?',(name,)).fetchone()[0])
   try:
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
    os.replace(tmp,target);self.db.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(aid,digest,mime or mimetypes.guess_type(name)[0] or 'application/octet-stream',len(data),name,version,source_kind,source_id,task_id,run_id,now,target.name));
    if task_id:self.db.execute('INSERT OR IGNORE INTO asset_tasks VALUES(?,?)',(aid,task_id))
    self._ref(aid,task_id,run_id,source_kind,source_id,now);self.db.commit();return self.get(aid)
   except Exception:
    self.db.rollback()
    try:tmp.unlink()
    except OSError:pass
    try:target.unlink()
    except OSError:pass
    raise
 def _ref(self,aid,task_id,run_id,source_kind,source_id,now):
  row=self.db.execute('SELECT 1 FROM asset_refs WHERE asset_id=? AND task_id IS ? AND run_id IS ? AND source_kind IS ? AND source_id IS ?',(aid,task_id,run_id,source_kind,source_id)).fetchone()
  if not row:self.db.execute('INSERT INTO asset_refs VALUES(?,?,?,?,?,?)',(aid,task_id,run_id,source_kind,source_id,now))
 def add_base64(self,encoded,**kw):
  if not isinstance(encoded,str) or len(encoded)>((MAX_BYTES+2)//3)*4+4:raise ValueError('asset_too_large')
  try:data=base64.b64decode(encoded,validate=True)
  except (ValueError,binascii.Error):raise ValueError('invalid_base64') from None
  return self.add_bytes(data,**kw)
 def add_path(self,path,*,allowed_root,**kw):
  if allowed_root is None:raise ValueError('allowed_root_required')
  root=Path(allowed_root); p=Path(path)
  if not root.is_absolute() or not p.is_absolute() or '..' in p.parts or '..' in root.parts:raise ValueError('path_outside_allowed_root')
  self._reject_symlink_components(root);self._reject_symlink_components(p)
  try: rel=p.relative_to(root)
  except ValueError:raise ValueError('path_outside_allowed_root') from None
  fdroot=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
  try:
   curfd=fdroot
   parts=rel.parts
   if not parts: raise ValueError('invalid_asset_path')
   for part in parts[:-1]:
    nxt=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=curfd)
    if curfd!=fdroot:os.close(curfd)
    curfd=nxt
   fd=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=curfd)
   try:
    st=os.fstat(fd)
    import stat
    if not stat.S_ISREG(st.st_mode) or st.st_size>MAX_BYTES:raise ValueError('invalid_asset_path')
    with os.fdopen(fd,'rb',closefd=True) as stream:data=stream.read(MAX_BYTES+1)
    fd=None
   finally:
    if fd is not None:
     try:os.close(fd)
     except OSError:pass
  except OSError:raise ValueError('invalid_asset_path') from None
  finally:
   try:os.close(curfd)
   except OSError:pass
   if curfd!=fdroot:
    try:os.close(fdroot)
    except OSError:pass
  if len(data)>MAX_BYTES:raise ValueError('asset_too_large')
  return self.add_bytes(data,name=kw.pop('name',p.name),mime=kw.pop('mime',None),**kw)
 def _read_rel(self,rel,include_content):
  if rel.is_absolute() or not rel.parts or any(x in ('..','') for x in rel.parts):raise ValueError('asset_path_invalid')
  rootfd=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
  curfd=rootfd; fd=None
  try:
   for part in rel.parts[:-1]:
    nxt=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=curfd)
    if curfd!=rootfd: os.close(curfd)
    curfd=nxt
   fd=os.open(rel.parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=curfd)
   st=os.fstat(fd)
   if not stat.S_ISREG(st.st_mode): raise ValueError('asset_integrity_error')
   if not include_content:return None
   with os.fdopen(fd,'rb',closefd=True) as stream:
    data=stream.read(MAX_BYTES+1)
   fd=None
   if len(data)>MAX_BYTES:raise ValueError('asset_too_large')
   return data
  except OSError: raise ValueError('asset_integrity_error') from None
  finally:
   if fd is not None:
    try: os.close(fd)
    except OSError: pass
   if curfd!=rootfd:
    try: os.close(curfd)
    except OSError: pass
   try: os.close(rootfd)
   except OSError: pass
 def get(self,aid,*,include_content=False):
  with self.lock:
   cur=self.db.execute('SELECT * FROM assets WHERE id=?',(aid,)); r=cur.fetchone()
   if not r:raise KeyError('asset_not_found')
   d=dict(r) if hasattr(r,'keys') else dict(zip([x[0] for x in cur.description],r)); rel=Path(d.pop('path'))
   content=self._read_rel(rel,include_content)
   rc=self.db.execute('SELECT task_id,run_id,source_kind,source_id,created_at FROM asset_refs WHERE asset_id=?',(aid,)); refs=[dict(zip([z[0] for z in rc.description],x)) for x in rc.fetchall()];d['refs']=refs
   if include_content:
    if hashlib.sha256(content).hexdigest()!=d['sha256']:raise ValueError('asset_integrity_error')
    d['content']=content
   return d
 def list(self,task_id=None,offset=0,limit=100):
  if not isinstance(offset,int) or not isinstance(limit,int) or offset<0 or not 1<=limit<=500:raise ValueError('invalid_pagination')
  with self.lock:
   q=('SELECT DISTINCT a.id FROM assets a JOIN asset_refs r ON r.asset_id=a.id WHERE r.task_id=? ORDER BY a.created_at LIMIT ? OFFSET ?' if task_id else 'SELECT id FROM assets ORDER BY created_at LIMIT ? OFFSET ?');args=(task_id,limit,offset) if task_id else (limit,offset)
   return [self.get(x[0]) for x in self.db.execute(q,args)]
 def link(self,aid,task_id,run_id=None,source_kind='link',source_id=None):
  with self.lock:self.get(aid);now=datetime.now(timezone.utc).isoformat();self.db.execute('INSERT OR IGNORE INTO asset_tasks VALUES(?,?)',(aid,task_id));self._ref(aid,task_id,run_id,source_kind,source_id,now);self.db.commit();return self.get(aid)
 def unlink_task(self,aid,task_id):
  with self.lock:self.db.execute('DELETE FROM asset_tasks WHERE asset_id=? AND task_id=?',(aid,task_id));self.db.execute('DELETE FROM asset_refs WHERE asset_id=? AND task_id=?',(aid,task_id));self.db.commit()
