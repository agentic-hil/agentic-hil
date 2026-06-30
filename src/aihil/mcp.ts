import type { JsonObject } from "./types.js";
import type { AIHILToolService } from "./tools.js";
import { packageVersion } from "./version.js";

export const MCP_PROTOCOL_VERSION = "2025-06-18";

const JSONRPC_PARSE_ERROR = -32700;
const JSONRPC_INVALID_REQUEST = -32600;
const JSONRPC_METHOD_NOT_FOUND = -32601;
const JSONRPC_INVALID_PARAMS = -32602;
const JSONRPC_INTERNAL_ERROR = -32603;

const EMPTY_OBJECT_SCHEMA: JsonObject = {
  type: "object",
  properties: {},
  additionalProperties: false,
};

export const MCP_TOOLS: JsonObject[] = [
  {
    name: "aihil_debugger_info",
    description: "Check whether the configured debugger backend is available.",
    inputSchema: EMPTY_OBJECT_SCHEMA,
  },
  {
    name: "aihil_probe_target",
    description: "Probe the configured embedded target through the configured debugger.",
    inputSchema: EMPTY_OBJECT_SCHEMA,
  },
  {
    name: "aihil_artifact_upload",
    description: "Upload a local or base64-encoded firmware artifact into the configured AI-HIL artifact store.",
    inputSchema: {
      type: "object",
      properties: {
        image_path: {
          type: "string",
          description: "Local firmware path under an allowed artifact root, for example build/firmware.elf.",
        },
        filename: {
          type: "string",
          description: "Original firmware filename for data_base64 uploads, used for extension validation.",
        },
        data_base64: {
          type: "string",
          description: "Padded base64-encoded firmware bytes.",
        },
      },
      oneOf: [{ required: ["image_path"] }, { required: ["filename", "data_base64"] }],
      additionalProperties: false,
    },
  },
  {
    name: "aihil_flash_firmware",
    description: "Flash a validated firmware artifact to the configured target. Provide exactly one of image_path or artifact_id.",
    inputSchema: {
      type: "object",
      properties: {
        image_path: {
          type: "string",
          description: "Local firmware path under an allowed artifact root, for example build/firmware.elf.",
        },
        artifact_id: {
          type: "string",
          description: "Uploaded artifact id, if artifact upload support is available.",
        },
      },
      oneOf: [{ required: ["image_path"] }, { required: ["artifact_id"] }],
      additionalProperties: false,
    },
  },
  {
    name: "aihil_reset_target",
    description: "Reset the configured target through the configured debugger.",
    inputSchema: {
      type: "object",
      properties: {
        mode: {
          type: "string",
          enum: ["run", "halt", "init"],
          default: "run",
          description: "Reset mode. Use run unless the task requires halt or init.",
        },
      },
      additionalProperties: false,
    },
  },
  {
    name: "aihil_get_last_report",
    description: "Return the most recent structured AI-HIL report.",
    inputSchema: EMPTY_OBJECT_SCHEMA,
  },
  {
    name: "aihil_classify_last_error",
    description: "Classify the most recent AI-HIL/debugger failure.",
    inputSchema: EMPTY_OBJECT_SCHEMA,
  },
  {
    name: "aihil_com_ports_list",
    description: "List configured named COM ports, streaming session status, and detected host serial/COM ports.",
    inputSchema: EMPTY_OBJECT_SCHEMA,
  },
  {
    name: "aihil_com_session_start",
    description: "Open a configured COM port and start a background streaming feedback session.",
    inputSchema: {
      type: "object",
      properties: {
        port_id: {
          type: "string",
          description: "Configured COM port id from .aihil/config.yaml, for example dut_uart.",
        },
        clear_buffer: {
          type: "boolean",
          default: true,
          description: "Clear any existing session buffer when the session is already active.",
        },
      },
      required: ["port_id"],
      additionalProperties: false,
    },
  },
  {
    name: "aihil_com_session_stop",
    description: "Stop a configured COM port streaming feedback session and close the port.",
    inputSchema: {
      type: "object",
      properties: {
        port_id: {
          type: "string",
          description: "Configured COM port id from .aihil/config.yaml, for example dut_uart.",
        },
      },
      required: ["port_id"],
      additionalProperties: false,
    },
  },
  {
    name: "aihil_com_write",
    description: "Write a text or hexadecimal stimulus to an active configured COM port session.",
    inputSchema: {
      type: "object",
      properties: {
        port_id: {
          type: "string",
          description: "Configured COM port id from .aihil/config.yaml, for example dut_uart.",
        },
        text: {
          type: "string",
          description: "Text stimulus encoded with the configured port encoding.",
        },
        hex: {
          type: "string",
          description: "Hexadecimal bytes stimulus, spaces allowed, for binary protocols.",
        },
      },
      required: ["port_id"],
      oneOf: [{ required: ["text"] }, { required: ["hex"] }],
      additionalProperties: false,
    },
  },
  {
    name: "aihil_com_read",
    description: "Read buffered feedback from an active configured COM port streaming session.",
    inputSchema: {
      type: "object",
      properties: {
        port_id: {
          type: "string",
          description: "Configured COM port id from .aihil/config.yaml, for example dut_uart.",
        },
        max_bytes: {
          type: "integer",
          minimum: 1,
          description: "Maximum buffered bytes to consume from the feedback session.",
        },
        wait_timeout_s: {
          type: "number",
          minimum: 0,
          default: 0,
          description: "Optional wait time for feedback if the buffer is initially empty.",
        },
      },
      required: ["port_id"],
      additionalProperties: false,
    },
  },
  {
    name: "aihil_can_buses_list",
    description: "List configured named CAN buses, adapter types, and active session status.",
    inputSchema: EMPTY_OBJECT_SCHEMA,
  },
  {
    name: "aihil_can_session_start",
    description: "Open a configured CAN bus and start a session for CAN frame send/read operations.",
    inputSchema: {
      type: "object",
      properties: {
        bus_id: {
          type: "string",
          description: "Configured CAN bus id from .aihil/config.yaml, for example dut_can.",
        },
        clear_rx_queue: {
          type: "boolean",
          default: true,
          description: "Clear the adapter receive queue when the session starts, if the backend supports it.",
        },
      },
      required: ["bus_id"],
      additionalProperties: false,
    },
  },
  {
    name: "aihil_can_session_stop",
    description: "Stop a configured CAN bus session and close the adapter channel.",
    inputSchema: {
      type: "object",
      properties: {
        bus_id: {
          type: "string",
          description: "Configured CAN bus id from .aihil/config.yaml, for example dut_can.",
        },
      },
      required: ["bus_id"],
      additionalProperties: false,
    },
  },
  {
    name: "aihil_can_send",
    description: "Send one classic CAN frame on an active configured CAN bus session.",
    inputSchema: {
      type: "object",
      properties: {
        bus_id: {
          type: "string",
          description: "Configured CAN bus id from .aihil/config.yaml, for example dut_can.",
        },
        frame_id: {
          oneOf: [{ type: "integer", minimum: 0 }, { type: "string" }],
          description: "CAN frame id as integer or string, for example 291 or 0x123.",
        },
        extended: {
          type: "boolean",
          default: false,
          description: "Use a 29-bit extended CAN identifier instead of an 11-bit standard identifier.",
        },
        rtr: {
          type: "boolean",
          default: false,
          description: "Send a remote transmission request frame.",
        },
        data_hex: {
          type: "string",
          default: "",
          description: "CAN payload bytes as hexadecimal text, spaces allowed, for example '01 02 ff'.",
        },
      },
      required: ["bus_id", "frame_id"],
      additionalProperties: false,
    },
  },
  {
    name: "aihil_can_read",
    description: "Read CAN frames from an active configured CAN bus session.",
    inputSchema: {
      type: "object",
      properties: {
        bus_id: {
          type: "string",
          description: "Configured CAN bus id from .aihil/config.yaml, for example dut_can.",
        },
        max_frames: {
          type: "integer",
          minimum: 1,
          description: "Maximum CAN frames to consume from the adapter receive queue.",
        },
        wait_timeout_s: {
          type: "number",
          minimum: 0,
          default: 0,
          description: "Optional wait time for at least one CAN frame if the receive queue is initially empty.",
        },
      },
      required: ["bus_id"],
      additionalProperties: false,
    },
  },
];

