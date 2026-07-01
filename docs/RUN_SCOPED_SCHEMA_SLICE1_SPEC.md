# Spec: Run-Scoped Schema Slice 1

This is the implementation specification for the first production slice of the run-scoped schema architecture described in:

- [ADR-002](decisions/ADR-002-engineering-guardrails-for-agent-and-architecture-decisions.md)
- [ADR-003](decisions/ADR-003-run-scoped-typed-field-contracts-and-generic-canonicalization.md)
- [RUN_SCOPED_SCHEMA_IMPLEMENTATION_CHECKLIST.md](RUN_SCOPED_SCHEMA_IMPLEMENTATION_CHECKLIST.md)

This spec is intentionally limited to the first safe vertical slice.

It does **not** attempt to implement the full long-term semantic canonicalization architecture in one step.

## Objective

Introduce a run-scoped field contract model that:

- replaces persistent-by-default custom field presets
- captures field typing for the current run only
- propagates that contract through the job pipeline
- keeps the current extractor behavior working
- creates the interface boundary needed for later type-aware extraction and canonicalization

Success means:

- operators define unresolved field types for the current run
- those definitions do not silently persist by default
- the run contract is visible, inspectable, and logged
- the existing extraction pipeline remains stable behind a rollback flag

## Scope

This slice includes:

1. run-scoped field contract data model
2. UI flow for unresolved field typing
3. propagation of run contracts into jobs and runtime
4. minimal extractor interface updates for contract awareness
5. logging and observability for contract decisions
6. rollback switch
7. seed eval dataset format for this slice

This slice does **not** include:

- semantic merge / clustering logic
- persistent templates by default
- embeddings-based canonicalization
- promotion of custom fields into canonical fields
- full type-aware extraction prompts

## Assumptions

1. The first user-facing output types remain:
   - `number`
   - `short_list`
   - `text`
2. Unknown fields should continue to resolve conservatively to generated fields rather than risky canonical matches.
3. Existing canonical extraction behavior must remain intact during migration.
4. The current custom preset file path can be deprecated in the implementation, but migration should not break existing UI sessions abruptly.

## Commands

Validation commands for the eventual implementation:

```powershell
py -3 -m unittest tests.test_input_normalization
py -3 -m unittest tests.test_scraper_image_first
py -3 -m unittest discover -s tests -p "test_*.py"
py -3 -m streamlit run ui_app.py
```

## Current Relevant Files

- [ui_app.py](../ui_app.py:1)
- [amazon_scraper/input_normalization.py](../amazon_scraper/input_normalization.py:1)
- [amazon_scraper/models.py](../amazon_scraper/models.py:1)
- [amazon_scraper/scraper.py](../amazon_scraper/scraper.py:1)
- [amazon_scraper/extraction.py](../amazon_scraper/extraction.py:1)
- [amazon_scraper/vision_extraction.py](../amazon_scraper/vision_extraction.py:1)
- [amazon_scraper/custom_field_presets.py](../amazon_scraper/custom_field_presets.py:1)

## Data Model

### New Contract Object

Add a run-scoped field contract model in [amazon_scraper/models.py](../amazon_scraper/models.py:1):

```python
@dataclass(frozen=True)
class RunFieldContract:
    requested_label: str
    internal_key: str
    output_type: str  # "number" | "short_list" | "text"
    merge_similar_values: bool = False
    matched_by: str = ""
```

Rationale:

- `requested_label` preserves the user-facing name
- `internal_key` remains the runtime field identifier
- `output_type` becomes the minimal typed contract
- `merge_similar_values` is captured now, even though semantic merging is not implemented in this slice
- `matched_by` preserves resolver provenance

### ScrapeJob Migration

Extend `ScrapeJob` to carry contracts:

```python
@dataclass(frozen=True)
class ScrapeJob:
    asin: str
    product_type: str = ""
    requested_fields: List[str] = field(default_factory=list)  # compatibility bridge
    field_contracts: List[RunFieldContract] = field(default_factory=list)
```

