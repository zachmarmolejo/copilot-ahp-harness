from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .ahp import AhpClient, AhpError
from .harness import CopilotHarness
from .host import AgentHost
from .terminal import TerminalOutput

try:
    import readline
except ImportError:  # pragma: no cover - readline is unavailable on Windows
    readline = None


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Drive GitHub Copilot through VS Code's Agent Host Protocol, outside the VS Code UI."
    )
    result.add_argument(
        "prompt", nargs="?", help="one-shot prompt; omit to enter an interactive chat"
    )
    result.add_argument(
        "--query",
        help='one-shot prompt, for example: --query "Inspect this project"',
    )
    result.add_argument("--workspace", type=Path, default=Path.cwd())
    result.add_argument(
        "--model",
        default="auto",
        help="model ID from --list-models (default: auto)",
    )
    result.add_argument(
        "--list-models",
        action="store_true",
        help="show models advertised by the Copilot Agent Host and exit",
    )
    result.add_argument("--provider", default="copilotcli")
    result.add_argument(
        "--approval",
        choices=("prompt", "deny", "all"),
        default="prompt",
        help="tool approval policy (default: prompt)",
    )
    result.add_argument(
        "--connect",
        metavar="WS_URL",
        help="connect to an existing AHP host instead of starting one",
    )
    result.add_argument(
        "--host-token-env",
        default="AHP_CONNECTION_TOKEN",
        help="environment variable containing the transport token for --connect",
    )
    result.add_argument(
        "--keep-session",
        action="store_true",
        help="do not dispose the AHP session on exit",
    )
    result.add_argument(
        "--output",
        choices=("text", "jsonl"),
        default="text",
        help="output format (default: text)",
    )
    result.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="terminal color policy (default: auto)",
    )
    result.add_argument(
        "--spinner",
        choices=("auto", "always", "never"),
        default="auto",
        help="working spinner policy (default: auto)",
    )
    result.add_argument("--debug-events", action="store_true")
    return result


def connection_url(url: str, token_environment: str) -> str:
    token = os.environ.get(token_environment)
    if not token:
        return url
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault("tkn", token)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


async def run_turn(harness: CopilotHarness, prompt: str) -> None:
    loop = asyncio.get_running_loop()
    previous_handler = signal.getsignal(signal.SIGINT)
    interrupted = False

    def handle_interrupt() -> None:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            loop.create_task(harness.cancel_active_turn())
            return
        loop.remove_signal_handler(signal.SIGINT)
        signal.signal(signal.SIGINT, previous_handler)
        signal.raise_signal(signal.SIGINT)

    installed = False
    try:
        loop.add_signal_handler(signal.SIGINT, handle_interrupt)
        installed = True
    except (NotImplementedError, RuntimeError):
        pass
    try:
        await harness.send(prompt)
    finally:
        if installed:
            loop.remove_signal_handler(signal.SIGINT)
            signal.signal(signal.SIGINT, previous_handler)


async def read_line(output: TerminalOutput, prompt: str) -> str:
    return await asyncio.to_thread(output.read_input, prompt)


async def read_multiline(output: TerminalOutput) -> str:
    output.human(
        "Enter a multiline prompt. Finish with a line containing only a period."
    )
    lines = []
    while True:
        line = await read_line(output, "... ")
        if line == ".":
            return "\n".join(lines)
        lines.append(line)


def print_help(output: TerminalOutput) -> None:
    output.human("Commands:", color="blue")
    output.human("  /help             Show this help")
    output.human("  /model             List available models")
    output.human("  /model MODEL_ID    Select a model")
    output.human("  /status            Show workspace, policy, model, and session")
    output.human("  /paste             Enter a multiline prompt; finish with .")
    output.human("  /new               Dispose this session and create a new one")
    output.human("  /exit, /quit       Exit")


async def run(args: argparse.Namespace) -> None:
    if not args.workspace.is_dir():
        raise AhpError(f"workspace is not a directory: {args.workspace}")

    output = TerminalOutput(mode=args.output, color=args.color, spinner=args.spinner)
    async with AsyncExitStack() as stack:
        if args.connect:
            url = connection_url(args.connect, args.host_token_env)
        else:
            url = await stack.enter_async_context(AgentHost())
        client = await stack.enter_async_context(
            AhpClient(url, debug_events=args.debug_events)
        )
        harness = CopilotHarness(
            client,
            args.workspace,
            provider=args.provider,
            model=args.model,
            approval_mode=args.approval,
            output=output,
        )
        await harness.initialize()
        if args.list_models:
            harness.print_models()
            return
        harness.print_selected_model()
        await harness.create_session()
        try:
            query = args.query or args.prompt
            if query == "-":
                query = sys.stdin.read()
            if query:
                await run_turn(harness, query)
                return
            output.human("Connected to Copilot through AHP. Type /help for commands.")
            while True:
                prompt = await read_line(output, "> ")
                command = prompt.strip()
                if command in {"/exit", "/quit"}:
                    return
                if command == "/help":
                    print_help(output)
                    continue
                if command == "/status":
                    harness.print_status()
                    continue
                if command == "/model":
                    harness.print_models()
                    continue
                if command.startswith("/model "):
                    harness.select_model(command.removeprefix("/model ").strip())
                    continue
                if command == "/paste":
                    prompt = await read_multiline(output)
                elif command == "/new":
                    await harness.dispose_session()
                    await harness.create_session()
                    output.human("[session] Started a new session.", color="green")
                    continue
                elif command.startswith("/"):
                    output.human(
                        f"Unknown command: {command}. Type /help.", color="red"
                    )
                    continue
                if prompt.strip():
                    if output.mode == "text":
                        output.info("")
                    output.human("copilot>", color="blue")
                    await run_turn(harness, prompt)
        finally:
            output.close()
            if not args.keep_session:
                await harness.dispose_session()


def main() -> None:
    if readline is not None:
        readline.set_history_length(1000)
    argument_parser = parser()
    args = argument_parser.parse_args()
    if args.query and args.prompt:
        argument_parser.error("use either --query or the positional prompt, not both")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130) from None
    except (AhpError, TimeoutError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
