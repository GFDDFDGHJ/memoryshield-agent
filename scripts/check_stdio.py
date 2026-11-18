"""Exercise the installed MCP entry point against a disposable HTTP backend."""
from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
from threading import Thread

from mcp import Client
from mcp.client.stdio import StdioServerParameters


class Backend(BaseHTTPRequestHandler):
    receipts: dict[tuple[str, str], dict] = {}
    memories: list[dict] = []
    add_requests = 0

    def log_message(self, format: str, *args: object) -> None:
        pass

    def respond(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self.respond(200, {"status": "ok", "version": "test-backend", "add_model_configured": True})

    def do_POST(self) -> None:
        if self.headers.get("Authorization") != "Bearer synthetic-token":
            self.respond(401, {"error": "invalid_auth"})
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if body["user_id"] != "synthetic-user":
            self.respond(403, {"error": "unexpected_namespace"})
            return
        if self.path == "/add":
            Backend.add_requests += 1
            key = (body["user_id"], body["request_id"])
            receipt = Backend.receipts.get(key)
            if receipt and receipt["body"] != body:
                self.respond(409, {"error": "idempotency_conflict"})
                return
            if not receipt:
                result = {"success": True, "written": len(body["messages"]),
                          "request_id": body["request_id"], "session_id": body["session_id"],
                          "user_id": body["user_id"]}
                receipt = {"body": body, "result": result}
                Backend.receipts[key] = receipt
                for message in body["messages"]:
                    Backend.memories.append({"id": str(len(Backend.memories) + 1),
                                             "content": message["content"], "score": 1.0,
                                             "source_session": body["session_id"]})
            self.respond(200, receipt["result"])
        elif self.path == "/search":
            self.respond(200, {"data": Backend.memories[:body["top_k"]]})
        else:
            self.respond(404, {})


async def check(port: int) -> None:
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "memoryshield_agent"],
        env={"MEMORYSHIELD_URL": f"http://127.0.0.1:{port}",
             "MEMORYSHIELD_API_KEY": "synthetic-token", "MEMORYSHIELD_USER_ID": "synthetic-user"},
    )
    async with Client(parameters, read_timeout_seconds=15) as client:
        catalog = await client.list_tools()
        tools = {tool.name: tool for tool in catalog.tools}
        assert set(tools) == {"memoryshield_remember", "memoryshield_recall", "memoryshield_status"}
        assert tools["memoryshield_remember"].annotations.read_only_hint is False
        assert tools["memoryshield_recall"].annotations.read_only_hint is True
        for tool in tools.values():
            assert "user_id" not in tool.input_schema.get("properties", {})
        health = await client.call_tool("memoryshield_status")
        assert not health.is_error, health
        assert health.structured_content["plugin_version"] == "1.0.0"
        arguments = {"messages": [{"content": "I prefer short explanations."}],
                     "session_id": "synthetic-session", "request_id": "synthetic-write-1"}
        for _ in range(2):
            saved = await client.call_tool("memoryshield_remember", arguments)
            assert not saved.is_error, saved
            assert saved.structured_content["written"] == 1
        assert len(Backend.memories) == 1
        conflict = await client.call_tool("memoryshield_remember", {
            **arguments, "messages": [{"content": "Different content."}],
        })
        assert conflict.is_error
        found = await client.call_tool("memoryshield_recall", {"query": "Preferred explanations?"})
        assert not found.is_error, found
        assert found.structured_content["memories"][0]["source_session"] == "synthetic-session"
        assert found.structured_content["memories"][0]["content"] == "I prefer short explanations."
        invalid = await client.call_tool("memoryshield_recall", {"query": "hello", "top_k": 101})
        assert invalid.is_error
    # A fresh MCP process still reads the same independent backend storage.
    async with Client(parameters, read_timeout_seconds=15) as client:
        found = await client.call_tool("memoryshield_recall", {"query": "Preferred explanations?"})
        assert found.structured_content["count"] == 1
    print("PASS: stdio initialization, 3 tools, fixed namespace, Add, replay, conflict, Search, reconnect")
    print("Provider calls: 0 (local synthetic HTTP backend)")


def main() -> None:
    http = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
    thread = Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        asyncio.run(check(http.server_port))
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
