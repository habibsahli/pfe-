-- =============================================================================
-- SEED 12 — mart.fact_stock for DATA_BUNDLE and VOD
-- Monthly snapshots across all 24 Tunisian governorates, 2024-01 → 2026-05
-- Products: DATA001-005 (service_id=3), VOD001-003 (service_id=4)
-- =============================================================================

-- dim_products rows for DATA/VOD are now seeded in 06c_seed_dimensions.sql.
-- The INSERT here is kept as a safety net only.
INSERT INTO mart.dim_products (product_id, product_name, product_category, service_id, is_active, product_line, product_family)
VALUES
  ('DATA001', 'Data Bundle 5Go',      'Subscription', 3, true, 'Mobile Data', 'DATA'),
  ('DATA002', 'Data Bundle 10Go',     'Subscription', 3, true, 'Mobile Data', 'DATA'),
  ('DATA003', 'Data Bundle 20Go',     'Subscription', 3, true, 'Mobile Data', 'DATA'),
  ('DATA004', 'Data Bundle 50Go',     'Subscription', 3, true, 'Mobile Data', 'DATA'),
  ('DATA005', 'Data Bundle Illimite', 'Subscription', 3, true, 'Mobile Data', 'DATA'),
  ('VOD001',  'VOD Basic',            'Subscription', 4, true, 'Video',       'VOD'),
  ('VOD002',  'VOD Standard',         'Subscription', 4, true, 'Video',       'VOD'),
  ('VOD003',  'VOD Premium',          'Subscription', 4, true, 'Video',       'VOD')
ON CONFLICT (product_id) DO NOTHING;

INSERT INTO mart.fact_stock
  (date_id, product_id, geo_id, warehouse_code,
   stock_quantity, available_qty, reserved_qty, current_stock_qty,
   avg_monthly_sales, sell_through_rate, days_of_supply,
   has_zero_stock, understock_risk, overstock_risk,
   activations_qty,
   snapshot_date, source_file, etl_batch_id, product_type, data_source, record_type)

WITH

months AS (
  SELECT date_id, date, annee, mois
  FROM mart.dim_temps
  WHERE date >= '2024-01-01' AND date <= '2026-05-01' AND jour = 1
),

geo AS (
  SELECT * FROM (VALUES
    (2,    'Tunis',       'TUN'),
    (7,    'Ariana',      'ARI'),
    (1,    'Ben Arous',   'BNA'),
    (26,   'Mannouba',    'MAN'),
    (5561, 'Sfax',        'SFX'),
    (5564, 'Sousse',      'SOU'),
    (5549, 'Bizerte',     'BIZ'),
    (5560, 'Nabeul',      'NAB'),
    (5559, 'Monastir',    'MON'),
    (5557, 'Mahdia',      'MAH'),
    (5558, 'Medenine',    'MED'),
    (5553, 'Kairouan',    'KAI'),
    (5554, 'Kasserine',   'KAS'),
    (5552, 'Jendouba',    'JEN'),
    (5556, 'Kef',         'KEF'),
    (5563, 'Siliana',     'SIL'),
    (5551, 'Gafsa',       'GAF'),
    (5548, 'Beja',        'BEJ'),
    (5550, 'Gabes',       'GAB'),
    (5562, 'Sidi Bouzid', 'SBO'),
    (5565, 'Tataouine',   'TAT'),
    (5555, 'Kebili',      'KEB'),
    (5566, 'Tozeur',      'TOZ'),
    (5567, 'Zaghouan',    'ZAG')
  ) AS t(geo_id, governorate, wh_code)
),

products AS (
  SELECT * FROM (VALUES
    ('DATA001', 3, 1800, 0.72),
    ('DATA002', 3, 2200, 0.68),
    ('DATA003', 3, 1600, 0.65),
    ('DATA004', 3,  900, 0.60),
    ('DATA005', 3,  500, 0.55),
    ('VOD001',  4,  600, 0.50),
    ('VOD002',  4,  900, 0.55),
    ('VOD003',  4,  400, 0.45)
  ) AS t(product_id, service_id, base_national_stock, str_rate)
),