Migration rule:

- `requested_fields` remains for compatibility in slice 1
- `field_contracts` becomes the new source of truth when present
- helper functions should derive `requested_fields` from `field_contracts` rather than duplicating free-form logic

## UI Flow

### Desired Operator Experience

1. User enters requested fields.
2. Resolver classifies fields as:
   - safe canonical
   - generated custom
3. For generated custom fields, the UI asks for:
   - `Data Type`
     - `Number`
     - `Short list`
     - `Text`
   - `Merge Similar Values`
     - `true/false`
4. The resulting run contract is displayed in the preview.
5. The contract is stored in session state for the current run only.
6. No automatic disk persistence occurs.

### UI Contract State

Replace persistent preset state in [ui_app.py](../ui_app.py:1) with run-scoped contract state:

```python
st.session_state["run_field_contracts"]
```

Recommended shape:

```python
{
  "flavor": {
    "requested_label": "Flavor",
    "internal_key": "flavor",
    "output_type": "text",
    "merge_similar_values": True,
    "matched_by": "generated",
  }
}
```

### UX Requirements

- no `Save Custom Field Rules` as the default action
- the contract editor must be clearly labeled as `Current Run Field Contract`
- help text must explain:
  - `Number`: one numeric value
  - `Short list`: multiple items in one cell
  - `Text`: one free-text value
- if the run is cleared, the contract state is cleared too

### Future-Compatible Optionality

Do **not** implement template save/load in this slice.

If needed later, it should appear as explicit actions such as:

- `Save as template`
- `Load template`

## Runtime Interface Changes

### Input Normalization Layer

Extend [amazon_scraper/input_normalization.py](../amazon_scraper/input_normalization.py:1) so the build result can support contract construction.

No behavioral redesign is required in this slice beyond using the existing conservative resolver.

New helper to add:

```python
def build_run_field_contracts(
    selections: Sequence[NormalizedFieldSelection],
    run_contract_inputs: Mapping[str, dict[str, object]],
) -> list[RunFieldContract]:
    ...
```

Behavior:

- canonical fields may receive default inferred types later, but slice 1 only requires contracts for generated fields
- generated fields must be rejected at run time if no output type is supplied

### Scraper Runtime

Update [amazon_scraper/scraper.py](../amazon_scraper/scraper.py:1):

- when `field_contracts` exist, derive active field list from `contract.internal_key`
- include contract metadata in job-level logging
- pass contract metadata into extractor interfaces

### HTML Extractor Interface

Update [amazon_scraper/extraction.py](../amazon_scraper/extraction.py:41):

```python
def extract_field(
    self,
    field_name: str,
    title: str,
    chunks: List[ChunkRecord],
    *,
    asin: str,
    retry_attempt: int = 0,
    field_contract: RunFieldContract | None = None,
) -> FieldResult:
    ...
```

Slice 1 behavior:

- `field_contract` is accepted and logged
- prompts do not yet fully enforce the type contract
- future slices will use `field_contract` to constrain output shape

### Vision Resolver Interface

Update [amazon_scraper/vision_extraction.py](../amazon_scraper/vision_extraction.py:117):

```python
def resolve_fields(
    self,
    *,
    asin: str,
    requested_fields: Iterable[str],
    images: list[GalleryImageRecord],
    field_contracts: Mapping[str, RunFieldContract] | None = None,
) -> dict[str, FieldResult]:
    ...
```

Slice 1 behavior:

- `field_contracts` are accepted and logged
- no semantic merge behavior yet

## Logging And Observability

This slice must make the run contract observable.

### Required Log Events

1. `field_resolution_event`

Required fields:

- `requested_label`
- `internal_key`
- `matched_by`
- `product_type`

2. `field_contract_event`

Required fields:

- `requested_label`
- `internal_key`
- `output_type`
- `merge_similar_values`

3. `job_contract_summary`

Required fields:

- `asin`
- `contract_count`
- `generated_contract_count`
- `contract_keys`

