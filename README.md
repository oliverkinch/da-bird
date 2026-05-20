# da-bird

Tools for building and evaluating **DA-BIRD**, a Danish text-to-SQL benchmark. The dataset is published at [oliverkinch/da-bird](https://huggingface.co/datasets/oliverkinch/da-bird) on HuggingFace.

## Dataset

DA-BIRD combines two corpora (363 tasks, 22 databases):

| Corpus | Tasks | Source | Difficulty |
|---|---|---|---|
| `bird_*` | 150 | Danish translations of [BIRD-SQL](https://bird-bench.github.io/) dev set | easy / medium / hard |
| `dst_*` | 213 | Original tasks from [Danmarks Statistik](https://www.statbank.dk/) open data | medium / hard |

## Setup

```bash
uv sync
```

## Scripts

**`scripts/generate_dst_tasks.py`** — SQL-first generation pipeline for DST databases. Generates SQL from a difficulty pattern, executes it against real DST data, then writes a Danish question from the actual results.

```bash
uv run python scripts/generate_dst_tasks.py \
  --out-dir data/dst \
  --model gpt-5.5 \
  --n-per-table 10 \
  --only-difficulty hard
```

**`scripts/build_multitable_db.py`** — Builds a two-table SQLite DB (FOLK1A + BOL101) and generates hard JOIN-based tasks.

```bash
uv run python scripts/build_multitable_db.py \
  --out-dir data/dst \
  --model gpt-5.5 \
  --n-tasks 10
```

**`scripts/push_to_hf.py`** — Merges `data/da-bird-bench/` and `data/dst/` into a staging directory and pushes to HuggingFace.

```bash
HF_TOKEN=hf_... uv run python scripts/push_to_hf.py
```

## Evaluation

```bash
uv run harbor run -p data/da-bird -a terminus-2 -m openai/o4-mini
```

Requires [Harbor](https://github.com/harborframework/harbor) and an OpenAI API key (`--ae OPENAI_API_KEY=$OPENAI_API_KEY`).
