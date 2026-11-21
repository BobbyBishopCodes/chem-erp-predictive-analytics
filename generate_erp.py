import argparse
import csv
import math
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from validate_erp import validate

DAY = timedelta(days=1)
OPENING_CASH = 200_000_000

MATERIALS = (
    ("RM-100", "Base binder", "RAW", 192),
    ("RM-200", "Performance additive", "RAW", 365),
    ("RM-300", "Mineral filler", "RAW", 72),
    ("FG-100", "Industrial coating base", "FINISHED", 0),
    ("FG-200", "Protective coating base", "FINISHED", 0),
)
SUPPLIERS = (
    ("SUP-100", "Alder Industrial Supply", 30, 100),
    ("SUP-200", "Brightwell Materials", 45, 103),
    ("SUP-300", "Crescent Specialty Supply", 30, 98),
)
ACCOUNTS = (
    ("1000", "Cash"), ("1100", "Trade receivables"), ("1200", "Raw material inventory"),
    ("1210", "Finished goods inventory"), ("1220", "Work in process"),
    ("2000", "Goods received not invoiced"), ("2100", "Trade payables"),
    ("3000", "Opening equity"), ("4000", "Product revenue"), ("5000", "Cost of goods sold"),
)
CUSTOMERS = (("CUS-100", "Northline Industrial", 30), ("CUS-200", "Harbor Coatings", 45),
             ("CUS-300", "Westfield Components", 30))


@dataclass(frozen=True)
class Product:
    material: str
    center: str
    center_name: str
    daily_kg: int
    sale_cents_per_kg: int
    labor_cents_per_kg: int
    overhead_cents_per_kg: int
    material_shares: tuple[int, int, int]


PRODUCTS = (
    Product("FG-100", "CC-110", "Coatings Line 1", 1_700, 435, 34, 24, (58, 12, 30)),
    Product("FG-200", "CC-120", "Coatings Line 2", 1_230, 575, 42, 29, (50, 28, 22)),
)
RAW_MATERIALS = tuple(row for row in MATERIALS if row[2] == "RAW")

SCHEMA = {
    "company_codes": "company_code company_name currency",
    "plants": "plant_id company_code plant_name",
    "materials": "material_id material_name material_type standard_price_cents_per_kg",
    "suppliers": "supplier_id supplier_name payment_terms_days",
    "customers": "customer_id customer_name payment_terms_days",
    "cost_centers": "cost_center_id plant_id cost_center_name",
    "accounts": "account_id account_name",
    "purchase_orders": "po_id order_date expected_receipt_date plant_id supplier_id material_id quantity_kg unit_price_cents_per_kg amount_cents",
    "supplier_transactions": "transaction_id po_id posting_date transaction_type amount_cents document_id",
    "production_batches": "batch_id posting_date cost_center_id product_id planned_output_kg good_output_kg scrap_kg material_cost_cents labor_cost_cents overhead_cost_cents total_cost_cents",
    "sales_shipments": "shipment_id posting_date plant_id customer_id product_id demand_kg shipped_kg unit_price_cents_per_kg revenue_cents cogs_cents",
    "customer_receipts": "receipt_id shipment_id posting_date amount_cents document_id",
    "material_movements": "movement_id posting_date plant_id material_id movement_type source_id document_id quantity_kg value_cents",
    "inventory_month_end": "month_end plant_id material_id quantity_kg value_cents",
    "cost_center_budgets": "month_start cost_center_id product_id planned_output_kg material_budget_cents labor_budget_cents overhead_budget_cents total_budget_cents",
    "gl_entries": "document_id line_no company_code posting_date source_type source_id account_id cost_center_id material_id debit_cents credit_cents",
}


def calendar_days(start, stop):
    while start < stop:
        yield start
        start += DAY


def seasonal(day):
    return 1 + 0.19 * math.sin(math.tau * (day.timetuple().tm_yday - 70) / 365)


