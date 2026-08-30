- Proposal Name: `standard_driven_bub`
- Start Date: 2026-08-29
- RFC PR: TBD
- Tracking Issue: TBD

# Summary

Bub should provide a coherent, standard-driven default for the common parts of
building and operating agents. When the ecosystem has an established solution,
Bub should adopt it, document one expected behavior, and support it as a
first-class capability.

Defining a private format or exposing unrestricted extension points is easy.
The harder and more valuable commitment is to preserve Bub's flexibility while
offering first-class support for open standards. Those standards may not
describe every Bub concept or form a complete agent stack, but they provide the
shared boundaries needed to work with the wider ecosystem.

Bub remains a framework for custom agents. Its main innovations are its
architecture and expression model; users should spend their customization
budget there rather than rebuilding event formats, trajectory adapters, or
observability integrations.

# Motivation

Bub already works, but its flexibility leaves many routine decisions to each
deployment. Plugins, permissive data structures, and local conventions can
represent the same concept in different ways. This is convenient inside one
project and easy for a framework to provide, but it transfers the cost of
interoperability to every user.

The cost is larger than the code for an adapter. Without a shared default:

- documentation cannot describe one dependable path;
- integrations cannot assume consistent semantics;
- executions are harder to compare, replay, inspect, and exchange;
- improvements made by one deployment do not naturally benefit another.

Many of these concerns are no longer research questions. Event exchange,
agent trajectories, tool protocols, client protocols, and telemetry already
have useful specifications and implementations. Bub gains more by integrating
them well than by creating parallel conventions.

Standards therefore serve two purposes: they reduce immediate integration
work, and they give Bub a deterministic public baseline. Custom behavior can
then be expressed as a deliberate extension instead of an undocumented local
replacement.

No single standard needs to define a complete agent system to be useful. For
people researching agents or building customized solutions, first-class support
provides stable points for exchanging data, comparing results, and reusing
tools. It reduces the mismatch between Bub's internal model and the ecosystem
without requiring either side to abandon its own abstractions.

# Guide-level explanation

## A strong default, not a closed system

The default Bub experience should be usable with standard ecosystem tools
without requiring a project-specific adapter. A user should be able to inspect
an execution, export telemetry, exchange a trajectory, or attach a compatible
client through documented behavior.

The initial areas include:

| Concern | Standard direction | User value |
| --- | --- | --- |
| Events | [CloudEvents] | Common event exchange and routing |
| Agent trajectories | [ATIF] | Portable inspection, evaluation, and training data |
| Observability | [OpenTelemetry GenAI] | Existing telemetry tools and conventions |
| Tool and client connections | MCP, ACP | Compatible integrations where needed |

This table describes direction, not a requirement that every installation run
every integration. A chat deployment may not need ACP; a Lody deployment does.
Both should still agree on the meaning of a Bub execution.

## Built-in contracts, optional surfaces

A capability belongs in builtin when it defines common Bub semantics, is useful
across deployments, and can be supported without a deployment-specific choice.
Schemas, mappings, validation, and stable extension points are examples.

A capability remains a plugin when it selects a channel, transport, vendor,
credential, or optional runtime. ACP, MCP, chat channels, and concrete telemetry
exporters may therefore remain optional even when Bub provides first-class
support for their standards.

An installer may offer presets and let users toggle plugins. Presets make a
deployment convenient; they do not create different Bub dialects.

## Customization above the baseline

Plugins and hooks may extend or replace behavior where a use case requires it.
Extensions should be explicit, namespaced, and preservable by consumers that do
not understand them. Standard behavior remains available as the reference path
and the basis for documentation and conformance tests.

# Reference-level explanation

## Standard adoption contract

Bub should adopt a standard when it:

- represents a common concern rather than a Bub-specific innovation;
- has useful ecosystem support or a credible interoperability path;
- can preserve Bub-specific extensions;
- can be versioned and tested without silently changing existing behavior;
- removes more user-owned integration work than it adds to Bub core.

Adoption means more than accepting a compatible payload. Bub owns a documented
mapping, versioning policy, conformance tests, and a high-quality default. An
upstream change must not silently alter an existing Bub execution.

Standards that meet these criteria may define builtin semantics. Transports and
product-specific adapters remain independently installable. Areas without a
good standard keep their Bub-native contract until a better option exists.

## Tape as the first reference case

Tape demonstrates how this policy applies; it is not the whole proposal.

The tape remains Bub's durable record. Common operational facts can use a
CloudEvents-compatible envelope. The same facts can produce an ATIF trajectory
and OpenTelemetry GenAI data through deterministic projections:

```text
durable tape
   |------> context construction
   |------> ATIF trajectory
   `------> OpenTelemetry GenAI
```

These projections do not become competing histories. Export configuration such
as endpoints, credentials, sampling, and redaction stays outside core tape
records. Bub-specific facts use versioned extensions rather than forcing every
concept into an ill-fitting standard field.

This case establishes the pattern for later work: one Bub authority, one
documented standard mapping, optional integrations, and explicit extensions.
Each additional area should justify its own mapping rather than inheriting a
blanket requirement from this RFC.

# Drawbacks

Standards evolve, sometimes overlap, and do not express every Bub concept.
First-class support creates compatibility and conformance work in core. Poorly
chosen standards could also constrain Bub without delivering real adoption.

The adoption criteria and explicit extension model limit that risk. Bub should
standardize selectively, not automatically.

# Rationale and alternatives

Keeping all integrations in plugins preserves a smaller core but leaves users
with inconsistent semantics and duplicated adapters. Defining only Bub-native
formats is simpler and gives maximum local control, but makes that simplicity a
cost paid by every consumer and isolates Bub from tools users already have.

The proposed boundary keeps Bub-native architecture where Bub adds value and
uses standards where uniqueness creates friction rather than advantage.

# Unresolved questions

- Which standards form the first supported Bub profile?
- What conformance level is required before support is called first-class?
- Which mappings belong in core and which belong beside their plugins?
- How should existing Bub-native data declare or acquire a profile version?

# Future possibilities

A standard-driven baseline can support shared viewers, evaluations, training
pipelines, telemetry backends, clients, and deployment presets without making
them mandatory. New standards can be adopted independently when they satisfy
the same value and compatibility criteria.

[CloudEvents]: https://github.com/cloudevents/spec
[ATIF]: https://github.com/harbor-framework/harbor/blob/main/rfcs/0001-trajectory-format.md
[OpenTelemetry GenAI]: https://github.com/open-telemetry/semantic-conventions-genai
