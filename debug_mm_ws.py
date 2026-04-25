"""Standalone MM WebSocket connect probe.

Connects to MM with the same options the adapter uses, prints every step.
Helps isolate whether the problem is TLS, auth, or post-auth message loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("mattermostdriver").setLevel(logging.DEBUG)
logging.getLogger("websockets").setLevel(logging.DEBUG)


async def main() -> None:
    import websockets

    url = os.environ["MATTERMOST_URL"]
    token = os.environ["MATTERMOST_TOKEN"]
    ssl_verify_env = os.environ.get("MATTERMOST_SSL_VERIFY", "true").lower()
    ssl_verify = ssl_verify_env not in ("0", "false", "no", "off")

    parsed = urlparse(url)
    host = parsed.hostname
    scheme = parsed.scheme or "https"
    port = parsed.port or (443 if scheme == "https" else 80)

    ws_url = f"{'wss' if scheme=='https' else 'ws'}://{host}:{port}/api/v4/websocket"
    print(f"WS URL: {ws_url}  ssl_verify={ssl_verify}")

    context: ssl.SSLContext | None = None
    if scheme == "https":
        context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
        if not ssl_verify:
            context.verify_mode = ssl.CERT_NONE
            context.check_hostname = False

    print("connecting…")
    try:
        async with websockets.connect(ws_url, ssl=context) as ws:
            print("connected. sending auth challenge…")
            import json
            await ws.send(json.dumps({
                "seq": 1,
                "action": "authentication_challenge",
                "data": {"token": token},
            }))
            print("auth sent, waiting for messages (≤10s)…")
            try:
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=10)
                    print(f"  ← {msg[:300]}")
            except asyncio.TimeoutError:
                print("(no more messages in 10s; closing)")
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
