"""SQL-first da-bird task generation.

Generation flow per task:
  1. Pick a SQL pattern matching the target difficulty.
  2. Ask LLM to write SQL that follows the pattern (given schema + sample data).
  3. Execute the SQL — if it errors or returns no rows, retry with next pattern.
  4. Ask LLM to write a natural Danish question whose answer is those results.
  5. Coherence check: ask LLM whether the question is unambiguously answered by the SQL.
  6. Scaffold a harbor task folder.

Usage:
    uv run python scripts/05_generate_dst_tasks_v2.py \
        --out-dir data/dst-v2 \
        --model gpt-5.5 \
        --n-per-table 10
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

import requests

# ---------------------------------------------------------------------------
# SQL patterns by difficulty
# Each pattern is a short instruction + example skeleton shown to the LLM.
# ---------------------------------------------------------------------------

PATTERNS: dict[str, list[dict]] = {
    "easy": [
        {
            "name": "filter",
            "instruction": "Skriv en simpel SELECT der filtrerer på én eller to specifikke værdier i WHERE-klausulen. Brug kun én tabel. Returnér én eller to kolonner.",
            "example": "SELECT col1, col2 FROM tabel WHERE col3 = 'værdi'",
        },
        {
            "name": "comparison",
            "instruction": "Skriv en SELECT der bruger en sammenligningsoperator (>, <, >=, <=) i WHERE-klausulen mod en numerisk kolonne.",
            "example": "SELECT col1 FROM tabel WHERE tal_kolonne > 1000",
        },
        {
            "name": "multi_filter",
            "instruction": "Skriv en SELECT med to eller tre AND-betingelser i WHERE-klausulen på tværs af forskellige kolonner.",
            "example": "SELECT col1 FROM tabel WHERE col2 = 'a' AND col3 = 'b' AND col4 > 0",
        },
    ],
    "medium": [
        {
            "name": "group_by_count",
            "instruction": "Skriv en SELECT med GROUP BY og COUNT eller SUM. Inkludér evt. en HAVING-klausul.",
            "example": "SELECT col1, COUNT(*) AS antal FROM tabel GROUP BY col1 HAVING COUNT(*) > 5",
        },
        {
            "name": "order_limit",
            "instruction": "Skriv en SELECT med aggregering (SUM, AVG, MAX, MIN) per gruppe, sortér med ORDER BY og begræns med LIMIT.",
            "example": "SELECT col1, AVG(tal) AS snit FROM tabel GROUP BY col1 ORDER BY snit DESC LIMIT 5",
        },
        {
            "name": "subquery",
            "instruction": "Skriv en SELECT der bruger en subforespørgsel i WHERE-klausulen til at sammenligne med et aggregat (fx gennemsnit eller max).",
            "example": "SELECT col1, tal FROM tabel WHERE tal > (SELECT AVG(tal) FROM tabel WHERE col2 = 'x')",
        },
    ],
    "hard": [
        {
            "name": "lag_window",
            "instruction": "Skriv en SELECT der bruger LAG() vinduesfunktion til at beregne ændring fra én periode til den næste. Brug en CTE eller subforespørgsel til at filtrere på ændringen.",
            "example": "WITH t AS (SELECT col, val, LAG(val) OVER (PARTITION BY grp ORDER BY tid) AS forrige FROM tabel) SELECT col, val - forrige AS aendring FROM t WHERE forrige IS NOT NULL ORDER BY aendring DESC LIMIT 1",
        },
        {
            "name": "rank_window",
            "instruction": "Skriv en SELECT der bruger RANK() eller ROW_NUMBER() vinduesfunktion til at rangere rækker inden for en gruppe.",
            "example": "SELECT col, val, RANK() OVER (PARTITION BY grp ORDER BY val DESC) AS rang FROM tabel",
        },
        {
            "name": "cte_multistep",
            "instruction": "Skriv en SELECT med mindst to CTEs (WITH-klausuler) der bygger på hinanden. Fx: første CTE aggregerer, anden CTE filtrerer eller rangerer.",
            "example": "WITH agg AS (SELECT grp, AVG(val) AS snit FROM tabel GROUP BY grp), ranked AS (SELECT grp, snit, RANK() OVER (ORDER BY snit DESC) AS rang FROM agg) SELECT grp, snit FROM ranked WHERE rang <= 3",
        },
        {
            "name": "correlated_subquery",
            "instruction": "Skriv en SELECT med en korreleret subforespørgsel der refererer til den ydre forespørgsel.",
            "example": "SELECT col1, val FROM tabel t1 WHERE val > (SELECT AVG(val) FROM tabel t2 WHERE t2.grp = t1.grp)",
        },
        {
            "name": "join_aggregate",
            "instruction": "Skriv en SELECT der joiner to tabeller på en fælles kolonne og aggregerer på tværs af begge. Brug GROUP BY og ORDER BY.",
            "example": "SELECT a.grp, AVG(a.val) AS snit_a, MAX(b.val) AS max_b FROM tabel_a a JOIN tabel_b b ON a.grp = b.grp GROUP BY a.grp ORDER BY snit_a DESC LIMIT 5",
        },
    ],
}

# ---------------------------------------------------------------------------
# DST tables (same as v1)
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
        "id": "FOLK1A",  # second slice: all civil statuses, total gender/age
        "db_id": "dst_folk1a_civ",
        "title": "Befolkningen den 1. i kvartalet efter civilstand",
        "variables": [
            {"code": "OMRÅDE", "values": ["101", "147", "461", "751", "000"]},  # KBH, FRB, Odense, Aarhus, Hele landet
            {"code": "KØN", "values": ["TOT"]},
            {"code": "ALDER", "values": ["IALT"]},
            {"code": "CIVILSTAND", "values": ["*"]},
            {"code": "Tid", "values": ["*"]},
        ],
        "table_name": "folk1a_civ",
        "col_types": {"INDHOLD": "INTEGER"},
    },
    {
        "id": "STRAF10",
        "db_id": "dst_straf10",
        "title": "Anmeldte forbrydelser efter forbrydelsestype og kvartal",
        "variables": [
            {"code": "OVERTRÆD", "values": ["*"]},
            {"code": "Tid", "values": ["*"]},
        ],
        "table_name": "straf10",
        "col_types": {"INDHOLD": "INTEGER"},
    },
    {
        "id": "AKU110K",
        "db_id": "dst_aku110k",
        "title": "Arbejdsmarkedstilknytning efter beskæftigelsesstatus, alder og køn",
        "variables": [
            {"code": "BESKSTATUS", "values": ["*"]},
            {"code": "ALDER", "values": ["*"]},
            {"code": "KØN", "values": ["*"]},
            {"code": "Tid", "values": ["*"]},
        ],
        "table_name": "aku110k",
        "col_types": {"INDHOLD": "REAL"},
    },
    {
        "id": "DODA1",
        "db_id": "dst_doda1",
        "title": "Døde efter dødsårsag, alder og køn",
        "variables": [
            {"code": "ÅRSAG", "values": ["*"]},
            {"code": "ALDER", "values": ["*"]},
            {"code": "KØN", "values": ["*"]},
            {"code": "Tid", "values": ["*"]},
        ],
        "table_name": "doda1",
        "col_types": {"INDHOLD": "INTEGER"},
    },
    {
        "id": "PRIS01",
        "db_id": "dst_pris01",
        "title": "Forbrugerprisindeks efter varegruppe",
        "variables": [
            {"code": "VAREGR", "values": ["*"]},
            {"code": "ENHED", "values": ["100"]},
            {"code": "Tid", "values": ["*"]},
        ],
        "table_name": "pris01",
        "col_types": {"INDHOLD": "REAL"},
    },
    {
        "id": "BOL101",
        "db_id": "dst_bol101",
        "title": "Beboede boliger efter område, anvendelse, udlejningsforhold og opførelsesår",
        "variables": [
            {"code": "OMRÅDE", "values": ["*"]},
            {"code": "BEBO", "values": ["1000"]},
            {"code": "ANVENDELSE", "values": ["*"]},
            {"code": "UDLFORH", "values": ["*"]},
            {"code": "Tid", "values": ["*"]},
        ],
        "table_name": "bol101",
        "col_types": {"INDHOLD": "INTEGER"},
    },
]

# ---------------------------------------------------------------------------
# Boilerplate
# ---------------------------------------------------------------------------

BOILERPLATE_SRC = Path("data/harbor-datasets/datasets/bird-bench/california_schools__23")
BOILERPLATE_FILES = ["tests/evaluate.py", "tests/test.sh", "environment/Dockerfile"]

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
version = "1.0"

[metadata]
author_name = "da-bird-v2"
author_email = ""
difficulty = "{difficulty}"
category = "database"
tags = ["nl2sql", "da-bird", "da-bird-v2", "text-to-sql", "sql", "dansk", "dst", "{difficulty}"]
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

# ---------------------------------------------------------------------------
# DST helpers
# ---------------------------------------------------------------------------

def fetch_dst(table_id: str, variables: list[dict]) -> str:
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


def csv_to_sqlite(csv_text: str, db_path: Path, table_name: str, col_types: dict) -> int:
    import csv, io
    rows = list(csv.DictReader(io.StringIO(csv_text), delimiter=";"))
    if not rows:
        raise ValueError("Empty CSV")
    cols = list(rows[0].keys())
    col_defs = ", ".join(f'"{c}" {col_types.get(c, "TEXT")}' for c in cols)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(f"DROP TABLE IF EXISTS {table_name}")
    conn.execute(f"CREATE TABLE {table_name} ({col_defs})")
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
        conn.execute(f"INSERT INTO {table_name} VALUES ({', '.join(['?']*len(cols))})", vals)
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


def get_sample(db_path: Path, table_name: str, n: int = 8) -> str:
    import io
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"SELECT * FROM {table_name} LIMIT {n}").fetchall()
    conn.close()
    if not rows:
        return "(ingen rækker)"
    buf = io.StringIO()
    headers = rows[0].keys()
    buf.write(" | ".join(headers) + "\n")
    buf.write("-" * 60 + "\n")
    for r in rows:
        buf.write(" | ".join(str(r[h]) for h in headers) + "\n")
    return buf.getvalue()


def get_unique_vals(db_path: Path, table_name: str, limit: int = 8) -> str:
    conn = sqlite3.connect(db_path)
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table_name})").fetchall()]
    lines = []
    for col in cols:
        vals = [str(r[0]) for r in conn.execute(
            f'SELECT DISTINCT "{col}" FROM {table_name} LIMIT {limit}'
        ).fetchall()]
        lines.append(f"  {col}: {', '.join(vals)}")
    conn.close()
    return "\n".join(lines)


def run_sql(db_path: Path, sql: str) -> tuple[list, list, str | None]:
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(sql)
        rows = cur.fetchmany(20)
        cols = [d[0] for d in cur.description] if cur.description else []
        conn.close()
        return cols, [list(r) for r in rows], None
    except Exception as e:
        return [], [], str(e)


# ---------------------------------------------------------------------------
# LLM calls
# ---------------------------------------------------------------------------

def llm(client, model: str, system: str, user: str) -> str:
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
    )
    return resp.choices[0].message.content


def generate_sql(client, model: str, schema: str, sample: str, unique: str, pattern: dict) -> str:
    system = f"""\
