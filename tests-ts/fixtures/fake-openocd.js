import net from "node:net";

const args = process.argv.slice(2);

if (args.includes("--version")) {
  console.log("Open On-Chip Debugger 0.12.0 fake");
  process.exit(0);
}

const commandIndex = args.lastIndexOf("-c");
const command = commandIndex >= 0 ? args[commandIndex + 1] ?? "" : "";

const gdbPortCommand = args.find((arg, index) => args[index - 1] === "-c" && /^gdb_port \d+$/.test(arg));
if (gdbPortCommand !== undefined) {
  const port = Number.parseInt(gdbPortCommand.split(/\s+/)[1], 10);
  const server = net.createServer((socket) => socket.end());
  server.listen(port, "127.0.0.1", () => {
    console.log(`Listening on port ${port} for gdb connections`);
  });
  const stop = () => server.close(() => process.exit(0));
  process.on("SIGTERM", stop);
  process.on("SIGINT", stop);
  await new Promise(() => {});
}

if (command.includes("AIHIL_RESULT:probe_target:ok")) {
  console.log("AIHIL_RESULT:probe_target:ok");
  process.exit(0);
}

if (command.includes("AIHIL_RESULT:flash_firmware:ok")) {
  console.log("AIHIL_RESULT:flash_firmware:ok");
  process.exit(0);
}

if (command.includes("AIHIL_RESULT:reset_target:ok")) {
  console.log("AIHIL_RESULT:reset_target:ok");
  process.exit(0);
}

console.error("fake OpenOCD command was not recognized");
process.exit(1);
