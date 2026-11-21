import argparse
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pandas as pd
from xgboost import XGBRegressor

from erp_artifact_sources import write_manifest
from validate_erp import validate

HORIZONS = (30, 60, 90)
LOOKBACKS = (7, 14, 30, 60, 90)
SEASONAL_LAG = 364
MIN_TRAINING_ROWS = 120
MODEL_PARAMETERS = {
    "objective": "reg:squarederror", "tree_method": "hist", "n_estimators": 100,
    "max_depth": 3, "learning_rate": 0.045, "min_child_weight": 12,
    "subsample": 0.85, "colsample_bytree": 0.85, "random_state": 413, "n_jobs": 1,
}


@dataclass(frozen=True)
class Target:
    metric: str
    table: str
    date_field: str
    groups: tuple[str, ...]
    numerator: str
    denominator: str | None = None

    @property
    def unit(self):
        return "USD/kg" if self.denominator else "USD"

    def display(self, value):
        return round(float(value), 4 if self.denominator else 2)


TARGETS = (
    Target("raw_material_unit_cost", "purchase_orders", "order_date", ("material_id",),
           "amount_cents", "quantity_kg"),
    Target("raw_material_purchase_spend", "purchase_orders",
           "order_date", ("material_id",), "amount_cents"),
    Target("production_cost", "production_batches", "posting_date", ("cost_center_id", "product_id"),
           "total_cost_cents"),
    Target("product_gross_profit", "sales_shipments",
           "posting_date", ("product_id",), "gross_profit_cents"),
)
KEY_FIELDS = ["metric", "material_id", "cost_center_id",
              "product_id", "unit", "horizon_days"]
ALGORITHMS = ("xgboost", "trailing_window", "prior_year_window")


def load_sources(directory, historical_cutoff):
    sources = {target.table: target.date_field for target in TARGETS}
    tables = {
        table: pd.read_csv(
            directory / f"{table}.csv", parse_dates=[date_field])
        for table, date_field in sources.items()
    }
    opening = pd.read_csv(directory / "gl_entries.csv", usecols=["source_type", "posting_date"],
                          parse_dates=["posting_date"])
    start = opening.loc[opening["source_type"].eq(
        "OPENING"), "posting_date"].iloc[0]
    snapshot_dates = pd.read_csv(directory / "inventory_month_end.csv", usecols=["month_end"],
                                 parse_dates=["month_end"])
    end = snapshot_dates["month_end"].max()
    if historical_cutoff is not None:
        cutoff = pd.Timestamp(historical_cutoff)
        if cutoff > end:
            raise ValueError(
                f"--as-of exceeds the final reconciled ERP day {end.date()}")
        end = cutoff
    if (end - start).days < SEASONAL_LAG + max(HORIZONS):
        raise ValueError(
            "At least 455 calendar days of ERP history are required for backtesting.")
    sales = tables["sales_shipments"]
    sales["gross_profit_cents"] = sales["revenue_cents"] - sales["cogs_cents"]
    return tables, pd.date_range(start, end, name="date")


def daily_series(target, tables, dates):
    source = tables[target.table]
    source = source.loc[source[target.date_field] <= dates[-1]]
    fields = [target.numerator]
    if target.denominator:
        fields.append(target.denominator)
    for values, rows in source.groupby(list(target.groups), sort=True):
        keys = dict(zip(target.groups, values))
        daily = rows.groupby(target.date_field)[
            fields].sum().reindex(dates, fill_value=0)
        yield keys, daily


def window_value(target, daily, length):
    totals = daily.rolling(length).sum()
    value = totals[target.numerator] / 100
    if target.denominator:
        value = value / totals[target.denominator].replace(0, np.nan)
    return value


def calendar_features(dates, horizon):
    windows = pd.date_range(dates[0], dates[-1] + pd.Timedelta(days=horizon))
    phase = 2 * np.pi * (windows.dayofyear.to_numpy() - 1) / 365.2425
    day_features = pd.DataFrame({
        "window_year_sin": np.sin(phase), "window_year_cos": np.cos(phase),
        "window_weekdays": (windows.dayofweek < 5).astype(int),
    }, index=windows)
    return day_features.rolling(horizon).sum().shift(-horizon).reindex(dates)


def make_frame(target, daily, horizon):
    dates = daily.index
    features = calendar_features(dates, horizon)
    for days in LOOKBACKS:
        features[f"recent_value_{days}d"] = window_value(target, daily, days)
        if target.denominator:
            features[f"recent_purchased_kg_{days}d"] = daily[target.denominator].rolling(
                days).sum()
    trailing = window_value(target, daily, horizon)
    features["trailing_window"] = trailing
    targets = pd.DataFrame({
        "window_end": dates + pd.Timedelta(days=horizon),
        "actual_value": trailing.shift(-horizon),
        "prior_year_window": trailing.shift(SEASONAL_LAG - horizon),
    }, index=dates)
    targets["residual"] = targets["actual_value"] - trailing
    return features, targets


def fit_at(features, targets, cutoff):
    eligible = (targets["window_end"] <= cutoff) & targets["residual"].notna()
    eligible &= features.index >= features.index[0] + \
        pd.Timedelta(days=max(LOOKBACKS) - 1)
    train = features.loc[eligible]
    if len(train) < MIN_TRAINING_ROWS:
        return None
    model = XGBRegressor(**MODEL_PARAMETERS)
    model.fit(train, targets.loc[eligible, "residual"])
    return model, len(train), targets.loc[eligible, "window_end"].max()


