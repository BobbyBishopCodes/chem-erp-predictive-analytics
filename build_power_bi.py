import argparse
import base64
from collections import defaultdict
import csv
from datetime import date, timedelta
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from erp_artifact_sources import check_manifest, source_fingerprint
from validate_erp import validate

ROOT = "https://developer.microsoft.com/json-schemas/fabric/"
REPORT = "item/report/definition/"
NAME = "Linden Manufacturing"
MODEL = f"{NAME}.SemanticModel"
CURRENCY = "$#,0;($#,0);$0"
RATE = "0.0%"
ALGORITHMS = ("xgboost", "trailing_window", "prior_year_window")
INK = "#173347"
MUTED = "#496478"
TEAL = "#087E79"
NAVY = "#205275"
GOLD = "#9D611B"


def load(directory, name):
    with (directory / f"{name}.csv").open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def cents(value):
    return int(Decimal(value) * 100)


def facts(erp, forecast_dir):
    validate(erp)
    cutoff = check_manifest(forecast_dir, source_fingerprint(erp), ("forecasts", "backtest_predictions"))
    budget = load(erp, "cost_center_budgets")
    batches = load(erp, "production_batches")
    materials = {row["material_id"]: row["material_name"] for row in load(erp, "materials")}
    inventory = load(erp, "inventory_month_end")
    scope = {}
    production = {}
    for row in budget:
        product, center = row["product_id"], row["cost_center_id"]
        if scope.setdefault(product, center) != center:
            raise ValueError(f"Sales and finished inventory cannot be assigned to multiple cost centers for {product}.")
        production[row["month_start"][:7], product] = [0, int(row["total_budget_cents"]), 0, 0]
    months = sorted({row[0] for row in production})
    daily_costs = defaultdict(list)
    for row in batches:
        day = date.fromisoformat(row["posting_date"])
        product, center = row["product_id"], row["cost_center_id"]
        cost = int(row["total_cost_cents"])
        totals = production[day.isoformat()[:7], product]
        totals[0] += cost
        totals[2] += int(row["good_output_kg"])
        totals[3] += int(row["scrap_kg"])
        daily_costs[product, center].append((day, cost))
    sales = defaultdict(lambda: [0, 0])
    for row in load(erp, "sales_shipments"):
        totals = sales[row["posting_date"][:7], row["product_id"]]
        totals[0] += int(row["revenue_cents"])
        totals[1] += int(row["cogs_cents"])
    finished = defaultdict(int)
    raw = defaultdict(int)
    for row in inventory:
        month, material, value = row["month_end"][:7], row["material_id"], int(row["value_cents"])
        if material in scope:
            finished[month, material] += value
        else:
            raw[month] += value

    forecasts = {}
    issue = date.fromisoformat(cutoff)
    for row in load(forecast_dir, "forecasts"):
        if row["metric"] != "production_cost":
            continue
        product, center, days = row["product_id"], row["cost_center_id"], int(row["horizon_days"])
        if (product, days) in forecasts or scope[product] != center or row["as_of_date"] != cutoff or (
            date.fromisoformat(row["window_start"]) != issue + timedelta(days=1)
            or date.fromisoformat(row["window_end"]) != issue + timedelta(days=days)
        ):
            raise ValueError(f"Invalid forecast scope or horizon for {product}, {days} days.")
        begin = issue - timedelta(days=days - 1)
        actual = sum(cost for day, cost in daily_costs[product, center] if begin <= day <= issue)
        forecasts[product, days] = [cents(row["forecast_value"]), actual]

    errors = defaultdict(lambda: [0, 0, 0, 0, 0])
    seen = set()
    for row in load(forecast_dir, "backtest_predictions"):
        if row["metric"] != "production_cost":
            continue
        product, days = row["product_id"], int(row["horizon_days"])
        key = product, days, row["as_of_date"]
        if key in seen or scope[product] != row["cost_center_id"] or row["window_end"] > cutoff or (
            row["last_training_window_end"] > row["as_of_date"]
        ):
            raise ValueError(f"Invalid production backtest window: {key}")
        seen.add(key)
        actual = cents(row["actual_value"])
        window = errors[product, days]
        window[0] += abs(actual)
        window[1] += abs(cents(row["xgboost"]) - actual)
        window[2] += abs(cents(row["trailing_window"]) - actual)
        window[3] += abs(cents(row["prior_year_window"]) - actual)
        window[4] += 1

    expected = {(product, days) for product in scope for days in (30, 60, 90)}
    if set(forecasts) != expected or set(errors) != expected:
        raise ValueError("Production forecasts and backtests must cover each product at 30, 60 and 90 days.")
    rows = {
        "Operating Scope": [[product, materials[product], center] for product, center in sorted(scope.items())],
        "Calendar": [[month] for month in months],
        "Horizon": [[days, f"{days} days"] for days in (30, 60, 90)],
        "Production": [[month, product, *values] for (month, product), values in sorted(production.items())],
        "Sales": [[month, product, *values] for (month, product), values in sorted(sales.items())],
        "Finished Inventory": [[month, product, value] for (month, product), value in sorted(finished.items())],
        "Raw Inventory": [[month, value] for month, value in sorted(raw.items())],
        "Production Forecast": [[product, days, *values] for (product, days), values in sorted(forecasts.items())],
        "Production Backtest": [[product, days, *values] for (product, days), values in sorted(errors.items())],
    }
    return rows, cutoff


