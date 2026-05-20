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
HF_PATH_IN_REPO = "datasets/da-bird"

README = """\
---
license: cc-by-4.0
language:
- da
task_categories:
- text-to-sql
tags:
- nl2sql
- danish
- bird-bench
- dst
pretty_name: "DA-BIRD: Danish NL2SQL Benchmark"
size_categories:
- 100<n<1K
---

# DA-BIRD: Danish NL2SQL Benchmark

DA-BIRD is a Danish text-to-SQL benchmark for evaluating LLMs on natural language to SQL generation. It combines two corpora:

- **`bird_*` tasks** (151 tasks, 11 databases): Danish-language translations of the [BIRD-SQL](https://bird-bench.github.io/) dev set, covering diverse English-domain databases (sports, finance, community Q&A, etc.)
- **`dst_*` tasks** (213 tasks, 11 databases): Original Danish tasks built from open data from [Danmarks Statistik](https://www.statbank.dk/), including population, housing, crime, labour market, consumer prices, and mortality statistics. Includes both single-table (medium difficulty) and multi-table JOIN tasks (hard difficulty).

**Total: 364 tasks** across 22 unique databases.

## Format

Each task is a self-contained folder compatible with the [Harbor evaluation framework](https://harborframework.com/):

```
{task_id}/
├── task.toml         # metadata: difficulty, db_id, tags, source
├── instruction.md    # Danish NL question + schema + output instructions
├── tests/
│   ├── gold.sql      # reference SQL query
│   ├── evaluate.py   # result-set comparison evaluator
│   └── test.sh
├── solution/
│   └── solve.sh      # reference solution script
└── environment/
    ├── Dockerfile
    └── db.sqlite     # SQLite database
```

## Usage with Harbor

```bash
uv run harbor run -p datasets/da-bird -a terminus-2 -m openai/o4-mini
```

## License

CC-BY 4.0. DST data is published under [Danmarks Statistik's open data terms](https://www.dst.dk/en/OmDS/betingelser).
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


def upload(token: str) -> None:
    from huggingface_hub import HfApi

    api = HfApi(token=token)

    print(f"Creating repo {HF_REPO} (if not exists)...")
    api.create_repo(HF_REPO, repo_type="dataset", exist_ok=True, private=False)

    task_count = sum(1 for p in MERGED_DIR.iterdir() if p.is_dir())
    print(f"Uploading {task_count} task folders + README to {HF_REPO}/{HF_PATH_IN_REPO} ...")
    print("This may take 30–90 minutes for ~21 GB. The call is resumable if interrupted.")

    api.upload_folder(
        repo_id=HF_REPO,
        repo_type="dataset",
        folder_path=str(MERGED_DIR),
        path_in_repo=HF_PATH_IN_REPO,
        commit_message=f"Add merged da-bird-bench + dst-v2-hard ({task_count} tasks)",
    )
    print(f"\nDone! https://huggingface.co/datasets/{HF_REPO}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-merge", action="store_true",
                        help="Skip building data/merged/ (use existing)")
    parser.add_argument("--no-upload", action="store_true",
                        help="Build data/merged/ but do not upload to HF")
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
        upload(token)


if __name__ == "__main__":
    main()
