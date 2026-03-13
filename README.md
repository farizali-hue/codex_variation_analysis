# Amazon Product Scraper

Selenium scraper for Amazon PDP pages with retrieval-first extraction driven by `asin + required_fields`.

## Documentation

- Architecture: `docs/SYSTEM_ARCHITECTURE.md`
- Developer walkthrough: `docs/CODEBASE_GUIDE.md`

## What is implemented
- Loads each Amazon PDP once with Selenium using a New York delivery ZIP context.
- Normalizes title, facts/details rows, bullets, description text, and selected variations into chunk records.
- Uses OpenAI embeddings to rank the most relevant chunks for each requested field.
- Uses an OpenAI chat model to extract structured field values from retrieved evidence only.
- Writes CSV output with `<field>`, `<field>_source`, `<field>_confidence`, and `<field>_evidence`.

## Input format
Primary flow:
```csv
asin,required_fields
B0B53Y7Z7D,"serving_size,caffeinated,caffeine_per_serving,roast_level"
```

Optional metadata:
```csv
asin,product_type,required_fields
B0B53Y7Z7D,mushroom_coffee,"serving_size,caffeinated,caffeine_per_serving,roast_level"
```

`required_fields` accepts comma, pipe, or semicolon separators.

## Required environment
Set these before running:
- `OPENAI_API_KEY`
- `OPENAI_EMBEDDING_MODEL`
- `OPENAI_CHAT_MODEL`

The process fails fast at startup if any of them are missing.

## Run
```bash
pip install -r requirements.txt
python run_mushroom_batch.py
```

Headless mode:
```bash
python run_mushroom_batch.py --headless
```

Default input:
- `inputs/mushroom_coffee_asins.csv`

Default output:
- `outputs/mushroom_coffee_results.csv`

## Repository map
- `run_mushroom_batch.py`: CLI entrypoint and batch runner
- `amazon_scraper/scraper.py`: Selenium orchestration and end-to-end job execution
- `amazon_scraper/html_extractors.py`: DOM parsing and page-state detection
- `amazon_scraper/chunking.py`: converts page content into retrieval chunks
- `amazon_scraper/retrieval.py`: embeddings, cache, and chunk ranking
- `amazon_scraper/extraction.py`: field prompts, JSON parsing, normalization
- `amazon_scraper/csv_export.py`: final CSV writing

## Notes
- Embeddings are cached locally in `.cache/embeddings.json`.
- `product_type` is metadata only. Extraction is driven entirely by `required_fields`.
