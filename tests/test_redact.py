"""A captured stream carries the secret in its value, where a key name cannot see it.

Key-name redaction is what `redact_sensitive` has always done, and it is blind to
the shape this catches: a package manager prints the index it was configured
with, and a private index URL routinely carries `https://user:token@host/simple/`
inside the `stderr` string of a failed upgrade. That string reaches an operator
terminal and every agent reading a `--json` result, because both sinks serialize
the same document through the same function.

The property under test is as much about what survives as about what goes. A
captured stream is published so the operator can read the manager's own words,
so each shape below is asserted as the whole stream against its source with
exactly the secret substring replaced: anything else the pass touched, reflowed
or dropped fails that comparison. The other half of the line is drawn by
`test_the_content_pass_stops_at_the_prose_fields`: a false positive over a
`summary` or a `likely_causes` entry would eat the diagnosis those fields exist
to carry, so the content pass runs under stream keys only.
"""

from __future__ import annotations

import time

import pytest

from agentic_hil.cli import emit_result
from agentic_hil.redact import redact_sensitive

MARKER = "[redacted]"

# uv's own resolution failure against a private index, with the credential the
# index URL was configured with. The hint prints the URL verbatim, which is the
# reported exposure: this text is a literal here because a paraphrase would stop
# testing the shape a real bench produced.
INDEX_SECRET = "s3cr3t-index-token"
UV_PRIVATE_INDEX_FAILURE = (
    "  x No solution found when resolving tool dependencies:\n"
    "  `-> Because agentic-hil was not found in the package registry, we can conclude that\n"
    "      your requirements are unsatisfiable.\n"
    "\n"
    f"      hint: An index URL (https://buildbot:{INDEX_SECRET}@packages.example.internal/simple/)\n"
    "      could not be queried due to a lack of valid authentication credentials (401\n"
    "      Unauthorized).\n"
)

# The same failure as pip reports it, which echoes the header it sent rather than
# the URL it sent it to.
BEARER_SECRET = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJidWlsZGJvdCJ9.2gTn-8Qk"
PIP_BEARER_FAILURE = (
    "Looking in indexes: https://packages.example.internal/simple/\n"
    f"  request headers: Authorization: Bearer {BEARER_SECRET}\n"
    "ERROR: Could not find a version that satisfies the requirement agentic-hil (from versions: none)\n"
)

BASIC_SECRET = "YnVpbGRib3Q6czNjcjN0LWluZGV4LXRva2Vu"
PIP_BASIC_FAILURE = (
    "  sending request to https://packages.example.internal/simple/agentic-hil/\n"
    f"  with headers {{'Authorization': 'Basic {BASIC_SECRET}', 'Accept': 'application/json'}}\n"
    "  got 403 Forbidden\n"
)

QUERY_SECRET = "b4dc0ffee0ddf00d"
ASSIGNMENT_SECRET = "hunter2-not-a-real-one"
FLAG_SECRET = "cs-9f2a41b8e7"
MANAGER_COMMAND_ECHO = (
    f"running: PIP_INDEX_PASSWORD={ASSIGNMENT_SECRET} uv tool upgrade agentic-hil\n"
    f"  GET https://packages.example.internal/simple/agentic-hil/?token={QUERY_SECRET}&trace=1\n"
    f"  retrying with --client-secret={FLAG_SECRET} --keyring-provider=subprocess\n"
    "  -> 401 Unauthorized\n"
)

# Everything a stream may carry that looks credential-ish and is not: a public
# index URL, a colon that introduces a diagnosis rather than a value, a
# non-credential name that happens to end in `key`, and a name whose secret word
# is not separated from its prefix (`mytoken`), which the key pattern would not
# have matched either.
CLEAN_UPGRADE_STREAM = (
    "Looking in indexes: https://pypi.org/simple/, https://packages.example.internal/simple/\n"
    "Requirement already satisfied: agentic-hil in /usr/lib/python3/dist-packages\n"
    "hint: token: expired at 2026-08-19T10:00:00Z, renew it and run the upgrade again\n"
    "  resource_key=dut_can mytoken=not-a-credential --password not-an-assignment\n"
    "  error: unexpected argument '--api-key' found\n"
)


