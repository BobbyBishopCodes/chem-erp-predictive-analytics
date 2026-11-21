CREATE SCHEMA IF NOT EXISTS erp;
CREATE SCHEMA IF NOT EXISTS analytics;
CREATE SCHEMA IF NOT EXISTS pipeline;

CREATE TABLE IF NOT EXISTS pipeline.loads (
    load_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    loaded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    input_sha256 text NOT NULL,
    erp_sha256 text NOT NULL,
    forecast_as_of date NOT NULL,
    risk_as_of date NOT NULL,
    row_counts jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS pipeline.reconciliations (
    load_id bigint NOT NULL REFERENCES pipeline.loads,
    control text NOT NULL,
    difference numeric NOT NULL CHECK (difference = 0),
    PRIMARY KEY (load_id, control)
);

CREATE TABLE IF NOT EXISTS erp.company_codes (
    company_code text PRIMARY KEY, company_name text NOT NULL, currency text NOT NULL
);
CREATE TABLE IF NOT EXISTS erp.plants (
    plant_id text PRIMARY KEY, company_code text NOT NULL REFERENCES erp.company_codes, plant_name text NOT NULL
);
CREATE TABLE IF NOT EXISTS erp.materials (
    material_id text PRIMARY KEY, material_name text NOT NULL,
    material_type text NOT NULL CHECK (material_type IN ('RAW', 'FINISHED')),
    standard_price_cents_per_kg bigint NOT NULL CHECK (standard_price_cents_per_kg >= 0)
);
CREATE TABLE IF NOT EXISTS erp.suppliers (
    supplier_id text PRIMARY KEY, supplier_name text NOT NULL, payment_terms_days integer NOT NULL
);
CREATE TABLE IF NOT EXISTS erp.customers (
    customer_id text PRIMARY KEY, customer_name text NOT NULL, payment_terms_days integer NOT NULL
);
CREATE TABLE IF NOT EXISTS erp.cost_centers (
    cost_center_id text PRIMARY KEY, plant_id text NOT NULL REFERENCES erp.plants, cost_center_name text NOT NULL
);
CREATE TABLE IF NOT EXISTS erp.accounts (
    account_id text PRIMARY KEY, account_name text NOT NULL
);
CREATE TABLE IF NOT EXISTS erp.gl_documents (
    document_id text PRIMARY KEY, company_code text NOT NULL REFERENCES erp.company_codes,
    posting_date date NOT NULL, source_type text NOT NULL, source_id text NOT NULL,
    UNIQUE (source_type, source_id),
    UNIQUE (document_id, company_code, posting_date, source_type, source_id)
);
CREATE TABLE IF NOT EXISTS erp.gl_entries (
    document_id text NOT NULL, line_no integer NOT NULL, company_code text NOT NULL,
    posting_date date NOT NULL, source_type text NOT NULL, source_id text NOT NULL,
    account_id text NOT NULL REFERENCES erp.accounts, cost_center_id text REFERENCES erp.cost_centers,
    material_id text REFERENCES erp.materials,
    debit_cents bigint NOT NULL, credit_cents bigint NOT NULL,
    PRIMARY KEY (document_id, line_no),
    FOREIGN KEY (document_id, company_code, posting_date, source_type, source_id)
        REFERENCES erp.gl_documents (document_id, company_code, posting_date, source_type, source_id),
    CHECK ((debit_cents > 0 AND credit_cents = 0) OR (credit_cents > 0 AND debit_cents = 0))
);
CREATE TABLE IF NOT EXISTS erp.purchase_orders (
    po_id text PRIMARY KEY, order_date date NOT NULL, expected_receipt_date date NOT NULL,
    plant_id text NOT NULL REFERENCES erp.plants, supplier_id text NOT NULL REFERENCES erp.suppliers,
    material_id text NOT NULL REFERENCES erp.materials,
    quantity_kg bigint NOT NULL CHECK (quantity_kg > 0),
    unit_price_cents_per_kg bigint NOT NULL CHECK (unit_price_cents_per_kg > 0),
    amount_cents bigint NOT NULL CHECK (amount_cents = quantity_kg * unit_price_cents_per_kg),
    CHECK (expected_receipt_date >= order_date)
);
CREATE TABLE IF NOT EXISTS erp.supplier_transactions (
    transaction_id text PRIMARY KEY, po_id text NOT NULL REFERENCES erp.purchase_orders,
    posting_date date NOT NULL, transaction_type text NOT NULL CHECK (
        transaction_type IN ('101_GOODS_RECEIPT', 'SUPPLIER_INVOICE', 'SUPPLIER_PAYMENT')
    ), amount_cents bigint NOT NULL CHECK (amount_cents > 0),
    document_id text NOT NULL UNIQUE REFERENCES erp.gl_documents,
    UNIQUE (po_id, transaction_type)
);
CREATE TABLE IF NOT EXISTS erp.production_batches (
    batch_id text PRIMARY KEY, posting_date date NOT NULL, cost_center_id text NOT NULL REFERENCES erp.cost_centers,
    product_id text NOT NULL REFERENCES erp.materials, planned_output_kg bigint NOT NULL,
    good_output_kg bigint NOT NULL, scrap_kg bigint NOT NULL, material_cost_cents bigint NOT NULL,
    labor_cost_cents bigint NOT NULL, overhead_cost_cents bigint NOT NULL, total_cost_cents bigint NOT NULL,
    CHECK (total_cost_cents = material_cost_cents + labor_cost_cents + overhead_cost_cents)
);
CREATE TABLE IF NOT EXISTS erp.sales_shipments (
    shipment_id text PRIMARY KEY, posting_date date NOT NULL, plant_id text NOT NULL REFERENCES erp.plants,
    customer_id text NOT NULL REFERENCES erp.customers, product_id text NOT NULL REFERENCES erp.materials,
    demand_kg bigint NOT NULL, shipped_kg bigint NOT NULL, unit_price_cents_per_kg bigint NOT NULL,
    revenue_cents bigint NOT NULL CHECK (revenue_cents = shipped_kg * unit_price_cents_per_kg),
    cogs_cents bigint NOT NULL, CHECK (shipped_kg BETWEEN 0 AND demand_kg)
);
CREATE TABLE IF NOT EXISTS erp.customer_receipts (
    receipt_id text PRIMARY KEY, shipment_id text NOT NULL UNIQUE REFERENCES erp.sales_shipments,
    posting_date date NOT NULL, amount_cents bigint NOT NULL CHECK (amount_cents > 0),
    document_id text NOT NULL UNIQUE REFERENCES erp.gl_documents
);
CREATE TABLE IF NOT EXISTS erp.material_movements (
    movement_id text PRIMARY KEY, posting_date date NOT NULL, plant_id text NOT NULL REFERENCES erp.plants,
    material_id text NOT NULL REFERENCES erp.materials, movement_type text NOT NULL, source_id text NOT NULL,
    document_id text NOT NULL REFERENCES erp.gl_documents, quantity_kg bigint NOT NULL, value_cents bigint NOT NULL
);
CREATE TABLE IF NOT EXISTS erp.inventory_month_end (
    month_end date NOT NULL, plant_id text NOT NULL REFERENCES erp.plants,
    material_id text NOT NULL REFERENCES erp.materials, quantity_kg bigint NOT NULL CHECK (quantity_kg >= 0),
    value_cents bigint NOT NULL CHECK (value_cents >= 0), PRIMARY KEY (month_end, plant_id, material_id)
);
CREATE TABLE IF NOT EXISTS erp.cost_center_budgets (
    month_start date NOT NULL, cost_center_id text NOT NULL REFERENCES erp.cost_centers,
    product_id text NOT NULL REFERENCES erp.materials, planned_output_kg bigint NOT NULL,
    material_budget_cents bigint NOT NULL, labor_budget_cents bigint NOT NULL, overhead_budget_cents bigint NOT NULL,
    total_budget_cents bigint NOT NULL CHECK (
        total_budget_cents = material_budget_cents + labor_budget_cents + overhead_budget_cents
    ), PRIMARY KEY (month_start, cost_center_id, product_id)
);

CREATE TABLE IF NOT EXISTS analytics.forecasts (
    metric text NOT NULL, material_id text REFERENCES erp.materials, cost_center_id text REFERENCES erp.cost_centers,
    product_id text REFERENCES erp.materials, unit text NOT NULL, horizon_days smallint NOT NULL,
    as_of_date date NOT NULL, window_start date NOT NULL, window_end date NOT NULL, forecast_value numeric NOT NULL,
    algorithm text NOT NULL, model_file text NOT NULL, training_rows integer NOT NULL,
    last_training_window_end date NOT NULL,
    UNIQUE NULLS NOT DISTINCT (metric, material_id, cost_center_id, product_id, horizon_days, as_of_date),
    CHECK (horizon_days IN (30, 60, 90) AND window_start = as_of_date + 1
        AND window_end = as_of_date + horizon_days AND last_training_window_end <= as_of_date)
);
CREATE TABLE IF NOT EXISTS analytics.backtest_predictions (
    metric text NOT NULL, material_id text REFERENCES erp.materials, cost_center_id text REFERENCES erp.cost_centers,
    product_id text REFERENCES erp.materials, unit text NOT NULL, horizon_days smallint NOT NULL,
    as_of_date date NOT NULL, window_start date NOT NULL, window_end date NOT NULL, training_rows integer NOT NULL,
    last_training_window_end date NOT NULL, actual_value numeric NOT NULL, xgboost numeric NOT NULL,
    trailing_window numeric NOT NULL, prior_year_window numeric NOT NULL,
    UNIQUE NULLS NOT DISTINCT (metric, material_id, cost_center_id, product_id, horizon_days, as_of_date),
    CHECK (horizon_days IN (30, 60, 90) AND window_start = as_of_date + 1
        AND window_end = as_of_date + horizon_days AND last_training_window_end <= as_of_date)
);
CREATE TABLE IF NOT EXISTS analytics.model_metrics (
    metric text NOT NULL, material_id text REFERENCES erp.materials, cost_center_id text REFERENCES erp.cost_centers,
    product_id text REFERENCES erp.materials, unit text NOT NULL, horizon_days smallint NOT NULL,
    algorithm text NOT NULL CHECK (algorithm IN ('xgboost', 'trailing_window', 'prior_year_window')),
    backtest_windows integer NOT NULL, first_as_of_date date NOT NULL, last_as_of_date date NOT NULL,
    mae numeric NOT NULL, rmse numeric NOT NULL, wape_pct numeric, mean_error numeric NOT NULL,
    UNIQUE NULLS NOT DISTINCT (metric, material_id, cost_center_id, product_id, horizon_days, algorithm)
);
CREATE TABLE IF NOT EXISTS analytics.purchase_anomalies (
    po_id text PRIMARY KEY REFERENCES erp.purchase_orders, order_date date NOT NULL, plant_id text NOT NULL REFERENCES erp.plants,
    supplier_id text NOT NULL REFERENCES erp.suppliers, material_id text NOT NULL REFERENCES erp.materials,
    quantity_kg bigint NOT NULL, unit_price_cents_per_kg bigint NOT NULL, reference_orders integer NOT NULL,
    median_cents_per_kg numeric, premium_pct numeric, robust_z numeric,
    estimated_premium_cents numeric CHECK (estimated_premium_cents = trunc(estimated_premium_cents)),
    flagged boolean NOT NULL, status text NOT NULL
);
CREATE TABLE IF NOT EXISTS analytics.production_variances (
    batch_id text PRIMARY KEY REFERENCES erp.production_batches, posting_date date NOT NULL,
    cost_center_id text NOT NULL REFERENCES erp.cost_centers, product_id text NOT NULL REFERENCES erp.materials,
    good_output_kg bigint NOT NULL, total_cost_cents bigint NOT NULL, material_standard_cents bigint NOT NULL,
    labor_standard_cents bigint NOT NULL, overhead_standard_cents bigint NOT NULL, material_variance_cents bigint NOT NULL,
    labor_variance_cents bigint NOT NULL, overhead_variance_cents bigint NOT NULL, flexed_standard_cents bigint NOT NULL,
    unfavorable_variance_cents bigint NOT NULL, unfavorable_variance_pct numeric NOT NULL,
    largest_unfavorable_component text, flagged boolean NOT NULL
);
CREATE TABLE IF NOT EXISTS analytics.batch_efficiency (
    batch_id text PRIMARY KEY REFERENCES erp.production_batches, posting_date date NOT NULL,
    cost_center_id text NOT NULL REFERENCES erp.cost_centers, product_id text NOT NULL REFERENCES erp.materials,
    scrap_rate numeric NOT NULL, labor_cents_per_input_kg numeric NOT NULL, overhead_cents_per_input_kg numeric NOT NULL,
    training_batches integer NOT NULL, training_through date, anomaly_score numeric, score_threshold numeric,
    elevated_metrics text, flagged boolean NOT NULL, status text NOT NULL,
    CHECK (training_through IS NULL OR training_through < posting_date)
);
CREATE TABLE IF NOT EXISTS analytics.budget_risk (
    cost_center_id text NOT NULL REFERENCES erp.cost_centers, month_start date NOT NULL, month_end date NOT NULL,
    as_of_date date NOT NULL, schedule text NOT NULL, business_days_elapsed integer NOT NULL,
    business_days_remaining integer NOT NULL, budget_cents bigint NOT NULL, month_to_date_cost_cents bigint NOT NULL,
    projected_cost_cents bigint NOT NULL, blended_history_cost_cents bigint NOT NULL, projected_variance_cents bigint NOT NULL,
    projected_variance_pct numeric NOT NULL, risk_status text NOT NULL, projected_to_overrun boolean NOT NULL,
    final_cost_cents bigint, actual_overrun boolean, PRIMARY KEY (cost_center_id, month_start, as_of_date),
    CHECK (month_start <= as_of_date AND as_of_date < month_end),
    CHECK (projected_variance_cents = projected_cost_cents - budget_cents)
);
CREATE TABLE IF NOT EXISTS analytics.budget_metrics (
    schedule text NOT NULL, method text NOT NULL, evaluated_cost_center_months integer NOT NULL,
    mae_cents numeric NOT NULL, true_positives integer NOT NULL, false_positives integer NOT NULL,
    false_negatives integer NOT NULL, true_negatives integer NOT NULL, precision numeric, recall numeric,
    PRIMARY KEY (schedule, method),
    CHECK (evaluated_cost_center_months = true_positives + false_positives + false_negatives + true_negatives)
);

CREATE INDEX IF NOT EXISTS gl_entries_account_date ON erp.gl_entries (account_id, posting_date);
CREATE INDEX IF NOT EXISTS production_center_product_date ON erp.production_batches (cost_center_id, product_id, posting_date);
CREATE INDEX IF NOT EXISTS sales_product_date ON erp.sales_shipments (product_id, posting_date);
CREATE INDEX IF NOT EXISTS movements_material_plant_date ON erp.material_movements (material_id, plant_id, posting_date);

CREATE OR REPLACE VIEW analytics.monthly_costs AS
WITH actual AS (
    SELECT date_trunc('month', posting_date)::date AS month_start, cost_center_id, product_id,
           sum(good_output_kg) AS good_output_kg, sum(scrap_kg) AS scrap_kg, sum(total_cost_cents) AS actual_cost_cents
    FROM erp.production_batches GROUP BY 1, 2, 3
)
SELECT b.month_start, b.cost_center_id, b.product_id, b.planned_output_kg,
       coalesce(a.good_output_kg, 0) AS good_output_kg, coalesce(a.scrap_kg, 0) AS scrap_kg,
       b.total_budget_cents, coalesce(a.actual_cost_cents, 0) AS actual_cost_cents,
       coalesce(a.actual_cost_cents, 0) - b.total_budget_cents AS variance_cents,
       a.actual_cost_cents / nullif(a.good_output_kg, 0) AS cost_cents_per_kg,
       a.good_output_kg / nullif(a.good_output_kg + a.scrap_kg, 0) AS production_yield
FROM erp.cost_center_budgets b LEFT JOIN actual a USING (month_start, cost_center_id, product_id);

CREATE OR REPLACE VIEW analytics.monthly_product_margins AS
SELECT date_trunc('month', posting_date)::date AS month_start, product_id, sum(shipped_kg) AS shipped_kg,
       sum(revenue_cents) AS revenue_cents, sum(cogs_cents) AS cogs_cents,
       sum(revenue_cents - cogs_cents) AS gross_profit_cents,
       sum(revenue_cents - cogs_cents) / nullif(sum(revenue_cents), 0) AS gross_margin
FROM erp.sales_shipments GROUP BY 1, 2;

CREATE OR REPLACE VIEW pipeline.control_differences AS
WITH gl AS (
    SELECT account_id, sum(debit_cents - credit_cents) AS balance FROM erp.gl_entries GROUP BY account_id
), stock AS (
    SELECT m.material_type, sum(i.value_cents) AS value FROM erp.inventory_month_end i
    JOIN erp.materials m USING (material_id)
    WHERE i.month_end = (SELECT max(month_end) FROM erp.inventory_month_end)
    GROUP BY m.material_type
), supplier AS (
    SELECT
        coalesce(sum(amount_cents) FILTER (WHERE transaction_type = '101_GOODS_RECEIPT'), 0) AS received,
        coalesce(sum(amount_cents) FILTER (WHERE transaction_type = 'SUPPLIER_INVOICE'), 0) AS invoiced,
        coalesce(sum(amount_cents) FILTER (WHERE transaction_type = 'SUPPLIER_PAYMENT'), 0) AS paid
    FROM erp.supplier_transactions
), sales AS (
    SELECT sum(revenue_cents) AS revenue, sum(cogs_cents) AS cogs FROM erp.sales_shipments
), expected_accounts (control, account, amount) AS (
    SELECT 'trade receivables', '1100', sales.revenue - (SELECT coalesce(sum(amount_cents), 0) FROM erp.customer_receipts) FROM sales
    UNION ALL SELECT 'raw inventory', '1200', coalesce((SELECT value FROM stock WHERE material_type = 'RAW'), 0)
    UNION ALL SELECT 'finished inventory', '1210', coalesce((SELECT value FROM stock WHERE material_type = 'FINISHED'), 0)
    UNION ALL SELECT 'work in process', '1220', 0
    UNION ALL SELECT 'received not invoiced', '2000', supplier.invoiced - supplier.received FROM supplier
    UNION ALL SELECT 'trade payables', '2100', supplier.paid - supplier.invoiced FROM supplier
    UNION ALL SELECT 'revenue', '4000', -sales.revenue FROM sales
    UNION ALL SELECT 'cost of goods sold', '5000', sales.cogs FROM sales
), inventory_check AS (
    SELECT i.month_end, i.plant_id, i.material_id
    FROM erp.inventory_month_end i LEFT JOIN erp.material_movements m
      ON i.plant_id = m.plant_id AND i.material_id = m.material_id AND m.posting_date <= i.month_end
    GROUP BY i.month_end, i.plant_id, i.material_id, i.quantity_kg, i.value_cents
    HAVING i.quantity_kg <> coalesce(sum(m.quantity_kg), 0) OR i.value_cents <> coalesce(sum(m.value_cents), 0)
), evaluated_metrics AS (
    SELECT b.metric, coalesce(b.material_id, '') AS material_key, coalesce(b.cost_center_id, '') AS center_key,
           coalesce(b.product_id, '') AS product_key, b.unit, b.horizon_days, p.algorithm,
           count(*) AS windows, min(b.as_of_date) AS first_date, max(b.as_of_date) AS last_date,
           round(avg(abs(p.prediction - b.actual_value)), 6) AS mae,
           round(sqrt(avg((p.prediction - b.actual_value)^2)), 6) AS rmse,
           round(100 * sum(abs(p.prediction - b.actual_value)) / nullif(sum(abs(b.actual_value)), 0), 6) AS wape,
           round(avg(p.prediction - b.actual_value), 6) AS mean_error
    FROM analytics.backtest_predictions b
    CROSS JOIN LATERAL (VALUES ('xgboost', b.xgboost), ('trailing_window', b.trailing_window),
                               ('prior_year_window', b.prior_year_window)) p(algorithm, prediction)
    GROUP BY 1, 2, 3, 4, 5, 6, 7
), imported_metrics AS (
    SELECT *, coalesce(material_id, '') AS material_key, coalesce(cost_center_id, '') AS center_key,
           coalesce(product_id, '') AS product_key
    FROM analytics.model_metrics
), budget_checks AS (
    SELECT r.*, (SELECT coalesce(sum(b.total_budget_cents), 0) FROM erp.cost_center_budgets b
                 WHERE b.month_start = r.month_start AND b.cost_center_id = r.cost_center_id) AS expected_budget,
           (SELECT coalesce(sum(p.total_cost_cents), 0) FROM erp.production_batches p
            WHERE p.cost_center_id = r.cost_center_id AND p.posting_date BETWEEN r.month_start AND r.as_of_date) AS expected_mtd,
           (SELECT coalesce(sum(p.total_cost_cents), 0) FROM erp.production_batches p
            WHERE p.cost_center_id = r.cost_center_id AND p.posting_date BETWEEN r.month_start AND r.month_end) AS expected_final
    FROM analytics.budget_risk r
)
SELECT e.control, coalesce(g.balance, 0) - e.amount AS difference
FROM expected_accounts e LEFT JOIN gl g ON g.account_id = e.account
UNION ALL SELECT 'unbalanced documents', count(*) FROM (
    SELECT document_id FROM erp.gl_entries GROUP BY document_id HAVING sum(debit_cents - credit_cents) <> 0
) docs
UNION ALL SELECT 'inventory month-end roll-forward', count(*) FROM inventory_check
UNION ALL SELECT 'monthly production total', (SELECT sum(actual_cost_cents) FROM analytics.monthly_costs)
    - (SELECT sum(total_cost_cents) FROM erp.production_batches)
UNION ALL SELECT 'monthly budget total', (SELECT sum(total_budget_cents) FROM analytics.monthly_costs)
    - (SELECT sum(total_budget_cents) FROM erp.cost_center_budgets)
UNION ALL SELECT 'model metrics match backtests', count(*)
FROM evaluated_metrics e FULL JOIN imported_metrics m USING (
    metric, material_key, center_key, product_key, unit, horizon_days, algorithm
)
WHERE e.windows IS DISTINCT FROM m.backtest_windows OR e.first_date IS DISTINCT FROM m.first_as_of_date
    OR e.last_date IS DISTINCT FROM m.last_as_of_date OR abs(e.mae - m.mae) > 0.000001
    OR abs(e.rmse - m.rmse) > 0.000001 OR abs(e.wape - m.wape_pct) > 0.000001
    OR (e.wape IS NULL) <> (m.wape_pct IS NULL) OR abs(e.mean_error - m.mean_error) > 0.000001
UNION ALL SELECT 'purchase risk matches ERP', count(*) FROM analytics.purchase_anomalies a JOIN erp.purchase_orders p USING (po_id)
WHERE (a.order_date, a.plant_id, a.supplier_id, a.material_id, a.quantity_kg, a.unit_price_cents_per_kg)
    IS DISTINCT FROM (p.order_date, p.plant_id, p.supplier_id, p.material_id, p.quantity_kg, p.unit_price_cents_per_kg)
UNION ALL SELECT 'production variance matches ERP', count(*)
FROM analytics.production_variances v JOIN erp.production_batches b USING (batch_id)
WHERE (v.posting_date, v.cost_center_id, v.product_id, v.good_output_kg, v.total_cost_cents)
    IS DISTINCT FROM (b.posting_date, b.cost_center_id, b.product_id, b.good_output_kg, b.total_cost_cents)
    OR v.unfavorable_variance_cents <> b.total_cost_cents - v.flexed_standard_cents
    OR v.unfavorable_variance_cents <> v.material_variance_cents + v.labor_variance_cents + v.overhead_variance_cents
UNION ALL SELECT 'batch efficiency matches ERP', count(*)
FROM analytics.batch_efficiency e JOIN erp.production_batches b USING (batch_id)
WHERE (e.posting_date, e.cost_center_id, e.product_id) IS DISTINCT FROM (b.posting_date, b.cost_center_id, b.product_id)
    OR abs(e.scrap_rate - b.scrap_kg::numeric / (b.good_output_kg + b.scrap_kg)) > 0.000001
UNION ALL SELECT 'budget risk matches ERP', count(*) FROM budget_checks
WHERE budget_cents <> expected_budget OR month_to_date_cost_cents <> expected_mtd
    OR projected_to_overrun <> (projected_cost_cents > budget_cents)
    OR (final_cost_cents IS NOT NULL AND (final_cost_cents <> expected_final
        OR actual_overrun IS DISTINCT FROM (expected_final > expected_budget)));
