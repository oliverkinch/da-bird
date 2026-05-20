"""Generate da-bird tasks from Danmarks Statistik tables using an LLM.

For each configured DST table the script:
  1. Downloads data via the DST statbank REST API and stores it in SQLite.
  2. Sends the schema + sample rows to an LLM and asks for N question/SQL pairs.
  3. Validates every gold SQL against the real database (must run, must return rows).
  4. Scaffolds a harbor-compatible task folder for each valid pair.

Usage:
    uv run python scripts/03_generate_dst_tasks.py \
        --out-dir data/da-bird-bench \
        --model o4-mini \
        --n-per-table 10

Boilerplate is copied from:
    data/harbor-datasets/datasets/bird-bench/california_schools__23/
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import textwrap
from pathlib import Path
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Table configurations
# Each entry defines how to pull a manageable slice from the DST API.
# ---------------------------------------------------------------------------

TABLES: list[dict[str, Any]] = [
    {
        "id": "FOLK1A",
        "db_id": "dst_folk1a",
        "title": "Befolkningen den 1. i kvartalet",
        "variables": [
            {"code": "OMRÅDE", "values": ["*"]},
            {"code": "KØN", "values": ["TOT"]},
            {"code": "ALDER", "values": ["IALT"]},
            {"code": "CIVILSTAND", "values": ["TOT"]},
            {"code": "Tid", "values": ["*"]},
        ],
        "table_name": "folk1a",
        "col_types": {"INDHOLD": "INTEGER"},
    },
    {
        "id": "FOLK2",
        "db_id": "dst_folk2",
        "title": "Befolkningen 1. januar efter køn og herkomst",
        "variables": [
            {"code": "KØN", "values": ["*"]},
            {"code": "HERKOMST", "values": ["*"]},
            {"code": "Tid", "values": ["*"]},
        ],
        "table_name": "folk2",
        "col_types": {"INDHOLD": "INTEGER"},
    },
    {
        "id": "AUP01",
        "db_id": "dst_aup01",
        "title": "Fuldtidsledige i pct. af arbejdsstyrken",
        "variables": [
            {"code": "OMRÅDE", "values": ["*"]},
            {"code": "KØN", "values": ["*"]},
            {"code": "Tid", "values": ["*"]},
        ],
        "table_name": "aup01",
        "col_types": {"INDHOLD": "REAL"},
    },
    {
        "id": "INDKP102",
        "db_id": "dst_indkp102",
        "title": "Indkomst i alt efter region, enhed, køn og indkomstinterval",
        "variables": [
            {"code": "REGION", "values": ["*"]},
            {"code": "ENHED", "values": ["*"]},
            {"code": "KOEN", "values": ["*"]},
            {"code": "INDKINTB", "values": ["*"]},
            {"code": "Tid", "values": ["*"]},
        ],
        "table_name": "indkp102",
        "col_types": {"INDHOLD": "INTEGER"},
    },
    {
        "id": "BY1",
        "db_id": "dst_by1",
        "title": "Befolkningen 1. januar efter byområder, alder og køn",
        "variables": [
            {"code": "BYER", "values": ["*"]},
            {"code": "ALDER", "values": ["IALT"]},
            {"code": "KØN", "values": ["*"]},
            {"code": "Tid", "values": ["*"]},
        ],
        "table_name": "by1",
        "col_types": {"INDHOLD": "INTEGER"},
    },
]

# ---------------------------------------------------------------------------
# Boilerplate paths
# ---------------------------------------------------------------------------

BOILERPLATE_SRC = Path(
    "data/harbor-datasets/datasets/bird-bench/california_schools__23"
)
BOILERPLATE_FILES = [
    "tests/evaluate.py",
    "tests/test.sh",
    "environment/Dockerfile",
]

INSTRUCTION_TEMPLATE = """\
Du har fået et naturligt sprog-spørgsmål og en SQLite-database. Din opgave er at skrive en SQL-forespørgsel, der besvarer spørgsmålet.

Databasefil: `/app/db.sqlite`
Du kan bruge `sqlite3` eller Python til at undersøge skema og data. Undlad at ændre databasen.

Spørgsmål:
{question}

Bevismateriale:
{evidence}

Skema:
{schema}

Output:
Skriv KUN SQL-forespørgslen til `/app/answer.sql`. Inkluder ikke kodeafgrænsere, kommentarer eller forklaringer.
"""

TASK_TOML_TEMPLATE = """\
# Refer to https://harborframework.com/docs/task-format for more details.

