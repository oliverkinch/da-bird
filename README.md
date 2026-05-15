# da-bird

Small first-draft utility for downloading BIRD benchmark data from Hugging Face.

## Project context

This work starts from the Harbor dataset snapshot and creates a Danish equivalent by translating instruction files while keeping the underlying task/data structure aligned with the original benchmark format.

## Requirements

- Python 3.13+
- `uv` (recommended) or `pip`

## Setup

```bash
uv sync
```

## Usage

Default download (BIRD subset only):

```bash
uv run python download.py
```

Custom output directory:

```bash
uv run python download.py --local-dir ./data-source
```

Custom include patterns (repeat flag):

```bash
uv run python download.py \
	--allow-pattern "datasets/bird-bench/**" \
	--allow-pattern "datasets/another-subset/**"
```

## Notes

- `data/`, `jobs/`, and downloaded dataset folders are ignored in git.
- This repo is currently an early draft and may change structure.