### Debug Artifact

If debug HTML snapshots are enabled, add a sidecar JSON section recording the job contract used for that ASIN.

This must not include secrets or API payloads.

## Rollback Strategy

Add a feature flag that cleanly disables the new path.

Recommended name:

```text
RUN_SCOPED_SCHEMA_ENABLED
```

Recommended behavior:

- `false` -> current legacy behavior continues
- `true` -> run-scoped field contract flow is used

Initial rollout recommendation:

- enabled in development/testing only
- disabled by default for production until eval seed and slice validation are complete

## Evaluation Dataset Shape

Slice 1 evals should focus on:

1. field resolution correctness
2. contract capture correctness
3. run propagation correctness

### Suggested JSONL Format

```json
{"requested_label":"Flavor","expected_internal_key":"flavor","expected_matched_by":"generated","expected_output_type":"text"}
{"requested_label":"Packaging","expected_internal_key":"packaging_type","expected_matched_by":"alias","expected_output_type":"text"}
{"requested_label":"Capsule counts","expected_internal_key":"capsule_counts","expected_matched_by":"generated","expected_output_type":"number"}
```

### Minimum Eval Questions

- Did the resolver generate the right `internal_key`?
- Did it avoid false-positive canonical mapping?
- Did the UI capture the chosen type correctly?
- Did the job carry the contract end to end?

## Acceptance Criteria

### Functional

- [ ] Generated custom fields use run-scoped contracts instead of persistent-by-default presets.
- [ ] Clearing the run clears unresolved-field type selections.
- [ ] Generated fields without an assigned type cannot start a run.
- [ ] The run contract is visible in the UI preview.
- [ ] `ScrapeJob` can carry `field_contracts`.
- [ ] Runtime derives active field keys from contracts when present.

### Observability

- [ ] The logs show how a field was resolved.
- [ ] The logs show what contract was applied.
- [ ] Debug artifacts can reveal the contract used for a job.

### Safety

- [ ] The feature can be disabled with a single flag.
- [ ] The existing batch path still works when the flag is off.
- [ ] No automatic disk persistence occurs in the default path.

## Files Likely Touched

- [ui_app.py](../ui_app.py:1)
- [amazon_scraper/models.py](../amazon_scraper/models.py:1)
- [amazon_scraper/input_normalization.py](../amazon_scraper/input_normalization.py:1)
- [amazon_scraper/scraper.py](../amazon_scraper/scraper.py:1)
- [amazon_scraper/extraction.py](../amazon_scraper/extraction.py:1)
- [amazon_scraper/vision_extraction.py](../amazon_scraper/vision_extraction.py:1)
- [amazon_scraper/custom_field_presets.py](../amazon_scraper/custom_field_presets.py:1)
- tests for:
  - input normalization
  - UI/session flow where practical
  - scraper contract propagation

## Implementation Tasks

### Task 1: Add data model support

Acceptance:

- `RunFieldContract` exists
- `ScrapeJob` can carry contracts

Verify:

- unit tests for model creation

### Task 2: Replace persistent default UI state with run-scoped contract state

Acceptance:

- unresolved generated fields require type selection for the current run
- no automatic save-to-disk path remains in default behavior

Verify:

- manual UI run
- targeted unit tests where practical

### Task 3: Build contract construction helpers

Acceptance:

- selections plus UI inputs become typed `RunFieldContract` objects

Verify:

- input normalization tests

### Task 4: Propagate contracts into runtime

Acceptance:

- `field_contracts` reach scraper and extractor interfaces

Verify:

- scraper integration tests

### Task 5: Add observability and rollback

Acceptance:

- feature flag exists
- logs include contract events

Verify:

- test logs / manual run

## Open Questions

- Should canonical fields in slice 1 also receive inferred `output_type`, or should that wait for slice 2?
- Should the UI block the run when generated fields are untyped, or auto-default them to `text` with an explicit warning?
- Should the feature flag be environment-only, UI-visible, or both?