def input_quantities(product, input_kg):
    first, second, _ = product.material_shares
    quantities = [input_kg * first // 100, input_kg * second // 100]
    return (*quantities, input_kg - sum(quantities))


class Simulation:
    def __init__(self, start_year, seed):
        self.start = date(start_year, 1, 1)
        self.stop = date(start_year + 3, 1, 1)
        self.rng = random.Random(seed)
        self.rows = {name: [] for name in SCHEMA}
        self.stock = {material[0]: (0, 0) for material in MATERIALS}
        self.market = {material[0]: 0.0 for material in RAW_MATERIALS}
        self.pending = defaultdict(list)
        self.document_number = 0
        self.company = "US10"
        self.plant = "P100"
        self.rows["company_codes"].append((self.company, "Linden Process Materials", "USD"))
        self.rows["plants"].append((self.plant, self.company, "Linden Manufacturing Plant"))
        self.rows["materials"].extend(MATERIALS)
        self.rows["suppliers"].extend(supplier[:3] for supplier in SUPPLIERS)
        self.rows["customers"].extend(CUSTOMERS)
        self.rows["accounts"].extend(ACCOUNTS)
        self.rows["cost_centers"].extend((p.center, self.plant, p.center_name) for p in PRODUCTS)

    def identifier(self, table, prefix):
        return f"{prefix}-{len(self.rows[table]) + 1:07d}"

    def journal(self, day, source_type, source_id, entries, center=""):
        gl = self.rows["gl_entries"]
        self.document_number += 1
        document_id = f"FI-{self.document_number:07d}"
        for line, (account, material, amount) in enumerate(entries, start=1):
            gl.append((document_id, line, self.company, day, source_type, source_id,
                       account, center, material, max(amount, 0), max(-amount, 0)))
        return document_id

    def movement(self, day, material, movement_type, source, document, quantity, value):
        on_hand, carrying_value = self.stock[material]
        self.stock[material] = on_hand + quantity, carrying_value + value
        movement_id = self.identifier("material_movements", "MM")
        self.rows["material_movements"].append((movement_id, day, self.plant, material, movement_type,
                                                source, document, quantity, value))

    def issue_value(self, material, quantity):
        on_hand, value = self.stock[material]
        return (value * quantity + on_hand // 2) // on_hand

    def purchase(self, day, material, quantity, initial=False):
        material_id, _, _, standard_price = material
        supplier, _, terms, premium = self.rng.choices(SUPPLIERS, weights=(5, 2, 3))[0]
        elapsed = (day - self.start).days
        shock = 0.20 * math.exp(-((elapsed - 530) / 95) ** 2)
        shock *= {"RM-100": 1.0, "RM-200": 1.6, "RM-300": 0.4}[material_id]
        exceptional_cost = self.rng.uniform(0.14, 0.36) if self.rng.random() < 0.045 else 0
        price = round(standard_price * premium / 100 * (
            1 + 0.00007 * elapsed + shock + self.market[material_id] + exceptional_cost
        ))
        po_id = self.identifier("purchase_orders", "PO")
        receipt_day = day if initial else day + self.rng.randint(3, 8) * DAY
        amount = quantity * price
        self.rows["purchase_orders"].append((po_id, day, receipt_day, self.plant, supplier, material_id,
                                             quantity, price, amount))
        receipt = ("101_GOODS_RECEIPT", po_id, material_id, quantity, amount)
        if initial:
            self.post_pending(day, receipt)
        else:
            self.pending[receipt_day].append(receipt)
        invoice_day = receipt_day + self.rng.randint(1, 6) * DAY
        payment_day = invoice_day + (terms + self.rng.randint(-3, 8)) * DAY
        self.pending[invoice_day].append(("SUPPLIER_INVOICE", po_id, material_id, quantity, amount))
        self.pending[payment_day].append(("SUPPLIER_PAYMENT", po_id, material_id, quantity, amount))

    def post_pending(self, day, event):
        kind, source, material, quantity, amount = event
        if kind == "CUSTOMER_PAYMENT":
            receipt_id = self.identifier("customer_receipts", "CR")
            document = self.journal(day, kind, receipt_id, [("1000", "", amount), ("1100", "", -amount)])
            self.rows["customer_receipts"].append((receipt_id, source, day, amount, document))
            return

        transaction_id = self.identifier("supplier_transactions", "ST")
        entries = {
            "101_GOODS_RECEIPT": [("1200", material, amount), ("2000", material, -amount)],
            "SUPPLIER_INVOICE": [("2000", material, amount), ("2100", "", -amount)],
            "SUPPLIER_PAYMENT": [("2100", "", amount), ("1000", "", -amount)],
        }
        document = self.journal(day, kind, transaction_id, entries[kind])
        self.rows["supplier_transactions"].append((transaction_id, source, day, kind, amount, document))
        if kind == "101_GOODS_RECEIPT":
            self.movement(day, material, kind, source, document, quantity, amount)

    def produce(self, day, product):
        plan = round(product.daily_kg * seasonal(day))
        input_kg = round(plan * 1.04 * self.rng.uniform(0.91, 1.09))
        quantities = input_quantities(product, input_kg)
        if any(self.stock[row[0]][0] < qty for row, qty in zip(RAW_MATERIALS, quantities)):
            return
        scrap_fraction = self.rng.uniform(0.018, 0.057)
        disrupted = self.rng.random() < (0.12 if day.month in (7, 8) else 0.04)
        if disrupted:
            scrap_fraction += self.rng.uniform(0.055, 0.16)
        scrap = round(input_kg * scrap_fraction)
        good = input_kg - scrap
        inflation = 1 + 0.033 * (day.year - self.start.year)
        labor = round(input_kg * product.labor_cents_per_kg * inflation *
                      self.rng.uniform(0.92, 1.11) * (1.32 if disrupted else 1))
        energy = 1.12 if day.month in (1, 2, 7, 8, 12) else 1
        overhead = round((26_000 + input_kg * product.overhead_cents_per_kg) * inflation *
                         energy * self.rng.uniform(0.92, 1.09))
        batch = self.identifier("production_batches", "PB")
        issues = [
            (row[0], quantity, self.issue_value(row[0], quantity))
            for row, quantity in zip(RAW_MATERIALS, quantities)
        ]
        material_cost = sum(value for _, _, value in issues)
        total = material_cost + labor + overhead
        entries = [("1200", material, -value) for material, _, value in issues]
        entries.extend([("1220", product.material, material_cost), ("1220", product.material, labor),
                        ("1220", product.material, overhead), ("1000", "", -labor),
                        ("1000", "", -overhead), ("1210", product.material, total),
                        ("1220", product.material, -total)])
        document = self.journal(day, "PRODUCTION", batch, entries, product.center)
        for material, quantity, value in issues:
            self.movement(day, material, "261_BATCH_ISSUE", batch, document, -quantity, -value)
        self.movement(day, product.material, "101_BATCH_RECEIPT", batch, document, good, total)
        self.rows["production_batches"].append((batch, day, product.center, product.material, plan,
                                                good, scrap, material_cost, labor, overhead, total))

    def sell(self, day, product):
        demand = round(product.daily_kg * seasonal(day) * self.rng.uniform(0.76, 1.26))
        available = self.stock[product.material][0]
        shipped = min(available, demand)
        customer, _, terms = self.rng.choice(CUSTOMERS)
        price = round(product.sale_cents_per_kg * (1 + 0.027 * (day.year - self.start.year)) *
                      self.rng.uniform(0.94, 1.06))
        revenue = shipped * price
        shipment = self.identifier("sales_shipments", "SD")
        cogs = self.issue_value(product.material, shipped) if shipped else 0
        self.rows["sales_shipments"].append((shipment, day, self.plant, customer, product.material, demand,
                                              shipped, price, revenue, cogs))
        if shipped:
            entries = [("1100", "", revenue), ("4000", product.material, -revenue),
                       ("5000", product.material, cogs), ("1210", product.material, -cogs)]
            document = self.journal(day, "SALE", shipment, entries)
            self.movement(day, product.material, "601_CUSTOMER_ISSUE", shipment, document, -shipped, -cogs)
            paid_on = day + (terms + self.rng.randint(-2, 8)) * DAY
            self.pending[paid_on].append(("CUSTOMER_PAYMENT", shipment, "", 0, revenue))

    def budget(self, month):
        working_days = [day for day in calendar_days(month, month + 31 * DAY)
                        if day.month == month.month and day.weekday() < 5]
        inflation = 1 + 0.028 * (month.year - self.start.year)
        for product in PRODUCTS:
            planned = sum(round(product.daily_kg * seasonal(day)) for day in working_days)
            gross_kg = round(planned * 1.04)
            materials = round(inflation * sum(
                row[3] * quantity for row, quantity in zip(RAW_MATERIALS, input_quantities(product, gross_kg))
            ))
            labor = round(inflation * gross_kg * product.labor_cents_per_kg)
            overhead = round(inflation * (gross_kg * product.overhead_cents_per_kg + 26_000 * len(working_days)))
            self.rows["cost_center_budgets"].append((month, product.center, product.material, planned,
                                                     materials, labor, overhead, materials + labor + overhead))

    def run(self):
        self.journal(self.start, "OPENING", "OPENING", [("1000", "", OPENING_CASH),
                                                       ("3000", "", -OPENING_CASH)])
        for material, quantity in zip(RAW_MATERIALS, (24_000, 10_000, 12_000)):
            self.purchase(self.start, material, quantity, initial=True)
        for day in calendar_days(self.start, self.stop):
            for event in self.pending.pop(day, ()):
                self.post_pending(day, event)
            if day.day == 1:
                self.budget(day)
            for material in RAW_MATERIALS:
                code = material[0]
                self.market[code] = 0.91 * self.market[code] + self.rng.gauss(0, 0.013)
            if day.weekday() < 5:
                for product in PRODUCTS:
                    self.produce(day, product)
                    self.sell(day, product)
            if day.weekday() == 0 and day != self.start:
                for index, material in enumerate(RAW_MATERIALS):
                    expected_daily = sum(input_quantities(p, round(p.daily_kg * seasonal(day) * 1.04))[index]
                                         for p in PRODUCTS)
                    inbound = sum(event[3] for events in self.pending.values() for event in events
                                  if event[0] == "101_GOODS_RECEIPT" and event[2] == material[0])
                    quantity = expected_daily * 15 - self.stock[material[0]][0] - inbound
                    if quantity >= expected_daily * 3:
                        self.purchase(day, material, round(quantity / 100) * 100)
            if (day + DAY).month != day.month:
                for material, (quantity, value) in self.stock.items():
                    self.rows["inventory_month_end"].append((day, self.plant, material, quantity, value))

    def export(self, directory):
        for table, columns in SCHEMA.items():
            with (directory / f"{table}.csv").open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream, lineterminator="\n")
                writer.writerow(columns.split())
                writer.writerows(self.rows[table])


def main():
    parser = argparse.ArgumentParser(description="Generate and reconcile three years of fictional ERP exports.")
    parser.add_argument("--output", type=Path, default=Path("data/generated"))
    parser.add_argument("--start-year", type=int, default=2023)
    parser.add_argument("--seed", type=int, default=413)
    args = parser.parse_args()
    if not 1 <= args.start_year <= 9996:
        parser.error("--start-year must be between 1 and 9996")
    if args.output.exists():
        parser.error(f"output already exists: {args.output}")
    simulation = Simulation(args.start_year, args.seed)
    simulation.run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=args.output.parent) as temporary:
        staged = Path(temporary) / "export"
        staged.mkdir()
        simulation.export(staged)
        summary = validate(staged)
        staged.rename(args.output)
    print(f"Exported {len(SCHEMA)} tables to {args.output.resolve()}")
    print(summary)


if __name__ == "__main__":
    main()
