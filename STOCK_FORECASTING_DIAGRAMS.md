# Stock Forecasting Data Flow Diagrams

## 1. CSV Upload → Session Creation → UI Auto-Switch

```
┌─────────────────────┐
│   User Upload       │
│ "inventory_2024.csv"│
└──────────┬──────────┘
           │
           ▼
┌─────────────────────────────────────┐
│  POST /api/upload                   │
│  (existing endpoint)                 │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│  etl_service.read_tabular_file()    │
│  & normalize_column_names()          │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│  etl_service.detect_file_type()     │
│  NEW: Check stock signature:        │
│  - YEAR_MONTH ✓        │
│  - PRODUCT_FAMILY ✓    │
│  - STOCK_START_OF_PERIOD ✓ │
└──────────────┬──────────────────────┘
               │
     ┌─────────┴────────────┐
     │                      │
     ▼                      ▼
file_type="stock"    file_type="sales"
     │                      │
     ▼                      ▼
etl_service._ingest_stock()     etl_service._ingest_sales()
     │                      │
     └─────────┬────────────┘
               │
               ▼
┌──────────────────────────────────────┐
│  landing_zone.stock_data            │
│  (or fact_ventes for sales)          │
│  INSERT rows                         │
└──────────────┬─────────────────────┐
               │
               ▼
┌──────────────────────────────────────┐
│  ETL Response                        │
│  {                                   │
│    "session_id": "sess-abc123",     │
│    "file_type": "stock",            │
│    "rows": 1234,                    │
│    "filename": "inventory_2024.csv" │
│  }                                   │
└──────────────┬──────────────────────┘
               │
               ▼
┌──────────────────────────────────────┐
│  Frontend (onSessionCreated callback)│
│  - Store sessionId: "sess-abc123"   │
│  - Detect fileType: "stock"         │
│  - Auto-switch to Stock Forecasting │
└──────────────────────────────────────┘
```

## 2. Training Flow

```
┌────────────────────────────────────────────┐
│ POST /api/inventory/training               │
│ {                                          │
│   "session_id": "sess-abc123",            │
│   "horizon": 6,                           │
│   "models": ["all"],                      │
│   "enable_generative": true,              │
│   "granularity": "monthly"                │
│ }                                          │
└───────────┬────────────────────────────────┘
            │
            ▼
┌────────────────────────────────────────────┐
│ SessionManager.get_upload_session()        │
│ Validate session exists                    │
└───────────┬────────────────────────────────┘
            │
            ▼
┌────────────────────────────────────────────┐
│ SessionManager.create_training_job()       │
│ Status: "running"                          │
└───────────┬────────────────────────────────┘
            │
            ▼
┌────────────────────────────────────────────────────────────────┐
│ inventory_forecasting_service.train_inventory_models()         │
└────────────┬──────────────────────────────────────────────────┘
             │
             ▼
┌────────────────────────────┐
│ load_inventory_history()   │
│                            │
│ SELECT STOCK_START_OF...   │
│ FROM landing_zone.stock_data
│ GROUP BY (YEAR_MONTH,     │
│          PRODUCT_FAMILY)  │
│ ORDER BY date              │
└────────────┬───────────────┘
             │
             ▼
┌──────────────────────────┐
│ _aggregate_global_...() │
│                         │
│ Global: SUM all stock   │
│ per date                │
│                         │
│ Families: per-family    │
│ aggregates              │
│                         │
│ Fill gaps: monthly freq │
└────────────┬────────────┘
             │
             ▼
┌──────────────────────┐
│ Time-Series Split   │
│                     │
│ 80% train           │
│ 20% test            │
│ (last 1-6 samples)  │
└────────────┬────────┘
             │
             ├───┬──────┬──────┬──────┬──────┬──────┬─────┬─────┐
             │   │      │      │      │      │      │     │     │
             ▼   ▼      ▼      ▼      ▼      ▼      ▼     ▼     ▼
         SARIMA Prophet XGBoost Linear ExpSmooth Naive Chronos TimesFM
             │   │      │      │      │      │      │     │     │
             └───┴──────┴──────┴──────┴──────┴──────┴─────┴─────┘
                         │
                         ▼
            ┌─────────────────────────────────┐
            │ For each model:                 │
            │                                 │
            │ 1. Train on X_train             │
            │ 2. Predict X_test (horizon pts)│
            │ 3. Compute metrics:             │
            │    └─ MAE: mean absolute error│
            │    └─ RMSE: root mean sq error │
            │    └─ MAPE: mean absolute %err │
            │    └─ SMAPE: symmetric mean %e │
            │    └─ MPE: mean percentage err │
            │ 4. Calculate weighted score:    │
            │    score = 0.35*MAPE_normalized│
            │           + 0.25*RMSE_norm     │
            │           + 0.20*MAE_norm      │
            │           + 0.15*SMAPE_norm    │
            │           + 0.05*|MPE|_norm    │
            └─────────────────────────────────┘
                         │
                         ▼
            ┌────────────────────────────┐
            │ Sort results by score       │
            │ (lower = better)            │
            │                            │
            │ [1] prophet: 0.1234        │
            │ [2] xgboost: 0.1456        │
            │ [3] sarima: 0.1678         │
            │ ...                        │
            └────────────┬───────────────┘
                         │
                         ▼
            ┌──────────────────────────────┐
            │ SessionManager.update_...()  │
            │ - best_model = "prophet"    │
            │ - training_results = [...]  │
            │ - status = "completed"      │
            └────────────┬─────────────────┘
                         │
                         ▼
            ┌──────────────────────────────┐
            │ HTTP Response                │
            │ {                            │
            │   "training_id": "...",     │
            │   "models_trained": 8,      │
            │   "best_model": "prophet",  │
            │   "results": [              │
            │     {model, mae, rmse, ...},│
            │     ...                     │
            │   ]                         │
            │ }                            │
            └──────────────────────────────┘
```

