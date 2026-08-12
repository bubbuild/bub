# Bub end-to-end harness

This directory contains Bub's external end-to-end harness. It defines the ownership boundaries, evidence sources, metrics, and first plugin compatibility workloads. It does not define a second Bub runtime or a new observability protocol.

## Run it

Prerequisites are Docker with Compose and model credentials supported by Bub. Set `BUB_MODEL` and its provider credentials, then run:

```console
make e2e-check
make e2e-run
```

`make e2e-run` runs the `acceptance` category and writes evidence to `.bub-e2e/run`. Select cases or categories with `BUB_E2E_IDS` and `BUB_E2E_CATEGORIES`:

```console
BUB_E2E_IDS=sqlite make e2e-run
BUB_E2E_CATEGORIES=observability make e2e-run
```

Phoenix remains available at `http://localhost:6006` after a run. Stop the services and remove their dedicated volumes with `make e2e-down`.

The Python CLI can validate manifests and rescore an existing run without Docker or model access:

```console
uv run --project e2e bub-e2e check --manifest e2e/cases
uv run --project e2e bub-e2e rescore .bub-e2e/run/sqlite
```

## Goals

The harness should answer three questions:

1. Did Bub complete the workload?
2. How did the agent behave while completing it?
3. Does the same Bub behavior remain valid with supported plugins installed?

Task completion alone is insufficient. Every run must retain enough evidence to explain failures, compare cost and latency, and rescore the run without calling the model again.

## Design principles

### Exercise Bub as an installed product

`bub_e2e` treats Bub as an external system. It must not:

- depend on the `bub` Python package;
- import Bub modules;
- register hooks;
- replace a tape store or other runtime component;
- mutate prompts, state, tools, or the agent loop; or
- interpret in-process Bub objects.

Bub and its plugins are installed through their supported commands. Workloads interact with Bub through its CLI and ACP server.

### Reuse upstream capabilities

The harness delegates each concern to the component that already owns it:

- Harbor owns task resolution, isolated execution, native verifiers, rewards, timeouts, and task artifacts.
- `bub-acp-server` exposes Bub through the standard ACP execution path.
- Bub owns agent execution, tools, skills, hooks, context construction, and tape recording.
- Tape is the canonical Bub execution record used for automated evaluation.
- `bub-tapestore-otel` projects committed tape activity to OpenTelemetry.
- Phoenix receives and displays traces for inspection and debugging.
- `tape-dataset-opendal` may export tape through the supported `bub tape-export` command when a portable artifact is needed.

The harness only installs, runs, collects, evaluates, and reports.

### Keep observation separate from evaluation

Tape and Harbor artifacts contain observations. Evaluators turn those observations into assertions and metrics. A failed assertion must not alter the original evidence.

OpenTelemetry is a projection of the tape, not a replacement for it. Missing telemetry should be reported as an observability failure only in workloads that explicitly require telemetry; it must not make the underlying tape unavailable for evaluation.

## Execution path

Every workload follows one execution path:

```text
case manifest
  -> install the declared Bub distribution
  -> install declared plugins with `bub install`
  -> Harbor Job
  -> Harbor ACP runner
  -> bub-acp-server
  -> Bub
  -> Harbor artifacts + Bub tape + optional Phoenix traces
  -> offline evaluation
  -> JSON and Markdown reports
```

There is no direct Python runner and no harness-specific Bub plugin.

Before running a task, the harness records the resolved Bub and plugin versions and the output of `bub hooks`. This makes plugin discovery and hook precedence part of the run provenance without reaching into the process.

## Evidence sources

### Harbor artifacts

Harbor remains authoritative for:

- the resolved task and checksum;
- the instruction delivered to the ACP agent;
- task start and finish times;
- process exceptions and timeouts;
- native verifier rewards;
- ACP events and trajectory; and
- task workspace artifacts.

### Bub tape

Tape remains authoritative for Bub behavior, including:

- messages and model runs;
- model and tool calls and their results;
- loop steps and terminal status;
- token usage when reported by the provider;
- anchors and handoffs;
- context-overflow recovery;
- plans and model or reasoning-effort changes; and
- errors recorded by the runtime.

The harness obtains tape as an external artifact. The initial implementation should use the supported `bub tape-export` command so that evaluation is independent of the configured tape-store backend. The export plugin is evidence transport; it does not add observation semantics.

