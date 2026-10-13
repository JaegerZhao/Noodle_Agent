from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import websockets
from fastapi import HTTPException, Request

from ..config import Settings


class HarnessGateway:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def harness_rpc(self, method: str, payload: dict) -> dict:
        """Call the loopback Harness RPC without exposing an unrestricted proxy."""

        rpc_id = str(uuid.uuid4())
        envelope = {
            "type": "client-request",
            "rpcId": rpc_id,
            "method": method,
            "payload": payload,
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self.settings.harness_web_url}/api/{method}",
                    json=envelope,
                )
                response.raise_for_status()
                body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(
                status_code=503,
                detail=f"DeepSeek Harness is unavailable: {exc}",
            ) from exc
        if body.get("rpcId") != rpc_id:
            raise HTTPException(
                status_code=502, detail="Harness returned a mismatched RPC response"
            )
        result = body.get("result") or {}
        if not result.get("ok"):
            error = result.get("error") or {}
            detail = error.get("message") or error.get("code") or "Harness RPC failed"
            raise HTTPException(status_code=409, detail=detail)
        value = result.get("value")
        return value if isinstance(value, dict) else {"value": value}

    async def pending_harness_approval_rpc_id(
        self,
        session_id: str,
        approval_id: str,
    ) -> str:
        """Resolve the transient response RPC id for one durable approval id.

        DSH deliberately sends approvals on its WebSocket mux and accepts answers
        at /api/respond. Opening a new mux replays every still-pending approval, so
        the Noodle adapter can answer safely without owning a second Agent loop.
        """

        base_url = self.settings.harness_web_url.rstrip("/")
        if base_url.startswith("https://"):
            mux_url = f"wss://{base_url.removeprefix('https://')}/api/events.mux"
        elif base_url.startswith("http://"):
            mux_url = f"ws://{base_url.removeprefix('http://')}/api/events.mux"
        else:
            raise HTTPException(status_code=500, detail="Unsupported Harness Web URL")
        try:
            async with asyncio.timeout(5.0):
                async with websockets.connect(
                    mux_url,
                    open_timeout=3.0,
                    close_timeout=1.0,
                    max_size=2 * 1024 * 1024,
                ) as socket:
                    while True:
                        raw = await socket.recv()
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8", errors="replace")
                        try:
                            envelope = json.loads(raw)
                        except (TypeError, ValueError):
                            continue
                        payload = envelope.get("payload") or {}
                        if (
                            payload.get("type") == "approval/requested"
                            and payload.get("sessionId") == session_id
                            and payload.get("approvalId") == approval_id
                        ):
                            rpc_id = envelope.get("rpcId")
                            if isinstance(rpc_id, str) and rpc_id:
                                return rpc_id
        except TimeoutError as exc:
            raise HTTPException(
                status_code=409,
                detail="Approval is no longer pending or could not be observed",
            ) from exc
        except (OSError, websockets.WebSocketException) as exc:
            raise HTTPException(
                status_code=503,
                detail=f"DeepSeek Harness approval channel is unavailable: {exc}",
            ) from exc
        raise HTTPException(status_code=409, detail="Approval is no longer pending")


def harness_actor(request: Request) -> str:
    session_id = request.headers.get("x-harness-session-id")
    call_id = request.headers.get("x-harness-tool-call-id")
    if session_id:
        return f"harness-session:{session_id}"
    if call_id:
        return f"harness-call:{call_id}"
    return "internal-api-user"
