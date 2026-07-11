-- =============================================================================
-- SEED 02 — mart.fact_ventes  (enriched)
-- Generates realistic sales rows for FIBRE (2023), 5G, DATA_BUNDLE, VOD
-- Period : 2023-01-01 → 2026-05-31
-- Strategy:
--   1. Populate dim_temps with every date in the period (ON CONFLICT DO NOTHING)
--   2. Generate monthly sales volumes per service × governorate
--      using seasonal multipliers and YoY growth factors
--   3. Link sales to active promo campaigns via promo_id
-- Unique constraint: (msisdn, created_at, service_id)
--   → satisfied because msisdn is derived from a global row_number() offset
-- =============================================================================

-- ─── STEP 1 : ensure dim_temps covers every day 2023-01-01 → 2026-05-31 ─────
INSERT INTO mart.dim_temps
  (date, annee, mois, jour, jour_semaine, trimestre, semaine,
   est_weekend, est_ferie, nom_mois, nom_jour, periode_ramadan, periode_ete)
SELECT
  d::date                                            AS date,
  EXTRACT(year    FROM d)::int                       AS annee,
  EXTRACT(month   FROM d)::int                       AS mois,
  EXTRACT(day     FROM d)::int                       AS jour,
  EXTRACT(dow     FROM d)::int                       AS jour_semaine,
  EXTRACT(quarter FROM d)::int                       AS trimestre,
  EXTRACT(week    FROM d)::int                       AS semaine,
  EXTRACT(dow FROM d) IN (0,6)                       AS est_weekend,
  FALSE                                              AS est_ferie,
  to_char(d, 'Month')                                AS nom_mois,
  to_char(d, 'Day')                                  AS nom_jour,
  (  d::date BETWEEN '2023-03-23' AND '2023-04-21'
  OR d::date BETWEEN '2024-03-11' AND '2024-04-09'
  OR d::date BETWEEN '2025-03-01' AND '2025-03-30'
  OR d::date BETWEEN '2026-02-18' AND '2026-03-19') AS periode_ramadan,
  EXTRACT(month FROM d) IN (6,7,8)                   AS periode_ete
FROM generate_series('2023-01-01'::date, '2026-05-31'::date, '1 day') AS d
ON CONFLICT (date) DO NOTHING;

-- ─── STEP 2 : insert sales rows ──────────────────────────────────────────────
-- Offre rotation for FIBRE (valid FK values: 1, 2, 4, 6)
-- 5G / DATA_BUNDLE / VOD → offre_id NULL (no offers defined for those services)
-- product_id NULL for services without physical products in dim_products

INSERT INTO mart.fact_ventes
  (date_id, service_id, geo_id, dealer_id, offre_id, product_id,
   promo_id, msisdn, client_id, created_at, transaction_type,
   source_file, etl_batch_id)

WITH

-- ── Governorates with one representative geo_id each ──────────────────────
geo AS (
  SELECT * FROM (VALUES
    (2,    'Tunis'),
    (7,    'Ariana'),
    (1,    'Ben Arous'),
    (26,   'Mannouba'),
    (5561, 'Sfax'),
    (5564, 'Sousse'),
    (5549, 'Bizerte'),
    (5560, 'Nabeul'),
    (5559, 'Monastir'),
    (5557, 'Mahdia'),
    (5558, 'Medenine'),
    (5553, 'Kairouan'),
    (5554, 'Kasserine'),
    (5552, 'Jendouba'),
    (5556, 'Kef'),
    (5563, 'Siliana'),
    (5551, 'Gafsa'),
    (5548, 'Beja'),
    (5550, 'Gabes'),
    (5562, 'Sidi Bouzid'),
    (5565, 'Tataouine'),
    (5555, 'Kebili'),
    (5566, 'Tozeur'),
    (5567, 'Zaghouan')
  ) AS t(geo_id, governorate)
),