Du er en SQL-ekspert. Du skriver KUN SQL-forespørgsler til SQLite-databaser.
Returner et JSON-objekt med én nøgle "sql" der indeholder forespørgslen.
Regler:
- SQL skal bruge tabeller og kolonner der eksisterer i skemaet.
- Kolonnenavne med specialtegn skal omgives af dobbelte anførselstegn.
- SQL skal følge det angivne mønster præcist.
- SQL skal returnere mindst én række med realistiske data.
- Undgå at filtrere på værdier der sandsynligvis ikke eksisterer i data.
"""
    user = f"""\
Skema:
{schema}

Eksempelrækker:
{sample}

Unikke værdier per kolonne:
{unique}

Mønster der skal følges ({pattern['name']}):
{pattern['instruction']}

Eksempel på mønsteret (brug som inspiration, ikke kopier):
{pattern['example']}

Skriv en konkret SQL-forespørgsel der følger mønsteret og returnerer interessante resultater fra denne tabel.
"""
    raw = llm(client, model, system, user)
    data = json.loads(raw)
    return data.get("sql", "").strip()


def generate_question(client, model: str, schema: str, sql: str, cols: list, rows: list) -> dict:
    rows_str = "\n".join(
        " | ".join(str(v) for v in row) for row in rows[:10]
    )
    system = """\
