import argparse
from collections import defaultdict
import csv
from datetime import date, timedelta
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory

from erp_artifact_sources import check_manifest, source_fingerprint
from validate_erp import validate

EXCEL_EPOCH = date(1899, 12, 30)
COSTS = ("material", "labor", "overhead")
PROJECT = Path(__file__).resolve().parent
WORKBOOK_CACHE = Path.home() / ".cache" / "ChemErpAnalytics" / "workbook"


def csv_rows(directory, name):
    with (directory / f"{name}.csv").open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def serial(day):
    return (date.fromisoformat(day) - EXCEL_EPOCH).days


def prepare(data, forecast_dir):
    validate(data)
    cutoff = check_manifest(forecast_dir, source_fingerprint(data), ("forecasts", "model_metrics"))
    batches = csv_rows(data, "production_batches")
    orders = csv_rows(data, "purchase_orders")
    sales = csv_rows(data, "sales_shipments")
    budgets = csv_rows(data, "cost_center_budgets")
    monthly = defaultdict(lambda: [0] * 7)
    records = defaultdict(list)

    for batch in batches:
        day = date.fromisoformat(batch["posting_date"])
        center, product = batch["cost_center_id"], batch["product_id"]
        totals = monthly[day.strftime("%Y-%m-01"), center, product]
        values = [int(batch[f"{component}_cost_cents"]) for component in COSTS]
        total = int(batch["total_cost_cents"])
        for index, value in enumerate([
            int(batch["good_output_kg"]), *values, total, int(batch["scrap_kg"]), 1
        ]):
            totals[index] += value
        records["production_cost", "", center, product].append((day, total, 0))

    for row in orders:
        day = date.fromisoformat(row["order_date"])
        amount, quantity = int(row["amount_cents"]), int(row["quantity_kg"])
        for metric in ("raw_material_unit_cost", "raw_material_purchase_spend"):
            records[metric, row["material_id"], "", ""].append((day, amount, quantity))

    for row in sales:
        profit = int(row["revenue_cents"]) - int(row["cogs_cents"])
        records["product_gross_profit", "", "", row["product_id"]].append(
            (date.fromisoformat(row["posting_date"]), profit, 0)
        )

    inputs = []
    for row in sorted(budgets, key=lambda row: (row["month_start"], row["cost_center_id"], row["product_id"])):
        month, center, product = row["month_start"], row["cost_center_id"], row["product_id"]
        output, material, labor, overhead, total, scrap, count = monthly.pop((month, center, product), [0] * 7)
        inputs.append([
            serial(month), int(month[:4]), center, product, output, scrap, count,
            material / 100, labor / 100, overhead / 100, total / 100,
            int(row["planned_output_kg"]), *(int(row[f"{part}_budget_cents"]) / 100 for part in COSTS),
            int(row["total_budget_cents"]) / 100,
        ])
    if monthly:
        raise ValueError("There are actuals without corresponding monthly budgets.")

    labels = {
        "production_cost": "Production cost",
        "raw_material_unit_cost": "Raw material price",
        "raw_material_purchase_spend": "Material purchase spend",
        "product_gross_profit": "Product gross profit",
    }
    metric_keys = ("metric", "material_id", "cost_center_id", "product_id", "horizon_days")
    scores = {
        tuple(row[key] for key in metric_keys): row
        for row in csv_rows(forecast_dir, "model_metrics") if row["algorithm"] == "xgboost"
    }
    forecasts = []
    first_day = date.fromisoformat(min(row["month_start"] for row in budgets))
    for row in csv_rows(forecast_dir, "forecasts"):
        key = tuple(row[field] for field in metric_keys)
        metric, material, center, product, horizon = key
        horizon_days = int(horizon)
        issue = date.fromisoformat(row["as_of_date"])
        start = date.fromisoformat(row["window_start"])
        end = date.fromisoformat(row["window_end"])
        if row["as_of_date"] != cutoff or start != issue + timedelta(days=1) or (end - issue).days != horizon_days:
            raise ValueError(f"Forecast dates disagree with the export cutoff or horizon: {key}")
        prior_start = issue - timedelta(days=horizon_days - 1)
        prior = [(amount, qty) for day, amount, qty in records[key[:4]] if prior_start <= day <= issue]
        actual = sum(amount for amount, _ in prior) / 100
        if prior_start < first_day:
            actual = "n.a."
        elif metric == "raw_material_unit_cost":
            quantity = sum(qty for _, qty in prior)
            actual = actual / quantity if quantity else "n.a."
        score = scores[key]
        forecasts.append([
            labels[metric], "/".join(value for value in (material, center, product) if value),
            horizon_days, serial(row["as_of_date"]), serial(row["window_start"]), serial(row["window_end"]),
            row["unit"], float(row["forecast_value"]), actual,
            float(score["wape_pct"]) / 100 if score["wape_pct"] else "n.a.",
            int(score["backtest_windows"]),
        ])
    return {"inputs": inputs, "forecasts": forecasts, "years": sorted({row[1] for row in inputs}),
            "cutoff": serial(cutoff)}


