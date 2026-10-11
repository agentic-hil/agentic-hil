## Agentic HIL: hilval-hq172-head

**Outcome:** success

Plan: `hilval/hq172/plans/head.testconfig.yaml` (sha256 `f2e346ebbc68c232387c1a60dd6de840b7c4d7e2b0f9d1d382cf9eef9cb81e0f`)

| # | Route | Action | Result | Elapsed (ms) |
| --- | --- | --- | --- | --- |
| 1 | dut | flash | pass | 1290 |
| 2 | dut_uart | uart_open | pass | 82 |
| 3 | dut | reset | pass | 367 |
| 4 | dut_uart | uart_read | pass | 35801 |
| 5 | dut_uart | uart_close | pass | 89 |

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
