# Hardware executor boundary

Real hardware remains outside the LLM installation container.

Current reference executor is the existing
[`harness/`](../../harness/README.md): Windows host, pristine VMware snapshot,
Ubuntu guest, USB-attached ST-Link/Nucleo, immutable install spec, deterministic
assertions.

This separation is intentional:

- Docker install eval proves an agent CLI/model can install and register
  Agentic HIL.
- Hardware harness proves an exact artifact can safely drive real silicon.
- Hardware access stays operator-controlled and serialized through Agentic HIL.
- USB transport may be native Linux, VMware passthrough, WSL/USB-IP, or a
  self-hosted runner without changing install cases.

Future HIL wrapper should translate one completed harness run into
[`../result.schema.json`](../result.schema.json) with:

```json
{
  "schema_version": 1,
  "kind": "hil",
  "executor": "vmware",
  "id": "run-01",
  "status": "passed",
  "started_at": "2026-01-01T00:00:00Z",
  "finished_at": "2026-01-01T00:05:00Z",
  "duration_seconds": 300,
  "checks": []
}
```

Do not add USB devices to `evals/install`. Add or select a HIL executor here.
