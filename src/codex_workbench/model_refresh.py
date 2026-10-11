"""显式触发的模型发现与验证任务；跨进程互斥，轮询不重复调用供应商。"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import fcntl
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

from .model_catalog import configured_models
from .model_discovery import discover, register_models, SOURCES
from .model_observations import ModelObservations
from .model_status import availability


def _terminate(process):
    """终止验证进程组，包括保险库启动的子进程。"""
    try:os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:return
    try:process.wait(timeout=1)
    except subprocess.TimeoutExpired:pass
    try:os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:pass


class ProbeProcesses:
    """跟踪受控验证进程，关闭后不接受新的付费请求。"""
    def __init__(self):
        self.lock=threading.Lock();self.active=set();self.closed=False

    def run(self, args):
        with self.lock:
            if self.closed:raise InterruptedError('验证已停止')
            process=subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
            self.active.add(process)
        try:
            output,_=process.communicate(timeout=150)
            if self.closed:raise InterruptedError('验证已停止')
            return output
        except subprocess.TimeoutExpired:
            _terminate(process);process.communicate(timeout=2)
            raise
        finally:
            with self.lock:self.active.discard(process)

    def close(self):
        with self.lock:
            self.closed=True;active=list(self.active)
        for process in active:_terminate(process)


def verify_one(model, resources_dir, data_dir, processes=None):
    """保险库将秘密直接送入验证子进程 stdin；父进程只接收固定结果字段。"""
    reference = model.get('credential_id')
    if not reference:
        ModelObservations(data_dir).record(model, success=False, source='probe', error_code='credential_missing')
        return 'missing_credential'
    vault = Path.home()/'.codex/scripts/key-vault/key-vault.sh'
    script = Path(__file__).resolve().parents[2]/'scripts/verify_registered_models.py'
    args = [str(vault), 'exec-stdin', reference, sys.executable, str(script),
            '--resources-dir', str(resources_dir), '--data-dir', str(data_dir),
            '--provider', model['provider_id'], '--credential-id', reference,
            '--model-id', model['id'], '--timeout', '120']
    try:
        output = (processes or ProbeProcesses()).run(args)
        lines = output.decode('utf-8').splitlines()
        if len(output) > 16384 or len(lines) != 1:
            raise ValueError('验证结果无效')
        value = json.loads(lines[0])
        if value.get('model_id') != model['id'] or set(value) != {'model_id','success','status','error_code'}:
            raise ValueError('验证结果不匹配')
        if value['status'] == 'failed':
            ModelObservations(data_dir).record(model, success=False, source='probe', error_code=value['error_code'])
        return availability(value['status'], value['error_code'])
    except InterruptedError:
        return None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        ModelObservations(data_dir).record(model, success=False, source='probe', error_code='verification_worker_failed')
        return 'verification_failed'


class ModelRefresh:
    """单次刷新先发现再验证；其他请求仅查看持久化进度。"""
    def __init__(self, resources_dir, data_dir, notify=lambda:None, *, discoverer=discover, verifier=verify_one):
        self.resources_dir=Path(resources_dir);self.data_dir=Path(data_dir)
        self.path=self.data_dir/'model-refresh.json';self.lock_path=self.data_dir/'model-refresh.lock'
        self.notify=notify;self.discoverer=discoverer;self.verifier=verifier
        self.mutex=threading.RLock();self.thread=None;self.stopping=threading.Event()
        self.processes=ProbeProcesses()

    def status(self):
        """读取进度，并将进程退出遗留的运行状态标记为已中断。"""
        try:
            if self.path.stat().st_size>16384:raise ValueError('进度过大')
            value=json.loads(self.path.read_text())
        except (OSError,ValueError):return {'state':'idle','running':False}
        if value.get('running') and self.lock_path.exists():
            with self.lock_path.open('a') as stream:
                try:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:return value
                value={**value,'state':'interrupted','running':False}
        return value

    def _save(self, value):
        fd,temporary=tempfile.mkstemp(prefix='.model-refresh-',dir=self.data_dir)
        try:
            with os.fdopen(fd,'w') as stream:json.dump(value,stream,ensure_ascii=False)
            os.replace(temporary,self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def start(self):
        """显式点击触发；持有文件锁直到整批任务结束，重复请求复用当前进度。"""
        with self.mutex:
            if self.stopping.is_set():raise ValueError('服务正在关闭')
            self.data_dir.mkdir(parents=True,exist_ok=True)
            fd=os.open(self.lock_path,os.O_CREAT|os.O_RDWR,0o600)
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd);return self.status()
            value={'state':'discovering','running':True,'started_at':datetime.now(timezone.utc).isoformat(),
                   'added':0,'completed':0,'total':0,'providers':{},'counts':{}}
            try:
                self._save(value)
                self.thread=threading.Thread(target=self._run,args=(fd,value),daemon=True,name='model-refresh')
                self.thread.start()
            except Exception:
                os.close(fd)
                raise
            return dict(value)

    def _run(self, fd, value):
        def update(**changes):
            with self.mutex:
                value.update(changes);self._save(value)
            self.notify()
        try:
            for provider in SOURCES:
                if self.stopping.is_set():return
                try:
                    candidates=self.discoverer(provider)
                    added=register_models(self.resources_dir,provider,candidates)
                    value['added']+=added
                    value['providers'][provider]={'state':'ok','added':added}
                except (OSError,ValueError,KeyError,TypeError):
                    value['providers'][provider]={'state':'failed','error':'官方目录暂时无法读取，保留已有模型'}
                update()
            models=[m for m in configured_models(self.resources_dir/'models/catalog.json') if m.get('provider_id') in SOURCES]
            update(state='validating',total=len(models))
            def provider_batch(provider):
                for model in models:
                    if self.stopping.is_set():return
                    if model['provider_id']!=provider:continue
                    state=(verify_one(model,self.resources_dir,self.data_dir,self.processes)
                           if self.verifier is verify_one else self.verifier(model,self.resources_dir,self.data_dir))
                    if self.stopping.is_set():return
                    with self.mutex:
                        value['completed']+=1
                        value['counts'][state]=value['counts'].get(state,0)+1
                        self._save(value)
                    self.notify()
            with ThreadPoolExecutor(max_workers=2,thread_name_prefix='model-validation') as pool:
                list(pool.map(provider_batch,SOURCES))
            update(state='interrupted' if self.stopping.is_set() else 'completed',running=False,
                   finished_at=datetime.now(timezone.utc).isoformat())
        except Exception:
            update(state='failed',running=False,error='刷新未完成，已保留目录与已完成的验证结果')
        finally:
            if value.get('running'):update(state='interrupted',running=False)
            os.close(fd)

    def close(self):
        """停止新的验证并终止活动验证进程，等待工作线程释放刷新锁。"""
        with self.mutex:self.stopping.set()
        self.processes.close()
        if self.thread and self.thread is not threading.current_thread():self.thread.join(5)
