import argparse
import csv
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_table(directory, table, *key_fields):
    with (directory / f"{table}.csv").open(newline="", encoding="utf-8") as stream:
        rows = {}
        for row in csv.DictReader(stream):
            require(None not in row and None not in row.values(), f"Malformed row in {table}")
            keys = tuple(row[field] for field in key_fields)
            key = keys[0] if len(keys) == 1 else keys
            require(all(keys) and key not in rows, f"Missing or duplicate key in {table}: {key}")
            rows[key] = row
    require(rows, f"Empty table: {table}")
    return rows


def numbers(row, *fields):
    return tuple(int(row[field]) for field in fields)


def signed(row):
    return int(row["debit_cents"]) - int(row["credit_cents"])


def currency(cents):
    return f"${cents / 100:,.2f}"


def validate(directory):
    tables = {
        "company_codes": "company_code", "plants": "plant_id", "materials": "material_id",
        "suppliers": "supplier_id", "customers": "customer_id", "cost_centers": "cost_center_id",
        "accounts": "account_id", "purchase_orders": "po_id", "supplier_transactions": "transaction_id",
        "production_batches": "batch_id", "sales_shipments": "shipment_id", "customer_receipts": "receipt_id",
        "material_movements": "movement_id", "inventory_month_end": "month_end plant_id material_id",
        "cost_center_budgets": "month_start cost_center_id product_id", "gl_entries": "document_id line_no",
    }
    data = {name: read_table(directory, name, *keys.split()) for name, keys in tables.items()}
    documents = defaultdict(list)
    trial_balance = Counter()
    for (doc, _), row in data["gl_entries"].items():
        debit, credit = numbers(row, "debit_cents", "credit_cents")
        require((debit > 0 and credit == 0) or (credit > 0 and debit == 0), f"Invalid GL line in {doc}")
        require(row["account_id"] in data["accounts"], f"Unknown account in {doc}")
        require(row["company_code"] in data["company_codes"], f"Unknown company in {doc}")
        for field, table in (("cost_center_id", "cost_centers"), ("material_id", "materials")):
            require(not row[field] or row[field] in data[table], f"Unknown {field} in {doc}")
        documents[doc].append(row)
        trial_balance[row["account_id"]] += debit - credit

    posted = {}
    for doc, lines in documents.items():
        metadata = {(r["posting_date"], r["source_type"], r["source_id"], r["cost_center_id"],
                     r["company_code"]) for r in lines}
        require(len(metadata) == 1 and sum(map(signed, lines)) == 0, f"Unbalanced or inconsistent journal: {doc}")
        day, kind, source, center, company = metadata.pop()
        require((kind, source) not in posted, f"Duplicate posting for {kind} {source}")
        posted[kind, source] = (doc, day, center, company, Counter(
            (r["account_id"], r["material_id"], signed(r)) for r in lines
        ))

    plants = data["plants"]
    centers = data["cost_centers"]
    for plant in plants.values():
        require(plant["company_code"] in data["company_codes"], "Unknown plant company")
    for center in centers.values():
        require(center["plant_id"] in plants, "Unknown cost center plant")

    opening = posted.pop(("OPENING", "OPENING"))
    start = date.fromisoformat(opening[1])
    require((start.month, start.day) == (1, 1), "Expected January 1 opening")
    stop = start.replace(year=start.year + 3)
    expected_months = {date(year, month, 1) for year in range(start.year, stop.year)
                       for month in range(1, 13)}
    opening_amount = sum(amount * count for (account, _, amount), count in opening[4].items()
                         if account == "1000")
    require(opening_amount > 0 and opening[4] == Counter(
        [("1000", "", opening_amount), ("3000", "", -opening_amount)]
    ), "Invalid opening entry")
    for table, field in (
        ("purchase_orders", "order_date"), ("supplier_transactions", "posting_date"),
        ("production_batches", "posting_date"), ("sales_shipments", "posting_date"),
        ("customer_receipts", "posting_date"), ("material_movements", "posting_date"),
        ("inventory_month_end", "month_end"), ("cost_center_budgets", "month_start"), ("gl_entries", "posting_date"),
    ):
        for row in data[table].values():
            require(start <= date.fromisoformat(row[field]) < stop, f"Date outside the export period in {table}")

    def check_posting(kind, source, day, plant, entries, center=""):
        posting = posted.pop((kind, source), None)
        require(posting is not None, f"Missing journal for {kind} {source}")
        doc, posting_day, cost_center, company, actual = posting
        expected = Counter((account, material, amount) for account, material, amount in entries if amount)
        require((posting_day, cost_center, company, actual) ==
                (day, center, plants[plant]["company_code"], expected), f"GL mismatch for {kind} {source}")
        return doc

    movement_groups = defaultdict(list)
    inventory_events = defaultdict(list)
    for movement_id, row in data["material_movements"].items():
        plant, material = row["plant_id"], row["material_id"]
        require(plant in plants and material in data["materials"], f"Unknown movement master data: {movement_id}")
        movement_groups[row["source_id"]].append(row)
        inventory_events[date.fromisoformat(row["posting_date"])].append(row)

    def check_movement(row, doc, day, plant, material, kind, quantity, value):
        require(
            (row["document_id"], row["posting_date"], row["plant_id"], row["material_id"], row["movement_type"],
             *numbers(row, "quantity_kg", "value_cents")) == (doc, day, plant, material, kind, quantity, value),
            f"Movement mismatch: {row['movement_id']}"
        )

    purchases = data["purchase_orders"]
    transactions = defaultdict(dict)
    for transaction_id, row in data["supplier_transactions"].items():
        po, kind = row["po_id"], row["transaction_type"]
        require(po in purchases and kind not in transactions[po], f"Unlinked or duplicate supplier event: {transaction_id}")
        transactions[po][kind] = row

    open_payables = open_receipts = 0
    for po, row in purchases.items():
        material, plant = row["material_id"], row["plant_id"]
        require(plant in plants and row["supplier_id"] in data["suppliers"], f"Unknown PO master data: {po}")
        require(data["materials"][material]["material_type"] == "RAW", f"Invalid purchased material: {po}")
        qty, price, amount = numbers(row, "quantity_kg", "unit_price_cents_per_kg", "amount_cents")
        require(qty > 0 and price > 0 and qty * price == amount, f"PO price extension mismatch: {po}")
        received = date.fromisoformat(row["expected_receipt_date"])
        require(received >= date.fromisoformat(row["order_date"]), f"Receipt scheduled before order: {po}")
        events = transactions[po]
        entries = {
            "101_GOODS_RECEIPT": [("1200", material, amount), ("2000", material, -amount)],
            "SUPPLIER_INVOICE": [("2000", material, amount), ("2100", "", -amount)],
            "SUPPLIER_PAYMENT": [("2100", "", amount), ("1000", "", -amount)],
        }
        kinds = list(entries)
        require(set(events) == set(kinds[:len(events)]) and (kinds[0] in events) == (received < stop),
                f"Incomplete supplier event sequence: {po}")
        previous = row["order_date"]
        movements = movement_groups.pop(po, [])
        require(len(movements) == int(kinds[0] in events), f"Incorrect receipt movement count: {po}")
        for kind in kinds[:len(events)]:
            event = events[kind]
            day = event["posting_date"]
            require(day >= previous and int(event["amount_cents"]) == amount, f"Supplier event mismatch: {po}")
            doc = check_posting(kind, event["transaction_id"], day, plant, entries[kind])
            require(event["document_id"] == doc, f"Supplier journal link mismatch: {po}")
            if kind == kinds[0]:
                require(day == received.isoformat(), f"Receipt date mismatch: {po}")
                check_movement(movements[0], doc, day, plant, material, kind, qty, amount)
            previous = day
        if len(events) == 1:
            open_receipts += amount
        if len(events) == 2:
            open_payables += amount

    production_cost = 0
    budget_keys = set(data["cost_center_budgets"])
    for batch, row in data["production_batches"].items():
        day, center, product = row["posting_date"], row["cost_center_id"], row["product_id"]
        plant = centers[center]["plant_id"]
        require(data["materials"][product]["material_type"] == "FINISHED", f"Invalid batch product: {batch}")
        require((day[:7] + "-01", center, product) in budget_keys, f"Missing budget for batch: {batch}")
        planned, good, scrap, materials, labor, overhead, total = numbers(
            row, "planned_output_kg", "good_output_kg", "scrap_kg", "material_cost_cents",
            "labor_cost_cents", "overhead_cost_cents", "total_cost_cents"
        )
        require(min(planned, good, materials, labor, overhead) > 0 and scrap >= 0 and
                total == materials + labor + overhead, f"Invalid batch totals: {batch}")
        movements = movement_groups.pop(batch, [])
        issues = [r for r in movements if r["movement_type"] == "261_BATCH_ISSUE"]
        outputs = [r for r in movements if r["movement_type"] == "101_BATCH_RECEIPT"]
        require(issues and len(outputs) == 1 and len(movements) == len(issues) + 1,
                f"Incomplete batch movements: {batch}")
        consumed = [numbers(r, "quantity_kg", "value_cents") for r in issues]
        require(all(qty < 0 and value < 0 for qty, value in consumed) and
                -sum(qty for qty, _ in consumed) == good + scrap and
                -sum(value for _, value in consumed) == materials, f"Batch mass or cost mismatch: {batch}")
        entries = [("1200", r["material_id"], int(r["value_cents"])) for r in issues]
        entries.extend([("1220", product, materials), ("1220", product, labor), ("1220", product, overhead),
                        ("1000", "", -labor), ("1000", "", -overhead), ("1210", product, total),
                        ("1220", product, -total)])
        doc = check_posting("PRODUCTION", batch, day, plant, entries, center)
        for issue, (qty, value) in zip(issues, consumed):
            material = issue["material_id"]
            require(data["materials"][material]["material_type"] == "RAW", f"Invalid batch input: {batch}")
            check_movement(issue, doc, day, plant, material, "261_BATCH_ISSUE", qty, value)
        check_movement(outputs[0], doc, day, plant, product, "101_BATCH_RECEIPT", good, total)
        production_cost += total

    sales = data["sales_shipments"]
    payments = {}
    for receipt, row in data["customer_receipts"].items():
        shipment = row["shipment_id"]
        require(shipment in sales and shipment not in payments, f"Unlinked or duplicate receipt: {receipt}")
        payments[shipment] = row

    revenue_total = cogs_total = receivables = 0
    for shipment, row in sales.items():
        day, plant, product = row["posting_date"], row["plant_id"], row["product_id"]
        require(plant in plants and row["customer_id"] in data["customers"] and
                data["materials"][product]["material_type"] == "FINISHED", f"Invalid sales master data: {shipment}")
        demand, shipped, price, revenue, cogs = numbers(
            row, "demand_kg", "shipped_kg", "unit_price_cents_per_kg", "revenue_cents", "cogs_cents"
        )
        require(demand > 0 and 0 <= shipped <= demand and price > 0 and revenue == shipped * price and
                (cogs > 0 if shipped else cogs == 0), f"Invalid shipment amounts: {shipment}")
        movements = movement_groups.pop(shipment, [])
        require(len(movements) == int(shipped > 0), f"Incorrect shipment movement count: {shipment}")
        if shipped:
            doc = check_posting("SALE", shipment, day, plant, [
                ("1100", "", revenue), ("4000", product, -revenue),
                ("5000", product, cogs), ("1210", product, -cogs)
            ])
            check_movement(movements[0], doc, day, plant, product, "601_CUSTOMER_ISSUE", -shipped, -cogs)
        receipt = payments.get(shipment)
        if receipt:
            paid_on = receipt["posting_date"]
            require(shipped and paid_on >= day and int(receipt["amount_cents"]) == revenue,
                    f"Customer payment mismatch: {shipment}")
            doc = check_posting("CUSTOMER_PAYMENT", receipt["receipt_id"], paid_on, plant,
                                [("1000", "", revenue), ("1100", "", -revenue)])
            require(doc == receipt["document_id"], f"Customer payment document mismatch: {shipment}")
        else:
            receivables += revenue
        revenue_total += revenue
        cogs_total += cogs

    require(not posted and not movement_groups, "Unmatched journals or material movements")

    budget_months = defaultdict(set)
    budget_total = 0
    for (month, center, product), row in data["cost_center_budgets"].items():
        planned, materials, labor, overhead, total = numbers(
            row, "planned_output_kg", "material_budget_cents", "labor_budget_cents", "overhead_budget_cents",
            "total_budget_cents"
        )
        require(center in centers and data["materials"][product]["material_type"] == "FINISHED", "Invalid budget key")
        require(min(planned, materials, labor, overhead) > 0 and total == materials + labor + overhead,
                f"Budget totals mismatch: {month} {center} {product}")
        budget_months[center, product].add(date.fromisoformat(month))
        budget_total += total
    batch_pairs = {(row["cost_center_id"], row["product_id"]) for row in data["production_batches"].values()}
    require(set(budget_months) == batch_pairs and all(months == expected_months for months in budget_months.values()),
            "Budgets do not cover every month for each producing cost center and product")

    balances = {(plant, material): (0, 0) for plant in plants for material in data["materials"]}
    snapshots = dict(data["inventory_month_end"])
    day = start
    one_day = timedelta(days=1)
    while day < stop:
        for row in sorted(inventory_events[day], key=lambda movement: movement["movement_id"]):
            key = row["plant_id"], row["material_id"]
            on_hand, carrying = balances[key]
            qty, value = numbers(row, "quantity_kg", "value_cents")
            if qty < 0:
                require(on_hand >= -qty, f"Negative inventory at {row['movement_id']}")
                expected = (carrying * -qty * 2 + on_hand) // (on_hand * 2)
                require(-value == expected, f"Moving-average valuation mismatch at {row['movement_id']}")
            balance = on_hand + qty, carrying + value
            require(min(balance) >= 0 and (balance[0] > 0 or balance[1] == 0),
                    f"Invalid inventory balance at {row['movement_id']}")
            balances[key] = balance
        if (day + one_day).month != day.month:
            for key, balance in balances.items():
                snapshot = snapshots.pop((day.isoformat(), *key), None)
                require(snapshot is not None and numbers(snapshot, "quantity_kg", "value_cents") == balance,
                        f"Inventory roll-forward mismatch: {day} {key}")
        day += one_day
    require(not snapshots, "Unexpected inventory snapshots")

    inventory_totals = Counter()
    for (_, material), (_, value) in balances.items():
        inventory_totals[data["materials"][material]["material_type"]] += value
    controls = {
        "1100": receivables, "1200": inventory_totals["RAW"], "1210": inventory_totals["FINISHED"],
        "1220": 0, "2000": -open_receipts, "2100": -open_payables, "4000": -revenue_total, "5000": cogs_total,
    }
    for account, expected in controls.items():
        require(trial_balance[account] == expected, f"Subledger total differs from GL account {account}")

    return "\n".join([
        f"Period: {start} through {stop - one_day}",
        f"Reconciled {len(documents):,} GL documents and {len(data['inventory_month_end']):,} inventory snapshots.",
        f"Purchase orders: {len(purchases):,}. Production batches: {len(data['production_batches']):,}. "
        f"Sales demand rows: {len(sales):,}.",
        f"Revenue: {currency(revenue_total)}. COGS: {currency(cogs_total)}. Gross profit: {currency(revenue_total - cogs_total)}.",
        f"Production costs: {currency(production_cost)}. Budget: {currency(budget_total)}.",
        f"Closing inventory: {currency(sum(inventory_totals.values()))}. Receivables: {currency(receivables)}. "
        f"Payables: {currency(open_payables)}. Receipts pending invoice: {currency(open_receipts)}.",
    ])


def main():
    parser = argparse.ArgumentParser(description="Reconcile a directory of exported ERP tables.")
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        print(validate(args.directory))
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Validation failed: {exc}\n")


if __name__ == "__main__":
    main()
