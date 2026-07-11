-- Create additional databases required by platform services.
-- This runs before all other init scripts (alphabetical order).
-- postgres-entrypoint connects to the default DB (fibre_forecast_db) first,
-- so we must use DO $$ to run CREATE DATABASE outside a transaction block.

SELECT 'CREATE DATABASE phoenix_db OWNER admin'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'phoenix_db')\gexec
