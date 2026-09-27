# CAN as a Service: One Bus Owner, Named Participants

Design for #146 and the participant-session completion in #500. A bus without
`shares:` keeps its existing single-owner behavior.

## Problem

A `can_buses` entry models the bus as a device with a single owner:
`can:<adapter>:<channel>` is one exclusive, machine-wide lock. CAN is a shared
medium. Several devices under test on one physical bus is the ordinary case,
and several projects testing against the same bus is the ordinary bench. The
current model serialises all of them against one lock and cannot express
sharing at all.

## Shape

One broker process owns one physical bus. It takes the existing exclusive bus
lock (the same key runs take today) and holds it for its lifetime, so
everything the single-owner model enforces keeps being enforced, by one
process, exactly once. It opens the adapter once.

Participants are named connections to the broker. Each attaches under
`<bus-key>#<name>`, carries its own identifier filter and frame view, and is
selected explicitly by `participant` on session start, stop, send and read.
Each run declares that same participant as a CAN device selector. This keeps a
run's resource key equal to the participant connection it opens, so a run
declaring `ecu_a` cannot reach `ecu_b`. Runs in different projects share the
bus by each holding a participant connection, not by racing for the bus lock.

## Rules

### Locking

- The physical bus lock lives in the broker. A machine where no broker runs
  behaves exactly as today.
- Participants take logical participant locks (`<bus-key>#<name>`), so two
  runs may not share one participant name, while any number of distinctly
  named participants proceed in parallel.
- A shared bus session without a participant name is refused. An unshared bus
  accepts no participant name and keeps its original exclusive lock.
- Operations that change the whole bus (bitrate change, adapter
  reconfiguration, diagnostics that must own the medium) need the bus
  exclusively: they are refused while any other participant is attached.

### Configuration

- The physical bus entry stays what it is. A new `shares:` section under it
  names the participant views (filter, permissions, frame budget). An entry
  without `shares:` keeps single-owner semantics unchanged.
- `listen_only` is a property of the bus, not of a participant: listen-only
  and transmitting participants cannot coexist on one bus, because the
  controller-level proof the flag stands for is per controller, not per
  filter. The configuration names the enforcement level explicitly
  (`listen_only_enforcement: controller | service`); a service-level claim is
  software filtering and is reported as such, never as the controller proof.

### Lifecycle

- The broker starts automatically with the first participant and exits when
  the last one detaches. No daemon management, nothing to install.
- A connection counter is the compatibility contract: it increments on every
  attach, and also on every change to the wire format or participant surface.
  A client whose counter expectation does not match is refused at attach,
  cleanly and with both values named.
- The broker protocol carries a version and a digest of its message surface;
  a client and broker from different releases refuse each other at attach
  instead of misparsing frames mid-run.

### Transport and authentication

- Local IPC only: `AF_UNIX` on POSIX, named pipes on Windows. No network
  listener of any kind.
- Connections authenticate with an HMAC authkey created with the broker; the
  attach handshake additionally proves the broker is the process actually
  holding the bus lock (a lock probe, not a self-description), so a stale or
  impostor endpoint fails the handshake.
- Each connection runs the authkey handshake on its own thread, within a
  bounded time. A peer that fails it, leaves in the middle of it or never
  answers it loses its own connection: the broker keeps serving every
  participant, and a stop still reaches it.

### Incidents

- A physical-bus incident (adapter gone, controller error) belongs to the
  bus: every participant's run aborts into its recovery action, and the
  broker gates bus-wide. The CAN service forwards that scope into the
  participant lease using the existing `can_send_effect_unconfirmed` or
  `can_read_effect_unconfirmed` recovery reason, so the coordinator retains the
  unresolved effect with the run's declared participant resource.
- A participant-scoped failure (its own filter, its own declared step) aborts
  that participant's run only; the bus and the other participants keep
  running. The service reports that failure on the participant session without
  turning it into a bus incident.
- Detaching a participant confirms only that connection's removal. The service
  releases its participant lease without claiming to have reaped the shared
  broker process; the broker closes itself when its last participant leaves.

### Audit

- The broker writes the whole-bus frame log; each participant's report keeps
  its own view beside it. A frame a participant sent is attributable in both.
- After the adapter accepts a participant's send, the broker queues that frame
  for matching, read-enabled peer participants; it excludes the sender. Those
  frames carry `origin: participant_tx` and
  `delivery_status: adapter_accepted`. This confirms adapter acceptance only,
  not physical receipt or an ACK. Frames read from the adapter carry
  `origin: adapter_rx` and `delivery_status: adapter_received`. A driver-marked
  own-transmit echo is logged as an echo and suppressed, since the accepted TX
  was already delivered to peers.

## Phases

1. **Broker with participant views and sessions**: several projects, one bus,
   the rules above. The adapter abstraction underneath is unchanged.
2. **Per-participant stimulus/expect vocabulary in the test reactor**: CAN
   steps address a participant, not the raw bus. Participant steps use plan
   format version 6; older plans remain valid for exclusive buses.
3. **Exclusive-mode operations** (bitrate, diagnostics) behind the
   all-detached gate.

## Out of scope

- Remote or networked bus access. The broker is a local process.
- Bridged adapters change nothing here: a bridge remains one adapter behind
  the broker.
- No scheduling or arbitration beyond the medium's own: the broker does not
  reorder frames.
