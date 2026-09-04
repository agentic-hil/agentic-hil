"""The issue forms this repository serves, held to the rules GitHub enforces.

A malformed issue form fails nowhere a developer looks. GitHub drops the form and
serves a blank issue body instead, so the first person to find out is the reader
whose report has just become free text, and the report is the thing this
repository asked them for. These files are YAML nobody executes, which makes this
the only place that reads them at all.

What is pinned here is the schema GitHub's form parser applies (an element type it
knows, an identifier it accepts, a label where one is required, options where a
choice is offered, and the one combination it refuses outright: a rendered
textarea that is also required) and the routing the templates promise, so a
Discussions category or a first-run field cannot quietly leave the set.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIRECTORY = REPOSITORY_ROOT / ".github" / "ISSUE_TEMPLATE"

# https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/syntax-for-githubs-form-schema
ELEMENT_TYPES = frozenset({"markdown", "input", "textarea", "dropdown", "checkboxes"})
CHOICE_TYPES = frozenset({"dropdown", "checkboxes"})
IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]+$")
DISCUSSIONS_QUESTIONS = "https://github.com/agentic-hil/agentic-hil/discussions/categories/q-a"


def templates() -> list[Path]:
    """Every issue form in the tree, or a skip where this checkout has none.

    `.github/` is repository content and MANIFEST.in does not ship it, so an
    unpacked source distribution has no template directory to read. That checkout
    is collected rather than run, exactly as `test_ci_workflow.py` handles the
    workflows next door.
    """
    if not TEMPLATE_DIRECTORY.is_dir():
        pytest.skip("the issue templates are repository content and this checkout has none")
    return sorted(path for path in TEMPLATE_DIRECTORY.glob("*.yml") if path.name != "config.yml")


def document(path: Path) -> dict[str, Any]:
    if not path.is_file():
        pytest.skip(f"{path.name} is repository content and this checkout has none")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def problems(form: dict[str, Any]) -> list[str]:
    """Everything in one form that GitHub's own parser would refuse."""
    found = []
    for key in ("name", "description", "body"):
        if not form.get(key):
            found.append(f"the form states no {key}")
    body = form.get("body")
    if not isinstance(body, list):
        return found
    seen: set[str] = set()
    for position, element in enumerate(body):
        where = f"body[{position}]"
        kind = element.get("type")
        if kind not in ELEMENT_TYPES:
            found.append(f"{where} has type {kind!r}, which is not a form element")
            continue
        attributes = element.get("attributes") or {}
        identifier = element.get("id")
        if kind == "markdown":
            if not attributes.get("value"):
                found.append(f"{where} is markdown with nothing to render")
        else:
            if not attributes.get("label"):
                found.append(f"{where} has no label")
            if identifier is None:
                found.append(f"{where} has no id, so its answer arrives unnamed")
            elif not IDENTIFIER.match(str(identifier)):
                found.append(f"{where} has the id {identifier!r}, which is not an identifier")
            elif identifier in seen:
                found.append(f"{where} repeats the id {identifier!r}")
            else:
                seen.add(str(identifier))
        if kind in CHOICE_TYPES and not attributes.get("options"):
            found.append(f"{where} is a {kind} offering no options")
        validations = element.get("validations") or {}
        for key, value in validations.items():
            if key != "required":
                found.append(f"{where} states the validation {key!r}, which this schema has no place for")
            elif not isinstance(value, bool):
                found.append(f"{where} states required as {value!r} rather than a boolean")
        # GitHub refuses this pair outright: a textarea whose answer it renders
        # into a code block cannot also be required, and a form carrying both is
        # dropped rather than corrected.
        if kind == "textarea" and attributes.get("render") and validations.get("required"):
            found.append(f"{where} is a rendered textarea that is also required")
    return found


def test_every_issue_form_in_the_tree_is_one_github_will_serve() -> None:
    offences = {path.name: problems(document(path)) for path in templates()}

    assert {name: found for name, found in offences.items() if found} == {}


