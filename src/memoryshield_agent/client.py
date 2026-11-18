"""HTTP adapter for MemoryShield+'s synchronous Add/Search contract."""
from __future__ import annotations

import math
from typing import Annotated, Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field

from memoryshield_agent.config import Settings


Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")]


class MemoryMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant", "system", "tool"] = "user"
    content: str = Field(min_length=1, max_length=32000, description="Original text to remember")
    timestamp: int | None = Field(default=None, ge=0, description="Optional Unix timestamp in milliseconds")


class BackendError(Exception):
    """A sanitized failure with sufficient metadata to retry the same write."""


class MemoryClient:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.http = httpx.AsyncClient(
            base_url=settings.base_url.rstrip("/") + "/",
            headers={"Authorization": "Bearer " + settings.api_key} if settings.api_key else {},
            timeout=httpx.Timeout(settings.timeout_seconds, connect=10),
            follow_redirects=False,
            verify=True,
            transport=transport,
        )

    async def close(self) -> None:
        await self.http.aclose()

    async def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        try:
            response = await self.http.request(method, path, json=payload)
        except httpx.TimeoutException:
            raise BackendError("backend_timeout") from None
        except httpx.HTTPError:
            raise BackendError("backend_connection_failed") from None
        if response.status_code != 200:
            raise BackendError(f"backend_http_{response.status_code}")
        try:
            result = response.json()
        except ValueError:
            raise BackendError("backend_invalid_json") from None
        if not isinstance(result, dict):
            raise BackendError("backend_invalid_response")
        return result

    async def remember(self, messages: list[MemoryMessage], session_id: str, request_id: str | None) -> dict:
        request_id = request_id or "mcp-" + uuid4().hex
        payload = {
            "user_id": self.settings.user_id,
            "session_id": session_id,
            "request_id": request_id,
            "messages": [message.model_dump() for message in messages],
        }
        try:
            if not messages or any(not message.content.strip() for message in messages):
                raise BackendError("messages_must_contain_nonempty_text")
            if sum(len(message.content) for message in messages) > 64000:
                raise BackendError("message_batch_exceeds_64000_characters")
            result = await self._request("POST", "add", payload)
            if (result.get("success") is not True
                    or result.get("request_id") != request_id
                    or result.get("user_id") != self.settings.user_id
                    or result.get("session_id") != session_id
                    or type(result.get("written")) is not int
                    or result["written"] != len(messages)):
                raise BackendError("backend_invalid_add_receipt")
        except BackendError as error:
            raise BackendError(f"{error}. Retry the identical messages and session_id with request_id={request_id}") from None
        return {"request_id": request_id, "session_id": session_id, "written": result["written"], "success": True}

    async def recall(self, query: str, top_k: int) -> dict:
        if not query.strip():
            raise BackendError("query_must_contain_nonempty_text")
        result = await self._request("POST", "search", {
            "query": query, "user_id": self.settings.user_id, "top_k": top_k,
        })
        memories = result.get("data")
        if not isinstance(memories, list) or len(memories) > top_k:
            raise BackendError("backend_invalid_search_response")
        for memory in memories:
            if (not isinstance(memory, dict)
                    or not isinstance(memory.get("id"), str)
                    or not isinstance(memory.get("content"), str)
                    or type(memory.get("score")) not in (int, float)
                    or not math.isfinite(memory["score"])):
                raise BackendError("backend_invalid_search_evidence")
        return {"memories": memories, "count": len(memories)}

    async def status(self) -> dict:
        result = await self._request("GET", "health")
        fields = ("status", "version", "embedding_model", "add_model", "add_model_configured",
                  "rerank_enabled", "rerank_model")
        return {"check": "backend_configuration", **{field: result[field] for field in fields if field in result}}
