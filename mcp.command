#!/bin/sh
# 使用已有 Codex 运行时启动本地工作台，不下载或安装依赖。
set -eu
TASK_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TASK_PYTHON=${WORKBENCH_PYTHON:-}
if [ -n "$TASK_PYTHON" ] && [ ! -x "$TASK_PYTHON" ]; then TASK_PYTHON=; fi
if [ -z "$TASK_PYTHON" ] && [ -x "$TASK_ROOT/.venv/bin/python3" ]; then
  TASK_PYTHON="$TASK_ROOT/.venv/bin/python3"
fi
if [ -z "$TASK_PYTHON" ]; then TASK_PYTHON=$(command -v python3 || true); fi
if [ -z "$TASK_PYTHON" ] || ! "$TASK_PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)'; then
  echo 'Codex 工作台需要已有 Python 3.11+；没有安装新运行时。' >&2
  exit 1
fi
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUTF8=1
exec "$TASK_PYTHON" -u "$TASK_ROOT/launch.py" "$@"
