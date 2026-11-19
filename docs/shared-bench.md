# Shared bench: one board, several machines

A shared bench is a machine with the hardware plugged into it that takes test
plans from other machines on its network. It runs the bench server,
`agentic-hil serve`, which queues each plan as a job, runs it with the bench's
own configuration and permissions, and answers with the report, the JUnit file
and the evidence bundle. A machine that submits needs no probe, no
configuration and no permission: a plan and the artifacts it names are all a
submission carries, and nothing in one can name a device the plan does not,
grant a permission or change the configuration.

This page is the bench operator's setup, what a submitter does, and what this
release protects and what it does not. The configuration keys are specified in
[Configuration](configuration.md#bench-job-configuration-version-4).

## Setting up the bench server

The bench is configured once, on the machine the hardware is plugged into, in
its authoritative configuration: `version: 4` and a `server` block.

```yaml
version: 4
server:
  job_root: /var/lib/agentic-hil/jobs
  listen: 0.0.0.0:8765
  allowed_networks: [192.168.1.0/24]
  hostnames: [bench.lab]
```

Then, on that machine:

```bash
agentic-hil serve
```

The server runs in the foreground until it is stopped. Without `server.listen`
it listens on 127.0.0.1 alone, so only the bench machine itself reaches it,
which is the way to try it before opening it to anybody. To be reached from
other machines it has to listen on one of the bench's own IPv4 addresses, such
as `192.168.1.20:8765`, or on `0.0.0.0`, which is every IPv4 address of the
machine. `listen` takes an IPv4 address and nothing else: a name would be
resolved to whatever it resolves to at startup, so a name or an IPv6 address is
refused when the server starts.

Three checks decide who is answered, and each reads what the connection itself
says rather than anything a request claims about itself:

- **The peer's network.** `allowed_networks` lists the IPv4 networks, in CIDR
  form, whose connections are answered. A connection from anywhere else is
  closed before a byte of it is read, with no answer at all. Loopback is always
  answered. The default is the three private ranges, `10.0.0.0/8`,
  `172.16.0.0/12` and `192.168.0.0/16`; naming the one network the submitters
  are on is narrower and better.
- **The Host rule.** A request's `Host` header has to name this listener: the
  listen address, or for a `0.0.0.0` listener the address the connection
  arrived on, or one of the names in `hostnames`, with the bound port when it
  carries a port. Anything else is refused as `host_refused`. That is what
  stops a web page that points a name of its own at the bench's address (DNS
  rebinding) from reaching it through a browser on the network. Submitters
  who reach the bench by address need no entry; a name they use instead, such
  as `bench.lab` above, has to be listed in `hostnames`.
- **The browser origin.** A request carrying an `Origin` header outside
  `allowed_origins` is refused before its body is read, and no answer carries
  a CORS header. Empty, the default, means no browser can submit or read.

The `server.limits` block bounds what one submission may take: the upload size,
how long a job waits for its artifacts, `max_job_runtime_s` for one run, how long
a job may wait in the queue, and how many jobs one submitter may have waiting or
running. All server settings are read at startup, so a change to them takes a
restart.

## The queue

Each submission becomes a job in one queue for the whole bench. A job starts
when every device its plan names is free, and the bench's own device locks are
the ones a local run takes, so a run started on the bench machine and a job
never drive a device at once. After `reserve_after_s` in the queue a job's
devices are claimed for it, so younger jobs stop passing it on them. The queue
is open to read: `GET /v1/queue`, or `agentic-hil remote queue`, shows every
waiting job, whose it is, and what it waits for.

Every job carries the name it was submitted under. `agentic-hil remote submit`
sends `--user` when it is given and this machine's login name when it is not;
a request without a `From` header, such as a bare `curl`, is queued as
`anonymous`, and every anonymous job counts against the same per-submitter
limit. The name is self-declared: nothing verifies it, anybody can send any
name, and everyone who can reach the bench's port reads it in the queue, the job
list and the job's report. It is a label for people sharing a bench, not an
identity.

The jobs are kept under `job_root`, so a restart of the server keeps the jobs it
had and their tokens. A job whose worker is still running when the server starts
again is asked to stop rather than adopted, because nothing supervised it in
between.

## Draining and recovering a device

Two requests change the bench rather than read it, and the server answers them
only to a client connected from 127.0.0.1, that is from the bench machine
itself over loopback:

```bash
curl -X POST http://127.0.0.1:8765/v1/devices/dut/drain
curl -X POST -d '{"operator_statement": "Board power-cycled and checked by hand."}' \
  http://127.0.0.1:8765/v1/devices/dut/recover
```

A drain stops new jobs from starting on the device, so it can be taken off the
bench once the job using it ends; jobs already queued for it stay in the queue.
A drain is not kept: it lasts until the server restarts. Recover is the
`agentic-hil recover` of a quarantined device, with the operator's statement
that the bench is in a safe state; add `"accept_config_change": true` when the
configuration changed since the incident.

A forwarding header is something a peer says about itself, so it is never read:
the socket peer is 127.0.0.1 or the request is refused as
`admin_loopback_only`. That has one consequence for the listen address. A server
listening on one LAN address, such as `192.168.1.20:8765`, has no loopback
listener, so drain and recover cannot be reached at all: a request from the
bench machine to that address arrives from that address, not from loopback.
Listen on `0.0.0.0` to keep both, and call them at 127.0.0.1 on the bench.

## Submitting a plan

### With curl

