# Pull Request

## Summary

Describe the user-facing change and why it is needed.

## User-visible Change

Describe CLI, MCP tool, config, report, docs, packaging, or workflow changes a user will notice.

## Validation

- [ ] `npm test` passes locally
- [ ] `npm run pack:check` was inspected if packaging changed
- [ ] Docs were updated if behavior changed
- [ ] Tests were added or updated for behavior changes
- [ ] Demo screenshots/GIFs were updated if README onboarding or demo behavior changed

## Hardware And Safety

- [ ] No raw debugger/OpenOCD command surface was added
- [ ] No mass erase default behavior was added
- [ ] Artifact root validation is preserved
- [ ] COM access remains limited to configured `port_id` values
- [ ] `permission_denied` behavior remains authoritative
- [ ] Report/error fields remain structured for agents

## Platform Impact

- [ ] Windows paths and COM-port examples still make sense if setup behavior changed
- [ ] Linux/macOS setup behavior is unchanged or documented

## Hardware Validation

If tested on real hardware, include board, probe, backend, artifact path, and sanitized report/log paths.

If not tested on real hardware, state that clearly.

## Breaking Changes

List any CLI, config, MCP tool, report schema, or workflow compatibility changes.
