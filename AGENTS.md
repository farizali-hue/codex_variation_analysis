# Project Rules For Codex And Other Agents

This file is a project-level working contract for AI agents and engineers contributing to this repository.

It exists because this codebase is being shaped toward an enterprise-grade intelligent extraction system, not a collection of local patches.

Agents should load and follow this file before giving substantive guidance, proposing architecture, or implementing code in this repository.

## Primary Goal

Build an intelligent, auditable, and enterprise-ready field extraction system that:

- prefers the most reliable source automatically
- preserves provenance and confidence
- scales to changing user-requested fields
- avoids brittle alias growth and field-specific patch logic
- can be evaluated, operated, and deployed safely in production

## Collaboration Contract

Treat the user as a senior technical stakeholder.

Assume:

- they can follow architectural tradeoffs
- they care about long-term system quality more than short-term demos
- they expect explicit reasoning, not hidden assumptions

Do not over-simplify responses unless asked.

## Response And Decision Protocol

For new chats in this repository, agents should default to the following behavior:

- inspect the current implementation before recommending changes
- explain at a senior-engineer level rather than a beginner level unless the user explicitly asks for simplification
- do not jump straight to short-term patches when the problem is architectural
- separate current-state facts, root-cause analysis, options, and recommendation clearly
- surface tradeoffs explicitly when the choice is non-obvious
- ask for approval before implementing architecture-impacting changes
- label temporary fixes as temporary
- be honest about limitations, uncertainty, and what is not yet production-ready

If response quality drifts, agents should realign to this protocol before continuing.

## Escalation And Research Expectations

When the task involves fast-moving AI product patterns, model behavior, evaluation strategy, or production architecture:

- prefer current official sources if local context is insufficient
- do not bluff through uncertainty
- escalate constraints or blockers to the user the way a senior engineer would escalate to a manager or technical lead
- state when a recommendation is a local inference versus a documented fact

The goal is not just to produce code. The goal is to make decisions that remain defensible at company scale.

## Non-Negotiable Engineering Rules

### 1. Do Not Present Symptom Fixes As Final Architecture

Examples of symptom fixes:

- adding one more alias for one more field
- hardcoding field-specific synonym maps into the main path
- introducing persistent hidden behavior to cover one observed edge case
- patching one wrong field match without addressing the resolver policy that caused it

If a short-term patch is proposed, it must be labeled clearly as temporary.

### 2. Architecture-Impacting Changes Need Design First

Before implementing changes in any of these areas:

- field resolution
- canonicalization / normalization
- schema / output contracts
- source precedence
- persistence / templates / hidden state
- AI routing or retrieval behavior

the agent must first provide:

1. the root problem
2. the long-term design options
3. tradeoffs
4. the recommended approach
5. a request for approval before implementation

### 3. Prefer False-Negative Matching Over False-Positive Canonical Mapping

When resolving user-entered fields:

- a wrong canonical match is worse than generating a new custom field
- unsafe fuzzy or alias-driven guesses should be avoided
- user intent should not be force-fit into existing schema terms unless confidence is high

### 4. Default To Run-Scoped Behavior Over Hidden Persistent State

If the system needs user-defined schema or field typing:

- keep it scoped to the current run by default
- persistence must be explicit and opt-in
- no silent accumulation of preset files or hidden rules should become core behavior

### 5. Generic Strategies Beat Field-Specific Patch Tables

For normalization and canonicalization:

- prefer strategies by field type
- avoid field-specific hardcoded equivalence tables in the main architecture
- if semantic merging is required, use a generic same-field pipeline with evaluation and abstention

### 6. Enterprise Readiness Is Part Of The Design

When proposing architecture, account for:

- provenance
- auditability
- rollback path
- observability
- cost behavior
- evaluation coverage
- operator clarity

## Required Decision Style

For significant coding tasks, the preferred sequence is:

1. clarify the true problem
2. inspect the current implementation
3. identify root-cause design issues
4. propose long-term options
5. ask for approval when tradeoffs are non-obvious
6. implement only after alignment

## Current Strategic Direction

The current accepted and proposed architecture is captured in:

- [docs/decisions/ADR-001-image-first-html-fallback-extraction.md](docs/decisions/ADR-001-image-first-html-fallback-extraction.md)
- [docs/decisions/ADR-002-engineering-guardrails-for-agent-and-architecture-decisions.md](docs/decisions/ADR-002-engineering-guardrails-for-agent-and-architecture-decisions.md)
- [docs/decisions/ADR-003-run-scoped-typed-field-contracts-and-generic-canonicalization.md](docs/decisions/ADR-003-run-scoped-typed-field-contracts-and-generic-canonicalization.md)

Agents should load and follow those ADRs before making architecture-sensitive changes in this repository.
