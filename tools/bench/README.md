# The bench tier's image

The tier of this project's suite that needs an in-circuit debugger or programmer
and the board behind it, and the image it runs in, so that a run on the board
measures one commit's build rather than whatever the machine the board is
attached to happened to carry.

## Why it runs in an image

`tests/bench` drives a real probe and a real board, so it runs on the one machine
they are attached to. Run there directly, it measured that machine as much as the
commit: its OpenOCD, its cross compiler, its GDB, its Python and whatever sat in
its user site. `tools/bench/Dockerfile` puts all of that into an image built from
the committed tree, the way `tools/container/Dockerfile` does for the tier that
needs no hardware, and `tools/bench_in_container.py` runs the tier in it against
the attached board. The machine provides the board, the probe, the device nodes
they appear as and the right to open them, and nothing else.

## What the machine provides once

All of this is set up once, outside this repository, on the Linux machine the
board is attached to and for the user the runs happen as. Nothing in this
repository installs any of it, and the gate's workflow neither installs nor
configures anything on the machine.

- The board, attached through its in-circuit debugger or programmer, with the
  probe's serial port enumerated. A run expects exactly one probe with a serial
  port; with more attached, `--usb-device` and `--serial-device` name the nodes.
- `python3` 3.10 or newer and `git`, which is everything the runner itself
  needs. It runs from a checkout and installs nothing.
- Rootless Podman, with crun as the OCI runtime, `newuidmap` and `newgidmap`
  from the uidmap package, and slirp4netns for the build's network. On Debian
  and Ubuntu that is `apt-get install podman crun uidmap slirp4netns`. crun is
  not optional: a run keeps the user's supplementary groups in the container
  with `--group-add keep-groups`, which crun implements and runc does not, and
  the runner refuses Podman without it.
- A range of subordinate user ids and one of group ids for that user, in
  `/etc/subuid` and `/etc/subgid`, which the build needs to create files owned
  by more than one user. `grep` for the user name in both files shows them;
  where they are missing, `usermod --add-subuids 100000-165535 --add-subgids
  100000-165535 <user>` adds them.
- Linger for that user, `loginctl enable-linger <user>`, so the user's runtime
  directory and its systemd user manager exist while nobody is logged in. That
  is the state a runner service meets them in, and rootless Podman keeps its own
  state there.
- The right to open the probe's USB node under `/dev/bus/usb` and its serial
  port for reading and writing, as that user and through a group: membership of
  the groups the two nodes carry, or a udev rule that gives them a group the
  user belongs to. `lsusb` names the bus and device number of the USB node, and
  `ls -l` on both nodes shows their owner, group and mode. Under rootless Podman
  the tier opens the nodes with exactly this user's rights, and the runner
  refuses a node the user cannot open before it builds or starts anything,
  naming the node.
- A restart of the runner's service, or a new login, after a group change. A
  process keeps the groups it was started with, and so does everything it
  starts.

`python3 tools/bench_in_container.py --build-only`, run as that user, is the
check that the runtime builds. A run of the tier from a terminal, and then the
gate's first dispatch, is the check that the rest is in place.

Docker is accepted instead of Podman, with `--runtime docker`, and differs in two
places. Docker's container root is the host's root, so the runner starts the
tier as the invoking user and adds each group the nodes are opened through that
the user already belongs to. And Docker recreates a device node in the container
with its owner, group and mode and without its ACL, so a node this user can open
only through an ACL is refused with the owner, group and mode that fell short.
Rootless Docker and Docker Desktop keep the container in a user namespace or a
virtual machine of their own, where the nodes are not the host's. Neither is
supported for a run, and the runner does not tell them apart from a Docker that
is; both build the image.

## Run it

From a checkout on the machine the board is attached to:

```
python3 tools/bench_in_container.py                      # the whole tier
python3 tools/bench_in_container.py -- tests/bench/test_bench_serial.py -x
python3 tools/bench_in_container.py --runtime docker
python3 tools/bench_in_container.py --build-only         # the image alone, anywhere
```

A run finds the probe through sysfs by its USB vendor and product ids, never by
a serial number; takes this machine's run lock; builds the image; runs the tier
with the probe's nodes handed in; copies the JUnit report out; and reads pytest's
summary line as the verdict.

- The image is built from the commit checked out, never from the working tree.
  Uncommitted changes are named, and are not what runs.
- Everything after `--` goes to pytest and replaces the default selection,
  `tests/bench -v`.
- `--output` is where the JUnit report, `bench-junit.xml`, and the log,
  `bench-tier.log`, land: `bench-results` by default. A report or a log an
  earlier run left there is removed first. The container never sees this
  directory: the tier writes its report into one of the run's own, and only
  that report, read as a regular file and never through a link, is copied out.
