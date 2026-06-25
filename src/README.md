# Source

This directory will contain the implementation of the AI-HIL host-side bridge.

For now, no implementation language is fixed.

The source code should eventually provide:

- an MCP server
- safe MCP tool handlers
- configuration loading
- policy checks
- OpenOCD process execution
- configured COM port streaming sessions
- separate plain text COM stdio bridge
- structured reports
- raw log storage

Keep this directory focused on the actual AI-HIL bridge. Development helpers, generated files, examples, and documentation should only be added when they are really needed.
