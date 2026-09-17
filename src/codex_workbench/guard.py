"""执行进程守卫：控制管道关闭或收到终止信号时，结束本次 CLI 进程组。"""

import os
import selectors
import signal
import subprocess
import sys


def main() -> int:
    """控制 fd 由父进程创建并继承；不接受网络命令，也不读取认证材料。"""
    descriptor = int(sys.argv[1])
    command = sys.argv[2:]
    stopped = False
    def stop(signum, frame):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    watcher = selectors.DefaultSelector()
    watcher.register(descriptor, selectors.EVENT_READ)
    process = None
    try:
        # 父进程若在守卫启动前退出，控制端已经 EOF，不应再派发 CLI。
        if watcher.select(0):
            return 1
        process = subprocess.Popen(command, start_new_session=True)
        while process.poll() is None and not stopped:
            if watcher.select(0.1):
                break
        return process.returncode if process.returncode is not None and process.returncode >= 0 else 1
    finally:
        watcher.close()
        os.close(descriptor)
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=0.8)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


if __name__ == "__main__":
    sys.exit(main())
