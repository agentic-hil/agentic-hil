## Agentic HIL: nucleo-f446re-hello-world

**Outcome:** success

Plan: `testconfig.yaml` (sha256 `6cb852c22f84cc585e8f8c18ccef3ebf98b0e381e22848847ec938516c5122c9`)

| # | Route | Action | Result | Elapsed (ms) |
| --- | --- | --- | --- | --- |
| 1 | dut | flash | pass | 978 |
| 2 | dut_uart | uart_open | pass | 61 |
| 3 | dut | reset | pass | 339 |
| 4 | dut_uart | uart_read | pass | 51 |

### Bench

| Field | Value |
| --- | --- |
| Configuration digest | `sha256:9e0b4615276e15b64107b9a0e09044c5c202265bbe5e337424c9b915ae67652c` |
| Diverged from the file on disk | no |
| Debuggers | `dut` |
| COM ports | `dut_uart` |
| Runner | [withheld] |
| Repository | `agentic-hil/agentic-hil` |
| Ref | `refs/heads/bench/hilval-hq172` |
| Commit | `efd4280f6e7bb2c52d1db74d6f9f108313c1225d` |
| Agentic HIL | `0.21.4` |
| Python | `3.12.3` |
