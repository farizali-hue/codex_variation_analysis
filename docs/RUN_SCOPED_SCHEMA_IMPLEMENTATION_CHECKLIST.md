# Run-Scoped Schema Implementation Checklist

This document is the execution bridge between:

- [ADR-002](decisions/ADR-002-engineering-guardrails-for-agent-and-architecture-decisions.md)
- [ADR-003](decisions/ADR-003-run-scoped-typed-field-contracts-and-generic-canonicalization.md)

It answers three practical questions:

1. When is it the right time to start implementation?
2. What checklist should be satisfied from feature request to rollout?
3. What should the implemented system concretely look like?

## Short Answer: When To Start Implementation

Start implementation only after the following are true:

- the architecture direction is approved
- the run-scoped field contract is agreed
- the first target use cases are fixed
- the evaluation plan exists
- the first implementation slice is small enough to ship safely

Do **not** start with full canonicalization or persistence.

Start with the first vertical slice only when the system has:

1. an accepted contract for run-scoped fields
2. a known output shape for each supported type
3. a seed eval set
4. a rollback path

## Recommended Start Point For This Repo

The best moment to begin coding is after these five items are approved:

### Gate A: Scope Lock

The first supported run-scoped types are fixed as:

- `number`
- `short_list`
- `text`

No broader type family should be added in the first slice.

### Gate B: First User Flow Lock

The first supported operator flow is fixed as:

1. user enters requested fields
2. system resolves safe canonicals conservatively
3. unresolved fields become run-scoped custom fields
4. user assigns type for unresolved fields
5. run proceeds using that contract

### Gate C: Eval Seed Exists

A small but real eval set exists for:

- field resolution correctness
- output typing correctness
- canonicalization merge vs no-merge decisions

This can be small initially, but it must exist before merge-heavy behavior is implemented.

### Gate D: Rollback Plan Exists

The feature can be disabled or bypassed cleanly.

For this repo, the preferred rollback approach is:

- keep the old extraction pipeline available behind a feature flag or configuration toggle
- enable run-scoped schema behavior incrementally

### Gate E: Observability Plan Exists

The implementation must log enough to answer:

- how a user field was resolved
- which type contract was used
- whether canonicalization merged or abstained
- what source won
- where a value came from

## Feature Lifecycle Checklist

This is the internal checklist from requested feature to production-ready implementation.

### Phase 0: Problem Framing

- Define the real problem, not the visible symptom.
- Identify whether the problem is:
  - resolver quality
  - extraction quality
  - normalization quality
  - operator UX
  - evaluation gap
- State the failure mode in one sentence.

Exit criteria:

- the problem statement is specific
- the failure mode is reproducible
- the non-goals are explicit

### Phase 1: Architecture Decision

- Write or update the ADR.
- Define the long-term options.
- Record pros and cons.
- Choose the default state model:
  - run-scoped
  - persisted only by opt-in
- Define the contract boundary between:
  - resolver
  - extractor
  - normalizer
  - export layer

Exit criteria:

- architecture is approved
- decision boundaries are documented
- no unresolved hidden-state behavior remains

### Phase 2: Contract Design

- Define the run-scoped field contract schema.
- Define required keys.
- Define allowed output types.
- Define how raw vs normalized values will be stored.
- Define provenance fields.

Recommended contract shape:

```json
{
  "requested_label": "Flavor",
  "internal_key": "flavor",
  "output_type": "text",
  "merge_similar_values": true
}
```

Exit criteria:

- contract is stable
- every participating component can consume it
- no component has to infer missing meaning from free text

### Phase 3: Evaluation Design

- Build task-specific evals before semantic logic ships.
- Put semantic merge behind a dedicated runtime boundary before it can rewrite production outputs.
  - recommended control: `RUN_SCOPED_TEXT_MERGE_MODE=off|shadow|apply`
  - `off`: do not evaluate or rewrite text merges
  - `shadow`: evaluate and log merge decisions without rewriting exported values
  - `apply`: allow merge decisions to rewrite exported values
- Include:
  - typical cases
  - edge cases
  - adversarial / ambiguous cases
- Separate eval categories:
  - field resolution
  - extraction correctness
  - canonicalization correctness
  - abstention correctness

OpenAI guidance aligned to this phase:

- eval-driven development
- task-specific evals
- pairwise or classification-style grading
- continuous evaluation on changes

References:

- https://platform.openai.com/docs/guides/evaluation-best-practices

Exit criteria:

- seed eval set exists
- scoring rules exist
- human review criteria exist

Current implementation note:

