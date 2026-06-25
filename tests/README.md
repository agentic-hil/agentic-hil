# Tests

This directory will contain tests for the AI-HIL bridge.

Default tests must not require real hardware.

The normal test suite should validate:

- configuration loading
- permission checks
- OpenOCD command construction
- OpenOCD error classification
- configured COM port session read/write behavior
- plain text COM stdio bridge behavior
- structured report generation
- MCP tool result shapes

Real hardware tests should be opt-in and added only when the first real board setup exists.
