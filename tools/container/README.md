# The container test image

The tier of this project's suite that runs against the real tools, and the image
it runs in, published together so the environment is part of the result rather
than a property of whoever happened to run it.

## Why it exists

Everything under `tests/` proves the code against fakes written beside it, so a
test can only ever be as right as the person who wrote the fake was about the
tool. Four defects reached a bench through exactly that gap:

- `uv tool install` writes `python` at the `[tool]` level of `uv-receipt.toml`,
  not under `[tool.options]`, so the reinstall line a pinned installation is
  handed came out with no `--python` at all, under a summary promising to
  rebuild the installation as it stands.
- A virtual environment's `bin/python` is a symlink to the system interpreter,
  so `/proc/<pid>/exe` resolves outside the environment for every process it
  starts. A live MCP server was therefore never found, and an upgrade answered
  that no restart was needed while a server went on serving the previous
  release.
- A procfs process directory is stamped with the time of the lookup that created
  it, not with the time the process started, so a server up for hours reported
  itself as having started at the moment it was looked at.
- OpenOCD prints a failure-worded line after a `reset run` that has already
  restarted the board, and reading it as a verdict refused a step that had
  worked.

A fake cannot tell anyone any of that. The tests in `tests/container` install
with the real uv, read the real receipt, read the real `/proc`, and drive the
real OpenOCD binary.

## What it does not cover

Anything that needs a debug probe or the board behind it. That is the third
tier, `tests/bench`, which runs on a machine somebody owns and keeps, and is
started by `AGENTIC_HIL_BENCH=1`. The image here has no hardware and asks no
question that needs any.

## Build and run

From the repository root:

```
DOCKER_BUILDKIT=1 docker build -f tools/container/Dockerfile -t agentic-hil-container-tests .
docker run --rm agentic-hil-container-tests
```

`DOCKER_BUILDKIT=1` is not decoration. This build's ignore file is
`Dockerfile.dockerignore`, which sits beside the Dockerfile, and only BuildKit
prefers it to the repository root's `.dockerignore`. The root one belongs to the
evaluation images and sends only `evals/` and `src/` to the context, which is
not enough to install this checkout.

The run needs the network: uv resolves this distribution's dependency set from
the package index, and one test reads the release index over HTTPS to establish
that a currency claim is only made after the index has answered.

To run a subset, or to see a failure in full:

```
docker run --rm agentic-hil-container-tests python -m pytest tests/container -m container -v
```

To iterate on a change without rebuilding, mount the two directories the tests
read over the ones in the image:

```
docker run --rm -v "$PWD/src:/work/src" -v "$PWD/tests:/work/tests" \
    agentic-hil-container-tests python -m pytest tests/container -m container -q
```

## What is pinned in the image

- a slim Python base, so the interpreter is one version and not whichever one
  the host happens to have;
- uv at one exact version, named in the Dockerfile as `UV_VERSION`. The receipts
  these tests read are written by that program, and a different version may
  write them differently, which is the whole reason the tests exist. Moving the
  pin is a deliberate change with a test run behind it;
- the distribution's OpenOCD and `procps`, which are the debugger backend and
  the second opinion on a process's start time;
- this checkout, installed with its `dev` and `can` extras;
- `AGENTIC_HIL_CONTAINER_TESTS=1`, which is what stops every test in that
  directory from skipping. Without it a plain `pytest` in a checkout collects
  them and runs none of them, which is what keeps a developer's run and the
  hosted matrix out of uv's way.

## Where it runs on its own

The `Container tests` job in `.github/workflows/ci.yml` builds this image and
runs it on every pull request, and it is one of the jobs `Required CI` insists
on. Measured cold, with no layer cache and the base image pulled, the build
takes 28 seconds and the tests 15, so the job is about three quarters of a
minute of work.
