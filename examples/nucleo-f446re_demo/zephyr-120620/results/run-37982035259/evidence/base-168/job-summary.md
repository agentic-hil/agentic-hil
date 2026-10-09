## Agentic HIL: zephyr-120620-base-168

**Outcome:** success

Plan: `zephyr-120620/plans/base-168.testconfig.yaml` (sha256 `d141aaf7236f0d6c847f30bc2e77762278fa58ac9c191b08ca31516d20a8be6b`)

| # | Route | Action | Result | Elapsed (ms) |
| --- | --- | --- | --- | --- |
| 1 | dut | flash | pass | 1491 |
| 2 | dut_uart | uart_open | pass | 57 |
| 3 | dut | reset | pass | 290 |
| 4 | dut_uart | uart_read | pass | 15798 |
| 5 | - | repeat | pass | 3802 |
| 6 | - | repeat | pass | 5452 |

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
