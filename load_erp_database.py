import argparse
import csv
from datetime import date
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from erp_artifact_sources import check_manifest, source_fingerprint
from validate_erp import validate

PROJECT = Path(__file__).resolve().parent
WINDOWS_CACHE = Path.home() / ".cache/ChemErpAnalytics"
LOCAL_PORT = 55432
LOCAL_USER = "chem_erp"
DATABASE = "chem_erp"
ERP_TABLES = (
    "company_codes", "plants", "materials", "suppliers", "customers", "cost_centers", "accounts",
    "gl_entries", "purchase_orders", "supplier_transactions", "production_batches", "sales_shipments",
    "customer_receipts", "material_movements", "inventory_month_end", "cost_center_budgets",
)
FORECAST_TABLES = ("forecasts", "backtest_predictions", "model_metrics")
RISK_TABLES = ("purchase_anomalies", "production_variances", "batch_efficiency", "budget_risk", "budget_metrics")
LOCK = 4766170311649381
PASSWORD_NAME = "CHEM_ERP_DB_PASSWORD"


def database_password(path, create):
    password = os.environ.get(PASSWORD_NAME)
    if password:
        return password
    if not path.exists() and create:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            stream.write(f"{PASSWORD_NAME}={secrets.token_urlsafe(32)}\n")
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.partition("=")
            if sep and name == PASSWORD_NAME and value:
                return value
    raise ValueError(f"Supply {PASSWORD_NAME} or initialize a local database with --start-db.")


def pg_command(pg_bin, name, *args, capture=True):
    executable = pg_bin / (name + (".exe" if os.name == "nt" else ""))
    if not executable.is_file():
        raise ValueError(f"Missing PostgreSQL command {executable}. Pass --pg-bin with a PostgreSQL 17 bin directory.")
    return subprocess.run([str(executable.resolve()), *args],
                          stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                          stderr=subprocess.STDOUT, text=True, errors="replace")


def connect(password, database=DATABASE, autocommit=False):
    return psycopg.connect(host="127.0.0.1", port=LOCAL_PORT, user=LOCAL_USER, dbname=database,
                           password=password, connect_timeout=5, autocommit=autocommit)


def start_database(pg_bin, pg_data, password):
    pg_data = pg_data.resolve()
    if not (pg_data / "PG_VERSION").exists():
        if pg_data.exists() and any(pg_data.iterdir()):
            raise ValueError(f"PostgreSQL directory is not empty: {pg_data}")
        pg_data.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory() as temporary:
            password_file = Path(temporary) / "pg-password"
            with os.fdopen(os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
                stream.write(password)
            result = pg_command(pg_bin, "initdb", "-D", str(pg_data), "-U", LOCAL_USER,
                                "-A", "scram-sha-256", "--encoding=UTF8", "--no-locale",
                                "--pwfile=" + str(password_file))
            if result.returncode:
                raise ValueError("PostgreSQL initialization failed. " + result.stdout.strip())

    status = pg_command(pg_bin, "pg_ctl", "-D", str(pg_data), "status")
    if status.returncode:
        log = pg_data.parent / "chem-erp-postgres.log"
        result = pg_command(pg_bin, "pg_ctl", "-D", str(pg_data), "-l", str(log), "-w", "-t", "30",
                            "-o", f"-h 127.0.0.1 -p {LOCAL_PORT}", "start", capture=False)
        if result.returncode:
            raise ValueError(f"Local PostgreSQL did not start. See {log}.")
    with connect(password, "postgres", autocommit=True) as connection:
        data_folder = Path(connection.execute("SHOW data_directory").fetchone()[0]).resolve()
        if data_folder != pg_data:
            raise ValueError("The loopback port is already in use by a different PostgreSQL cluster.")
        exists = connection.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DATABASE,)).fetchone()
        if not exists:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DATABASE)))


def stage_csv(source, destination):
    destination.mkdir()
    for path in sorted(source.glob("*.csv")):
        shutil.copyfile(path, destination / path.name)


def stage_analytics(source, destination, names):
    destination.mkdir()
    for name in ("source.json", *(f"{table}.csv" for table in names)):
        shutil.copyfile(source / name, destination / name)