### OpenTelemetry and Phoenix

The fixed Compose harness should include Phoenix and configure `bub-tapestore-otel` to send OTLP traces to it. This provides a standard view of agent, step, model, and tool activity and makes failed runs easier to inspect.

Phoenix serves two purposes:

- an interactive trace UI during local and CI diagnosis; and
- an integration target for checking that the OTel tape-store decorator remains compatible with Bub and another active tape store.

Automated task and agent evaluation must still be possible from Harbor artifacts and tape alone. The first observability workload may additionally require that Phoenix received a trace containing an agent span and at least one model or tool child span.

## Case manifests

Case manifests should remain small. Their essential inputs are the Bub revision, Harbor task, plugin revisions, model, and budgets. They declare external inputs and acceptance thresholds, not implementation details of Bub:

```yaml
schema: bub.e2e-case/v1
id: tapestore-sqlite
categories:
  - acceptance
  - tapestore
dataset:
  path: e2e/harbor-tasks
  task_id: tape-continuity
  checksum: <sha256>
agent:
  bub:
    repository: https://github.com/bubbuild/bub.git
    commit: <git-commit>
  plugins:
    - name: bub-acp-server
      commit: <bub-contrib-commit>
    - name: bub-tapestore-sqlite
      commit: <bub-contrib-commit>
    - name: tape-dataset-opendal
      commit: <bub-contrib-commit>
  budgets:
    max_agent_steps: 20
    max_total_tokens: 50000
    max_tokens_per_call: 8192
    timeout_seconds: 900
evaluation:
  required_reward: 1
  minimum_turns: 2
  minimum_tool_pairs: 2
```

`agent.bub` accepts exactly one of `version` or `commit`. A version installs from the configured package index. A commit installs from `repository` at that immutable revision. Each plugin accepts exactly one of `version`, `commit`, or `spec`:

- `version` installs the published package version;
- `commit` maps to the supported contrib form `bub install <name>@<commit>`; and
- `spec` passes an explicit supported Bub plugin spec, for example a third-party repository reference.

The harness records the resolved distribution metadata after installation. Branch names such as `main` may be useful for local experiments but should not be used by acceptance cases.

Secrets, provider credentials, endpoint configuration, and output locations remain environment settings rather than manifest data.

## Evaluation model

The first evaluator should calculate a small, stable set of results.

### Assertions

Assertions gate acceptance:

- the declared task checksum matches Harbor's resolved task;
- required Harbor and ACP artifacts exist;
- Bub and every declared plugin installed successfully;
- expected hook implementations appear in `bub hooks`;
- the run completed without an unhandled agent exception;
- the native verifier met the declared reward;
- the tape export exists and can be parsed;
- the tape contains a terminal record for the run; and
- declared step, token, and time budgets were not exceeded.

Plugin cases add only assertions specific to the public behavior of that plugin.

### Metrics

Metrics are reported but do not all gate acceptance:

- wall-clock duration;
- agent steps;
- model calls;
- tool calls and tool errors;
- input, output, total, and cached tokens when available;
- anchors and handoffs;
- tape entries and exported segments;
- Harbor rewards; and
- OTel trace and span counts when telemetry is enabled.

Missing provider usage is reported as unavailable, not as zero.

### Acceptance

The initial acceptance rule is deliberately direct:

```text
accepted =
  provenance matches
  AND required evidence exists
  AND native verifier passes
  AND Bub runtime assertions pass
  AND declared budgets pass
  AND plugin-specific assertions pass
```

There is no aggregate quality score in the first version. An optional LLM judge can be added later for workloads whose outcome cannot be verified deterministically.

## Initial plugin compatibility coverage

The harness should begin with tape-store plugins because they affect Bub's canonical execution record. Each case installs plugins through `bub install` and runs the same `tape-continuity` Harbor workload.

### Tape continuity workload

The workload should exercise only public Bub behavior:

1. Run an ordinary multi-step task that invokes at least one tool.
2. Continue in the same ACP session and use information from the earlier turn.
3. Create or exercise a normal tape anchor or handoff.
4. Run `bub tape-export` after the agent finishes.
5. Verify the task workspace independently through Harbor.

The evaluator checks that the exported tape contains the expected conversation, tool call/result pairing, completed run records, and anchor-derived segment. It compares semantic invariants rather than backend-specific files or identifiers.

