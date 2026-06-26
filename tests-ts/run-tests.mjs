import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { ArtifactManager } from "../dist/artifacts.js";
import { loadConfig } from "../dist/config.js";
import { handleMcpMessage } from "../dist/mcp.js";
import { runStdioServer } from "../dist/stdio.js";
import { AIHILToolService } from "../dist/tools.js";
import { initConfig, mcpConfig, schema } from "../dist/main.js";
import { Readable, Writable } from "node:stream";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const fakeOpenocd = path.join(root, "tests-ts", "fixtures", "fake-openocd.js").replace(/\\/g, "/");
const tests = [];

function test(name, fn) {
  tests.push({ name, fn });
}

function tempDir() {
  return mkdtempSync(path.join(tmpdir(), "aihil-ts-"));
}

function writeConfig(directory, options = {}) {
  const allowUpload = options.allowUpload ?? true;
  const maxUploadSizeMb = options.maxUploadSizeMb ?? 1;
  const configPath = path.join(directory, ".aihil", "config.yaml");
  mkdirSync(path.dirname(configPath), { recursive: true });
  writeFileSync(
    configPath,
    `target:
  name: "example-target"
  controller: "stm32f4"
debugger:
  type: "openocd"
  executable: "${fakeOpenocd}"
  interface_cfg: "interface/stlink.cfg"
  target_cfg: "target/stm32f4x.cfg"
  timeout_s: 5
artifacts:
  allowed_roots: ["build"]
  allowed_extensions: [".elf", ".hex", ".bin"]
  upload_directory: ".aihil/artifacts"
  max_upload_size_mb: ${maxUploadSizeMb}
  allow_upload: ${allowUpload ? "true" : "false"}
reports:
  directory: ".aihil/reports"
logs:
  directory: ".aihil/logs"
`,
    "utf8",
  );
  return configPath;
}

async function withService(directory, fn, configOptions = {}) {
  const service = new AIHILToolService(loadConfig(writeConfig(directory, configOptions), directory));
  try {
    return await fn(service);
  } finally {
    await service.close();
  }
}

async function mcpToolCall(service, name, arguments_ = {}) {
  const response = await handleMcpMessage(
    {
      jsonrpc: "2.0",
      id: name,
      method: "tools/call",
      params: { name, arguments: arguments_ },
    },
    service,
  );
  assert.ok(response);
  return response.result.structuredContent;
}

