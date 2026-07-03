# ADR-003: Run-Scoped Typed Field Contracts And Generic Canonicalization

## Status

Proposed

## Date

2026-04-17

## Context

The current system supports a mix of built-in canonical fields and generated custom fields.

That is useful, but the present custom-field path is not yet the right long-term architecture for enterprise use.

Current pain points:

- user-entered fields can drift into alias- or fuzzy-driven misclassification
- output normalization can become field-specific and brittle
- persistent presets can create hidden long-term behavior
- operators may want standardized output without predefining every possible value in advance

The project needs a design that:

- allows changing requested fields per run
- produces stable typed output
- avoids unbounded preset growth
- supports canonicalization without field-specific patch tables
- preserves auditability

This is especially important because the system is intended to become a production intelligent extraction layer, not a one-off enrichment script.

## Decision

Adopt the following architecture direction for future schema and canonicalization work.

### 1. Run-scoped typed field contracts

For each run, the operator defines a field contract for every requested output column.

The contract should minimally include:

- `requested_label`
- `internal_key`
- `output_type`
- `merge_similar_values`

The default user-facing output types are:

- `number`
- `short_list`
- `text`

This contract belongs to the current run by default.

It should not be persisted automatically.

### 2. Optional persistence, not default persistence

If field contracts are worth reusing, they should be saved only through an explicit operator action such as:

- save as named template
- reuse template

Silent preset accumulation is rejected as the default behavior.

### 3. Extraction should target the schema contract directly

For fields that require model extraction, the system should request outputs in the declared type shape rather than extracting free text and trying to infer type afterward.

Where OpenAI is used, the preferred direction is schema-constrained structured output rather than loose JSON mode.

Official reference:

- OpenAI Structured Outputs / JSON schema guidance:
  https://platform.openai.com/docs/guides/json-mode

### 4. Canonicalization should be generic by field type

The main architecture should avoid field-specific hardcoded synonym tables.

Instead:

- `number`
  - deterministic numeric parsing and optional unit handling
- `short_list`
  - deterministic split, trim, per-item cleanup, and dedupe
- `text` with `merge_similar_values = true`
  - generic same-field canonicalization pipeline within the current run

### 5. Text merging should be run-scoped and generic

For `text` fields with merging enabled, the canonicalization pipeline should be:

1. orthographic normalization
2. candidate generation within the same field for the same run
3. semantic similarity scoring
4. equivalence adjudication with abstention
5. canonical representative selection

Embeddings are appropriate for candidate generation and clustering, not for exact value extraction.

Official reference:

- OpenAI embeddings clustering guidance:
  https://platform.openai.com/docs/guides/embeddings/clustering

### 6. Evaluation is required before production rollout

Semantic canonicalization must be measured with a task-specific evaluation set.

The system should support:

- merge / no-merge gold labels
- abstention thresholds
- regression tests
- operator review for uncertain merges

Official reference:

- OpenAI evaluation best practices:
  https://platform.openai.com/docs/guides/evaluation-best-practices

## Definitive Example

Run contract:

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

Observed raw values during the run:

- `flavor_raw = "No Flavour"`
- `flavor_raw = "Plain"`
- `flavor_raw = "Unflavored"`
- `flavor_raw = "French Vanilla"`

Expected system behavior:

- the first three values are considered as candidates for the same-field merge pipeline
- semantically equivalent values cluster together
- the canonical output becomes `unflavored`
- `French Vanilla` remains a separate canonical value

Final output example:

```text
asin,flavor,flavor_raw,flavor_source,flavor_confidence
ASIN_A,unflavored,No Flavour,image://...,0.93
ASIN_B,unflavored,Plain,image://...,0.90
ASIN_C,unflavored,Unflavored,html://...,0.95
ASIN_D,french vanilla,French Vanilla,image://...,0.94
```

This produces standardization without requiring the operator to enumerate every possible flavor value in advance.

## Alternatives Considered

### Persistent preset files as the default behavior

Pros:

- easy reuse
- simple to implement

Cons:

- hidden long-term behavior
- preset drift
- difficult to govern across teams
- poor fit for highly variable per-run field requests

Rejected as the default behavior.

### Field-specific synonym maps in core logic

Pros:

- easy for narrow demos
- deterministic for the exact values already seen

Cons:

- symptom-based
- does not scale to new fields
- creates maintenance debt
- hides domain logic in code patches

Rejected as the long-term approach.

### No canonicalization, raw text only

Pros:

- simpler
- preserves original evidence

Cons:

- poor analyst experience
- inconsistent downstream data
- weak fit for enterprise reporting and comparison

Rejected because standardization is part of the product goal.

## Consequences

Positive:

- field definitions become explicit and run-scoped
- the system can standardize outputs without unbounded preset growth
- canonicalization becomes generic rather than field-specific
- auditability improves because raw and normalized values can coexist

Negative:

- this architecture is more complex than ad hoc preset mapping
- semantic merging requires evaluation and thresholds
- operator UX must clearly show run contracts and review paths

## Follow-On Notes

- This ADR is intentionally `Proposed`, not `Accepted`, until the implementation plan is reviewed and approved.
- [ADR-002](ADR-002-engineering-guardrails-for-agent-and-architecture-decisions.md) defines the decision-quality bar that applies to this proposal.
