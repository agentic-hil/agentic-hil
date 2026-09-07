"""The action gate drawing, and the two pages that carry its claim.

The drawing at the top of `docs/safety-model.md` ends in a terminal box that
reads `crash / unknown effect` into `quarantine: operator recovery required`,
and the `aria-label` of both SVG files and the Markdown alt text of both image
lines repeat it in prose. The product does not do that. A COM write that raises
answers `serial_write_failed` with `side_effect_status: "unknown"` and opens an
incident on the lease; that incident stands down when the call ends, the lease
goes back to `active`, and the next write goes to the port. Only a broken audit
trail stands, because no reset writes a report that was never written.

So the same page says one thing in its picture and the other in its body, and
the picture is what a reader looks at first. This file holds the picture and the
prose around it to the rule the body, the README and the served guidance already
state.

What each test is for:

* The two drawings are one drawing in two palettes, so their wording is compared
  place for place and their geometry box for box, rather than each being read on
  its own. A correction applied to one and not the other is the failure this
  repository has already had once.
* The Markdown alt text is the drawing for a reader who cannot see it, so the
  whole of it is held to the drawing's own `aria-label` and not to a quotation of
  it, and both are held positively to the claim that is true.
* The claim itself is gated across every document a reader receives, as a rule
  and not as a blacklist: a clause that holds a resource for an unsettled effect
  until a person lifts it has to be a clause about the audit trail, which is the
  one family that really stands. The exemption term is read off
  `AUDIT_BROKEN_MARKER`, so it moves when the code moves, and it is applied to
  the clause that carries the hold rather than to the sentence around it, so a
  sentence cannot exempt itself by naming the audit chain somewhere else.
* The terminal box names what stands and nothing wider.
* The drawings stay renderable. A label that does not fit its chip is a defect
  the reader sees, so the chip rule every shipped drawing already obeys is
  derived from those drawings and applied to the corrected one.
* The three places that already state the rule correctly are pinned unchanged,
  because the correction is to the picture and never to the product, and the
  corrected `docs/security-design.md` sentence is pinned for what it must still
  say as well as for what it must stop saying.

`MANIFEST.in` ships `docs/safety-model.md`, `docs/security-design.md`,
`docs/installation.md` and `docs/mcp-hosts.md`, but not `docs/diagrams/`, so in a
source distribution the drawing readers skip while the page readers run. Every
contributor checkout and this project's CI have the drawings.
"""

from __future__ import annotations

import re
import subprocess
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest

from agentic_hil.coordination import AUDIT_BROKEN_MARKER
from agentic_hil.knowledge import LEASE_LIFECYCLE_DOCUMENT

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SVG = "{http://www.w3.org/2000/svg}"

# The page the drawing opens, and the two files it opens with. Light and dark are
# one drawing in two palettes and are never read apart.
DIAGRAM_PAGE = "docs/safety-model.md"
LIGHT = "docs/diagrams/action-gate.svg"
DARK = "docs/diagrams/action-gate-dark.svg"
DRAWINGS = (LIGHT, DARK)
DIAGRAM_DIRECTORY = REPOSITORY_ROOT / "docs" / "diagrams"

# The condition half of the claim: a call whose physical effect is not settled.
# These are the words the drawing and the two pages use for it today, plus the
# debug teardown's spelling of the same state, which `docs/security-design.md`
# sends to the same place.
UNSETTLED_EFFECT = re.compile(
    r"\bcrash(?:es|ed)?\b|\bunknown effect\b|\bunconfirmed (?:flash|reset)\b|\bhalt[^.;]{0,60}?\bnot (?:re)?confirmed\b",
    re.IGNORECASE,
)

# The hold half: a state a later call cannot lift, only a person.
STANDING_HOLD = re.compile(r"\boperator recovery\b|\ban operator recover\w*|\brecovery required\b", re.IGNORECASE)

# The one family that really stands, spelled from the marker the coordinator
# decides `incident_stands` by, so a rename takes this exemption with it.
AUDIT_TERM = AUDIT_BROKEN_MARKER.split("_")[0]

