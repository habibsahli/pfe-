-- =============================================================================
-- SEED 03 — public.fact_promotions  (simulation training data)
-- 55 rows covering past campaigns with measured uplift outcomes.
-- These rows are what the what-if simulation's _query_historical_uplift()
-- uses to return real measured uplifts instead of the generic elasticity table.
--
-- Uplift rates reflect:
--   - Channel: boutique > online > app > call_center > partenaire
--   - Event:   ramadan/eid > rentree > fete_nationale > ete > nouvel_an
--   - Discount: higher discount → higher uplift (non-linear)
--   - Service:  5G highest elasticity, VOD very high, DATA moderate, FIBRE lower
-- =============================================================================

INSERT INTO public.fact_promotions
  (service_type, region, channel, discount_percent, promo_start, promo_end,
   event_type, actual_uplift_percent, units_sold_during,
   baseline_units_expected, notes)
VALUES

-- ══════════════════════════════════════════════════════════════════
-- FIBRE — Ramadan campaigns
-- ══════════════════════════════════════════════════════════════════
('FIBRE', 'Tunis',   'boutique',    20.00, '2023-03-10', '2023-04-21', 'ramadan',          28.5,  456,  354,  'Ramadan 2023 — WiFi extender offert, très forte conversion boutique'),
('FIBRE', 'Ariana',  'boutique',    20.00, '2023-03-10', '2023-04-21', 'ramadan',          25.2,  189,  151,  'Ramadan 2023 Ariana — affluence supérieure à la cible'),
('FIBRE', 'Tunis',   'online',      20.00, '2023-03-10', '2023-04-21', 'ramadan',          19.8,  317,  264,  'Ramadan 2023 online — conversion nocturne forte (21h-1h)'),
('FIBRE', 'Tunis',   'boutique',    25.00, '2024-03-01', '2024-04-09', 'ramadan',          34.1,  612,  456,  'Ramadan 2024 — installation offerte, record de ventes mensuel'),
('FIBRE', 'Sfax',    'boutique',    25.00, '2024-03-01', '2024-04-09', 'ramadan',          30.8,  243,  186,  'Ramadan 2024 Sfax — déploiement réseau récent, forte demande latente'),
('FIBRE', 'Sousse',  'boutique',    25.00, '2024-03-01', '2024-04-09', 'ramadan',          28.4,  198,  154,  'Ramadan 2024 Sousse — zone touristique, bonnes performances'),
('FIBRE', 'Tunis',   'online',      25.00, '2024-03-01', '2024-04-09', 'ramadan',          22.3,  389,  318,  'Ramadan 2024 online — pic commandes 22h-02h notable'),
('FIBRE', NULL,      'call_center', 25.00, '2024-03-01', '2024-04-09', 'ramadan',          14.6,  178,  155,  'Ramadan 2024 call center — taux transformation stable'),
('FIBRE', 'Tunis',   'boutique',    30.00, '2025-02-20', '2025-03-30', 'ramadan',          38.7,  734,  529,  'Ramadan 2025 — TV incluse, meilleur résultat historique FIBRE'),
('FIBRE', 'Ariana',  'online',      30.00, '2025-02-20', '2025-03-30', 'ramadan',          29.5,  356,  275,  'Ramadan 2025 online Ariana — campagne emailing segmentée efficace'),

-- ══════════════════════════════════════════════════════════════════
-- FIBRE — Eid el-Fitr
-- ══════════════════════════════════════════════════════════════════
('FIBRE', 'Tunis',   'boutique',    15.00, '2023-04-22', '2023-05-05', 'eid_fitr',         18.9,  276,  232,  'Eid el-Fitr 2023 — période courte, bonne conversion'),
('FIBRE', 'Tunis',   'boutique',    15.00, '2024-04-10', '2024-04-30', 'eid_fitr',         21.4,  318,  262,  'Eid el-Fitr 2024 — frais installation offerts, bonnes perfs'),
('FIBRE', NULL,      'online',      15.00, '2024-04-10', '2024-04-30', 'eid_fitr',         15.6,  234,  202,  'Eid el-Fitr 2024 online — résultats conformes aux attentes'),
('FIBRE', 'Tunis',   'boutique',    20.00, '2025-03-31', '2025-04-15', 'eid_fitr',         27.8,  445,  348,  'Eid el-Fitr 2025 — Wi-Fi 6 offert, forte valeur perçue'),

