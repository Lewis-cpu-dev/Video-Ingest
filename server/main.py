from __future__ import annotations

import argparse
import hmac
import os

import uvicorn
from starlette.responses import JSONResponse

from server.config import Settings
from server.service import VideoService
from server.tools.mcp import build_server


class BearerAuth:
    """Single-owner private HTTP authentication. All requests, including resources, are verified."""
    def __init__(self, app, token: str):
        if len(token) < 32:
            raise ValueError("VIDEO_INGEST_TOKEN must contain at least 32 characters")
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            values = [v for k, v in scope.get("headers", []) if k.lower() == b"authorization"]
            expected = ("Bearer " + self.token).encode()
            if len(values) != 1 or not hmac.compare_digest(values[0], expected):
                await JSONResponse({"error": "UNAUTHORIZED"}, status_code=401,
                                   headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"})(scope, receive, send)
                return
        await self.app(scope, receive, send)


def main():
    parser = argparse.ArgumentParser(description="Private video evidence MCP service")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", choices=["127.0.0.1", "::1"], default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    token = os.getenv("VIDEO_INGEST_TOKEN", "")
    if args.transport == "http" and len(token) < 32:
        parser.error("HTTP requires VIDEO_INGEST_TOKEN with at least 32 characters")
    os.umask(0o077)
    service = VideoService(Settings.from_env())
    server = build_server(service)
    if args.transport == "stdio":
        server.run(transport="stdio")
    else:
        app = BearerAuth(server.streamable_http_app(), token)
        uvicorn.run(app, host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
