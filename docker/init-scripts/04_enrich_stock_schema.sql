-- ===== ENRICH PRODUCTS DIMENSION FOR STOCK DATA =====
-- Add stock-specific and product detail columns to dim_products

ALTER TABLE mart.dim_products ADD COLUMN IF NOT EXISTS sku VARCHAR(100);
ALTER TABLE mart.dim_products ADD COLUMN IF NOT EXISTS cod_prod VARCHAR(100);
ALTER TABLE mart.dim_products ADD COLUMN IF NOT EXISTS product_line VARCHAR(100);
ALTER TABLE mart.dim_products ADD COLUMN IF NOT EXISTS cost_price_ht DECIMAL(12, 2);
ALTER TABLE mart.dim_products ADD COLUMN IF NOT EXISTS selling_price_ttc DECIMAL(12, 2);
ALTER TABLE mart.dim_products ADD COLUMN IF NOT EXISTS vat_rate DECIMAL(5, 2);
ALTER TABLE mart.dim_products ADD COLUMN IF NOT EXISTS flag_fibre BOOLEAN DEFAULT FALSE;
ALTER TABLE mart.dim_products ADD COLUMN IF NOT EXISTS is_sellable BOOLEAN DEFAULT TRUE;
ALTER TABLE mart.dim_products ADD COLUMN IF NOT EXISTS is_deliverable BOOLEAN DEFAULT TRUE;
ALTER TABLE mart.dim_products ADD COLUMN IF NOT EXISTS is_eol BOOLEAN DEFAULT FALSE;
ALTER TABLE mart.dim_products ADD COLUMN IF NOT EXISTS product_family VARCHAR(100);

CREATE INDEX IF NOT EXISTS idx_dim_products_sku ON mart.dim_products(sku);
CREATE INDEX IF NOT EXISTS idx_dim_products_cod_prod ON mart.dim_products(cod_prod);
CREATE INDEX IF NOT EXISTS idx_dim_products_flag_fibre ON mart.dim_products(flag_fibre);
CREATE INDEX IF NOT EXISTS idx_dim_products_is_eol ON mart.dim_products(is_eol);

-- ===== ENRICH STOCK FACT TABLE FOR DETAILED INVENTORY TRACKING =====
-- Add inventory granularity, movement, and sales metrics

ALTER TABLE mart.fact_stock ADD COLUMN IF NOT EXISTS current_stock_qty INTEGER;
ALTER TABLE mart.fact_stock ADD COLUMN IF NOT EXISTS inventory_qty INTEGER;
ALTER TABLE mart.fact_stock ADD COLUMN IF NOT EXISTS reserved_qty INTEGER;
ALTER TABLE mart.fact_stock ADD COLUMN IF NOT EXISTS available_qty INTEGER;
ALTER TABLE mart.fact_stock ADD COLUMN IF NOT EXISTS sales_qty INTEGER;
ALTER TABLE mart.fact_stock ADD COLUMN IF NOT EXISTS activations_qty INTEGER;
ALTER TABLE mart.fact_stock ADD COLUMN IF NOT EXISTS stock_movement INTEGER;
ALTER TABLE mart.fact_stock ADD COLUMN IF NOT EXISTS avg_monthly_sales DECIMAL(10, 2);
ALTER TABLE mart.fact_stock ADD COLUMN IF NOT EXISTS sell_through_rate DECIMAL(5, 2);
ALTER TABLE mart.fact_stock ADD COLUMN IF NOT EXISTS days_of_supply INTEGER;
ALTER TABLE mart.fact_stock ADD COLUMN IF NOT EXISTS stock_vs_min INTEGER;
ALTER TABLE mart.fact_stock ADD COLUMN IF NOT EXISTS stock_vs_max INTEGER;
ALTER TABLE mart.fact_stock ADD COLUMN IF NOT EXISTS has_zero_stock BOOLEAN DEFAULT FALSE;
ALTER TABLE mart.fact_stock ADD COLUMN IF NOT EXISTS has_negative_stock BOOLEAN DEFAULT FALSE;
ALTER TABLE mart.fact_stock ADD COLUMN IF NOT EXISTS understock_risk BOOLEAN DEFAULT FALSE;
ALTER TABLE mart.fact_stock ADD COLUMN IF NOT EXISTS overstock_risk BOOLEAN DEFAULT FALSE;

-- Add dealer and data lineage info
ALTER TABLE mart.fact_stock ADD COLUMN IF NOT EXISTS dealer_id VARCHAR(50) REFERENCES mart.dim_dealers(dealer_id);
ALTER TABLE mart.fact_stock ADD COLUMN IF NOT EXISTS data_source VARCHAR(100);
ALTER TABLE mart.fact_stock ADD COLUMN IF NOT EXISTS record_type VARCHAR(50);
ALTER TABLE mart.fact_stock ADD COLUMN IF NOT EXISTS product_type VARCHAR(100);
ALTER TABLE mart.fact_stock ADD COLUMN IF NOT EXISTS exercise VARCHAR(50);
ALTER TABLE mart.fact_stock ADD COLUMN IF NOT EXISTS last_updated TIMESTAMP;

