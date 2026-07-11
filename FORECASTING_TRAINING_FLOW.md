# Backend Forecasting Training Flow & Architecture

## 1. File Paths of Key Forecasting Services

### Main Training & Forecasting Services
- **[backend/app/services/forecasting_service.py](backend/app/services/forecasting_service.py)** - Core sales forecasting (1400+ lines)
  - `train_models()` - Main training orchestration
  - `_build_features()` - Feature engineering (line 541)
  - `_fill_temporal_gaps()` - Data preprocessing (line 182)
  - Metric calculation functions (lines 99-123)
  
- **[backend/app/services/inventory_forecasting_service.py](backend/app/services/inventory_forecasting_service.py)** - Stock forecasting
  - `train_inventory_models()` - Inventory training (line 196)
  - `generate_inventory_forecast()` - Generates stock forecasts
  - Weighted scoring for model selection (line 40)

- **[backend/app/services/etl_service.py](backend/app/services/etl_service.py)** - Data loading & preprocessing
  - `load_monthly_sales()` - Loads aggregated sales data from data warehouse
  - `load_daily_sales()` - Daily sales data loading
  - Data validation and normalization

### API Endpoints
- **[backend/app/api/training.py](backend/app/api/training.py)** - Training API routes
  - `POST /` - Start training job (line 76)
  - `GET /status/{training_id}` - Training status tracking

- **[backend/app/api/inventory.py](backend/app/api/inventory.py)** - Inventory API routes
  - `POST /api/inventory/training` - Start inventory training

- **[backend/app/api/forecast.py](backend/app/api/forecast.py)** - Forecast generation
  - `POST /` - Generate forecast endpoint

---

## 2. Training Flow & Orchestration

### Sales Forecasting Training Flow

```
API: POST /api/training (TrainingRequest)
  ├─ Validate session
  ├─ Create training_job in session_manager
  ├─ Resolve target (service/product/category/region)
  └─ Call: train_models()
      ├─ Load data via load_monthly_sales() or load_daily_sales()
      │   └─ Sources: mart.vw_monthly_sales_forecasting (view)
      ├─ _fill_temporal_gaps() - Fill missing dates
      ├─ _build_features() - Engineer features (line 541)
      ├─ Calculate train/test split via _resolve_test_size()
      ├─ For each model:
      │   ├─ Run model-specific training
      │   ├─ Calculate metrics on test set
      │   └─ Record result
      └─ Return sorted results (best model first)
```