-- ══════════════════════════════════════════════════════════════════
-- FIBRE — Rentrée scolaire
-- ══════════════════════════════════════════════════════════════════
('FIBRE', 'Tunis',   'boutique',    15.00, '2023-08-20', '2023-09-30', 'rentree_scolaire', 22.3,  345,  282,  'Rentrée 2023 — pack famille fort taux transformation'),
('FIBRE', 'Tunis',   'online',      15.00, '2023-08-20', '2023-09-30', 'rentree_scolaire', 16.8,  259,  222,  'Rentrée 2023 online — SEA performant sur mots-clés rentrée'),
('FIBRE', 'Tunis',   'boutique',    20.00, '2024-08-20', '2024-09-30', 'rentree_scolaire', 28.5,  512,  398,  'Rentrée 2024 — 2 mois offerts sur 12, forte demande famille'),
('FIBRE', 'Sfax',    'boutique',    20.00, '2024-08-20', '2024-09-30', 'rentree_scolaire', 25.1,  198,  158,  'Rentrée 2024 Sfax — marché dynamique, objectif dépassé'),
('FIBRE', 'Ariana',  'app',         20.00, '2024-08-20', '2024-09-30', 'rentree_scolaire', 18.4,  145,  122,  'Rentrée 2024 app — push notification ciblé étudiants'),
('FIBRE', 'Tunis',   'boutique',    20.00, '2025-08-20', '2025-09-30', 'rentree_scolaire', 26.9,  489,  385,  'Rentrée 2025 — mois offert, résultats légèrement sous objectif'),

-- ══════════════════════════════════════════════════════════════════
-- FIBRE — Été & Fête Nationale
-- ══════════════════════════════════════════════════════════════════
('FIBRE', 'Sousse',  'boutique',    10.00, '2023-06-15', '2023-08-15', 'ete',               12.4,  145,  129,  'Été 2023 Sousse — zone balnéaire, résidences secondaires'),
('FIBRE', 'Tunis',   'boutique',    10.00, '2024-06-15', '2024-08-15', 'ete',               11.8,  278,  248,  'Été 2024 — décodeur TV inclus, uplift modéré'),
('FIBRE', 'Tunis',   'boutique',    20.00, '2024-07-20', '2024-08-05', 'fete_nationale',    24.6,  312,  250,  'Fête Nationale 2024 — 25 juillet, momentum fort 3 jours'),
('FIBRE', 'Tunis',   'boutique',    15.00, '2025-07-20', '2025-08-05', 'fete_nationale',    20.3,  289,  240,  'Fête Nationale 2025 — résultats conformes aux projections'),

-- ══════════════════════════════════════════════════════════════════
-- 5G — all channels and events
-- ══════════════════════════════════════════════════════════════════
('5G',    'Sfax',    'boutique',    40.00, '2023-10-01', '2023-11-30', NULL,                52.8,  189,  124,  'Lancement 5G Sfax — demande latente forte, dépassement objectif x1.5'),
('5G',    'Tunis',   'boutique',    25.00, '2024-03-01', '2024-04-09', 'ramadan',           41.3,  312,  221,  'Ramadan 5G 2024 — CPE promotionnel, activation immédiate'),
('5G',    'Tunis',   'online',      25.00, '2024-03-01', '2024-04-09', 'ramadan',           32.7,  245,  185,  'Ramadan 5G 2024 online — forte conversion panier moyen élevé'),
('5G',    'Sousse',  'boutique',    25.00, '2024-08-20', '2024-09-30', 'rentree_scolaire',  38.5,  178,  128,  'Rentrée 5G 2024 Sousse — smartphone offert, pic J1 et J2'),
('5G',    NULL,      'partenaire',  25.00, '2024-08-20', '2024-09-30', 'rentree_scolaire',  22.4,  134,  109,  'Rentrée 5G 2024 partenaires — réseau distribution limité'),
('5G',    'Tunis',   'boutique',    30.00, '2025-02-20', '2025-03-30', 'ramadan',           48.6,  534,  359,  'Ramadan 5G 2025 — CPE haut de gamme inclus, meilleur lancement'),
('5G',    'Sfax',    'boutique',    30.00, '2025-02-20', '2025-03-30', 'ramadan',           44.2,  312,  216,  'Ramadan 5G 2025 Sfax — région leader en taux de conversion'),
('5G',    'Tunis',   'app',         30.00, '2025-02-20', '2025-03-30', 'ramadan',           28.9,  198,  154,  'Ramadan 5G 2025 app — tunnel 100% digital, bon taux complétion'),
('5G',    'Tunis',   'boutique',    25.00, '2025-06-15', '2025-08-15', 'ete',               31.5,  289,  220,  'Été 5G 2025 — data illimitée weekend, forte activation jeunes'),
('5G',    'Tunis',   'boutique',    30.00, '2025-08-20', '2025-09-30', 'rentree_scolaire',  45.8,  612,  420,  'Rentrée 5G 2025 — CPE Wi-Fi 6 offert, record de ventes'),
('5G',    'Ariana',  'boutique',    30.00, '2025-08-20', '2025-09-30', 'rentree_scolaire',  42.3,  356,  250,  'Rentrée 5G 2025 Ariana — excellent rapport coût/acquisition'),

