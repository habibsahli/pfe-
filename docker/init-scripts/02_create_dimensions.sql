-- ===== DIMENSION TABLES =====

-- Time Dimension
CREATE TABLE IF NOT EXISTS mart.dim_temps (
  date_id SERIAL PRIMARY KEY,
  date DATE NOT NULL UNIQUE,
  annee INTEGER NOT NULL,
  mois INTEGER NOT NULL,
  jour INTEGER NOT NULL,
  jour_semaine INTEGER,
  trimestre INTEGER,
  semaine INTEGER,
  est_weekend BOOLEAN DEFAULT FALSE,
  est_ferie BOOLEAN DEFAULT FALSE,
  nom_mois VARCHAR(20),
  nom_jour VARCHAR(20),
  periode_ramadan BOOLEAN DEFAULT FALSE,
  periode_ete BOOLEAN DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_dim_temps_date ON mart.dim_temps(date);
CREATE INDEX IF NOT EXISTS idx_dim_temps_annee_mois ON mart.dim_temps(annee, mois);

-- Geography Dimension
CREATE TABLE IF NOT EXISTS mart.dim_geographie (
  geo_id SERIAL PRIMARY KEY,
  city VARCHAR(100),
  governorate VARCHAR(100),
  delegation VARCHAR(100),
  locality VARCHAR(100),
  postal_code VARCHAR(20),
  latitude DECIMAL(10, 8),
  longitude DECIMAL(11, 8),
  region_code VARCHAR(10),
  UNIQUE (city, governorate, delegation)
);

CREATE INDEX IF NOT EXISTS idx_dim_geo_city ON mart.dim_geographie(city);
CREATE INDEX IF NOT EXISTS idx_dim_geo_governorate ON mart.dim_geographie(governorate);

-- Services Dimension
CREATE TABLE IF NOT EXISTS mart.dim_services (
  service_id SERIAL PRIMARY KEY,
  service_code VARCHAR(50) UNIQUE NOT NULL,
  service_name VARCHAR(100) NOT NULL,
  service_category VARCHAR(50),
  description TEXT,
  is_active BOOLEAN DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_dim_services_code ON mart.dim_services(service_code);

-- Pre-populate services
INSERT INTO mart.dim_services (service_code, service_name, service_category, is_active) VALUES
('FIBRE', 'Fibre Optique FTTH', 'connectivity', TRUE),
('5G', 'Fixe Jdid 5G Home', 'connectivity', TRUE),
('DATA_BUNDLE', 'Data Mobile Bundle', 'connectivity', TRUE),
('VOD', 'Video On Demand', 'entertainment', TRUE)
ON CONFLICT (service_code) DO NOTHING;

-- Offers Dimension
CREATE TABLE IF NOT EXISTS mart.dim_offres (
  offre_id SERIAL PRIMARY KEY,
  offre_code VARCHAR(100) UNIQUE NOT NULL,
  offre_name VARCHAR(200),
  service_id INTEGER REFERENCES mart.dim_services(service_id),
  debit VARCHAR(50),
  price DECIMAL(10, 2),
  category VARCHAR(50),
  is_active BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_dim_offres_code ON mart.dim_offres(offre_code);
CREATE INDEX IF NOT EXISTS idx_dim_offres_service ON mart.dim_offres(service_id);

-- Products Dimension
CREATE TABLE IF NOT EXISTS mart.dim_products (
  product_id VARCHAR(50) PRIMARY KEY,
  product_name VARCHAR(200),
  product_category VARCHAR(100),
  service_id INTEGER REFERENCES mart.dim_services(service_id),
  price DECIMAL(10, 2),
  is_active BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_dim_products_service ON mart.dim_products(service_id);

-- Dealers Dimension
CREATE TABLE IF NOT EXISTS mart.dim_dealers (
  dealer_id VARCHAR(50) PRIMARY KEY,
  dealer_name VARCHAR(200),
  dealer_type VARCHAR(50),
  activation_date DATE,
  is_active BOOLEAN DEFAULT TRUE,
  geo_id INTEGER REFERENCES mart.dim_geographie(geo_id),
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_dim_dealers_active ON mart.dim_dealers(is_active);
CREATE INDEX IF NOT EXISTS idx_dim_dealers_geo ON mart.dim_dealers(geo_id);

-- Promotions Dimension
CREATE TABLE IF NOT EXISTS mart.dim_promotions (
  promo_id SERIAL PRIMARY KEY,
  promo_code VARCHAR(100) UNIQUE NOT NULL,
  promo_name VARCHAR(200),
  promo_type VARCHAR(50),  -- discount, bundle, loyalty
  discount_pct DECIMAL(5, 2),
  date_debut DATE NOT NULL,
  date_fin DATE NOT NULL,
  service_id INTEGER REFERENCES mart.dim_services(service_id),
  geo_id INTEGER REFERENCES mart.dim_geographie(geo_id),
  description TEXT,
  source_document VARCHAR(500),
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_dim_promos_dates ON mart.dim_promotions(date_debut, date_fin);
CREATE INDEX IF NOT EXISTS idx_dim_promos_service ON mart.dim_promotions(service_id);
