- Proposal Name: `standard_driven_bub`
- Start Date: 2026-08-29
- Target Release: 0.5.0
- RFC PR: TBD
- Tracking Issue: TBD

# Summary

Bub should provide a coherent, standard-driven default for the common parts of
building and operating agents. Every Bub event is a CloudEvents 1.0 event and
the tape is the only durable execution history. Sidecars derive views and
effects from committed tape records without creating parallel histories.

The first profile covers:

| Concern | Direction | Bub contract |
| --- | --- | --- |
| Event model | CloudEvents 1.0 | Bub events are official `CloudEvent` objects |
| Agent trajectories | ATIF | CloudEvent `data` is an ATIF StepObject |
| Observability | OpenTelemetry GenAI | Official GenAI instrumentation utility |
| Persistence | Format-neutral | `TapeRecord` positions plus an explicit event codec |

This work targets Bub 0.5.0. The release may make breaking changes where the
0.4.x interfaces would otherwise distort the design.

# Motivation

Bub already records agent work, but its current contracts combine concerns that
need to evolve independently:

- `TapeStore.fetch_all()` requires callers to materialize a complete result;
- synchronous and asynchronous store protocols describe the same capability
  twice and require an adapter in the builtin agent;
- `FileTapeStore` owns both filesystem persistence and the JSONL record shape;
- `Tape` writes JSONL archives itself;
- entry identity, store position, session identity, and model-call identity are
  not clearly separated;
- telemetry is configured through Logfire at CLI startup but Bub does not emit
  an explicit OpenTelemetry GenAI operation model.

Without a shared default, integrations cannot depend on stable semantics and
each deployment must rebuild trajectory, event, and telemetry adapters.

# Design principles

## One Bub authority

The tape is Bub's durable record and CloudEvents is its event envelope. The
CloudEvent `data` is already an ATIF StepObject, so ATIF is not exported or
projected into a second representation. OpenTelemetry observes the same
committed events. None of these creates another authority, and configuration
such as exporters, endpoints, credentials, sampling, and redaction does not
belong in tape records.

## Standards own common mechanics

Bub does not define substitutes for standard streaming, log exchange,
observability, or agent-trajectory semantics. It uses Python's
`AsyncIterator`, the official CloudEvents SDK, the official OpenTelemetry GenAI
instrumentation utility, and the published ATIF schema directly.

Bub owns only its business vocabulary: conversations and turns, tape identity
and position, handoff anchors, forks, sidecars, and the correlation
between those concepts and standard operations. Namespaced extensions are
limited to that Bub-specific information. They must not reproduce a standard
field under a Bub name.

This rule also applies to implementation. Bub does not hard-code a parallel
OpenTelemetry attribute vocabulary, wrap CloudEvents in a second Bub event
model, or publish a custom streaming protocol. The official CloudEvents object
is stored directly; adapters delegate telemetry lifecycle and serialization to
the standard implementation whenever one exists.

## Persist before publish

A record becomes visible on the committed tape stream only after its store
append succeeds. Observers may fail or lag without rolling back the tape. A
cursor allows a consumer to resume from durable state rather than treating a
notification as the source of truth.

## Explicit layers, one sidecar hook

The implementation separates three responsibilities:

1. `TapeStore` stores and scans semantic records.
2. `TapeCodec` provides a lossless representation for a concrete record store.
3. `provide_tape_sidecar` mounts every operational capability beside tape.

The existing sidecar hook is the only extension path for the third layer. Its
implementations conform to capability specs rather than creating parallel
hooks: `SiblingTapeSidecar` owns sibling tapes, `TapeOverlaySidecar` creates an
isolated store, and `CommittedTapeSidecar` reacts only after a fact commits.
Each hook implementation returns exactly one named `TapeSidecar`; pluggy's
normal multi-call behavior collects multiple implementations.

CloudEvents and ATIF need no sidecar because they are the native persisted event
shape. Spill, fork overlay, and OpenTelemetry are builtin implementations
mounted through the same sidecar hook; none receives a private registration
channel in `Tape`.

# Core vocabulary

The 0.5.0 model distinguishes:

- `session_id`: stable logical conversation identity;
- `turn_id`: one framework inbound-to-outbound turn;
- `invocation_id`: one agent execution;
- `model_call_id`: one model request;
- `tool_call_id`: one tool execution;
- CloudEvents `id`: stable identity of one occurrence;
- `cursor`: store-assigned position within one tape.

A `CloudEvent` keeps its `id` when copied or merged into another store. Its
`TapeRecord.cursor` may change because a cursor describes storage order, not
event identity.

# Tape and store contract

The public store is asynchronous and streaming:

```python
class TapeStore(Protocol):
    async def append(self, tape: str, event: CloudEvent) -> TapeRecord: ...
    def scan(self, query: TapeQuery) -> AsyncIterator[TapeRecord]: ...
    async def list_tapes(self) -> list[str]: ...
    async def reset(self, tape: str) -> None: ...
```

`scan()` is a finite, lazy read expressed as the standard Python
`AsyncIterator` protocol. `Tape.stream(..., follow=True)` follows committed
records and resumes scans from the last durable cursor. Its in-process wake-up
mechanism is private and does not introduce a public stream abstraction or an
alternative event-log protocol.

The 0.4.x synchronous protocol, `AsyncTapeStoreAdapter`, and `fetch_all()` are
removed rather than retained as permanent dual paths.