def fingerprints(data, forecasts, risk):
    erp_digest = source_fingerprint(data)
    forecast_date = date.fromisoformat(check_manifest(forecasts, erp_digest, FORECAST_TABLES))
    risk_date = date.fromisoformat(check_manifest(risk, erp_digest, RISK_TABLES))
    with (data / "gl_entries.csv").open(newline="", encoding="utf-8") as stream:
        opening = next(row for row in csv.DictReader(stream) if row["source_type"] == "OPENING")
        first_date = date.fromisoformat(opening["posting_date"])
    with (data / "inventory_month_end.csv").open(newline="", encoding="utf-8") as stream:
        final_date = max(date.fromisoformat(row["month_end"]) for row in csv.DictReader(stream))
    if not all(first_date <= cutoff <= final_date for cutoff in (forecast_date, risk_date)):
        raise ValueError("An analytics export cutoff falls outside the ERP reporting period.")
    parts = [erp_digest, source_fingerprint(forecasts), source_fingerprint(risk),
             forecast_date.isoformat(), risk_date.isoformat()]
    return sha256("\n".join(parts).encode()).hexdigest(), erp_digest, forecast_date, risk_date


def copy_csv(cursor, schema, table, path):
    target = sql.Identifier(schema, table)
    statement = sql.SQL("COPY {} FROM STDIN WITH (FORMAT CSV, HEADER MATCH, ENCODING 'UTF8')").format(target)
    with path.open("rb") as stream, cursor.copy(statement) as copy:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            copy.write(chunk)
    return cursor.rowcount


def copy_documents(cursor, path):
    with path.open(newline="", encoding="utf-8") as stream, cursor.copy(
        "COPY erp.gl_documents (document_id, company_code, posting_date, source_type, source_id) FROM STDIN"
    ) as copy:
        seen = set()
        for row in csv.DictReader(stream):
            document = row["document_id"]
            if document not in seen:
                copy.write_row((document, row["company_code"], row["posting_date"], row["source_type"], row["source_id"]))
                seen.add(document)
    return len(seen)


def loaded_counts(cursor, tables):
    return {
        f"{schema}.{name}": cursor.execute(
            sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(schema, name))
        ).fetchone()[0]
        for schema, name in tables
    }


def reconcile(cursor, forecast_date, risk_date):
    differences = dict(cursor.execute("SELECT control, difference FROM pipeline.control_differences").fetchall())
    checks = cursor.execute(
        """
        SELECT
            (SELECT count(*) FROM analytics.forecasts WHERE as_of_date <> %s),
            (SELECT count(*) FROM analytics.backtest_predictions WHERE window_end > %s),
            (SELECT count(*) FROM analytics.purchase_anomalies WHERE order_date > %s),
            (SELECT count(*) FROM analytics.production_variances WHERE posting_date > %s),
            (SELECT count(*) FROM analytics.batch_efficiency WHERE posting_date > %s),
            (SELECT count(*) FROM analytics.budget_risk WHERE as_of_date > %s
                 OR (final_cost_cents IS NOT NULL AND month_end > %s))
        """,
        (forecast_date, forecast_date, risk_date, risk_date, risk_date, risk_date, risk_date),
    ).fetchone()
    differences.update(zip((
        "forecast issue cutoff", "historical forecast windows cutoff", "purchase risk cutoff",
        "production variance cutoff", "batch efficiency cutoff", "budget risk cutoff",
    ), checks, strict=True))
    violations = {control: delta for control, delta in differences.items() if delta != 0}
    if violations:
        raise ValueError(f"Database reconciliation failed: {violations}")
    return differences