# A changelog records what a release said when it shipped. It is history, not a
# claim about the bench in front of a reader, and rewriting it would be rewriting
# the record.
NOT_A_CLAIM_ABOUT_THIS_BENCH = {"CHANGELOG.md"}

# Font sizes by class, off the stylesheet every one of these drawings carries.
FONT_SIZE = {"t": 15.0, "d": 13.0, "chip-label": 13.0, "cluster-label": 13.0}

# The flat advance the drawings are laid out with, and the padding a chip keeps
# around its label. Both are derived from the shipped chips rather than guessed;
# `test_the_chip_rule_is_the_one_every_shipped_drawing_already_obeys` is the
# derivation, and it fails if a drawing stops obeying it.
ADVANCE_PER_PIXEL = 6.7 / 13.0
CHIP_PADDING = 14.0

STOP_WORDS = {
    "a", "an", "the", "it", "its", "and", "or", "of", "to", "is", "are", "be", "that", "this",
    "by", "for", "with", "on", "in", "at", "from", "as", "not", "no", "then", "until", "while",
}


def stem(word: str) -> str:
    """A crude stem, enough to read `recovery`, `recovers` and `recovered` as one claim."""
    for suffix in ("ing", "ed", "s", "y"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def claim_words(text: str) -> set[str]:
    """What a phrase claims, with its grammar and its punctuation taken off."""
    return {stem(word) for word in re.findall(r"[a-z0-9-]+", text.lower()) if word not in STOP_WORDS}


def clauses(sentence: str) -> list[str]:
    """A sentence as the pieces a claim is actually made in.

    The hold is read per clause rather than per sentence, because a sentence that
    names the audit chain in one clause says nothing about what the next clause
    holds. This is the difference between a rule and an escape hatch."""
    return [piece.strip() for piece in re.split(r"[,;:]", sentence) if piece.strip()]


def terminal_clause(text: str) -> str:
    """The last clause of a sentence or of a list of them.

    Both the `aria-label` and the Markdown alt text put the outcome of a failed
    call last, after a semicolon or a full stop. That clause is the claim this
    file is about, and reading it structurally keeps the two held to each other
    rather than to a quotation either of them could drift away from."""
    pieces = [piece.strip() for piece in re.split(r"[.;]", text) if piece.strip()]
    assert pieces, f"nothing to read in {text!r}"
    return pieces[-1]


def document(relative: str) -> Path:
    path = REPOSITORY_ROOT / relative
    if not path.is_file():
        pytest.skip(f"this is a checkout without {relative}; the drawings ship with the repository, not with the sdist")
    return path


def tracked_documents() -> list[str]:
    """Every tracked file a reader reads: Markdown, plain text and the drawings.

    Listed by git the way `tests/test_prose_convention.py` lists the tree, and
    skipped where git cannot be asked, for the same reason: an unpacked sdist and
    a checkout with no git binary can answer nothing about what is tracked, and a
    gate that guessed would be reading build output and virtualenvs."""
    try:
        listing = subprocess.run(
            ["git", "-C", str(REPOSITORY_ROOT), "ls-files", "-z"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:  # pragma: no cover - needs a host without git
        pytest.skip(f"git could not list the tracked tree: {error}")
    if listing.returncode != 0:  # pragma: no cover - needs a directory that is not a repository
        pytest.skip(f"git could not list the tracked tree: {listing.stderr.strip() or listing.returncode}")
    listed = [name for name in listing.stdout.split("\0") if name]
    return [
        name
        for name in listed
        if name.endswith((".md", ".txt", ".svg"))
        and name not in NOT_A_CLAIM_ABOUT_THIS_BENCH
        and (REPOSITORY_ROOT / name).is_file()
    ]


def aria_label(relative: str) -> str:
    root = ElementTree.parse(document(relative)).getroot()
    label = root.get("aria-label")
    assert label, f"{relative} carries no aria-label; this test is not reading it any more"
    return label


def readable_prose(relative: str) -> list[str]:
    """What a reader is told by this file, as sentences.

    A drawing speaks to a reader in two ways and only one of them is prose: the
    `aria-label` is a sentence, and the boxes are a picture whose words are laid
    out rather than written. The picture is held by `terminal_box` instead."""
    text = document(relative).read_text(encoding="utf-8")
    if relative.endswith(".svg"):
        found = re.search(r'aria-label="([^"]*)"', text)
        text = found.group(1) if found else ""
    sentences = []
    for line in text.splitlines():
        sentences += [piece.strip() for piece in re.split(r"(?<=[.;])\s+", line) if piece.strip()]
    return sentences


def boxes_and_runs(relative: str) -> list[tuple[dict[str, float], str, str]]:
    """Every drawn text run with the box it is drawn in and the class it is drawn as.

    These drawings are written box then text, in document order, so a run belongs
    to the last rectangle before it. Read that way rather than by hit-testing
    coordinates, because a run that has drifted out of its box is exactly the
    defect this has to catch and hit-testing would silently drop it.

    The walk is over the whole document rather than over the root's direct
    children, so a run nested inside a group is measured rather than skipped; a
    run that is invisible to the measurement is a run that can overflow anything.

    A run drawn before any box is a free label on the canvas and has no box to be
    measured against; the action gate has none, and
    `test_the_drawing_is_well_formed_svg` is where that is asserted rather than
    assumed."""
    found: list[tuple[dict[str, float], str, str]] = []
    box: dict[str, float] | None = None
    outer = ""
    for element in ElementTree.parse(document(relative)).getroot().iter():
        if element.tag == f"{SVG}rect":
            if "cluster" in (element.get("class") or ""):
                continue
            box = {key: float(element.get(key, 0.0)) for key in ("x", "y", "width", "height")}
        elif element.tag in (f"{SVG}text", f"{SVG}tspan"):
            if element.tag == f"{SVG}text":
                outer = element.get("class") or ""
            style = element.get("class") or outer
            if box is not None and (element.text or "").strip():
                found.append((box, (element.text or "").strip(), style))
    return found


def drawn_runs(relative: str) -> list[str]:
    """Every word drawn in the file, read off the source rather than off the tree.

    Derived a second way on purpose: `boxes_and_runs` is the reading every
    measurement in this file depends on, and a reading compared only against
    itself proves nothing about what the file contains."""
    source = document(relative).read_text(encoding="utf-8")
    return [run.strip() for run in re.findall(r">([^<>]+)</(?:tspan|text)>", source) if run.strip()]


def chips(relative: str) -> list[tuple[dict[str, float], str]]:
    return [(box, run) for box, run, style in boxes_and_runs(relative) if "chip-label" in style]


def terminal_box(relative: str) -> str:
    """Everything a reader reads at the end of the drawing, the chip on the last edge included.

    Located as the lowest box on the canvas plus the chip label above it, so it
    stays the terminal box when its wording changes."""
    runs = boxes_and_runs(relative)
    lowest = max(box["y"] for box, _run, _style in runs)
    words = [run for box, run, _style in runs if box["y"] == lowest]
    words += [run for box, run in chips(relative) if box["y"] < lowest]
    return " ".join(words)


def geometry(relative: str) -> dict[str, object]:
    """The shape of the drawing, with every colour left out.

    Light and dark are one drawing in two palettes, so everything except the
    palette has to match: the canvas, every rectangle, and where every run of
    text is placed."""
    root = ElementTree.parse(document(relative)).getroot()
    return {
        "viewBox": root.get("viewBox"),
        "size": (root.get("width"), root.get("height")),
        "rects": [
            tuple(element.get(key) for key in ("class", "x", "y", "width", "height", "rx"))
            for element in root.iter(f"{SVG}rect")
        ],
        "placements": [
            tuple(element.get(key) for key in ("class", "x", "y"))
            for element in root.iter()
            if element.tag in (f"{SVG}text", f"{SVG}tspan")
        ],
        "edges": [element.get("d") for element in root.iter(f"{SVG}path")],
    }


def run_width(text: str, style: str) -> float:
    size = next((points for name, points in FONT_SIZE.items() if name in style), 13.0)
    return len(text) * size * ADVANCE_PER_PIXEL


def image_lines() -> dict[str, str]:
    """The alt text of each action gate image on the page, by the file it introduces."""
    lines = document(DIAGRAM_PAGE).read_text(encoding="utf-8").splitlines()
    found = {}
    for line in lines:
        match = re.match(r"!\[(?P<alt>[^]]*)]\((?P<target>[^)#]*)", line.strip())
        if match:
            target = f"docs/{match.group('target')}"
            if target in DRAWINGS:
                found[target] = match.group("alt")
    assert set(found) == set(DRAWINGS), f"{DIAGRAM_PAGE} opens with {sorted(found)}, not with both action gate drawings"
    return found


def statement_under(relative: str, heading: str, carrying: str) -> str:
    """The one statement under `heading` that carries `carrying`, as a reader meets it."""
    lines = document(relative).read_text(encoding="utf-8").splitlines()
    headings = [index for index, line in enumerate(lines) if line.strip() == heading]
    assert len(headings) == 1, f"{relative} has {len(headings)} {heading!r} headings; this test is not reading it any more"
    for line in lines[headings[0] + 1 :]:
        if line.startswith("## "):
            break
        if carrying in line:
            return line.strip()
    raise AssertionError(f"{relative} says nothing carrying {carrying!r} under {heading!r}")


@pytest.mark.parametrize("place", ["aria-label", "terminal box", "chip label"])
def test_the_light_and_the_dark_drawing_carry_the_same_wording(place: str) -> None:
    """One drawing in two palettes. A correction to one is a correction to both."""
    reader = {
        "aria-label": aria_label,
        "terminal box": terminal_box,
        "chip label": lambda relative: " | ".join(run for _box, run in chips(relative)),
    }[place]

    assert reader(LIGHT) == reader(DARK), f"the two action gate drawings disagree in their {place}"


def test_the_light_and_the_dark_drawing_carry_the_same_geometry() -> None:
    """The palette is the only thing the two files are allowed to differ in.

    A chip resized to fit a corrected label in one file and not in the other
    renders as two different pictures to two readers, and every measurement in
    this file is per file and would not see it."""
    light = geometry(LIGHT)
    dark = geometry(DARK)

    differing = sorted(key for key in light if light[key] != dark[key])
    assert differing == [], f"the two action gate drawings are not one drawing any more; they differ in {differing}"


@pytest.mark.parametrize("drawing", DRAWINGS)
def test_the_alt_text_says_what_the_aria_label_says(drawing: str) -> None:
    """The alt text is the drawing for a reader who cannot see it.

    The whole description is held, not only the claim at the end of it, because
    the flow a reader who cannot see the picture is told about is the rest of the
    alt text. Wording and grammar are allowed to differ from the `aria-label`;
    what is claimed is not."""
    spoken = aria_label(drawing)
    written = image_lines()[drawing]

    assert claim_words(written) == claim_words(spoken), (
        f"{DIAGRAM_PAGE} introduces {drawing} with a description its aria-label does not give: "
        f"{sorted(claim_words(written) ^ claim_words(spoken))}"
    )
    assert claim_words(terminal_clause(written)) == claim_words(terminal_clause(spoken)), (
        f"{DIAGRAM_PAGE} introduces {drawing} with a claim its aria-label does not make: "
        f"{sorted(claim_words(terminal_clause(written)) ^ claim_words(terminal_clause(spoken)))}"
    )


@pytest.mark.parametrize("drawing", DRAWINGS)
def test_the_spoken_claim_names_the_audit_trail_and_nothing_wider(drawing: str) -> None:
    """What the drawing tells a reader who receives it as prose.

    The drawn box is pinned positively below; this is the same pin on the two
    strings a screen reader is handed instead of the picture, so the correction
    is checked for being right and not only for having stopped being wrong."""
    for source, text in ((drawing, aria_label(drawing)), (DIAGRAM_PAGE, image_lines()[drawing])):
        claim = terminal_clause(text)
        assert AUDIT_TERM in claim.lower(), f"{source} ends its description of {drawing} without naming the audit trail: {claim!r}"
        assert not UNSETTLED_EFFECT.search(claim), f"{source} ends its description of {drawing} with an effect the next contact settles: {claim!r}"


def test_no_document_a_reader_receives_holds_a_resource_for_an_unsettled_effect() -> None:
    """The claim itself, gated as a rule across everything a reader receives.

    A clause that holds a resource until a person lifts it is a clause about the
    audit trail or it is wrong, because every other missing proof comes back at
    the next contact: the target's at the next reset into halt, a serial handle's
    and a CAN adapter's at their own next open, which the operating system
    refuses by itself if the handle is really stuck. The audit trail exempts the
    clause that carries the hold and never the sentence around it."""
    wrong = []
    for relative in tracked_documents():
        for sentence in readable_prose(relative):
            if not (UNSETTLED_EFFECT.search(sentence) and STANDING_HOLD.search(sentence)):
                continue
            holding = [clause for clause in clauses(sentence) if STANDING_HOLD.search(clause)]
            if holding and all(AUDIT_TERM in clause.lower() for clause in holding):
                continue
            wrong.append(f"{relative}: {sentence}")

    assert wrong == [], "these say an unsettled effect holds a resource until an operator recovers it:\n" + "\n".join(wrong)


@pytest.mark.parametrize("drawing", DRAWINGS)
def test_the_terminal_box_names_the_audit_trail_and_nothing_wider(drawing: str) -> None:
    """What the last box of the drawing is allowed to say.

    The standing quarantine is the broken audit trail, so that is what the box
    names. A crash and an unknown effect are settled by the next contact and
    belong nowhere near it."""
    box = terminal_box(drawing)

    assert AUDIT_TERM in box.lower(), f"{drawing} ends in a box that does not name the audit trail: {box!r}"
    assert not UNSETTLED_EFFECT.search(box), f"{drawing} ends in a box that names an effect the next contact settles: {box!r}"


def test_the_chip_rule_is_the_one_every_shipped_drawing_already_obeys() -> None:
    """The advance and the padding this file measures with, derived rather than guessed.

    Every chip in every shipped drawing is exactly its label wide plus its
    padding. That is the rule a corrected chip has to keep, and reading it off
    the drawings is what makes the width check below a measurement of this
    repository's own layout instead of a number somebody chose."""
    shipped = sorted(DIAGRAM_DIRECTORY.glob("*.svg")) if DIAGRAM_DIRECTORY.is_dir() else []
    if not shipped:
        pytest.skip("this is a checkout without the drawings; docs/diagrams ships with the repository, not with the sdist")

    measured = []
    for path in shipped:
        relative = path.relative_to(REPOSITORY_ROOT).as_posix()
        for box, label in chips(relative):
            measured.append((relative, label, box["width"], round(run_width(label, "chip-label") + CHIP_PADDING, 1)))
    assert measured, f"{len(shipped)} drawings are present and not one chip was read out of them; the reader has stopped seeing them"

    off = [entry for entry in measured if abs(entry[2] - entry[3]) > 0.1]
    assert off == [], f"these chips are not their label wide plus {CHIP_PADDING}: {off}"


@pytest.mark.parametrize("drawing", DRAWINGS)
def test_every_label_fits_the_box_it_is_drawn_in(drawing: str) -> None:
    """A longer label that overflows its chip is a defect a reader sees.

    So the wording is shortened and the box is not: this measures the wording
    against the box it was given."""
    overflowing = [
        (run, box["width"], round(run_width(run, style) + CHIP_PADDING, 1))
        for box, run, style in boxes_and_runs(drawing)
        if run_width(run, style) + CHIP_PADDING > box["width"] + 0.1
    ]

    assert overflowing == [], f"{drawing} draws these wider than the box they sit in: {overflowing}"


@pytest.mark.parametrize("drawing", DRAWINGS)
def test_every_box_and_every_label_stays_inside_the_view_box(drawing: str) -> None:
    """The drawing renders inside its own viewBox, or it renders cropped."""
    root = ElementTree.parse(document(drawing)).getroot()
    left, top, width, height = (float(value) for value in (root.get("viewBox") or "").split())

    outside = []
    for box, run, style in boxes_and_runs(drawing):
        extent = run_width(run, style) / 2.0
        centre = box["x"] + box["width"] / 2.0
        for name, low, high in (
            ("box", box["x"], box["x"] + box["width"]),
            ("label", centre - extent, centre + extent),
        ):
            if low < left or high > left + width:
                outside.append((run, name, round(low, 1), round(high, 1)))
        if box["y"] < top or box["y"] + box["height"] > top + height:
            outside.append((run, "box height", box["y"], box["y"] + box["height"]))

    assert outside == [], f"{drawing} draws these outside its viewBox: {outside}"


@pytest.mark.parametrize("drawing", DRAWINGS)
def test_the_drawing_is_well_formed_svg(drawing: str) -> None:
    """Valid SVG, and every word in the file reaches the measurements above.

    The two lists are derived apart, one by parsing the tree and one by reading
    the source, so a word the measuring walk drops is a word this notices."""
    root = ElementTree.parse(document(drawing)).getroot()

    assert root.tag == f"{SVG}svg"
    assert root.get("role") == "img"
    assert (root.findtext(f"{SVG}title") or "").strip(), f"{drawing} carries no title"

    written = drawn_runs(drawing)
    measured = [run for _box, run, _style in boxes_and_runs(drawing)]
    assert measured == written, f"{drawing} draws words this file never measures: {sorted(set(written) - set(measured))}"


def test_the_page_body_still_states_the_rule_it_states_now() -> None:
    """`docs/safety-model.md` under `## Permissions and validation`, unchanged.

    The body of the page is right and the picture above it is wrong, so the
    correction goes to the picture. This is the half that may not move."""
    statement = statement_under(DIAGRAM_PAGE, "## Permissions and validation", "standing, human-visible quarantine")

    assert "the standing, human-visible quarantine is the audit halt and nothing else" in statement
    assert "Those incidents end when the call that raised them ends" in statement
    assert "`no_standing_state` line in the recovery ledger" in statement
    assert "an `audit_broken` incident holds the bench" in statement


def test_the_landing_page_still_states_the_rule_it_states_now() -> None:
    """`README.md` under `## Security by construction`, unchanged."""
    statement = statement_under("README.md", "## Security by construction", "standing quarantine")

    assert "the standing quarantine is kept for the one state no later contact can rebuild, a broken audit trail" in statement


def test_the_served_guidance_still_states_the_rule_it_states_now() -> None:
    """The text the tool's own guidance hands a caller, unchanged.

    This one reaches a reader who has no repository around them, so it is read
    from the module rather than from a file."""
    assert "Only a broken audit trail qualifies, because no reset writes a report that was never written" in LEASE_LIFECYCLE_DOCUMENT
    assert "every other incident is open only for the length of the call that raised it" in LEASE_LIFECYCLE_DOCUMENT
    assert "stands down at the end of the call with a `no_standing_state` line in the recovery ledger" in LEASE_LIFECYCLE_DOCUMENT


def test_the_debug_teardown_sentence_promises_no_operator_recovery() -> None:
    """`docs/security-design.md`, the related sentence, corrected in the same pass.

    A session that ends without its halt reconfirmed is reported unconfirmed, and
    that report is what a caller acts on. An unconfirmed flash from a bare call
    gets no operator recovery either: its incident stands down when the call
    ends. The sentence loses the promise of a recovery nobody is owed and keeps
    everything else it tells a reader: which two ends reconfirm the halt, the two
    fields the report carries, that it is not a clean stop, and what happens to
    the incident instead."""
    statement = statement_under("docs/security-design.md", "## Mitigations", "halt_not_confirmed")

    assert "`stop_session` and service shutdown both re-interrupt the target" in statement
    assert "`safe_state_confirmed: false`" in statement
    assert "`halt_not_confirmed: true`" in statement
    assert "reported unconfirmed" in statement
    assert "rather than released as a clean stop" in statement
    assert "stands down when the call ends" in statement
    assert not STANDING_HOLD.search(statement), f"docs/security-design.md still promises an operator recovery: {statement}"
