# Stock Forecasting Implementation Summary

## What Was Delivered

### Complete Stock Forecasting Pipeline

A parallel inventory forecasting system built on the existing sales forecasting infrastructure, with new backend services, API routes, and frontend UI components.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  FRONTEND (React/Next.js)                                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  Dashboard (dashboard.tsx)                                       │
│  ├─ Data Ingestion  ──→ Auto-detect file_type ──→ "stock" or   │
│  ├─ Sales Forecasting (existing)                 "sales"        │
│  └─ Stock Forecasting (NEW) ────────┐                          │
│                                      │                           │
│  Stock Forecasting UI (stock-forecasting.tsx)   │                │
│  ├─ Config: horizon [1-12], granularity [m/d]   │               │
│  ├─ Model selection: 6 classic + 2 generative   │               │
│  ├─ Train button → POST /api/inventory/training │               │
│  ├─ Results table: [model, MAPE, RMSE, score]   │               │
│  ├─ Forecast button → POST /api/inventory/forecast              │
│  └─ Chart: historical + forecast + bounds       │               │
│                                      │                           │
└──────────────────────────┬───────────────────────────────────────┘
                           │ HTTP REST
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  BACKEND (FastAPI)                                              │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  API Routes (inventory.py)                    [NEW]             │
│  ├─ POST /api/inventory/training                                │
│  ├─ POST /api/inventory/forecast                                │
│  └─ GET /api/inventory/training/{id}                            │
│      │                                                            │
│      ├─ Validates session (SessionManager)                      │
│      ├─ Calls forecasting service                               │
│      └─ Caches results                                          │
│                      │                                           │
│                      ▼                                           │
│  Inventory Forecasting Service (inventory_forecasting_service.py) [NEW]
│  │                                                              │
│  ├─ load_inventory_history()                                    │
│  │  └─ SELECT STOCK_START_OF_PERIOD from landing_zone.stock_data
│  │     GROUP BY (YEAR_MONTH, PRODUCT_FAMILY)                   │
│  │     SUM aggregation                                          │
│  │                                                              │
│  ├─ train_inventory_models()                                    │
│  │  ├─ Aggregate to global total                               │
│  │  ├─ Time-series split: 80% train, 20% test                 │
│  │  ├─ Train in parallel:                                      │
│  │  │  ├─ Classic: SARIMA, Prophet, XGBoost, Linear, ExpSmooth├─ Reuses
│  │  │  │           Naive (via forecasting_service._run_forecast│ existing
│  │  │  └─ Generative: Chronos, TimesFM (via Ollama)           │ training
│  │  ├─ Compute metrics: MAE, RMSE, MAPE, SMAPE, MPE           │ utilities
│  │  ├─ Weighted score: 0.35*MAPE + 0.25*RMSE + ...            │
│  │  └─ Rank by score (lower = better)                          │
│  │                                                              │
│  └─ generate_inventory_forecast()                               │
│     ├─ Retrieve best model from session                        │
│     ├─ Retrain on full history                                 │
│     ├─ Generate predictions                                    │
│     ├─ Add confidence intervals: pred ± 1.28*σ                 │
│     └─ Compute trend indicator                                 │
│      │                                                            │
│      └─ Return: {historical, forecast, metadata}               │
│                                                                   │
│  ETL Service (etl_service.py)  [MODIFIED]                       │
│  └─ detect_file_type()                                          │
│     ├─ [NEW] Check stock signature: YEAR_MONTH +               │
│     │   PRODUCT_FAMILY + STOCK_START_OF_PERIOD                 │
│     ├─ Fallback: old heuristics                                │
│     └─ Return: "stock" | "sales" | "promotion"                 │
│                                                                   │
└─────────────────────────────┬──────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  DATA LAYER                                                     │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  PostgreSQL                                                     │
│  └─ landing_zone.stock_data                                    │
│     ├─ DEALER_ID, COD_PROD, PRODUCT_NAME, PRODUCT_FAMILY      │
│     ├─ YEAR_MONTH, STOCK_START_OF_PERIOD                       │
│     ├─ CURRENT_STOCK_QTY, INVENTORY_QTY                        │
│     └─ [... other columns]                                     │
│                                                                   │
│  In-Memory Caching (SessionManager)                            │
│  ├─ Upload sessions                                            │
│  ├─ Training jobs + results                                    │
│  └─ Forecast cache (forecast:inventory)                        │
│                                                                   │
└─────────────────────────────────────────────────────────────────┘
```

## Key Design Decisions

### 1. Parallel Pipeline Architecture
- **Motivation**: Reuse sales forecasting code without coupling
- **Implementation**: Separate loaders, same training utilities
- **Benefit**: Easy to maintain, extend, or disable stock forecasting

### 2. Weighted Multi-Metric Model Selection
- **Weights**: MAPE 35% + RMSE 25% + MAE 20% + SMAPE 15% + Bias 5%
- **Rationale**: Different metrics capture different forecast quality aspects
  - MAPE: Percentage errors (% of actual)
  - RMSE: Penalizes large errors
  - MAE: Interpretable in original units
  - SMAPE: Symmetric, doesn't punish over/under equally
  - Bias: Systematic directional error
- **Normalization**: Each metric scaled to 0-100 before weighting

### 3. Temporal Granularity
- **Default**: Monthly (required for 6-month horizon)
- **Optional**: Daily (experimental, requires more data)
- **Aggregation**: SUM by (DATE_TRUNC, PRODUCT_FAMILY)

### 4. CSV Detection Strategy
- **Stock Signature**: YEAR_MONTH + PRODUCT_FAMILY + STOCK_START_OF_PERIOD (all required)
- **Rationale**: Minimal viable set of columns to identify stock data
- **Fallback**: Existing heuristics (STOCK_QTY, WAREHOUSE, etc.)
- **Override**: User can force `?service_type=stock`

### 5. Forecast Methodology
- **Target**: STOCK_START_OF_PERIOD (inventory at period start)
- **Horizon**: 6 months (configurable 1-12 months)
- **Confidence**: 1.28σ bounds (≈80% confidence)
- **Trend**: Absolute % change from last historical to last forecast

## Files Changed

### Backend
| File | Action | Key Changes |
|------|--------|-------------|
| `app/services/inventory_forecasting_service.py` | **CREATE** | New service with load/aggregate/train/forecast functions |
| `app/api/inventory.py` | **CREATE** | New API routes with Pydantic request/response schemas |
| `app/main.py` | **MODIFY** | Import + register inventory router |
| `app/services/etl_service.py` | **MODIFY** | Enhanced `detect_file_type()` with stock signature check |

### Frontend
| File | Action | Key Changes |
|------|--------|-------------|
| `components/sections/stock-forecasting.tsx` | **CREATE** | Full stock forecasting UI component |
| `components/dashboard.tsx` | **MODIFY** | Added Stock tab, session state, auto-switch logic |
| `components/sections/data-ingestion.tsx` | **MODIFY** | Added callback prop, file_type emission |

## Testing Readiness

### ✅ Code Quality
- Type annotations on all functions
- Docstrings on all public functions
- Error handling with logging
- Follows existing patterns

### ✅ Python Validation
- `inventory_forecasting_service.py`: No syntax errors (Pylance)
- `inventory.py`: No syntax errors (Pylance)

### ✅ TypeScript Validation
- `stock-forecasting.tsx`: Valid React (imports, hooks, handlers)
- `dashboard.tsx`: Updated navigation + state
- `data-ingestion.tsx`: Callback integration

### 📋 Pending
- Docker rebuild + start (shell currently has issues)
- Backend integration tests
- Frontend E2E testing
- Performance benchmarks

## Usage Example

### 1. Upload Stock CSV
```
User uploads: "inventory_2024.csv"
├─ Columns detected: YEAR_MONTH, PRODUCT_FAMILY, STOCK_START_OF_PERIOD
├─ File type: "stock"
└─ Session created: "sess-abc123"
```

### 2. Configure & Train
```
UI shows Stock Forecasting tab
├─ Set horizon: 6 months (default)
├─ Select models: All (default)
└─ Click "Train Models"

Backend:
├─ Loads 12 months history (min)
├─ Trains 8 models (6 classic + 2 generative)
├─ Computes weighted scores
└─ Returns ranked results + best model
```

### 3. Generate & View Forecast
```
UI shows results table
├─ Best model: prophet (MAPE: 12.3%, score: 0.1234)
└─ Click "Generate Forecast"

Backend:
├─ Retrains prophet on full history
├─ Generates 6 monthly predictions
├─ Adds confidence bounds
└─ Calculates trend: +4.5% (rising inventory)

Frontend:
├─ Renders line chart
├─ Historical: blue line
├─ Forecast: red line  
├─ Bounds: purple dashed
└─ Metadata: "↑ Increasing (4.5%)"
```

## API Endpoints

### Train Models
```
POST /api/inventory/training

Request:
{
  "session_id": "string",
  "horizon": 1-12 (default: 6),
  "models": ["all"] or specific names,
  "enable_generative": boolean (default: true),
  "granularity": "monthly" | "daily" (default: "monthly")
}

Response:
{
  "training_id": "uuid",
  "models_trained": number,
  "best_model": "string (model name)",
  "results": [
    {
      "model": "string",
      "mae": number,
      "rmse": number,
      "mape": number,
      "smape": number,
      "mpe": number,
      "score": number,
      "training_time_sec": number,
      "yhat": [numbers] (predictions on test set)
    }
  ]
}
```

### Generate Forecast
```
POST /api/inventory/forecast

Request:
{
  "session_id": "string",
  "model": "string (model name)",
  "horizon": 1-12 (default: 6),
  "granularity": "monthly" | "daily" (default: "monthly")
}

Response:
{
  "historical": [
    { "date": "YYYY-MM-DD", "value": number },
    ...
  ],
  "forecast": [
    {
      "date": "YYYY-MM-DD",
      "value": number,
      "lower_bound": number,
      "upper_bound": number
    },
    ...
  ],
  "metadata": {
    "model_used": "string",
    "trend": "hausse" | "baisse",
    "change_pct": number
  }
}
```

## Next Steps (For User)

1. **Rebuild Docker**: `docker compose down && docker compose build --no-cache && docker compose up -d`
2. **Test Backend**: Upload stock CSV, POST to `/api/inventory/training`, verify response
3. **Test Frontend**: Navigate to http://localhost:3000, check Stock Forecasting tab
4. **Run E2E**: Full flow from upload → train → forecast
5. **Tune Weights**: Adjust METRIC_WEIGHTS in `inventory_forecasting_service.py` if desired
6. **Add Features**: Per-family forecasts, additional metrics, async training, etc.

## Support

For detailed testing guide, see: [STOCK_FORECASTING_GUIDE.md](./STOCK_FORECASTING_GUIDE.md)

