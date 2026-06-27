"""
dashboard_server.py — Single-port HTTP + WebSocket server for the live dashboard.

Serves the dashboard PAGE and the live STATE FEED on the SAME port (default 8000):
  GET  /        -> dashboard/index.html
  GET  /ws      -> WebSocket; pushes a JSON state snapshot every second.

Why one port: a browser opening the page at http://host:PORT/ connects the WebSocket to
ws://host:PORT/ws — the exact same host+port it already reached. That sidesteps every
cross-port / IPv4-vs-IPv6 / Safari-local-network failure mode that made the old
two-port (8000 page + 8888 ws) setup hang on "Connecting…".
"""

import asyncio
import json
import math
import os
import threading
from typing import Callable
from loguru import logger
import config

try:
    from aiohttp import web
    _AIOHTTP_AVAILABLE = True
except ImportError:
    _AIOHTTP_AVAILABLE = False

_DASH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard")
_INDEX = os.path.join(_DASH_DIR, "index.html")


def _sanitize(obj):
    """
    Replace non-finite floats (NaN / Infinity) with None. Python's json emits these as
    bare `NaN`/`Infinity` tokens, which are INVALID JSON — the browser's JSON.parse then
    throws, the message is silently dropped, and the dashboard reads as 'disconnected'.
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj


def _dumps(snapshot: dict) -> str:
    return json.dumps(_sanitize(snapshot), default=str, allow_nan=False)


class DashboardServer:

    def __init__(self, state_fn: Callable[[], dict], toggle_fn: Callable[[], bool] = None):
        """state_fn: returns the current bot state dict; called every push interval.
        toggle_fn: optional LIVE kill switch; returns the new halted state (True=stopped)."""
        self._state_fn = state_fn
        self._toggle_fn = toggle_fn

    def run(self):
        """Run the server. Blocks the calling thread — call in a daemon thread."""
        if not _AIOHTTP_AVAILABLE:
            logger.error("aiohttp not installed. Run: pip install aiohttp")
            return
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        except Exception as exc:
            logger.error(f"Dashboard server crashed: {exc}")

    async def _serve(self):
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/ws", self._ws)
        app.router.add_post("/api/live/toggle", self._toggle)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        host = config.DASHBOARD_HOST or "127.0.0.1"
        bind = None if host in ("0.0.0.0", "::") else host   # None = all interfaces
        site = web.TCPSite(runner, bind, config.DASHBOARD_HTTP_PORT)
        await site.start()
        logger.info("=" * 60)
        if bind is None:
            logger.warning(f"  DASHBOARD EXPOSED on 0.0.0.0:{config.DASHBOARD_HTTP_PORT} — "
                           f"firewall it; anyone can view your trading state")
        else:
            logger.info(f"  DASHBOARD (localhost only): http://localhost:{config.DASHBOARD_HTTP_PORT}/")
            logger.info(f"  Remote: ssh -L {config.DASHBOARD_HTTP_PORT}:localhost:"
                        f"{config.DASHBOARD_HTTP_PORT} user@your-vps  then open the URL")
        logger.info("=" * 60)
        while True:
            await asyncio.sleep(3600)

    async def _index(self, request):
        if not os.path.exists(_INDEX):
            return web.Response(status=404, text="dashboard/index.html not found")
        # No-cache so a freshly-pulled dashboard JS always loads (a normal browser refresh
        # was serving the cached old client, which is why prior UI fixes appeared to "not
        # take" — the page never reloaded the new code).
        return web.FileResponse(_INDEX, headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache", "Expires": "0",
        })

    async def _toggle(self, request):
        """LIVE kill switch: flip the bot's live-halt flag. Returns the new state so the
        button can re-label without waiting for the next snapshot push."""
        if self._toggle_fn is None:
            return web.json_response({"error": "no control channel"}, status=400)
        # When a token is configured (dashboard reachable beyond localhost), require it so a
        # stranger who finds the URL can't stop/start live trading.
        if config.DASHBOARD_TOKEN and request.headers.get("X-Dashboard-Token") != config.DASHBOARD_TOKEN:
            logger.warning(f"Kill switch toggle REJECTED (bad/missing token) from {request.remote}")
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            halted = bool(self._toggle_fn())
            logger.warning(f"Dashboard kill switch toggled — live_halt={halted}")
            return web.json_response({"live_halt": halted})
        except Exception as exc:
            logger.error(f"Kill switch toggle failed: {exc}")
            return web.json_response({"error": str(exc)}, status=500)

    async def _ws(self, request):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        peer = request.remote
        logger.debug(f"Dashboard client connected: {peer}")
        # Push every interval. A failure to BUILD or SERIALIZE the snapshot must NOT tear
        # down the socket (that surfaced as a spurious "BOT DISCONNECTED" the user had to
        # refresh away) — only a real transport error ends the loop.
        first = True
        while not ws.closed:
            if not first:
                await asyncio.sleep(config.STATE_PUSH_INTERVAL)
            first = False
            try:
                snap = self._state_fn()
            except Exception as exc:
                logger.debug(f"Dashboard state build failed (kept connection): {exc}")
                continue
            if not snap:
                continue
            try:
                payload = _dumps(snap)
            except Exception as exc:
                logger.debug(f"Dashboard serialize failed (kept connection): {exc}")
                continue
            try:
                await ws.send_str(payload)
            except (ConnectionResetError, asyncio.CancelledError):
                break                      # client really gone
            except Exception as exc:
                logger.debug(f"Dashboard ws send failed: {exc}")
                break
        logger.debug(f"Dashboard client disconnected: {peer}")
        return ws
