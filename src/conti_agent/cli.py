from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Callable, TextIO

from .collab import CrewManager
from .commands import CommandContext
from .config import load_config
from .errors import ConfigurationError, ContiAgentError
from .runtime import Runtime
from .service import RuntimeService


def build_runtime(workspace: Path, config_path: Path | None,
                  input_function: Callable[[str], str] | None = None,
                  output_function: Callable[[str], None] | None = None) -> Runtime:
    config = load_config(config_path)
    return Runtime(config, workspace,
                   input_function=input_function, output_function=output_function)


def print_error(text: str, stream: TextIO) -> None:
    print(f"错误：{text}", file=stream)


def read_multi_line(input_function: Callable[[str], str]) -> str:
    first = input_function("你 > ")
    while first.endswith("\\"):
        first = first[:-1] + "\n" + input_function("  > ")
    return first


async def run_chat(runtime: Runtime, session_id: str | None,
                   input_function: Callable[[str], str],
                   output_function: Callable[[str], None], *,
                   delta_function: Callable[[str], None] | None = None) -> None:
    info = runtime.describe()
    output_function("conti-agent 终端对话")
    output_function(f"模型：{info['provider']} / {info['model']}")
    output_function(f"权限：{info['permission_mode']}    工作区：{info['workspace']}")
    output_function("直接输入任务；/help 查看命令，/exit 退出。")
    while True:
        try:
            prompt = read_multi_line(input_function)
        except (EOFError, KeyboardInterrupt):
            output_function("")
            return
        if not prompt.strip():
            continue
        if runtime.commands.is_command(prompt):
            context = CommandContext(
                runtime,
                session_id=session_id,
                compact_session=lambda sid: compact_session(runtime, sid),
                undo_checkpoint=runtime.undo_last,
            )
            result = await runtime.commands.execute(prompt, context)
            for line in result.output:
                output_function(line)
            if result.new_session_requested:
                session_id = None
            elif result.session_id is not None:
                session_id = result.session_id
            if result.exit_requested:
                return
            continue
        output_function("助手：")
        streamed = False
        sink = delta_function or (
            lambda text: print(text, end="", flush=True)
        )
        def write_delta(text: str) -> None:
            nonlocal streamed
            streamed = True
            sink(text)
        final, session_id, _ = await runtime.ask(
            prompt, session_id=session_id, output_format="text",
            text_callback=write_delta,
        )
        if not streamed:
            output_function(final)
        if not final:
            output_function("")
        output_function("")


async def run_tui(runtime: Runtime) -> None:
    """启动独立设计的全屏终端界面。"""
    try:
        from .tui import ContiTui, show_startup_logo
    except ImportError as exc:
        raise ContiAgentError(
            "TUI 需要 prompt-toolkit。请执行：pip install -e .[tui]"
        ) from exc
    show_startup_logo()
    interface = ContiTui(runtime)
    try:
        await interface.run_async()
    finally:
        # alternate screen 退出后会还原进入前的启动画面，这里清除残留。
        print("\033[2J\033[H", end="", flush=True)


async def compact_session(runtime: Runtime, session_id: str) -> str:
    """手动压缩：逻辑与自动压缩一致，仅触发方式不同。"""
    _, messages = runtime.sessions.load(session_id)
    return await runtime.compact_messages(messages, session_id, reason="manual")


def make_http_handler(service: RuntimeService):
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def send_json(self, status: int, payload) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        def do_POST(self) -> None:
            length = int(self.request.headers.get("Content-Length", "0"))
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                result = asyncio.run(service.submit(payload))
                self.send_json(200, result)
            except Exception as exc:
                self.send_json(400, {"error": str(exc), "type": type(exc).__name__})
    return Handler


async def run_cli(argv: list[str] | None = None, *,
                  workspace: Path | None = None,
                  input_function: Callable[[str], str] | None = None,
                  output_function: Callable[[str], None] | None = None,
                  error_function: Callable[[str], None] | None = None) -> int:
    workspace = workspace or Path.cwd()
    output = output_function or (lambda text: print(text))
    errors = error_function or (lambda text: print_error(text, sys.stderr))
    parser = argparse.ArgumentParser(prog="conti-agent", description="本地可控的 Python coding agent 运行时")
    parser.add_argument("--config", type=Path, help="显式 TOML 配置路径")
    subparsers = parser.add_subparsers(dest="command")
    ask_parser = subparsers.add_parser("ask", help="执行一次性任务")
    ask_parser.add_argument("prompt")
    ask_parser.add_argument("--session")
    ask_parser.add_argument("--event-format", choices=["text", "jsonl"], default="text")
    chat_parser = subparsers.add_parser("chat", help="启动终端 TUI 对话")
    chat_parser.add_argument("--line", action="store_true", help="使用兼容行式界面")
    chat_parser.add_argument("--tui", action="store_true", help="强制使用全屏 TUI")
    subparsers.add_parser("sessions", help="列出保存的会话")
    subparsers.add_parser("config-check", help="校验配置")
    worker_parser = subparsers.add_parser("worker", help="执行本地协作任务")
    worker_parser.add_argument("--crew", required=True)
    worker_parser.add_argument("--agent", required=True)
    worker_parser.add_argument("--task", required=True)
    serve_parser = subparsers.add_parser("serve", help="启动本地 HTTP 服务")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8791)
    args = parser.parse_args(argv)

    # 打包 exe 后的最简体验：直接双击或运行 conti-agent.exe 进入 chat。
    if not args.command:
        args.command = "chat"
        args.line = False
        args.tui = False
    try:
        runtime = build_runtime(workspace, args.config, input_function, output)
    except (ConfigurationError, ContiAgentError) as exc:
        errors(str(exc))
        return 2

    if args.command != "config-check":
        await runtime.start_external_tools(warn=errors)

    try:
        if args.command == "ask":
            final, _, _ = await runtime.ask(args.prompt, session_id=args.session,
                                            output_format=args.event_format)
            if args.event_format == "text":
                output(final)
            return 0
        if args.command == "chat":
            if args.tui or (not args.line and sys.stdout.isatty()):
                await run_tui(runtime)
            else:
                await run_chat(runtime, None,
                               input_function or (lambda prompt: input(prompt)),
                               output)
            return 0
        if args.command == "sessions":
            for item in runtime.sessions.list():
                output(f"{item['session_id']}  {item['title']}")
            return 0
        if args.command == "config-check":
            output("配置有效")
            return 0
        if args.command == "worker":
            crew = CrewManager(runtime.root / "runtime" / "crews", args.crew)
            crew.update_task(args.task, owner=args.agent, status="doing")
            final, _, _ = await runtime.ask(crew.get_task(args.task).title)
            crew.update_task(args.task, status="done", result=final, owner=args.agent)
            output(final)
            return 0
        if args.command == "serve":
            from http.server import ThreadingHTTPServer
            if args.host not in {"127.0.0.1", "localhost"}:
                errors("非 loopback 绑定需要修改服务实现并显式接受风险")
                return 2
            server = ThreadingHTTPServer((args.host, args.port), make_http_handler(RuntimeService(runtime)))
            output(f"http://{args.host}:{args.port}")
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
            return 0
    except ContiAgentError as exc:
        errors(str(exc))
        return 3
    finally:
        await runtime.close_external_tools()
    return 2


def main() -> None:
    raise SystemExit(asyncio.run(run_cli()))


if __name__ == "__main__":
    main()
