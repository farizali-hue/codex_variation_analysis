# ADR-002: Engineering Guardrails For Agent And Architecture Decisions

## Status

Accepted

## Date

2026-04-17

## Context

This repository is evolving from a useful extraction prototype into an enterprise-facing intelligent system.

That shift changes the standard for acceptable implementation decisions.

In earlier iterations, some fixes improved immediate behavior but did so in ways that do not scale well:

- field-specific patch logic
- alias growth as a substitute for resolver design
- hidden persistent behavior
- symptom fixes presented as if they were architectural solutions

Those patterns are costly in an enterprise setting because they:

- hide complexity
- create non-obvious behavior
- make deployment riskier
- are hard to evaluate systematically
- increase long-term maintenance cost

The project therefore needs explicit decision-making guardrails for both human and agent contributors.

## Decision

Adopt the following engineering guardrails for architecture-sensitive work in this repository.

### 1. No symptom fixes presented as final design

Temporary patches are allowed only when they are clearly labeled as temporary.

They must not be framed as durable architecture.

### 2. Design review before architecture-impacting implementation

Before implementing non-trivial changes to:

- field resolution
- output schema
- normalization / canonicalization
- persistence
- AI routing
- multimodal behavior

contributors should first document:

1. the root problem
2. the long-term options
3. pros and cons
4. the recommended direction
5. whether approval is required before implementation

### 3. Prefer conservative field resolution

False-positive canonical matches are worse than generating a new custom field.

Resolver design should therefore bias toward:

- exact matches
- explicit safe aliases
- conservative fuzzy matching
- generating new fields when confidence is low

### 4. Default to run-scoped state over persistent hidden state

If user-defined schema or field typing is needed, the default should be:

- scoped to the current run
- explicit in the UI or run manifest
- discarded after the run unless the operator explicitly chooses to save it

### 5. Favor generic strategies over field-specific equivalence maps

Canonicalization should be driven by:

- field type
- schema contract
- evidence quality
- evaluation-backed similarity pipelines

It should not rely on a growing set of field-specific hardcoded synonym tables in the main design.

### 6. Enterprise constraints are first-class requirements

Architecture proposals should consider:

- provenance
- observability
- auditability
- rollback behavior
- evaluation coverage
- operator clarity
- cost predictability

## Alternatives Considered

### Implicit agent memory only

Pros:

- no extra documentation work
- faster in the short term

Cons:

- does not survive sessions
- creates repeated misalignment
- cannot be inspected by future contributors

Rejected because the project needs durable, inspectable guidance.

### Informal chat-only alignment

Pros:

- lightweight
- low documentation overhead

Cons:

- easy to lose
- not enforceable in future sessions
- not visible to new engineers or new agents

Rejected because the constraints are architectural, not conversational.

## Consequences

Positive:

- architecture discussions become more explicit and reviewable
- hidden technical debt is less likely to enter the main path
- the repository gains project-specific standards for agent behavior
- future contributors can recover intent without replaying chat history

Negative:

- some tasks will take longer before code is written
- contributors must distinguish between tactical patches and architectural changes more carefully
- more decisions will require lightweight documentation up front

## Follow-On Notes

- [AGENTS.md](../../AGENTS.md) provides the repo-level always-on version of these rules.
- Subsequent ADRs should reference this document when proposing major schema or AI behavior changes.
