import argparse
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

from validate_erp import validate

PURCHASE_LOOKBACK_DAYS = 120
PURCHASE_MIN_ORDERS = 10
PURCHASE_MIN_PREMIUM = 0.08
PURCHASE_MIN_ROBUST_Z = 3.5
VARIANCE_MIN_CENTS = 35_000
VARIANCE_MIN_RATE = 0.12
BATCH_MIN_HISTORY = 60
BATCH_LOOKBACK_DAYS = 365
BATCH_FEATURES = ["scrap_rate", "labor_cents_per_input_kg", "overhead_cents_per_input_kg"]
BUDGET_CHECKPOINTS = (7, 14, 21)
BUDGET_PRIOR_BUSINESS_DAYS = 60
BUDGET_PRIOR_WEIGHT = 5
COST_COMPONENTS = ("material", "labor", "overhead")
PAIR = ["cost_center_id", "product_id"]


def load_sources(directory, requested_cutoff):
    files = {
        "purchase_orders": "order_date", "production_batches": "posting_date",
        "cost_center_budgets": "month_start", "inventory_month_end": "month_end",
        "gl_entries": "posting_date",
    }
    sources = {
        table: pd.read_csv(directory / f"{table}.csv", parse_dates=[field])
        for table, field in files.items()
    }
    final_day = sources["inventory_month_end"]["month_end"].max()
    opening = sources["gl_entries"].loc[sources["gl_entries"]["source_type"].eq("OPENING"), "posting_date"].min()
    cutoff = final_day if requested_cutoff is None else pd.Timestamp(requested_cutoff)
    if not opening <= cutoff <= final_day:
        raise ValueError(f"--as-of must fall from {opening.date()} to {final_day.date()}")
    observed = {
        table: sources[table].loc[sources[table][field] <= cutoff].copy()
        for table, field in files.items() if table in ("purchase_orders", "production_batches", "cost_center_budgets")
    }
    return observed, opening, cutoff


def purchase_anomalies(orders):
    rows = []
    fields = ["po_id", "order_date", "plant_id", "supplier_id", "material_id",
              "quantity_kg", "unit_price_cents_per_kg"]
    for _, material in orders.groupby(["plant_id", "material_id"]):
        material = material.sort_values(["order_date", "po_id"])
        dates = material["order_date"].to_numpy(dtype="datetime64[D]")
        prices = material["unit_price_cents_per_kg"].to_numpy()
        for index, row in enumerate(material[fields].to_dict("records")):
            day = dates[index]
            begin = np.searchsorted(dates, day - np.timedelta64(PURCHASE_LOOKBACK_DAYS, "D"))
            end = np.searchsorted(dates, day)
            prior = prices[begin:end]
            result = {
                **row, "reference_orders": len(prior), "median_cents_per_kg": None,
                "premium_pct": None, "robust_z": None, "estimated_premium_cents": None,
                "flagged": False, "status": "insufficient_history",
            }
            if len(prior) >= PURCHASE_MIN_ORDERS:
                price = row["unit_price_cents_per_kg"]
                median = float(np.median(prior))
                mad = np.median(np.abs(prior - median))
                z_score = (price - median) / max(1.4826 * mad, median * 0.02)
                premium = price / median - 1
                flagged = premium >= PURCHASE_MIN_PREMIUM and z_score >= PURCHASE_MIN_ROBUST_Z
                result.update(
                    median_cents_per_kg=median, premium_pct=100 * premium, robust_z=z_score,
                    estimated_premium_cents=round((price - median) * row["quantity_kg"]),
                    flagged=flagged, status="unusual_high_price" if flagged else "within_threshold",
                )
            rows.append(result)
    return pd.DataFrame(rows)


def production_variances(batches, budgets):
    actuals = batches.assign(month_start=batches["posting_date"].dt.to_period("M").dt.to_timestamp())
    result = actuals.merge(
        budgets, on=["month_start", *PAIR], how="left", validate="many_to_one", suffixes=("", "_budget")
    )
    if result["planned_output_kg_budget"].isna().any():
        raise ValueError("Missing cost standard for a production batch.")
    standard_fields = []
    variance_fields = []
    for component in COST_COMPONENTS:
        standard = f"{component}_standard_cents"
        variance = f"{component}_variance_cents"
        planned_kg = result["planned_output_kg_budget"]
        scaled = result[f"{component}_budget_cents"] * result["good_output_kg"]
        result[standard] = (2 * scaled + planned_kg) // (2 * planned_kg)
        result[variance] = result[f"{component}_cost_cents"] - result[standard]
        standard_fields.append(standard)
        variance_fields.append(variance)
    result["flexed_standard_cents"] = result[standard_fields].sum(axis=1)
    result["unfavorable_variance_cents"] = result[variance_fields].sum(axis=1)
    result["unfavorable_variance_pct"] = (
        100 * result["unfavorable_variance_cents"] / result["flexed_standard_cents"]
    )
    result["flagged"] = (
        (result["unfavorable_variance_cents"] >= VARIANCE_MIN_CENTS)
        & (result["unfavorable_variance_pct"] >= 100 * VARIANCE_MIN_RATE)
    )
    result["largest_unfavorable_component"] = (
        result[variance_fields].idxmax(axis=1).str.replace("_variance_cents", "", regex=False)
        .where(result[variance_fields].max(axis=1) > 0, "")
    )
    return result[[
        "batch_id", "posting_date", *PAIR, "good_output_kg", "total_cost_cents",
        *standard_fields, *variance_fields, "flexed_standard_cents", "unfavorable_variance_cents",
        "unfavorable_variance_pct", "largest_unfavorable_component", "flagged",
    ]]


