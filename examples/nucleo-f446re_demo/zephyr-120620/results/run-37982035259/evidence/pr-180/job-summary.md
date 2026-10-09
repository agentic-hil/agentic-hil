## Agentic HIL: zephyr-120620-pr-180

**Outcome:** success

Plan: `zephyr-120620/plans/pr-180.testconfig.yaml` (sha256 `6b84ce14fdb2a2a32ef969c31059da0ee0217b42b2f1e36ac8bb5d1375d0a6d0`)

| # | Route | Action | Result | Elapsed (ms) |
| --- | --- | --- | --- | --- |
| 1 | dut | flash | pass | 1521 |
| 2 | dut_uart | uart_open | pass | 134 |
| 3 | dut | reset | pass | 340 |
| 4 | dut_uart | uart_read | pass | 15758 |
| 5 | - | repeat | pass | 4004 |
| 6 | - | repeat | pass | 5143 |

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
