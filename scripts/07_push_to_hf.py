"""Merge da-bird-bench + dst-v2-hard and push to HuggingFace as oliverkinch/da-bird.

Steps:
  1. Copy da-bird-bench tasks to data/merged/ with bird_ prefix
  2. Copy dst-v2-hard tasks to data/merged/ as-is
  3. Update tags in bird_*/task.toml (add "da-bird", "dansk"; drop "database", "programming")
  4. Write README.md dataset card
  5. Upload to HuggingFace

Usage:
    HF_TOKEN=hf_... uv run python scripts/07_push_to_hf.py
    HF_TOKEN=hf_... uv run python scripts/07_push_to_hf.py --skip-merge  # if data/merged/ already exists
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

MERGED_DIR = Path("data/merged")
BIRD_SRC = Path("data/da-bird-bench")
DST_SRC = Path("data/dst-v2-hard")
HF_REPO = "oliverkinch/da-bird"
HF_PATH_IN_REPO = ""

README = """\
---
license: cc-by-4.0
language:
- da
task_categories:
- table-question-answering
tags:
- nl2sql
- text-to-sql
- danish
- bird-bench
- dst
- sql
pretty_name: "DA-BIRD: Danish NL2SQL Benchmark"
size_categories:
- 100<n<1K
---

# DA-BIRD: Danish NL2SQL Benchmark

