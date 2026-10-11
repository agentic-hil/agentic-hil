## Agentic HIL: hilval-hq175-patched

**Outcome:** success

Plan: `hilval/hq175/plans/patched.testconfig.yaml` (sha256 `7ca37cdda9cd51bdbadc282cbfb75f36d1bb606daf55c48d94e95110f283867d`)

| # | Route | Action | Result | Elapsed (ms) |
| --- | --- | --- | --- | --- |
| 1 | dut | flash | pass | 1621 |
| 2 | dut_uart | uart_open | pass | 124 |
| 3 | dut | reset | pass | 414 |
| 4 | dut_uart | uart_read | pass | 58830 |
| 5 | dut_uart | uart_close | pass | 76 |

### Bench

| Field | Value |
| --- | --- |
| Configuration digest | `sha256:9e0b4615276e15b64107b9a0e09044c5c202265bbe5e337424c9b915ae67652c` |
| Diverged from the file on disk | no |
| Debuggers | `dut` |
| COM ports | `dut_uart` |
| Runner | [withheld] |
| Repository | `agentic-hil/agentic-hil` |
| Ref | `refs/heads/bench/hilval-hq175` |
| Commit | `5f5e0a77573bc79762702270cc728c502d652bf4` |
| Agentic HIL | `0.21.4` |
| Python | `3.12.3` |
