# Hardware executor boundary

Real hardware stays outside the LLM installation container. The installation
evaluation proves an agent CLI and model can install and register Agentic HIL; a
hardware evaluation proves an exact artifact can safely drive real silicon.
Those are different questions and they need different executors.

The VMware harness that used to answer the second question is gone. It also
re-tested installation and setup, which `evals/install` now does reproducibly,
and the hardware half was manual enough that a stale path could sit in it
unnoticed. What it encoded is here instead:

- [`testconfig.openocd.yaml`](testconfig.openocd.yaml) and
  [`testconfig.stlink.yaml`](testconfig.stlink.yaml) — the test plans: flash,
  open the UART, reset-halt a debug session, run to a breakpoint, stop, close.
- [`config.openocd.template.yaml`](config.openocd.template.yaml) and
  [`config.stlink.template.yaml`](config.stlink.template.yaml) — authoritative
  configuration for an STM32 Nucleo-F446RE on an ST-Link, per backend.
- [`mcp_probe.py`](mcp_probe.py) — a minimal MCP client that speaks
  `initialize` and `tools/list` over stdio.

## Running this in a container

The executor is to be a container like the installation one, with the board
passed through. What that needs, and what it costs:

- **Linux host**: `--device=/dev/bus/usb/<bus>/<device>` for the ST-Link, plus
  `/dev/ttyACM*` for the UART. The container needs no extra capabilities for
  either; udev rules on the host decide who may open them.
- **Windows host**: Docker Desktop has no USB passthrough. The device has to be
  attached to WSL2 with `usbipd-win` first, and the container then runs against
  the WSL2 daemon.
- **Serialization**: one board, one run. Hardware access stays
  operator-controlled and serialized through Agentic HIL's own coordination, so
  a hardware executor must not run its jobs in parallel the way the installation
  matrix does.
- **No credentials, no network** for the verifier half, exactly as in
  `evals/install`.

A completed run should be translated into
[`../result.schema.json`](../result.schema.json):

```json
{
  "schema_version": 1,
  "kind": "hil",
  "executor": "docker",
  "id": "run-01",
  "status": "passed",
  "started_at": "2026-01-01T00:00:00Z",
  "finished_at": "2026-01-01T00:05:00Z",
  "duration_seconds": 300,
  "checks": []
}
```

Do not add USB devices to `evals/install`. Its containers have no devices on
purpose, and an installation case must stay runnable on a machine with no board
attached.
