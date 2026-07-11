-- ===== FACT TABLES =====

-- Sales Fact Table
CREATE TABLE IF NOT EXISTS mart.fact_ventes (
  vente_id BIGSERIAL PRIMARY KEY,
  
  -- Foreign keys
  date_id INTEGER NOT NULL REFERENCES mart.dim_temps(date_id),
  dealer_id VARCHAR(50) REFERENCES mart.dim_dealers(dealer_id),
  geo_id INTEGER REFERENCES mart.dim_geographie(geo_id),
  offre_id INTEGER REFERENCES mart.dim_offres(offre_id),
  service_id INTEGER NOT NULL REFERENCES mart.dim_services(service_id),
  product_id VARCHAR(50) REFERENCES mart.dim_products(product_id),
  promo_id INTEGER REFERENCES mart.dim_promotions(promo_id),
  
  -- Customer identification
  msisdn VARCHAR(50),
  client_id VARCHAR(100),
  
  -- Transaction data
  created_at TIMESTAMP NOT NULL,
  transaction_type VARCHAR(50),  -- new_subscription, renewal, upgrade, etc.
  
  -- Metadata
  source_file VARCHAR(500),
  etl_batch_id VARCHAR(100),
  created_etl TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  
  -- Constraints
  CONSTRAINT unique_vente UNIQUE (msisdn, created_at, service_id)
);

CREATE INDEX IF NOT EXISTS idx_fact_ventes_date ON mart.fact_ventes(date_id);
CREATE INDEX IF NOT EXISTS idx_fact_ventes_service ON mart.fact_ventes(service_id);
CREATE INDEX IF NOT EXISTS idx_fact_ventes_dealer ON mart.fact_ventes(dealer_id);
CREATE INDEX IF NOT EXISTS idx_fact_ventes_created_at ON mart.fact_ventes(created_at);
CREATE INDEX IF NOT EXISTS idx_fact_ventes_service_date ON mart.fact_ventes(service_id, date_id);

-- Stock Fact Table
CREATE TABLE IF NOT EXISTS mart.fact_stock (
  stock_id BIGSERIAL PRIMARY KEY,
  
  -- Foreign keys
  date_id INTEGER NOT NULL REFERENCES mart.dim_temps(date_id),
  product_id VARCHAR(50) NOT NULL REFERENCES mart.dim_products(product_id),
  geo_id INTEGER REFERENCES mart.dim_geographie(geo_id),
  
  -- Stock data
  stock_quantity INTEGER NOT NULL,
  stock_min_threshold INTEGER,
  stock_max_capacity INTEGER,
  warehouse_code VARCHAR(100),
  
  -- Status
  is_rupture BOOLEAN DEFAULT FALSE,
  is_low_stock BOOLEAN DEFAULT FALSE,
  
  -- Metadata
  snapshot_date TIMESTAMP NOT NULL,
  source_file VARCHAR(500),
  etl_batch_id VARCHAR(100),
  created_etl TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  
  CONSTRAINT unique_stock UNIQUE (date_id, product_id, warehouse_code)
);

CREATE INDEX IF NOT EXISTS idx_fact_stock_date ON mart.fact_stock(date_id);
CREATE INDEX IF NOT EXISTS idx_fact_stock_product ON mart.fact_stock(product_id);
CREATE INDEX IF NOT EXISTS idx_fact_stock_rupture ON mart.fact_stock(is_rupture);

-- ===== STAGING TABLES FOR ETL =====

CREATE TABLE IF NOT EXISTS staging.fact_ventes_raw (
  id SERIAL PRIMARY KEY,
  raw_data JSONB NOT NULL,
  detection_service VARCHAR(50),
  detection_confidence DECIMAL(3, 2),
  validation_status VARCHAR(50),  -- valid, invalid, warning
  validation_errors TEXT,
  processed BOOLEAN DEFAULT FALSE,
  processed_at TIMESTAMP,
  etl_batch_id VARCHAR(100)
);

CREATE INDEX IF NOT EXISTS idx_staging_ventes_batch ON staging.fact_ventes_raw(etl_batch_id);
CREATE INDEX IF NOT EXISTS idx_staging_ventes_processed ON staging.fact_ventes_raw(processed);

-- ===== MATERIALIZED VIEWS FOR FORECASTING =====

-- Daily sales by service (core for forecasting)
CREATE OR REPLACE VIEW mart.vw_daily_sales_by_service AS
SELECT
  t.date,
  s.service_code,
  s.service_id,
  COUNT(*) as nb_ventes,
  COUNT(DISTINCT v.dealer_id) as nb_dealers_actifs,
  COUNT(DISTINCT CASE WHEN v.promo_id IS NOT NULL THEN v.vente_id END) as nb_ventes_promo,
  SUM(CASE WHEN v.promo_id IS NOT NULL THEN 1 ELSE 0 END)::DECIMAL / NULLIF(COUNT(*), 0) *100 as pct_ventes_promo,
  AVG(o.price) as prix_moyen
