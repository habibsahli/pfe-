-- =============================================================================
-- SEED 13 — mart.fact_stock for 5G CPE products
-- Monthly stock snapshots for products 8812215–8812221, all 24 governorates.
-- Period: 2023-06-01 → 2026-05-01  (5G launched nationally mid-2023)
-- =============================================================================

INSERT INTO mart.fact_stock
  (date_id, product_id, geo_id, warehouse_code,
   stock_quantity, stock_min_threshold, stock_max_capacity,
   current_stock_qty, available_qty, reserved_qty,
   sales_qty, activations_qty, avg_monthly_sales, days_of_supply,
   is_rupture, is_low_stock, understock_risk, overstock_risk,
   has_zero_stock, has_negative_stock,
   snapshot_date, data_source, record_type, product_type,
   etl_batch_id)

WITH

months AS (
  SELECT date_id, date AS snap_date, annee, mois
  FROM mart.dim_temps
  WHERE date >= '2023-06-01' AND date <= '2026-05-01' AND jour = 1
),

-- 5G CPE product catalogue — base stock at Tunis level
products AS (
  SELECT * FROM (VALUES
  --  product_id   base_tunis  margin_factor
    ('8812215',     80,         0.55),   -- CPE Indoor Entry  (high volume)
    ('8812216',     65,         0.50),   -- CPE Indoor Standard
    ('8812217',     45,         0.45),   -- CPE Indoor Pro
    ('8812218',     30,         0.40),   -- CPE Outdoor       (low volume)
    ('8812219',    120,         0.70),   -- SIM Activée       (high volume, low unit)
    ('8812220',     20,         0.35),   -- Pack Business
    ('8812221',     35,         0.42)    -- CPE WiFi 6 Premium
  ) AS t(product_id, base_stock_tunis, margin_factor)
),

-- Regional deployment weights — 5G is urban-first
-- Matches the fiveg_w values in the sales seed
warehouses AS (
  SELECT * FROM (VALUES
    ('Tunis',       2,    1.00, 'TUN'),
    ('Ariana',      7,    0.65, 'ARI'),
    ('Ben Arous',   1,    0.40, 'BNA'),
    ('Mannouba',    26,   0.20, 'MAN'),
    ('Sfax',        5561, 0.80, 'SFX'),
    ('Sousse',      5564, 0.70, 'SOU'),
    ('Bizerte',     5549, 0.35, 'BIZ'),
    ('Nabeul',      5560, 0.30, 'NAB'),
    ('Monastir',    5559, 0.35, 'MON'),
    ('Mahdia',      5557, 0.12, 'MAH'),
    ('Medenine',    5558, 0.15, 'MED'),
    ('Kairouan',    5553, 0.10, 'KAI'),
    ('Kasserine',   5554, 0.06, 'KAS'),
    ('Jendouba',    5552, 0.06, 'JEN'),
    ('Kef',         5556, 0.05, 'KEF'),
    ('Siliana',     5563, 0.04, 'SIL'),
    ('Gafsa',       5551, 0.08, 'GAF'),
    ('Beja',        5548, 0.06, 'BEJ'),
    ('Gabes',       5550, 0.10, 'GAB'),
    ('Sidi Bouzid', 5562, 0.04, 'SBO'),
    ('Tataouine',   5565, 0.04, 'TAT'),
    ('Kebili',      5555, 0.04, 'KEB'),
    ('Tozeur',      5566, 0.04, 'TOZ'),
    ('Zaghouan',    5567, 0.05, 'ZAG')
  ) AS t(warehouse_code, geo_id, size_factor, wh_code)
),

