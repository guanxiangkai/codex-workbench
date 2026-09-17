"""用户点击后打开 macOS 标准文件夹选择器，不需要安装辅助应用。"""

import subprocess
import sys
from pathlib import Path


def choose_workspace() -> dict:
    """只选择已有工作目录；取消或超时不会创建项目或修改当前选择。"""
    if sys.platform != "darwin":
        raise ValueError("当前系统未接入目录选择器")
    try:
        result = subprocess.run(["/usr/bin/osascript", "-e", 'POSIX path of (choose folder with prompt "选择项目工作区")'],
                                capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired:
        raise ValueError("目录选择已超时，可再次打开选择器") from None
    if result.returncode != 0:
        if "-128" in result.stderr:
            return {"path": None, "cancelled": True}
        raise ValueError("目录选择器未能完成，请重试")
    selected = Path(result.stdout.strip())
    if not selected.is_absolute() or not selected.is_dir():
        raise ValueError("未返回有效的项目目录")
    return {"path": str(selected.resolve()), "cancelled": False}
