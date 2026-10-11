## Agentic HIL: hilval-hq175-baseline

**Outcome:** success

Plan: `hilval/hq175/plans/baseline.testconfig.yaml` (sha256 `8f80c934011ac56e38fda0362baaf1a5c85b5576067d8c06c6961b95ee0de9f6`)

| # | Route | Action | Result | Elapsed (ms) |
| --- | --- | --- | --- | --- |
| 1 | dut | flash | pass | 1362 |
| 2 | dut_uart | uart_open | pass | 105 |
| 3 | dut | reset | pass | 346 |
| 4 | dut_uart | uart_read | pass | 60057 |
| 5 | dut_uart | uart_close | pass | 134 |

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
