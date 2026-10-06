# Tool Definition Score

Every pull request that changes the MCP tool definitions is scored before it
merges. The gate exports the `tools/list` of the base and of the head, scores
both with the Tool Definition Quality Score (TDQS) v1.3 using one pinned model,
and fails when the mean TDQS of the tools falls. A pull request that leaves the
tool definitions unchanged passes without a single model call.

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
- **The model.** `claude-opus-5-5`, asked through the Claude Code
  command line 2.1.288, which `tools/tdqs/package-lock.json` pins by version
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

The mean TDQS over all tools decides, compared as its rollup: the exact mean
rounded half up to one decimal once. The overall score, description quality,
coherence and the lowest TDQS are reported next to it as information. They do
not decide because they are not steady enough on identical text: the overall
weighs the single lowest tool at 28 % and the judged coherence at 30 %. Over
three runs of the same definitions the lowest TDQS moved from 1.7 to 2.1 and
the overall from 2.8 to 3.0; over three runs of another set, coherence moved
from 3.3 to 3.5. The mean over every tool moved by less than 0.05 unrounded in
both, and it still falls when descriptions get worse.

| Result | Exit code | When |
|---|---|---|
| PASS | 0 | The definitions are unchanged; or the first pair holds (head mean TDQS at least base mean TDQS); or the first pair drops but the median of each side over three pairs does not |
| BLOCK | 1 | The first pair drops and two more pairs confirm it: the head's median mean TDQS over the three pairs is below the base's |
| INVALID | 2 | The definitions changed and could not be scored completely: no token, a prompt that fails its hash, an answer still unreadable after its retries, a different model answering, or a saved report that no longer matches the exports |

One scoring run of the same definitions can still differ from the next by 0.1
on the mean TDQS once it is rounded (the three calibration runs below gave 3.2,
3.2 and 3.3, from 3.207 to 3.255 unrounded, against 2.8, 3.0 and 2.9 on the
overall score), so a drop on one pair alone never blocks.

The size of the definitions is reported next to the scores and is never part
of the decision: description characters, `tools/list` bytes and the bytes of
each tool's definition, for base, head and the change between them.

The report is written to `.tool-definition-score/tool-definition-score.json`,
with a Markdown summary beside it in `tool-definition-score.md`; in CI the
summary is also the job summary. When the mean TDQS drops on the first pair,
the report names the tools that fell, the dimensions that fell with the model's
justification for each, and what each tool's fall alone costs the unrounded
mean TDQS; the coherence dimensions that fell and a fallen minimum are listed as
information.

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
warning that the scores were calibrated with 2.1.288. On Windows the gate
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

One pair of the 44 tools takes about two and a half minutes and 90 model calls
at the default concurrency; a confirmed drop takes three pairs.

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
| Overall | 3.1 (B) | 2.8, 3.0, 2.9 | 2.9 (C) |
| Description quality | 2.8 (C) | 2.6, 2.8, 2.7 | 2.7 (C) |
| Coherence | 3.8 (A) | 3.3, 3.3, 3.3 | 3.3 (B) |
| Mean TDQS | 3.6 | 3.2, 3.2, 3.3 | 3.2 |
| Lowest TDQS | 1.7 `debug_symbol_info` | 1.7, 2.1, 1.8 `debug_symbol_info` | 1.8 |

Scored once on `5762f9b`'s own `tools/list`, with the titles, the gate gives
2.9 (C) overall, 2.7 description quality, 3.3 coherence, 3.2 mean TDQS and
2.0 lowest (`debug_symbol_info`).

### Per dimension

The mean absolute difference between the gate's median and the registry's
score, over the 44 tools:

| Measure | Mean absolute difference | Mean signed difference | Gate higher | Gate lower | Equal |
|---|---|---|---|---|---|
| TDQS | 0.40 | -0.35 | 4 | 37 | 3 |
| Purpose clarity | 0.48 | -0.30 | 4 | 17 | 23 |
| Usage guidelines | 0.23 | -0.18 | 1 | 9 | 34 |
| Behavioral transparency | 0.59 | -0.55 | 1 | 24 | 19 |
| Parameter semantics | 0.25 | -0.16 | 2 | 9 | 33 |
| Conciseness and structure | 0.50 | -0.41 | 2 | 19 | 23 |
| Contextual completeness | 0.68 | -0.68 | 0 | 30 | 14 |

### The largest differences