# Persistence format

The builtin composition passes an event codec into the file store; the store
does not import or choose a concrete JSON schema. Bub's one supported codec
writes one official structured CloudEvent per JSONL line. No Bub record envelope is written: the line order supplies
`TapeRecord.cursor`, and the committed ATIF `step_id` is the same position.
Archive code uses the same explicit codec and does not live in `Tape`.

The 0.5 runtime has no legacy reader, field aliases, sentinel identifiers, or
alternate append path for 0.4 JSONL. Breaking the old format keeps one
authoritative representation in the runtime.

# Standard event model

## CloudEvents

Bub business events are CloudEvents 1.0 objects. They use the reverse-DNS-style
`build.bub.<name>.v<version>` type namespace. Bub owns its business event types,
source, data schemas, and Bub correlation extensions; the official CloudEvents
Python SDK owns the event model and structured JSON serialization. There is no
`CloudEventsProjector` and no `cloudevents` sidecar. Incompatible Bub event data
changes require a new Bub event type or data-schema version.

## ATIF

Every CloudEvent `data` value is an ATIF StepObject. User, system, and agent
messages use ATIF `source` and `message`; a model response, its tool calls, tool
observations, model name, and metrics form one agent step. Operational facts use
system steps and observations. Bub-specific correlation stays in CloudEvents
extension attributes. Handoff and context-anchor state is one system observation,
not a second handoff event.

The store assigns `step_id` when it commits the event. Consequently the tape
stream itself is the trajectory and no `AtifProjector`, export API, or duplicate
trajectory document exists.

```text
Tape JSONL line
└── structured CloudEvent
    ├── id / source / type / time
    ├── bub* correlation extensions
    └── data: ATIF StepObject
        ├── step_id / source / message
        ├── tool_calls / observation
        └── model_name / metrics / extra
```

## OpenTelemetry GenAI

The OpenTelemetry sidecar observes committed agent invocation, model-call,
and tool-execution tape events, then uses `opentelemetry-util-genai` to emit
telemetry. The utility owns span lifecycle, semantic-convention attributes,
content capture, and span construction; Bub does not mirror these definitions.
Bub adds only namespaced correlation fields for Bub-owned identities when the
standard has no equivalent.

Content capture uses the standard
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` setting. SDKs, exporters,
Logfire, endpoints, credentials, sampling, and content policy remain deployment
choices rather than tape data or Bub-specific telemetry settings.

# Lifecycle and cancellation

Agent invocations, model calls, and tool executions write start and terminal
events to tape for durable self-diagnosis. Every start settles as completed,
failed, or cancelled, including when a consumer closes an async stream early.
These entries are committed runtime facts, not a second telemetry schema. Only
after commit does the OpenTelemetry sidecar start or settle the corresponding
official GenAI operation, whose attributes and span representation remain owned
by the instrumentation utility. An observer failure cannot roll back or hide
the committed tape entry.

Generic runtime events carry `context=false` at construction, so even a custom
context selector does not project them into the next model prompt. Only message
and handoff-anchor constructors produce context-visible facts by default. Runtime
facts remain available to tape search,
streaming consumers, direct CloudEvents serialization, and diagnostics. Streaming model
chunks remain ephemeral by default; the tape does not persist every token
delta.

# Alternatives rejected

Embedding a cursor as a CloudEvents extension confuses event identity with store
position; `TapeRecord` keeps those concerns separate while ATIF `step_id`
expresses trajectory order inside the standard data object. A separate ATIF
export creates a redundant history and makes users understand an avoidable
mapping. Treating OpenTelemetry spans as the record loses replay semantics and
makes sampling part of correctness.

Keeping every integration in plugins preserves a smaller core but leaves Bub
without a dependable execution meaning. The selected boundary keeps transports
and exporters optional while putting schemas, mappings, and conformance in the
supported Bub profile.

# Acceptance criteria

- Historical tape scans have bounded memory use; ATIF steps stream directly.
- Committed streams do not publish before persistence and can resume by cursor.
- Store implementations do not choose a serialization format through the
  `TapeStore` protocol.
- The CloudEvents JSON codec round-trips an official structured event directly;
  file stores recover cursors from line order.
- Bub events use the official CloudEvents SDK directly; CloudEvents and ATIF
  behavior has versioned conformance tests.
- OpenTelemetry integration uses the official GenAI utility for success,
  failure, timeout, cancellation, model usage, and tool execution without a
  Bub-owned semantic-convention layer.
- OpenTelemetry receives only committed tape entries, and observer failure
  cannot change tape state or block committed-stream visibility.
- Agent, model, and tool starts have exactly one durable terminal tape event,
  and runtime events are excluded before all prompt-context selection.
- Public streaming APIs use Python async iteration; live tape wake-ups remain
  an implementation detail.
- Sibling storage, fork overlay, and OpenTelemetry are sidecar spec
  implementations contributed only through `provide_tape_sidecar`.
- Session, turn, invocation, model-call, tool-call, event, and cursor identities
  remain distinct in code and documentation.
- Ruff, mypy, pytest, and the documentation build pass.

# Future possibilities

The same committed tape feed can support database stores, remote event buses,
shared viewers, evaluations, training pipelines, and deployment presets. New
standards must satisfy the same adoption criteria and provide an explicit,
versioned mapping rather than becoming an implicit replacement for Bub's
native model.
