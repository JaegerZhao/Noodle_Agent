from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, Query

from .context import ApiContext
from .models import (
    HarnessApprovalDecisionRequest,
    HarnessPromptRequest,
    HarnessSessionCreateRequest,
    HarnessSessionRenameRequest,
)


def create_router(context: ApiContext):
    router = APIRouter()
    resolved = context.settings
    harness_rpc = context.harness.harness_rpc
    pending_harness_approval_rpc_id = context.harness.pending_harness_approval_rpc_id

    @router.get("/ui-config")
    def ui_config() -> dict:
        return {
            "harness_web_url": resolved.harness_web_url,
            "harness_chat_api": "/internal/v1/harness",
            "api_prefix": "/internal/v1",
            "poll_interval_ms": 2500,
        }

    @router.get("/harness/sessions")
    async def list_harness_sessions() -> dict:
        return await harness_rpc("session.list", {})

    @router.post("/harness/sessions", status_code=201)
    async def create_harness_session(payload: HarnessSessionCreateRequest) -> dict:
        created = await harness_rpc("session.create", {})
        title = (payload.title or "").strip()
        if title:
            renamed = await harness_rpc(
                "session.rename",
                {"sessionId": created["sessionId"], "title": title},
            )
            created["title"] = renamed.get("title", title)
        return created

    @router.patch("/harness/sessions/{session_id}")
    async def rename_harness_session(
        session_id: str,
        payload: HarnessSessionRenameRequest,
    ) -> dict:
        title = payload.title.strip()
        if not title:
            raise HTTPException(status_code=422, detail="Session title cannot be blank")
        return await harness_rpc(
            "session.rename",
            {"sessionId": session_id, "title": title},
        )

    @router.get("/harness/sessions/{session_id}/history")
    async def get_harness_history(
        session_id: str,
        max_messages: int = Query(default=80, ge=1, le=200),
    ) -> dict:
        return await harness_rpc(
            "session.history",
            {"sessionId": session_id, "maxMessages": max_messages},
        )

    @router.post("/harness/prompt", status_code=202)
    async def prompt_harness(payload: HarnessPromptRequest) -> dict:
        prompt = payload.message.strip()
        if payload.context and payload.context.strip():
            prompt = (
                "可信 Noodle 界面定位（仅用于选择实体；正文与证据必须通过工具读取）：\n"
                f"{payload.context.strip()}\n\n用户请求：\n{prompt}"
            )
        return await harness_rpc(
            "session.prompt",
            {
                "sessionId": payload.session_id,
                "mode": payload.mode,
                "content": [{"type": "text", "text": prompt}],
                "clientTimeZone": "Asia/Shanghai",
            },
        )

    @router.post("/harness/sessions/{session_id}/approvals/{approval_id}")
    async def decide_harness_approval(
        session_id: str,
        approval_id: str,
        payload: HarnessApprovalDecisionRequest,
    ) -> dict:
        rpc_id = await pending_harness_approval_rpc_id(session_id, approval_id)
        response_envelope = {
            "type": "client-response",
            "rpcId": rpc_id,
            "result": {
                "ok": True,
                "value": {
                    "sessionId": session_id,
                    "approvalId": approval_id,
                    "outcome": payload.outcome,
                },
            },
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{resolved.harness_web_url}/api/respond",
                    json=response_envelope,
                )
                response.raise_for_status()
                receipt = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(
                status_code=503,
                detail=f"DeepSeek Harness approval response failed: {exc}",
            ) from exc
        if receipt.get("accepted") is not True:
            raise HTTPException(
                status_code=409,
                detail=receipt.get("reason") or "Approval is no longer pending",
            )
        return {
            "accepted": True,
            "session_id": session_id,
            "approval_id": approval_id,
            "outcome": payload.outcome,
        }

    @router.post("/harness/sessions/{session_id}/cancel")
    async def cancel_harness_session(session_id: str) -> dict:
        return await harness_rpc("session.cancel", {"sessionId": session_id})

    return router