## 3. Forecast Generation Flow

```
┌──────────────────────────────────────────────┐
│ POST /api/inventory/forecast                 │
│ {                                            │
│   "session_id": "sess-abc123",              │
│   "model": "prophet",                       │
│   "horizon": 6,                             │
│   "granularity": "monthly"                  │
│ }                                            │
└──────────────┬───────────────────────────────┘
               │
               ▼
┌────────────────────────────────────────────┐
│ Check cache:                               │
│ SessionManager.get_cached_forecast()       │
│ Key: "sess-abc123:forecast:inventory"      │
└────────────────────┬──────────────────────┘
                     │
         ┌───────────┴───────────┐
         │                       │
    Cache HIT              Cache MISS
         │                       │
         ▼                       ▼
    Return cached       Proceed to generate
    forecast            new forecast
                              │
                              ▼
                ┌──────────────────────────────────┐
                │ load_inventory_history()         │
                │ [same as training, full history] │
                └──────────────────────────────────┘
                              │
                              ▼
                ┌──────────────────────────────────┐
                │ _aggregate_global_and_families() │
                │ [aggregate all data]             │
                └──────────────┬───────────────────┘
                               │
                               ▼
                ┌──────────────────────────────────┐
                │ Retrain best model on full       │
                │ history (prophet in this case)   │
                └──────────────┬───────────────────┘
                               │
                               ▼
                ┌──────────────────────────────────┐
                │ Generate horizon=6 predictions   │
                │ Each with 1 forecast value       │
                │ (monthly predictions)            │
                │                                  │
                │ [pred_1, pred_2, ..., pred_6]   │
                └──────────────┬───────────────────┘
                               │
                               ▼
                ┌──────────────────────────────────┐
                │ Compute confidence intervals     │
                │ σ = std(y_train)                │
                │                                  │
                │ For each pred_i:                 │
                │   lower_i = max(0, pred - 1.28σ)│
                │   upper_i = pred + 1.28σ        │
                └──────────────┬───────────────────┘
                               │
                               ▼
                ┌──────────────────────────────────┐
                │ Format output                    │
                │                                  │
                │ historical: [                   │
                │   {date, value},                │
                │   ...                           │
                │ ]                               │
                │                                  │
                │ forecast: [                     │
                │   {date, value, lower, upper},  │
                │   ...                           │
                │ ]                               │
                │                                  │
                │ metadata: {                     │
                │   trend: "hausse"/"baisse",    │
                │   change_pct: 4.5               │
                │ }                               │
                └──────────────┬───────────────────┘
                               │
                               ▼
                ┌──────────────────────────────────┐
                │ SessionManager.cache_forecast() │
                │ Store: {session, type, results} │
                └──────────────┬───────────────────┘
                               │
                               ▼
                ┌──────────────────────────────────┐
                │ HTTP Response                    │
                │ {                               │
                │   "historical": [...],          │
                │   "forecast": [...],            │
                │   "metadata": {...}             │
                │ }                               │
                └──────────────────────────────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │ Frontend Chart      │
                    │                     │
                    │ Blue:   historical  │
                    │ Red:    forecast    │
                    │ Purple: bounds      │
                    │                     │
                    │ Trend: ↑ (+4.5%)   │
                    └─────────────────────┘
```