-- ══════════════════════════════════════════════════════════════════
-- DATA_BUNDLE — all channels and events
-- ══════════════════════════════════════════════════════════════════
('DATA',  'Tunis',   'boutique',    15.00, '2023-03-10', '2023-04-21', 'ramadan',           21.4,  867,  714,  'Ramadan Data 2023 — data nocturne x2, très forte adoption jeunes'),
('DATA',  'Tunis',   'app',         15.00, '2023-03-10', '2023-04-21', 'ramadan',           18.6,  634,  534,  'Ramadan Data 2023 app — push personnalisé, bon taux ouverture'),
('DATA',  NULL,      'online',      15.00, '2023-08-20', '2023-09-30', 'rentree_scolaire',  19.8,  1245, 1039, 'Rentrée Data 2023 — offre étudiante 5Go bonus, fort volume'),
('DATA',  'Tunis',   'boutique',    20.00, '2024-03-01', '2024-04-09', 'ramadan',           28.3,  1123,  875, 'Ramadan Data 2024 — data nocturne incluse, conversion forte'),
('DATA',  NULL,      'call_center', 20.00, '2024-03-01', '2024-04-09', 'ramadan',           12.5,  456,  405,  'Ramadan Data 2024 call center — campagne outbound segmentée'),
('DATA',  NULL,      'boutique',    15.00, '2024-06-15', '2024-08-15', 'ete',               15.4,  1456, 1262, 'Été Data 2024 — roaming Maghreb offert, demande touristique'),
('DATA',  NULL,      'app',         20.00, '2024-08-20', '2024-09-30', 'rentree_scolaire',  22.6,  1678, 1369, 'Rentrée Data 2024 app — offre étudiante viral sur réseaux sociaux'),
('DATA',  'Tunis',   'boutique',    25.00, '2025-02-20', '2025-03-30', 'ramadan',           33.8,  1534, 1146, 'Ramadan Data 2025 — streaming HD inclus, record mensuel'),
('DATA',  NULL,      'online',      25.00, '2025-02-20', '2025-03-30', 'ramadan',           24.7,  1234,  989, 'Ramadan Data 2025 online — landing page dédiée, bon ROI'),

-- ══════════════════════════════════════════════════════════════════
-- VOD — all channels and events (high elasticity, price-sensitive)
-- ══════════════════════════════════════════════════════════════════
('VOD',   'Tunis',   'boutique',    30.00, '2023-03-10', '2023-04-21', 'ramadan',           48.5,  678,  456,  'Ramadan VOD 2023 — catalogue spécial Ramadan, très forte demande'),
('VOD',   'Tunis',   'online',      30.00, '2023-03-10', '2023-04-21', 'ramadan',           41.2,  534,  378,  'Ramadan VOD 2023 online — SEA pics 20h-23h fort'),
('VOD',   NULL,      'app',         40.00, '2023-06-15', '2023-08-15', 'ete',               58.3,  789,  499,  'Été VOD 2023 app — streaming vacances, fort taux trial → paid'),
('VOD',   'Tunis',   'boutique',    30.00, '2024-03-01', '2024-04-09', 'ramadan',           51.4,  712,  470,  'Ramadan VOD 2024 — séries arabes exclusives, conversion record'),
('VOD',   NULL,      'online',      40.00, '2024-06-15', '2024-08-15', 'ete',               54.8,  634,  410,  'Été VOD 2024 — 4 écrans simultanés, forte adoption famille'),
('VOD',   'Tunis',   'boutique',    35.00, '2025-02-20', '2025-03-30', 'ramadan',           55.9,  823,  527,  'Ramadan VOD 2025 — catalogue Ramadan exclusif, meilleure édition')

ON CONFLICT DO NOTHING;