Du er ekspert i at formulere naturlige danske spørgsmål til et NL2SQL benchmark.
Returner et JSON-objekt med nøglerne:
  "question_da"  – naturligt dansk spørgsmål hvis svar er de viste resultater
  "evidence_da"  – kort hint der hjælper modellen (dansk), fx hvilke filterværdier der er relevante
Reglerne:
- Spørgsmålet skal lyde som noget en analytiker ville spørge om.
- Spørgsmålet må ikke afsløre SQL-strukturen (ingen "brug LAG", "lav en CTE" osv.).
- Spørgsmålet skal præcist matche de viste resultater — hverken bredere eller smallere.
- Bevismaterialet skal hjælpe modellen forstå datastrukturen, ikke give SQL-svaret.
"""
    user = f"""\
Skema:
{schema}

SQL der producerede resultaterne:
{sql}

Kolonner: {' | '.join(cols)}
Resultater (op til 10 rækker):
{rows_str}

Skriv et naturligt dansk spørgsmål hvis svar præcist er disse resultater.
"""
    raw = llm(client, model, system, user)
    return json.loads(raw)


def coherence_check(client, model: str, question: str, sql: str, cols: list, rows: list) -> tuple[bool, str]:
    rows_str = "\n".join(" | ".join(str(v) for v in row) for row in rows[:5])
    system = """\