DA-BIRD is a Danish text-to-SQL benchmark for evaluating large language models on natural language to SQL generation. All tasks are in Danish and use SQLite databases. The dataset is designed for use with the [Harbor evaluation framework](https://github.com/harborframework/harbor).

It combines two corpora — **363 tasks** across **22 unique databases**:

| Corpus | Tasks | Databases | Language | Difficulty |
|---|---|---|---|---|
| `bird_*` | 150 | 11 | Danish (translated) | easy / medium / hard |
| `dst_*` | 213 | 11 | Danish (original) | medium / hard |

---

## Corpora

### bird_* — BIRD-SQL (translated to Danish)

The `bird_*` tasks are a Danish translation of a subset of the [BIRD-SQL](https://bird-bench.github.io/) development set, originally published by Li et al. (2024). The original benchmark contains 12,751 question-SQL pairs across 95 databases covering 37+ professional domains.

The tasks used here are sourced from [`harborframework/harbor-datasets`](https://huggingface.co/datasets/harborframework/harbor-datasets/tree/main/datasets/bird-bench), where they have been packaged as self-contained Harbor evaluation tasks with Danish-translated instructions.

**Databases:** california_schools, card_games, codebase_community, debit_card_specializing, european_football_2, financial, formula_1, student_club, superhero, thrombosis_prediction, toxicology

**Difficulty distribution:**
- Easy: 84 tasks
- Medium: 43 tasks
- Hard: 23 tasks

**Evaluation:** Result-set comparison (execution accuracy). A task is correct if the predicted SQL returns an identical result set to the gold SQL.

---

### dst_* — Danmarks Statistik (original Danish tasks)

The `dst_*` tasks are purpose-built Danish NL2SQL tasks created from open statistical data published by [Danmarks Statistik (DST)](https://www.statbank.dk/) via their public REST API. All databases, questions, and gold SQL are original to this benchmark.

**Databases and DST source tables:**

| db_id | DST table | Description | Tasks |
|---|---|---|---|
| dst_folk1a | FOLK1A | Population by municipality, quarterly | 30 |
| dst_folk1a_civ | FOLK1A | Population by municipality and civil status | 30 |
| dst_folk2 | FOLK2 | Population by gender and origin, annual | 30 |
| dst_indkp102 | INDKP102 | Income by region, gender and income bracket | 30 |
| dst_aup01 | AUP01 | Full-time unemployment by municipality | 30 |
| dst_folk_bol | FOLK1A + BOL101 | Population joined with housing (multi-table) | 13 |
| dst_straf10 | STRAF10 | Reported crimes by type, quarterly | 10 |
| dst_aku110k | AKU110K | Labour market participation by status, age, gender | 10 |
| dst_doda1 | DODA1 | Deaths by cause, age and gender, annual | 10 |
| dst_pris01 | PRIS01 | Consumer price index by commodity group | 10 |
| dst_bol101 | BOL101 | Occupied dwellings by municipality and type | 10 |

**Generation methodology (SQL-first):**

Tasks were generated using a SQL-first pipeline (source: [oliverkinch/da-bird](https://github.com/oliverkinch/da-bird)):

1. A SQL pattern is sampled for the target difficulty (window functions, CTEs, correlated subqueries, JOINs, etc.)
2. An LLM (GPT-4.5) generates a concrete SQL query following the pattern, given the real schema and sample data
3. The SQL is executed against the actual DST database — invalid or empty-result queries are discarded
4. The LLM writes a natural Danish question whose answer is the actual query results
5. A coherence check asks the LLM whether a data analyst would unambiguously recognise the results as the answer to the question — tasks that fail are discarded

This SQL-first approach consistently outperforms question-first generation: in internal evaluation, SQL-first tasks scored a mean execution accuracy of 0.780 vs. 0.518 for question-first tasks when evaluated with the same agent.

**Difficulty:**
- Medium (200 tasks): single-table queries — window functions (LAG/RANK), CTEs, correlated subqueries, grouped aggregations
- Hard (13 tasks): multi-table JOIN queries across `folk1a` and `bol101`, requiring cross-table aggregation and filtering

---

## Task format

Each task is a self-contained folder compatible with the [Harbor evaluation framework](https://github.com/harborframework/harbor):

```
{task_id}/
├── task.toml           # metadata: difficulty, db_id, tags, source
├── instruction.md      # Danish NL question + schema + output instruction
├── tests/
│   ├── gold.sql        # reference SQL query
│   ├── evaluate.py     # result-set comparison evaluator
│   └── test.sh
├── solution/
│   └── solve.sh        # reference solution (writes gold SQL to /app/answer.sql)
└── environment/
    ├── Dockerfile
    └── db.sqlite       # SQLite database
```

`instruction.md` is written entirely in Danish and asks the agent to write a SQL query to `/app/answer.sql`. The agent has access to `sqlite3` and Python to inspect the database.

---

## Usage with Harbor

```bash
# Download the dataset
huggingface-cli download oliverkinch/da-bird --repo-type dataset --local-dir data/da-bird

# Run evaluation (requires Harbor)
uv run harbor run -p data/da-bird -a terminus-2 -m openai/o4-mini
```

---

## Citation

If you use the BIRD-SQL portion of this dataset, please cite:

```bibtex
@inproceedings{li2024bird,
  title={Can LLM Already Serve as A Database Interface? A Big Bench for Large-Scale Database Grounded Text-to-SQLs},
  author={Li, Jinyang and Hui, Binyuan and Qu, Ge and Yang, Jiaxi and Li, Binhua and Li, Bowen and Wang, Bailin and Qin, Bowen and Geng, Ruiying and Huo, Nan and others},
  booktitle={Advances in Neural Information Processing Systems},
  year={2024}
}
```

---

## License

- `bird_*` tasks: [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/) (inherited from BIRD-SQL)
- `dst_*` tasks: [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
- DST source data: published under [Danmarks Statistik's open data terms](https://www.dst.dk/en/OmDS/betingelser)
"""


def merge(skip_merge: bool) -> None:
    if skip_merge:
        if not MERGED_DIR.exists():
            sys.exit(f"--skip-merge set but {MERGED_DIR} does not exist")
        print(f"Skipping merge — using existing {MERGED_DIR}/")
        return

    if MERGED_DIR.exists():
        print(f"Removing existing {MERGED_DIR}/")
        shutil.rmtree(MERGED_DIR)
    MERGED_DIR.mkdir(parents=True)

    # Copy bird tasks with bird_ prefix
    bird_dirs = sorted(p for p in BIRD_SRC.iterdir() if p.is_dir())
    print(f"Copying {len(bird_dirs)} bird tasks...")
    for src in bird_dirs:
        shutil.copytree(src, MERGED_DIR / f"bird_{src.name}")

    # Copy dst tasks as-is
    dst_dirs = sorted(p for p in DST_SRC.iterdir() if p.is_dir())
    print(f"Copying {len(dst_dirs)} dst tasks...")
    for src in dst_dirs:
        shutil.copytree(src, MERGED_DIR / src.name)

    print(f"Merged: {len(bird_dirs) + len(dst_dirs)} tasks in {MERGED_DIR}/")


def update_bird_tags() -> None:
    bird_tomls = sorted((MERGED_DIR).glob("bird_*/task.toml"))
    print(f"Updating tags in {len(bird_tomls)} bird task.toml files...")

    # Replace the tags line — pattern matches any difficulty at the end
    tag_pattern = re.compile(
        r'tags\s*=\s*\[.*?\]',
        re.DOTALL,
    )

    for toml_path in bird_tomls:
        content = toml_path.read_text(encoding="utf-8")

        # Extract the current difficulty from the tags (last quoted value before ])
        diff_match = re.search(r'"(easy|medium|hard)"\s*\]', content)
        difficulty = diff_match.group(1) if diff_match else "medium"

        new_tags = f'tags = ["nl2sql", "da-bird", "bird", "text-to-sql", "sql", "dansk", "{difficulty}"]'
        new_content = tag_pattern.sub(new_tags, content)

        if new_content != content:
            toml_path.write_text(new_content, encoding="utf-8")

    print("  Done.")


def write_readme() -> None:
    readme_path = MERGED_DIR / "README.md"
    readme_path.write_text(README, encoding="utf-8")
    print(f"Written {readme_path}")


def upload(token: str, delete_datasets_folder: bool = False) -> None:
    from huggingface_hub import HfApi

    api = HfApi(token=token)

    print(f"Creating repo {HF_REPO} (if not exists)...")
    api.create_repo(HF_REPO, repo_type="dataset", exist_ok=True, private=False)

    if delete_datasets_folder:
        print("Deleting existing datasets/ folder from HF repo...")
        try:
            api.delete_folder(
                path_in_repo="datasets",
                repo_id=HF_REPO,
                repo_type="dataset",
                commit_message="Remove datasets/ nesting — task folders move to root",
            )
            print("  Deleted.")
        except Exception as e:
            print(f"  Could not delete (may not exist): {e}")

    task_count = sum(1 for p in MERGED_DIR.iterdir() if p.is_dir())
    path_label = HF_PATH_IN_REPO or "(repo root)"
    print(f"Uploading {task_count} task folders + README to {HF_REPO}/{path_label} ...")
    print("Using upload_large_folder — resumable, handles >20 GB. This will take a while.")

    api.upload_large_folder(
        repo_id=HF_REPO,
        repo_type="dataset",
        folder_path=str(MERGED_DIR),
    )
    print(f"\nDone! https://huggingface.co/datasets/{HF_REPO}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-merge", action="store_true",
                        help="Skip building data/merged/ (use existing)")
    parser.add_argument("--no-upload", action="store_true",
                        help="Build data/merged/ but do not upload to HF")
    parser.add_argument("--delete-datasets-folder", action="store_true",
                        help="Delete existing datasets/ folder from HF repo before uploading")
    args = parser.parse_args()

    token = os.environ.get("HF_TOKEN")
    if not args.no_upload and not token:
        sys.exit("HF_TOKEN not set. Export it or use --no-upload to only build the merged dir.")

    merge(args.skip_merge)
    update_bird_tags()
    write_readme()

    if args.no_upload:
        print(f"\nMerged dataset ready at {MERGED_DIR}/ — skipping upload (--no-upload)")
    else:
        upload(token, delete_datasets_folder=args.delete_datasets_folder)


if __name__ == "__main__":
    main()
