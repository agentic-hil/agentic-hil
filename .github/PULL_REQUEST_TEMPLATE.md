# Pull Request

## Summary

Describe the user-facing change and why it is needed.

## Validation

- [ ] `npm test` passes locally
- [ ] `npm pack --dry-run` was inspected if packaging changed
- [ ] Docs were updated if behavior changed
- [ ] Tests were added or updated for behavior changes

## Hardware And Safety

- [ ] No raw debugger/OpenOCD command surface was added
- [ ] No mass erase default behavior was added
- [ ] Artifact root validation is preserved
- [ ] COM access remains limited to configured `port_id` values
- [ ] `permission_denied` behavior remains authoritative
- [ ] Report/error fields remain structured for agents

## Hardware Validation

If tested on real hardware, include board, probe, backend, artifact path, and sanitized report/log paths.

If not tested on real hardware, state that clearly.

## Breaking Changes

List any CLI, config, MCP tool, report schema, or workflow compatibility changes.
