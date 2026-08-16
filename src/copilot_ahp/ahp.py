from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from typing import Any, Self

import websockets
from websockets.asyncio.client import ClientConnection

ROOT_CHANNEL = "ahp-root://"
PROTOCOL_VERSIONS = ("0.8.0",)


class AhpError(RuntimeError):
    """Base error raised by the minimal AHP client."""


class AhpRpcError(AhpError):
    def __init__(self, method: str, error: dict[str, Any]) -> None:
        self.method = method
        self.code = error.get("code")
        self.data = error.get("data")
        super().__init__(
            f"{method} failed ({self.code}): {error.get('message', 'unknown error')}"
        )


class AhpClient:
    def __init__(self, url: str, *, debug_events: bool = False) -> None:
        self.url = url
        self.debug_events = debug_events
        self.websocket: ClientConnection | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._next_request_id = 1
        self._next_client_seq = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._event_backlog: list[dict[str, Any]] = []

    async def __aenter__(self) -> Self:
        self.websocket = await websockets.connect(self.url, max_size=32 * 1024 * 1024)
        self._reader_task = asyncio.create_task(self._reader(), name="ahp-reader")
        return self

    async def __aexit__(self, *_: object) -> None:
        if self.websocket is not None:
            await self.websocket.close()
        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)

    async def _reader(self) -> None:
        assert self.websocket is not None
        try:
            async for raw_message in self.websocket:
                message = json.loads(raw_message)
                request_id = message.get("id")
                if request_id is not None and request_id in self._pending:
                    future = self._pending.pop(request_id)
                    if not future.done():
                        future.set_result(message)
                    continue
                if self.debug_events:
                    params = message.get("params", {})
                    action_type = params.get("action", {}).get("type")
                    print(
                        f"[AHP] {message.get('method')} {params.get('channel')} {action_type or ''}",
                        file=sys.stderr,
                    )
                await self._events.put(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail every pending RPC on transport failure
            error = AhpError(f"AHP connection closed: {exc}")
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)
            self._pending.clear()

    async def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float = 120,
    ) -> Any:
        assert self.websocket is not None
        request_id = self._next_request_id
        self._next_request_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self.websocket.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
        )
        try:
            response = await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(request_id, None)
        if "error" in response:
            raise AhpRpcError(method, response["error"])
        return response.get("result")

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        assert self.websocket is not None
        await self.websocket.send(
            json.dumps({"jsonrpc": "2.0", "method": method, "params": params})
        )

    async def dispatch(self, channel: str, action: dict[str, Any]) -> None:
        client_seq = self._next_client_seq
        self._next_client_seq += 1
        await self.notify(
            "dispatchAction",
            {"channel": channel, "clientSeq": client_seq, "action": action},
        )

    async def next_event(
        self,
        predicate: Callable[[dict[str, Any]], bool] | None = None,
        *,
        timeout: float = 120,
    ) -> dict[str, Any]:
        predicate = predicate or (lambda _: True)
        for index, event in enumerate(self._event_backlog):
            if predicate(event):
                return self._event_backlog.pop(index)

        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for an AHP event")
            event = await asyncio.wait_for(self._events.get(), remaining)
            if predicate(event):
                return event
            self._event_backlog.append(event)


def action_from(event: dict[str, Any]) -> dict[str, Any]:
    return event.get("params", {}).get("action", {})


def channel_from(event: dict[str, Any]) -> str | None:
    return event.get("params", {}).get("channel")
