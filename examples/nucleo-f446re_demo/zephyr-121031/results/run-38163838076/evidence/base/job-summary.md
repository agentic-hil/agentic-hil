## Agentic HIL: hilval-hq172-base

**Outcome:** success

Plan: `hilval/hq172/plans/base.testconfig.yaml` (sha256 `8e97ad10a510975cf2ed3e73f666f6e6e0a44dd25ff886167c0f6ab454ec6a74`)

| # | Route | Action | Result | Elapsed (ms) |
| --- | --- | --- | --- | --- |
| 1 | dut | flash | pass | 1453 |
| 2 | dut_uart | uart_open | pass | 85 |
| 3 | dut | reset | pass | 326 |
| 4 | dut_uart | uart_read | pass | 35849 |
| 5 | dut_uart | uart_close | pass | 91 |

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
