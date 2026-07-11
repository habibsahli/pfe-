-- =============================================================================
-- SEED 04 — mart.fact_stock  (FIBRE product stock data, all 24 governorates)
-- Generates monthly stock snapshots for FIBRE subscription products and CPE.
-- Period : 2023-01-01 → 2026-05-01  (monthly, first of each month)
-- Unique constraint: (date_id, product_id, warehouse_code)
--
-- Stock model:
--   - Opening stock based on expected monthly demand × safety factor
--   - Seasonal adjustments: pre-Ramadan stock-up, post-summer depletion
--   - Regional sizing: Grand Tunis higher, interior lower
--   - Rupture flags set when stock falls below threshold
-- =============================================================================

INSERT INTO mart.fact_stock
  (date_id, product_id, geo_id, warehouse_code,
   stock_quantity, stock_min_threshold, stock_max_capacity,
   current_stock_qty, available_qty, reserved_qty,
   sales_qty, avg_monthly_sales, days_of_supply,
   is_rupture, is_low_stock, understock_risk, overstock_risk,
   has_zero_stock, has_negative_stock,
   snapshot_date, data_source, record_type, product_type,
   etl_batch_id)

WITH

-- ── Month spine (first of each month, 2023-01 → 2026-05) ──────────────────
months AS (
  SELECT date_id, date AS snap_date, annee, mois
  FROM mart.dim_temps
  WHERE date >= '2023-01-01' AND date <= '2026-05-01' AND jour = 1
),

-- ── FIBRE products to stock (physical CPE + key subscription SKUs) ─────────
-- Only products where stock tracking makes sense operationally
products AS (
  SELECT * FROM (VALUES
  --  product_id         base_stock  type
    ('FBR001',           120,        'SUBSCRIPTION'),
    ('FBR002',           100,        'SUBSCRIPTION'),
    ('FBR003',            60,        'SUBSCRIPTION'),
    ('FBR004',           150,        'SUBSCRIPTION'),
    ('FBR005',           130,        'SUBSCRIPTION'),
    ('FBR006',           110,        'SUBSCRIPTION'),
    ('FBR007',            80,        'SUBSCRIPTION'),
    ('FBR008',            70,        'SUBSCRIPTION'),
    ('FBR009',            50,        'SUBSCRIPTION')
  ) AS t(product_id, base_stock_tunis, type_prod)
),

-- ── Warehouse definitions (one per governorate) ────────────────────────────
warehouses AS (
  SELECT * FROM (VALUES
    ('Tunis',       2,    1.00),
    ('Ariana',      7,    0.75),
    ('Ben Arous',   1,    0.55),
    ('Mannouba',    26,   0.22),
    ('Sfax',        5561, 0.42),
    ('Sousse',      5564, 0.37),
    ('Bizerte',     5549, 0.26),
    ('Nabeul',      5560, 0.22),
    ('Monastir',    5559, 0.22),
    ('Mahdia',      5557, 0.10),
    ('Medenine',    5558, 0.10),
    ('Kairouan',    5553, 0.08),
    ('Kasserine',   5554, 0.06),
    ('Jendouba',    5552, 0.06),
    ('Kef',         5556, 0.05),
    ('Siliana',     5563, 0.05),
    ('Gafsa',       5551, 0.07),
    ('Beja',        5548, 0.06),
    ('Gabes',       5550, 0.08),
    ('Sidi Bouzid', 5562, 0.05),
    ('Tataouine',   5565, 0.04),
    ('Kebili',      5555, 0.04),
    ('Tozeur',      5566, 0.04),
    ('Zaghouan',    5567, 0.05)
  ) AS t(warehouse_code, geo_id, size_factor)
),