**Key Code**: [training.py lines 76-140](backend/app/api/training.py#L76)

### Inventory Forecasting Training Flow

```
API: POST /api/inventory/training (InventoryTrainingRequest)
  ├─ Validate session
  ├─ Load inventory history via load_inventory_history()
  │   └─ Source: mart.fact_stock + mart.dim_products
  ├─ Aggregate by (date, product_family)
  ├─ Split into: global_agg, families_dict
  ├─ Calculate train/test split
  └─ For each model (INVENTORY_MODELS):
      ├─ Call _run_forecast_for_model()
      ├─ Calculate metrics (MAE, RMSE, MAPE, SMAPE, MPE)
      ├─ Calculate weighted_score
      └─ Store result
  └─ Sort by score (ascending) and return
```

**Key Code**: [inventory_forecasting_service.py lines 196-290](backend/app/services/inventory_forecasting_service.py#L196)

---

## 3. Train/Test Split Implementation

### Sales Forecasting Split
```python
# File: forecasting_service.py (line ~1125)
x, y = _build_features(series, granularity=granularity)
test_size = _resolve_test_size(len(x), horizon, granularity)
x_train, x_test = x.iloc[:-test_size], x.iloc[-test_size:]
y_train, y_test = y.iloc[:-test_size], y.iloc[-test_size:]
```

**`_resolve_test_size()` logic**:
- Returns time-based split for time series (last N samples)
- `test_size = max(1, min(horizon, len(data) - 1))`
- Respects minimum training set requirement

### Inventory Forecasting Split
```python
# File: inventory_forecasting_service.py (line ~228)
test_size = max(1, min(horizon, len(y_train) - 1))
y_test = y_train.iloc[-test_size:].copy()
y_train_split = y_train.iloc[:-test_size].copy()
```

**Time-series aware split**: Uses last N samples for testing (preserves temporal order)

---

## 4. Metrics Calculation

### Standard Metrics Function
**File**: [forecasting_service.py lines 119-123](backend/app/services/forecasting_service.py#L119)

```python
def _metrics(y_true: np.ndarray, y_pred: np.ndarray) -> tuple[float, float, float, float]:
    mae = float(mean_absolute_error(y_true, y_pred))                    # sklearn
    rmse = float(math.sqrt(mean_squared_error(y_true, y_pred)))        # √(MSE)
    mape = _mape(y_true, y_pred)                                        # Custom MAPE
    smape = _smape(y_true, y_pred)                                      # Custom SMAPE
    return mae, rmse, mape, smape
```

### MAPE Calculation
**File**: [forecasting_service.py lines 107-110](backend/app/services/forecasting_service.py#L107)

```python
def _mape(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    safe = np.where(y_true == 0, 1, y_true)  # Avoid division by zero
    return float(np.mean(np.abs((safe - y_pred) / safe)) * 100.0)
```

### SMAPE Calculation
**File**: [forecasting_service.py lines 112-117](backend/app/services/forecasting_service.py#L112)

```python
def _smape(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    denom = np.abs(y_true) + np.abs(y_pred)
    denom[denom == 0] = 1                    # Avoid division by zero
    return float(np.mean(2.0 * np.abs(y_pred - y_true) / denom) * 100.0)
```

### Inventory Weighted Scoring
**File**: [inventory_forecasting_service.py lines 40-72](backend/app/services/inventory_forecasting_service.py#L40)

```python
METRIC_WEIGHTS = {
    "mape": 0.35,    # Highest weight
    "rmse": 0.25,
    "mae": 0.20,
    "smape": 0.15,
    "mpe": 0.05,     # Mean Percentage Error
}

def _weighted_model_score(mae, rmse, mape, smape, mpe) -> float:
    # Normalize each metric to 0-100 scale
    mape_norm = min(100.0, mape) / 100.0
    rmse_norm = min(100.0, rmse) / 100.0
    mae_norm = min(100.0, mae) / 100.0
    smape_norm = min(100.0, smape) / 100.0
    mpe_norm = min(100.0, abs(mpe)) / 100.0
    
    # Weighted sum (lower score = better)
    score = (0.35 * mape_norm + 0.25 * rmse_norm + 0.20 * mae_norm + 
             0.15 * smape_norm + 0.05 * mpe_norm)
    return score
```

---

## 5. Data Preprocessing & Feature Engineering

### Data Filling (Temporal Gap Handling)
**File**: [forecasting_service.py lines 182-220](backend/app/services/forecasting_service.py#L182)

```python
def _fill_temporal_gaps(df: pd.DataFrame, freq: str) -> pd.DataFrame:
    # Create full date range (no missing dates)
    full_index = pd.date_range(
        start=work["date"].min(), 
        end=work["date"].max(), 
        freq=freq  # "MS" for monthly, "D" for daily
    )
    
    # Reindex and fill gaps
    # Numeric columns: fillna(0)
    # Categorical columns: forward-fill, backward-fill, then fillna("UNKNOWN")
    # Special: compute pct_ventes_promo if missing
    # Special: interpolate prix_moyen
```

**Fills for numeric columns**: `nb_ventes`, `nb_dealers_actifs`, `nb_ventes_promo`, `pct_ventes_promo`, `prix_moyen`

### Feature Engineering (Monthly)
**File**: [forecasting_service.py lines 541-590](backend/app/services/forecasting_service.py#L541)

```python
def _build_features(df: pd.DataFrame, granularity: str = "daily"):
    work = df.copy()
    work["trend_index"] = np.arange(len(work), dtype=float)  # Linear trend
    work["month"] = work["date"].dt.month
    work["quarter"] = work["date"].dt.quarter
    work["year"] = work["date"].dt.year
    work["promo_active"] = (work["nb_ventes_promo"] > 0).astype(int)
    work["promo_rate"] = work["nb_ventes_promo"] / work["nb_ventes"]
    
    # One-hot encode service_code
    service_oh = encoder.fit_transform(work[["service_code"]])
    
    if granularity == "monthly":
        # Seasonal encoding
        work["month_sin"] = np.sin(2.0 * np.pi * work["month"] / 12.0)
        work["month_cos"] = np.cos(2.0 * np.pi * work["month"] / 12.0)
        
        # Lag features
        for lag in [1, 2, 3, 12]:  # monthly lags
            work[f"sales_lag_{lag}"] = work["nb_ventes"].shift(lag)
        
        # Rolling window features
        for window in [3, 6, 12]:
            work[f"sales_roll_{window}"] = work["nb_ventes"].rolling(window, min_periods=1).mean()
            work[f"price_roll_{window}"] = work["prix_moyen"].rolling(window, min_periods=1).mean()
    
    feature_cols = [
        "trend_index", "month", "quarter",
        "month_sin", "month_cos",
        "promo_active", "promo_rate",
        "prix_moyen", "nb_dealers_actifs",
        "sales_lag_1", "sales_lag_2", "sales_lag_3", "sales_lag_12",
        "sales_roll_3", "sales_roll_6", "sales_roll_12",
        "price_roll_3", "price_roll_6", "price_roll_12",
    ]
    
    x = pd.concat([work[feature_cols], service_df], axis=1)
    y = work["nb_ventes"].astype(float)
    return x, y
```

**Features used for training**:
- **Trend**: Linear (`trend_index`)
- **Seasonal**: Month cyclical encoding (`month_sin`, `month_cos`)
- **Lagged values**: 1, 2, 3, and 12-month lags
- **Rolling averages**: 3, 6, 12-month windows
- **Promotions**: `promo_active`, `promo_rate`
- **Price**: `prix_moyen`, rolling price averages
- **Dealers**: `nb_dealers_actifs`
- **Categorical**: One-hot encoded `service_code`

### Daily Features
Same as monthly, but with:
- `day_of_week`, `is_weekend`
- `month_sin`, `month_cos` for yearly seasonality
- 7-day and 30-day rolling windows instead of monthly

---

## 6. Supported Models

### Sales Forecasting Models
**File**: [forecasting_service.py lines 44-50](backend/app/services/forecasting_service.py#L44)

```python
CLASSIC_MODELS = [
    "naive_last",           # Last observed value
    "seasonal_naive",       # Repeat last seasonal period
    "prophet",              # Facebook's time series library
    "sarima",               # Seasonal ARIMA
    "xgboost",              # Gradient boosting (uses features)
    "lstm",                 # LSTM proxy (simplified)
    "exp_smoothing",        # Exponential smoothing
    "linear_regression",    # With engineered features (uses features)
]

GENERATIVE_MODELS = [
    "chronos",              # Hugging Face Chronos
    "timesfm",              # Google TimesFM
    "patchtst",             # NeuralForecast PatchTST
    "autogluon",            # AutoML for time series
]
```

### Inventory Forecasting Models
**File**: [inventory_forecasting_service.py line 259](backend/app/services/inventory_forecasting_service.py#L259)

```python
INVENTORY_MODELS = [
    "naive_last",           # MAPE=1.18
    "seasonal_naive",       # MAPE=4.04
    "prophet",              # MAPE=113
    "lstm",                 # MAPE=1.68
]
# Note: xgboost, linear_regression excluded (no promo/price features for stock)
# Note: sarima, exp_smoothing excluded from inventory
```

### Model Implementation Functions
**File**: [forecasting_service.py lines 723-790](backend/app/services/forecasting_service.py#L723)

```python
def _run_forecast_for_model(model_key, y_train, history, horizon, granularity, freq, seasonal_period):
    if model_key == "sarima":
        return _run_sarima(y_train, horizon, seasonal_period)
    if model_key == "linear_regression":
        return _run_feature_model_autoregressive(...)  # Uses engineered features
    if model_key == "xgboost":
        return _run_feature_model_autoregressive(...)  # Uses engineered features
    if model_key == "exp_smoothing":
        return _run_exp_smoothing(y_train, horizon)
    if model_key == "prophet":
        return _run_prophet(y_train, horizon, freq=freq)
    if model_key == "lstm":
        return _run_lstm_proxy(y_train, horizon)
    if model_key == "naive_last":
        return _run_naive_last(y_train, horizon)
    if model_key == "seasonal_naive":
        return _run_seasonal_naive(y_train, horizon, seasonal_period)
    if model_key in GENERATIVE_MODELS:
        return _run_generative_model(model_key, y_train, horizon, freq)

def _run_sarima(y_train, steps, seasonal_period):
    model = SARIMAX(y_train, order=(1,1,1), seasonal_order=(1,1,1,seasonal_period))
    fit = model.fit(disp=False)
    return fit.forecast(steps=steps)

def _run_prophet(y_train, steps, freq):
    ds = pd.date_range(start="2020-01-01", periods=len(y_train), freq=freq)
    pdf = pd.DataFrame({"ds": ds, "y": y_train.values})
    model = Prophet(daily_seasonality=(freq=="D"), yearly_seasonality=True)
    model.fit(pdf)
    future = model.make_future_dataframe(periods=steps, freq=freq)
    fcst = model.predict(future)
    return fcst["yhat"].tail(steps).to_numpy()
```

---

## 7. Training Result Output

### Result Structure
**File**: [forecasting_service.py line 95-105](backend/app/services/forecasting_service.py#L95)

```python
@dataclass
class ModelRunResult:
    model: str              # Model name
    mae: float              # Mean Absolute Error
    rmse: float             # Root Mean Squared Error
    mape: float             # Mean Absolute Percentage Error
    smape: float            # Symmetric MAPE
    training_time_sec: float
    yhat: list[float]       # Test set predictions
```

**Inventory result includes**:
- `model`, `mae`, `rmse`, `mape`, `smape`
- `mpe`: Mean Percentage Error (can be negative)
- `score`: Weighted score (lower = better)
- `training_time_sec`
- `yhat`: Predictions on test set

### Returned to API
```json
{
  "status": "completed",
  "training_id": "...",
  "best_model": "prophet",
  "results": [
    {
      "model": "prophet",
      "mae": 1234.5,
      "rmse": 2345.6,
      "mape": 12.34,
      "smape": 11.45,
      "training_time_sec": 15.2,
      "yhat": [1000, 1050, 1100, ...]
    },
    ...
  ]
}
```

---

## 8. Data Pipeline Summary

```
CSV Upload
   ↓
ETL Service (etl_service.py)
   - Validate CSV columns
   - Auto-detect service type
   - Insert into staging table
   ↓
Training Pipeline
   ├─ Load from Data Warehouse (mart.* views)
   │   - mart.vw_monthly_sales_forecasting
   │   - mart.vw_daily_sales_forecasting
   │   - mart.fact_stock
   │   - mart.dim_products
   │
   ├─ Data Preprocessing (_fill_temporal_gaps)
   │   - Fill missing dates
   │   - Interpolate/forward-fill as needed
   │
   ├─ Feature Engineering (_build_features)
   │   - Temporal features (trend, season cycles)
   │   - Lag features (1, 2, 3, 12 months)
   │   - Rolling windows (3, 6, 12 periods)
   │   - One-hot encode categories
   │
   ├─ Train/Test Split
   │   - Time-series split (last N samples for testing)
   │   - N = min(horizon, len(data) - 1)
   │
   ├─ Model Training & Evaluation
   │   - Train each model on training set
   │   - Predict on test set
   │   - Calculate metrics (MAE, RMSE, MAPE, SMAPE)
   │   - For inventory: calculate weighted score
   │
   └─ Return Results Ranked by Performance
       - Sales: ranked by MAPE (ascending)
       - Inventory: ranked by weighted score (ascending)
```

---

## Key Configuration Parameters

**File**: [backend/app/core/config.py](backend/app/core/config.py)

```python
FORECAST_HORIZON_MONTHLY_DEFAULT = 6          # months
FORECAST_HORIZON_DAILY_DEFAULT = 30           # days
FORECAST_MIN_SAMPLES = 24                     # minimum history length
MLFLOW_TRACKING_URI = "http://mlflow:5000"
MLFLOW_EXPERIMENT_NAME = "Fibre_Forecast"
```

---

## Testing Files

- [backend/tests/test_forecasting_monthly.py](backend/tests/test_forecasting_monthly.py) - Monthly forecasting tests
- [backend/tests/test_forecasting_monthly.py#L46](backend/tests/test_forecasting_monthly.py#L46) - `test_build_features_monthly_adds_calendar_and_lag_signals`

---

## Summary Table

| Component | File | Key Function | Purpose |
|-----------|------|--------------|---------|
| Training Orchestration | forecasting_service.py | `train_models()` line 1097 | Main training loop for sales |
| Inventory Training | inventory_forecasting_service.py | `train_inventory_models()` line 196 | Stock-level training |
| Feature Engineering | forecasting_service.py | `_build_features()` line 541 | Create temporal + lag features |
| Data Loading | forecasting_service.py | `load_monthly_sales()` | Load sales from DW |
| Metrics | forecasting_service.py | `_metrics()` line 119 | Calculate MAE, RMSE, MAPE, SMAPE |
| Weighted Scoring | inventory_forecasting_service.py | `_weighted_model_score()` line 53 | Multi-metric model ranking |
| API Training | training.py | `start_training()` line 76 | HTTP endpoint |
| API Inventory | inventory.py | `start_inventory_training()` line 67 | HTTP inventory endpoint |