Du er kvalitetssikrer for et NL2SQL benchmark.
Returner et JSON-objekt med nøglerne:
  "ok": true/false — er spørgsmålet entydigt besvaret af SQL-resultaterne?
  "reason": kort forklaring (dansk)
"""
    user = f"""\
Spørgsmål: {question}

SQL: {sql}

Resultater (kolonner: {' | '.join(cols)}):
{rows_str}

Er spørgsmålet entydigt besvaret af disse resultater? Vurdér om en analytiker ville forstå spørgsmålet og genkende resultaterne som svaret.
"""
    raw = llm(client, model, system, user)
    data = json.loads(raw)
    return bool(data.get("ok", False)), data.get("reason", "")


# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------

def scaffold(out_dir: Path, db_id: str, task_id: int, dst_table: str,
             db_path: Path, schema: str, sql: str, question: str,
             evidence: str, difficulty: str) -> Path:
    folder = out_dir / f"{db_id}__{task_id}"
    folder.mkdir(parents=True, exist_ok=True)
    for sub in ("tests", "solution", "environment"):
        (folder / sub).mkdir(exist_ok=True)

    for rel in BOILERPLATE_FILES:
        shutil.copy2(BOILERPLATE_SRC / rel, folder / rel)

    shutil.copy2(db_path, folder / "environment" / "db.sqlite")

    sql = sql.strip()
    (folder / "instruction.md").write_text(
        INSTRUCTION_TEMPLATE.format(question=question, evidence=evidence, schema=schema),
        encoding="utf-8",
    )
    (folder / "tests" / "gold.sql").write_text(sql, encoding="utf-8")
    solve = f"#!/bin/bash\nset -euo pipefail\n\ncat > /app/answer.sql <<'SQL'\n{sql}\nSQL\n"
    (folder / "solution" / "solve.sh").write_text(solve, encoding="utf-8")
    (folder / "solution" / "solve.sh").chmod(0o755)
    (folder / "task.toml").write_text(
        TASK_TOML_TEMPLATE.format(
            difficulty=difficulty, dst_table=dst_table, db_id=db_id, source_id=str(task_id)
        ),
        encoding="utf-8",
    )
    return folder


def _next_id(out_dir: Path, db_id: str) -> int:
    existing = [p for p in out_dir.glob(f"{db_id}__*") if p.is_dir()]
    ids = []
    for p in existing:
        try:
            ids.append(int(p.name.split("__")[-1]))
        except ValueError:
            pass
    return max(ids) + 1 if ids else 1


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

DIFFICULTY_DISTRIBUTION = ["easy", "easy", "easy", "medium", "medium", "medium", "hard", "hard", "hard", "hard"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, default=Path("data/dst-v2"))
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument("--n-per-table", type=int, default=10)
    parser.add_argument("--max-retries", type=int, default=5, help="Retries per task slot")
    parser.add_argument("--only-difficulty", choices=["easy", "medium", "hard"], default=None)
    parser.add_argument("--only-tables", type=str, default=None,
                        help="Comma-separated list of db_ids to process (e.g. dst_straf10,dst_aku110k)")
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        sys.exit("OPENAI_API_KEY not set")
    from openai import OpenAI
    client = OpenAI(api_key=api_key)

    tmp_dir = Path("/tmp/da-bird-dbs-v2")
    tmp_dir.mkdir(exist_ok=True)

    only_tables = {t.strip() for t in args.only_tables.split(",")} if args.only_tables else None

    for tbl in TABLES:
        if only_tables and tbl["db_id"] not in only_tables:
            continue
        print(f"\n{'='*60}")
        print(f"Table: {tbl['id']} ({tbl['db_id']}) — {tbl['title']}")
        print("  Fetching from DST...", flush=True)

        db_path = tmp_dir / f"{tbl['db_id']}.sqlite"
        if db_path.exists():
            db_path.unlink()

        try:
            csv_text = fetch_dst(tbl["id"], tbl["variables"])
            n_rows = csv_to_sqlite(csv_text, db_path, tbl["table_name"], tbl.get("col_types", {}))
            print(f"  {n_rows} rows loaded")
        except Exception as e:
            print(f"  ERROR: {e} — skipping")
            continue

        schema = get_schema(db_path, tbl["table_name"])
        sample = get_sample(db_path, tbl["table_name"])
        unique = get_unique_vals(db_path, tbl["table_name"])

        difficulties = DIFFICULTY_DISTRIBUTION[: args.n_per_table]
        if args.only_difficulty:
            difficulties = [args.only_difficulty] * args.n_per_table
        random.shuffle(difficulties)

        accepted = 0
        task_id = _next_id(args.out_dir, tbl["db_id"])

        for diff in difficulties:
            patterns = PATTERNS[diff].copy()
            random.shuffle(patterns)
            success = False

            for attempt in range(args.max_retries):
                pattern = patterns[attempt % len(patterns)]
                try:
                    # Step 1: generate SQL
                    sql = generate_sql(client, args.model, schema, sample, unique, pattern)
                    if not sql:
                        continue

                    # Step 2: execute
                    cols, rows, err = run_sql(db_path, sql)
                    if err or not rows:
                        print(f"    [{diff}/{pattern['name']}] attempt {attempt+1}: SQL invalid or empty — retry")
                        continue

                    # Step 3: generate question
                    q_data = generate_question(client, args.model, schema, sql, cols, rows)
                    question = q_data.get("question_da", "").strip()
                    evidence = q_data.get("evidence_da", "").strip()
                    if not question:
                        continue

                    # Step 4: coherence check
                    ok, reason = coherence_check(client, args.model, question, sql, cols, rows)
                    if not ok:
                        print(f"    [{diff}/{pattern['name']}] attempt {attempt+1}: coherence fail — {reason}")
                        continue

                    # Step 5: scaffold
                    folder = scaffold(
                        args.out_dir, tbl["db_id"], task_id, tbl["id"],
                        db_path, schema, sql, question, evidence, diff,
                    )
                    print(f"    [{diff:6}/{pattern['name']:20}] {folder.name}: {question[:65]}")
                    task_id += 1
                    accepted += 1
                    success = True
                    break

                except Exception as e:
                    print(f"    [{diff}/{pattern['name']}] attempt {attempt+1}: ERROR {e}")

            if not success:
                print(f"    [{diff}] gave up after {args.max_retries} attempts")

        print(f"  Done: {accepted}/{args.n_per_table} tasks for {tbl['db_id']}")

    print(f"\nAll done. Tasks written to {args.out_dir}/")


if __name__ == "__main__":
    main()