def batch_efficiency(batches):
    records = []
    for _, pair in batches.groupby(PAIR):
        pair = pair.sort_values(["posting_date", "batch_id"]).copy()
        input_kg = pair["good_output_kg"] + pair["scrap_kg"]
        pair["scrap_rate"] = pair["scrap_kg"] / input_kg
        pair["labor_cents_per_input_kg"] = pair["labor_cost_cents"] / input_kg
        pair["overhead_cents_per_input_kg"] = pair["overhead_cost_cents"] / input_kg
        for month, observations in pair.groupby(pair["posting_date"].dt.to_period("M")):
            month_start = month.start_time
            history = pair.loc[
                pair["posting_date"].between(
                    month_start - pd.Timedelta(days=BATCH_LOOKBACK_DAYS), month_start, inclusive="left"
                )
            ]
            scores = observations[["batch_id", "posting_date", *PAIR, *BATCH_FEATURES]].copy()
            scores["training_batches"] = len(history)
            scores["training_through"] = history["posting_date"].max()
            scores["anomaly_score"] = np.nan
            scores["score_threshold"] = np.nan
            scores["elevated_metrics"] = ""
            scores["flagged"] = False
            scores["status"] = "insufficient_history"
            if len(history) >= BATCH_MIN_HISTORY:
                train = history[BATCH_FEATURES]
                observed = observations[BATCH_FEATURES]
                model = IsolationForest(n_estimators=100, max_samples=min(128, len(train)),
                                        n_jobs=1, random_state=413)
                model.fit(train)
                threshold = np.quantile(-model.score_samples(train), 0.95)
                high_drivers = observed.gt(train.quantile(0.90))
                scores["anomaly_score"] = -model.score_samples(observed)
                scores["score_threshold"] = threshold
                scores["elevated_metrics"] = high_drivers.apply(
                    lambda row: "|".join(row.index[row]), axis=1
                )
                scores["flagged"] = (scores["anomaly_score"] >= threshold) & high_drivers.any(axis=1)
                scores["status"] = np.where(scores["flagged"], "potentially_inefficient", "within_threshold")
            records.append(scores)
    return pd.concat(records, ignore_index=True)


def budget_projections(batches, budgets, opening, cutoff):
    rows = []
    calendar = pd.bdate_range(opening, cutoff)
    for center, center_budgets in budgets.groupby("cost_center_id"):
        costs = batches.loc[batches["cost_center_id"].eq(center)].groupby("posting_date")["total_cost_cents"].sum()
        if (costs.index.dayofweek >= 5).any():
            raise ValueError(f"Budget projections expect weekday production in {center}.")
        daily = costs.reindex(calendar, fill_value=0)
        for month, budget in center_budgets.groupby("month_start")["total_budget_cents"].sum().items():
            month_end = month + pd.offsets.MonthEnd(0)
            workdays = pd.bdate_range(month, month_end)
            previous = daily.loc[daily.index < month].tail(BUDGET_PRIOR_BUSINESS_DAYS)
            prior_weight = min(BUDGET_PRIOR_WEIGHT, len(previous))
            for checkpoint in projection_dates(month, month_end, cutoff):
                elapsed = workdays[workdays <= checkpoint]
                if elapsed.empty:
                    continue
                remaining = len(workdays) - len(elapsed)
                actual = int(daily.loc[elapsed].sum())
                prior_mean = previous.mean() if prior_weight else 0
                blended_rate = (actual + prior_weight * prior_mean) / (len(elapsed) + prior_weight)
                projected = actual + round(actual / len(elapsed) * remaining)
                blended = actual + round(blended_rate * remaining)
                final_cost = int(daily.reindex(workdays).sum()) if month_end <= cutoff else None
                rows.append({
                    "cost_center_id": center, "month_start": month.date(), "month_end": month_end.date(),
                    "as_of_date": checkpoint.date(), "schedule": (
                        f"day_{checkpoint.day}" if checkpoint.day in BUDGET_CHECKPOINTS else "current"
                    ),
                    "business_days_elapsed": len(elapsed), "business_days_remaining": remaining,
                    "budget_cents": int(budget), "month_to_date_cost_cents": actual,
                    "projected_cost_cents": projected, "blended_history_cost_cents": blended,
                    "projected_variance_cents": projected - int(budget),
                    "projected_variance_pct": 100 * (projected - budget) / budget,
                    "risk_status": budget_status(actual, projected, budget),
                    "projected_to_overrun": projected > budget,
                    "final_cost_cents": final_cost,
                    "actual_overrun": final_cost > budget if final_cost is not None else None,
                })
    return pd.DataFrame(rows)


