"""Build a multi-table DST database and generate hard JOIN-based tasks.

Creates a SQLite DB with two tables:
  folk1a  — population by municipality (quarterly) from DST FOLK1A
  bol101  — housing by municipality (annual) from DST BOL101

Both share OMRÅDE (municipality name), enabling JOIN queries.
Generates 10 hard tasks using join-based SQL patterns.

Usage:
    uv run python scripts/06_build_multitable_db.py \
        --out-dir data/dst-v2-hard \
        --model gpt-5.5 \
        --n-tasks 10
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

import requests

DB_ID = "dst_folk_bol"
DST_TABLE_LABEL = "FOLK1A+BOL101"

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
author_name = "da-bird"
author_email = ""
difficulty = "hard"
category = "database"
tags = ["nl2sql", "da-bird", "text-to-sql", "sql", "dansk", "dst", "hard", "join"]
source = "Danmarks Statistik / FOLK1A+BOL101"
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

# Hard JOIN patterns — require joining folk1a and bol101 on OMRÅDE
JOIN_PATTERNS = [
    {
        "name": "join_aggregate",
        "instruction": (
            "Skriv en SELECT der joiner folk1a og bol101 på OMRÅDE og aggregerer over begge tabeller. "
            "Brug GROUP BY og ORDER BY. Husk at begge tabeller har INDHOLD-kolonnen, så brug tabelaliaser."
        ),
        "example": (
            "SELECT f.OMRÅDE, AVG(f.INDHOLD) AS snit_pop, MAX(b.INDHOLD) AS max_boliger "
            "FROM folk1a f JOIN bol101 b ON f.OMRÅDE = b.OMRÅDE "
            "GROUP BY f.OMRÅDE ORDER BY snit_pop DESC LIMIT 5"
        ),
    },
    {
        "name": "join_filter_aggregate",
        "instruction": (
            "Skriv en SELECT der joiner folk1a og bol101 på OMRÅDE, filtrerer på specifikke værdier "
            "i begge tabeller (fx TID eller ANVENDELSE), og aggregerer. Brug tabelaliaser."
        ),
        "example": (
            "SELECT f.OMRÅDE, SUM(f.INDHOLD) AS total_pop, SUM(b.INDHOLD) AS total_boliger "
            "FROM folk1a f JOIN bol101 b ON f.OMRÅDE = b.OMRÅDE "
            "WHERE f.TID = '2023K1' AND b.ANVENDELSE = 'Enfamiliehuse' "
            "GROUP BY f.OMRÅDE ORDER BY total_pop DESC LIMIT 10"
        ),
    },
    {
        "name": "join_ratio",
        "instruction": (
            "Beregn et forhold (ratio) mellem værdier fra de to tabeller via JOIN på OMRÅDE. "
            "Fx antal boliger per person eller befolkning per bolig. Brug CAST eller division."
        ),
        "example": (
            "SELECT f.OMRÅDE, "
            "CAST(SUM(f.INDHOLD) AS REAL) / NULLIF(SUM(b.INDHOLD), 0) AS pop_per_bolig "
            "FROM folk1a f JOIN bol101 b ON f.OMRÅDE = b.OMRÅDE "
            "WHERE f.TID = '2023K1' "
            "GROUP BY f.OMRÅDE ORDER BY pop_per_bolig DESC LIMIT 5"
        ),
    },
    {
        "name": "join_cte_rank",
        "instruction": (
            "Brug en CTE til at aggregere fra begge tabeller via JOIN, og brug derefter RANK() "
            "eller ROW_NUMBER() til at rangere OMRÅDE efter en beregnet størrelse."
        ),
        "example": (
            "WITH merged AS ("
            "  SELECT f.OMRÅDE, SUM(f.INDHOLD) AS pop, SUM(b.INDHOLD) AS boliger "
            "  FROM folk1a f JOIN bol101 b ON f.OMRÅDE = b.OMRÅDE "
            "  WHERE f.TID = '2023K1' GROUP BY f.OMRÅDE"
            ") "
            "SELECT OMRÅDE, pop, boliger, RANK() OVER (ORDER BY pop DESC) AS rang FROM merged LIMIT 10"
        ),
    },
    {
        "name": "join_subquery_filter",
        "instruction": (
            "Brug en subforespørgsel eller CTE til at filtrere OMRÅDE fra én tabel, "
            "og join derefter med den anden tabel. Fx: find kommuner med befolkning over gennemsnittet "
            "og vis deres boligtal."
        ),
        "example": (
            "WITH store AS ("
            "  SELECT OMRÅDE FROM folk1a WHERE TID = '2023K1' AND INDHOLD > "
            "  (SELECT AVG(INDHOLD) FROM folk1a WHERE TID = '2023K1')"
            ") "
            "SELECT b.OMRÅDE, SUM(b.INDHOLD) AS boliger "
            "FROM bol101 b JOIN store s ON b.OMRÅDE = s.OMRÅDE "
            "GROUP BY b.OMRÅDE ORDER BY boliger DESC"
        ),
    },
]


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
    return resp.text.lstrip("﻿")


def csv_to_sqlite(csv_text: str, conn: sqlite3.Connection, table_name: str, col_types: dict) -> int:
    import csv, io
    rows = list(csv.DictReader(io.StringIO(csv_text), delimiter=";"))
    if not rows:
        raise ValueError(f"Empty CSV for {table_name}")
    cols = list(rows[0].keys())
    col_defs = ", ".join(f'"{c}" {col_types.get(c, "TEXT")}' for c in cols)
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
    return conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]


def get_schema(conn: sqlite3.Connection, table_names: list[str]) -> str:
    parts = []
    for name in table_names:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        if row:
            parts.append(row[0])
    return "\n\n".join(parts)


def get_sample(conn: sqlite3.Connection, table_name: str, n: int = 5) -> str:
    import io
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"SELECT * FROM {table_name} LIMIT {n}").fetchall()
    if not rows:
        return f"({table_name}: ingen rækker)"
    buf = io.StringIO()
    headers = rows[0].keys()
    buf.write(f"-- {table_name}\n")
    buf.write(" | ".join(headers) + "\n")
    buf.write("-" * 60 + "\n")
    for r in rows:
        buf.write(" | ".join(str(r[h]) for h in headers) + "\n")
    return buf.getvalue()


def get_unique_vals(conn: sqlite3.Connection, table_name: str, limit: int = 6) -> str:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table_name})").fetchall()]
    lines = [f"-- {table_name}"]
    for col in cols:
        vals = [str(r[0]) for r in conn.execute(
            f'SELECT DISTINCT "{col}" FROM {table_name} LIMIT {limit}'
        ).fetchall()]
        lines.append(f"  {col}: {', '.join(vals)}")
    return "\n".join(lines)


def run_sql(conn: sqlite3.Connection, sql: str) -> tuple[list, list, str | None]:
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(sql)
        rows = cur.fetchmany(20)
        cols = [d[0] for d in cur.description] if cur.description else []
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


def generate_sql(client, model: str, schema: str, samples: str, unique: str, pattern: dict) -> str:
    system = """\
