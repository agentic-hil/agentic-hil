# Support

Agentic HIL exists so that a coding agent can develop firmware on the board in
front of you, with the run on that hardware, rather than a green build, deciding
whether the work is done. This page says what is supported when you take that
path, how quickly you can expect an answer, and what is deliberately not
promised.

## Platforms

Linux, macOS and Windows are supported equally. CI runs the full suite on all
three, and the reference path is proven on Linux and Windows benches, with a
board attached rather than only in a hosted runner. Python 3.10 or newer is the
floor on every one of them; [Installation](installation.md) has the per-platform
detail.

## What "supported" is attached to

The supported unit is the debug probe together with the backend that drives it,
not a board:

| Probe | Backend |
|---|---|
| ST-Link | OpenOCD, or the STM32CubeProgrammer CLI |
| CMSIS-DAP probes | pyOCD |

All three backends are supported. Your board is reached through one of them, so
what decides whether a bench works is the probe on it, the backend behind that
probe, and the target script or controller name your configuration binds to it,
which [Configuration](configuration.md) describes. The Nucleo-F446RE is the
reference this is proven on: it is the board in
[the STM32 starter](https://github.com/agentic-hil/stm32-starter) and in the
worked example in this repository. It is the reference, not the requirement.

## Response

- **Issues: within 24 hours on workdays.** That is a first answer, not a fix: a
  question answered, a reproduction asked for, or a defect confirmed and
  labelled.
- **Security reports: within seven days.**
  [SECURITY.md](https://github.com/agentic-hil/agentic-hil/blob/master/SECURITY.md)
  is the route and the acknowledgement window, and a policy or permission bypass
  is a vulnerability rather than a defect. Report one privately and never as a
  public issue.

## Releases

The last two minor releases are maintained. A report against either is one this
project acts on, and a fix ships forward as a new release rather than as a patch
onto an older line, so the answer to a defect you hit is an upgrade.
[Release strategy](release-strategy.md) has the versioning rules and the release
gate.

## Where to go

| What you have | Where it goes |
|---|---|
| a question, or something that is not clearly a defect yet | [Discussions Q&A](https://github.com/agentic-hil/agentic-hil/discussions/categories/q-a) |
| a first run on your own bench, green or red | [the first run report](https://github.com/agentic-hil/agentic-hil/issues/new?template=first-run.yml) |
| a run, a board or a bench worth showing | [Discussions, Show and tell](https://github.com/agentic-hil/agentic-hil/discussions/categories/show-and-tell) |
| a reproducible defect | [the bug report](https://github.com/agentic-hil/agentic-hil/issues/new?template=bug_report.yml) |
| a vulnerability | [SECURITY.md](https://github.com/agentic-hil/agentic-hil/blob/master/SECURITY.md) |

A first run report is wanted whichever way the run went. A red one says where
the path breaks for somebody who has not read the source, which is the only way
that gets found; a green one says which probe, backend and operating system
combination has actually been walked end to end by someone other than this
project.

## What is not promised

**No list of supported or unsupported boards.** The probe and its backend
decide, so a board is not something this project certifies. Where the probe and
backend are supported and your configuration binds the target, the board
follows; where they are not, no entry in a table would have made it work.

**No remote bench.** Agentic HIL drives hardware you own and have plugged in.
There is no board to borrow here and nothing that reaches a board over the
network on your behalf, so a report that needs hardware to reproduce needs your
hardware.