| Tool | Registry | Gate | Difference | Dimensions that differ (gate minus registry) |
|---|---|---|---|---|
| `bench_run_start` | 4.4 | 3.6 | -0.8 | purpose clarity -1, behavioral transparency -1, parameter semantics -1, conciseness and structure -1, contextual completeness -1 |
| `debug_start_session` | 2.9 | 2.1 | -0.8 | purpose clarity -1, behavioral transparency -1, parameter semantics -1, conciseness and structure -1, contextual completeness -1 |
| `flash_firmware` | 4.5 | 3.7 | -0.8 | purpose clarity -1, usage guidelines -1, behavioral transparency -1, parameter semantics -1, conciseness and structure +1, contextual completeness -1 |
| `test_reactor_run` | 4.3 | 3.5 | -0.8 | purpose clarity -1, behavioral transparency -2, contextual completeness -1 |
| `project_config_set` | 4.0 | 3.3 | -0.7 | usage guidelines -1, behavioral transparency -1, parameter semantics -1, conciseness and structure -1, contextual completeness -1 |
| `artifact_upload` | 3.3 | 2.7 | -0.6 | behavioral transparency -1, parameter semantics -1, conciseness and structure -1, contextual completeness -1 |
| `classify_last_error` | 3.4 | 2.8 | -0.6 | purpose clarity -1, behavioral transparency -1, conciseness and structure -1, contextual completeness -1 |
| `debug_continue` | 3.3 | 2.7 | -0.6 | usage guidelines -1, behavioral transparency -1, conciseness and structure -1, contextual completeness -1 |
| `debug_stop_session` | 2.8 | 2.2 | -0.6 | purpose clarity -1, parameter semantics -1, conciseness and structure -1, contextual completeness -1 |
| `get_last_report` | 3.4 | 2.8 | -0.6 | purpose clarity -1, behavioral transparency -1, conciseness and structure -1, contextual completeness -1 |

### Likely causes

- **A stricter judge, by a constant offset.** The gate scores with
  `claude-opus-5-5`; the registry does not say which model it scores with,
  and its justifications read differently. The gate is lower on 37 tools and
  higher on 4, by 0.35 TDQS on average, but it orders the tools much as the
  registry does: per tool, the gate's median and the registry's score
  correlate at 0.89, and for 32 of the 44 tools the difference lies within 0.3
  of that 0.35 offset. A judge that disagreed about the tools would scatter;
  this one sits one step lower on nearly all of them. The offset comes mostly
  from contextual completeness (lower on 30 tools, higher on none), behavioral
  transparency (lower on 24, higher on 1) and conciseness and structure, while
  usage guidelines and parameter semantics match the registry on 34 and 33
  tools.
- **Dense descriptions.** The largest differences, 0.6 to 0.8, fall on
  descriptions that carry several rules in one or two sentences or point to a
  resource for details: `bench_run_start`, `flash_firmware`,
  `test_reactor_run`, `project_config_set`. Each is one or two points lower on
  three to five dimensions rather than far lower on one, and below the registry in
  every run. No tool differs by more than 0.8, and the four the gate rates
  higher differ by at most 0.4.
- **Run-to-run variation.** The gate's own three runs differ from each other by
  0.10 TDQS per tool on average and by up to 0.6, and gave the same coherence
  in all three; the median of three narrows that further, but the registry's
  single published score carries its own variation, which one score cannot
  show.
- **The request around the prompts.** The command line puts a billing line and
  one sentence naming the SDK ahead of the system prompt, and a reminder with
  the date ahead of the prompt itself, and samples at its default temperature.
  None of that can be switched off, and its effect on the absolute level is not
  known. It is the same for both sides of a comparison.
- **Not the definitions.** The calibration scored the registry's own
  definitions, so the missing `annotations.title` is not a cause here.

Across the whole set the offset shows in every rollup: the mean TDQS is 3.2
against the registry's 3.6, the overall 2.9 against 3.1, and coherence 3.3
against 3.8. The lowest tool is the same, `debug_symbol_info`, at 1.8 against
1.7.

The gate compares a head with its own base under the same model, so an offset
the pinned model holds against the registry applies to both sides and leaves
the decision alone. What it cannot see is a change the two models would judge
in opposite directions; the close per-tool agreement above makes that rare.

### Every tool

