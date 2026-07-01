import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { writeFileSync } from "node:fs";

export interface GdbMiCommandResult {
  resultClass: string;
  line: string | null;
  records: string[];
  timedOut: boolean;
  errorMessage?: string;
}

export interface GdbMiStopResult {
  line: string | null;
  reason: string;
  timedOut: boolean;
  errorMessage?: string;
}

interface PendingCommand {
  token: number;
  command: string;
  records: string[];
  timeout: NodeJS.Timeout;
  resolve: (result: GdbMiCommandResult) => void;
}

interface PendingStop {
  timeout: NodeJS.Timeout;
  resolve: (result: GdbMiStopResult) => void;
}

export class GdbMiClient {
  readonly child: ChildProcessWithoutNullStreams;
  readonly commandHistory: Array<Record<string, unknown>> = [];
  stdoutText = "";
  stderrText = "";
  private stdoutRemainder = "";
  private stderrRemainder = "";
  private nextToken = 0;
  private pending: PendingCommand | null = null;
  private pendingStop: PendingStop | null = null;
  private lastStopLine: string | null = null;
  private exited = false;

  constructor(executablePath: string, cwd: string) {
    const invocation = executablePath.endsWith(".js") || executablePath.endsWith(".mjs")
      ? [process.execPath, executablePath]
      : [executablePath];
    this.child = spawn(invocation[0], [...invocation.slice(1), "--nx", "--quiet", "--interpreter=mi2"], {
      cwd,
      windowsHide: true,
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.child.stdout.on("data", (chunk) => this.handleStdout(chunk));
    this.child.stderr.on("data", (chunk) => this.handleStderr(chunk));
    this.child.on("exit", (code, signal) => this.handleExit(code, signal));
    this.child.on("error", (error) => this.handleProcessError(error));
  }

  command(miCommand: string, timeoutSeconds: number): Promise<GdbMiCommandResult> {
    if (this.pending !== null) {
      return Promise.resolve({
        resultClass: "error",
        line: null,
        records: [],
        timedOut: false,
        errorMessage: "Another GDB/MI command is still pending.",
      });
    }
    if (this.exited || this.child.stdin.destroyed) {
      return Promise.resolve({
        resultClass: "error",
        line: null,
        records: [],
        timedOut: false,
        errorMessage: "GDB process is not running.",
      });
    }
    if (miCommand.includes("\n") || miCommand.includes("\r")) {
      return Promise.resolve({
        resultClass: "error",
        line: null,
        records: [],
        timedOut: false,
        errorMessage: "GDB/MI command must be a single line.",
      });
    }

    const token = ++this.nextToken;
    return new Promise((resolve) => {
      const timeout = setTimeout(() => {
        if (this.pending?.token === token) {
          const records = this.pending.records;
          this.pending = null;
          const result = {
            resultClass: "timeout",
            line: null,
            records,
            timedOut: true,
            errorMessage: "GDB/MI command timed out.",
          };
          this.commandHistory.push({ token, command: miCommand, result_class: result.resultClass, timed_out: true });
          resolve(result);
        }
      }, Math.max(0, timeoutSeconds) * 1000);

      this.pending = { token, command: miCommand, records: [], timeout, resolve };
      this.child.stdin.write(`${token}${miCommand}\n`, (error) => {
        if (error !== undefined && error !== null && this.pending?.token === token) {
          clearTimeout(timeout);
          this.pending = null;
          const result = {
            resultClass: "error",
            line: null,
            records: [],
            timedOut: false,
            errorMessage: error.message,
          };
          this.commandHistory.push({ token, command: miCommand, result_class: result.resultClass, error: error.message });
          resolve(result);
        }
      });
    });
  }

  async waitForStop(timeoutSeconds: number): Promise<GdbMiStopResult> {
    if (this.lastStopLine !== null) {
      const line = this.lastStopLine;
      this.lastStopLine = null;
      return { line, reason: miField(line, "reason") ?? "unknown", timedOut: false };
    }
    if (this.pendingStop !== null) {
      return { line: null, reason: "debugger_error", timedOut: false, errorMessage: "Another GDB stop wait is still pending." };
    }
    if (this.exited) {
      return { line: null, reason: "debugger_error", timedOut: false, errorMessage: "GDB process is not running." };
    }
    return new Promise((resolve) => {
      const timeout = setTimeout(() => {
        if (this.pendingStop !== null) {
          this.pendingStop = null;
          resolve({ line: null, reason: "timeout", timedOut: true, errorMessage: "Timed out waiting for target stop." });
        }
      }, Math.max(0, timeoutSeconds) * 1000);
      this.pendingStop = { timeout, resolve };
    });
  }

  async close(timeoutSeconds: number): Promise<void> {
    if (!this.exited && !this.child.stdin.destroyed) {
      await this.command("-gdb-exit", timeoutSeconds);
    }
    if (!this.exited) {
      await waitForExit(this.child, Math.max(0, timeoutSeconds) * 1000);
    }
    if (!this.exited) {
      this.child.kill();
    }
  }

  kill(): void {
    if (!this.exited) {
      this.child.kill();
    }
  }

  private handleStdout(chunk: Buffer | string): void {
    const text = Buffer.isBuffer(chunk) ? chunk.toString("utf8") : chunk;
    this.stdoutText += text;
    const lines = `${this.stdoutRemainder}${text}`.split(/\r?\n/);
    this.stdoutRemainder = lines.pop() ?? "";
    for (const line of lines) {
      this.handleLine(line);
    }
  }

  private handleStderr(chunk: Buffer | string): void {
    const text = Buffer.isBuffer(chunk) ? chunk.toString("utf8") : chunk;
    this.stderrText += text;
    const lines = `${this.stderrRemainder}${text}`.split(/\r?\n/);
    this.stderrRemainder = lines.pop() ?? "";
    for (const line of lines) {
      this.pending?.records.push(`stderr:${line}`);
    }
  }

  private handleLine(line: string): void {
    this.pending?.records.push(line);
    if (line.startsWith("*stopped,")) {
      if (this.pendingStop !== null) {
        clearTimeout(this.pendingStop.timeout);
        const pendingStop = this.pendingStop;
        this.pendingStop = null;
        pendingStop.resolve({ line, reason: miField(line, "reason") ?? "unknown", timedOut: false });
      } else {
        this.lastStopLine = line;
      }
      return;
    }

    const resultMatch = /^(\d+)\^(done|running|connected|error|exit)(?:,.*)?$/.exec(line);
    if (resultMatch === null) {
      return;
    }
    const token = Number.parseInt(resultMatch[1], 10);
    if (this.pending?.token !== token) {
      return;
    }
    const pending = this.pending;
    this.pending = null;
    clearTimeout(pending.timeout);
    const result = {
      resultClass: resultMatch[2],
      line,
      records: pending.records,
      timedOut: false,
      errorMessage: resultMatch[2] === "error" ? miField(line, "msg") ?? "GDB/MI command failed." : undefined,
    };
    this.commandHistory.push({
      token,
      command: pending.command,
      result_class: result.resultClass,
      error: result.errorMessage,
    });
    pending.resolve(result);
  }

  private handleExit(code: number | null, signal: NodeJS.Signals | null): void {
    this.exited = true;
    if (this.pending !== null) {
      clearTimeout(this.pending.timeout);
      const pending = this.pending;
      this.pending = null;
      pending.resolve({
        resultClass: "error",
        line: null,
        records: pending.records,
        timedOut: false,
        errorMessage: `GDB exited before command completed (${code ?? signal ?? "unknown"}).`,
      });
    }
    if (this.pendingStop !== null) {
      clearTimeout(this.pendingStop.timeout);
      const pendingStop = this.pendingStop;
      this.pendingStop = null;
      pendingStop.resolve({
        line: null,
        reason: "debugger_error",
        timedOut: false,
        errorMessage: `GDB exited before target stopped (${code ?? signal ?? "unknown"}).`,
      });
    }
  }

  private handleProcessError(error: Error): void {
    this.stderrText += `${error.message}\n`;
  }
}

export function miString(value: string): string {
  return `"${value.replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/\n/g, "\\n").replace(/\r/g, "\\r")}"`;
}

export function miField(line: string | null, name: string): string | null {
  if (line === null) {
    return null;
  }
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`(?:^|[,{}])${escaped}="((?:\\\\.|[^"\\\\])*)"`).exec(line);
  return match === null ? null : unescapeMiString(match[1]);
}

export function parseGdbInteger(value: string): number | null {
  const trimmed = value.trim();
  try {
    if (/^0x[0-9a-fA-F]+$/.test(trimmed)) {
      return Number.parseInt(trimmed, 16);
    }
    if (/^[0-9]+$/.test(trimmed)) {
      return Number.parseInt(trimmed, 10);
    }
  } catch {
    return null;
  }
  return null;
}

export function writeIntelHexFile(filePath: string, startAddress: number, data: Buffer): void {
  const records: string[] = [];
  let currentUpper = -1;
  for (let offset = 0; offset < data.length; offset += 16) {
    const absolute = startAddress + offset;
    const upper = Math.floor(absolute / 0x10000) & 0xffff;
    if (upper !== currentUpper) {
      currentUpper = upper;
      records.push(intelHexRecord(0, 0x04, Buffer.from([(upper >> 8) & 0xff, upper & 0xff])));
    }
    records.push(intelHexRecord(absolute & 0xffff, 0x00, data.subarray(offset, offset + 16)));
  }
  records.push(":00000001FF");
  writeFileSync(filePath, `${records.join("\n")}\n`, "ascii");
}

function intelHexRecord(address: number, recordType: number, data: Buffer): string {
  const bytes = [data.length, (address >> 8) & 0xff, address & 0xff, recordType, ...data];
  const checksum = ((~bytes.reduce((sum, byte) => sum + byte, 0) + 1) & 0xff) >>> 0;
  return `:${bytes.map(hexByte).join("")}${hexByte(checksum)}`;
}

function hexByte(value: number): string {
  return (value & 0xff).toString(16).padStart(2, "0").toUpperCase();
}

function unescapeMiString(value: string): string {
  return value.replace(/\\([\\"nrt])/g, (_match, escaped: string) => {
    if (escaped === "n") {
      return "\n";
    }
    if (escaped === "r") {
      return "\r";
    }
    if (escaped === "t") {
      return "\t";
    }
    return escaped;
  });
}

function waitForExit(child: ChildProcessWithoutNullStreams, timeoutMs: number): Promise<void> {
  return new Promise((resolve) => {
    const timeout = setTimeout(resolve, timeoutMs);
    child.once("exit", () => {
      clearTimeout(timeout);
      resolve();
    });
  });
}
