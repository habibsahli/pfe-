-- Migration: Promo What-If Simulation Tables
-- Run with: psql $DATABASE_URL -f migrations/add_promo_simulation_tables.sql

-- Historical promotions — used to estimate uplift from real past campaigns
CREATE TABLE IF NOT EXISTS fact_promotions (
    id SERIAL PRIMARY KEY,
    service_type VARCHAR(50) NOT NULL,          -- FIBRE, 5G, DATA, VOD
    region VARCHAR(100),                         -- governorate or NULL = national
    discount_percent FLOAT NOT NULL,
    promo_start DATE NOT NULL,
    promo_end DATE NOT NULL,
    channel VARCHAR(50),                         -- online, in-store, etc.
    actual_uplift_percent FLOAT,                 -- measured after promo ended
    units_sold_during FLOAT,
    baseline_units_expected FLOAT,
    notes TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_fact_promotions_service
    ON fact_promotions(service_type, discount_percent);
CREATE INDEX IF NOT EXISTS idx_fact_promotions_dates
    ON fact_promotions(promo_start, promo_end);

-- Default elasticity curve — fallback when no historical promos match
CREATE TABLE IF NOT EXISTS promo_elasticity (
    id SERIAL PRIMARY KEY,
    service_type VARCHAR(50),                    -- NULL = global default
    discount_min FLOAT NOT NULL,
    discount_max FLOAT NOT NULL,
    expected_uplift_percent FLOAT NOT NULL,
    post_promo_dip_percent FLOAT NOT NULL DEFAULT 0.30,
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Seed global defaults (service_type IS NULL = applies to all services)
INSERT INTO promo_elasticity (service_type, discount_min, discount_max, expected_uplift_percent, post_promo_dip_percent)
SELECT NULL, 5,  10, 10.0, 0.20 WHERE NOT EXISTS (SELECT 1 FROM promo_elasticity WHERE discount_min = 5  AND service_type IS NULL);
INSERT INTO promo_elasticity (service_type, discount_min, discount_max, expected_uplift_percent, post_promo_dip_percent)
SELECT NULL, 10, 20, 20.0, 0.25 WHERE NOT EXISTS (SELECT 1 FROM promo_elasticity WHERE discount_min = 10 AND service_type IS NULL);
INSERT INTO promo_elasticity (service_type, discount_min, discount_max, expected_uplift_percent, post_promo_dip_percent)
SELECT NULL, 20, 30, 30.0, 0.30 WHERE NOT EXISTS (SELECT 1 FROM promo_elasticity WHERE discount_min = 20 AND service_type IS NULL);
INSERT INTO promo_elasticity (service_type, discount_min, discount_max, expected_uplift_percent, post_promo_dip_percent)
SELECT NULL, 30, 50, 45.0, 0.35 WHERE NOT EXISTS (SELECT 1 FROM promo_elasticity WHERE discount_min = 30 AND service_type IS NULL);

-- Seed 30 synthetic historical promotions for development
INSERT INTO fact_promotions
    (service_type, region, discount_percent, promo_start, promo_end, channel, actual_uplift_percent, units_sold_during, baseline_units_expected, notes)
VALUES
    ('FIBRE', 'Tunis',    15, '2023-01-05', '2023-01-20', 'online',    18.2,  420,  355, 'Campagne Janvier Fibre Tunis'),
    ('FIBRE', 'Sfax',     20, '2023-02-10', '2023-02-28', 'in-store',  24.5,  188,  151, 'Promo Fibre Sud'),
    ('FIBRE', NULL,       25, '2023-03-15', '2023-03-31', 'online',    28.1, 1240, 968, 'Campagne nationale Printemps Fibre'),
    ('FIBRE', 'Tunis',    10, '2023-05-01', '2023-05-15', 'online',    12.0,  390,  348, 'Mini promo Fibre mai'),
    ('FIBRE', 'Sousse',   20, '2023-06-01', '2023-06-30', 'in-store',  22.8,  270,  220, 'Ete Fibre Sousse'),
    ('FIBRE', NULL,       30, '2023-09-01', '2023-09-30', 'online',    34.2, 2100, 1565, 'Rentree Fibre nationale'),
    ('FIBRE', 'Tunis',    15, '2023-11-20', '2023-12-05', 'online',    17.5,  460,  391, 'Black Friday Fibre'),
    ('FIBRE', NULL,       25, '2023-12-15', '2023-12-31', 'in-store',  29.0, 1850, 1434, 'Offre de fin d annee Fibre'),
    ('5G',    'Tunis',    20, '2023-02-01', '2023-02-28', 'online',    22.4,  320,  261, 'Lancement 5G Tunis'),
    ('5G',    'Sfax',     15, '2023-04-10', '2023-04-25', 'in-store',  16.8,  145,  124, 'Extension 5G Sfax'),
    ('5G',    NULL,       30, '2023-07-01', '2023-07-31', 'online',    33.5,  980,  734, 'Promo ete 5G nationale'),
    ('5G',    'Tunis',    10, '2023-08-15', '2023-08-31', 'online',    11.2,  285,  256, 'Mini promo aout 5G'),
    ('5G',    NULL,       25, '2023-10-01', '2023-10-31', 'online',    27.8,  870,  680, 'Campagne octobre 5G'),
    ('5G',    'Tunis',    20, '2023-11-20', '2023-12-05', 'online',    23.1,  410,  333, 'Black Friday 5G'),
    ('DATA',  NULL,       15, '2023-01-10', '2023-01-31', 'online',    17.3, 5600, 4776, 'Promo Data janvier'),
    ('DATA',  'Tunis',    20, '2023-03-01', '2023-03-31', 'in-store',  23.9, 3100, 2502, 'Data Tunis mars'),
    ('DATA',  NULL,       30, '2023-06-15', '2023-07-15', 'online',    36.2, 9800, 7195, 'Promo ete Data'),
    ('DATA',  NULL,       25, '2023-09-01', '2023-09-30', 'online',    29.4, 8200, 6337, 'Rentree Data'),
    ('DATA',  NULL,       20, '2023-11-20', '2023-12-05', 'online',    24.1, 6100, 4916, 'Black Friday Data'),
    ('DATA',  NULL,       15, '2023-12-15', '2023-12-31', 'in-store',  18.7, 4800, 4044, 'Noel Data'),
    ('VOD',   NULL,       20, '2023-02-14', '2023-02-28', 'online',    19.5, 2200, 1840, 'Saint Valentin VOD'),
    ('VOD',   NULL,       30, '2023-05-01', '2023-05-31', 'online',    28.4, 3100, 2413, 'Promo VOD mai'),
    ('VOD',   NULL,       15, '2023-07-01', '2023-08-31', 'online',    14.2, 5800, 5079, 'Ete VOD'),
    ('VOD',   NULL,       25, '2023-10-15', '2023-10-31', 'online',    27.1, 2900, 2282, 'Halloween VOD'),
    ('VOD',   NULL,       35, '2023-11-20', '2023-12-05', 'online',    38.5, 4200, 3029, 'Black Friday VOD'),
    ('FIBRE', NULL,       20, '2022-09-01', '2022-09-30', 'online',    21.6, 1600, 1316, 'Rentree 2022 Fibre'),
    ('5G',    NULL,       20, '2022-06-01', '2022-06-30', 'online',    19.8,  780,  651, 'Ete 2022 5G'),
    ('DATA',  NULL,       25, '2022-11-20', '2022-12-05', 'online',    27.3, 7400, 5812, 'BF 2022 Data'),
    ('FIBRE', 'Bizerte',  15, '2023-04-01', '2023-04-30', 'in-store',  16.9,  155,  133, 'Promo Bizerte Fibre'),
    ('5G',    'Sousse',   20, '2023-09-15', '2023-09-30', 'online',    22.0,  210,  172, 'Sousse 5G septembre');

-- Saved simulation scenarios
CREATE TABLE IF NOT EXISTS whatif_scenarios (
    id SERIAL PRIMARY KEY,
    scenario_name VARCHAR(200),
    request_params JSONB NOT NULL,
    results JSONB NOT NULL,
    rag_explanation TEXT,
    rag_sources JSONB DEFAULT '[]'::jsonb,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_whatif_scenarios_created
    ON whatif_scenarios(created_at DESC);
