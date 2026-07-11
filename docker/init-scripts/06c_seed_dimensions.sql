-- =============================================================================
-- SEED DIMENSIONS — must run before any fact seeds (alphabetically after 06b)
-- Populates: dim_geographie, dim_offres, dim_products, dim_dealers
--
-- WHY OVERRIDING SYSTEM VALUE on geo_id / offre_id:
--   Fact seeds hardcode specific IDs (e.g. geo_id=5561 for Sfax, offre_id=4
--   for FIBRE-ULTRA). These IDs must match exactly or FK inserts will fail.
-- =============================================================================

-- ─── 1. GEOGRAPHY (24 Tunisian governorates) ──────────────────────────────────
INSERT INTO mart.dim_geographie
  (geo_id, city, governorate, region_code, latitude, longitude)
OVERRIDING SYSTEM VALUE
VALUES
  (1,    'Ben Arous',   'Ben Arous',   '13',  36.7475,  10.2368),
  (2,    'Tunis',       'Tunis',       '11',  36.8065,  10.1815),
  (7,    'Ariana',      'Ariana',      '12',  36.8625,  10.1952),
  (26,   'Mannouba',    'Mannouba',    '14',  36.8084,  10.0980),
  (5548, 'Béja',        'Beja',        '31',  36.7256,   9.1817),
  (5549, 'Bizerte',     'Bizerte',     '23',  37.2746,   9.8739),
  (5550, 'Gabès',       'Gabes',       '81',  33.8828,  10.0982),
  (5551, 'Gafsa',       'Gafsa',       '71',  34.4250,   8.7842),
  (5552, 'Jendouba',    'Jendouba',    '32',  36.5014,   8.7802),
  (5553, 'Kairouan',    'Kairouan',    '41',  35.6812,  10.0966),
  (5554, 'Kasserine',   'Kasserine',   '42',  35.1667,   8.8300),
  (5555, 'Kébili',      'Kebili',      '73',  33.7046,   8.9693),
  (5556, 'Kef',         'Kef',         '33',  36.1822,   8.7149),
  (5557, 'Mahdia',      'Mahdia',      '54',  35.5047,  11.0622),
  (5558, 'Médenine',    'Medenine',    '82',  33.3549,  10.5055),
  (5559, 'Monastir',    'Monastir',    '53',  35.7643,  10.8113),
  (5560, 'Nabeul',      'Nabeul',      '21',  36.4511,  10.7343),
  (5561, 'Sfax',        'Sfax',        '61',  34.7398,  10.7600),
  (5562, 'Sidi Bouzid', 'Sidi Bouzid', '43',  35.0382,   9.4858),
  (5563, 'Siliana',     'Siliana',     '34',  36.0840,   9.3708),
  (5564, 'Sousse',      'Sousse',      '51',  35.8245,  10.6346),
  (5565, 'Tataouine',   'Tataouine',   '83',  32.9211,  10.4510),
  (5566, 'Tozeur',      'Tozeur',      '72',  33.9197,   8.1335),
  (5567, 'Zaghouan',    'Zaghouan',    '22',  36.4020,  10.1431)
ON CONFLICT (geo_id) DO NOTHING;

-- Reset the sequence so future INSERTs get IDs above the max we just inserted
SELECT setval(
  pg_get_serial_sequence('mart.dim_geographie', 'geo_id'),
  (SELECT MAX(geo_id) FROM mart.dim_geographie)
);


-- ─── 2. OFFERS (FIBRE subscription tiers) ────────────────────────────────────
-- Sales seed uses offre_id IN (1, 2, 4, 6) for FIBRE rows.
-- Insert 6 sequential rows so those IDs are guaranteed.
INSERT INTO mart.dim_offres
  (offre_id, offre_code, offre_name, service_id, debit, price, category, is_active)
OVERRIDING SYSTEM VALUE
VALUES
  (1, 'FIBRE-STARTER', 'Fibre Starter 20 Mbps',       1, '20/10 Mbps',    29.90, 'residential', true),
  (2, 'FIBRE-CONFORT', 'Fibre Confort 50 Mbps',        1, '50/25 Mbps',    39.90, 'residential', true),
  (3, 'FIBRE-PLUS',    'Fibre Plus 100 Mbps',          1, '100/50 Mbps',   49.90, 'residential', true),
  (4, 'FIBRE-ULTRA',   'Fibre Ultra 200 Mbps',         1, '200/100 Mbps',  59.90, 'residential', true),
  (5, 'FIBRE-PRO',     'Fibre Pro Business 500 Mbps',  1, '500/200 Mbps',  89.90, 'business',    true),
  (6, 'FIBRE-GIGA',    'Fibre Giga 1 Gbps',            1, '1000/500 Mbps', 79.90, 'premium',     true)
ON CONFLICT (offre_id) DO NOTHING;

SELECT setval(
  pg_get_serial_sequence('mart.dim_offres', 'offre_id'),
  (SELECT MAX(offre_id) FROM mart.dim_offres)
);


-- ─── 3. PRODUCTS ──────────────────────────────────────────────────────────────
-- FIBRE physical CPE (referenced in fact_ventes and fact_stock)
INSERT INTO mart.dim_products
  (product_id, product_name, product_category, service_id,
   price, is_active, product_line, product_family, flag_fibre, is_sellable, is_deliverable)
