-- =============================================================================
-- SEED 05 — public.promo_elasticity  (service-specific elasticity curves)
-- Adds per-service rows so the simulation fallback returns a meaningful
-- uplift calibrated to each product line instead of one generic number.
--
-- Elasticity rationale:
--   FIBRE   : mature captive product — low price elasticity, customers
--             subscribe for infrastructure not price alone.
--   5G      : new market, price-sensitive early adopters, high elasticity.
--   DATA    : commodity bundle, very price-elastic, frequent churn if no deal.
--   VOD     : pure entertainment, highest elasticity — consumers cancel/
--             subscribe based on price every month.
--
-- Format: (service_type, discount_min, discount_max,
--          expected_uplift_percent, post_promo_dip_percent)
-- =============================================================================

INSERT INTO public.promo_elasticity
  (service_type, discount_min, discount_max,
   expected_uplift_percent, post_promo_dip_percent)
VALUES

-- ─── FIBRE ─────────────────────────────────────────────────────────────────
-- Lower elasticity: a 10% discount barely moves the needle (customers
-- choose FIBRE for infrastructure quality, not discounts). 30%+ does work.
('FIBRE',  5.0, 10.0,  8.0, 0.10),
('FIBRE', 10.0, 20.0, 16.0, 0.15),
('FIBRE', 20.0, 30.0, 26.0, 0.20),
('FIBRE', 30.0, 50.0, 38.0, 0.25),

-- ─── 5G ────────────────────────────────────────────────────────────────────
-- Higher elasticity: early adopters are tech-savvy deal-seekers.
-- Hardware bundling (CPE) amplifies the uplift effect.
('5G',     5.0, 10.0, 14.0, 0.18),
('5G',    10.0, 20.0, 25.0, 0.22),
('5G',    20.0, 30.0, 38.0, 0.28),
('5G',    30.0, 50.0, 52.0, 0.32),

-- ─── DATA ──────────────────────────────────────────────────────────────────
-- High elasticity: data bundles are commodities, customers switch
-- on price. Large base means even small uplifts = large absolute volumes.
('DATA',   5.0, 10.0, 12.0, 0.22),
('DATA',  10.0, 20.0, 22.0, 0.28),
('DATA',  20.0, 30.0, 34.0, 0.32),
('DATA',  30.0, 50.0, 48.0, 0.38),

-- ─── VOD ───────────────────────────────────────────────────────────────────
-- Highest elasticity: subscription streaming is discretionary spend.
-- Customers trial then cancel — high post-promo dip is structural.
('VOD',    5.0, 10.0, 16.0, 0.35),
('VOD',   10.0, 20.0, 28.0, 0.40),
('VOD',   20.0, 30.0, 42.0, 0.45),
('VOD',   30.0, 50.0, 58.0, 0.50)

ON CONFLICT DO NOTHING;