Du er en SQL-ekspert. Du skriver KUN SQL-forespørgsler til SQLite-databaser med TO tabeller.
Returner et JSON-objekt med én nøgle "sql" der indeholder forespørgslen.
Regler:
- SQL skal bruge tabeller og kolonner der eksisterer i skemaet.
- Kolonnenavne med specialtegn skal omgives af dobbelte anførselstegn.
- SQL SKAL joine de to tabeller (folk1a og bol101) på OMRÅDE.
- Brug tabelaliaser (f for folk1a, b for bol101) for at undgå tvetydige INDHOLD-kolonner.
- SQL skal returnere mindst én række med realistiske data.
- Undgå at filtrere på værdier der sandsynligvis ikke eksisterer i data.
"""
    user = f"""\
Skema (to tabeller):
{schema}

Eksempelrækker:
{samples}

Unikke værdier per kolonne:
{unique}

Mønster der skal følges ({pattern['name']}):
{pattern['instruction']}

Eksempel på mønsteret (brug som inspiration, tilpas til de faktiske kolonneværdier):
{pattern['example']}

Skriv en konkret SQL-forespørgsel der joiner folk1a og bol101 på OMRÅDE og returnerer interessante resultater.
"""
    raw = llm(client, model, system, user)
    data = json.loads(raw)
    return data.get("sql", "").strip()


def generate_question(client, model: str, schema: str, sql: str, cols: list, rows: list) -> dict:
    rows_str = "\n".join(" | ".join(str(v) for v in row) for row in rows[:10])
    system = """\
Du er ekspert i at formulere naturlige danske spørgsmål til et NL2SQL benchmark.
Returner et JSON-objekt med nøglerne:
  "question_da"  – naturligt dansk spørgsmål hvis svar er de viste resultater
  "evidence_da"  – kort hint der hjælper modellen (dansk), fx hvilke tabeller og filter der er relevante
Reglerne:
- Spørgsmålet skal lyde som noget en analytiker ville spørge om.
- Spørgsmålet må ikke afsløre SQL-strukturen (ingen "brug JOIN", "lav en CTE" osv.).
- Spørgsmålet skal præcist matche de viste resultater — hverken bredere eller smallere.
- Bevismaterialet skal nævne at der er to tabeller (befolkning og boliger) og join-nøglen OMRÅDE.
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