| Tool | Registry | Gate (median) | Difference | Gate runs |
|---|---|---|---|---|
| `artifact_upload` | 3.3 | 2.7 | -0.6 | 2.7, 2.7, 2.7 |
| `bench_run_start` | 4.4 | 3.6 | -0.8 | 3.6, 3.6, 3.8 |
| `bench_run_status` | 3.9 | 3.8 | -0.1 | 3.8, 3.7, 3.8 |
| `bench_run_stop` | 4.2 | 4.1 | -0.1 | 4.1, 4.1, 4.1 |
| `can_buses_list` | 4.4 | 4.2 | -0.2 | 4.2, 4.2, 4.1 |
| `can_read` | 3.5 | 3.8 | +0.3 | 3.8, 3.8, 3.9 |
| `can_send` | 3.2 | 3.2 | 0.0 | 2.9, 3.4, 3.2 |
| `can_session_start` | 2.9 | 2.6 | -0.3 | 2.6, 2.5, 2.7 |
| `can_session_stop` | 2.7 | 2.4 | -0.3 | 2.4, 2.4, 2.4 |
| `classify_last_error` | 3.4 | 2.8 | -0.6 | 2.8, 2.7, 2.8 |
| `com_ports_list` | 4.4 | 4.1 | -0.3 | 4.1, 4.2, 4.1 |
| `com_read` | 3.8 | 4.1 | +0.3 | 4.1, 3.5, 4.1 |
| `com_session_start` | 2.9 | 2.7 | -0.2 | 2.7, 2.7, 2.7 |
| `com_session_stop` | 2.6 | 2.4 | -0.2 | 2.2, 2.4, 2.6 |
| `com_write` | 2.9 | 2.9 | 0.0 | 2.9, 2.9, 2.9 |
| `debug_clear_breakpoints` | 3.8 | 3.4 | -0.4 | 3.4, 3.4, 3.4 |
| `debug_continue` | 3.3 | 2.7 | -0.6 | 2.7, 2.9, 2.7 |
| `debug_dump_symbol_ihex` | 3.0 | 2.7 | -0.3 | 2.7, 2.7, 2.7 |
| `debug_get_session_status` | 3.2 | 2.7 | -0.5 | 2.7, 2.7, 2.7 |
| `debug_get_stop_reason` | 3.1 | 2.7 | -0.4 | 2.7, 2.7, 2.7 |
| `debug_halt` | 3.0 | 2.5 | -0.5 | 2.6, 2.5, 2.5 |
| `debug_list_breakpoints` | 3.7 | 3.3 | -0.4 | 3.5, 3.1, 3.3 |
| `debug_set_breakpoint` | 3.4 | 2.9 | -0.5 | 2.9, 2.9, 2.9 |
| `debug_start_session` | 2.9 | 2.1 | -0.8 | 2.1, 2.2, 2.1 |
| `debug_stop_session` | 2.8 | 2.2 | -0.6 | 2.2, 2.2, 2.2 |
| `debug_symbol_info` | 1.7 | 1.8 | +0.1 | 1.7, 2.1, 1.8 |
| `debug_symbol_value` | 3.8 | 3.3 | -0.5 | 3.3, 3.3, 3.1 |
| `debugger_info` | 3.9 | 3.8 | -0.1 | 3.8, 3.8, 3.8 |
| `debugger_probes_list` | 4.4 | 4.2 | -0.2 | 4.1, 4.2, 4.2 |
| `flash_firmware` | 4.5 | 3.7 | -0.8 | 3.7, 3.7, 3.9 |
| `get_last_report` | 3.4 | 2.8 | -0.6 | 2.8, 2.8, 2.8 |
| `hardware_lease_status` | 3.9 | 3.8 | -0.1 | 3.9, 3.8, 3.8 |
| `hardware_recover` | 3.3 | 3.7 | +0.4 | 3.9, 3.7, 3.6 |
| `probe_target` | 3.4 | 3.0 | -0.4 | 3.0, 3.0, 3.0 |
| `project_config_adopt_hardware` | 4.1 | 3.7 | -0.4 | 3.7, 3.7, 3.7 |
| `project_config_create` | 4.3 | 3.7 | -0.6 | 3.7, 3.3, 3.7 |
| `project_config_describe` | 4.6 | 4.2 | -0.4 | 4.2, 4.2, 4.4 |
| `project_config_reload_description` | 4.4 | 3.8 | -0.6 | 3.8, 3.8, 3.8 |
| `project_config_set` | 4.0 | 3.3 | -0.7 | 3.3, 3.2, 3.6 |
| `reset_target` | 3.6 | 3.2 | -0.4 | 3.7, 3.1, 3.2 |
| `server_upgrade` | 4.1 | 4.1 | 0.0 | 4.2, 4.1, 4.1 |
| `test_reactor_run` | 4.3 | 3.5 | -0.8 | 3.5, 3.5, 3.7 |
| `test_reactor_status` | 3.4 | 2.8 | -0.6 | 2.8, 2.8, 3.0 |
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