- seed pairwise merge eval cases live at `evals/run_scoped_text_merge_seed.jsonl`
- the offline scorer lives at `amazon_scraper/run_scoped_text_merge_eval.py`
- shadow-mode runtime review artifacts are written into debug sidecar metadata as `run_scoped_merge_events`

### Phase 4: Implementation Planning

- Decompose into vertical slices, not horizontal refactors.
- Each slice should produce a user-visible improvement with rollback safety.

Recommended slice order:

1. Run-scoped contract only
2. UI for unresolved field typing
3. Type-aware extraction/output enforcement
4. Deterministic normalization for `number` and `short_list`
5. Observability and audit fields
6. Generic `text` canonicalization with abstention
7. Optional template save/load

Exit criteria:

- each slice has acceptance criteria
- each slice can be tested independently
- each slice can be rolled back independently

### Phase 5: Implementation

- Implement the smallest safe slice first.
- Keep the old path available until the new path is proven.
- Do not combine:
  - new contract model
  - new canonicalization strategy
  - persistence
  - new UI behavior
  in a single large rollout

Exit criteria:

- slice passes targeted tests
- logs are inspectable
- behavior matches the contract doc

### Phase 6: Validation

- Run unit tests.
- Run integration tests.
- Run a small real ASIN eval batch.
- Compare against human-reviewed expected outputs.
- Review false merges and false splits.

Exit criteria:

- no blocking regressions
- eval results are acceptable
- failure cases are understood

### Phase 7: Rollout

- Deploy behind a feature flag or staged enablement.
- Start with a small operator group or limited field set.
- Log:
  - field resolution decisions
  - type decisions
  - merge decisions
  - abstentions
  - costs

Exit criteria:

- operators can debug failures
- performance and cost are within bounds
- no hidden schema drift is observed

### Phase 8: Post-Launch Hardening

- mine logs for new eval cases
- promote recurring custom fields to canonical only when justified
- keep the merge pipeline conservative
- tune thresholds with human feedback

Exit criteria:

- eval set grows from production cases
- canonical promotions are documented
- operator UX remains comprehensible

## Definitive Example

### Requested Fields

```text
Flavor
Capsule counts
Ingredients
```

### Run Contract

```json
[
  {
    "requested_label": "Flavor",
    "internal_key": "flavor",
    "output_type": "text",
    "merge_similar_values": true
  },
  {
    "requested_label": "Capsule counts",
    "internal_key": "capsule_count",
    "output_type": "number",
    "merge_similar_values": false
  },
  {
    "requested_label": "Ingredients",
    "internal_key": "ingredients",
    "output_type": "short_list",
    "merge_similar_values": false
  }
]
```

### Raw Evidence

```text
ASIN_A flavor_raw = "No Flavour"
ASIN_B flavor_raw = "Plain"
ASIN_C flavor_raw = "Unflavored"
ASIN_D flavor_raw = "French Vanilla"

ASIN_A capsule_count_raw = "120 Quick Release Softgels"
ASIN_A ingredients_raw = "Gelatin, Rice Bran Oil; Soy Lecithin"
```

### Expected Output

```text
asin,flavor,flavor_raw,capsule_count,capsule_count_raw,ingredients,ingredients_raw
ASIN_A,unflavored,No Flavour,120,120 Quick Release Softgels,Gelatin | Rice Bran Oil | Soy Lecithin,"Gelatin, Rice Bran Oil; Soy Lecithin"
ASIN_B,unflavored,Plain,,,
ASIN_C,unflavored,Unflavored,,,
ASIN_D,french vanilla,French Vanilla,,,
```

### What Happened Internally

- `Flavor` was typed as `text` with merge enabled.
- The system treated all observed `flavor` values in this run as candidates for same-field canonicalization.
- `No Flavour`, `Plain`, and `Unflavored` were merged only if similarity plus adjudication supported equivalence.
- `Capsule counts` was typed as `number`, so only the numeric component survived.
- `Ingredients` was typed as `short_list`, so the extractor preserved multiple items but normalized separators and deduplicated them.

## Current Recommendation

For this repository, the recommended implementation order is:

1. Finalize the run-scoped field contract.
2. Replace persistent-by-default custom presets with run-scoped state.
3. Make extraction consume the contract directly.
4. Add deterministic normalization for `number` and `short_list`.
5. Add observability fields and logs.
6. Only then implement generic text canonicalization with evals.

Do **not** start with:

- persistent template growth
- field-specific synonym maps
- broad semantic merging without evals
- multi-type expansion beyond the first three user-facing types