def test_a_credentialed_index_url_in_a_stream_keeps_the_account_and_masks_the_password() -> None:
    """`scheme://user:secret@host` loses the secret and nothing else.

    The account survives on purpose: it names which credential the bench is
    configured with, which is half the diagnosis of a 401, and it is not the
    thing that opens the index."""
    redacted = redact_sensitive({"install": {"returncode": 2, "stderr": UV_PRIVATE_INDEX_FAILURE}})

    stream = redacted["install"]["stderr"]
    assert INDEX_SECRET not in stream
    assert "https://buildbot:[redacted]@packages.example.internal/simple/" in stream
    assert stream == UV_PRIVATE_INDEX_FAILURE.replace(INDEX_SECRET, MARKER)


def test_a_credentialed_url_after_leading_punctuation_still_masks_the_password() -> None:
    """A `.`, `-` or `+` directly before a URL is punctuation, not part of the
    scheme, because none of them can begin a URI scheme; the scheme starts at the
    following letter and the credential after it is masked all the same.

    A scheme-anchoring shortcut once read those three characters as proof that no
    scheme could start next, so a stream carrying `-https://user:secret@host`,
    `.https://…` or `+https://…` came through unchanged and leaked the password.
    The account survives and only the secret goes, as for a URL that leads its
    line."""
    for lead in (".", "-", "+"):
        line = f"hint: {lead}https://buildbot:{INDEX_SECRET}@packages.example.internal/simple/\n"

        out = redact_sensitive({"stderr": line})["stderr"]

        assert INDEX_SECRET not in out, f"password leaked after a leading {lead!r}"
        assert out == line.replace(INDEX_SECRET, MARKER)


def test_a_bearer_authorization_line_in_a_stream_masks_the_token_and_keeps_the_scheme() -> None:
    """The line still says a bearer token was sent, which is the diagnosis."""
    redacted = redact_sensitive({"stdout": PIP_BEARER_FAILURE})

    stream = redacted["stdout"]
    assert BEARER_SECRET not in stream
    assert "Authorization: Bearer [redacted]" in stream
    assert stream == PIP_BEARER_FAILURE.replace(BEARER_SECRET, MARKER)


def test_a_basic_authorization_blob_in_a_stream_is_masked_inside_the_quotes() -> None:
    """A base64 blob is the credential in clear text, and the quoting around the
    header dict a manager echoes has to come through untouched."""
    redacted = redact_sensitive({"stderr": PIP_BASIC_FAILURE})

    stream = redacted["stderr"]
    assert BASIC_SECRET not in stream
    assert "'Authorization': 'Basic [redacted]'" in stream
    assert stream == PIP_BASIC_FAILURE.replace(BASIC_SECRET, MARKER)


def test_a_token_or_password_assignment_in_a_stream_masks_only_the_value() -> None:
    """`token=` in a query string, `PASSWORD=` and `--client-secret=` in a command.

    The query separator, the trailing parameter, the following flag and the rest
    of the command are part of what the operator needs, so the mask ends where
    the value does."""
    redacted = redact_sensitive({"install": {"stdout": MANAGER_COMMAND_ECHO}})

    stream = redacted["install"]["stdout"]
    assert QUERY_SECRET not in stream
    assert ASSIGNMENT_SECRET not in stream
    assert FLAG_SECRET not in stream
    assert "PIP_INDEX_PASSWORD=[redacted] uv tool upgrade agentic-hil" in stream
    assert "?token=[redacted]&trace=1" in stream
    assert "--client-secret=[redacted] --keyring-provider=subprocess" in stream
    assert stream == MANAGER_COMMAND_ECHO.replace(QUERY_SECRET, MARKER).replace(ASSIGNMENT_SECRET, MARKER).replace(FLAG_SECRET, MARKER)


def test_a_quoted_assignment_masks_through_the_closing_quote_not_the_first_space() -> None:
    """A quoted value carries the credential across the space or comma inside it.

    `PASSWORD='correct horse battery staple'` is one password, not a first word
    and a leaked remainder, and `PASSWORD="alpha,beta gamma"` is one too. A mask
    that ended at the first delimiter emitted the tail in clear text; the quote
    ends the value, and the quote characters themselves come through so the line
    still reads as an assignment."""
    spaced = "  running: PIP_INDEX_PASSWORD='correct horse battery staple' uv tool upgrade\n"
    comma = "  env: DB_PASSWORD=\"alpha,beta gamma\" other=kept\n"

    spaced_out = redact_sensitive({"stdout": spaced})["stdout"]
    comma_out = redact_sensitive({"stdout": comma})["stdout"]

    assert "correct" not in spaced_out
    assert "horse battery staple" not in spaced_out
    assert spaced_out == "  running: PIP_INDEX_PASSWORD='[redacted]' uv tool upgrade\n"
    assert "alpha" not in comma_out
    assert "beta gamma" not in comma_out
    assert comma_out == '  env: DB_PASSWORD="[redacted]" other=kept\n'


