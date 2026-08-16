from __future__ import annotations

import asyncio
import os
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .ahp import ROOT_CHANNEL, AhpClient, AhpError, action_from, channel_from
from .terminal import TerminalOutput, format_usage


def github_token() -> str:
    for variable in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        if token := os.environ.get(variable):
            return token
    try:
        completed = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise AhpError(
            "GitHub authentication unavailable. Set COPILOT_GITHUB_TOKEN or run `gh auth login`."
        ) from exc
    token = completed.stdout.strip()
    if not token:
        raise AhpError("GitHub CLI returned an empty token")
    return token


def root_state(initialize_result: dict[str, Any]) -> dict[str, Any]:
    for snapshot in initialize_result.get("snapshots", []):
        if snapshot.get("resource") == ROOT_CHANNEL:
            return snapshot.get("state", {})
    return {}


def display_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("value") or value.get("markdown") or value)
    return str(value)


class InputRequestCompletion(Exception):
    def __init__(self, response: str) -> None:
        self.response = response


class CopilotHarness:
    def __init__(
        self,
        client: AhpClient,
        workspace: Path,
        *,
        provider: str = "copilotcli",
        model: str = "auto",
        approval_mode: str = "prompt",
        output: TerminalOutput | None = None,
    ) -> None:
        self.client = client
        self.workspace = workspace.resolve()
        self.provider = provider
        self.model = model
        self.approval_mode = approval_mode
        self.output = output or TerminalOutput()
        self.session_channel: str | None = None
        self.chat_channel: str | None = None
        self.available_models: list[dict[str, Any]] = []
        self._tool_names: dict[str, str] = {}
        self.active_turn_id: str | None = None
        self._turn_started = 0.0
        self._cancel_requested = False
        self.last_usage: dict[str, Any] | None = None

    async def initialize(self) -> None:
        client_id = f"python-copilot-ahp-{uuid.uuid4()}"
        result = await self.client.request(
            "initialize",
            {
                "channel": ROOT_CHANNEL,
                "clientId": client_id,
                "clientInfo": {"name": "copilot-ahp-harness", "version": "0.1.0"},
                "protocolVersions": ["0.8.0"],
                "initialSubscriptions": [ROOT_CHANNEL],
            },
        )
        if result.get("protocolVersion") != "0.8.0":
            raise AhpError(f"unsupported AHP version: {result.get('protocolVersion')}")

        state = root_state(result)
        await self.client.request(
            "authenticate",
            {
                "channel": ROOT_CHANNEL,
                "resource": "https://api.github.com",
                "token": github_token(),
            },
        )
        agents = state.get("agents", [])
        if not self._provider_ready(agents):
            event = await self.client.next_event(
                lambda candidate: (
                    action_from(candidate).get("type") == "root/agentsChanged"
                    and self._provider_ready(action_from(candidate).get("agents", []))
                ),
                timeout=60,
            )
            agents = action_from(event).get("agents", [])
        provider = next(
            agent for agent in agents if agent.get("provider") == self.provider
        )
        self.available_models = provider.get("models", [])
        model_ids = {model.get("id") for model in self.available_models}
        if self.model not in model_ids:
            raise AhpError(
                f"model {self.model!r} is unavailable; available models: {sorted(model_ids)}. "
                "Run with --list-models to inspect the live catalog."
            )

    def print_models(self) -> None:
        if self.output.mode == "jsonl":
            self.output.event(
                "models", provider=self.provider, models=self.available_models
            )
            return
        print(f"Models advertised by provider {self.provider}:")
        for model in self.available_models:
            details = []
            if family := model.get("family"):
                details.append(str(family))
            if version := model.get("version"):
                details.append(str(version))
            suffix = f" ({', '.join(details)})" if details else ""
            print(f"  {model.get('id')}: {model.get('name', model.get('id'))}{suffix}")

    def print_selected_model(self) -> None:
        selected = next(
            model for model in self.available_models if model.get("id") == self.model
        )
        name = selected.get("name", self.model)
        self.output.info(f"[model] {name} ({self.model})", color="blue")
        self.output.event("model", id=self.model, name=name)

    def select_model(self, model: str) -> None:
        available = {item.get("id"): item for item in self.available_models}
        if model not in available:
            raise AhpError(
                f"model {model!r} is unavailable; available models: {sorted(available)}"
            )
        self.model = model
        self.print_selected_model()

    def print_status(self) -> None:
        fields = {
            "model": self.model,
            "workspace": str(self.workspace),
            "approval": self.approval_mode,
            "session": self.session_channel,
            "activeTurn": self.active_turn_id,
        }
        if self.output.mode == "jsonl":
            self.output.event("status", **fields)
            return
        self.output.info(f"Model: {self.model}")
        self.output.info(f"Workspace: {self.workspace}")
        self.output.info(f"Approval: {self.approval_mode}")
        self.output.info(f"Session: {self.session_channel or 'not created'}")
        self.output.info(f"Turn: {self.active_turn_id or 'idle'}")

    def _provider_ready(self, agents: list[dict[str, Any]]) -> bool:
        return any(
            agent.get("provider") == self.provider and agent.get("models")
            for agent in agents
        )

    async def create_session(self) -> None:
        # VS Code 1.133/AHP 0.8 currently expects the provider's legacy URI
        # scheme even though the generic AHP guide documents ahp-session:/.
        self.session_channel = f"{self.provider}:/{uuid.uuid4()}"
        auto_approve = "autoApprove" if self.approval_mode == "all" else "default"
        await self.client.request(
            "createSession",
            {
                "channel": self.session_channel,
                "provider": self.provider,
                "workingDirectories": [self.workspace.as_uri()],
                "config": {"mode": "interactive", "autoApprove": auto_approve},
                "progressToken": f"create-{uuid.uuid4()}",
            },
            timeout=180,
        )
        result = await self.client.request(
            "subscribe", {"channel": self.session_channel}
        )
        snapshot = result.get("snapshot", {})
        state = snapshot.get("state", {})
        self.chat_channel = state.get("defaultChat")
        # Copilot sessions are deliberately provisional until their first
        # message materializes the underlying SDK chat. They remain in the
        # "creating" lifecycle while already exposing a usable default chat.
        if not self.chat_channel and state.get("lifecycle") != "ready":
            event = await self.client.next_event(
                lambda candidate: (
                    channel_from(candidate) == self.session_channel
                    and action_from(candidate).get("type")
                    in {"session/ready", "session/creationFailed"}
                ),
                timeout=180,
            )
            action = action_from(event)
            if action.get("type") == "session/creationFailed":
                raise AhpError(f"session creation failed: {action.get('error')}")
        if not self.chat_channel:
            refreshed = await self.client.request(
                "subscribe", {"channel": self.session_channel}
            )
            self.chat_channel = (
                refreshed.get("snapshot", {}).get("state", {}).get("defaultChat")
            )
        if not self.chat_channel:
            raise AhpError("session became ready without a default chat")
        await self.client.request("subscribe", {"channel": self.chat_channel})

    async def dispose_session(self) -> None:
        if self.session_channel:
            await self.client.request(
                "disposeSession", {"channel": self.session_channel}
            )

    async def send(self, prompt: str) -> None:
        if not self.chat_channel:
            raise AhpError("session has not been created")
        turn_id = str(uuid.uuid4())
        self.active_turn_id = turn_id
        self._turn_started = time.monotonic()
        self._cancel_requested = False
        self.last_usage = None
        tool_count = 0
        self.output.event("turn_started", turnId=turn_id, model=self.model)
        self.output.start_status("Thinking (Ctrl-C to cancel)")
        await self.client.dispatch(
            self.chat_channel,
            {
                "type": "chat/turnStarted",
                "turnId": turn_id,
                "startedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "message": {
                    "text": prompt,
                    "origin": {"kind": "user"},
                    "model": {"id": self.model},
                },
            },
        )
        while True:
            event = await self.client.next_event(
                lambda candidate: channel_from(candidate) == self.chat_channel,
                timeout=600,
            )
            action = action_from(event)
            if action.get("turnId") not in (None, turn_id):
                continue
            action_type = action.get("type")
            self.output.raw_event(action)
            if action_type == "chat/responsePart":
                part = action.get("part", {})
                if part.get("kind") == "markdown":
                    self.output.response(part.get("content", ""))
            elif action_type == "chat/delta":
                self.output.response(action.get("content", ""))
            elif action_type == "chat/activityChanged":
                if activity := action.get("activity"):
                    self.output.start_status(str(activity))
                else:
                    self.output.stop_status()
            elif action_type == "chat/usage":
                self.last_usage = action.get("usage")
            elif action_type == "chat/toolCallStart":
                tool_count += 1
                tool_id = action.get("toolCallId", "unknown")
                self._tool_names[tool_id] = (
                    action.get("displayName") or action.get("toolName") or tool_id
                )
                name = self._tool_names[tool_id]
                self.output.info(f"[tool] {name}", color="yellow")
                self.output.start_status(f"Running {name} (Ctrl-C to cancel)")
            elif action_type == "chat/toolCallReady" and not action.get("confirmed"):
                await self._confirm_tool(turn_id, action)
            elif action_type == "chat/toolCallComplete":
                tool_id = action.get("toolCallId", "unknown")
                success = action.get("result", {}).get("success")
                result = "ok" if success else "failed"
                self.output.info(
                    f"[tool {result}] {self._tool_names.get(tool_id, tool_id)}",
                    color="green" if success else "red",
                )
                self.output.start_status("Thinking (Ctrl-C to cancel)")
            elif action_type == "chat/inputRequested":
                await self._handle_input_request(action.get("request", {}))
            elif action_type == "chat/error":
                self.output.stop_status()
                raise AhpError(f"Copilot turn failed: {action.get('error')}")
            elif action_type in {"chat/turnComplete", "chat/turnCancelled"}:
                cancelled = action_type == "chat/turnCancelled"
                duration_ms = action.get("duration")
                if duration_ms is None:
                    duration_ms = round((time.monotonic() - self._turn_started) * 1000)
                self.output.stop_status()
                self.output.finish_response()
                details = [
                    f"{duration_ms / 1000:.1f}s",
                    f"{tool_count} tools",
                    f"model {self.model}",
                ]
                if usage_text := format_usage(self.last_usage):
                    details.append(usage_text)
                label = "Cancelled" if cancelled else "Done"
                self.output.info(
                    f"[{label.lower()}] {' - '.join(details)}",
                    color="yellow" if cancelled else "green",
                )
                self.output.info("")
                self.output.event(
                    "turn_finished",
                    turnId=turn_id,
                    cancelled=cancelled,
                    durationMs=duration_ms,
                    toolCount=tool_count,
                    usage=self.last_usage,
                )
                self.active_turn_id = None
                return

    async def cancel_active_turn(self) -> bool:
        if not self.chat_channel or not self.active_turn_id or self._cancel_requested:
            return False
        self._cancel_requested = True
        duration = round((time.monotonic() - self._turn_started) * 1000)
        self.output.stop_status()
        self.output.info(
            "[cancel] Cancellation requested; waiting for Copilot to stop.",
            color="yellow",
        )
        self.output.event("cancellation_requested", turnId=self.active_turn_id)
        await self.client.dispatch(
            self.chat_channel,
            {
                "type": "chat/turnCancelled",
                "turnId": self.active_turn_id,
                "duration": duration,
            },
        )
        return True

    async def _confirm_tool(self, turn_id: str, action: dict[str, Any]) -> None:
        assert self.chat_channel is not None
        tool_id = action.get("toolCallId", "unknown")
        name = self._tool_names.get(tool_id, tool_id)
        self.output.human(
            f"[approval] {name}: {display_value(action.get('invocationMessage', ''))}"
        )
        if tool_input := action.get("toolInput"):
            self.output.human(f"[input] {tool_input}")

        approved = self.approval_mode == "all"
        if self.approval_mode == "prompt":
            answer = await self._read_input("Approve this tool call? [y/N] ")
            approved = answer.strip().lower() in {"y", "yes"}
        confirmation: dict[str, Any] = {
            "type": "chat/toolCallConfirmed",
            "turnId": turn_id,
            "toolCallId": tool_id,
            "approved": approved,
        }
        if approved:
            confirmation["confirmed"] = "user-action"
        else:
            confirmation["reason"] = "denied"
        await self.client.dispatch(self.chat_channel, confirmation)
        self.output.start_status("Thinking (Ctrl-C to cancel)")

    async def _handle_input_request(self, request: dict[str, Any]) -> None:
        assert self.chat_channel is not None
        request_id = request.get("id")
        if not request_id:
            raise AhpError("Copilot sent an input request without an ID")

        self.output.human("[ask user]", color="yellow")
        if message := request.get("message"):
            self.output.human(str(message), color="yellow")
        if url := request.get("url"):
            self.output.human(f"Review: {url}")

        answers: dict[str, Any] = {}
        try:
            for question in request.get("questions") or []:
                answer = await self._ask_question(question)
                if answer is not None:
                    answers[question["id"]] = answer
            if not request.get("questions"):
                await self._confirm_unstructured_request()
            response = "accept"
        except InputRequestCompletion as completion:
            response = completion.response
            answers = {}
        except EOFError:
            self.output.human("Input closed; cancelling the request.", color="yellow")
            response = "cancel"
            answers = {}

        action: dict[str, Any] = {
            "type": "chat/inputCompleted",
            "requestId": request_id,
            "response": response,
        }
        if response == "accept":
            action["answers"] = answers
        await self.client.dispatch(self.chat_channel, action)
        self.output.start_status("Thinking (Ctrl-C to cancel)")

    async def _ask_question(self, question: dict[str, Any]) -> dict[str, Any] | None:
        title = question.get("title")
        if title:
            self.output.human(str(title), color="yellow")
        self.output.human(str(question.get("message", "Input requested")))

        kind = question.get("kind")
        if kind in {"single-select", "multi-select"}:
            return await self._ask_select(question)
        if kind == "boolean":
            return await self._ask_boolean(question)
        if kind in {"number", "integer"}:
            return await self._ask_number(question)
        return await self._ask_text(question)

    async def _ask_select(self, question: dict[str, Any]) -> dict[str, Any] | None:
        options = question.get("options") or []
        for index, option in enumerate(options, start=1):
            recommendation = " [recommended]" if option.get("recommended") else ""
            self.output.human(
                f"  {index}. {option.get('label', option.get('id'))}{recommendation}",
                color="blue",
            )
            if description := option.get("description"):
                self.output.human(f"     {description}")

        multiple = question.get("kind") == "multi-select"
        freeform = bool(question.get("allowFreeformInput"))
        if multiple:
            hint = "Enter comma-separated option numbers"
        else:
            hint = "Enter an option number"
        if freeform:
            hint += " or custom text"

        while True:
            raw = (await self._read_input(f"{hint}: ")).strip()
            if not raw:
                if question.get("required"):
                    self.output.human("A response is required.", color="red")
                    continue
                return None
            special = self._special_input(raw, required=bool(question.get("required")))
            if special is not False:
                return special

            tokens = [token.strip() for token in raw.split(",") if token.strip()]
            if not multiple and len(tokens) != 1:
                self.output.human("Choose exactly one option.", color="red")
                continue

            selected: list[str] = []
            custom: list[str] = []
            invalid = False
            for token in tokens:
                if token.isdigit() and 1 <= int(token) <= len(options):
                    selected.append(str(options[int(token) - 1]["id"]))
                elif freeform:
                    custom.append(token)
                else:
                    invalid = True
            if invalid or not tokens:
                self.output.human(
                    "Enter one of the displayed option numbers.", color="red"
                )
                continue

            count = len(selected) + len(custom)
            minimum = question.get("min")
            maximum = question.get("max")
            if minimum is not None and count < minimum:
                self.output.human(f"Choose at least {minimum} values.", color="red")
                continue
            if maximum is not None and count > maximum:
                self.output.human(f"Choose no more than {maximum} values.", color="red")
                continue

            if multiple:
                value = {
                    "kind": "selected-many",
                    "value": selected,
                    **({"freeformValues": custom} if custom else {}),
                }
            elif selected:
                value = {
                    "kind": "selected",
                    "value": selected[0],
                    **({"freeformValues": custom} if custom else {}),
                }
            else:
                value = {"kind": "selected", "value": custom[0]}
            return {"state": "submitted", "value": value}

    async def _ask_boolean(self, question: dict[str, Any]) -> dict[str, Any] | None:
        default = question.get("defaultValue")
        suffix = (
            " [Y/n] "
            if default is True
            else " [y/N] "
            if default is False
            else " [y/n] "
        )
        while True:
            raw = (await self._read_input(suffix)).strip().lower()
            if not raw:
                if default is not None:
                    return self._submitted("boolean", default)
                if question.get("required"):
                    self.output.human("A response is required.", color="red")
                    continue
                return None
            special = self._special_input(raw, required=bool(question.get("required")))
            if special is not False:
                return special
            if raw in {"y", "yes", "true", "1"}:
                return self._submitted("boolean", True)
            if raw in {"n", "no", "false", "0"}:
                return self._submitted("boolean", False)
            self.output.human("Enter yes or no.", color="red")

    async def _ask_number(self, question: dict[str, Any]) -> dict[str, Any] | None:
        default = question.get("defaultValue")
        while True:
            suffix = f" [{default}] " if default is not None else ": "
            raw = (await self._read_input(suffix)).strip()
            if not raw:
                if default is not None:
                    return self._submitted("number", default)
                if question.get("required"):
                    self.output.human("A response is required.", color="red")
                    continue
                return None
            special = self._special_input(raw, required=bool(question.get("required")))
            if special is not False:
                return special
            try:
                value: int | float
                value = int(raw) if question.get("kind") == "integer" else float(raw)
            except ValueError:
                self.output.human("Enter a valid number.", color="red")
                continue
            if question.get("min") is not None and value < question["min"]:
                self.output.human(
                    f"Enter a value of at least {question['min']}.", color="red"
                )
                continue
            if question.get("max") is not None and value > question["max"]:
                self.output.human(
                    f"Enter a value no greater than {question['max']}.", color="red"
                )
                continue
            return self._submitted("number", value)

    async def _ask_text(self, question: dict[str, Any]) -> dict[str, Any] | None:
        default = question.get("defaultValue")
        while True:
            suffix = f" [{default}] " if default is not None else ": "
            raw = await self._read_input(suffix)
            if not raw:
                if default is not None:
                    return self._submitted("text", default)
                if question.get("required"):
                    self.output.human("A response is required.", color="red")
                    continue
                return None
            special = self._special_input(
                raw.strip(), required=bool(question.get("required"))
            )
            if special is not False:
                return special
            if question.get("min") is not None and len(raw) < question["min"]:
                self.output.human(
                    f"Enter at least {question['min']} characters.", color="red"
                )
                continue
            if question.get("max") is not None and len(raw) > question["max"]:
                self.output.human(
                    f"Enter no more than {question['max']} characters.", color="red"
                )
                continue
            return self._submitted("text", raw)

    async def _confirm_unstructured_request(self) -> None:
        while True:
            raw = (await self._read_input("Accept? [y/N] ")).strip().lower()
            if raw in {"y", "yes"}:
                return
            if raw in {"d", "decline", "n", "no"}:
                raise InputRequestCompletion("decline")
            if raw in {"c", "cancel", "/cancel"}:
                raise InputRequestCompletion("cancel")
            self.output.human("Enter yes, no, or cancel.", color="red")

    async def _read_input(self, prompt: str) -> str:
        return await asyncio.to_thread(self.output.read_input, prompt)

    @staticmethod
    def _special_input(raw: str, *, required: bool) -> dict[str, Any] | None | bool:
        lowered = raw.lower()
        if lowered in {"/cancel", "cancel"}:
            raise InputRequestCompletion("cancel")
        if lowered in {"/decline", "decline"}:
            raise InputRequestCompletion("decline")
        if lowered in {"/skip", "skip"} and not required:
            return {"state": "skipped"}
        return False

    @staticmethod
    def _submitted(kind: str, value: Any) -> dict[str, Any]:
        return {"state": "submitted", "value": {"kind": kind, "value": value}}