const AIHIL_WORKFLOW_PROMPT = `Use AI-HIL as the safe gate to the configured embedded hardware.

Workflow:
1. Build the firmware first.
2. Check debugger availability with aihil_debugger_info if setup is unclear.
3. Probe the target before flashing.
4. Flash with a validated image_path from configured allowed roots, or upload first with aihil_artifact_upload when you need an artifact_id.
5. Read the structured result after every hardware action.
6. Reset only when needed or when the task explicitly requires it.
7. For serial stimuli and feedback, use only configured COM port ids, start a session before writing or reading, and stop the session when done.
8. For CAN stimuli and feedback, use only configured CAN bus ids, start a CAN session before sending or reading frames, and stop the session when done.
9. If ok is false, diagnose using error_type, backend_error_type, likely_causes, report_path, and log_path before changing code again.

Safety rules:
- Do not request raw OpenOCD or debugger commands.
- Do not request arbitrary shell access for hardware actions.
- Do not flash files outside configured artifact roots.
- Do not open CAN adapters directly outside configured AI-HIL CAN tools.
- Treat permission_denied as authoritative and stop.
`;

const MCP_PROMPTS: JsonObject[] = [
  {
    name: "aihil_embedded_workflow",
    description: "Safe workflow for using AI-HIL hardware tools from an AI agent.",
  },
];

export function parseErrorResponse(): JsonObject {
  return errorResponse(null, JSONRPC_PARSE_ERROR, "Parse error");
}

export async function handleMcpMessage(
  message: unknown,
  tools: AIHILToolService,
): Promise<JsonObject | JsonObject[] | null> {
  if (Array.isArray(message)) {
    const responses: JsonObject[] = [];
    for (const item of message) {
      const response = await handleSingleMcpMessage(item, tools);
      if (response !== null) {
        responses.push(response);
      }
    }
    return responses.length > 0 ? responses : null;
  }
  return handleSingleMcpMessage(message, tools);
}

