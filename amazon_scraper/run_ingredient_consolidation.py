"""CLI: canonicalize a supplement-facts ledger into clean ingredient columns.

This is the ONLY entry point that calls the real OpenAI API. Run it yourself with your
own credentials (same OPENAI_API_KEY the scraper uses):

    python -m amazon_scraper.run_ingredient_consolidation outputs/pumpkin_seed_oil_20260615T183916Z_supplement_facts_rows.csv

It writes:
  <name>_canonical.csv                    -- wide table, one column per canonical ingredient
  <name>_canonical_needs_review.csv       -- items it would not guess (for your review)
  <name>_canonical_store_commit_audit.csv -- every decision and whether it was persisted

and updates the shared memory at .cache/canonical_ingredients_store.json so repeats are
free next time. Prints a summary including the estimated API cost for the run.

Store writes are staged in a `RunDecisionLedger` and committed only after consolidation
has validated them (ADR-004), so a decision that collided or went to review never reaches
the permanent vocabulary.
"""

from __future__ import annotations

import argparse
import csv
import logging
from pathlib import Path

from .canonical_decisions import CommitAudit, RunDecisionLedger
from .canonical_store import CanonicalStore
from .feature_flags import ingredient_identity_guard_mode
from .ingredient_canonicalizer import (
    IngredientCanonicalizer,
    OpenAIIdentityAdjudicator,
    OpenAIIngredientLLM,
)
from .ledger_consolidation import consolidate_from_ledger
from .openai_client import OpenAIClient, OpenAIConfig

LOGGER = logging.getLogger(__name__)
_DEFAULT_STORE = ".cache/canonical_ingredients_store.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Canonicalize a supplement-facts ledger CSV into clean ingredient columns.")
    parser.add_argument("rows_csv", help="path to a *_supplement_facts_rows.csv ledger file")
    parser.add_argument("--output", default=None, help="output CSV path (default: alongside input)")
    parser.add_argument("--store", default=_DEFAULT_STORE, help="canonical memory store path")
    parser.add_argument("--confidence-threshold", type=float, default=0.75)
    parser.add_argument("--canonicalization-model", default=None,
                        help="model for canonicalization (default: OPENAI_CANONICALIZATION_MODEL, else chat model)")
    parser.add_argument("--identity-guard-mode", default=None, choices=["off", "shadow", "apply"],
                        help="identity guards (default: INGREDIENT_IDENTITY_GUARD_MODE, else off)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(message)s")

    config = OpenAIConfig.from_env()
    client = OpenAIClient(config)
    client.reset_batch_usage()
    store = CanonicalStore.load(args.store)
    ledger = RunDecisionLedger(store)
    canonicalization_model = args.canonicalization_model or config.resolved_canonicalization_model()
    guard_mode = args.identity_guard_mode or ingredient_identity_guard_mode()
    engine = IngredientCanonicalizer(
        store=ledger,
        llm=OpenAIIngredientLLM(client, model=canonicalization_model),
        confidence_threshold=args.confidence_threshold,
        identity_guard_mode=guard_mode,
        adjudicator=(
            OpenAIIdentityAdjudicator(client, model=canonicalization_model)
            if guard_mode != "off" else None
        ),
    )

    before = len(store)
    output_path, review_path, stats = consolidate_from_ledger(
        args.rows_csv, canonicalizer=engine, output_csv=args.output,
        decision_ledger=ledger,
    )
    audit = ledger.commit()
    store.save()
    audit_path = _write_commit_audit(output_path, audit)

    usage = client.get_batch_usage()
    print("\n=== Ingredient consolidation complete ===")
    print(f"  input ledger      : {args.rows_csv}")
    print(f"  clean output      : {output_path}")
    print(f"  please-check list : {review_path}")
    print(f"  commit audit      : {audit_path}")
    print(f"  products (ASINs)  : {stats['asins']}")
    print(f"  canonical columns : {stats['canonical_columns']}")
    print(f"  items to review   : {stats['review_items']}")
    guard_flags = sum(1 for row in audit.rows if row["guard_rule"])
    print(f"  decisions: {audit.committed} committed, {audit.rejected} rejected "
          f"({stats['rows_dropped_by_rejection']} rows dropped)")
    print(f"  identity guards   : mode={guard_mode}, {guard_flags} decision(s) flagged")
    print(f"  memory: {before} -> {len(store)} canonical names ({args.store})")
    print(f"  estimated API cost: ${usage.estimated_cost_usd:.4f}  (chat calls={usage.chat_calls})")
    return 0


def _write_commit_audit(output_path: Path, audit: CommitAudit) -> Path:
    path = output_path.with_name(f"{output_path.stem}_store_commit_audit.csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CommitAudit.fieldnames())
        writer.writeheader()
        writer.writerows(audit.rows)
    return path


if __name__ == "__main__":
    raise SystemExit(main())
