"""Minimal MCP client over Streamable HTTP (spec 2025-06-18).

* every JSON-RPC message is an HTTP POST to the MCP endpoint
* ``Accept: application/json, text/event-stream``
* the server answers with JSON or an SSE stream containing the response
* ``Mcp-Session-Id`` from the initialize response is echoed on every request
* ``MCP-Protocol-Version`` is sent after initialization
* ``Authorization: Bearer <token>``; on 401 the token is refreshed once
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

import requests

from .util import log

PROTOCOL_VERSION = "2025-06-18"


class MCPError(RuntimeError):
    pass


class MCPToolError(MCPError):
    """The tool ran but reported ``isError``."""


class MCPClient:
    def __init__(self, url: str, token_provider: Callable[[bool], str], delay: float = 0.1,
                 client_name: str = "meister2nc", client_version: str = "0.1"):
        self.url = url
        self.token_provider = token_provider
        self.delay = delay
        self.client_name = client_name
        self.client_version = client_version
        self.http = requests.Session()
        self.session_id: str | None = None
        self.protocol_version: str | None = None
        self._next_id = 1
        self.server_info: dict | None = None
        self._last_call = 0.0

    # --- transport --------------------------------------------------------
    def _headers(self, token: str) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    def _post(self, message: dict, expect_response: bool = True, _retry: int = 0) -> dict | None:
        wait = self.delay - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        token = self.token_provider(False)
        resp = self.http.post(self.url, headers=self._headers(token), data=json.dumps(message),
                              stream=True, timeout=120)
        self._last_call = time.monotonic()
        if resp.status_code == 401 and _retry == 0:
            log.info("MCP 401 → refreshing token")
            self.token_provider(True)
            return self._post(message, expect_response, _retry=1)
        if resp.status_code == 404 and self.session_id and _retry < 2:
            log.info("MCP session expired → re-initializing")
            self.session_id = None
            self.initialize()
            return self._post(message, expect_response, _retry=_retry + 1)
        if resp.status_code == 429 and _retry < 5:
            delay = float(resp.headers.get("Retry-After") or resp.headers.get("ratelimit-reset") or 15)
            log.warning("MCP rate limited, sleeping %.0fs", delay)
            time.sleep(delay)
            return self._post(message, expect_response, _retry=_retry + 1)
        if resp.status_code >= 500 and _retry < 3:
            time.sleep(3 * (_retry + 1))
            return self._post(message, expect_response, _retry=_retry + 1)
        if resp.status_code == 202:
            return None
        if resp.status_code >= 400:
            raise MCPError(f"HTTP {resp.status_code} from MCP endpoint: {resp.text[:500]}")
        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid
        if not expect_response:
            resp.close()
            return None
        ctype = resp.headers.get("Content-Type", "")
        if ctype.startswith("text/event-stream"):
            return self._read_sse(resp, message.get("id"))
        body = resp.json()
        if isinstance(body, list):  # batch not used, but be tolerant
            for item in body:
                if item.get("id") == message.get("id"):
                    return item
            return body[0] if body else None
        return body

    @staticmethod
    def _read_sse(resp: requests.Response, want_id: Any) -> dict | None:
        data_lines: list[str] = []
        result: dict | None = None
        for raw in resp.iter_lines(decode_unicode=True):
            if raw is None:
                continue
            line = raw.rstrip("\r")
            if line == "":
                if data_lines:
                    payload = "\n".join(data_lines)
                    data_lines = []
                    try:
                        msg = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(msg, dict) and msg.get("id") == want_id and ("result" in msg or "error" in msg):
                        result = msg
                        break
                continue
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        resp.close()
        if result is None and data_lines:
            try:
                msg = json.loads("\n".join(data_lines))
                if isinstance(msg, dict) and msg.get("id") == want_id:
                    result = msg
            except json.JSONDecodeError:
                pass
        return result

    # --- protocol ---------------------------------------------------------
    def _request(self, method: str, params: dict | None = None) -> Any:
        msg_id = self._next_id
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            message["params"] = params
        reply = self._post(message)
        if reply is None:
            raise MCPError(f"no JSON-RPC response for {method}")
        if "error" in reply:
            err = reply["error"]
            raise MCPError(f"{method}: {err.get('code')} {err.get('message')} {err.get('data', '')}")
        return reply.get("result")

    def _notify(self, method: str, params: dict | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._post(message, expect_response=False)

    def initialize(self) -> dict:
        self.session_id = None
        result = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": self.client_name, "version": self.client_version},
        })
        self.protocol_version = result.get("protocolVersion") or PROTOCOL_VERSION
        self.server_info = result.get("serverInfo")
        self._notify("notifications/initialized")
        return result

    def list_tools(self) -> list[dict]:
        tools: list[dict] = []
        cursor = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = self._request("tools/list", params)
            tools.extend(result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict | None = None, raise_on_error: bool = True) -> dict:
        if self.session_id is None and self.server_info is None:
            self.initialize()
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        if raise_on_error and result.get("isError"):
            texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
            raise MCPToolError(f"{name} {json.dumps(arguments or {}, ensure_ascii=False)[:200]}: {' '.join(texts)[:500]}")
        return result
