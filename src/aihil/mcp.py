# Copyright 2026 Hannes Pauli
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from typing import Any

from .tools import AIHILToolService


MCP_PROTOCOL_VERSION = "2025-06-18"

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603


EMPTY_OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "aihil_debugger_info",
        "description": "Check whether the configured debugger backend is available.",
        "inputSchema": EMPTY_OBJECT_SCHEMA,
    },
    {
        "name": "aihil_probe_target",
        "description": "Probe the configured embedded target through the configured debugger.",
        "inputSchema": EMPTY_OBJECT_SCHEMA,
    },
    {
        "name": "aihil_flash_firmware",
        "description": (
            "Flash a validated firmware artifact to the configured target. "
            "Provide exactly one of image_path or artifact_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "image_path": {
                    "type": "string",
                    "description": "Local firmware path under an allowed artifact root, for example build/firmware.elf.",
                },
                "artifact_id": {
                    "type": "string",
                    "description": "Uploaded artifact id, if artifact upload support is available.",
                },
            },
            "oneOf": [
                {"required": ["image_path"]},
                {"required": ["artifact_id"]},
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "aihil_reset_target",
        "description": "Reset the configured target through the configured debugger.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["run", "halt", "init"],
                    "default": "run",
                    "description": "Reset mode. Use run unless the task requires halt or init.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "aihil_get_last_report",
        "description": "Return the most recent structured AI-HIL report.",
        "inputSchema": EMPTY_OBJECT_SCHEMA,
    },
    {
        "name": "aihil_classify_last_error",
        "description": "Classify the most recent AI-HIL/debugger failure.",
        "inputSchema": EMPTY_OBJECT_SCHEMA,
    },
]

AIHIL_WORKFLOW_PROMPT = """Use AI-HIL as the safe gate to the configured embedded hardware.

Workflow:
1. Build the firmware first.
2. Probe the target before flashing.
3. Flash only validated artifacts from configured allowed roots.
4. Read the structured result after every hardware action.
5. Reset only when needed or when the task explicitly requires it.
6. If ok is false, diagnose using error_type, backend_error_type, likely_causes, report_path, and log_path before changing code again.

Safety rules:
- Do not request raw OpenOCD or debugger commands.
- Do not request arbitrary shell access for hardware actions.
- Do not flash files outside configured artifact roots.
- Treat permission_denied as authoritative and stop.
"""

MCP_PROMPTS: list[dict[str, Any]] = [
    {
        "name": "aihil_embedded_workflow",
        "description": "Safe workflow for using AI-HIL hardware tools from an AI agent.",
    }
]


def mcp_headers() -> dict[str, str]:
    return {"MCP-Protocol-Version": MCP_PROTOCOL_VERSION}


def parse_error_response() -> dict[str, Any]:
    return _error_response(None, JSONRPC_PARSE_ERROR, "Parse error")


def handle_mcp_message(message: Any, tools: AIHILToolService) -> dict[str, Any] | list[dict[str, Any]] | None:
    if isinstance(message, list):
        responses = [_handle_single_mcp_message(item, tools) for item in message]
        responses = [response for response in responses if response is not None]
        return responses or None
    return _handle_single_mcp_message(message, tools)


def _handle_single_mcp_message(message: Any, tools: AIHILToolService) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return _error_response(None, JSONRPC_INVALID_REQUEST, "Invalid Request")
    request_id = message.get("id")
    is_notification = "id" not in message
    if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return None if is_notification else _error_response(request_id, JSONRPC_INVALID_REQUEST, "Invalid Request")

    method = message["method"]
    if is_notification:
        return None

    try:
        return _handle_method(request_id, method, message.get("params", {}), tools)
    except ValueError as exc:
        return _error_response(
            request_id,
            JSONRPC_INVALID_PARAMS,
            "Invalid params",
            {"summary": str(exc)},
        )
    except Exception as exc:  # pragma: no cover - defensive JSON-RPC boundary
        return _error_response(
            request_id,
            JSONRPC_INTERNAL_ERROR,
            "Internal error",
            {"summary": str(exc)},
        )


def _handle_method(
    request_id: Any,
    method: str,
    params: Any,
    tools: AIHILToolService,
) -> dict[str, Any]:
    if method == "initialize":
        params = _params_object(params)
        protocol_version = str(params.get("protocolVersion") or MCP_PROTOCOL_VERSION)
        return _result_response(
            request_id,
            {
                "protocolVersion": protocol_version,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "prompts": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                },
                "serverInfo": {
                    "name": "aihil",
                    "version": "0.1.0",
                },
            },
        )
    if method == "ping":
        return _result_response(request_id, {})
    if method == "tools/list":
        return _result_response(request_id, {"tools": MCP_TOOLS})
    if method == "tools/call":
        return _result_response(request_id, _call_tool(params, tools))
    if method == "prompts/list":
        return _result_response(request_id, {"prompts": MCP_PROMPTS})
    if method == "prompts/get":
        return _result_response(request_id, _get_prompt(params))
    if method in {"resources/list", "resources/templates/list"}:
        key = "resourceTemplates" if method == "resources/templates/list" else "resources"
        return _result_response(request_id, {key: []})
    return _error_response(request_id, JSONRPC_METHOD_NOT_FOUND, "Method not found", {"method": method})


def _call_tool(params: Any, tools: AIHILToolService) -> dict[str, Any]:
    params = _params_object(params)
    name = params.get("name")
    arguments = params.get("arguments", {})
    if not isinstance(name, str):
        return _tool_error("unknown", "invalid_argument", "tools/call requires a string name.")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return _tool_error(name, "invalid_argument", "tools/call arguments must be an object.")

    result = tools.call(name, arguments)
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(result, indent=2, sort_keys=True),
            }
        ],
        "structuredContent": result,
        "isError": result.get("ok") is False,
    }


def _get_prompt(params: Any) -> dict[str, Any]:
    params = _params_object(params)
    name = params.get("name")
    if name != "aihil_embedded_workflow":
        return {
            "description": "Unknown AI-HIL prompt.",
            "messages": [
                {
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": "Unknown AI-HIL prompt. Use aihil_embedded_workflow.",
                    },
                }
            ],
        }
    return {
        "description": "Safe workflow for using AI-HIL hardware tools from an AI agent.",
        "messages": [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": AIHIL_WORKFLOW_PROMPT,
                },
            }
        ],
    }


def _params_object(params: Any) -> dict[str, Any]:
    if params is None:
        return {}
    if isinstance(params, dict):
        return params
    raise ValueError("JSON-RPC params must be an object.")


def _tool_error(tool: str, error_type: str, summary: str) -> dict[str, Any]:
    result = {
        "ok": False,
        "tool": tool,
        "error_type": error_type,
        "summary": summary,
    }
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(result, indent=2, sort_keys=True),
            }
        ],
        "structuredContent": result,
        "isError": True,
    }


def _result_response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": result,
    }


def _error_response(
    request_id: Any,
    code: int,
    message: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {
        "code": code,
        "message": message,
    }
    if data is not None:
        error["data"] = data
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": error,
    }