def predict(target, model, features, cutoff):
    snapshot = features.loc[[cutoff]]
    trailing = snapshot["trailing_window"].iat[0]
    if pd.isna(trailing):
        raise ValueError(
            f"{target.metric} has no trailing observations as of {cutoff.date()}")
    forecast = float(model.predict(snapshot)[0]) + trailing
    if target.metric != "product_gross_profit":
        forecast = max(0, forecast)
    if not np.isfinite(forecast):
        raise ValueError(
            f"{target.metric} produced a non-finite forecast as of {cutoff.date()}")
    return forecast


def window_fields(cutoff, horizon):
    return {
        "as_of_date": cutoff.date(),
        "window_start": (cutoff + pd.Timedelta(days=1)).date(),
        "window_end": (cutoff + pd.Timedelta(days=horizon)).date(),
    }


def historical_predictions(target, keys, features, targets, horizon):
    results = []
    earliest = features.index[0] + pd.Timedelta(days=SEASONAL_LAG)
    latest = features.index[-1] - pd.Timedelta(days=horizon)
    candidates = targets.loc[earliest:latest].dropna(
        subset=["actual_value", "prior_year_window", "residual"])
    for cutoff, row in candidates.loc[candidates.index.is_month_end].iterrows():
        fitted = fit_at(features, targets, cutoff)
        if fitted is None:
            continue
        model, samples, last_label_date = fitted
        results.append({
            **keys, **window_fields(cutoff, horizon),
            "training_rows": samples, "last_training_window_end": last_label_date.date(),
            "actual_value": target.display(row["actual_value"]),
            "xgboost": target.display(predict(target, model, features, cutoff)),
            "trailing_window": target.display(features.at[cutoff, "trailing_window"]),
            "prior_year_window": target.display(row["prior_year_window"]),
        })
    return results


def metrics(results):
    rows = []
    for keys, folds in results.groupby(KEY_FIELDS, sort=False, dropna=False):
        identity = dict(zip(KEY_FIELDS, keys))
        actual = folds["actual_value"]
        magnitude = actual.abs().sum()
        for algorithm in ALGORITHMS:
            error = folds[algorithm] - actual
            absolute = error.abs()
            rows.append({
                **identity, "algorithm": algorithm, "backtest_windows": len(folds),
                "first_as_of_date": folds["as_of_date"].min(), "last_as_of_date": folds["as_of_date"].max(),
                "mae": absolute.mean(), "rmse": np.sqrt(error.pow(2).mean()),
                "wape_pct": 100 * absolute.sum() / magnitude if magnitude else np.nan,
                "mean_error": error.mean(),
            })
    return pd.DataFrame(rows)


def run(directory, cutoff, output):
    if output.exists():
        raise ValueError(f"Output already exists: {output}")
    validate(directory)
    tables, dates = load_sources(directory, cutoff)
    cutoff = dates[-1]
    forecasts = []
    backtests = []
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output.parent) as temporary:
        staging = Path(temporary) / "export"
        model_dir = staging / "models"
        model_dir.mkdir(parents=True)
        for target in TARGETS:
            for group, daily in daily_series(target, tables, dates):
                for horizon in HORIZONS:
                    keys = dict.fromkeys(KEY_FIELDS, "")
                    keys.update(group, metric=target.metric,
                                unit=target.unit, horizon_days=horizon)
                    features, targets = make_frame(target, daily, horizon)
                    folds = historical_predictions(
                        target, keys, features, targets, horizon)
                    if not folds:
                        raise ValueError(
                            f"No completed historical backtest windows for {target.metric} {group}")
                    backtests.extend(folds)
                    model, samples, last_label_date = fit_at(
                        features, targets, cutoff)
                    model_name = f"model_{len(forecasts) + 1:03d}.json"
                    model.save_model(model_dir / model_name)
                    forecasts.append({
                        **keys, **window_fields(cutoff, horizon),
                        "forecast_value": target.display(predict(target, model, features, cutoff)),
                        "algorithm": "xgboost", "model_file": f"models/{model_name}",
                        "training_rows": samples, "last_training_window_end": last_label_date.date(),
                    })

        historical = pd.DataFrame(backtests)
        pd.DataFrame(forecasts).to_csv(staging / "forecasts.csv", index=False)
        historical.to_csv(staging / "backtest_predictions.csv", index=False)
        metrics(historical).to_csv(staging / "model_metrics.csv",
                                   index=False, float_format="%.6f")
        write_manifest(staging, directory, cutoff.date())
        staging.rename(output)
    print(
        f"Saved {len(forecasts)} forecasts and {len(backtests)} backtest predictions to {output.resolve()}")


def main():
    parser = argparse.ArgumentParser(
        description="Forecast 30-, 60-, and 90-day costs and gross profit from ERP exports.")
    parser.add_argument("--data", type=Path, default=Path("data/generated"))
    parser.add_argument("--output", type=Path, default=Path("data/forecasts"))
    parser.add_argument("--as-of", type=date.fromisoformat,
                        help="Last calendar day of data to use, in YYYY-MM-DD.")
    args = parser.parse_args()
    try:
        run(args.data, args.as_of, args.output)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Forecasting failed: {exc}\n")


if __name__ == "__main__":
    main()