def test_a_form_element_without_an_id_is_refused() -> None:
    """The answer to an unnamed field arrives with nothing saying which field it was."""
    found = problems(
        {
            "name": "x",
            "description": "x",
            "body": [{"type": "input", "attributes": {"label": "Probe"}}],
        }
    )

    assert found == ["body[0] has no id, so its answer arrives unnamed"]


def test_a_repeated_element_id_is_refused() -> None:
    found = problems(
        {
            "name": "x",
            "description": "x",
            "body": [
                {"type": "input", "id": "probe", "attributes": {"label": "Probe"}},
                {"type": "input", "id": "probe", "attributes": {"label": "Board"}},
            ],
        }
    )

    assert found == ["body[1] repeats the id 'probe'"]


def test_a_rendered_textarea_that_is_also_required_is_refused() -> None:
    """The one combination GitHub answers by dropping the whole form."""
    found = problems(
        {
            "name": "x",
            "description": "x",
            "body": [
                {
                    "type": "textarea",
                    "id": "output",
                    "attributes": {"label": "Output", "render": "text"},
                    "validations": {"required": True},
                }
            ],
        }
    )

    assert found == ["body[0] is a rendered textarea that is also required"]


def test_a_choice_with_nothing_to_choose_from_is_refused() -> None:
    found = problems(
        {
            "name": "x",
            "description": "x",
            "body": [{"type": "dropdown", "id": "agent", "attributes": {"label": "Agent"}}],
        }
    )

    assert found == ["body[0] is a dropdown offering no options"]


def first_run_form() -> dict[str, Any]:
    return document(TEMPLATE_DIRECTORY / "first-run.yml")


def fields(form: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {element["id"]: element for element in form["body"] if element.get("id")}


def test_the_first_run_form_asks_for_what_a_report_has_to_carry() -> None:
    """A first run is only worth reporting if the report says which bench it was.

    The probe with its backend, the operating system and the flow it replaced are
    what turn one person's run into a path the next person can be told about, so
    each of them is required and none of them is a free-text afterthought.
    """
    form = first_run_form()
    element = fields(form)

    assert form["labels"] == ["first-run"]
    required = {name for name, field in element.items() if (field.get("validations") or {}).get("required")}
    assert required == {"agent", "probe", "host_os", "previous_flow", "what_happened"}
    assert element["agent"]["type"] == "dropdown"
    assert element["agent"]["attributes"]["options"] == ["Claude Code", "Codex", "opencode", "other"]
    # Optional on purpose: it is the field a reader may not have an answer to,
    # and refusing the report over it would cost the whole report.
    assert "last_real_bug" in element
    assert not (element["last_real_bug"].get("validations") or {}).get("required")


def test_the_first_run_form_asks_for_the_decisive_line_and_the_doctor_output() -> None:
    """Two things make a red run diagnosable, and a form that asks for neither gets neither."""
    what_happened = fields(first_run_form())["what_happened"]["attributes"]

    assert "decisive line" in what_happened["description"]
    assert "agentic-hil doctor" in what_happened["description"]


def test_the_first_run_form_says_a_red_run_is_as_welcome_as_a_green_one() -> None:
    """Nobody files the report that says it did not work unless they are told to."""
    intro = [element for element in first_run_form()["body"] if element["type"] == "markdown"]

    assert intro, "the form opens with no intro at all"
    assert "as welcome" in intro[0]["attributes"]["value"]


def test_a_reader_with_a_question_is_sent_to_discussions_rather_than_to_an_issue() -> None:
    """The question that is not a defect yet is the one this set used to have no answer for."""
    configuration = document(TEMPLATE_DIRECTORY / "config.yml")
    links = configuration["contact_links"]

    assert configuration["blank_issues_enabled"] is True
    assert DISCUSSIONS_QUESTIONS in [link["url"] for link in links]
    assert any("security" in link["url"] for link in links)
    assert len({link["name"] for link in links}) == len(links)
    assert len({link["url"] for link in links}) == len(links)
    for link in links:
        assert link["about"].strip(), link["name"]
