#!/usr/bin/env python3
"""从源码目录启动自研 MCP，不安装 Python 包或改写全局解释器。"""

import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from codex_workbench.__main__ import main

if __name__ == "__main__":
    main()
