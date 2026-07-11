-- ===== CREATE SCHEMAS =====

-- Drop existing if needed (for dev)
DROP SCHEMA IF EXISTS mart CASCADE;
DROP SCHEMA IF EXISTS staging CASCADE;

-- Production schema (facts + dimensions)
CREATE SCHEMA mart;

-- ETL staging (temporary)
CREATE SCHEMA staging;

-- Grant permissions
GRANT ALL PRIVILEGES ON SCHEMA mart TO admin;
GRANT ALL PRIVILEGES ON SCHEMA staging TO admin;

-- ===== MLFLOW SCHEMA =====
-- MLflow will create its own tables automatically when first run
CREATE SCHEMA IF NOT EXISTS mlflow;
GRANT ALL PRIVILEGES ON SCHEMA mlflow TO admin;

COMMENT ON SCHEMA mart IS 'Production star schema for forecasting';
COMMENT ON SCHEMA staging IS 'ETL staging area';