def test_a_quoted_assignment_masks_across_an_escaped_quote() -> None:
    """An escaped quote inside the value does not end it, so the credential after
    it is masked too rather than leaking past a `\\"`."""
    line = 'token="a\\"b c d" trailing=kept\n'

    out = redact_sensitive({"stderr": line})["stderr"]

    assert "b c d" not in out
    assert out == 'token="[redacted]" trailing=kept\n'


def test_a_quoted_assignment_that_never_closes_masks_to_the_line_end() -> None:
    """An unterminated quote is the shell reading the rest of the line as the
    value, so the mask follows it there rather than stopping at the first space
    and leaking the rest."""
    line = "  PASSWORD='alpha beta gamma\n  next line kept\n"

    out = redact_sensitive({"stdout": line})["stdout"]

    assert "alpha beta gamma" not in out
    assert out == "  PASSWORD='[redacted]\n  next line kept\n"


def test_a_quoted_assignment_masks_across_a_newline_inside_the_value() -> None:
    """A value quoted across a line break is still one credential up to its
    matching quote, so the mask crosses the newline instead of stopping at it and
    emitting the second line in clear text. Everything after the closing quote is
    byte-identical."""
    line = 'PASSWORD="alpha\nbeta gamma" rest\n'

    out = redact_sensitive({"stdout": line})["stdout"]

    assert "alpha" not in out
    assert "beta gamma" not in out
    assert out == 'PASSWORD="[redacted]" rest\n'


def test_a_quoted_assignment_masks_across_an_escaped_line_continuation() -> None:
    """A backslash-newline inside the value is a line continuation, not the end of
    the value, so it is consumed whole and the credential after it is masked too
    rather than leaking past the break."""
    line = 'token="alpha\\\nbeta" trailing=kept\n'

    out = redact_sensitive({"stderr": line})["stderr"]

    assert "beta" not in out
    assert out == 'token="[redacted]" trailing=kept\n'


def test_an_unterminated_quoted_assignment_masks_across_a_line_continuation() -> None:
    """A backslash-newline inside an *unterminated* double quote is a line
    continuation, not the value's end, so the mask crosses it to the logical
    line's end rather than leaking the bytes on the next physical line.

    `token="alpha\\<newline>beta gamma` never closes its quote, and the shell reads
    the backslash-newline as joining the two physical lines into one value; a mask
    that stopped its unterminated arm at the backslash-newline emitted `beta gamma`
    in clear text. Repeated continuations are consumed the same way, and a second
    pass over the masked line changes nothing."""
    line = 'token="alpha\\\nbeta gamma\nnext line kept\n'
    repeated = 'token="a\\\n\\\nb c\nnext line kept\n'

    out = redact_sensitive({"stderr": line})["stderr"]
    repeated_out = redact_sensitive({"stderr": repeated})["stderr"]

    assert "beta gamma" not in out
    assert out == 'token="[redacted]\nnext line kept\n'
    assert redact_sensitive({"stderr": out})["stderr"] == out
    assert "b c" not in repeated_out
    assert repeated_out == 'token="[redacted]\nnext line kept\n'
    assert redact_sensitive({"stderr": repeated_out})["stderr"] == repeated_out


def test_a_long_unterminated_value_is_masked_in_linear_time() -> None:
    """The content pass runs over captured output before both CLI and MCP
    serialization, so it must stay linear: one long line cannot be allowed to stall
    result delivery.

    A quadratic pass took over a second on 50k characters in this checkout and did
    not finish a 1 MB unterminated value at all; the linear pass does 1 MB in well
    under this bound. The exact output is asserted alongside the time so a
    regression that traded correctness for speed would fail the same test."""
    line = 'PASSWORD="' + "a" * 1_000_000

    start = time.perf_counter()
    out = redact_sensitive({"stderr": line})["stderr"]
    elapsed = time.perf_counter() - start

    assert elapsed < 5.0, f"redaction of a 1 MB unterminated value took {elapsed:.2f}s"
    assert out == 'PASSWORD="[redacted]'


