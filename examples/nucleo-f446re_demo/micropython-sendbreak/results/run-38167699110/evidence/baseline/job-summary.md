## Agentic HIL: hilval-hq176-baseline

**Outcome:** success

Plan: `hilval/hq176/plans/baseline.testconfig.yaml` (sha256 `bce744d0f613bc919f7c6337eeefc344e96a9338c152edf59bb37412f6382526`)

| # | Route | Action | Result | Elapsed (ms) |
| --- | --- | --- | --- | --- |
| 1 | dut | flash | pass | 10659 |
| 2 | dut_uart | uart_open | pass | 106 |
| 3 | dut | reset | pass | 394 |
| 4 | dut_uart | uart_read | pass | 29106 |
| 5 | dut_uart | uart_close | pass | 87 |

### Bench

| Field | Value |
| --- | --- |
| Configuration digest | `sha256:9e0b4615276e15b64107b9a0e09044c5c202265bbe5e337424c9b915ae67652c` |
| Diverged from the file on disk | no |
| Debuggers | `dut` |
| COM ports | `dut_uart` |
| Runner | [withheld] |
| Repository | `agentic-hil/agentic-hil` |
| Ref | `refs/heads/bench/hilval-hq176` |
| Commit | `ebd3eeaf97903f68d25528e8c90ff2f9c6678646` |
| Agentic HIL | `0.21.4` |
| Python | `3.12.3` |
