import asyncio
from pathlib import Path

from copilot_ahp.cli import connection_url, parser
from copilot_ahp.harness import CopilotHarness, root_state


class RecordingClient:
    def __init__(self) -> None:
        self.dispatched = []

    async def dispatch(self, channel, action) -> None:
        self.dispatched.append((channel, action))


def test_root_state_selects_root_snapshot() -> None:
    result = {
        "snapshots": [
            {"resource": "ahp-session:/ignored", "state": {"wrong": True}},
            {"resource": "ahp-root://", "state": {"agents": ["copilot"]}},
        ]
    }
    assert root_state(result) == {"agents": ["copilot"]}


def test_connection_url_adds_token(monkeypatch) -> None:
    monkeypatch.setenv("TEST_AHP_TOKEN", "a token")
    assert connection_url("ws://127.0.0.1:1234", "TEST_AHP_TOKEN") == (
        "ws://127.0.0.1:1234?tkn=a+token"
    )


def test_connection_url_preserves_existing_transport_token(monkeypatch) -> None:
    monkeypatch.setenv("TEST_AHP_TOKEN", "replacement")
    assert connection_url("ws://127.0.0.1:1234?tkn=original", "TEST_AHP_TOKEN") == (
        "ws://127.0.0.1:1234?tkn=original"
    )


def test_query_argument() -> None:
    arguments = parser().parse_args(["--query", "Inspect this project"])
    assert arguments.query == "Inspect this project"
    assert arguments.prompt is None


def test_model_arguments() -> None:
    arguments = parser().parse_args(["--list-models", "--model", "gpt-example"])
    assert arguments.list_models is True
    assert arguments.model == "gpt-example"


def test_output_arguments() -> None:
    arguments = parser().parse_args(
        ["--output", "jsonl", "--color", "never", "--spinner", "always"]
    )
    assert arguments.output == "jsonl"
    assert arguments.color == "never"
    assert arguments.spinner == "always"


def test_ask_user_single_select_submission() -> None:
    client = RecordingClient()
    harness = CopilotHarness(client, Path.cwd())
    harness.chat_channel = "ahp-chat:/test"

    async def select_first(_prompt: str) -> str:
        return "1"

    harness._read_input = select_first
    asyncio.run(
        harness._handle_input_request(
            {
                "id": "request-1",
                "questions": [
                    {
                        "id": "color",
                        "kind": "single-select",
                        "message": "Choose a color",
                        "required": True,
                        "options": [
                            {"id": "red", "label": "Red", "recommended": True},
                            {"id": "blue", "label": "Blue"},
                        ],
                    }
                ],
            }
        )
    )

    assert client.dispatched == [
        (
            "ahp-chat:/test",
            {
                "type": "chat/inputCompleted",
                "requestId": "request-1",
                "response": "accept",
                "answers": {
                    "color": {
                        "state": "submitted",
                        "value": {"kind": "selected", "value": "red"},
                    }
                },
            },
        )
    ]
