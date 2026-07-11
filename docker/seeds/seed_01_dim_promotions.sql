-- =============================================================================
-- SEED 01 — mart.dim_promotions
-- 40 historical promotion campaigns across FIBRE, 5G, DATA_BUNDLE, VOD
-- Period: 2023-01 → 2026-05
-- Islamic calendar dates used (approximate official Tunisian dates):
--   Ramadan 2023  : 2023-03-23 → 2023-04-21
--   Eid Fitr 2023 : 2023-04-22 → 2023-04-24
--   Eid Adha 2023 : 2023-06-28 → 2023-07-01
--   Ramadan 2024  : 2024-03-11 → 2024-04-09
--   Eid Fitr 2024 : 2024-04-10 → 2024-04-12
--   Eid Adha 2024 : 2024-06-16 → 2024-06-19
--   Ramadan 2025  : 2025-03-01 → 2025-03-30
--   Eid Fitr 2025 : 2025-03-31 → 2025-04-02
--   Eid Adha 2025 : 2025-06-07 → 2025-06-10
--   Ramadan 2026  : 2026-02-18 → 2026-03-19
--   Eid Fitr 2026 : 2026-03-20 → 2026-03-22
-- =============================================================================

INSERT INTO mart.dim_promotions
  (promo_code, promo_name, promo_type, discount_pct, date_debut, date_fin, service_id, geo_id, description, source_document)
VALUES

-- ─── FIBRE 2023 ──────────────────────────────────────────────────────────────
('FIBRE-RAM-2023',   'Ramadan Fibre 2023',          'seasonal',  20.00, '2023-03-10', '2023-04-21', 1, NULL, 'Promo Ramadan nationale Fibre 20% — abonnements nouveaux', 'plan_promo_2023.pdf'),
('FIBRE-EID-F-2023', 'Aïd el-Fitr Fibre 2023',      'seasonal',  15.00, '2023-04-22', '2023-05-05', 1, NULL, 'Promo Eid el-Fitr Fibre 15% sur 3 mois engagement',         'plan_promo_2023.pdf'),
('FIBRE-EID-A-2023', 'Aïd el-Adha Fibre 2023',      'seasonal',  15.00, '2023-06-20', '2023-07-10', 1, NULL, 'Promo Eid el-Adha Fibre 15%',                               'plan_promo_2023.pdf'),
('FIBRE-ETE-2023',   'Vacances Été Fibre 2023',      'seasonal',  10.00, '2023-06-15', '2023-08-15', 1, NULL, 'Promo estivale Fibre 10% — zones côtières',                 'plan_promo_2023.pdf'),
('FIBRE-REN-2023',   'Rentrée Fibre 2023',           'seasonal',  15.00, '2023-08-20', '2023-09-30', 1, NULL, 'Pack Rentrée Fibre — WiFi extender offert + 15% remise',     'plan_promo_2023.pdf'),
('FIBRE-NY-2024',    'Nouvel An Fibre 2024',         'seasonal',  20.00, '2023-12-26', '2024-01-07', 1, NULL, 'Offre Nouvel An Fibre 20% première mensualité offerte',      'plan_promo_2024.pdf'),
('FIBRE-IND-2024',   'Fête Indépendance Fibre 2024', 'national',  10.00, '2024-03-15', '2024-03-25', 1, NULL, 'Promo Fête Indépendance Fibre 10%',                         'plan_promo_2024.pdf'),
-- ─── FIBRE 2024 ──────────────────────────────────────────────────────────────
('FIBRE-RAM-2024',   'Ramadan Fibre 2024',           'seasonal',  25.00, '2024-03-01', '2024-04-09', 1, NULL, 'Promo Ramadan Fibre 2024 25% — 3 premiers mois offerts',     'plan_promo_2024.pdf'),
('FIBRE-EID-F-2024', 'Aïd el-Fitr Fibre 2024',      'seasonal',  15.00, '2024-04-10', '2024-04-30', 1, NULL, 'Promo Eid el-Fitr Fibre 15% frais installation offerts',    'plan_promo_2024.pdf'),
('FIBRE-EID-A-2024', 'Aïd el-Adha Fibre 2024',      'seasonal',  15.00, '2024-06-10', '2024-07-05', 1, NULL, 'Promo Eid el-Adha Fibre 15% engagement 12 mois',            'plan_promo_2024.pdf'),
('FIBRE-ETE-2024',   'Vacances Été Fibre 2024',      'seasonal',  10.00, '2024-06-15', '2024-08-15', 1, NULL, 'Pack Été Fibre 10% — décodeur TV inclus',                   'plan_promo_2024.pdf'),
('FIBRE-FNAT-2024',  'Fête Nationale Fibre 2024',    'national',  20.00, '2024-07-20', '2024-08-05', 1, NULL, 'Promo Fête Nationale 25 Juillet Fibre 20%',                 'plan_promo_2024.pdf'),
('FIBRE-REN-2024',   'Rentrée Fibre 2024',           'seasonal',  20.00, '2024-08-20', '2024-09-30', 1, NULL, 'Pack Rentrée Fibre 20% — 2 mois offerts sur 12',            'plan_promo_2024.pdf'),
-- ─── FIBRE 2025 ──────────────────────────────────────────────────────────────
('FIBRE-IND-2025',   'Fête Indépendance Fibre 2025', 'national',  10.00, '2025-03-15', '2025-03-25', 1, NULL, 'Promo Fête Indépendance Fibre 10%',                         'plan_promo_2025.pdf'),
('FIBRE-RAM-2025',   'Ramadan Fibre 2025',           'seasonal',  30.00, '2025-02-20', '2025-03-30', 1, NULL, 'Ramadan Fibre 2025 — 30% 3 premiers mois + TV gratuite',     'plan_promo_2025.pdf'),
('FIBRE-EID-F-2025', 'Aïd el-Fitr Fibre 2025',      'seasonal',  20.00, '2025-03-31', '2025-04-15', 1, NULL, 'Aïd el-Fitr Fibre 2025 — 20% + router Wi-Fi 6 offert',      'plan_promo_2025.pdf'),
('FIBRE-ETE-2025',   'Vacances Été Fibre 2025',      'seasonal',  15.00, '2025-06-15', '2025-08-15', 1, NULL, 'Promo Été Fibre 2025 — 15% + décodeur TV offert',           'plan_promo_2025.pdf'),
('FIBRE-FNAT-2025',  'Fête Nationale Fibre 2025',    'national',  15.00, '2025-07-20', '2025-08-05', 1, NULL, 'Promo Fête Nationale Fibre 2025 — 15% sur abonnements',     'plan_promo_2025.pdf'),
('FIBRE-REN-2025',   'Rentrée Fibre 2025',           'seasonal',  20.00, '2025-08-20', '2025-09-30', 1, NULL, 'Pack Rentrée Fibre 2025 — 20% + 1 mois offert',             'plan_promo_2025.pdf'),
('FIBRE-NY-2026',    'Nouvel An Fibre 2026',         'seasonal',  20.00, '2025-12-26', '2026-01-07', 1, NULL, 'Nouvel An Fibre 2026 — 20% + installation offerte',          'plan_promo_2026.pdf'),

