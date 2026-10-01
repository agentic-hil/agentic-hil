"""The tool-definition score against the real model, end to end, once.

Everything else about the gate is tested without a model in
test_tool_definition_score.py. This test scores this checkout's own tools/list
with the pinned model through the Claude Code command line: the prompts fetched
from the pinned specification commit and checked against their recorded
hashes, every tool answered within the retry limit, the coherence answered, and
the result a complete report that the gate's own check accepts.

It is marked `tdqs_model`, and tests/tdqs_model_selection.py leaves it out of
every run unless AGENTIC_HIL_TDQS_MODEL is 1. With CLAUDE_CODE_OAUTH_TOKEN set
it uses that token, as the score job does; without it, the login of whoever
runs it.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

sys.path.insert(0, str(ROOT / "tools"))

import tool_definition_score as tds  # noqa: E402

pytestmark = pytest.mark.tdqs_model

TOKEN_VARIABLE = "CLAUDE_CODE_OAUTH_TOKEN"
# Taken at import, before any fixture of the suite can change the environment.
ENVIRON = dict(os.environ)


def test_the_real_model_scores_every_tool_of_this_checkout() -> None:
    record = tds.load_version_record()
    exported = tds.export_tools(SRC)
    prompts = tds.load_prompts(record, tds.default_cache_dir(ENVIRON), tds.fetch_url)
    scorer = tds.ClaudeCliScorer(prompts, record, environ=ENVIRON, token_env=TOKEN_VARIABLE if ENVIRON.get(TOKEN_VARIABLE) else None)

    scored = tds.score_side(exported, scorer, version_digest=tds.version_digest(record))

    names = [item["name"] for item in exported["tools"]]
    assert sorted(scored["tools"]) == sorted(names)
    for result in scored["tools"].values():
        assert set(result["justifications"]) == set(tds.DIMENSIONS)
        assert all(1 <= score <= 5 for score in result["scores"].values())
    assert set(tds.COHERENCE_DIMENSIONS) <= set(scored["coherence"])
    assert scored["rollups"]["scoredToolCount"] == len(names)
    report = tds.report_from_pairs(exported, exported, record, [(scored, scored)])
    assert report["decision"] == "pass"
    tds.check_report(json.loads(tds.report_json(report)), exported, exported, record)
    assert len(names) + 1 <= scorer.calls <= (len(names) + 1) * (1 + tds.RETRIES)