A submission is one `POST /v1/jobs`: either the plan alone as
`application/yaml`, or a multipart form with the plan in a part named `plan`
and each artifact in a part named by the path the plan uses for it.

```bash
curl -H "From: alice" \
  -F plan=@testconfig.yaml \
  -F build/app.elf=@build/app.elf \
  http://192.168.1.20:8765/v1/jobs
```

The answer, `202 Accepted`, carries the `job_id` and the `job_token`. The token
is shown once and the bench keeps only its digest, so keep it: reading the
report, the JUnit file, the evidence bundle and the logs, uploading an artifact
later and withdrawing the job all take it as `Authorization: Bearer`. A job's
status needs no token.

```bash
curl http://192.168.1.20:8765/v1/jobs/<job_id>
curl -H "Authorization: Bearer <job_token>" http://192.168.1.20:8765/v1/jobs/<job_id>/report
curl -H "Authorization: Bearer <job_token>" -o junit.xml http://192.168.1.20:8765/v1/jobs/<job_id>/junit
curl -X DELETE -H "Authorization: Bearer <job_token>" http://192.168.1.20:8765/v1/jobs/<job_id>
```

A status read with `If-None-Match` set to the status's `ETag` and
`Prefer: wait=60` is held until the job changes or the wait ends, and
`GET /v1/jobs/<job_id>/events` streams every change as server-sent events.
A plan submitted alone waits for its artifacts, `PUT` one at a time to
`/v1/jobs/<job_id>/artifacts/<path>`, and expires as `artifact_missing` when
they do not arrive within `max_artifact_wait_s`. `POST /v1/plans/check` checks a
plan against the bench without queueing it.

### With the client

`agentic-hil remote` is the same thing from the command line, with nothing to
configure on the submitting machine:

```bash
export AGENTIC_HIL_BENCH_URL=http://192.168.1.20:8765
agentic-hil remote submit --plan testconfig.yaml --wait --junit-xml junit.xml
```

`submit` reads the plan, sends it with every artifact it names in one
submission, and refuses before sending anything when one of them is missing
(`remote_artifact_missing`). `--bench` names the bench on the command line,
`AGENTIC_HIL_BENCH_URL` when it is not given. Without `--wait` it answers as
soon as the bench has accepted the job. With `--wait` it follows the job through
the queue to its end, prints the run's report and exits with the run's own
verdict, the way a local `agentic-hil test-reactor` does, and `--junit-xml`
writes the run's JUnit file. `--user` names the submitter, as above.

The job's token is kept in `.agentic-hil/remote/<job_id>.json` under the
directory the command ran in, readable by this account alone where the platform
can say so, so `agentic-hil remote withdraw --job <job_id>` and
`agentic-hil remote fetch --job <job_id> --into <directory>` from the same
directory find it. `agentic-hil remote status --job <job_id>` and
`agentic-hil remote queue` need no token. A bench that does not answer is
`bench_unreachable`.

`examples/ci/shared-bench.yml` is a GitHub Actions workflow that builds, submits
with `--wait --junit-xml` and uploads the result; see
[CI examples](ci-examples.md).

### From an MCP host

The bench server also answers MCP over Streamable HTTP at `/mcp`, with the
plan-only tools: check a plan, run it, follow it, read its report, and read the
bench and its queue. [MCP hosts](mcp-hosts.md#the-bench-server-over-http) has
the entry.

## What this release protects, and what it does not

There is no authentication. Anybody who can reach the bench's port can submit
a plan, and can read the queue, the job list, the device inventory and every
submitter name in them. The `job_token` keeps a job's report, logs, evidence and
withdrawal to whoever submitted it, and it is all that does.

What a submission cannot do is reach past the plan. The bench's authoritative
configuration decides which devices exist and what may be done to them, a plan
asking for more is refused at admission as it would be locally, and an upload
is stored as bytes: it is never built, imported or executed by the server.
`max_job_runtime_s` bounds how long one job holds the devices it names: a plan
that could not finish inside it is refused at admission, and a run that passes
it is stopped as `plan_runtime_exceeded`. The per-submitter limit bounds the
queue only as far as names are honest, because a name is self-declared.

The server speaks plain HTTP. Plans, artifacts and tokens cross the network
unencrypted, and anybody on the path can read them or replay a token. Run a
shared bench on a network you trust, and put a firewall in front of it:
`allowed_networks` is the server refusing connections it has already accepted,
not a firewall, and the host firewall is what should keep the port closed to
everything outside the submitters' network.

A CI runner on the bench machine itself needs one more rule. Loopback is always
answered, and drain and recover are answered to loopback alone, so any account
on the bench machine, including a runner executing a pull request's code, could
drain or recover a device. Have such a runner reach the bench by the bench's
LAN address, with a `0.0.0.0` listener, and block its account from the port on
loopback with a per-account firewall rule. On Linux that is the `owner` match,
for a runner account named `runner`:

```bash
iptables -A OUTPUT -d 127.0.0.0/8 -p tcp --dport 8765 -m owner --uid-owner runner -j REJECT
```

The server is the only part of Agentic HIL that listens on the network. The MCP
server an agent starts speaks over its standard input and output, and the CAN
broker never listens on the network.

## Related

- [Configuration](configuration.md#bench-job-configuration-version-4): the
  `server` block and its limits.
- [CI examples](ci-examples.md): the workflows, including the shared bench one.
- [Security design](security-design.md#the-shared-bench): the boundary this page
  describes, as a design.
- [Safety model](safety-model.md): the locks and the quarantine a job runs
  under.