def test_a_long_scheme_like_run_without_a_url_is_scanned_in_linear_time() -> None:
    """The URL-userinfo pass runs over captured output before both serializers, so
    a long line of scheme-valid characters that never reaches `://` must not be
    retried as a scheme from every position.

    A `scheme://…` regex was quadratic on exactly this input, a captured stream
    can carry 10k+ such bytes on one line, enough to stall result delivery, which
    the deterministic single pass avoids by locating each `://` once. A 1 MB run
    with no `://` finishes well under this bound and comes through byte for byte,
    so a regression that reintroduced the quadratic scan would fail the same
    test."""
    line = "a" * 1_000_000

    start = time.perf_counter()
    out = redact_sensitive({"stderr": line})["stderr"]
    elapsed = time.perf_counter() - start

    assert elapsed < 5.0, f"scanning a 1 MB scheme-like run took {elapsed:.2f}s"
    assert out == line


def test_an_unquoted_value_runs_through_the_punctuation_that_is_valid_inside_it() -> None:
    """A comma and a bracket are ordinary characters in a shell token and in query
    data, so the value runs through them to the space that ends the token; a mask
    that stopped at the comma or the bracket left the tail in clear text.

    Idempotence does not depend on treating the bracket as a boundary: the
    `[redacted]` the first pass writes is itself masked back to `[redacted]`."""
    comma = "  env: PASSWORD=alpha,beta rest\n"
    bracket = "  env: PASSWORD=abc[def rest\n"

    comma_out = redact_sensitive({"stdout": comma})["stdout"]
    bracket_out = redact_sensitive({"stdout": bracket})["stdout"]

    assert "alpha,beta" not in comma_out
    assert ",beta" not in comma_out
    assert comma_out == "  env: PASSWORD=[redacted] rest\n"
    assert "abc" not in bracket_out
    assert "[def" not in bracket_out
    assert bracket_out == "  env: PASSWORD=[redacted] rest\n"
    assert redact_sensitive({"stdout": comma_out})["stdout"] == comma_out
    assert redact_sensitive({"stdout": bracket_out})["stdout"] == bracket_out


def test_a_shell_assignment_runs_through_a_hash_and_an_escaped_space() -> None:
    """A `#` is a comment only at the start of a word, so mid-value it is data, and
    a backslash-space is one escaped character that keeps the word going.

    `PASSWORD=alpha#beta` is one shell value, not `alpha` and a leaked `#beta`, and
    `PASSWORD=alpha\\ beta` is one value `alpha beta`, not `alpha` and a leaked
    ` beta`; a mask that took `#` or the escaped space for a boundary emitted the
    tail in clear text. Both end at the unescaped space that really ends the
    word."""
    hashed = "  env: PASSWORD=alpha#beta rest\n"
    escaped = "  env: PASSWORD=alpha\\ beta rest\n"

    hashed_out = redact_sensitive({"stdout": hashed})["stdout"]
    escaped_out = redact_sensitive({"stdout": escaped})["stdout"]

    assert "beta" not in hashed_out
    assert hashed_out == "  env: PASSWORD=[redacted] rest\n"
    assert "beta" not in escaped_out
    assert escaped_out == "  env: PASSWORD=[redacted] rest\n"
    assert redact_sensitive({"stdout": hashed_out})["stdout"] == hashed_out
    assert redact_sensitive({"stdout": escaped_out})["stdout"] == escaped_out


def test_a_shell_assignment_masks_concatenated_quoted_segments() -> None:
    """A shell word is any run of quoted and unquoted segments stuck together, so a
    quote that ends does not end the value if the word keeps going.

    `PASSWORD=alpha"beta gamma"delta` and `PASSWORD="alpha"beta` are each one value
    to the space that ends the word; a mask that stopped at the first closing quote
    left the concatenated remainder in clear text. The whole word is masked, and
    the surrounding quotes are dropped because the value is not a lone quoted
    string."""
    trailing = 'PASSWORD=alpha"beta gamma"delta rest\n'
    leading = 'PASSWORD="alpha"beta rest\n'

    trailing_out = redact_sensitive({"stdout": trailing})["stdout"]
    leading_out = redact_sensitive({"stderr": leading})["stderr"]

    assert "beta gamma" not in trailing_out
    assert "delta" not in trailing_out
    assert trailing_out == "PASSWORD=[redacted] rest\n"
    assert "beta" not in leading_out
    assert leading_out == "PASSWORD=[redacted] rest\n"
    assert redact_sensitive({"stdout": trailing_out})["stdout"] == trailing_out