- The run lock is the one `tools/ci_linux.py` and `tools/loop_in_container.py`
  take, `~/.agentic-hil/ci-linux.lock`, so runs of any of them on one machine
  queue behind one another. `--no-wait` refuses instead of queueing. Below it,
  the machine's device-lock directory, `~/.agentic-hil/device-locks`, is mounted
  into the container at the same place under its home, so the tier and every
  other run on the machine still meet at the board's own lock.
- `--usb-device` and `--serial-device` name the probe's nodes instead of
  finding them. They go together, and each can be given more than once.

The container gets the probe's device nodes, the machine's device-lock
directory, the serial port's `/dev/serial/by-id` links read only, and the
directory the report is written to, and nothing else of the machine: no network,
no capabilities, and a process table and a host name of its own. A run as root
is refused, because root's device locks are not the ones the board's user takes.

## Interrupting a run

Ctrl-C, or the SIGTERM a runner's cancel sends after its SIGINT, stops the
container with SIGINT, the image's stop signal. tini hands it to pytest's whole
process group, as Ctrl-C in a terminal would, so the tier tears down the way an
interrupted pytest does and the fixture that puts the demo back on the board
runs. The runner waits up to a minute for that, and ignores further interrupts
while it does.

A container the runner cannot confirm removed may still hold the board. The
runner then exits 7, names the container and the command that removes it, and
leaves the run lock held and marked for cleanup, so no run of these tools starts
beside it; once the container is gone, remove the lock file. Every run also
stops and removes any container an earlier run of the same user left behind
before it builds anything, after giving one that is still running a minute to
finish its teardown.

## The verdict

It is pytest's summary line or nothing:

- a run whose output lacks the marker the image writes did not run in the image
  this commit built, and has no result;
- a run with no summary line has no result;
- a run with a skip is not green. The tier's setup fails rather than skips when
  the board is missing, so a skip is a test that did not reach the board, and
  `-ra` puts each skip's reason in the summary;
- a run in which nothing passed is not green.

The exit status is pytest's own when pytest reported, and otherwise one of these:

- 2: this machine cannot run the tier, such as a run off Linux, as root, with no
  runtime or with Podman and no crun;
- 3: the image did not build;
- 4: no result;
- 5: `--no-wait` met a held machine;
- 6: no probe could be handed in;
- 7: a container was left behind;
- 130: interrupted.

## What is withheld

The tier prints the probe's serial number and paths of the machine it runs on,
and the gate's log and artifact can be read by anyone who can read the
repository. Every line the runner prints or logs, and the JUnit report, has the
probe's serial numbers, the machine's host name, the user's home directory and
the user name replaced with `[withheld]`, line by line. No line is removed, so a
failure still shows the line it failed on.

## The gate

`.github/workflows/bench-gate.yml` runs this tool on the self-hosted runner the
board is attached to, for one commit named when it is started, on
`workflow_dispatch` and on no other event:

```
gh workflow run bench-gate.yml -f ref=<commit, branch or tag>
```

The runner script is checked out from the branch the workflow is dispatched
from, the default branch unless `--ref` names another, and the named commit
beside it, so the named commit reaches the machine only as the image built from
it and the tier that image runs. That is what lets the head of a pull request
from a fork be named. For the length of the run the board and its probe are that
commit's to drive, which is the decision a dispatch makes.

The gate shares its concurrency group with `.github/workflows/hardware-bench.yml`,
so the two never hold the board at once and neither cancels the other. A group
keeps one pending run: a second dispatch while one waits takes its place. The report and the log are uploaded as the `bench-tier` artifact on
every path, including a red or cancelled run, and kept for fourteen days.

## What is in the image

- The base image, pinned by digest, with the tag it was resolved from on the
  line beside it.
- From the distribution: OpenOCD, which the tier flashes, resets and debugs the
  board through; `gcc-arm-none-eabi` with `libnewlib-arm-none-eabi`, which build
  the demo and the tier's own images; `gdb-multiarch` for the debug sessions;
  `cmake` and `ninja-build` for the demo's Debug preset; `libusb-1.0-0`; and
  `tini` as process 1, which hands on the stop signal and reaps what the tier's
  commands leave behind. These are unpinned for the reason
  `tools/container/README.md` gives, and the base digest fixes the versions a
  build gets.
- The two locked files the container tier installs, with their hashes, and this
  checkout on top with `--no-deps`.
- `/etc/agentic-hil/bench-test-image`, written by this build and by nothing
  else, which is the marker a verdict needs.

`AGENTIC_HIL_BENCH` is deliberately not set in the image. It is the statement
that a probe and a board are attached, which an image cannot know, and the
runner sets it on the run that hands the devices in.