-- New indexes for stock analysis
CREATE INDEX IF NOT EXISTS idx_fact_stock_available_qty ON mart.fact_stock(available_qty);
CREATE INDEX IF NOT EXISTS idx_fact_stock_reserved_qty ON mart.fact_stock(reserved_qty);
CREATE INDEX IF NOT EXISTS idx_fact_stock_understock_risk ON mart.fact_stock(understock_risk);
CREATE INDEX IF NOT EXISTS idx_fact_stock_overstock_risk ON mart.fact_stock(overstock_risk);
CREATE INDEX IF NOT EXISTS idx_fact_stock_has_zero ON mart.fact_stock(has_zero_stock);
CREATE INDEX IF NOT EXISTS idx_fact_stock_dealer ON mart.fact_stock(dealer_id);

-- ===== ENRICHED STOCK VIEWS =====

-- Detailed stock position by product and dealer
CREATE OR REPLACE VIEW mart.vw_stock_position_detail AS
SELECT
  t.date,
  d.dealer_id,
  d.dealer_name,
  dg.governorate,
  dg.city,
  p.product_id,
  p.product_name,
  p.product_line,
  p.product_family,
  p.flag_fibre,
  s.service_code,
  fs.current_stock_qty,
  fs.inventory_qty,
  fs.reserved_qty,
  fs.available_qty,
  fs.stock_movement,
  fs.avg_monthly_sales,
  fs.sell_through_rate,
  fs.days_of_supply,
  fs.stock_vs_min,
  fs.stock_vs_max,
  fs.understock_risk,
  fs.overstock_risk,
  CASE 
    WHEN fs.has_zero_stock THEN 'OUT_OF_STOCK'
    WHEN fs.understock_risk THEN 'LOW_STOCK'
    WHEN fs.overstock_risk THEN 'HIGH_STOCK'
    ELSE 'NORMAL'
  END AS stock_status
FROM mart.fact_stock fs
JOIN mart.dim_temps t ON fs.date_id = t.date_id
JOIN mart.dim_products p ON fs.product_id = p.product_id
JOIN mart.dim_services s ON p.service_id = s.service_id
LEFT JOIN mart.dim_dealers d ON fs.dealer_id = d.dealer_id
LEFT JOIN mart.dim_geographie dg ON d.geo_id = dg.geo_id
ORDER BY t.date DESC, p.product_id, d.dealer_id;

-- Stock summary by region and product line
CREATE OR REPLACE VIEW mart.vw_stock_summary_by_region AS
SELECT
  t.date,
  dg.governorate,
  dg.city,
  p.product_line,
  p.product_family,
  p.flag_fibre,
  COUNT(DISTINCT fs.product_id) AS num_products,
  SUM(fs.current_stock_qty) AS total_current_stock,
  AVG(fs.current_stock_qty) AS avg_stock_per_product,
  SUM(fs.available_qty) AS total_available,
  SUM(fs.reserved_qty) AS total_reserved,
  COUNT(CASE WHEN fs.understock_risk THEN 1 END) AS num_understock_products,
  COUNT(CASE WHEN fs.overstock_risk THEN 1 END) AS num_overstock_products,
  COUNT(CASE WHEN fs.has_zero_stock THEN 1 END) AS num_out_of_stock,
  AVG(fs.sell_through_rate) AS avg_sell_through_rate,
  AVG(fs.days_of_supply) AS avg_days_of_supply
FROM mart.fact_stock fs
JOIN mart.dim_temps t ON fs.date_id = t.date_id
JOIN mart.dim_products p ON fs.product_id = p.product_id
LEFT JOIN mart.dim_geographie dg ON fs.geo_id = dg.geo_id
GROUP BY t.date, dg.governorate, dg.city, p.product_line, p.product_family, p.flag_fibre
ORDER BY t.date DESC, dg.governorate, p.product_line;

-- Stock trends by product
CREATE OR REPLACE VIEW mart.vw_stock_trend_by_product AS
SELECT
  t.date,
  p.product_id,
  p.product_name,
  p.product_category,
  p.flag_fibre,
  s.service_code,
  SUM(fs.current_stock_qty) AS total_stock,
  AVG(fs.available_qty) AS avg_available,
  SUM(fs.sales_qty) AS total_sales,
  AVG(fs.sell_through_rate) AS avg_sell_through_rate,
  COUNT(CASE WHEN fs.understock_risk THEN 1 END) AS num_locations_understock,
  COUNT(CASE WHEN fs.overstock_risk THEN 1 END) AS num_locations_overstock
FROM mart.fact_stock fs
JOIN mart.dim_temps t ON fs.date_id = t.date_id
JOIN mart.dim_products p ON fs.product_id = p.product_id
JOIN mart.dim_services s ON p.service_id = s.service_id
GROUP BY t.date, p.product_id, p.product_name, p.product_category, p.flag_fibre, s.service_code
ORDER BY t.date DESC, s.service_code, p.product_id;

COMMENT ON VIEW mart.vw_stock_position_detail IS 'Detailed stock position by product, dealer, and date for inventory analysis';
COMMENT ON VIEW mart.vw_stock_summary_by_region IS 'Stock summary aggregated by region and product line for regional planning';
COMMENT ON VIEW mart.vw_stock_trend_by_product IS 'Stock trends by product for forecasting and demand planning';