FROM mart.fact_ventes v
JOIN mart.dim_temps t ON v.date_id = t.date_id
JOIN mart.dim_services s ON v.service_id = s.service_id
LEFT JOIN mart.dim_offres o ON v.offre_id = o.offre_id
GROUP BY t.date, s.service_code, s.service_id
ORDER BY t.date DESC, s.service_code;

-- Monthly sales forecasting view by service, product, category, and region
CREATE OR REPLACE VIEW mart.vw_monthly_sales_forecasting AS
SELECT
  DATE_TRUNC('month', t.date)::date AS month_start,
  t.annee AS year,
  t.mois AS month,
  s.service_code,
  s.service_id,
  COALESCE(v.product_id, 'UNKNOWN') AS product_id,
  COALESCE(p.product_name, 'UNKNOWN') AS product_name,
  COALESCE(p.product_category, 'UNKNOWN') AS product_category,
  COALESCE(g.region_code, g.governorate, g.city, 'UNKNOWN') AS region_key,
  COALESCE(g.governorate, g.city, 'UNKNOWN') AS region_label,
  COUNT(*) AS nb_ventes,
  COUNT(DISTINCT v.dealer_id) AS nb_dealers_actifs,
  COUNT(DISTINCT CASE WHEN v.promo_id IS NOT NULL THEN v.vente_id END) AS nb_ventes_promo,
  SUM(CASE WHEN v.promo_id IS NOT NULL THEN 1 ELSE 0 END)::DECIMAL / NULLIF(COUNT(*), 0) * 100 AS pct_ventes_promo,
  AVG(COALESCE(o.price, p.price)) AS prix_moyen
FROM mart.fact_ventes v
JOIN mart.dim_temps t ON v.date_id = t.date_id
JOIN mart.dim_services s ON v.service_id = s.service_id
LEFT JOIN mart.dim_products p ON v.product_id = p.product_id
LEFT JOIN mart.dim_geographie g ON v.geo_id = g.geo_id
LEFT JOIN mart.dim_offres o ON v.offre_id = o.offre_id
GROUP BY
  DATE_TRUNC('month', t.date)::date,
  t.annee,
  t.mois,
  s.service_code,
  s.service_id,
  COALESCE(v.product_id, 'UNKNOWN'),
  COALESCE(p.product_name, 'UNKNOWN'),
  COALESCE(p.product_category, 'UNKNOWN'),
  COALESCE(g.region_code, g.governorate, g.city, 'UNKNOWN'),
  COALESCE(g.governorate, g.city, 'UNKNOWN')
ORDER BY month_start DESC, s.service_code, product_category, region_key;

-- Daily sales forecasting view by service, product, category, and region
CREATE OR REPLACE VIEW mart.vw_daily_sales_forecasting AS
SELECT
  t.date,
  s.service_code,
  s.service_id,
  COALESCE(v.product_id, 'UNKNOWN') AS product_id,
  COALESCE(p.product_name, 'UNKNOWN') AS product_name,
  COALESCE(p.product_category, 'UNKNOWN') AS product_category,
  COALESCE(g.region_code, g.governorate, g.city, 'UNKNOWN') AS region_key,
  COALESCE(g.governorate, g.city, 'UNKNOWN') AS region_label,
  COUNT(*) AS nb_ventes,
  COUNT(DISTINCT v.dealer_id) AS nb_dealers_actifs,
  COUNT(DISTINCT CASE WHEN v.promo_id IS NOT NULL THEN v.vente_id END) AS nb_ventes_promo,
  SUM(CASE WHEN v.promo_id IS NOT NULL THEN 1 ELSE 0 END)::DECIMAL / NULLIF(COUNT(*), 0) * 100 AS pct_ventes_promo,
  AVG(COALESCE(o.price, p.price)) AS prix_moyen
FROM mart.fact_ventes v
JOIN mart.dim_temps t ON v.date_id = t.date_id
JOIN mart.dim_services s ON v.service_id = s.service_id
LEFT JOIN mart.dim_products p ON v.product_id = p.product_id
LEFT JOIN mart.dim_geographie g ON v.geo_id = g.geo_id
LEFT JOIN mart.dim_offres o ON v.offre_id = o.offre_id
GROUP BY
  t.date,
  s.service_code,
  s.service_id,
  COALESCE(v.product_id, 'UNKNOWN'),
  COALESCE(p.product_name, 'UNKNOWN'),
  COALESCE(p.product_category, 'UNKNOWN'),
  COALESCE(g.region_code, g.governorate, g.city, 'UNKNOWN'),
  COALESCE(g.governorate, g.city, 'UNKNOWN');