def _next_id(out_dir: Path) -> int:
    existing = [p for p in out_dir.glob(f"{DB_ID}__*") if p.is_dir()]
    ids = []
    for p in existing:
        try:
            ids.append(int(p.name.split("__")[-1]))
        except ValueError:
            pass
    return max(ids) + 1 if ids else 1


def scaffold(out_dir: Path, task_id: int, db_path: Path, schema: str,
             sql: str, question: str, evidence: str) -> Path:
    folder = out_dir / f"{DB_ID}__{task_id}"
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
        TASK_TOML_TEMPLATE.format(db_id=DB_ID, source_id=str(task_id)),
        encoding="utf-8",
    )
    return folder


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, default=Path("data/dst-v2-hard"))
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument("--n-tasks", type=int, default=10)
    parser.add_argument("--max-retries", type=int, default=6)
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        sys.exit("OPENAI_API_KEY not set")
    from openai import OpenAI
    client = OpenAI(api_key=api_key)

    # Build the multi-table SQLite DB
    db_path = Path("/tmp/da-bird-dbs-v2/dst_folk_bol.sqlite")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(db_path)

    # Fetch folk1a: total population by municipality, all quarters
    print("Fetching FOLK1A from DST...", flush=True)
    folk1a_csv = fetch_dst("FOLK1A", [
        {"code": "OMRÅDE", "values": ["*"]},
        {"code": "KØN", "values": ["TOT"]},
        {"code": "ALDER", "values": ["IALT"]},
        {"code": "CIVILSTAND", "values": ["TOT"]},
        {"code": "Tid", "values": ["*"]},
    ])
    n_folk = csv_to_sqlite(folk1a_csv, conn, "folk1a", {"INDHOLD": "INTEGER"})
    print(f"  folk1a: {n_folk} rows")

    # Fetch bol101: occupied dwellings by municipality, all years
    print("Fetching BOL101 from DST...", flush=True)
    bol101_csv = fetch_dst("BOL101", [
        {"code": "OMRÅDE", "values": ["*"]},
        {"code": "BEBO", "values": ["1000"]},
        {"code": "ANVENDELSE", "values": ["*"]},
        {"code": "UDLFORH", "values": ["*"]},
        {"code": "Tid", "values": ["*"]},
    ])
    n_bol = csv_to_sqlite(bol101_csv, conn, "bol101", {"INDHOLD": "INTEGER"})
    print(f"  bol101: {n_bol} rows")

    schema = get_schema(conn, ["folk1a", "bol101"])
    samples = get_sample(conn, "folk1a") + "\n" + get_sample(conn, "bol101")
    unique = get_unique_vals(conn, "folk1a") + "\n\n" + get_unique_vals(conn, "bol101")

    # Verify join key overlap
    overlap = conn.execute(
        "SELECT COUNT(DISTINCT f.OMRÅDE) FROM folk1a f JOIN bol101 b ON f.OMRÅDE = b.OMRÅDE"
    ).fetchone()[0]
    print(f"  Join overlap: {overlap} matching OMRÅDE values")
    if overlap < 5:
        sys.exit("Too few matching OMRÅDE values — check variable configs")

    conn.close()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    task_id = _next_id(args.out_dir)
    accepted = 0
    patterns = JOIN_PATTERNS.copy()

    for i in range(args.n_tasks):
        pattern = patterns[i % len(patterns)]
        success = False

        for attempt in range(args.max_retries):
            try:
                conn = sqlite3.connect(db_path)

                sql = generate_sql(client, args.model, schema, samples, unique, pattern)
                if not sql:
                    conn.close()
                    continue

                cols, rows, err = run_sql(conn, sql)
                conn.close()

                if err or not rows:
                    print(f"  [{pattern['name']}] attempt {attempt+1}: SQL invalid or empty — retry")
                    continue

                q_data = generate_question(client, args.model, schema, sql, cols, rows)
                question = q_data.get("question_da", "").strip()
                evidence = q_data.get("evidence_da", "").strip()
                if not question:
                    continue

                ok, reason = coherence_check(client, args.model, question, sql, cols, rows)
                if not ok:
                    print(f"  [{pattern['name']}] attempt {attempt+1}: coherence fail — {reason}")
                    continue

                folder = scaffold(args.out_dir, task_id, db_path, schema, sql, question, evidence)
                print(f"  [{pattern['name']:25}] {folder.name}: {question[:65]}")
                task_id += 1
                accepted += 1
                success = True
                break

            except Exception as e:
                print(f"  [{pattern['name']}] attempt {attempt+1}: ERROR {e}")
                try:
                    conn.close()
                except Exception:
                    pass

        if not success:
            print(f"  [{pattern['name']}] gave up after {args.max_retries} attempts")

    print(f"\nDone: {accepted}/{args.n_tasks} tasks written to {args.out_dir}/{DB_ID}__*/")


if __name__ == "__main__":
    main()