-- Regional weight: DATA is more uniform, VOD urban-weighted
geo_weight AS (
  SELECT * FROM (VALUES
    ('Tunis',       1.00, 1.00),
    ('Ariana',      0.70, 0.70),
    ('Ben Arous',   0.50, 0.50),
    ('Mannouba',    0.30, 0.25),
    ('Sfax',        0.60, 0.50),
    ('Sousse',      0.55, 0.45),
    ('Bizerte',     0.35, 0.30),
    ('Nabeul',      0.35, 0.30),
    ('Monastir',    0.35, 0.30),
    ('Mahdia',      0.20, 0.15),
    ('Medenine',    0.22, 0.15),
    ('Kairouan',    0.18, 0.12),
    ('Kasserine',   0.12, 0.08),
    ('Jendouba',    0.12, 0.08),
    ('Kef',         0.10, 0.07),
    ('Siliana',     0.10, 0.07),
    ('Gafsa',       0.15, 0.10),
    ('Beja',        0.12, 0.08),
    ('Gabes',       0.16, 0.12),
    ('Sidi Bouzid', 0.10, 0.07),
    ('Tataouine',   0.08, 0.05),
    ('Kebili',      0.08, 0.05),
    ('Tozeur',      0.08, 0.05),
    ('Zaghouan',    0.10, 0.07)
  ) AS t(governorate, data_w, vod_w)
),

combos AS (
  SELECT
    m.date_id,
    m.date AS snapshot_date,
    m.annee,
    m.mois,
    g.geo_id,
    g.wh_code,
    p.product_id,
    p.service_id,
    p.str_rate,
    GREATEST(10, ROUND(
      p.base_national_stock
      * CASE p.service_id WHEN 3 THEN gw.data_w ELSE gw.vod_w END
      -- seasonal multiplier
      * CASE
          WHEN m.mois IN (3, 4) THEN 0.75  -- Ramadan: lower stock (sell-through)
          WHEN m.mois IN (8, 9) THEN 1.20  -- Rentree: stock up
          WHEN m.mois IN (6, 7) THEN 1.10  -- Summer
          ELSE 1.00
        END
    )::int) AS qty
  FROM months m
  CROSS JOIN geo g
  CROSS JOIN products p
  JOIN geo_weight gw ON gw.governorate = g.governorate
)

SELECT
  c.date_id,
  c.product_id,
  c.geo_id,
  c.wh_code                                   AS warehouse_code,
  c.qty                                        AS stock_quantity,
  ROUND(c.qty * 0.90)::int                     AS available_qty,
  ROUND(c.qty * 0.10)::int                     AS reserved_qty,
  c.qty                                        AS current_stock_qty,
  ROUND(c.qty * c.str_rate / 3.0, 1)          AS avg_monthly_sales,
  c.str_rate                                   AS sell_through_rate,
  ROUND(c.qty / NULLIF(c.qty * c.str_rate / 30.0, 0))::int AS days_of_supply,
  false                                        AS has_zero_stock,
  (c.qty * 0.90 < c.qty * c.str_rate * 2)     AS understock_risk,
  false                                        AS overstock_risk,
  ROUND(c.qty * c.str_rate * 0.75)::int        AS activations_qty,
  c.snapshot_date,
  'seed_12_v1'                                 AS source_file,
  'seed_data_vod_stock_2024_2026'              AS etl_batch_id,
  CASE c.service_id WHEN 3 THEN 'DATA_BUNDLE' ELSE 'VOD' END AS product_type,
  'seed'                                       AS data_source,
  'monthly_snapshot'                           AS record_type
FROM combos c

ON CONFLICT DO NOTHING;
