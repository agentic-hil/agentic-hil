# Source

This directory contains the TypeScript implementation of the AI-HIL host-side bridge.

The source code should eventually provide:

- MCP stdio server handling
- safe MCP tool handlers
- configuration loading and schema validation
- policy checks
- OpenOCD process execution
- configured COM port streaming sessions
- separate plain text COM stdio bridge
- structured reports
- raw log storage

Keep this directory focused on the actual AI-HIL bridge. Development helpers, generated files, examples, and documentation should only be added when they are really needed.