-- ── Cross and compute base stock per cell ─────────────────────────────────
combos AS (
  SELECT
    m.date_id,
    m.snap_date,
    m.annee,
    m.mois,
    p.product_id,
    p.base_stock_tunis,
    p.type_prod,
    w.warehouse_code,
    w.geo_id,
    w.size_factor,
    -- seasonal stock multiplier (pre-event stock-up)
    CASE
      -- Pre-Ramadan stock-up (month before Ramadan starts)
      WHEN (m.annee=2023 AND m.mois=3)
        OR (m.annee=2024 AND m.mois=2)
        OR (m.annee=2025 AND m.mois=2)
        OR (m.annee=2026 AND m.mois=2)                    THEN 1.40
      -- Ramadan month (high sales, stock drawn down)
      WHEN (m.annee=2023 AND m.mois=4)
        OR (m.annee=2024 AND m.mois=3)
        OR (m.annee=2025 AND m.mois=3)
        OR (m.annee=2026 AND m.mois=3)                    THEN 0.80
      -- Pre-rentrée stock-up (August)
      WHEN m.mois = 8                                      THEN 1.30
      -- Rentrée month (September, high sales)
      WHEN m.mois = 9                                      THEN 0.85
      -- Summer: stock adequate, no urgency
      WHEN m.mois IN (6,7)                                 THEN 1.10
      -- Q4: end of year replenishment
      WHEN m.mois = 12                                     THEN 1.20
      -- January: post-holiday excess
      WHEN m.mois = 1                                      THEN 1.15
      ELSE 1.00
    END AS seasonal_factor,
    -- YoY stock growth (FIBRE market expands ~8% per year)
    POWER(1.08, m.annee - 2023) AS growth_factor
  FROM months m
  CROSS JOIN products p
  CROSS JOIN warehouses w
),

-- ── Compute stock quantities ──────────────────────────────────────────────
computed AS (
  SELECT
    *,
    -- raw stock (rounded to nearest 5 for realism)
    GREATEST(2, ROUND(
      base_stock_tunis * size_factor * seasonal_factor * growth_factor / 5.0
    )::int * 5) AS stock_qty
  FROM combos
),

final AS (
  SELECT
    c.date_id,
    c.product_id,
    c.geo_id,
    c.warehouse_code,
    c.stock_qty                                             AS stock_quantity,
    -- min threshold = 15% of base stock for this warehouse
    GREATEST(2, ROUND(c.base_stock_tunis * c.size_factor * 0.15)::int)
                                                           AS stock_min_threshold,
    -- max capacity = 2.5× base stock
    ROUND(c.base_stock_tunis * c.size_factor * 2.5)::int  AS stock_max_capacity,
    c.stock_qty                                             AS current_stock_qty,
    -- available = stock minus 10% reserved
    GREATEST(0, ROUND(c.stock_qty * 0.90)::int)            AS available_qty,
    ROUND(c.stock_qty * 0.10)::int                         AS reserved_qty,
    -- estimated monthly sales for this warehouse
    GREATEST(1, ROUND(c.base_stock_tunis * c.size_factor * 0.55 * c.seasonal_factor * c.growth_factor)::int)
                                                           AS sales_qty,
    -- avg monthly sales (slightly smoothed)
    ROUND((c.base_stock_tunis * c.size_factor * 0.52 * c.growth_factor)::numeric, 2)
                                                           AS avg_monthly_sales,
    -- days of supply
    ROUND(c.stock_qty::numeric /
      NULLIF(GREATEST(1, ROUND(c.base_stock_tunis * c.size_factor * 0.55 * c.seasonal_factor * c.growth_factor)::int), 0) * 30
    )::int                                                 AS days_of_supply,
    -- risk flags
    c.stock_qty <= GREATEST(2, ROUND(c.base_stock_tunis * c.size_factor * 0.15)::int)
                                                           AS is_rupture,
    c.stock_qty <= GREATEST(2, ROUND(c.base_stock_tunis * c.size_factor * 0.25)::int)
                                                           AS is_low_stock,
    c.stock_qty <= GREATEST(2, ROUND(c.base_stock_tunis * c.size_factor * 0.20)::int)
                                                           AS understock_risk,
    c.stock_qty >= ROUND(c.base_stock_tunis * c.size_factor * 2.2)::int
                                                           AS overstock_risk,
    FALSE                                                  AS has_zero_stock,
    FALSE                                                  AS has_negative_stock,
    c.snap_date::timestamp                                 AS snapshot_date,
    'seed_fibre_v1'                                        AS data_source,
    'monthly_snapshot'                                     AS record_type,
    c.type_prod                                            AS product_type,
    'seed_04_v1'                                           AS etl_batch_id
  FROM computed c
)

SELECT
  date_id, product_id, geo_id, warehouse_code,
  stock_quantity, stock_min_threshold, stock_max_capacity,
  current_stock_qty, available_qty, reserved_qty,
  sales_qty, avg_monthly_sales, days_of_supply,
  is_rupture, is_low_stock, understock_risk, overstock_risk,
  has_zero_stock, has_negative_stock,
  snapshot_date, data_source, record_type, product_type,
  etl_batch_id
FROM final

ON CONFLICT (date_id, product_id, warehouse_code) DO NOTHING;