-- ─── 5G 2023 ─────────────────────────────────────────────────────────────────
('5G-LAUNCH-SFX-23', 'Lancement 5G Sfax 2023',       'launch',    40.00, '2023-10-01', '2023-11-30', 2, 5561, 'Lancement réseau 5G Sfax — 40% premier abonnement',         'plan_lancement_5g.pdf'),
('5G-REN-2023',      'Rentrée 5G 2023',              'seasonal',  25.00, '2023-08-20', '2023-09-30', 2, NULL, 'Pack Rentrée Fixe Jdid 5G 25% — CPE inclus',                'plan_promo_2023.pdf'),
-- ─── 5G 2024 ─────────────────────────────────────────────────────────────────
('5G-RAM-2024',      'Ramadan Fixe Jdid 5G 2024',    'seasonal',  25.00, '2024-03-01', '2024-04-09', 2, NULL, 'Promo Ramadan 5G 2024 — 25% abonnement + CPE promotionnel', 'plan_promo_2024.pdf'),
('5G-ETE-2024',      'Vacances Été 5G 2024',         'seasonal',  20.00, '2024-06-15', '2024-08-15', 2, NULL, 'Pack Été 5G 2024 — 20% + bonus data illimité le week-end',  'plan_promo_2024.pdf'),
('5G-REN-2024',      'Rentrée Fixe Jdid 5G 2024',    'seasonal',  25.00, '2024-08-20', '2024-09-30', 2, NULL, 'Rentrée 5G 2024 — 25% + smartphone offert sous conditions', 'plan_promo_2024.pdf'),
-- ─── 5G 2025 ─────────────────────────────────────────────────────────────────
('5G-RAM-2025',      'Ramadan Fixe Jdid 5G 2025',    'seasonal',  30.00, '2025-02-20', '2025-03-30', 2, NULL, 'Promo Ramadan 5G 2025 — 30% abonnement + CPE high-end',     'plan_promo_2025.pdf'),
('5G-EID-F-2025',    'Aïd el-Fitr 5G 2025',         'seasonal',  20.00, '2025-03-31', '2025-04-15', 2, NULL, 'Aïd el-Fitr 5G 2025 — 20% + mois gratuit',                 'plan_promo_2025.pdf'),
('5G-ETE-2025',      'Vacances Été 5G 2025',         'seasonal',  25.00, '2025-06-15', '2025-08-15', 2, NULL, 'Été 5G 2025 — 25% + data illimitée le week-end',            'plan_promo_2025.pdf'),
('5G-FNAT-2025',     'Fête Nationale 5G 2025',       'national',  20.00, '2025-07-20', '2025-08-05', 2, NULL, 'Fête Nationale 5G 2025 — 20% engagement 12 mois',           'plan_promo_2025.pdf'),
('5G-REN-2025',      'Rentrée Fixe Jdid 5G 2025',    'seasonal',  30.00, '2025-08-20', '2025-09-30', 2, NULL, 'Rentrée 5G 2025 — 30% + CPE Wi-Fi 6 offert',               'plan_promo_2025.pdf'),
('5G-RAM-2026',      'Ramadan Fixe Jdid 5G 2026',    'seasonal',  30.00, '2026-02-10', '2026-03-22', 2, NULL, 'Promo Ramadan 5G 2026 — 30% abonnement + CPE',             'plan_promo_2026.pdf'),