def test_a_query_value_runs_through_the_shell_delimiters_that_are_data_in_it() -> None:
    """`)` and `;` end a shell word but are ordinary inside a query value, so a
    query parameter runs through them to the `&` that ends it.

    `?access_token=alpha)beta&next=1` masks `alpha)beta`, not `alpha` and a leaked
    `)beta`; the `&` and the following parameter are what the operator needs, so
    the mask ends there."""
    query = "  GET https://h/p?access_token=alpha)beta;more&next=1\n"

    out = redact_sensitive({"stdout": query})["stdout"]

    assert "alpha)beta;more" not in out
    assert ")beta" not in out
    assert out == "  GET https://h/p?access_token=[redacted]&next=1\n"
    assert redact_sensitive({"stdout": out})["stdout"] == out


def test_a_second_pass_over_a_masked_quoted_assignment_changes_nothing() -> None:
    """Redacting `'[redacted]'` again yields `'[redacted]'`, so the quoted pass is
    idempotent the same way the unquoted one is."""
    once = redact_sensitive({"stdout": "PASSWORD='correct horse' TOKEN=\"a b\"\n"})

    assert redact_sensitive(once) == once
    assert once["stdout"] == 'PASSWORD=\'[redacted]\' TOKEN="[redacted]"\n'


def test_a_stream_with_no_secrets_comes_through_byte_identical() -> None:
    """The common case, and the one a content pass gets wrong.

    Nothing here is a credential, and every line of it is diagnosis: the pass
    that eats one of these lines has cost more than it saved."""
    document = {"install": {"returncode": 1, "stdout": CLEAN_UPGRADE_STREAM, "stderr": ""}}

    redacted = redact_sensitive(document)

    assert redacted == document
    assert redacted["install"]["stdout"] == CLEAN_UPGRADE_STREAM


def test_the_content_pass_stops_at_the_prose_fields() -> None:
    """Where the line is drawn: stream keys at any depth, and nothing else.

    A `summary` and a `likely_causes` entry are written by this project for a
    person to read, and a mask over one of them would replace text nobody chose
    to make a secret. The key-name arm keeps working beside the new one."""
    document = {
        "error_type": "upgrade_failed",
        "summary": "The package manager reported token=expired for the configured index.",
        "likely_causes": ["The index credential in PIP_INDEX_PASSWORD=... has been rotated."],
        "api_token": INDEX_SECRET,
        "install": {"stderr": f"https://buildbot:{INDEX_SECRET}@packages.example.internal/simple/"},
    }

    redacted = redact_sensitive(document)

    assert redacted["summary"] == document["summary"]
    assert redacted["likely_causes"] == document["likely_causes"]
    assert redacted["api_token"] == MARKER
    assert redacted["install"]["stderr"] == f"https://buildbot:{MARKER}@packages.example.internal/simple/"


def test_a_stream_that_arrives_as_a_list_of_lines_is_masked_line_by_line() -> None:
    """A stream key is a stream whether the capture kept it whole or split it."""
    redacted = redact_sensitive({"stdout": [f"index https://buildbot:{INDEX_SECRET}@h/simple/", "done"]})

    assert redacted["stdout"] == [f"index https://buildbot:{MARKER}@h/simple/", "done"]


def test_both_operator_facing_sinks_publish_the_masked_stream(capsys: pytest.CaptureFixture[str]) -> None:
    """The document and the rendering are the two ways a stream leaves this process.

    They share one function, which is why the fix lands in `redact_sensitive`
    rather than in the renderer: masking one sink and not the other would leave
    the credential a `--json` result away. The masked URL is asserted whole in
    both, since the renderer wraps with `break_long_words=False` and cannot
    split it."""
    document = {"ok": False, "error_type": "upgrade_failed", "summary": "The package manager reported an upgrade failure.", "stderr": UV_PRIVATE_INDEX_FAILURE}
    masked_url = f"https://buildbot:{MARKER}@packages.example.internal/simple/"

    emit_result(document, "upgrade", human=False)
    machine = capsys.readouterr().out
    emit_result(document, "upgrade", human=True)
    rendered = capsys.readouterr().out

    assert INDEX_SECRET not in machine
    assert INDEX_SECRET not in rendered
    assert masked_url in machine
    assert masked_url in rendered


def test_a_second_pass_over_an_already_masked_stream_changes_nothing() -> None:
    """Both sinks redact, and a document can reach one of them through the other.

    A pass that masked its own marker would grow the stream a bracket at a time
    and stop being byte-identical to itself."""
    once = redact_sensitive({"stdout": MANAGER_COMMAND_ECHO, "stderr": UV_PRIVATE_INDEX_FAILURE})

    assert redact_sensitive(once) == once
