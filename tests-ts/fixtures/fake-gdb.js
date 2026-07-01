import readline from "node:readline";

let nextBreakpoint = 1;

function write(line) {
  process.stdout.write(`${line}\n`);
}

write('=thread-group-added,id="i1"');
write('(gdb)');

const rl = readline.createInterface({ input: process.stdin });
rl.on("line", (line) => {
  const match = /^(\d+)(.*)$/.exec(line);
  if (match === null) {
    return;
  }
  const token = match[1];
  const command = match[2];

  if (command.startsWith("-gdb-exit")) {
    write(`${token}^exit`);
    process.exit(0);
  }
  if (command.startsWith("-gdb-set") || command.startsWith("-file-exec-and-symbols") || command.startsWith("-target-select")) {
    write(`${token}^done`);
    write("(gdb)");
    return;
  }
  if (command.startsWith("-interpreter-exec") || command.startsWith("-target-download")) {
    write(`${token}^done`);
    write("(gdb)");
    return;
  }
  if (command.startsWith("-break-insert")) {
    const number = nextBreakpoint++;
    write(`${token}^done,bkpt={number="${number}",type="breakpoint",disp="keep",enabled="y"}`);
    write("(gdb)");
    return;
  }
  if (command.startsWith("-break-delete")) {
    write(`${token}^done`);
    write("(gdb)");
    return;
  }
  if (command.startsWith("-exec-continue")) {
    write(`${token}^running`);
    write("*running,thread-id=\"all\"");
    setTimeout(() => {
      write('*stopped,reason="breakpoint-hit",bkptno="1",frame={addr="0x08000100",func="test_done",file="tests.c",line="123"},thread-id="1"');
      write("(gdb)");
    }, 20);
    return;
  }
  if (command.startsWith("-exec-interrupt")) {
    write(`${token}^done`);
    write('*stopped,reason="signal-received",signal-name="SIGINT",frame={addr="0x08000100",func="test_done"},thread-id="1"');
    write("(gdb)");
    return;
  }
  if (command.startsWith("-data-evaluate-expression")) {
    if (command.includes("missing_symbol")) {
      write(`${token}^error,msg="No symbol \\\"missing_symbol\\\" in current context."`);
    } else if (command.includes("&CTC_array")) {
      write(`${token}^done,value="0x200006f0"`);
    } else if (command.includes("sizeof(CTC_array)")) {
      write(`${token}^done,value="408"`);
    } else {
      write(`${token}^done,value="0"`);
    }
    write("(gdb)");
    return;
  }
  if (command.startsWith("-data-read-memory-bytes")) {
    const parts = command.split(/\s+/);
    const address = Number.parseInt(parts[1], 16);
    const count = Number.parseInt(parts[2], 10);
    const data = Buffer.alloc(count);
    for (let index = 0; index < data.length; index += 1) {
      data[index] = (address + index) & 0xff;
    }
    write(`${token}^done,memory=[{begin="0x${address.toString(16)}",offset="0x0",end="0x${(address + count).toString(16)}",contents="${data.toString("hex")}"}]`);
    write("(gdb)");
    return;
  }

  write(`${token}^error,msg="fake gdb command was not recognized"`);
  write("(gdb)");
});