-- ─── DATA_BUNDLE 2023-2025 ───────────────────────────────────────────────────
('DATA-RAM-2023',    'Ramadan Data Bundle 2023',      'seasonal',  15.00, '2023-03-10', '2023-04-21', 3, NULL, 'Promo Ramadan Data Bundle — 15% + data nocturne x2',        'plan_promo_2023.pdf'),
('DATA-EID-A-2023',  'Aïd el-Adha Data Bundle 2023', 'seasonal',  10.00, '2023-06-20', '2023-07-10', 3, NULL, 'Promo Eid el-Adha Data Bundle 10%',                         'plan_promo_2023.pdf'),
('DATA-REN-2023',    'Rentrée Data Bundle 2023',      'seasonal',  15.00, '2023-08-20', '2023-09-30', 3, NULL, 'Pack Rentrée Data Bundle Étudiant — 15% + bonus 5Go',        'plan_promo_2023.pdf'),
('DATA-RAM-2024',    'Ramadan Data Bundle 2024',      'seasonal',  20.00, '2024-03-01', '2024-04-09', 3, NULL, 'Promo Ramadan Data Bundle 2024 — 20% + data nocturne',      'plan_promo_2024.pdf'),
('DATA-ETE-2024',    'Été Data Bundle 2024',          'seasonal',  15.00, '2024-06-15', '2024-08-15', 3, NULL, 'Promo Été Data Bundle — 15% + roaming Maghreb offert',      'plan_promo_2024.pdf'),
('DATA-RAM-2025',    'Ramadan Data Bundle 2025',      'seasonal',  25.00, '2025-02-20', '2025-03-30', 3, NULL, 'Promo Ramadan Data Bundle 2025 — 25% + streaming HD inclus','plan_promo_2025.pdf'),

-- ─── VOD 2023-2025 ───────────────────────────────────────────────────────────
('VOD-RAM-2023',     'Ramadan VOD 2023',              'seasonal',  30.00, '2023-03-10', '2023-04-21', 4, NULL, 'Promo Ramadan VOD — 30% abonnement + catalogue spécial',    'plan_promo_2023.pdf'),
('VOD-ETE-2023',     'Été VOD 2023',                  'seasonal',  40.00, '2023-06-15', '2023-08-15', 4, NULL, 'Pack Été VOD — 40% + films exclusifs saison',               'plan_promo_2023.pdf'),
('VOD-RAM-2024',     'Ramadan VOD 2024',              'seasonal',  30.00, '2024-03-01', '2024-04-09', 4, NULL, 'Promo Ramadan VOD 2024 — 30% + séries arabes exclusives',   'plan_promo_2024.pdf'),
('VOD-ETE-2024',     'Été VOD 2024',                  'seasonal',  40.00, '2024-06-15', '2024-08-15', 4, NULL, 'Pack Été VOD 2024 — 40% + max 4 écrans simultanés',         'plan_promo_2024.pdf'),
('VOD-EID-A-2024',   'Aïd el-Adha VOD 2024',         'seasonal',  25.00, '2024-06-16', '2024-07-05', 4, NULL, 'Promo Eid el-Adha VOD 2024 — 25% + contenu famille',        'plan_promo_2024.pdf'),
('VOD-RAM-2025',     'Ramadan VOD 2025',              'seasonal',  35.00, '2025-02-20', '2025-03-30', 4, NULL, 'Promo Ramadan VOD 2025 — 35% + catalogue Ramadan exclusif', 'plan_promo_2025.pdf'),
('VOD-ETE-2025',     'Été VOD 2025',                  'seasonal',  40.00, '2025-06-15', '2025-08-15', 4, NULL, 'Pack Été VOD 2025 — 40% + streaming 4K inclus',             'plan_promo_2025.pdf')

ON CONFLICT (promo_code) DO NOTHING;
