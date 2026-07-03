# Spec: Generalized Supplement Ingredient Canonicalization (Silver Layer)

Status: DRAFT for review
Owner: (you)
Skill workflow: spec-driven-development → planning-and-task-breakdown → incremental-implementation → test-driven-development

---

## 1. Problem

The scraper produces, per ASIN, a rich long-form ledger of supplement-facts lines
(`*_supplement_facts_rows.csv`) and a lossy wide CSV. The same real-world
ingredient appears under many surface forms across products
(`Pumpkin Seed Oil (Cucurbita pepo)`, `Pumpkin Seed Oil`, `Organic Pumpkin Seed Oil…`;
`Vitamin D`, `Vitamin D3 (as cholecalciferol)`, `Vitamin D3 Vegan (VegaDELight™…)`),
so a clean per-product comparison sheet requires collapsing those forms into a small
set of **canonical columns**.

Today this collapsing is done by hand. A per-product dictionary does not scale: this
is **continuous category research**, and new products with new ingredients arrive
constantly. We need canonicalization that handles **unseen** ingredients automatically,
with **no hand-maintained per-product dictionary**.

This spec is **category-general**. It must work for any supplement product type
(vitamins, minerals, oils, botanicals, amino acids, proprietary blends), not only the
pumpkin-seed-oil or D3K2 datasets used for evaluation.

## 2. Goals / Non-goals

### Goals
- G1. Given any ingredient string, return a **canonical ingredient name** with no
  product-specific configuration.
- G2. The canonical vocabulary is a **self-populating cache**, grown from the engine's
  own confident decisions — never hand-edited per product.
- G3. **Quality first**: the merge decision is made by LLM reasoning, never by raw
  string/vector similarity. Cost is optimized only where it does not reduce quality.
- G4. **Lossless inputs**: consume the long-form ledger; preserve `amount_text`,
  `source_name` (which raw form the value came from), `category`, `parent_blend_name`,
  and provenance through to the output.
- G5. **Never silently mis-merge.** Distinct nutrients (e.g. D2≠D3, K1≠K2) must not be
  merged. Low-confidence cases go to a human review queue, not into the output.
- G6. **Auditable & reversible.** Every merge records the source form, the canonical it
  mapped to, the method (rule/cache/embedding/LLM), and a confidence.

### Non-goals (this spec)
- External-data joins (Brand, Reviews, BSR, Bought-in-month from Helium10/Index) —
  separate enrichment step.
- Pivot/Index/summary sheet generation (Gold layer).
- Unit conversion correctness (IU↔mcg) beyond preserving `amount_text` — tracked
  separately, but the data contract leaves room for it.
- Fixing scraper extraction accuracy (e.g. packaging_type) — upstream of this layer.

## 3. Key design decision: consume the rich ledger, not the wide CSV

The wide CSV keeps only `csv_value` keyed by raw name and drops `amount_text`, `%DV`,
`category`, `parent_blend_name`, provenance, and the blend hierarchy; un-normalizable
amounts become blank. The canonicalizer therefore reads the **long-form ledger**
(`SupplementFactsIngredient` records / `*_supplement_facts_rows.csv`) so it can:
- keep `amount_text` + the originating `source_name` (cf. the D3K2 final file's
  `Vitamin D3_amount_text` / `Vitamin D3_source_name`),
- treat `blend_sub_ingredient` rows deliberately (keep nested or hoist on purpose),
- **flag** un-normalizable amounts for review instead of silently blanking them.

## 4. Architecture — EDC pipeline (Extract → Define → Canonicalize)

Grounded in the EDC framework (arXiv:2404.03868) + LLM-cascade cost control
(FrugalGPT-style) + semantic caching.

```
ingredient string
  │
  1. NORMALIZE        generic, category-wide regex (strip ™/®, "(as …)", "(MK-n)",
  │                   plant parts, word-order). Deterministic. $0. NOT per-product.
  2. CACHE LOOKUP     persistent canonical store; exact + near-duplicate hit → done. $0.
  3. CANDIDATE GEN    embed normalized name, retrieve top-K nearest canonicals (recall
  │                   only — never the decision). ~ $0 with cached embeddings.
  4. DECIDE (LLM)     LLM picks a candidate OR names a new canonical, WITH disambiguation
  │                   guards. Cheap model first; escalate to strong model when unsure.
  5. WRITE-BACK       persist {variant → canonical, method, confidence} to the store.
  6. REVIEW QUEUE     confidence < threshold OR disambiguation-ambiguous → human queue,
                      never auto-applied.
```

- Steps 2–6 already largely exist in `RunScopedCanonicalizer` (embedding ranking + LLM
  equivalence judge + confidence threshold + clustering + caching). Reuse it; repoint
  from "values within a field" to **ingredient names**, add the Define/naming step, and
  persist the store across runs.

## 5. Data contracts