-- Daily-by-service mart
CREATE OR REPLACE VIEW mart.vw_daily_sales_forecasting_service AS
SELECT *
FROM mart.vw_daily_sales_forecasting;

-- Daily-by-region mart
CREATE OR REPLACE VIEW mart.vw_daily_sales_forecasting_region AS
SELECT *
FROM mart.vw_daily_sales_forecasting;

-- Daily-by-category mart
CREATE OR REPLACE VIEW mart.vw_daily_sales_forecasting_category AS
SELECT *
FROM mart.vw_daily_sales_forecasting;

-- Daily-by-product mart
CREATE OR REPLACE VIEW mart.vw_daily_sales_forecasting_product AS
SELECT *
FROM mart.vw_daily_sales_forecasting;

-- Daily stock summary
CREATE OR REPLACE VIEW mart.vw_daily_stock_summary AS
SELECT
  t.date,
  p.product_name,
  p.product_id,
  s.service_code,
  s.service_id,
  SUM(st.stock_quantity) as stock_total,
  COUNT(CASE WHEN st.is_rupture THEN 1 END) as nb_ruptures,
  COUNT(CASE WHEN st.is_low_stock THEN 1 END) as nb_low_stock,
  AVG(st.stock_quantity) as stock_moyen_warehouse
FROM mart.fact_stock st
JOIN mart.dim_temps t ON st.date_id = t.date_id
JOIN mart.dim_products p ON st.product_id = p.product_id
JOIN mart.dim_services s ON p.service_id = s.service_id
GROUP BY t.date, p.product_name, p.product_id, s.service_code, s.service_id
ORDER BY t.date DESC;

-- ===== HELPER FUNCTIONS =====

-- Get or create date_id for a given date
CREATE OR REPLACE FUNCTION mart.get_or_create_date_id(input_date DATE)
RETURNS INTEGER AS $$
DECLARE
  v_date_id INTEGER;
BEGIN
  SELECT date_id INTO v_date_id FROM mart.dim_temps WHERE date = input_date;
  
  IF v_date_id IS NULL THEN
    INSERT INTO mart.dim_temps (
      date, annee, mois, jour, jour_semaine, trimestre, semaine,
      est_weekend, nom_mois, nom_jour
    ) VALUES (
      input_date,
      EXTRACT(YEAR FROM input_date)::INTEGER,
      EXTRACT(MONTH FROM input_date)::INTEGER,
      EXTRACT(DAY FROM input_date)::INTEGER,
      EXTRACT(DOW FROM input_date)::INTEGER,
      CEILING(EXTRACT(MONTH FROM input_date) / 3.0)::INTEGER,
      EXTRACT(WEEK FROM input_date)::INTEGER,
      EXTRACT(DOW FROM input_date) IN (0, 6),
      TO_CHAR(input_date, 'Month'),
      TO_CHAR(input_date, 'Day')
    ) RETURNING date_id INTO v_date_id;
  END IF;
  
  RETURN v_date_id;
END;
$$ LANGUAGE plpgsql;

-- Get or create geo_id for a given location
CREATE OR REPLACE FUNCTION mart.get_or_create_geo_id(
  p_city VARCHAR,
  p_governorate VARCHAR,
  p_delegation VARCHAR DEFAULT NULL,
  p_locality VARCHAR DEFAULT NULL,
  p_postal_code VARCHAR DEFAULT NULL,
  p_latitude DECIMAL(10, 8) DEFAULT NULL,
  p_longitude DECIMAL(11, 8) DEFAULT NULL
)
RETURNS INTEGER AS $$
DECLARE
  v_geo_id INTEGER;
BEGIN
  SELECT geo_id INTO v_geo_id 
  FROM mart.dim_geographie 
  WHERE city = p_city AND governorate = p_governorate AND (delegation = p_delegation OR p_delegation IS NULL);
  
  IF v_geo_id IS NULL THEN
    INSERT INTO mart.dim_geographie (city, governorate, delegation, locality, postal_code, latitude, longitude)
    VALUES (p_city, p_governorate, p_delegation, p_locality, p_postal_code, p_latitude, p_longitude)
    RETURNING geo_id INTO v_geo_id;
  END IF;
  
  RETURN v_geo_id;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION mart.get_or_create_date_id IS 'Get or create a date dimension key';
COMMENT ON FUNCTION mart.get_or_create_geo_id IS 'Get or create a geography dimension key';