version = "1.0"

[metadata]
author_name = "da-bird"
author_email = ""
difficulty = "{difficulty}"
category = "database"
tags = ["nl2sql", "da-bird", "text-to-sql", "database", "sql", "dansk", "dst", "{difficulty}"]
source = "Danmarks Statistik / {dst_table}"
db_id = "{db_id}"
source_id = "{source_id}"

[verifier]
timeout_sec = 1800.0

[agent]
timeout_sec = 3600.0

[environment]
build_timeout_sec = 1800.0
cpus = 1
memory_mb = 4096
storage_mb = 8192
"""

SOLVE_SH_TEMPLATE = """\
#!/bin/bash
set -euo pipefail

cat > /app/answer.sql <<'SQL'
{sql}
SQL
"""

LLM_SYSTEM = """\
Du er ekspert i SQL og Danmarks Statistik data. Du genererer spørgsmål-SQL par til et dansk NL2SQL benchmark (da-bird).

Regler:
- Spørgsmål og bevismateriale skal være på dansk.
- SQL skal køre korrekt mod den angivne SQLite-database.
- Returner KUN et JSON-array – ingen markdown, ingen forklaringer.
- Fordel sværhedsgraden jævnt: ca. 1/3 easy, 1/3 medium, 1/3 hard.
  - easy: simpel SELECT med WHERE på én tabel
  - medium: aggregering (COUNT, SUM, AVG, GROUP BY), eller subforespørgsel
  - hard: kompleks aggregering, ORDER BY + LIMIT, flere betingelser, eller vinduesfunktioner
- SQL må kun bruge tabeller og kolonner der faktisk eksisterer i skemaet.
- Undgå at spørge om specifikke rækker der sandsynligvis ikke eksisterer.
- Kolonnenavne i SQL skal matche skemaet præcist (store/små bogstaver).
"""

LLM_SYSTEM_HARD = """\
Du er ekspert i SQL og Danmarks Statistik data. Du genererer KUN svære (hard) spørgsmål-SQL par til et dansk NL2SQL benchmark (da-bird).

Regler:
- Spørgsmål og bevismateriale skal være på dansk.
- SQL skal køre korrekt mod den angivne SQLite-database (SQLite).
- Returner KUN et JSON-objekt – ingen markdown, ingen forklaringer.
- Alle par skal have difficulty = "hard".
- "Hard" betyder SQL der bruger mindst ét af følgende mønstre:
    * LAG() eller LEAD() vinduesfunktion til at beregne ændring fra periode til periode
    * RANK() eller ROW_NUMBER() til rangering
    * Korreleret subforespørgsel (subquery der refererer til ydre forespørgsel)
    * Self-join (tabellen joines med sig selv)
    * CTE (WITH-klausul) med mindst to trin
    * HAVING-klausul kombineret med GROUP BY og subforespørgsel
- SQL må kun bruge tabeller og kolonner der faktisk eksisterer i skemaet.
- Kolonnenavne i SQL skal matche skemaet præcist (store/små bogstaver).
- Spørgsmålet skal lyde naturligt på dansk og ikke afsløre SQL-strukturen.
"""

LLM_USER_TEMPLATE = """\
Tabel: {title}

Skema:
{schema}

Eksempelrækker (op til 10):
{sample_rows}

Unikke værdier per kolonne (op til 8 vist):
{unique_vals}

Generer {n} spørgsmål-SQL par. Returner et JSON-objekt med én nøgle "pairs" der indeholder et array. Hvert element i arrayet skal have præcis disse fire nøgler:
  "question_da"  – det danske spørgsmål
  "evidence_da"  – kort hint/forklaring til modellen (dansk)
  "gold_sql"     – korrekt SQL-forespørgsel
  "difficulty"   – "easy", "medium" eller "hard"