COLUMNS = {
    "Operating Scope": ("Product ID", "Product", "Cost center"),
    "Calendar": ("Month",),
    "Horizon": ("Days", "Window"),
    "Production": ("Month", "Product ID", "Cost cents", "Budget cents", "Good kg", "Scrap kg"),
    "Sales": ("Month", "Product ID", "Revenue cents", "COGS cents"),
    "Finished Inventory": ("Month", "Product ID", "Value cents"),
    "Raw Inventory": ("Month", "Value cents"),
    "Production Forecast": ("Product ID", "Days", "Forecast cents", "Prior cost cents"),
    "Production Backtest": ("Product ID", "Days", "Actual magnitude cents", "XGBoost error cents",
                            "Recent error cents", "Seasonal error cents", "Windows"),
}
TEXT_COLUMNS = {"Product ID", "Product", "Cost center", "Month", "Window"}
DIMENSIONS = {"Operating Scope", "Calendar", "Horizon"}

MEASURES = {
    "Production": [
        ("Actual cost", "DIVIDE(SUM(Production[Cost cents]), 100)", CURRENCY),
        ("Budget cost", "DIVIDE(SUM(Production[Budget cents]), 100)", CURRENCY),
        ("Production yield", "DIVIDE(SUM(Production[Good kg]), SUM(Production[Good kg]) + SUM(Production[Scrap kg]))", RATE),
    ],
    "Sales": [
        ("Gross margin", "DIVIDE(SUM(Sales[Revenue cents]) - SUM(Sales[COGS cents]), SUM(Sales[Revenue cents]))", RATE),
    ],
    "Finished Inventory": [
        ("Finished goods", "DIVIDE(SUM('Finished Inventory'[Value cents]), 100)", CURRENCY),
        ("Ending finished inventory", "CALCULATE([Finished goods], 'Calendar'[Month] = MAX('Calendar'[Month]))", CURRENCY),
    ],
    "Raw Inventory": [
        ("Raw materials (company)", "DIVIDE(SUM('Raw Inventory'[Value cents]), 100)", CURRENCY),
    ],
    "Production Forecast": [
        ("XGBoost forecast", "DIVIDE(SUM('Production Forecast'[Forecast cents]), 100)", CURRENCY),
        ("Preceding actual", "DIVIDE(SUM('Production Forecast'[Prior cost cents]), 100)", CURRENCY),
    ],
    "Production Backtest": [
        ("XGBoost", "DIVIDE(SUM('Production Backtest'[XGBoost error cents]), SUM('Production Backtest'[Actual magnitude cents]))", RATE),
        ("Recent baseline", "DIVIDE(SUM('Production Backtest'[Recent error cents]), SUM('Production Backtest'[Actual magnitude cents]))", RATE),
        ("Seasonal baseline", "DIVIDE(SUM('Production Backtest'[Seasonal error cents]), SUM('Production Backtest'[Actual magnitude cents]))", RATE),
    ],
}


def model_table(name, rows):
    columns = COLUMNS[name]
    payload = base64.b64encode(json.dumps(rows, separators=(",", ":"), ensure_ascii=False).encode()).decode()
    types = ", ".join(
        f'{{"{column}", {"type text" if column in TEXT_COLUMNS else "Int64.Type"}}}' for column in columns
    )
    headers = "{" + ", ".join(f'"{column}"' for column in columns) + "}"
    expression = [
        "let",
        f'    Rows = Json.Document(Binary.FromText("{payload}", BinaryEncoding.Base64)),',
        f"    Source = Table.FromRows(Rows, {headers}),",
        f'    Typed = Table.TransformColumnTypes(Source, {{{types}}}, "en-US")',
        "in",
        "    Typed",
    ]
    table = {"name": name, "columns": [
        {"name": column, "dataType": "string" if column in TEXT_COLUMNS else "int64", "sourceColumn": column,
         "summarizeBy": "none", "isHidden": name not in DIMENSIONS or column == "Product ID"}
        for column in columns
    ], "partitions": [{"name": name, "mode": "import", "source": {"type": "m", "expression": expression}}]}
    if name == "Horizon":
        table["columns"][1]["sortByColumn"] = "Days"
    if name in MEASURES:
        table["measures"] = [
            {"name": measure, "expression": dax, "formatString": format_string}
            for measure, dax, format_string in MEASURES[name]
        ]
    return table