test("initConfig writes starter config", async () => {
  const directory = tempDir();
  try {
    const configPath = path.join(directory, ".aihil", "config.yaml");
    const result = await initConfig(configPath);
    assert.equal(result.ok, true);
    assert.match(readFileSync(configPath, "utf8"), /target:/);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("schema exports bundled config schema", () => {
  const directory = tempDir();
  try {
    const schemaPath = path.join(directory, "config.schema.json");
    const result = schema(schemaPath);
    assert.equal(result.ok, true);
    assert.match(readFileSync(schemaPath, "utf8"), /AI-HIL project configuration/);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("mcpConfig uses stdio command", () => {
  const result = mcpConfig("custom.yaml");
  assert.deepEqual(result.mcpServers.aihil, {
    command: "aihil",
    args: ["mcp-stdio", "--config", "custom.yaml"],
  });
});

test("config loads defaults", () => {
  const directory = tempDir();
  try {
    const config = loadConfig(writeConfig(directory), directory);
    assert.equal(config.target.name, "example-target");
    assert.deepEqual(config.artifacts.allowed_extensions, [".elf", ".hex", ".bin"]);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("artifact validation computes sha256", () => {
  const directory = tempDir();
  try {
    const config = loadConfig(writeConfig(directory), directory);
    const firmware = path.join(directory, "build", "firmware.elf");
    mkdirSync(path.dirname(firmware), { recursive: true });
    const data = Buffer.from([0x7f, 0x45, 0x4c, 0x46, 0x66, 0x61, 0x6b, 0x65]);
    writeFileSync(firmware, data);
    const result = new ArtifactManager(config).validateLocalPath("build/firmware.elf");
    assert.equal(result.ok, true);
    assert.equal(result.artifact.sha256, createHash("sha256").update(data).digest("hex"));
    assert.equal(result.validation.sha256_computed, true);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("artifact validation blocks outside root", () => {
  const directory = tempDir();
  try {
    const config = loadConfig(writeConfig(directory), directory);
    const firmware = path.join(directory, "other", "firmware.elf");
    mkdirSync(path.dirname(firmware), { recursive: true });
    writeFileSync(firmware, Buffer.from([0x7f, 0x45, 0x4c, 0x46]));
    const result = new ArtifactManager(config).validateLocalPath("other/firmware.elf");
    assert.equal(result.ok, false);
    assert.equal(result.error_type, "artifact_validation_failed");
    assert.equal(result.validation.allowed_root, false);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("artifact upload stores and resolves artifact ids", () => {
  const directory = tempDir();
  try {
    const config = loadConfig(writeConfig(directory), directory);
    const manager = new ArtifactManager(config);
    const data = Buffer.from([0x7f, 0x45, 0x4c, 0x46, 0x66, 0x61, 0x6b, 0x65]);
    const uploaded = manager.upload({ filename: "firmware.elf", data_base64: data.toString("base64") });
    assert.equal(uploaded.ok, true);
    assert.match(uploaded.artifact_id, /^[a-f0-9]{64}\.elf$/);
    assert.equal(uploaded.artifact.source, "upload");
    assert.equal(uploaded.artifact.sha256, createHash("sha256").update(data).digest("hex"));

    const resolved = manager.resolveArtifactId(uploaded.artifact_id);
    assert.equal(resolved.ok, true);
    assert.equal(resolved.artifact.source, "upload");
    assert.equal(resolved.artifact.artifact_id, uploaded.artifact_id);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("artifact upload accepts local image paths", () => {
  const directory = tempDir();
  try {
    const config = loadConfig(writeConfig(directory), directory);
    const firmware = path.join(directory, "build", "firmware.elf");
    mkdirSync(path.dirname(firmware), { recursive: true });
    const data = Buffer.from([0x7f, 0x45, 0x4c, 0x46, 0x66]);
    writeFileSync(firmware, data);

    const uploaded = new ArtifactManager(config).upload({ image_path: "build/firmware.elf" });
    assert.equal(uploaded.ok, true);
    assert.match(uploaded.artifact_id, /^[a-f0-9]{64}\.elf$/);
    assert.equal(uploaded.artifact.source, "upload");
    assert.equal(uploaded.artifact.source_path, "build/firmware.elf");
    assert.equal(uploaded.artifact.sha256, createHash("sha256").update(data).digest("hex"));
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("artifact upload honors allow_upload", () => {
  const directory = tempDir();
  try {
    const config = loadConfig(writeConfig(directory, { allowUpload: false }), directory);
    const result = new ArtifactManager(config).upload({ filename: "firmware.elf", data_base64: "fw==" });
    assert.equal(result.ok, false);
    assert.equal(result.error_type, "permission_denied");
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("artifact upload rejects oversized payloads", () => {
  const directory = tempDir();
  try {
    const config = loadConfig(writeConfig(directory, { maxUploadSizeMb: 0 }), directory);
    const result = new ArtifactManager(config).upload({ filename: "firmware.bin", data_base64: "AA==" });
    assert.equal(result.ok, false);
    assert.equal(result.error_type, "artifact_too_large");
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("mcp initialize and tools/list", async () => {
  const directory = tempDir();
  try {
    await withService(directory, async (service) => {
      const initialized = await handleMcpMessage({ jsonrpc: "2.0", id: 1, method: "initialize", params: {} }, service);
      assert.equal(initialized.result.serverInfo.name, "aihil");
      const listed = await handleMcpMessage({ jsonrpc: "2.0", id: "tools", method: "tools/list" }, service);
      const toolNames = new Set(listed.result.tools.map((tool) => tool.name));
      assert.equal(toolNames.has("aihil_probe_target"), true);
      assert.equal(toolNames.has("aihil_artifact_upload"), true);
      assert.equal(toolNames.has("aihil_flash_firmware"), true);
    });
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("mcp uploads and flashes artifact ids", async () => {
  const directory = tempDir();
  try {
    await withService(directory, async (service) => {
      const data = Buffer.from([0x7f, 0x45, 0x4c, 0x46, 0x66]);
      const upload = await mcpToolCall(service, "aihil_artifact_upload", {
        filename: "firmware.elf",
        data_base64: data.toString("base64"),
      });
      assert.equal(upload.ok, true);
      assert.match(upload.artifact_id, /^[a-f0-9]{64}\.elf$/);

      const flash = await mcpToolCall(service, "aihil_flash_firmware", { artifact_id: upload.artifact_id });
      assert.equal(flash.ok, true);
      assert.equal(flash.artifact.source, "upload");
      assert.equal(flash.artifact.path, upload.artifact.path);
    });
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("mcp uploads local image paths and flashes artifact ids", async () => {
  const directory = tempDir();
  try {
    const firmware = path.join(directory, "build", "firmware.elf");
    mkdirSync(path.dirname(firmware), { recursive: true });
    writeFileSync(firmware, Buffer.from([0x7f, 0x45, 0x4c, 0x46, 0x66]));
    await withService(directory, async (service) => {
      const upload = await mcpToolCall(service, "aihil_artifact_upload", { image_path: "build/firmware.elf" });
      assert.equal(upload.ok, true);
      assert.equal(upload.artifact.source_path, "build/firmware.elf");

      const flash = await mcpToolCall(service, "aihil_flash_firmware", { artifact_id: upload.artifact_id });
      assert.equal(flash.ok, true);
      assert.equal(flash.artifact.source, "upload");
      assert.equal(flash.artifact.path, upload.artifact.path);
    });
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("mcp tool calls debugger and flash paths", async () => {
  const directory = tempDir();
  try {
    const firmware = path.join(directory, "build", "firmware.elf");
    mkdirSync(path.dirname(firmware), { recursive: true });
    writeFileSync(firmware, Buffer.from([0x7f, 0x45, 0x4c, 0x46, 0x66]));
    await withService(directory, async (service) => {
      assert.equal((await mcpToolCall(service, "aihil_debugger_info")).ok, true);
      assert.equal((await mcpToolCall(service, "aihil_probe_target")).ok, true);
      const flash = await mcpToolCall(service, "aihil_flash_firmware", { image_path: "build/firmware.elf" });
      assert.equal(flash.ok, true);
      assert.ok(flash.artifact.sha256);
      const lastReport = await mcpToolCall(service, "aihil_get_last_report");
      assert.equal(lastReport.ok, true);
      assert.equal(lastReport.report.tool, "aihil_flash_firmware");
    });
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("mcp rejects invalid reset mode", async () => {
  const directory = tempDir();
  try {
    await withService(directory, async (service) => {
      const result = await mcpToolCall(service, "aihil_reset_target", { mode: "bad" });
      assert.equal(result.ok, false);
      assert.equal(result.error_type, "invalid_argument");
    });
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("stdio server handles line-delimited json", async () => {
  const directory = tempDir();
  try {
    const config = loadConfig(writeConfig(directory), directory);
    const input = Readable.from([JSON.stringify({ jsonrpc: "2.0", id: "tools", method: "tools/list" }) + "\n"]);
    let outputText = "";
    const output = new Writable({
      write(chunk, _encoding, callback) {
        outputText += chunk.toString();
        callback();
      },
    });
    const status = await runStdioServer(config, input, output);
    assert.equal(status, 0);
    assert.equal(JSON.parse(outputText).id, "tools");
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

let failed = false;
for (const { name, fn } of tests) {
  try {
    await fn();
    console.log(`ok - ${name}`);
  } catch (error) {
    failed = true;
    console.error(`not ok - ${name}`);
    console.error(error);
  }
}

if (failed) {
  process.exitCode = 1;
}