def ingest(password, data, forecasts, risk):
    digest, erp_digest, forecast_date, risk_date = fingerprints(data, forecasts, risk)
    groups = (("erp", ERP_TABLES, data), ("analytics", FORECAST_TABLES, forecasts), ("analytics", RISK_TABLES, risk))
    tables = [("erp", "gl_documents")]
    tables.extend((schema, name) for schema, names, _ in groups for name in names)
    ddl = (PROJECT / "erp_schema.sql").read_text(encoding="utf-8")
    with connect(password) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK,))
        cursor.execute(ddl)
        latest = cursor.execute(
            "SELECT load_id, input_sha256, row_counts FROM pipeline.loads ORDER BY load_id DESC LIMIT 1"
        ).fetchone()
        if latest and latest[1] == digest and loaded_counts(cursor, tables) == latest[2]:
            reconcile(cursor, forecast_date, risk_date)
            print(f"Inputs already loaded in run {latest[0]}. Database reconciliation passed.")
            return

        for schema, name in reversed(tables[1:]):
            cursor.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier(schema, name)))
            if (schema, name) == ("erp", "gl_entries"):
                cursor.execute("DELETE FROM erp.gl_documents")

        counts = {}
        for schema, names, directory in groups:
            for name in names:
                if name == "gl_entries":
                    counts["erp.gl_documents"] = copy_documents(cursor, directory / "gl_entries.csv")
                counts[f"{schema}.{name}"] = copy_csv(cursor, schema, name, directory / f"{name}.csv")
        if counts != loaded_counts(cursor, tables):
            raise ValueError("The PostgreSQL row counts differ from the copied row counts.")
        differences = reconcile(cursor, forecast_date, risk_date)
        load_id = cursor.execute(
            "INSERT INTO pipeline.loads (input_sha256, erp_sha256, forecast_as_of, risk_as_of, row_counts) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING load_id",
            (digest, erp_digest, forecast_date, risk_date, Jsonb(counts)),
        ).fetchone()[0]
        cursor.executemany(
            "INSERT INTO pipeline.reconciliations (load_id, control, difference) VALUES (%s, %s, %s)",
            ((load_id, control, delta) for control, delta in differences.items()),
        )
    print(f"Loaded {sum(counts.values()):,} rows across {len(counts)} PostgreSQL tables in run {load_id}.")
    print(f"Passed {len(differences)} database reconciliation controls.")


def main():
    runtime_bin = WINDOWS_CACHE / "postgresql-17.12/pgsql/bin" if os.name == "nt" else Path(shutil.which("pg_ctl") or "/usr/bin/pg_ctl").parent
    parser = argparse.ArgumentParser(description="Load reconciled ERP and analytics exports into local PostgreSQL.")
    parser.add_argument("--data", type=Path, default=Path("data/generated"))
    parser.add_argument("--forecasts", type=Path, default=Path("data/forecasts"))
    parser.add_argument("--risk", type=Path, default=Path("data/risk"))
    parser.add_argument("--rebuild-analytics", action="store_true", help="Run forecasting and risk scoring before loading.")
    parser.add_argument("--start-db", action="store_true", help="Initialize or start the dedicated PostgreSQL cluster.")
    parser.add_argument("--pg-bin", type=Path, default=runtime_bin)
    parser.add_argument("--pg-data", type=Path, default=WINDOWS_CACHE / "postgres-data")
    parser.add_argument("--password-file", type=Path, default=PROJECT / "data/postgres.env")
    args = parser.parse_args()
    try:
        with TemporaryDirectory() as temporary:
            stage = Path(temporary)
            data, forecasts, risk = (stage / name for name in ("erp", "forecasts", "risk"))
            stage_csv(args.data, data)
            print(validate(data), flush=True)
            if args.rebuild_analytics:
                for script, output in (("forecast_erp.py", forecasts), ("detect_erp_risks.py", risk)):
                    subprocess.run([sys.executable, str(PROJECT / script), "--data", str(data),
                                    "--output", str(output)], check=True)
            else:
                stage_analytics(args.forecasts, forecasts, FORECAST_TABLES)
                stage_analytics(args.risk, risk, RISK_TABLES)
            password = database_password(args.password_file, args.start_db)
            if args.start_db:
                start_database(args.pg_bin, args.pg_data, password)
            ingest(password, data, forecasts, risk)
    except (OSError, ValueError, KeyError, psycopg.Error, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"ERP pipeline failed: {exc}\n")


if __name__ == "__main__":
    main()