combos AS (
  SELECT
    m.date_id,
    m.snap_date,
    m.annee,
    m.mois,
    p.product_id,
    p.base_stock_tunis,
    p.margin_factor,
    w.warehouse_code,
    w.geo_id,
    w.size_factor,
    -- Seasonal factor: pre-Ramadan stock-up, post-summer depletion, rentrée bump
    CASE
      WHEN (m.annee=2023 AND m.mois=3)
        OR (m.annee=2024 AND m.mois=2)
        OR (m.annee=2025 AND m.mois=2)
        OR (m.annee=2026 AND m.mois=2)                    THEN 1.35
      WHEN (m.annee=2023 AND m.mois=4)
        OR (m.annee=2024 AND m.mois=3)
        OR (m.annee=2025 AND m.mois=3)
        OR (m.annee=2026 AND m.mois=3)                    THEN 0.85
      WHEN m.mois = 8                                      THEN 1.25
      WHEN m.mois = 9                                      THEN 0.90
      WHEN m.mois = 12                                     THEN 1.15
      WHEN m.mois = 1                                      THEN 1.10
      ELSE 1.00
    END AS seasonal_factor,
    -- 5G market growing faster than FIBRE: +30%/yr (matches sales seed growth_model='fast')
    POWER(1.30, m.annee - 2023 + (m.mois - 6)::float / 12.0) AS growth_factor
  FROM months m
  CROSS JOIN products p
  CROSS JOIN warehouses w
),

computed AS (
  SELECT
    *,
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
    c.stock_qty                                                         AS stock_quantity,
    GREATEST(2, ROUND(c.base_stock_tunis * c.size_factor * 0.12)::int) AS stock_min_threshold,
    ROUND(c.base_stock_tunis * c.size_factor * 2.0)::int               AS stock_max_capacity,
    c.stock_qty                                                         AS current_stock_qty,
    GREATEST(0, ROUND(c.stock_qty * 0.88)::int)                        AS available_qty,
    ROUND(c.stock_qty * 0.12)::int                                      AS reserved_qty,
    GREATEST(1, ROUND(c.base_stock_tunis * c.size_factor * 0.65 * c.seasonal_factor * c.growth_factor)::int)
                                                                        AS sales_qty,
    GREATEST(1, ROUND(c.base_stock_tunis * c.size_factor * 0.55 * c.seasonal_factor * c.growth_factor)::int)
                                                                        AS activations_qty,
    ROUND((c.base_stock_tunis * c.size_factor * 0.60 * c.growth_factor)::numeric, 2)
                                                                        AS avg_monthly_sales,
    ROUND(c.stock_qty::numeric /
      NULLIF(GREATEST(1, ROUND(c.base_stock_tunis * c.size_factor * 0.65 * c.seasonal_factor * c.growth_factor)::int), 0) * 30
    )::int                                                              AS days_of_supply,
    c.stock_qty <= GREATEST(2, ROUND(c.base_stock_tunis * c.size_factor * 0.12)::int) AS is_rupture,
    c.stock_qty <= GREATEST(2, ROUND(c.base_stock_tunis * c.size_factor * 0.20)::int) AS is_low_stock,
    c.stock_qty <= GREATEST(2, ROUND(c.base_stock_tunis * c.size_factor * 0.18)::int) AS understock_risk,
    c.stock_qty >= ROUND(c.base_stock_tunis * c.size_factor * 1.8)::int               AS overstock_risk,
    FALSE                                                               AS has_zero_stock,
    FALSE                                                               AS has_negative_stock,
    c.snap_date::timestamp                                              AS snapshot_date,
    'seed_5g_v1'                                                        AS data_source,
    'monthly_snapshot'                                                  AS record_type,
    '5G'                                                                AS product_type,
    'seed_13_v1'                                                        AS etl_batch_id
  FROM computed c
)

SELECT
  date_id, product_id, geo_id, warehouse_code,
  stock_quantity, stock_min_threshold, stock_max_capacity,
  current_stock_qty, available_qty, reserved_qty,
  sales_qty, activations_qty, avg_monthly_sales, days_of_supply,
  is_rupture, is_low_stock, understock_risk, overstock_risk,
  has_zero_stock, has_negative_stock,
  snapshot_date, data_source, record_type, product_type,
  etl_batch_id
FROM final

ON CONFLICT (date_id, product_id, warehouse_code) DO NOTHING;