VALUES
  ('FBR001', 'Décodeur TV HD FTTH',             'CPE',          1,   0.00, true, 'FTTH Equipment', 'FIBRE', true,  true,  true),
  ('FBR002', 'Routeur WiFi 5 FTTH',             'CPE',          1,   0.00, true, 'FTTH Equipment', 'FIBRE', true,  true,  true),
  ('FBR003', 'Routeur WiFi 6 FTTH Pro',         'CPE',          1,   0.00, true, 'FTTH Equipment', 'FIBRE', true,  true,  true),
  ('FBR004', 'Pack Démarrage Fibre',            'Bundle',       1, 199.00, true, 'FTTH Bundle',    'FIBRE', true,  true,  true),
  ('FBR005', 'Pack Fibre Premium TV',           'Bundle',       1, 299.00, true, 'FTTH Bundle',    'FIBRE', true,  true,  true),
  ('FBR006', 'Pack Fibre Business',             'Bundle',       1, 349.00, true, 'FTTH Bundle',    'FIBRE', true,  true,  true),
  ('FBR007', 'CPE Fibre Compact',               'CPE',          1,   0.00, true, 'FTTH Equipment', 'FIBRE', true,  true,  true),
  ('FBR008', 'Répéteur WiFi Mesh FTTH',         'Accessoire',   1,  89.00, true, 'FTTH Equipment', 'FIBRE', true,  true,  true),
  ('FBR009', 'Kit Extension WiFi Fibre',        'Accessoire',   1,  69.00, true, 'FTTH Equipment', 'FIBRE', true,  true,  true),
  -- 5G CPE devices (referenced in fact_ventes)
  ('8812215', '5G CPE Indoor Entry (B535)',     'CPE',          2, 299.00, true, '5G Home',        '5G',    false, true,  true),
  ('8812216', '5G CPE Indoor Standard (MC801)', 'CPE',          2, 349.00, true, '5G Home',        '5G',    false, true,  true),
  ('8812217', '5G CPE Indoor Pro (H155-381)',   'CPE',          2, 429.00, true, '5G Home',        '5G',    false, true,  true),
  ('8812218', '5G CPE Outdoor (B818)',          'CPE',          2, 499.00, true, '5G Home',        '5G',    false, true,  true),
  ('8812219', '5G SIM Activée Résidentielle',  'SIM',          2,   9.00, true, '5G Access',      '5G',    false, true,  false),
  ('8812220', '5G Pack Business CPE',          'Bundle',       2, 599.00, true, '5G Business',    '5G',    false, true,  true),
  ('8812221', '5G CPE WiFi 6 Premium',         'CPE',          2, 549.00, true, '5G Home',        '5G',    false, true,  true),
  -- DATA_BUNDLE subscription products
  ('DATA001', 'Data Bundle 5Go',               'Subscription', 3,  19.00, true, 'Mobile Data',    'DATA',  false, true,  false),
  ('DATA002', 'Data Bundle 10Go',              'Subscription', 3,  29.00, true, 'Mobile Data',    'DATA',  false, true,  false),
  ('DATA003', 'Data Bundle 20Go',              'Subscription', 3,  39.00, true, 'Mobile Data',    'DATA',  false, true,  false),
  ('DATA004', 'Data Bundle 50Go',              'Subscription', 3,  59.00, true, 'Mobile Data',    'DATA',  false, true,  false),
  ('DATA005', 'Data Bundle Illimité',          'Subscription', 3,  79.00, true, 'Mobile Data',    'DATA',  false, true,  false),
  -- VOD subscription products
  ('VOD001',  'VOD Basic',                     'Subscription', 4,   9.90, true, 'Video',          'VOD',   false, true,  false),
  ('VOD002',  'VOD Standard',                  'Subscription', 4,  14.90, true, 'Video',          'VOD',   false, true,  false),
  ('VOD003',  'VOD Premium',                   'Subscription', 4,  24.90, true, 'Video',          'VOD',   false, true,  false)
ON CONFLICT (product_id) DO NOTHING;


-- ─── 4. DEALERS ───────────────────────────────────────────────────────────────
-- One retail dealer per governorate as used in the sales seed dealer_map CTE.
-- geo_id values reference the rows inserted in section 1 above.
INSERT INTO mart.dim_dealers
  (dealer_id, dealer_name, dealer_type, activation_date, is_active, geo_id)
VALUES
  ('M01',  'Agence Tunis Centre',     'retail', '2019-01-01', true,  2),
  ('I07',  'Agence Ariana',           'retail', '2019-06-01', true,  7),
  ('I22',  'Agence Ben Arous',        'retail', '2020-01-01', true,  1),
  ('M23',  'Agence Mannouba',         'retail', '2020-03-01', true,  26),
  ('S12',  'Agence Sfax',             'retail', '2020-06-01', true,  5561),
  ('S29',  'Agence Sousse',           'retail', '2020-06-01', true,  5564),
  ('I37',  'Agence Bizerte',          'retail', '2021-01-01', true,  5549),
  ('S34',  'Agence Nabeul',           'retail', '2021-03-01', true,  5560),
  ('S40',  'Agence Monastir',         'retail', '2021-03-01', true,  5559),
  ('S44',  'Agence Mahdia',           'retail', '2021-06-01', true,  5557),
  ('M18',  'Agence Médenine',         'retail', '2021-09-01', true,  5558),
  ('I45',  'Agence Kairouan',         'retail', '2022-01-01', true,  5553),
  ('I64',  'Agence Kasserine',        'retail', '2022-06-01', true,  5554),
  ('I73',  'Agence Jendouba',         'retail', '2022-06-01', true,  5552),
  ('I76',  'Agence Kef',              'retail', '2022-09-01', true,  5556),
  ('TST',  'Agence Intérieur Test',   'retail', '2022-09-01', true,  NULL)
ON CONFLICT (dealer_id) DO NOTHING;
