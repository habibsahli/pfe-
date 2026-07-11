-- ===== PUBLIC SCHEMA TABLES =====
-- Tables used by the what-if simulation service.
-- Created here so seed files can insert data without
-- requiring the backend to run first.

CREATE TABLE IF NOT EXISTS public.fact_promotions (
    id SERIAL PRIMARY KEY,
    service_type VARCHAR(50) NOT NULL,
    region VARCHAR(100),
    discount_percent FLOAT NOT NULL,
    promo_start DATE NOT NULL,
    promo_end DATE NOT NULL,
    channel VARCHAR(50),
    event_type VARCHAR(50),
    actual_uplift_percent FLOAT,
    units_sold_during FLOAT,
    baseline_units_expected FLOAT,
    notes TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_fact_promotions_service
    ON public.fact_promotions(service_type, discount_percent);
CREATE INDEX IF NOT EXISTS idx_fact_promotions_event
    ON public.fact_promotions(event_type);

CREATE TABLE IF NOT EXISTS public.promo_elasticity (
    id SERIAL PRIMARY KEY,
    service_type VARCHAR(50),
    discount_min FLOAT NOT NULL,
    discount_max FLOAT NOT NULL,
    expected_uplift_percent FLOAT NOT NULL,
    post_promo_dip_percent FLOAT NOT NULL DEFAULT 0.30,
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.whatif_scenarios (
    id SERIAL PRIMARY KEY,
    scenario_name VARCHAR(200),
    request_params JSONB NOT NULL,
    results JSONB NOT NULL,
    rag_explanation TEXT,
    rag_sources JSONB DEFAULT '[]'::jsonb,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_whatif_scenarios_created
    ON public.whatif_scenarios(created_at DESC);

CREATE TABLE IF NOT EXISTS public.anomaly_reviews (
    id SERIAL PRIMARY KEY,
    anomaly_id VARCHAR(200) NOT NULL,
    action VARCHAR(50) NOT NULL,   -- reviewed | dismissed | escalated
    note TEXT,
    reviewed_at TIMESTAMP DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_anomaly_reviews_anomaly_id
    ON public.anomaly_reviews(anomaly_id);
CREATE INDEX IF NOT EXISTS idx_anomaly_reviews_action
    ON public.anomaly_reviews(action);

-- Schema must match app/api/anomaly.py ensure_anomaly_explanations_table()
CREATE TABLE IF NOT EXISTS public.anomaly_explanations (
    anomaly_id   TEXT        PRIMARY KEY,
    cause        TEXT        NOT NULL,
    sources      JSONB       NOT NULL DEFAULT '[]',
    explained_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Schema must match services/stock_recommendation_service.py _SKU_THRESHOLDS_DDL
CREATE TABLE IF NOT EXISTS public.sku_thresholds (
    product_id   TEXT        NOT NULL,
    governorate  TEXT        NOT NULL DEFAULT 'NATIONAL',
    min_stock    INTEGER,
    max_stock    INTEGER,
    notes        TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (product_id, governorate)
);

CREATE INDEX IF NOT EXISTS idx_sku_thresholds_product
    ON public.sku_thresholds(product_id);
