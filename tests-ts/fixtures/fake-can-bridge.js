#!/usr/bin/env node
import readline from "node:readline";

const lines = readline.createInterface({ input: process.stdin, crlfDelay: Number.POSITIVE_INFINITY });
let opened = false;
const queuedFrames = [];

function write(value) {
  process.stdout.write(`${JSON.stringify(value)}\n`);
}

function reply(request, value) {
  write({ reply_id: request.request_id, ...value });
}

for await (const line of lines) {
  if (!line.trim()) {
    continue;
  }
  const request = JSON.parse(line);
  if (request.op === "open") {
    opened = true;
    reply(request, { ok: true, backend: "fake-can", channel: request.channel, bitrate: request.bitrate });
    continue;
  }
  if (request.op === "send") {
    if (!opened) {
      reply(request, { ok: false, error_type: "session_not_active", summary: "fake CAN bridge is closed" });
      continue;
    }
    queuedFrames.push({ ...request.frame, timestamp_us: queuedFrames.length + 1 });
    reply(request, { ok: true, backend: "fake-can" });
    continue;
  }
  if (request.op === "read") {
    if (!opened) {
      reply(request, { ok: false, error_type: "session_not_active", summary: "fake CAN bridge is closed" });
      continue;
    }
    const maxFrames = Math.max(1, Number(request.max_frames ?? 1));
    reply(request, { ok: true, backend: "fake-can", frames: queuedFrames.splice(0, maxFrames) });
    continue;
  }
  if (request.op === "close") {
    opened = false;
    reply(request, { ok: true, backend: "fake-can" });
    break;
  }
  reply(request, { ok: false, error_type: "invalid_argument", summary: `unknown op: ${request.op}` });
}
