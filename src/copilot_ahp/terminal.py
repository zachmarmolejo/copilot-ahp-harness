from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, ClassVar, TextIO


class TerminalOutput:
    """Keep model output, transient status, and machine events separate."""

    COLORS: ClassVar[dict[str, str]] = {
        "dim": "\x1b[2m",
        "blue": "\x1b[36m",
        "green": "\x1b[32m",
        "yellow": "\x1b[33m",
        "red": "\x1b[31m",
    }
    RESET = "\x1b[0m"
    FRAMES = ("|", "/", "-", "\\")

    def __init__(
        self,
        *,
        mode: str = "text",
        color: str = "auto",
        spinner: str = "auto",
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
    ) -> None:
        self.mode = mode
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr
        self._stderr_tty = bool(getattr(self.stderr, "isatty", lambda: False)())
        color_allowed = (
            "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
        )
        self.color_enabled = mode == "text" and (
            color == "always"
            or (color == "auto" and self._stderr_tty and color_allowed)
        )
        self.spinner_enabled = mode == "text" and (
            spinner == "always" or (spinner == "auto" and self._stderr_tty)
        )
        if spinner == "never":
            self.spinner_enabled = False
        self._status_task: asyncio.Task[None] | None = None
        self._status_text = ""
        self._status_started = 0.0
        self._status_visible = False
        self._response_open = False
        self._human_input = False

    def styled(self, value: str, color: str) -> str:
        if not self.color_enabled:
            return value
        return f"{self.COLORS[color]}{value}{self.RESET}"

    def event(self, event_type: str, **fields: Any) -> None:
        if self.mode != "jsonl":
            return
        payload = {"type": event_type, **fields}
        self.stdout.write(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=True) + "\n"
        )
        self.stdout.flush()

    def raw_event(self, action: dict[str, Any]) -> None:
        self.event("ahp_action", action=action)

    def response(self, content: str) -> None:
        if self.mode != "text" or not content:
            return
        self.stop_status()
        self.stdout.write(content)
        self.stdout.flush()
        self._response_open = True

    def finish_response(self) -> None:
        if self.mode == "text" and self._response_open:
            self.stdout.write("\n")
            self.stdout.flush()
        self._response_open = False

    def info(self, message: str, *, color: str = "dim") -> None:
        if self.mode != "text" and not self._human_input:
            return
        self.stop_status()
        self.stderr.write(self.styled(message, color) + "\n")
        self.stderr.flush()

    @contextmanager
    def human_input(self) -> Iterator[None]:
        previous = self._human_input
        self._human_input = True
        try:
            yield
        finally:
            self._human_input = previous

    def human(self, message: str, *, color: str = "dim") -> None:
        with self.human_input():
            self.info(message, color=color)

    def prompt(self, message: str) -> None:
        self.stop_status()
        self.stderr.write(message)
        self.stderr.flush()

    def read_input(self, prompt: str) -> str:
        """Read a line without letting readline erase a manually drawn prompt."""
        self.stop_status()
        stdin_tty = bool(getattr(sys.stdin, "isatty", lambda: False)())
        stdout_tty = bool(getattr(self.stdout, "isatty", lambda: False)())
        if (
            self.mode == "text"
            and self.stdout is sys.stdout
            and stdin_tty
            and stdout_tty
        ):
            # readline must receive the real prompt so its redisplay logic knows
            # which columns are editable and never backspaces over the prefix.
            return input(prompt)
        self.prompt(prompt)
        return input()

    def start_status(self, message: str) -> None:
        if self.mode != "text":
            return
        self.stop_status()
        self._status_text = message
        self._status_started = time.monotonic()
        if self.spinner_enabled:
            self._status_task = asyncio.create_task(self._spin())

    def update_status(self, message: str) -> None:
        if self.mode != "text":
            return
        self._status_text = message

    def stop_status(self) -> None:
        task = self._status_task
        self._status_task = None
        if task:
            task.cancel()
        if self._status_visible:
            self.stderr.write("\r\x1b[2K")
            self.stderr.flush()
            self._status_visible = False

    async def _spin(self) -> None:
        index = 0
        try:
            while True:
                elapsed = time.monotonic() - self._status_started
                status = f"{self.FRAMES[index]} {self._status_text} {elapsed:.1f}s"
                self.stderr.write("\r\x1b[2K" + self.styled(status, "blue"))
                self.stderr.flush()
                self._status_visible = True
                index = (index + 1) % len(self.FRAMES)
                await asyncio.sleep(0.12)
        except asyncio.CancelledError:
            return

    def close(self) -> None:
        self.stop_status()
        self.finish_response()


def format_usage(usage: dict[str, Any] | None) -> str | None:
    if not usage:
        return None
    values = []
    if (input_tokens := usage.get("inputTokens")) is not None:
        values.append(f"{input_tokens} in")
    if (output_tokens := usage.get("outputTokens")) is not None:
        values.append(f"{output_tokens} out")
    if (cache_tokens := usage.get("cacheReadTokens")) is not None:
        values.append(f"{cache_tokens} cached")
    return ", ".join(values) or None