Eksempel på korrekt format:
{{"pairs": [{{"question_da": "...", "evidence_da": "...", "gold_sql": "SELECT ...", "difficulty": "easy"}}]}}
"""


# ---------------------------------------------------------------------------
# DST API
# ---------------------------------------------------------------------------

def fetch_dst_csv(table_id: str, variables: list[dict]) -> str:
    resp = requests.post(
        "https://api.statbank.dk/v1/data",
        data=json.dumps(
            {"table": table_id, "format": "CSV", "lang": "da", "variables": variables},
            ensure_ascii=False,
        ).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.text.lstrip("\ufeff")


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

def csv_to_sqlite(csv_text: str, db_path: Path, table_name: str, col_types: dict[str, str]) -> int:
    import csv, io
    rows = list(csv.DictReader(io.StringIO(csv_text), delimiter=";"))
    if not rows:
        raise ValueError("Empty CSV")

    cols = list(rows[0].keys())
    col_defs = ", ".join(
        f'"{c}" {col_types.get(c, "TEXT")}' for c in cols
    )
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(f"CREATE TABLE IF NOT EXISTS {table_name} ({col_defs})")

    for r in rows:
        vals = []
        for c in cols:
            v = r[c]
            ct = col_types.get(c, "TEXT")
            if ct == "INTEGER":
                vals.append(int(v) if v.strip().lstrip("-").isdigit() else None)
            elif ct == "REAL":
                try:
                    vals.append(float(v.replace(",", ".")))
                except ValueError:
                    vals.append(None)
            else:
                vals.append(v)
        placeholders = ", ".join(["?"] * len(cols))
        conn.execute(f"INSERT INTO {table_name} VALUES ({placeholders})", vals)

    conn.commit()
    count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
    conn.close()
    return count


def get_schema(db_path: Path, table_name: str) -> str:
    conn = sqlite3.connect(db_path)
    schema = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).fetchone()[0]
    conn.close()
    return schema


def get_sample_rows(db_path: Path, table_name: str, n: int = 10) -> str:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"SELECT * FROM {table_name} LIMIT {n}").fetchall()
    if not rows:
        conn.close()
        return "(ingen rækker)"
    headers = rows[0].keys()
    lines = [" | ".join(str(h) for h in headers)]
    lines.append("-" * len(lines[0]))
    for r in rows:
        lines.append(" | ".join(str(r[h]) for h in headers))
    conn.close()
    return "\n".join(lines)


def get_unique_vals(db_path: Path, table_name: str, limit: int = 8) -> str:
    conn = sqlite3.connect(db_path)
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table_name})").fetchall()]
    lines = []
    for col in cols:
        vals = [
            str(r[0])
            for r in conn.execute(
                f'SELECT DISTINCT "{col}" FROM {table_name} LIMIT {limit}'
            ).fetchall()
        ]
        lines.append(f'  {col}: {", ".join(vals)}')
    conn.close()
    return "\n".join(lines)


def validate_sql(db_path: Path, sql: str) -> tuple[bool, str]:
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(sql).fetchall()
        conn.close()
        if not rows:
            return False, "Ingen resultater"
        return True, ""
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

def generate_pairs(client, model: str, title: str, schema: str, sample: str, unique: str, n: int, only_difficulty: str | None = None) -> list[dict]:
    system = LLM_SYSTEM_HARD if only_difficulty == "hard" else LLM_SYSTEM
    user_msg = LLM_USER_TEMPLATE.format(
        title=title, schema=schema, sample_rows=sample, unique_vals=unique, n=n
    )
    kwargs = dict(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
        response_format={"type": "json_object"},
    )

    resp = client.chat.completions.create(**kwargs)
    raw = resp.choices[0].message.content
    data = json.loads(raw)
    # handle {"pairs": [...]} or [...]
    if isinstance(data, list):
        return data
    for v in data.values():
        if isinstance(v, list):
            return v
    return []


# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------

def scaffold_task(
    out_dir: Path,
    db_id: str,
    task_id: int,
    dst_table: str,
    db_path: Path,
    schema: str,
    pair: dict,
) -> Path:
    folder = out_dir / f"{db_id}__{task_id}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "tests").mkdir(exist_ok=True)
    (folder / "solution").mkdir(exist_ok=True)
    (folder / "environment").mkdir(exist_ok=True)

    # Copy boilerplate
    for rel in BOILERPLATE_FILES:
        src = BOILERPLATE_SRC / rel
        dst = folder / rel
        shutil.copy2(src, dst)

    # db.sqlite
    shutil.copy2(db_path, folder / "environment" / "db.sqlite")

    q = pair["question_da"]
    ev = pair.get("evidence_da", "")
    sql = pair["gold_sql"].strip()
    diff = pair.get("difficulty", "medium")

    # instruction.md
    (folder / "instruction.md").write_text(
        INSTRUCTION_TEMPLATE.format(question=q, evidence=ev, schema=schema),
        encoding="utf-8",
    )

    # gold.sql
    (folder / "tests" / "gold.sql").write_text(sql, encoding="utf-8")

    # solve.sh
    (folder / "solution" / "solve.sh").write_text(
        SOLVE_SH_TEMPLATE.format(sql=sql), encoding="utf-8"
    )
    (folder / "solution" / "solve.sh").chmod(0o755)

    # task.toml
    (folder / "task.toml").write_text(
        TASK_TOML_TEMPLATE.format(
            difficulty=diff,
            dst_table=dst_table,
            db_id=db_id,
            source_id=str(task_id),
        ),
        encoding="utf-8",
    )

    return folder


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, default=Path("data/da-bird-bench"))
    parser.add_argument("--model", default="o4-mini")
    parser.add_argument("--n-per-table", type=int, default=10)
    parser.add_argument("--only-difficulty", choices=["easy", "medium", "hard"], default=None)
    parser.add_argument("--backend", default="openai")
    args = parser.parse_args()

    if args.backend == "openai":
        from openai import OpenAI
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            sys.exit("OPENAI_API_KEY not set")
        client = OpenAI(api_key=api_key)
    else:
        sys.exit(f"Unsupported backend: {args.backend}")

    tmp_dir = Path("/tmp/da-bird-dbs")
    tmp_dir.mkdir(exist_ok=True)

    for tbl in TABLES:
        print(f"\n{'='*60}")
        print(f"Table: {tbl['id']} — {tbl['title']}")
        print("  Fetching data from DST...", flush=True)

        db_path = tmp_dir / f"{tbl['db_id']}.sqlite"
        if db_path.exists():
            db_path.unlink()

        try:
            csv_text = fetch_dst_csv(tbl["id"], tbl["variables"])
            n_rows = csv_to_sqlite(csv_text, db_path, tbl["table_name"], tbl.get("col_types", {}))
            print(f"  {n_rows} rows loaded into SQLite")
        except Exception as e:
            print(f"  ERROR fetching/loading: {e} — skipping table")
            continue

        schema = get_schema(db_path, tbl["table_name"])
        sample = get_sample_rows(db_path, tbl["table_name"])
        unique = get_unique_vals(db_path, tbl["table_name"])

        print(f"  Asking {args.model} for {args.n_per_table} question/SQL pairs...", flush=True)
        # Ask for extra pairs to have buffer for validation failures
        try:
            pairs = generate_pairs(
                client, args.model, tbl["title"], schema, sample, unique,
                n=args.n_per_table + 5,
                only_difficulty=args.only_difficulty,
            )
        except Exception as e:
            print(f"  ERROR from LLM: {e} — skipping table")
            continue

        print(f"  LLM returned {len(pairs)} pairs, validating...")

        accepted = 0
        task_id_start = _next_task_id(args.out_dir, tbl["db_id"])

        for pair in pairs:
            if accepted >= args.n_per_table:
                break

            sql = pair.get("gold_sql", "").strip()
            if not sql:
                continue

            if args.only_difficulty and pair.get("difficulty") != args.only_difficulty:
                continue

            ok, err = validate_sql(db_path, sql)
            if not ok:
                print(f"    [SKIP] {pair.get('question_da','?')[:60]!r} — {err}")
                continue

            task_id = task_id_start + accepted
            folder = scaffold_task(
                args.out_dir,
                tbl["db_id"],
                task_id,
                tbl["id"],
                db_path,
                schema,
                pair,
            )
            diff = pair.get("difficulty", "?")
            print(f"    [{diff:6}] {folder.name}: {pair['question_da'][:70]}")
            accepted += 1

        print(f"  Done: {accepted}/{args.n_per_table} tasks created for {tbl['db_id']}")

    print(f"\nAll done. Tasks written to {args.out_dir}/")


def _next_task_id(out_dir: Path, db_id: str) -> int:
    """Return the next available task ID for a given db_id."""
    existing = sorted(out_dir.glob(f"{db_id}__*"))
    if not existing:
        return 1
    ids = []
    for p in existing:
        try:
            ids.append(int(p.name.split("__")[-1]))
        except ValueError:
            pass
    return max(ids) + 1 if ids else 1


if __name__ == "__main__":
    main()
