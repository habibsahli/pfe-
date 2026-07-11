-- SQL Migration: Add 5G Dataset columns to mart schema
-- Deployed: 2026-05-24
-- Purpose: Support new 5G stock forecasting dataset with product type, activations, and 5G flag columns

BEGIN;

-- 1. Add new columns to mart.fact_stock for 5G dataset support
ALTER TABLE IF EXISTS mart.fact_stock
  ADD COLUMN IF NOT EXISTS activations_qty    INTEGER DEFAULT 0 CHECK (activations_qty >= 0),
  ADD COLUMN IF NOT EXISTS stock_opening_qty  INTEGER DEFAULT 0 CHECK (stock_opening_qty >= 0),
  ADD COLUMN IF NOT EXISTS flag_5g            BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS product_type       VARCHAR(30),
  ADD COLUMN IF NOT EXISTS data_source        VARCHAR(20) DEFAULT 'CSV_IMPORT';

-- 2. Add new columns to mart.dim_products for product classification
ALTER TABLE IF EXISTS mart.dim_products
  ADD COLUMN IF NOT EXISTS type_prod   VARCHAR(10),
  ADD COLUMN IF NOT EXISTS cod_group   VARCHAR(20),
  ADD COLUMN IF NOT EXISTS flag_5g     BOOLEAN DEFAULT FALSE;

-- 3. Ensure mart.dim_geographie has normalized governorate field
ALTER TABLE IF EXISTS mart.dim_geographie
  ADD COLUMN IF NOT EXISTS governorate_normalized VARCHAR(100);

-- 4. Create or update index on fact_stock for common query patterns (date, product, data_source)
CREATE INDEX IF NOT EXISTS idx_fact_stock_date_product_source 
  ON mart.fact_stock(snapshot_date, product_id, data_source);

-- 5. Create index for governorate lookup in geographie table
CREATE INDEX IF NOT EXISTS idx_dim_geographie_governorate_normalized 
  ON mart.dim_geographie(governorate_normalized);

-- 6. Create index on product_type for segmentation queries
CREATE INDEX IF NOT EXISTS idx_fact_stock_product_type 
  ON mart.fact_stock(product_type);

-- 7. Create index on flag_5g for 5G-specific queries
CREATE INDEX IF NOT EXISTS idx_fact_stock_flag_5g 
  ON mart.fact_stock(flag_5g);

COMMIT;