-- ── Month spine: first of each month 2023-01 → 2026-05 ────────────────────
months AS (
  SELECT
    t.date_id,
    t.date        AS month_date,
    t.annee,
    t.mois
  FROM mart.dim_temps t
  WHERE t.date >= '2023-01-01' AND t.date <= '2026-05-01' AND t.jour = 1
),

-- ── Service definitions: base monthly volume in Tunis + growth model ───────
services AS (
  SELECT * FROM (VALUES
  --  svc_id  base_tunis   growth_model  (growth applied per year vs 2023)
    (1,        90,          'flat'),      -- FIBRE:  mature market, mild growth
    (2,        20,          'fast'),      -- 5G:     fast-growth +30%/yr
    (3,       300,          'slow'),      -- DATA:   stable +5%/yr
    (4,        90,          'decline')   -- VOD:    declining -10%/yr
  ) AS t(service_id, base_tunis, growth_model)
),

-- ── Regional weight per service ────────────────────────────────────────────
-- FIBRE: concentrated in Grand Tunis + coastal cities
-- 5G:    strong in Sfax/Sousse (recent deployment push)
-- DATA:  more uniform nationally
-- VOD:   urban-weighted
geo_weight AS (
  SELECT * FROM (VALUES
  -- governorate,   fibre,  fiveg, data,  vod
    ('Tunis',       1.00,   1.00,  1.00,  1.00),
    ('Ariana',      0.75,   0.65,  0.70,  0.75),
    ('Ben Arous',   0.50,   0.40,  0.50,  0.50),
    ('Mannouba',    0.20,   0.20,  0.30,  0.20),
    ('Sfax',        0.40,   0.80,  0.60,  0.40),
    ('Sousse',      0.35,   0.70,  0.55,  0.35),
    ('Bizerte',     0.25,   0.35,  0.35,  0.25),
    ('Nabeul',      0.20,   0.30,  0.35,  0.20),
    ('Monastir',    0.20,   0.35,  0.35,  0.20),
    ('Mahdia',      0.08,   0.12,  0.20,  0.08),
    ('Medenine',    0.08,   0.15,  0.20,  0.08),
    ('Kairouan',    0.06,   0.10,  0.18,  0.06),
    ('Kasserine',   0.04,   0.06,  0.12,  0.04),
    ('Jendouba',    0.04,   0.06,  0.12,  0.04),
    ('Kef',         0.03,   0.05,  0.10,  0.03),
    ('Siliana',     0.03,   0.04,  0.10,  0.03),
    ('Gafsa',       0.05,   0.08,  0.15,  0.05),
    ('Beja',        0.04,   0.06,  0.12,  0.04),
    ('Gabes',       0.06,   0.10,  0.15,  0.06),
    ('Sidi Bouzid', 0.03,   0.04,  0.10,  0.03),
    ('Tataouine',   0.02,   0.04,  0.08,  0.02),
    ('Kebili',      0.02,   0.04,  0.08,  0.02),
    ('Tozeur',      0.02,   0.04,  0.08,  0.02),
    ('Zaghouan',    0.03,   0.05,  0.10,  0.03)
  ) AS t(governorate, fibre_w, fiveg_w, data_w, vod_w)
),

