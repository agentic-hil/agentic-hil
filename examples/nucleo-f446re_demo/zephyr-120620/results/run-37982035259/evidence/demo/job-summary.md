## Agentic HIL: nucleo-f446re-hello-world

**Outcome:** success

Plan: `testconfig.yaml` (sha256 `6cb852c22f84cc585e8f8c18ccef3ebf98b0e381e22848847ec938516c5122c9`)

| # | Route | Action | Result | Elapsed (ms) |
| --- | --- | --- | --- | --- |
| 1 | dut | flash | pass | 989 |
| 2 | dut_uart | uart_open | pass | 140 |
| 3 | dut | reset | pass | 339 |
| 4 | dut_uart | uart_read | pass | 55 |

### Bench

| Field | Value |
| --- | --- |
| Configuration digest | `sha256:9e0b4615276e15b64107b9a0e09044c5c202265bbe5e337424c9b915ae67652c` |
| Diverged from the file on disk | no |
| Debuggers | `dut` |
| COM ports | `dut_uart` |
| Runner | [withheld] |
| Repository | `agentic-hil/agentic-hil` |
| Ref | `refs/heads/bench/zephyr-120620-f446re` |
| Commit | `577b338f3adfb0335f4490d44d0eed2b06c03d63` |
| Agentic HIL | `0.21.4` |
| Python | `3.12.3` |