async function handleSingleMcpMessage(message: unknown, tools: AIHILToolService): Promise<JsonObject | null> {
  if (!isRecord(message)) {
    return errorResponse(null, JSONRPC_INVALID_REQUEST, "Invalid Request");
  }
  const requestId = message.id;
  const isNotification = !("id" in message);
  if (message.jsonrpc !== "2.0" || typeof message.method !== "string") {
    return isNotification ? null : errorResponse(requestId, JSONRPC_INVALID_REQUEST, "Invalid Request");
  }
  if (isNotification) {
    return null;
  }

  try {
    return await handleMethod(requestId, message.method, message.params ?? {}, tools);
  } catch (error) {
    if (error instanceof TypeError || error instanceof RangeError || error instanceof Error) {
      return errorResponse(requestId, JSONRPC_INVALID_PARAMS, "Invalid params", { summary: error.message });
    }
    return errorResponse(requestId, JSONRPC_INTERNAL_ERROR, "Internal error", { summary: String(error) });
  }
}

async function handleMethod(requestId: unknown, method: string, params: unknown, tools: AIHILToolService): Promise<JsonObject> {
  if (method === "initialize") {
    const paramsObject = paramsObjectOrThrow(params);
    const protocolVersion = String(paramsObject.protocolVersion ?? MCP_PROTOCOL_VERSION);
    return resultResponse(requestId, {
      protocolVersion,
      capabilities: {
        tools: { listChanged: false },
        prompts: { listChanged: false },
        resources: { subscribe: false, listChanged: false },
      },
      serverInfo: {
        name: "aihil",
        version: packageVersion(),
      },
    });
  }
  if (method === "ping") {
    return resultResponse(requestId, {});
  }
  if (method === "tools/list") {
    return resultResponse(requestId, { tools: MCP_TOOLS });
  }
  if (method === "tools/call") {
    return resultResponse(requestId, await callTool(params, tools));
  }
  if (method === "prompts/list") {
    return resultResponse(requestId, { prompts: MCP_PROMPTS });
  }
  if (method === "prompts/get") {
    return resultResponse(requestId, getPrompt(params));
  }
  if (method === "resources/list" || method === "resources/templates/list") {
    return resultResponse(requestId, { [method === "resources/templates/list" ? "resourceTemplates" : "resources"]: [] });
  }
  return errorResponse(requestId, JSONRPC_METHOD_NOT_FOUND, "Method not found", { method });
}

async function callTool(params: unknown, tools: AIHILToolService): Promise<JsonObject> {
  const paramsObject = paramsObjectOrThrow(params);
  const name = paramsObject.name;
  let arguments_ = paramsObject.arguments ?? {};
  if (typeof name !== "string") {
    return toolError("unknown", "invalid_argument", "tools/call requires a string name.");
  }
  if (arguments_ === null) {
    arguments_ = {};
  }
  if (!isRecord(arguments_)) {
    return toolError(name, "invalid_argument", "tools/call arguments must be an object.");
  }

  const result = await tools.call(name, arguments_);
  return {
    content: [
      {
        type: "text",
        text: JSON.stringify(result, null, 2),
      },
    ],
    structuredContent: result,
    isError: result.ok === false,
  };
}

function getPrompt(params: unknown): JsonObject {
  const paramsObject = paramsObjectOrThrow(params);
  if (paramsObject.name !== "aihil_embedded_workflow") {
    return {
      description: "Unknown AI-HIL prompt.",
      messages: [
        {
          role: "user",
          content: {
            type: "text",
            text: "Unknown AI-HIL prompt. Use aihil_embedded_workflow.",
          },
        },
      ],
    };
  }
  return {
    description: "Safe workflow for using AI-HIL hardware tools from an AI agent.",
    messages: [
      {
        role: "user",
        content: {
          type: "text",
          text: AIHIL_WORKFLOW_PROMPT,
        },
      },
    ],
  };
}

function paramsObjectOrThrow(params: unknown): JsonObject {
  if (params === null || params === undefined) {
    return {};
  }
  if (isRecord(params)) {
    return params;
  }
  throw new Error("JSON-RPC params must be an object.");
}

function toolError(tool: string, errorType: string, summary: string): JsonObject {
  const result = {
    ok: false,
    tool,
    error_type: errorType,
    summary,
  };
  return {
    content: [
      {
        type: "text",
        text: JSON.stringify(result, null, 2),
      },
    ],
    structuredContent: result,
    isError: true,
  };
}

function resultResponse(requestId: unknown, result: JsonObject): JsonObject {
  return {
    jsonrpc: "2.0",
    id: requestId,
    result,
  };
}

function errorResponse(requestId: unknown, code: number, message: string, data?: JsonObject): JsonObject {
  const error: JsonObject = { code, message };
  if (data !== undefined) {
    error.data = data;
  }
  return {
    jsonrpc: "2.0",
    id: requestId,
    error,
  };
}

function isRecord(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