def model_definition(rows):
    relationships = []
    for table, columns in COLUMNS.items():
        if table not in DIMENSIONS:
            for column, dimension in (("Month", "Calendar"), ("Product ID", "Operating Scope"), ("Days", "Horizon")):
                if column in columns:
                    relationships.append({
                        "name": f"{table} to {dimension}", "fromTable": table, "fromColumn": column,
                        "toTable": dimension, "toColumn": column, "crossFilteringBehavior": "oneDirection",
                    })
    return {"name": NAME, "compatibilityLevel": 1601, "model": {
        "culture": "en-US", "sourceQueryCulture": "en-US", "defaultPowerBIDataSourceVersion": "powerBI_V3",
        "tables": [model_table(table, data) for table, data in rows.items()],
        "relationships": relationships,
    }}


def write(directory, relative, values, schema=None):
    path = directory / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    content = {"$schema": ROOT + schema + "/schema.json", **values} if schema else values
    path.write_text(json.dumps(content, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def literal(value):
    if isinstance(value, bool):
        value = str(value).lower()
    elif isinstance(value, int):
        value = f"{value}D"
    else:
        value = "'" + value.replace("'", "''") + "'"
    return {"expr": {"Literal": {"Value": value}}}


def color(value):
    return {"solid": {"color": literal(value)}}


def format_object(**properties):
    return [{"properties": {key: literal(value) for key, value in properties.items()}}]


def projection(table, name, measure=False):
    kind = "Measure" if measure else "Column"
    result = {"field": {kind: {"Expression": {"SourceRef": {"Entity": table}}, "Property": name}},
              "queryRef": f"{table}.{name}", "nativeQueryRef": name}
    if not measure:
        result["active"] = True
    return result


def textbox(lines):
    paragraphs = [
        {"textRuns": [{"value": value, "textStyle": {"fontFamily": font, "fontSize": f"{size}px", "color": fill}}],
         "horizontalTextAlignment": "left"}
        for value, size, fill, font in lines
    ]
    return {"visualType": "textbox", "objects": {"general": [{"properties": {"paragraphs": paragraphs}}]},
            "visualContainerObjects": {"background": format_object(show=False),
                                       "padding": format_object(top=0, bottom=0, left=0, right=0)}}


def chrome(title):
    return {
        "title": [{"properties": {"show": literal(True), "text": literal(title), "fontColor": color(INK),
                                  "fontFamily": literal("Segoe UI Semibold"), "fontSize": literal(13)}}],
        "background": [{"properties": {"show": literal(True), "color": color("#FFFFFF"), "transparency": literal(0)}}],
        "border": [{"properties": {"show": literal(True), "color": color("#D7E4EB"), "radius": literal(8)}}],
        "padding": format_object(top=10, bottom=8, left=10, right=10),
    }


def chart(kind, title, category, measures):
    category_field = projection(*category)
    series = [projection(table, field, True) for table, field, _ in measures]
    styles = [
        {"properties": {"fill": color(fill)}, "selector": {"metadata": field["queryRef"]}}
        for field, (_, _, fill) in zip(series, measures)
    ]
    return {
        "visualType": kind,
        "query": {
            "queryState": {"Category": {"projections": [category_field]}, "Y": {"projections": series}},
            "sortDefinition": {"sort": [{"field": category_field["field"], "direction": "Ascending"}]},
        },
        "objects": {
            "legend": format_object(show=len(series) > 1, position="Top", showTitle=False, fontSize=10),
            "categoryAxis": format_object(showAxisTitle=False, fontSize=9),
            "valueAxis": format_object(showAxisTitle=False, fontSize=9),
            "dataPoint": styles,
        },
        "visualContainerObjects": chrome(title), "drillFilterOtherVisuals": False,
    }


def card(title, table, field):
    return {
        "visualType": "card", "query": {"queryState": {"Fields": {"projections": [projection(table, field, True)]}}},
        "objects": {"categoryLabels": format_object(show=False), "labels": format_object(fontSize=24)},
        "visualContainerObjects": chrome(title),
    }


def slicer(field):
    return {
        "visualType": "slicer",
        "query": {"queryState": {"Values": {"projections": [projection("Operating Scope", field)]}}},
        "objects": {"data": format_object(mode="Dropdown"), "header": format_object(show=True, text=field)},
        "drillFilterOtherVisuals": True,
    }


def report_definition(directory, rows, cutoff):
    model = directory / MODEL
    write(model, "definition.pbism", {"version": "1.0"}, "item/semanticModel/definitionProperties/1.0.0")
    write(model, "model.bim", model_definition(rows))
    report = directory / f"{NAME}.Report"
    write(report, "definition.pbir", {"version": "4.0", "datasetReference": {"byPath": {"path": f"../{MODEL}"}}},
          "item/report/definitionProperties/2.0.0")
    definition = report / "definition"
    write(definition, "report.json", {"themeCollection": {}}, REPORT + "report/3.0.0")
    write(definition, "version.json", {"version": "2.0.0"}, REPORT + "versionMetadata/1.0.0")
    page = "ManufacturingPerformance"
    write(definition, "pages/pages.json", {"pageOrder": [page], "activePageName": page}, REPORT + "pagesMetadata/1.0.0")
    location = definition / "pages" / page
    write(location, "page.json", {"name": page, "displayName": "Manufacturing performance", "displayOption": "FitToPage",
          "width": 1440, "height": 1050, "objects": {
              "background": [{"properties": {"color": color("#EFF5F7"), "transparency": literal(0)}}]
          }}, REPORT + "page/2.0.0")
    period = f"{rows['Calendar'][0][0]} to {rows['Calendar'][-1][0]}"
    header = textbox([
        ("Manufacturing performance", 31, INK, "Segoe UI Semibold"),
        (f"Linden Process Materials. Synthetic ERP data from {period}. Forecasts issued {cutoff}.",
         14, MUTED, "Segoe UI"),
    ])
    visuals = [
        (24, 16, 840, 77, header),
        (914, 23, 252, 68, slicer("Product")),
        (1182, 23, 234, 68, slicer("Cost center")),
        (24, 108, 336, 87, card("Production cost", "Production", "Actual cost")),
        (376, 108, 336, 87, card("Gross margin", "Sales", "Gross margin")),
        (728, 108, 336, 87, card("Ending finished inventory", "Finished Inventory", "Ending finished inventory")),
        (1080, 108, 336, 87, card("Production yield", "Production", "Production yield")),
        (24, 211, 688, 258, chart("lineChart", "Monthly production costs (USD)", ("Calendar", "Month"),
            [("Production", "Actual cost", TEAL), ("Production", "Budget cost", NAVY)])),
        (728, 211, 688, 258, chart("clusteredColumnChart", "Forecast vs. preceding actual window (USD)", ("Horizon", "Window"),
            [("Production Forecast", "XGBoost forecast", TEAL), ("Production Forecast", "Preceding actual", NAVY)])),
        (24, 485, 688, 258, chart("lineChart", "Gross margin by month", ("Calendar", "Month"),
            [("Sales", "Gross margin", TEAL)])),
        (728, 485, 688, 258, chart("lineChart", "Month-end inventory value (USD)", ("Calendar", "Month"),
            [("Finished Inventory", "Finished goods", TEAL), ("Raw Inventory", "Raw materials (company)", NAVY)])),
        (24, 759, 688, 258, chart("lineChart", "Production yield by month", ("Calendar", "Month"),
            [("Production", "Production yield", TEAL)])),
        (728, 759, 688, 258, chart("clusteredColumnChart", "Production cost forecast error (WAPE)",
            ("Horizon", "Window"),
            [("Production Backtest", "XGBoost", TEAL), ("Production Backtest", "Recent baseline", NAVY),
             ("Production Backtest", "Seasonal baseline", GOLD)])),
    ]
    for index, (x, y, width, height, visual) in enumerate(visuals, start=1):
        name = f"{index:020x}"
        write(location, f"visuals/{name}/visual.json", {"name": name, "position": {
            "x": x, "y": y, "z": index * 1000, "width": width, "height": height, "tabOrder": index * 1000
        }, "visual": visual}, REPORT + "visualContainer/2.9.0")
    write(directory, f"{NAME}.pbip", {"version": "1.0", "artifacts": [{"report": {"path": report.name}}]},
          "pbip/pbipProperties/1.0.0")


def main():
    parser = argparse.ArgumentParser(description="Build a local Power BI manufacturing report from ERP and forecast exports.")
    parser.add_argument("--data", type=Path, default=Path("data/generated"))
    parser.add_argument("--forecasts", type=Path, default=Path("data/forecasts"))
    parser.add_argument("--output", type=Path, default=Path("outputs/manufacturing-performance/power-bi"))
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise ValueError(f"Output already exists: {args.output}")
        rows, cutoff = facts(args.data, args.forecasts)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=args.output.parent) as temporary:
            staging = Path(temporary) / "report"
            report_definition(staging, rows, cutoff)
            staging.rename(args.output)
        print(f"Created {(args.output / (NAME + '.pbip')).resolve()}")
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Power BI build failed: {exc}\n")


if __name__ == "__main__":
    main()