## 4. Database Schema (View Used)

```
PostgreSQL Schema:

landing_zone.stock_data (table)
├─ dealer_id (varchar)
├─ cod_prod (varchar)
├─ product_name (varchar)
├─ product_family (varchar) ◄─── AGGREGATION KEY
├─ year_month (varchar/date) ◄─── TEMPORAL KEY
├─ stock_start_of_period (numeric) ◄─── TARGET VARIABLE
├─ current_stock_qty (numeric)
├─ inventory_qty (numeric)
└─ [other columns...]

Aggregation SQL:
┌──────────────────────────────────────────────┐
│ SELECT                                       │
│   DATE_TRUNC('month', TO_DATE(...)) AS date,│
│   COALESCE(PRODUCT_FAMILY, 'UNKNOWN'),      │
│   SUM(STOCK_START_OF_PERIOD) AS stock_value │
│ FROM landing_zone.stock_data                │
│ WHERE STOCK_START_OF_PERIOD IS NOT NULL     │
│ GROUP BY 1, 2                               │
│ ORDER BY date                               │
└──────────────────────────────────────────────┘

Aggregation result:
┌──────────┬──────────────────┬─────────────┐
│ date     │ product_family   │ stock_value │
├──────────┼──────────────────┼─────────────┤
│ 2024-01  │ Family X         │ 15000       │
│ 2024-01  │ Family Y         │ 8000        │
│ 2024-01  │ Family Z         │ 5000        │
│ 2024-02  │ Family X         │ 15500       │
│ ...      │ ...              │ ...         │
└──────────┴──────────────────┴─────────────┘

Global aggregation (summed across families):
┌──────────┬─────────────┐
│ date     │ stock_value │
├──────────┼─────────────┤
│ 2024-01  │ 28000       │
│ 2024-02  │ 29100       │
│ ...      │ ...         │
└──────────┴─────────────┘
```

## 5. State Flow in SessionManager

```
Session Lifecycle:

1. UPLOAD PHASE
   ┌─────────────────────────────────────────┐
   │ SessionManager.create_upload_session()  │
   │                                         │
   │ Sessions = {                            │
   │   "sess-abc": {                         │
   │     "source_file": "inventory.csv",     │
   │     "uploaded_at": datetime,            │
   │     "rows": 1234,                       │
   │     "service": "FIBRE" (optional)       │
   │   }                                     │
   │ }                                       │
   └─────────────────────────────────────────┘

2. TRAINING PHASE
   ┌─────────────────────────────────────────┐
   │ SessionManager.create_training_job()    │
   │                                         │
   │ TrainingJobs = {                        │
   │   "train-xyz": {                        │
   │     "session_id": "sess-abc",           │
   │     "status": "running",                │
   │     "started_at": datetime,             │
   │     "results": null,                    │
   │     "error": null                       │
   │   }                                     │
   │ }                                       │
   └─────────────────────────────────────────┘
                   │
                   ▼
   ┌─────────────────────────────────────────┐
   │ SessionManager.update_training_status() │
   │                                         │
   │ TrainingJobs = {                        │
   │   "train-xyz": {                        │
   │     ...                                 │
   │     "status": "completed",              │
   │     "results": [                        │
   │       {model: "prophet", ...},          │
   │       ...                               │
   │     ],                                  │
   │     "completed_at": datetime            │
   │   }                                     │
   │ }                                       │
   └─────────────────────────────────────────┘

3. MODEL SELECTION PHASE
   ┌─────────────────────────────────────────┐
   │ SessionManager.update_best_model()      │
   │                                         │
   │ Sessions = {                            │
   │   "sess-abc": {                         │
   │     ...                                 │
   │     "best_model": "prophet",            │
   │     "training_id": "train-xyz"          │
   │   }                                     │
   │ }                                       │
   └─────────────────────────────────────────┘

4. FORECAST PHASE
   ┌─────────────────────────────────────────┐
   │ SessionManager.cache_forecast()         │
   │                                         │
   │ ForecastCache = {                       │
   │   "sess-abc:forecast:inventory": {      │
   │     "historical": [...],                │
   │     "forecast": [...],                  │
   │     "metadata": {...},                  │
   │     "cached_at": datetime               │
   │   }                                     │
   │ }                                       │
   └─────────────────────────────────────────┘

RETRIEVAL:
   SessionManager.get_cached_forecast()
   → Returns cached forecast or None
```