### First matrix

| Case | Installed tape-store path | Additional service | Purpose |
| --- | --- | --- | --- |
| `builtin` | Bub builtin | none | Reference behavior |
| `sqlite` | `bub-tapestore-sqlite` | none | SQLite backend compatibility |
| `sqlalchemy-sqlite` | `bub-tapestore-sqlalchemy` | none | SQLAlchemy adapter compatibility |
| `redis` | `bub-tapestore-redis` | Redis | Remote asynchronous backend compatibility |
| `otel-builtin` | builtin wrapped by `bub-tapestore-otel` | Phoenix | OTel decorator and trace ingestion |
| `otel-sqlite` | SQLite wrapped by `bub-tapestore-otel` | Phoenix | Decorator composition with a contributed backend |

This is not a full cross-product. One reference backend and one contributed backend are sufficient to establish decorator composition initially.

Backend cases should share the same Harbor task and evaluator. A backend-specific evaluator is a sign that the test is depending on private storage details.

### Compatibility assertions

For every tape-store case:

- plugin installation and discovery succeed;
- the same task verifier passes;
- all expected turns appear after export;
- model tool calls have corresponding tool results;
- a terminal run record exists;
- anchor segmentation is valid; and
- no evidence is silently lost compared with the builtin reference invariants.

For OTel cases:

- Bub completes even if trace export is best-effort;
- Phoenix becomes ready before the task starts;
- at least one trace for the tested session is discoverable; and
- agent, model, and tool or step span projection is present as expected for the workload.

Persistence across a complete Bub process restart is valuable, but it should be a separate follow-up workload. It should not complicate the first continuity test.

## Output layout

The harness should preserve upstream artifacts rather than copying them into a new event schema:

```text
<output>/<case-id>/
  run.json
  eval-report.json
  report.md
  harbor-jobs/        # Harbor, ACP, task workspace, and exported Tape artifacts
  phoenix/            # only when telemetry is enabled
```

`run.json` records resolved versions, checksums, environment labels, timestamps, and artifact fingerprints. `eval-report.json` contains assertions and metrics derived from the other directories. Offline rescoring reads the same directory and rewrites only the reports.

Known secrets must be redacted from final reports. Native Harbor, tape, and trace artifacts can contain task or tool content and must be reviewed before publication.

## Layout

```text
e2e/
  README.md
  pyproject.toml
  compose.yaml
  cases/
  harbor-tasks/
  src/bub_e2e/
    __main__.py
    models.py
    runner.py
    artifacts.py
    evaluate.py
    report.py
  tests/
```

`bub_e2e` depends on Harbor and ordinary data-processing libraries only. It invokes `bub`, `bub install`, and `bub tape-export` as external commands.

## Extension stages

### Reproducible execution

The case model, installation contract, Harbor runner, artifact layout, offline rescore command, and builtin file-store acceptance case form the base harness.

### Tape-store compatibility

The same workload and evaluator run against SQLite, SQLAlchemy-backed SQLite, and Redis. Backend-specific code is limited to environment and service configuration.

### Observability composition

`bub-tapestore-otel`, Phoenix, and two decorator-composition cases keep Tape as the evaluation source and use Phoenix to verify and inspect the projection.

### Agent behavior coverage

Add context-overflow recovery, handoff, steering, and selected plugin combinations after the tape-store contract is stable. Each new test must express user-visible behavior or preserve a demonstrated regression.

## Test policy

Harness acceptance cases are behavior tests: they run the installed product through its public interfaces and verify task outcomes and durable evidence. Focused regression tests are appropriate after a real failure exposes a reusable boundary such as an ambiguous revision, corrupted artifact, or incomplete tape.

Do not add tests that pin helper calls, command assembly internals, private Harbor objects, backend-specific rows, buffer sizes, or exhaustive translations of dependency errors. Small scripts and deterministic transformations should remain readable enough to audit from top to bottom. When their implementation changes without changing user-visible behavior, tests should not need to change.

## Non-goals

The first harness does not:

- replace Harbor's task or verifier model;
- implement another ACP client or Bub runner;
- define a new runtime event protocol;
- instrument Bub with harness hooks;
- require OTel for normal evaluation;
- test real third-party messaging credentials;
- build a full plugin cross-product; or
- reduce all agent behavior to one score.
