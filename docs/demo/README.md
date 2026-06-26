# AI-HIL Demo Recording Checklist

Record the demo from a real NUCLEO-F446RE + onboard ST-Link session. Do not use mocked OpenOCD, mocked COM output, or edited success JSON as proof assets.

## Target Assets

```text
docs/demo/aihil-nucleo-loop.gif
docs/demo/aihil-nucleo-loop.mp4
docs/demo/thumbnail.png
```

## Before Recording

- Connect the NUCLEO-F446RE over USB/ST-Link.
- Use the supported demo project at `examples/nucleo-f446re_demo`.
- Run `aihil init` if `.aihil/config.yaml` is missing.
- Set a Windows OpenOCD executable path only if OpenOCD is not on `PATH`.
- Add a `com_ports` entry only if serial feedback is part of the recording.
- Run `aihil doctor` and confirm `ok: true`.

## Shot List

1. Show the connected NUCLEO-F446RE briefly.
2. Show `aihil doctor` returning `ok: true`.
3. Show `aihil mcp-config > .mcp.json` or the generated MCP config.
4. Show the agent prompt:

```text
Use AI-HIL to probe the target, flash build/Debug/nucleo-f446re_demo.elf, reset it in run mode, read the last report, and read the configured COM port if one is available.
```

5. Show `aihil_probe_target` returning `ok: true` and `target_detected: true`.
6. Show `aihil_flash_firmware` returning `ok: true`, `verify: true`, and `reset_after_flash: true`.
7. Show `aihil_get_last_report` with the report path and artifact path.
8. Optionally show `aihil_com_read` using the configured `port_id`.
9. End on a short success frame: `probe -> flash -> reset -> report -> optional COM read`.

## README Embed

After recording, place the GIF at `docs/demo/aihil-nucleo-loop.gif` and add this under the README value proposition:

```md
![AI-HIL Nucleo loop demo](docs/demo/aihil-nucleo-loop.gif)

<sub>Probe -> flash -> reset -> read structured report -> optional COM read on a NUCLEO-F446RE.</sub>
```
