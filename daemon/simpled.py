#!/usr/bin/env python3
"""simpled.py — minimal bridge for muse-browser extension.

- WebSocket server on 127.0.0.1:19091: extension connects here.
- HTTP server on 127.0.0.1:18010: POST /tool {"tool": name, "args": {...}}.

No MCP protocol. Direct JSON forwarding. Loopback only.
"""
import asyncio
import json
import os
import socket
import sys
import threading
from concurrent.futures import Future as ConcurrentFuture
from http.server import BaseHTTPRequestHandler, HTTPServer

import websockets

from _auth import get_or_create_token, is_authorized

# Ports are overridable per-machine via env (defaults suit the Chrome extension).
WS_HOST, WS_PORT = "127.0.0.1", int(os.environ.get("MUSE_WS_PORT", 19091))
HTTP_HOST, HTTP_PORT = "127.0.0.1", int(os.environ.get("MUSE_HTTP_PORT", 18010))


def _check_port_free(host, port, owner_hint):
    """Fail fast with a clear message if another daemon already owns the port.

    simpled.py, mused.py and mcp_server.py are mutually exclusive frontends —
    they share ports 19091 (extension WS) and 18010 (HTTP). Running two at
    once used to die with a cryptic OSError; now it explains itself.
    """
    s = socket.socket()
    try:
        s.bind((host, port))
    except OSError:
        print(
            f"[simpled] ERROR: {host}:{port} is already in use — "
            f"{owner_hint} is probably running. Stop the other daemon first "
            "(only one of simpled / mused / mcp_server may run at a time).",
            flush=True,
        )
        sys.exit(1)
    finally:
        s.close()

# Extension connection
ext_ws = None
ext_lock = threading.Lock()
pending = {}  # id -> Future
seq = 0
loop = None


async def ws_handler(ws):
    global ext_ws
    with ext_lock:
        ext_ws = ws
    print("[simpled] extension connected", flush=True)
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            mid = msg.get("id")
            if mid is not None and mid in pending:
                fut = pending.pop(mid)
                if not fut.done():
                    fut.set_result(msg)
    finally:
        with ext_lock:
            if ext_ws is ws:
                ext_ws = None
        print("[simpled] extension disconnected", flush=True)


def call_extension(method, params, timeout=60):
    """Send a tool call to the extension, wait for result. Thread-safe."""
    global seq
    with ext_lock:
        ws = ext_ws
        if ws is None:
            return {"ok": False, "error": "extension not connected"}
        seq += 1
        mid = seq
        # Use concurrent.futures.Future (supports timeout) instead of asyncio.Future
        fut = ConcurrentFuture()
        pending[mid] = fut
        msg = json.dumps({"id": mid, "method": method, "params": params or {}})
        asyncio.run_coroutine_threadsafe(ws.send(msg), loop)
    try:
        result = fut.result(timeout=timeout)
    except Exception as e:
        pending.pop(mid, None)
        return {"ok": False, "error": f"timeout/error: {e}"}
    if result.get("ok"):
        return {"ok": True, "result": result.get("result")}
    return {"ok": False, "error": result.get("error", "unknown")}


# Map HTTP tool names to extension method names
TOOL_MAP = {
    "tabs_list": "tabs.list",
    "tab_create": "tabs.create",
    "tab_close": "tabs.close",
    "evaluate": "page.evaluate",
    "click": "input.click",
    "press_key": "input.press_key",
    "type": "input.type",
    "scroll": "input.scroll",
    "navigate": "page.navigate",
    "snapshot": "page.snapshot",
    "screenshot": "page.screenshot",
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/health":
            with ext_lock:
                ext = ext_ws is not None
            body = json.dumps({"ok": True, "ext": ext}).encode()
        else:
            body = json.dumps({"ok": False, "error": "not found"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/tool":
            self.send_response(404)
            self.end_headers()
            return
        # Local trust boundary: any local process could otherwise drive the
        # browser and export the full cookie jar. Require the bearer token.
        if not is_authorized(self.headers):
            body = json.dumps({"ok": False, "error": "unauthorized: missing or invalid bearer token"}).encode()
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            data = {}
        tool = data.get("tool", "")
        args = data.get("args", {})
        # Translate evaluate's "js" param: extension expects it inside params
        method = TOOL_MAP.get(tool, tool)
        result = call_extension(method, args)
        body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_http():
    srv = HTTPServer((HTTP_HOST, HTTP_PORT), Handler)
    print(f"[simpled] HTTP on {HTTP_HOST}:{HTTP_PORT}", flush=True)
    srv.serve_forever()


async def run_ws():
    global loop
    loop = asyncio.get_running_loop()
    async with websockets.serve(ws_handler, WS_HOST, WS_PORT):
        print(f"[simpled] WS on {WS_HOST}:{WS_PORT}", flush=True)
        await asyncio.Future()


def main():
    _check_port_free(WS_HOST, WS_PORT, "mcp_server.py or another simpled")
    _check_port_free(HTTP_HOST, HTTP_PORT, "mused.py or another simpled")
    get_or_create_token()  # ensure the bearer token exists before serving
    threading.Thread(target=run_http, daemon=True).start()
    asyncio.run(run_ws())


if __name__ == "__main__":
    main()
