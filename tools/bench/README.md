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
python3 tools/bench_in_container.py --without-device-group
python3 tools/bench_in_container.py --runtime podman --live-device-tree -- tests/bench/usb_reset_reenumeration.py
python3 tools/bench_in_container.py --distribution debian-12
python3 tools/bench_in_container.py --build-only         # the image alone, anywhere
python3 tools/bench_in_container.py --cubeprogrammer-archive ~/.cache/agentic-hil/toolchains/cubeprogrammer-2.23.0.zip --build-only
```

The licensed STM32CubeProgrammer layer is opt-in. Its archive stays outside the
checkout in the runner cache at
`~/.cache/agentic-hil/toolchains/cubeprogrammer-2.23.0.zip`; the runner obtains
it through authenticated access to the unpublished release asset. The helper
checks the supplied archive against SHA-256
`6a9e60a5a048c45eb3241f9bb66bdc2e6cbd0119fb2e42568dc059fc6167442a` before
copying it into the temporary build context, which is removed when the command
finishes. The binary is never committed or published as a public release.

With `--cubeprogrammer-archive`, the build selects the separate
`bench-tier-cubeprogrammer` stage. It installs CubeProgrammer 2.23.0 at
`/opt/st/cubeprogrammer-2.23.0` and runs the dedicated USB-free recording smoke
test during the build. The CLI is not added to `PATH`, so an ordinary `init`
continues to discover OpenOCD by default; a test that needs CubeProgrammer names
the installed executable explicitly. The installer log reported all three
packages installed, including TrustedPackageCreator, even though the unattended
XML marked that pack unselected.

A run finds the probe through sysfs by its USB vendor and product ids, never by
a serial number; takes this machine's run lock; builds the image; runs the tier
with the probe's nodes handed in; copies the JUnit report out; and reads pytest's
summary line as the verdict.

The USB reset re-enumeration stage, pyOCD discovery diagnostic, and opt-in
incident recovery check are the exceptions to static device-node mounts. They
run with
`--runtime podman --live-device-tree`: the rootless
container receives the host `/dev` directory as a read-only bind mount, so a
kernel-recreated tty node and updated `/dev/serial/by-id` links can be resolved
by the same MCP server after USB reset or during the recovery check's reset and
probe. The first test in the pyOCD stage
records native probe-listing evidence, requests one identity-checked USB reset,
then requires the same MCP process to list the configured probe again; that
diagnostic does not connect to or alter the target. The existing healthy pyOCD
baseline runs next and still connects, flashes and verifies the demo boot over
UART. This mode does not add per-node
`--device` mounts or stage by-id links, and Docker is refused before build or
run because its device-cgroup policy for newly registered nodes is unsupported.
The read-only bind protects directory entries from container changes; it does
not make character devices read-only. Processes in the container may perform
device I/O that the invoking user is permitted to perform, and can see other
host `/dev` entries that this user may access. The gate uses this broader view
only for explicitly selected pyOCD, USB reset, or incident recovery checks.
Network remains disabled, capabilities remain dropped, and the existing
non-root and crun checks still
apply.

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
- Anything else that drives the board from the machine itself takes the same
  lock with `tools/run_lock.py`: `python3 tools/run_lock.py run -- <command>`
  holds it for one command's life, queued like these tools, and exits with the
  command's status, and `take` and `give-back` hold it across a workflow's
  steps, as the nightly does. A run that holds the board without it meets the
  tier as `device_busy` in the middle of what it was doing.
- `--distribution` builds the image on another distribution, with that
  distribution's OpenOCD, compilers and Python; see "Other distributions"
  below.
- `--usb-device` and `--serial-device` name the probe's nodes instead of
  finding them. They go together, and each can be given more than once.
- `--without-device-group` runs the one stage the tier cannot hold,
  `tests/bench/test_bench_without_device_group.py`, and nothing else: the
  container gets the probe's nodes and none of the groups they are opened
  through, the way a Linux account meets a probe it has not been given the group
  of, and the product has to say the probe is attached and this user may not
  open it: `doctor` fails its device-access check for the probe and the port,
  naming the group, and `init` binds both and warns in the same words. It
  relies on the nodes being opened through a group, as above; a node the
  container could still open fails the stage by name. Every other run leaves
  the stage out, as deselected rather than skipped.

In ordinary stages, the container gets only the probe's device nodes, the
machine's device-lock directory, the serial port's `/dev/serial/by-id` links
read only, and the directory the report is written to. The USB live-device-tree
stage instead gets the host `/dev` directory read only, as described above. The
container has no network or capabilities, and has a process table and host name
of its own. A run as root is refused, because root's device locks are not the
ones the board's user takes.

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

The optional hardware stages are off by default. Enable the status-gated reset
preflight with `run_recovery_check`, and recordings independently with
`run_pyocd_recordings`, `run_cubeprogrammer_recordings`, or
`run_usb_reset_reenumeration`:

```
gh workflow run bench-gate.yml -f ref=<commit, branch or tag> -f run_pyocd_recordings=true -f run_cubeprogrammer_recordings=true -f run_usb_reset_reenumeration=true
```

When selected, the recovery check runs before the standard tier in the default
image. It reads lease status first and refuses a failed audit or any standing
incident; otherwise it declares the debugger, requests reset into halt, and
probes the target through MCP. A failed check stops the standard gates. The
withheld-device-group diagnostic still runs after an ordinary red standard
tier, but only when the selected recovery check succeeded.

The ordinary tier and the stage without the probe's device group run first.
Then the requested recordings run in this order, each in its own container
invocation and only while all earlier stages have succeeded. Every workflow
attempt writes under `bench-results/<run-id>-<attempt>/`, and the upload reads
only that directory. This keeps a skipped optional stage from publishing a
report left by an earlier run in the persistent self-hosted workspace. The
pyOCD stage uses the ordinary bench image and writes to the attempt's `pyocd`
subdirectory; it does not need the optional CubeProgrammer archive. The
CubeProgrammer stage runs its probe, flash, reset and capture recordings and
writes to the attempt's `cubeprogrammer` subdirectory. It uses the pinned
archive already in the runner's user-local cache; the workflow does not install
the host toolchain. Its temporary ST-Link description sets `connect_mode` to
`under_reset` for flash; the Nucleo-F446RE's on-board ST-Link reset line is wired
to NRST. The build-time smoke check is not evidence of a hardware recording run.

The USB stage requests targeted `USBDEVFS_RESET` on the verified ST-Link node
inside the existing unprivileged container, through the same MCP server and
bench run. It uses no `sudo` or privileged container. A reset request does not
guarantee a physical USB disconnect. The report records whether enumeration
changes were observed; it is not proof of a physical disconnect or of UART
reopening. Its report is under the attempt's `usb-reset` subdirectory.

The runner script is checked out from the branch the workflow is dispatched
from, the default branch unless `--ref` names another, and the named commit
beside it. The runner receives that candidate checkout explicitly with
`--source` and verifies its full HEAD SHA with `--expected-commit` before it
checks the board or builds. The named commit reaches the machine only as the
image built from it and the tier that image runs. That is what lets the head of
a pull request from a fork be named. For the length of the run the board and its
probe are that commit's to drive, which is the decision a dispatch makes.

The gate runs the tier, and then `--without-device-group` on the image the
tier's run built: after a red tier too, because what the stage proves does not
depend on what the tier found, and never after a cancelled run. The opt-in
pyOCD, CubeProgrammer and USB reset recordings each get their own invocation
and output directory after the standard stages; they run only when the earlier
gates succeeded. No hardware result for an opt-in stage is implied by the
container build or its smoke test.

The gate shares its concurrency group with `.github/workflows/hardware-bench.yml`,
so the two never hold the board at once and neither cancels the other. A group
keeps one pending run: a second dispatch while one waits takes its place. The report and the log are uploaded as the `bench-tier` artifact on
every path, including a red or cancelled run, and kept for fourteen days; the
stage's are in its `without-device-group` directory inside it.

## The nightly

`.github/workflows/hardware-bench.yml` runs every night on the same runner. Its
first job drives the release installed on the machine itself through the demo:
doctor, the build, the demo's plan and the pytest plugin. It takes the machine's
run lock with `tools/run_lock.py take` before doctor, queued behind whoever holds
it, for the life of the job's process, and gives it back with `give-back` after
the pytest plugin's run whatever happened, so a tier started on the machine
meanwhile waits for the job instead of meeting it on the board.

Its second job runs this tool once for every distribution `--distribution`
offers, one after another, after the first job whatever it found: each run
queues on the lock itself, as the gate's does, and each distribution's report
and log are uploaded as `bench-tier-<distribution>`, whatever the run did, and
kept for fourteen days.

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

The image installs the STM32F4 CMSIS pack with pyOCD's supported
`pyocd pack install stm32f446retx` command. The pack is stored under
`/bench-home/.local/share/cmsis-pack-manager`, matching the image's runtime
`HOME`. This installer resolves the current vendor index at build time; an
offline image check requires the reviewed `Keil.STM32F4xx_DFP` version 3.1.1
and verifies that both `stm32f446re` and `stm32f446retx` are listed from a
pack. That check reads software metadata only and opens no probe. A change in
the vendor index therefore requires reviewing and updating the expected
version; the install itself is not a hash-pinned pack download.

`AGENTIC_HIL_BENCH` is deliberately not set in the image. It is the statement
that a probe and a board are attached, which an image cannot know, and the
runner sets it on the run that hands the devices in.

## Other distributions

`--distribution` builds the tier's image on another distribution than the
default image's: `ubuntu-22.04`, `ubuntu-24.04`, `debian-12` or `fedora-44`.
Everything else about a run is the same, the lock, the devices, the verdict and
the output included.

Each has a head of its own, `tools/bench/distributions/<distribution>.Dockerfile`:
the distribution's base image, pinned by digest with the tag it was resolved
from beside it, as `tools/bench/Dockerfile` pins its own, and one install of the
packages the default image installs, under that distribution's names, with its
Python. The runner builds the head followed by `tools/bench/Dockerfile` from its
first `WORKDIR` on, so the checkout, its locked dependencies, the marker, the
stop signal and the entry point are the default image's own, and what differs
is what the distribution packages: its OpenOCD, its cross compiler and C
library, its GDB, its CMake and its Python. Such an image is tagged
`agentic-hil-bench-tier:<distribution>`; without `--distribution`,
`tools/bench/Dockerfile` is built as it stands. Whatever every image needs goes
into `tools/bench/Dockerfile` after that first `WORKDIR`, and the file keeps a
single stage, as a test holds it to, since the composition knows of one: a
second would be missing from the composed file, or would put the default base
image in the distribution's place.

A head also names its distribution in `/etc/agentic-hil/bench-distribution`,
which the default image does not write. The tests marked `wheelhouse` install
the product for a clean account through the quick start's
`python -m pip install --user`, run from that account's login shell. On an image
that names its distribution, where that shell's `python` has no pip or is marked
externally managed, they are deselected, since a skip fails the tier, and the
run says so in one line naming the distribution and what that `python` lacks.
The default image names none, so they always run there, and fail there if they
cannot.

What the distributions packaged when the tier was run on each of them, on
2026-09-27 and 2026-09-28:

| Distribution | OpenOCD | GCC for Arm | GDB | CMake | Python |
| --- | --- | --- | --- | --- | --- |
| `ubuntu-22.04` | 0.11.0 | 10.3-2021.07 | 12.1 | 3.22.1 | 3.10.12 |
| `ubuntu-24.04` | 0.12.0 | 13.2.rel1 | 15.1 | 3.28.3 | 3.12.3 |
| `debian-12` | 0.12.0 | 12.2.rel1 | 13.1 | 3.25.1 | 3.11.2 |
| `fedora-44` | 0.12.0, snapshot cb52502 | 15.2.0 | 17.2 | 4.3.0 | 3.14.7 |

The whole tier passed on `ubuntu-24.04`, `debian-12` and `fedora-44`. From the
start of the build to the verdict, each image built afresh, the runs took
12m42s, 11m43s and 19m26s: on Fedora 44 the tier itself took 17 minutes, where
it took 10 to 11 on the others. The nightly's limits are set from these runs.

OpenOCD 0.11, Ubuntu 22.04's, has no `adapter serial`, which is how the OpenOCD
backend selects the probe when a configuration binds it by serial number, as
the tier's does. It refuses the command before it opens the probe, with
`invalid subcommand "serial <serial>"`, and the product reports that as
`debugger_command_rejected` naming `adapter serial`: the board was not touched
and stays in service. Every test that reaches the board through OpenOCD fails
there on that refusal, so the tier on `ubuntu-22.04` is red.
