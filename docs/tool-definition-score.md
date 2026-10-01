# Tool Definition Score

Every pull request that changes the MCP tool definitions is scored before it
merges. The gate exports the `tools/list` of the base and of the head, scores
both with the Tool Definition Quality Score (TDQS) v1.3 using one pinned model,
and fails when the overall score falls. A pull request that leaves the tool
definitions unchanged passes without a single model call.

The scores are the same kind a public MCP registry publishes for this server.
How close the gate's scores come to the registry's is measured below, in
[Calibration against the registry](#calibration-against-the-registry).

## What is scored

- **The definitions a host sees.** Each side is exported by asking a
  provisioned server for `tools/list` over stdio: name, title, description,
  input schema, output schema and annotations of every tool. The base is
  exported from a detached worktree of the base revision, with the
  interpreter and dependencies of the environment running the gate; only the
  package itself comes from the base. Both exports are hashed, and two equal
  hashes mean nothing to score.
- **The specification.** TDQS v1.3 at commit
  `b9881b0cfec88969e42672c92544487ca191a992` of the specification
  repository. Its four prompts are fetched from that commit, checked against
  the SHA-256 hashes in `tools/tdqs/version.json`, and cached outside the
  repository, in `agentic-hil-tdqs` under `XDG_CACHE_HOME`, `LOCALAPPDATA` or
  `~/.cache`. The prompt texts are never stored in this repository, because the
  specification repository grants no license to copy them. A prompt that no
  longer matches its hash stops the gate.
- **The model.** `claude-haiku-4-5-20251001`, asked through the Claude Code
  command line 2.1.258, which `tools/tdqs/package-lock.json` pins by version
  and integrity. Each tool is one call, the coherence of the whole set is one
  more, and an answer that cannot be read is asked again up to two times. The
  calls run with no tools, no MCP servers, no settings, no project memory and
  no saved session, each in a fresh empty directory. An answer from any other
  model is refused.
- **The arithmetic.** Each tool's TDQS weighs purpose clarity 25, usage
  guidelines 20, behavioral transparency 20, parameter semantics 15, conciseness
  and structure 10, and contextual completeness 10. Description quality is
  60 % of the mean TDQS plus 40 % of the lowest one, so a single weak tool
  costs the whole set. The overall score is 70 % description quality plus
  30 % coherence. Every rollup is computed on exact fractions and rounded half
  up to one decimal once. Tiers start at 3.5 (A), 3.0 (B), 2.0 (C) and 1.0 (D).

## The decision

| Result | Exit code | When |
|---|---|---|
| PASS | 0 | The definitions are unchanged; or the first pair holds (head overall at least base overall); or the first pair drops but the median of each side over three pairs does not |
| BLOCK | 1 | The first pair drops and two more pairs confirm it: the head's median overall over the three pairs is below the base's |
| INVALID | 2 | The definitions changed and could not be scored completely: no token, a prompt that fails its hash, an answer still unreadable after its retries, a different model answering, or a saved report that no longer matches the exports |

One scoring run of the same definitions can differ from the next by 0.1 or
0.2 on the overall score (the three calibration runs below gave 2.9, 3.1 and
3.0), so a drop on one pair alone never blocks.

The size of the definitions is reported next to the scores and is never part
of the decision: description characters, `tools/list` bytes and the bytes of
each tool's definition, for base, head and the change between them.

The report is written to `.tool-definition-score/tool-definition-score.json`,
with a Markdown summary beside it in `tool-definition-score.md`; in CI the
summary is also the job summary. When the overall score drops, the report names
the tools that fell, the dimensions that fell with the model's justification
for each, and what each tool's fall alone costs the overall score.

## Running it locally

The gate needs the development install (`python -m pip install -e '.[dev,can]'`)
and the `claude` command on `PATH`, logged in. The pinned command line is one
`npm ci` away:

```bash
cd tools/tdqs && npm ci && cd ../..
export PATH="$PWD/tools/tdqs/node_modules/.bin:$PATH"
python tools/tool_definition_score.py --base origin/master
```

Another installed version of Claude Code also works; the report then carries a
warning that the scores were calibrated with 2.1.258. On Windows the gate
refuses a `claude.cmd` batch wrapper, which cannot pass the system prompt
intact, and needs the native `claude` executable.

| Option | Meaning |
|---|---|
| `--base REV` | the revision to compare against, for example `origin/master` |
| `--head REV` | a head revision; without it the working tree is the head |
| `--report-dir DIR` | where the report goes (default `.tool-definition-score`) |
| `--token-env NAME` | the variable holding a Claude Code OAuth token; without it the local login is used |
| `--concurrency N` | model calls at once (default 6) |
| `--cache-dir DIR` | where the fetched specification is cached |
| `--check REPORT` | check a saved report against the current exports instead of scoring |
| `--calibrate CAPTURE` with `--runs N` | score a capture of the registry's published definitions N times and write `calibration.json` |

One pair of the 44 tools takes about three minutes and 90 model calls at the
default concurrency; a confirmed drop takes three pairs.

The end-to-end test of the gate against the real model is left out of every
test run unless `AGENTIC_HIL_TDQS_MODEL` is exactly `1`:

```bash
AGENTIC_HIL_TDQS_MODEL=1 python -m pytest tests/test_tool_definition_score_model.py
```

## In CI

The `tool_definition_score` job in `.github/workflows/ci.yml` runs on pull
requests only, since only a pull request has a base to compare. It installs the
pinned command line with `npm ci`, scores the merge of the pull request against
`origin/<base branch>`, runs the end-to-end test when the model was called, and
uploads the report as the `tool-definition-score` artifact whatever happened.
Required CI needs the job, and accepts it skipped only on a push.

The model is reached with the repository secret `CLAUDE_CODE_OAUTH_TOKEN`, a
token printed by `claude setup-token` for the account that pays for the calls.
Only the scoring step and the end-to-end step receive it. A pull request from a
fork receives no secrets: with its tool definitions unchanged it passes as
usual, with them changed it is INVALID, because nothing could score it.

`tools/tdqs/version.json` records everything a score depends on: the model, the
command line version, the specification commit, the prompt hashes, the
weights, the tier thresholds, the retries and the confirmation rule. Changing
any of it changes the record's digest, and the calibration record below carries
the digest it was made with, so a test fails until the calibration is made
again.

## Calibration against the registry

The [Glama registry](https://glama.ai/mcp/servers/agentic-hil/agentic-hil)
published TDQS scores for release `0.22.1-dev.0`, scored on 2026-10-01. The
definitions it scored are those of `5762f9b` in every field but one: its copy
carries no `annotations.title` on any of the 44 tools, while this server's
`tools/list` carries one on each. So the calibration scores the registry's own
definitions, and both evaluators judged the same text.

The gate scored those definitions three times; per tool and per dimension, the
median of the three is set against the registry's published score. Nothing is
adjusted: the gate's scores stay what the pinned model gives, and the
differences are recorded here and in `tools/tdqs/calibration.json` with the
version digest, the scored set hash and the definition differences.

### The whole set

| Measure | Registry | Gate runs | Gate median |
|---|---|---|---|
| Overall | 3.1 (B) | 2.9, 3.1, 3.0 | 3.0 (B) |
| Description quality | 2.8 (C) | 2.7, 2.7, 2.7 | 2.7 (C) |
| Coherence | 3.8 (A) | 3.3, 4.0, 3.8 | 3.8 (A) |
| Mean TDQS | 3.6 | 3.4, 3.4, 3.3 | 3.4 |
| Lowest TDQS | 1.7 `debug_symbol_info` | 1.8, 1.6, 1.8 `hardware_recover` | 1.8 |

Scored once on `5762f9b`'s own `tools/list`, with the titles, the gate gives
3.1 (B) overall, 2.7 description quality, 4.0 coherence, 3.3 mean TDQS and
1.8 lowest (`hardware_recover`).

### Per dimension

The mean absolute difference between the gate's median and the registry's
score, over the 44 tools:

| Measure | Mean absolute difference | Mean signed difference | Gate higher | Gate lower | Equal |
|---|---|---|---|---|---|
| TDQS | 0.50 | -0.22 | 16 | 24 | 4 |
| Purpose clarity | 0.50 | -0.36 | 3 | 16 | 25 |
| Usage guidelines | 0.68 | -0.18 | 9 | 14 | 21 |
| Behavioral transparency | 0.84 | +0.07 | 19 | 14 | 11 |
| Parameter semantics | 0.91 | +0.23 | 24 | 10 | 10 |
| Conciseness and structure | 0.70 | -0.39 | 5 | 16 | 23 |
| Contextual completeness | 1.00 | -1.00 | 0 | 35 | 9 |

### The largest differences

| Tool | Registry | Gate | Difference | Dimensions that differ (gate minus registry) |
|---|---|---|---|---|
| `test_reactor_run` | 4.3 | 2.0 | -2.3 | purpose clarity -2, usage guidelines -2, behavioral transparency -2, parameter semantics -3, conciseness and structure -2, contextual completeness -3 |
| `bench_run_start` | 4.4 | 2.5 | -1.9 | purpose clarity -2, usage guidelines -2, behavioral transparency -2, parameter semantics -1, conciseness and structure -3, contextual completeness -2 |
| `hardware_recover` | 3.3 | 1.8 | -1.5 | purpose clarity -1, usage guidelines -1, behavioral transparency -1, parameter semantics -2, conciseness and structure -3, contextual completeness -2 |
| `project_config_set` | 4.0 | 2.8 | -1.2 | usage guidelines -2, behavioral transparency -2, parameter semantics -1, conciseness and structure -1, contextual completeness -2 |
| `test_reactor_status` | 3.4 | 2.2 | -1.2 | purpose clarity -2, usage guidelines -1, parameter semantics -1, conciseness and structure -2, contextual completeness -1 |
| `project_config_create` | 4.3 | 3.5 | -0.8 | purpose clarity -1, usage guidelines -1, behavioral transparency -1, parameter semantics +1, conciseness and structure -1, contextual completeness -2 |
| `can_session_stop` | 2.7 | 3.4 | +0.7 | usage guidelines +1, behavioral transparency +1, parameter semantics +1, conciseness and structure +1 |
| `debug_clear_breakpoints` | 3.8 | 4.5 | +0.7 | usage guidelines +2, behavioral transparency +1, parameter semantics +1 |
| `debug_get_session_status` | 3.2 | 3.9 | +0.7 | usage guidelines +1, behavioral transparency +2, parameter semantics +1, contextual completeness -1 |
| `debug_list_breakpoints` | 3.7 | 4.4 | +0.7 | purpose clarity +1, usage guidelines +1, behavioral transparency +1, parameter semantics +1, contextual completeness -1 |

### Likely causes

- **A different model.** The gate scores with `claude-haiku-4-5-20251001`;
  the registry does not say which model it scores with, and its justifications
  read differently. The part of the difference that goes one way is the
  clearest sign: contextual completeness is lower on 35 tools and higher on
  none, and purpose clarity and conciseness are lower far more often than
  higher. That is a stricter judge on those dimensions, not chance.
- **Dense descriptions.** The largest differences fall on descriptions that
  carry several rules in one or two sentences, lead with what the tool
  replaces, or point to a resource for details: `test_reactor_run`,
  `bench_run_start`, `hardware_recover`, `project_config_set`. The registry
  rated most of them 4.0 to 4.4; the pinned model marks them down on conciseness
  and structure and on contextual completeness, the same way in all three runs.
  Short, single-purpose descriptions such as `can_session_stop` and
  `debug_clear_breakpoints` go the other way.
- **Run-to-run variation.** The gate's own three runs differ from each other by
  0.20 TDQS per tool on average and by up to 0.9; the median of three narrows
  that, but the registry's single published score carries its own variation,
  which one score cannot show.
- **The request around the prompts.** The command line puts a billing line and
  one sentence naming the SDK ahead of the system prompt, and a reminder with
  the date ahead of the prompt itself, and samples at its default temperature.
  None of that can be switched off, and its effect on the absolute level is not
  known. It is the same for both sides of a comparison.
- **Not the definitions.** The calibration scored the registry's own
  definitions, so the missing `annotations.title` is not a cause here.

Across the whole set the per-tool differences largely cancel: the overall
score is 3.0 against the registry's 3.1, and the coherence median matches.

The gate compares a head with its own base under the same model, so an offset
the pinned model holds against the registry applies to both sides and leaves
the decision alone. What it cannot see is a change the two models would judge
in opposite directions.

### Every tool

| Tool | Registry | Gate (median) | Difference | Gate runs |
|---|---|---|---|---|
| `artifact_upload` | 3.3 | 3.3 | 0.0 | 3.3, 2.9, 3.3 |
| `bench_run_start` | 4.4 | 2.5 | -1.9 | 3.0, 2.5, 2.5 |
| `bench_run_status` | 3.9 | 3.3 | -0.6 | 3.1, 3.3, 3.4 |
| `bench_run_stop` | 4.2 | 4.7 | +0.5 | 4.7, 4.6, 4.8 |
| `can_buses_list` | 4.4 | 4.2 | -0.2 | 4.1, 4.2, 4.2 |
| `can_read` | 3.5 | 4.0 | +0.5 | 3.1, 4.0, 4.0 |
| `can_send` | 3.2 | 3.0 | -0.2 | 3.2, 3.0, 2.9 |
| `can_session_start` | 2.9 | 2.9 | 0.0 | 2.9, 2.9, 2.9 |
| `can_session_stop` | 2.7 | 3.4 | +0.7 | 3.4, 3.4, 3.1 |
| `classify_last_error` | 3.4 | 3.1 | -0.3 | 3.1, 3.3, 3.0 |
| `com_ports_list` | 4.4 | 4.6 | +0.2 | 4.7, 4.6, 4.6 |
| `com_read` | 3.8 | 3.5 | -0.3 | 3.9, 3.5, 3.3 |
| `com_session_start` | 2.9 | 2.6 | -0.3 | 2.5, 2.8, 2.6 |
| `com_session_stop` | 2.6 | 3.0 | +0.4 | 2.9, 3.0, 3.0 |
| `com_write` | 2.9 | 2.9 | 0.0 | 2.7, 2.9, 2.9 |
| `debug_clear_breakpoints` | 3.8 | 4.5 | +0.7 | 4.5, 4.5, 4.4 |
| `debug_continue` | 3.3 | 2.9 | -0.4 | 2.7, 2.9, 3.0 |
| `debug_dump_symbol_ihex` | 3.0 | 2.4 | -0.6 | 2.4, 2.2, 2.6 |
| `debug_get_session_status` | 3.2 | 3.9 | +0.7 | 3.7, 3.9, 4.0 |
| `debug_get_stop_reason` | 3.1 | 3.1 | 0.0 | 3.3, 3.1, 2.6 |
| `debug_halt` | 3.0 | 2.7 | -0.3 | 2.9, 2.6, 2.7 |
| `debug_list_breakpoints` | 3.7 | 4.4 | +0.7 | 4.6, 4.4, 4.2 |
| `debug_set_breakpoint` | 3.4 | 2.7 | -0.7 | 2.7, 2.6, 2.7 |
| `debug_start_session` | 2.9 | 2.4 | -0.5 | 2.1, 2.4, 2.4 |
| `debug_stop_session` | 2.8 | 2.9 | +0.1 | 2.9, 3.0, 2.8 |
| `debug_symbol_info` | 1.7 | 2.1 | +0.4 | 2.1, 1.8, 2.1 |
| `debug_symbol_value` | 3.8 | 3.4 | -0.4 | 3.4, 3.3, 3.4 |
| `debugger_info` | 3.9 | 4.2 | +0.3 | 4.1, 4.6, 4.2 |
| `debugger_probes_list` | 4.4 | 4.6 | +0.2 | 4.7, 4.6, 4.6 |
| `flash_firmware` | 4.5 | 4.1 | -0.4 | 4.1, 4.2, 3.9 |
| `get_last_report` | 3.4 | 3.7 | +0.3 | 3.7, 3.7, 3.7 |
| `hardware_lease_status` | 3.9 | 3.2 | -0.7 | 3.1, 3.6, 3.2 |
| `hardware_recover` | 3.3 | 1.8 | -1.5 | 1.8, 1.6, 1.8 |
| `probe_target` | 3.4 | 3.5 | +0.1 | 3.5, 3.9, 3.5 |
| `project_config_adopt_hardware` | 4.1 | 4.0 | -0.1 | 4.0, 3.8, 4.0 |
| `project_config_create` | 4.3 | 3.5 | -0.8 | 3.3, 3.5, 3.9 |
| `project_config_describe` | 4.6 | 4.7 | +0.1 | 4.7, 4.7, 5.0 |
| `project_config_reload_description` | 4.4 | 4.0 | -0.4 | 4.2, 4.0, 3.9 |
| `project_config_set` | 4.0 | 2.8 | -1.2 | 2.8, 2.8, 2.3 |
| `reset_target` | 3.6 | 3.9 | +0.3 | 4.4, 3.7, 3.9 |
| `server_upgrade` | 4.1 | 4.0 | -0.1 | 4.0, 4.0, 3.9 |
| `test_reactor_run` | 4.3 | 2.0 | -2.3 | 2.0, 2.1, 1.9 |
| `test_reactor_status` | 3.4 | 2.2 | -1.2 | 2.5, 2.1, 2.2 |
| `test_reactor_stop` | 3.5 | 2.9 | -0.6 | 2.9, 2.9, 2.9 |

### Making the calibration again

Capture the registry's published scores as a JSON file of the form
`{"release": ..., "server": {...}, "tools": [{"name", "definition", "qualityScore"}]}`
from the registry's page for the server, then:

```bash
python tools/tool_definition_score.py --calibrate registry.json --runs 3 --report-dir calibration-run
cp calibration-run/calibration.json tools/tdqs/calibration.json
```

The record keeps only the registry's numbers, tiers and scoring time, not its
justification texts. It refuses a run whose scored definitions are not the
registry's.