-- ── Combine and compute monthly volume per cell ────────────────────────────
combos AS (
  SELECT
    m.date_id,
    m.month_date,
    m.annee,
    m.mois,
    g.geo_id,
    g.governorate,
    s.service_id,
    s.base_tunis,
    s.growth_model,
    -- regional weight for this service
    CASE s.service_id
      WHEN 1 THEN gw.fibre_w
      WHEN 2 THEN gw.fiveg_w
      WHEN 3 THEN gw.data_w
      WHEN 4 THEN gw.vod_w
    END AS reg_weight,
    -- YoY growth factor
    CASE s.growth_model
      WHEN 'fast'    THEN POWER(1.30, m.annee - 2023)
      WHEN 'slow'    THEN POWER(1.05, m.annee - 2023)
      WHEN 'decline' THEN POWER(0.90, m.annee - 2023)
      ELSE                POWER(1.03, m.annee - 2023)
    END AS growth_factor,
    -- seasonal multiplier
    CASE
      -- Ramadan windows
      WHEN (m.annee=2023 AND m.mois IN (3,4))
        OR (m.annee=2024 AND m.mois IN (3,4))
        OR (m.annee=2025 AND m.mois IN (2,3,4))
        OR (m.annee=2026 AND m.mois IN (2,3))          THEN
          CASE s.service_id WHEN 4 THEN 1.55 WHEN 3 THEN 1.30 ELSE 1.35 END
      -- Rentrée scolaire
      WHEN m.mois IN (8,9)                              THEN
          CASE s.service_id WHEN 2 THEN 1.35 WHEN 3 THEN 1.25 ELSE 1.22 END
      -- Summer
      WHEN m.mois IN (6,7)                              THEN
          CASE s.service_id WHEN 4 THEN 1.20 WHEN 3 THEN 1.10 ELSE 1.10 END
      -- Q4 end of year
      WHEN m.mois = 12                                  THEN 1.10
      -- January slowdown
      WHEN m.mois = 1                                   THEN 0.85
      ELSE 1.00
    END AS seasonal_mult
  FROM months m
  CROSS JOIN geo g
  CROSS JOIN services s
  JOIN geo_weight gw ON gw.governorate = g.governorate
  WHERE
    -- FIBRE not yet deployed in deep south / interior (2023 constraint)
    NOT (s.service_id = 1 AND g.governorate IN ('Kasserine','Kef','Siliana','Tataouine','Kebili','Tozeur') AND m.annee = 2023)
    -- 5G only available nationally from mid-2023
    AND NOT (s.service_id = 2 AND m.annee = 2023 AND m.mois < 6)
    -- Skip zero-volume cells
    AND CASE s.service_id
          WHEN 1 THEN gw.fibre_w
          WHEN 2 THEN gw.fiveg_w
          WHEN 3 THEN gw.data_w
          WHEN 4 THEN gw.vod_w
        END > 0
),

-- ── Compute final row count per cell ──────────────────────────────────────
volumes AS (
  SELECT
    *,
    GREATEST(1, ROUND(base_tunis * reg_weight * growth_factor * seasonal_mult)::int) AS n_rows
  FROM combos
),

-- ── Expand each cell into individual sale rows ─────────────────────────────
expanded AS (
  SELECT v.*, g.n AS row_in_month
  FROM volumes v,
       generate_series(1, v.n_rows) AS g(n)
),

-- ── Assign a global sequence number for unique msisdn generation ───────────
numbered AS (
  SELECT
    *,
    -- offset from max existing msisdn (21699991715) to guarantee no collision
    ROW_NUMBER() OVER (ORDER BY service_id, date_id, geo_id, row_in_month) + 21700000000 AS seq
  FROM expanded
),

-- ── Lookup active promo for this sale date + service ─────────────────────
promo_lookup AS (
  SELECT
    p.promo_id,
    p.service_id AS p_svc,
    p.date_debut,
    p.date_fin
  FROM mart.dim_promotions p
),

-- ── Dealer rotation (only retail dealers exist, use closest by geo cluster) -
dealer_map AS (
  SELECT * FROM (VALUES
    ('Tunis',       'M01'),
    ('Ariana',      'I07'),
    ('Ben Arous',   'I22'),
    ('Mannouba',    'M23'),
    ('Sfax',        'S12'),
    ('Sousse',      'S29'),
    ('Bizerte',     'I37'),
    ('Nabeul',      'S34'),
    ('Monastir',    'S40'),
    ('Mahdia',      'S44'),
    ('Medenine',    'M18'),
    ('Kairouan',    'I45'),
    ('Kasserine',   'I64'),
    ('Jendouba',    'I73'),
    ('Kef',         'I76'),
    ('Siliana',     'TST'),
    ('Gafsa',       'TST'),
    ('Beja',        'TST'),
    ('Gabes',       'TST'),
    ('Sidi Bouzid', 'TST'),
    ('Tataouine',   'TST'),
    ('Kebili',      'TST'),
    ('Tozeur',      'TST'),
    ('Zaghouan',    'TST')
  ) AS t(governorate, dealer_id)
),