## 6. Metric Computation Pipeline

```
For each model after prediction:

1. Predictions Array
   ┌────────────────┐
   │ y_true: [5000, 5200, 5100, 5300, 5400, 5600]
   │ y_pred: [5050, 5150, 5080, 5350, 5450, 5620]
   └────────────────┘

2. Compute 5 Metrics
   │
   ├─ MAE (Mean Absolute Error)
   │  MAE = mean(|y_pred - y_true|)
   │      = mean([50, 50, 20, 50, 50, 20])
   │      = 40.0
   │
   ├─ RMSE (Root Mean Square Error)
   │  RMSE = sqrt(mean((y_pred - y_true)²))
   │       = sqrt(mean([2500, 2500, 400, ...]))
   │       = 41.25
   │
   ├─ MAPE (Mean Absolute Percentage Error)
   │  MAPE = 100 * mean(|y_pred - y_true| / |y_true|)
   │       = 100 * mean([0.010, 0.010, 0.004, ...])
   │       = 0.82 (%)
   │
   ├─ SMAPE (Symmetric Mean Absolute Percentage Error)
   │  SMAPE = 100 * mean(2|y_pred - y_true| / (|y_pred| + |y_true|))
   │        = 100 * mean(0.010, ...)
   │        = 0.77 (%)
   │
   └─ MPE (Mean Percentage Error) — can be negative
      MPE = 100 * mean((y_pred - y_true) / y_true)
          = 100 * mean([+0.010, -0.010, ...])
          = +0.50 (%)  ◄─ Bias indicator

3. Normalize to 0-100 scale
   │
   ├─ MAPE_norm = min(100, MAPE) / 100 = 0.0082
   ├─ RMSE_norm = min(100, RMSE) / 100 = 0.4125
   ├─ MAE_norm = min(100, MAE) / 100 = 0.40
   ├─ SMAPE_norm = min(100, SMAPE) / 100 = 0.0077
   └─ MPE_norm = min(100, |MPE|) / 100 = 0.0050

4. Compute Weighted Score
   │
   score = 0.35 * MAPE_norm
         + 0.25 * RMSE_norm
         + 0.20 * MAE_norm
         + 0.15 * SMAPE_norm
         + 0.05 * MPE_norm
   
   score = 0.35 * 0.0082
         + 0.25 * 0.4125
         + 0.20 * 0.40
         + 0.15 * 0.0077
         + 0.05 * 0.0050
   
   score = 0.00287
         + 0.10313
         + 0.0800
         + 0.00116
         + 0.00025
   
   score = 0.1873  ◄─ FINAL SCORE (lower = better)

5. Return Result Object
   │
   {
     "model": "prophet",
     "mae": 40.0,
     "rmse": 41.25,
     "mape": 0.82,
     "smape": 0.77,
     "mpe": 0.50,
     "score": 0.1873,
     "training_time_sec": 2.34,
     "yhat": [5050, 5150, 5080, 5350, 5450, 5620]
   }
```

---

**Generated**: Stock Forecasting Implementation Documentation
**Version**: 1.0
**Status**: Complete ✅

