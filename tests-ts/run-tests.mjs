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
import { initConfig, main, schema } from "../dist/main.js";
import { Readable, Writable } from "node:stream";
import { fc, safePathSegment } from "./property-arbitraries.js";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const fakeOpenocd = path.join(root, "tests-ts", "fixtures", "fake-openocd.js").replace(/\\/g, "/");
const fakeStlink = path.join(root, "tests-ts", "fixtures", "fake-stlink.js").replace(/\\/g, "/");
const fakeStlinkUnconfirmed = path.join(root, "tests-ts", "fixtures", "fake-stlink-unconfirmed.js").replace(/\\/g, "/");
const tests = [];

function test(name, fn) {
  tests.push({ name, fn });
}

function tempDir() {
  return mkdtempSync(path.join(tmpdir(), "aihil-ts-"));
}

async function captureStdout(fn) {
  let output = "";
  const originalWrite = process.stdout.write;
  process.stdout.write = (chunk, encoding, callback) => {
    output += String(chunk);
    if (typeof encoding === "function") {
      encoding();
    } else if (typeof callback === "function") {
      callback();
    }
    return true;
  };
  try {
    const result = await fn();
    return { result, output };
  } finally {
    process.stdout.write = originalWrite;
  }
}

function writeConfig(directory, options = {}) {
  const allowUpload = options.allowUpload ?? true;
  const maxUploadSizeMb = options.maxUploadSizeMb ?? 1;
  const probeId = options.probeId ?? null;
  const debuggerType = options.debuggerType ?? "openocd";
  const debuggerExecutable = options.debuggerExecutable ?? (debuggerType === "stlink" ? fakeStlink : fakeOpenocd);
  const flashAddress = options.flashAddress ?? null;
  const configPath = path.join(directory, ".aihil", "config.yaml");
  mkdirSync(path.dirname(configPath), { recursive: true });
  writeFileSync(
    configPath,
    `target:
  name: "example-target"
  controller: "stm32f4"
debugger:
  type: "${debuggerType}"
  executable: ${JSON.stringify(debuggerExecutable)}
  probe_id: ${probeId === null ? "null" : JSON.stringify(probeId)}
  interface: "SWD"
  interface_cfg: "interface/stlink.cfg"
  target_cfg: "target/stm32f4x.cfg"
  flash_address: ${flashAddress === null ? "null" : `"${flashAddress}"`}
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

test("main supports help and version flags", async () => {
  const help = await captureStdout(() => main(["--help"]));
  assert.equal(help.result, 0);
  assert.match(help.output, /Usage:/);

  const version = await captureStdout(() => main(["--version"]));
  assert.equal(version.result, 0);
  assert.match(version.output.trim(), /^(\d+\.\d+\.\d+|unknown)$/);
});

test("packaged MCP template is portable", () => {
  const templatePath = path.join(root, "dist", "templates", "mcp.json");
  const result = JSON.parse(readFileSync(templatePath, "utf8"));
  assert.equal(result.mcpServers.aihil.command, "aihil");
  assert.deepEqual(result.mcpServers.aihil.args, ["mcp-stdio", "--config", ".aihil/config.yaml"]);
});

test("config loads defaults", () => {
  const directory = tempDir();
  try {
    const config = loadConfig(writeConfig(directory), directory);
    assert.equal(config.target.name, "example-target");
    assert.equal(config.debugger.probe_id, null);
    assert.deepEqual(config.artifacts.allowed_extensions, [".elf", ".hex", ".bin"]);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("openocd passes configured probe id", async () => {
  const directory = tempDir();
  try {
    await withService(
      directory,
      async (service) => {
        const probe = await mcpToolCall(service, "aihil_probe_target");
        assert.equal(probe.ok, true);
        const logPath = path.join(directory, probe.log_path);
        assert.match(readFileSync(logPath, "utf8"), /adapter serial STLINK123/);
      },
      { probeId: "STLINK123" },
    );
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("stlink backend probes and flashes with probe id", async () => {
  const directory = tempDir();
  try {
    const firmware = path.join(directory, "build", "firmware.elf");
    mkdirSync(path.dirname(firmware), { recursive: true });
    writeFileSync(firmware, Buffer.from([0x7f, 0x45, 0x4c, 0x46, 0x66]));
    await withService(
      directory,
      async (service) => {
        const info = await mcpToolCall(service, "aihil_debugger_info");
        assert.equal(info.ok, true);
        assert.equal(info.backend, "stlink");

        const probe = await mcpToolCall(service, "aihil_probe_target");
        assert.equal(probe.ok, true);
        assert.equal(probe.backend, "stlink");

        const flash = await mcpToolCall(service, "aihil_flash_firmware", { image_path: "build/firmware.elf" });
        assert.equal(flash.ok, true);
        assert.equal(flash.operation_result.confirmed, true);
        assert.deepEqual(flash.operation_result.matched_success_text, ["Download verified successfully"]);
        const logPath = path.join(directory, flash.log_path);
        const logText = readFileSync(logPath, "utf8");
        assert.match(logText, /port=SWD/);
        assert.match(logText, /sn=STLINK123/);
        assert.match(logText, /-w/);
        assert.match(logText, /-v/);
        assert.match(logText, /-rst/);
      },
      { debuggerType: "stlink", probeId: "STLINK123" },
    );
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("stlink rejects unconfirmed successful exit", async () => {
  const directory = tempDir();
  try {
    await withService(
      directory,
      async (service) => {
        const result = await mcpToolCall(service, "aihil_reset_target", { mode: "run" });
        assert.equal(result.ok, false);
        assert.equal(result.error_type, "reset_failed");
        assert.equal(result.backend_error_type, "reset_unconfirmed");
        assert.equal(result.operation_result.confirmed, false);
      },
      { debuggerType: "stlink", debuggerExecutable: fakeStlinkUnconfirmed },
    );
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("stlink requires flash address for bin artifacts", async () => {
  const directory = tempDir();
  try {
    const firmware = path.join(directory, "build", "firmware.bin");
    mkdirSync(path.dirname(firmware), { recursive: true });
    writeFileSync(firmware, Buffer.from([0x01, 0x02, 0x03, 0x04]));
    await withService(
      directory,
      async (service) => {
        const result = await mcpToolCall(service, "aihil_flash_firmware", { image_path: "build/firmware.bin" });
        assert.equal(result.ok, false);
        assert.equal(result.error_type, "invalid_argument");
        assert.match(result.summary, /debugger\.flash_address/);
      },
      { debuggerType: "stlink" },
    );
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("stlink command logs escape backslashes", async () => {
  const directory = tempDir();
  try {
    const debuggerExecutable = "tools\\fake-stlink.js";
    const resolvedFakeStlink = path.resolve(directory, debuggerExecutable);
    mkdirSync(path.dirname(resolvedFakeStlink), { recursive: true });
    writeFileSync(resolvedFakeStlink, readFileSync(fakeStlink, "utf8"));

    await withService(
      directory,
      async (service) => {
        const probe = await mcpToolCall(service, "aihil_probe_target");
        assert.equal(probe.ok, true);

        const log = JSON.parse(readFileSync(path.resolve(directory, String(probe.log_path)), "utf8"));
        assert.equal(log.command.includes("tools\\\\fake-stlink.js"), true);
      },
      { debuggerType: "stlink", debuggerExecutable },
    );
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

test("artifact validation rejects traversal segments for arbitrary paths", () => {
  const directory = tempDir();
  try {
    const config = loadConfig(writeConfig(directory), directory);
    const manager = new ArtifactManager(config);
    fc.assert(
      fc.property(fc.array(safePathSegment, { minLength: 0, maxLength: 3 }), safePathSegment, (prefixSegments, filename) => {
        const prefix = ["build", ...prefixSegments].join("/");
        const result = manager.validateLocalPath(`${prefix}/../${filename}.elf`);
        assert.equal(result.ok, false);
        assert.equal(result.error_type, "artifact_validation_failed");
        assert.equal(result.validation.path_traversal_safe, false);
      }),
      { numRuns: 100 },
    );
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

test("flash command escapes Tcl-special artifact paths in logs", async () => {
  const directory = tempDir();
  try {
    const filename = "firmware $[name].elf";
    const firmware = path.join(directory, "build", filename);
    mkdirSync(path.dirname(firmware), { recursive: true });
    writeFileSync(firmware, Buffer.from([0x7f, 0x45, 0x4c, 0x46, 0x66]));
    await withService(directory, async (service) => {
      const flash = await mcpToolCall(service, "aihil_flash_firmware", { image_path: `build/${filename}` });
      assert.equal(flash.ok, true);

      const log = JSON.parse(readFileSync(path.resolve(directory, String(flash.log_path)), "utf8"));
      assert.match(log.command, /program \\".*firmware \\\\\$\\\\\[name\\\\\]\.elf\\" verify reset/);
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
