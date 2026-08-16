from __future__ import annotations

import asyncio
import os
import re
import secrets
import shutil
import signal
import stat
import tempfile
from pathlib import Path
from urllib.parse import quote

DEFAULT_MACOS_CODE = Path(
    "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"
)
HOST_URL_PATTERN = re.compile(r"ws://(?:localhost|127\.0\.0\.1):(?P<port>\d+)")


def find_code() -> str:
    configured = os.environ.get("VSCODE_CLI_PATH")
    if configured:
        return configured
    discovered = shutil.which("code")
    if discovered:
        return discovered
    if DEFAULT_MACOS_CODE.is_file():
        return str(DEFAULT_MACOS_CODE)
    raise RuntimeError("VS Code CLI not found; set VSCODE_CLI_PATH")


class AgentHost:
    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None
        self.url: str | None = None
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        self._drain_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> str:
        token = secrets.token_urlsafe(32)
        self._temporary_directory = tempfile.TemporaryDirectory(prefix="copilot-ahp-")
        token_file = Path(self._temporary_directory.name) / "connection-token"
        token_file.write_text(token, encoding="ascii")
        token_file.chmod(stat.S_IRUSR | stat.S_IWUSR)

        self.process = await asyncio.create_subprocess_exec(
            find_code(),
            "agent",
            "host",
            "--new-instance",
            "--foreground",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--connection-token-file",
            str(token_file),
            "--idle-timeout",
            "300",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=os.name == "posix",
        )
        assert self.process.stdout is not None
        try:
            async with asyncio.timeout(180):
                while line := await self.process.stdout.readline():
                    decoded = line.decode(errors="replace")
                    match = HOST_URL_PATTERN.search(decoded)
                    if match:
                        self.url = (
                            f"ws://127.0.0.1:{match.group('port')}?tkn={quote(token)}"
                        )
                        self._drain_task = asyncio.create_task(
                            self._drain_output(), name="agent-host-output-drain"
                        )
                        return self.url
                return_code = await self.process.wait()
                raise RuntimeError(
                    f"agent host exited before becoming ready ({return_code})"
                )
        except Exception:
            await self.stop()
            raise

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    async def stop(self) -> None:
        process = self.process
        if process is not None and process.returncode is None:
            self._signal_process_tree(process, signal.SIGINT)
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                self._signal_process_tree(process, signal.SIGKILL)
                await process.wait()
        self.process = None
        if self._drain_task is not None:
            self._drain_task.cancel()
            await asyncio.gather(self._drain_task, return_exceptions=True)
            self._drain_task = None
        if self._temporary_directory is not None:
            self._temporary_directory.cleanup()
            self._temporary_directory = None

    @staticmethod
    def _signal_process_tree(
        process: asyncio.subprocess.Process, requested_signal: signal.Signals
    ) -> None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, requested_signal)
            elif requested_signal == signal.SIGKILL:
                process.kill()
            else:
                process.terminate()
        except ProcessLookupError:
            pass

    async def _drain_output(self) -> None:
        if self.process is None or self.process.stdout is None:
            return
        stdout = self.process.stdout
        while await stdout.readline():
            pass
