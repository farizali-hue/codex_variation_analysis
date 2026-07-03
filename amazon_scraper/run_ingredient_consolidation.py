"""CLI: canonicalize a supplement-facts ledger into clean ingredient columns.

This is the ONLY entry point that calls the real OpenAI API. Run it yourself with your
own credentials (same OPENAI_API_KEY the scraper uses):

    python -m amazon_scraper.run_ingredient_consolidation outputs/pumpkin_seed_oil_20260615T183916Z_supplement_facts_rows.csv

It writes:
  <name>_canonical.csv              -- wide table, one column per canonical ingredient
  <name>_canonical_needs_review.csv -- items it would not guess (for your review)

and updates the shared memory at .cache/canonical_ingredients_store.json so repeats are
free next time. Prints a summary including the estimated API cost for the run.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .canonical_store import CanonicalStore
from .ingredient_canonicalizer import IngredientCanonicalizer, OpenAIIngredientLLM
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
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(message)s")

    config = OpenAIConfig.from_env()
    client = OpenAIClient(config)
    client.reset_batch_usage()
    store = CanonicalStore.load(args.store)
    engine = IngredientCanonicalizer(
        store=store,
        llm=OpenAIIngredientLLM(client),
        confidence_threshold=args.confidence_threshold,
    )

    before = len(store)
    output_path, review_path, stats = consolidate_from_ledger(
        args.rows_csv, canonicalizer=engine, output_csv=args.output,
    )
    store.save()

    usage = client.get_batch_usage()
    print("\n=== Ingredient consolidation complete ===")
    print(f"  input ledger      : {args.rows_csv}")
    print(f"  clean output      : {output_path}")
    print(f"  please-check list : {review_path}")
    print(f"  products (ASINs)  : {stats['asins']}")
    print(f"  canonical columns : {stats['canonical_columns']}")
    print(f"  items to review   : {stats['review_items']}")
    print(f"  memory: {before} -> {len(store)} canonical names ({args.store})")
    print(f"  estimated API cost: ${usage.estimated_cost_usd:.4f}  (chat calls={usage.chat_calls})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