def projection_dates(month, month_end, cutoff):
    dates = {month.replace(day=day) for day in BUDGET_CHECKPOINTS if month.replace(day=day) <= cutoff}
    if month <= cutoff < month_end:
        dates.add(cutoff)
    return sorted(dates)


def budget_status(actual, projected, budget):
    if actual > budget:
        return "already_over_budget"
    if 10 * projected >= 11 * budget:
        return "high"
    return "watch" if projected > budget else "on_track"


def budget_metrics(projections):
    fields = ["schedule", "method", "evaluated_cost_center_months", "mae_cents", "true_positives", "false_positives",
              "false_negatives", "true_negatives", "precision", "recall"]
    if projections.empty:
        return pd.DataFrame(columns=fields)
    complete = projections.loc[projections["final_cost_cents"].notna()]
    rows = []
    for schedule, scores in complete.groupby("schedule"):
        actual = scores["final_cost_cents"] > scores["budget_cents"]
        for method, field in (
            ("month_to_date_run_rate", "projected_cost_cents"),
            ("blended_historical_run_rate", "blended_history_cost_cents"),
        ):
            predicted = scores[field] > scores["budget_cents"]
            tp = int((predicted & actual).sum())
            fp = int((predicted & ~actual).sum())
            fn = int((~predicted & actual).sum())
            rows.append({
                "schedule": schedule, "method": method, "evaluated_cost_center_months": len(scores),
                "mae_cents": (scores[field] - scores["final_cost_cents"]).abs().mean(),
                "true_positives": tp, "false_positives": fp, "false_negatives": fn,
                "true_negatives": len(scores) - tp - fp - fn,
                "precision": tp / (tp + fp) if tp + fp else None,
                "recall": tp / (tp + fn) if tp + fn else None,
            })
    return pd.DataFrame(rows, columns=fields)


def run(directory, requested_cutoff, output):
    if output.exists():
        raise ValueError(f"Output already exists: {output}")
    validate(directory)
    data, opening, cutoff = load_sources(directory, requested_cutoff)
    orders, batches, budgets = (data[table] for table in ("purchase_orders", "production_batches", "cost_center_budgets"))
    if batches.empty:
        raise ValueError(f"No production batches available through {cutoff.date()}.")
    purchases = purchase_anomalies(orders)
    variances = production_variances(batches, budgets)
    efficiency = batch_efficiency(batches)
    projections = budget_projections(batches, budgets, opening, cutoff)
    if not projections.empty:
        projections["final_cost_cents"] = projections["final_cost_cents"].astype("Int64")
        projections["actual_overrun"] = projections["actual_overrun"].astype("boolean")
    tables = {
        "purchase_anomalies": purchases, "production_variances": variances, "batch_efficiency": efficiency,
        "budget_risk": projections, "budget_metrics": budget_metrics(projections),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output.parent) as temporary:
        staging = Path(temporary) / "export"
        staging.mkdir()
        for name, table in tables.items():
            table.to_csv(staging / f"{name}.csv", index=False, float_format="%.6f")
        staging.rename(output)
    print(f"Saved {len(tables)} risk tables through {cutoff.date()} to {output.resolve()}")
    for name in ("purchase_anomalies", "production_variances", "batch_efficiency"):
        print(f"{name}: {tables[name]['flagged'].sum()} flagged of {len(tables[name])}")


def main():
    parser = argparse.ArgumentParser(description="Score purchase price, production, batch efficiency and budget risks.")
    parser.add_argument("--data", type=Path, default=Path("data/generated"))
    parser.add_argument("--output", type=Path, default=Path("data/risk"))
    parser.add_argument("--as-of", type=date.fromisoformat, help="Last calendar day of data to use, in YYYY-MM-DD.")
    args = parser.parse_args()
    try:
        run(args.data, args.as_of, args.output)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Risk scoring failed: {exc}\n")


if __name__ == "__main__":
    main()
