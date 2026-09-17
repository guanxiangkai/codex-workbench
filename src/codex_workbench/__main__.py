"""MCP、运行核心和本机预览入口。"""
import argparse
import json
import os
from pathlib import Path
from .catalog import VERSION
from .runtime import RuntimeClient, default_data_dir, default_resources_dir, serve


def main():
    """默认启动 stdio MCP，不安装依赖或复制登录态。"""
    parser = argparse.ArgumentParser(description="Codex 工作台 MCP")
    parser.add_argument("command", nargs="?", choices=["mcp", "runtime", "preview", "status"], default="mcp")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--resources-dir", type=Path, default=default_resources_dir())
    parser.add_argument("--codex", default="/Applications/ChatGPT.app/Contents/Resources/codex" if Path("/Applications/ChatGPT.app/Contents/Resources/codex").exists() else "codex")
    parser.add_argument("--preview-port", type=int, help="runtime 的受管回环预览端口；临时测试可传 0")
    parser.add_argument("--page", choices=["workbench"], default="workbench")
    parser.add_argument("--entry-revision", help=argparse.SUPPRESS)
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == "runtime":
        return serve(args.data_dir, args.resources_dir, args.codex, args.preview_port)
    client = RuntimeClient(args.data_dir, args.resources_dir, args.codex)
    if args.command == "mcp":
        from .mcp import serve_stdio
        return serve_stdio(client, args.page)
    if args.command == "status":
        data = client.manifest("workbench")
        print(json.dumps({"version":VERSION,"read_only":True,"entry":data["entry"],"revision":data["revision"],"tools":len(data["tools"])},ensure_ascii=False))
        return
    if args.preview_port is not None:
        parser.error("--preview-port 只能与 runtime 一起使用")
    print(client.preview_url(), flush=True)


if __name__ == "__main__":
    main()
