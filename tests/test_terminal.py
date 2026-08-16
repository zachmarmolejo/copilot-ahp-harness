import asyncio
import io
import json
import sys
from pathlib import Path

from copilot_ahp.harness import CopilotHarness
from copilot_ahp.terminal import TerminalOutput, format_usage


class EventClient:
    def __init__(self, actions):
        self.actions = list(actions)
        self.dispatched = []

    async def dispatch(self, channel, action):
        self.dispatched.append((channel, action))

    async def next_event(self, _predicate, timeout):
        assert timeout == 600
        return {
            "params": {
                "channel": "ahp-chat:/test",
                "action": self.actions.pop(0),
            }
        }


class TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_text_turn_keeps_model_response_on_stdout() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    output = TerminalOutput(
        stdout=stdout, stderr=stderr, spinner="never", color="never"
    )
    client = EventClient(
        [
            {"type": "chat/delta", "content": "hello", "turnId": None},
            {
                "type": "chat/usage",
                "turnId": None,
                "usage": {"inputTokens": 4, "outputTokens": 2},
            },
            {"type": "chat/turnComplete", "turnId": None, "duration": 1250},
        ]
    )
    harness = CopilotHarness(client, Path.cwd(), output=output)
    harness.chat_channel = "ahp-chat:/test"

    asyncio.run(harness.send("hi"))

    assert stdout.getvalue() == "hello\n"
    assert "[done] 1.2s - 0 tools - model auto - 4 in, 2 out" in stderr.getvalue()
    assert stderr.getvalue().endswith("\n\n")
    assert "Thinking" not in stderr.getvalue()


def test_jsonl_turn_emits_parseable_events_only() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    output = TerminalOutput(mode="jsonl", stdout=stdout, stderr=stderr)
    client = EventClient(
        [
            {"type": "chat/delta", "content": "hello", "turnId": None},
            {"type": "chat/turnComplete", "turnId": None, "duration": 50},
        ]
    )
    harness = CopilotHarness(client, Path.cwd(), output=output)
    harness.chat_channel = "ahp-chat:/test"

    asyncio.run(harness.send("hi"))

    records = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [record["type"] for record in records] == [
        "turn_started",
        "ahp_action",
        "ahp_action",
        "turn_finished",
    ]
    assert stderr.getvalue() == ""


def test_cancel_active_turn_dispatches_protocol_action() -> None:
    output = TerminalOutput(stdout=io.StringIO(), stderr=io.StringIO())
    client = EventClient([])
    harness = CopilotHarness(client, Path.cwd(), output=output)
    harness.chat_channel = "ahp-chat:/test"
    harness.active_turn_id = "turn-1"
    harness._turn_started = 1.0

    assert asyncio.run(harness.cancel_active_turn()) is True
    assert client.dispatched[0][0] == "ahp-chat:/test"
    assert client.dispatched[0][1]["type"] == "chat/turnCancelled"
    assert client.dispatched[0][1]["turnId"] == "turn-1"
    assert isinstance(client.dispatched[0][1]["duration"], int)
    assert asyncio.run(harness.cancel_active_turn()) is False


def test_format_usage_omits_missing_fields() -> None:
    assert format_usage({"outputTokens": 3}) == "3 out"
    assert format_usage({}) is None


def test_jsonl_human_prompts_use_stderr_without_corrupting_stdout() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    output = TerminalOutput(mode="jsonl", stdout=stdout, stderr=stderr)

    output.info("ordinary status")
    output.human("Choose an option")
    output.prompt("Selection: ")

    assert stdout.getvalue() == ""
    assert stderr.getvalue() == "Choose an option\nSelection: "


def test_tty_input_gives_prompt_to_readline(monkeypatch) -> None:
    stdin = TtyStringIO()
    stdout = TtyStringIO()
    stderr = TtyStringIO()
    received = []

    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(
        "builtins.input", lambda prompt="": received.append(prompt) or "ok"
    )
    output = TerminalOutput(stdout=stdout, stderr=stderr, spinner="never")

    assert output.read_input("> ") == "ok"
    assert received == ["> "]
    assert stderr.getvalue() == ""


def test_redirected_input_keeps_prompt_on_stderr(monkeypatch) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    received = []

    monkeypatch.setattr(
        "builtins.input", lambda prompt="": received.append(prompt) or "ok"
    )
    output = TerminalOutput(stdout=stdout, stderr=stderr, spinner="never")

    assert output.read_input("Selection: ") == "ok"
    assert received == [""]
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == "Selection: "