def resolve_packages(packages):
    candidates = (packages,) if packages is not None else (
        PROJECT / "node_modules", WORKBOOK_CACHE / "node_modules"
    )
    for directory in candidates:
        if (directory / "@oai" / "artifact-tool" / "package.json").is_file():
            return directory.resolve()
    raise ValueError("Supply --packages with a node_modules directory containing @oai/artifact-tool.")


def build(payload, output, node, packages, previews):
    if output.exists():
        raise ValueError(f"Output already exists: {output}")
    if node is None or not node.is_file():
        raise ValueError("Install Node.js on PATH or supply its executable with --node.")
    packages = resolve_packages(packages)
    with TemporaryDirectory() as temporary:
        scratch = Path(temporary)
        if os.name == "nt":
            env = {**os.environ, "ERP_NODE_JUNCTION": str(scratch / "node_modules"),
                   "ERP_NODE_PACKAGES": str(packages)}
            subprocess.run([
                "powershell.exe", "-NoProfile", "-Command",
                "$ErrorActionPreference = 'Stop'; New-Item -ItemType Junction "
                "-Path $env:ERP_NODE_JUNCTION -Target $env:ERP_NODE_PACKAGES | Out-Null",
            ], env=env, check=True)
        else:
            (scratch / "node_modules").symlink_to(packages, target_is_directory=True)
        builder = scratch / "financial_workbook.mjs"
        shutil.copyfile(PROJECT / builder.name, builder)
        source = scratch / "model_inputs.json"
        source.write_text(json.dumps(payload), encoding="utf-8")
        staged = scratch / output.name
        command = [str(node.resolve()), str(builder), str(source), str(staged)]
        if previews:
            command.append(str(previews.resolve()))
        subprocess.run(command, check=True)
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            raise ValueError(f"Output already exists: {output}")
        shutil.move(staged, output)
    print(f"Saved workbook to {output.resolve()}")


def main():
    system_node = shutil.which("node")
    parser = argparse.ArgumentParser(description="Build an Excel financial model from reconciled ERP exports.")
    parser.add_argument("--data", type=Path, default=Path("data/generated"))
    parser.add_argument("--forecasts", type=Path, default=Path("data/forecasts"))
    parser.add_argument("--output", type=Path, default=Path("outputs/financial-model/Linden_Manufacturing_Financial_Model.xlsx"))
    parser.add_argument("--node", type=Path, default=Path(system_node) if system_node else None,
                        help="Path to Node.js. Defaults to the executable on PATH.")
    parser.add_argument("--packages", type=Path, help="Path to node_modules containing @oai/artifact-tool.")
    parser.add_argument("--preview-dir", type=Path)
    args = parser.parse_args()
    try:
        build(prepare(args.data, args.forecasts), args.output, args.node, args.packages, args.preview_dir)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Workbook build failed: {exc}\n")


if __name__ == "__main__":
    main()