### 5.1 Canonical store (persisted JSON/SQLite, grows automatically)
```
{
  "canonicals": [
    {
      "canonical_name": "Vitamin D3",
      "aliases": ["vitamin d", "vitamin d3 as cholecalciferol", "vitamin d3 vegan", ...],
      "embedding_ref": "...",
      "disambiguation_class": "vitamin_d",      // guards: D2 vs D3 must differ
      "created_by": "llm|seed|human",
      "confidence": 0.0-1.0
    }
  ],
  "variant_index": { "<normalized variant>": "<canonical_name>" }
}
```
- The store is **append-only by the engine**; humans only resolve review-queue items,
  which then write back. No per-product file.

### 5.2 Canonicalized ingredient record (output of the engine, per ASIN × ingredient)
```
asin, canonical_name, source_name (raw form chosen), amount_text, amount_value,
amount_unit, category, parent_canonical (if blend sub-ingredient),
match_method (rule|cache|embedding|llm), confidence, needs_review (bool), evidence
```
This is what a downstream "build wide sheet" step pivots into canonical columns,
preserving `_amount_text` / `_source_name` like the D3K2 final.

## 6. Disambiguation rules (quality guard — must never over-merge)

The LLM prompt and store carry explicit `disambiguation_class` guards. Non-exhaustive,
extensible:
- Vitamin D2 (ergocalciferol) ≠ Vitamin D3 (cholecalciferol).
- Vitamin K1 (phylloquinone) ≠ Vitamin K2 (menaquinone); MK-4 and MK-7 are both K2.
- Different mineral forms map to the element (Magnesium Glycinate → Magnesium) **unless**
  the research intent needs the form distinguished — configurable per run.
- Stereochemistry/isomers where they matter (d- vs dl-alpha tocopherol) flagged, not
  silently merged.
Any pair the LLM cannot confidently separate → review queue.

## 7. Cost controls (secondary to quality)

- **Semantic cache**: each unique ingredient costs ≤1 LLM call ever; repeats are free.
  Supplement ingredients repeat heavily across products → high hit rate over time.
- **Blocking**: LLM sees only top-K candidates, not the whole vocabulary.
- **Model cascade**: cheap model for clear cases, escalate to strong model only on low
  confidence / disambiguation-sensitive cases.
- **Batching** of embedding + LLM calls.
- Cost levers are caching/routing only — they avoid redundant work, they do not weaken
  the decision-maker. The cascade can be pinned to the strong model when quality must
  win.

## 8. Quality safeguards

- LLM-as-decider (never similarity-as-decider).
- Disambiguation guards (§6).
- Confidence threshold → review queue; review decisions train the store.
- Full provenance + method on every record (§5.2) → auditable, reversible.
- Regression eval gate (§9) on every change.

## 9. Acceptance criteria (measurable, category-general)

Evaluated on a **held-out gold set spanning ≥3 distinct product categories** built from
existing hand-cleaned finals (pumpkin, D3K2, + one more from `outputs/`, e.g. milk
thistle or beetroot). The engine is run with an **empty store** (no product-specific
seeding) to prove generalization.

- A1. **Column canonicalization accuracy ≥ 95%**: raw ingredient → correct canonical
  column, averaged across the ≥3 categories, with **no per-product dictionary entries**.
- A2. **Value-placement accuracy ≥ 95%**: the right amount lands in the right canonical
  column for the right ASIN (measured on populated cells; blanks from empty source
  excluded).
- A3. **Zero disambiguation violations**: across all gold rows, D2/D3, K1/K2 (and any
  declared guard pair) are **never** merged. A single violation fails the gate.
- A4. **Cost discipline**: each unique normalized ingredient triggers ≤1 LLM resolve;
  a second run over the same data triggers **0** resolves (100% cache hit).
- A5. **Review queue is precise**: every queued item has a suggested canonical + evidence
  + reason; spot-check ≥90% of queued items are genuinely ambiguous (not trivial).
- A6. **Lossless fields preserved**: `amount_text`, `source_name`, `category`,
  `parent_canonical`, provenance present on every output record.

(Targets are initial; validated and adjusted against the first gold run. The residual
human-review tail is intrinsic — the goal is to shrink it, not claim 100%.)

## 10. Eval methodology

1. Build gold maps from each final's Consolidated tab: `raw ingredient string → canonical column`.
2. Run engine on each product's `*_supplement_facts_rows.csv` with an empty store.
3. Score A1–A6. Persist a report (per-category accuracy, review-queue size, LLM-call count).
4. Wire as a pytest gate using small committed fixtures (do not commit proprietary OneDrive data wholesale).

## 11. Open questions (resolve before/early in build)
- Q1. Store backend: JSON (simple, versionable) vs SQLite (scales, concurrent). Default: JSON now, SQLite if volume demands.
- Q2. Mineral-form policy (element vs form) — global default, per-run override? Default: collapse to element, override flag.
- Q3. Cheap/strong model pair for the cascade (e.g. Haiku → Opus). Pick during build.
- Q4. Confidence thresholds for auto-apply vs review — calibrate on the first gold run.

## 12. Out of scope / later
- External enrichment join (Brand/Reviews/BSR/Bought-in-month).
- Pivot/Index/summary (Gold) generation.
- IU↔mcg unit conversion correctness.
- Multi-run de-duplication / highest-value cleanup across scrape re-runs.
```