-- ── Final assembly ─────────────────────────────────────────────────────────
final AS (
  SELECT
    n.date_id,
    n.service_id,
    n.geo_id,
    dm.dealer_id,
    -- offre_id: FIBRE only, cycle through 1,2,4,6
    CASE n.service_id
      WHEN 1 THEN (ARRAY[1,2,4,6])[ (n.seq % 4) + 1 ]
      ELSE NULL
    END::int AS offre_id,
    -- product_id: assign physical/subscription SKUs for all services so
    -- sales can be joined to stock inventory by product.
    CASE n.service_id
      WHEN 1 THEN (ARRAY['FBR004','FBR005','FBR006','FBR001','FBR002','FBR003','FBR007','FBR008','FBR009'])[ (n.seq % 9) + 1 ]
      WHEN 2 THEN (ARRAY['8812215','8812216','8812217','8812218','8812219','8812220','8812221'])[ (n.seq % 7) + 1 ]
      WHEN 3 THEN (ARRAY['DATA001','DATA002','DATA003','DATA004','DATA005'])[ (n.seq % 5) + 1 ]
      WHEN 4 THEN (ARRAY['VOD001','VOD002','VOD003'])[ (n.seq % 3) + 1 ]
    END AS product_id,
    -- promo_id: link if a promo was active on this month for this service
    (SELECT pl.promo_id
     FROM promo_lookup pl
     WHERE pl.p_svc = n.service_id
       AND n.month_date BETWEEN pl.date_debut AND pl.date_fin
     ORDER BY pl.promo_id
     LIMIT 1) AS promo_id,
    -- unique msisdn
    ('216' || LPAD(n.seq::text, 9, '0')) AS msisdn,
    ('CLI-' || n.seq::text) AS client_id,
    -- spread timestamps across the month proportional to row position
    (n.month_date + ((n.row_in_month::float / NULLIF(n.n_rows,0)) * 28 || ' days')::interval
                  + ((n.seq % 23) || ' hours')::interval
                  + ((n.seq % 59) || ' minutes')::interval) AS created_at,
    -- transaction type mix: 80% new, 10% renewal, 7% upgrade, 3% cancellation
    CASE
      WHEN (n.seq % 100) = 0                      THEN 'resiliation'
      WHEN (n.seq % 100) BETWEEN 1  AND 3         THEN 'upgrade'
      WHEN (n.seq % 100) BETWEEN 4  AND 13        THEN 'renouvellement'
      ELSE                                              'new_subscription'
    END AS transaction_type
  FROM numbered n
  JOIN dealer_map dm ON dm.governorate = n.governorate
)

SELECT
  date_id, service_id, geo_id, dealer_id, offre_id, product_id,
  promo_id, msisdn, client_id, created_at, transaction_type,
  'seed_02_v1' AS source_file,
  'seed_enrichment_2023_2026' AS etl_batch_id
FROM final

ON CONFLICT (msisdn, created_at, service_id) DO NOTHING;

-- ─── STEP 3 : link unmatched FIBRE sales in 2024+ to their promotions ────────
-- (rows inserted before promo linkage had NULL promo_id — update them)
UPDATE mart.fact_ventes v
SET promo_id = (
  SELECT p.promo_id
  FROM mart.dim_promotions p
  JOIN mart.dim_services s ON p.service_id = s.service_id
  WHERE s.service_id = v.service_id
    AND v.created_at::date BETWEEN p.date_debut AND p.date_fin
  ORDER BY p.promo_id
  LIMIT 1
)
WHERE v.promo_id IS NULL
  AND v.etl_batch_id IN ('seed_enrichment_2023_2026', 'seed_02_v1')
  AND EXISTS (
    SELECT 1 FROM mart.dim_promotions p
    WHERE p.service_id = v.service_id
      AND v.created_at::date BETWEEN p.date_debut AND p.date_fin
  );
