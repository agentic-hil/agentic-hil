# Copyright 2026 Hannes Pauli
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from .config import AIHILConfig
from .mcp import handle_mcp_message, mcp_headers, parse_error_response
from .tools import AIHILToolService


def create_app(config: AIHILConfig) -> FastAPI:
    app = FastAPI(title="AI-HIL", version="0.1.0")
    tools = AIHILToolService(config)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "backend": config.debugger.type,
            "target": {
                "name": config.target.name,
                "controller": config.target.controller,
            },
        }

    @app.post("/mcp")
    async def mcp_endpoint(request: Request) -> Response:
        try:
            message = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return JSONResponse(parse_error_response(), headers=mcp_headers())

        response = handle_mcp_message(message, tools)
        if response is None:
            return Response(status_code=202, headers=mcp_headers())
        return JSONResponse(response, headers=mcp_headers())

    return app
