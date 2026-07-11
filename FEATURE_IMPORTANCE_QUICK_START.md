# Quick Start: Feature Importance API

**New Endpoint**: `POST /api/forecast/explain/factors`

## Basic Usage

### 1. Train a Model (Existing Flow)
```bash
curl -X POST http://localhost:8000/api/training \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "sess_abc123",
    "horizon": 6,
    "models": ["xgboost", "linear_regression"],
    "granularity": "monthly"
  }'
```

Response includes training results with `feature_importance` field:
```json
{
  "training_id": "job_xyz",
  "results": [
    {
      "model": "xgboost",
      "mape": 5.2,
      "feature_importance": [
        {"feature": "promo_rate", "importance": 0.45, "normalized": 45.0},
        {"feature": "month_sin", "importance": 0.32, "normalized": 32.0},
        {"feature": "sales_lag_1", "importance": 0.23, "normalized": 23.0}
      ]
    }
  ]
}
```

### 2. Get Factors for a Specific Model
```bash
curl -X POST http://localhost:8000/api/forecast/explain/factors \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "sess_abc123",
    "model": "xgboost"
  }'
```

**Response**:
```json
{
  "session_id": "sess_abc123",
  "model": "xgboost",
  "factors": [
    {
      "feature": "promo_rate",
      "importance": 0.45,
      "normalized": 45.0
    },
    {
      "feature": "month_sin",
      "importance": 0.32,
      "normalized": 32.0
    }
  ],
  "source": "training_results"
}
```

### 3. Auto-Select Best Model's Factors
```bash
curl -X POST http://localhost:8000/api/forecast/explain/factors \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "sess_abc123",
    "model": "best"
  }'
```

Automatically retrieves factors for the best performing model (lowest MAPE).

---

## Feature Interpretation Guide

| Feature Name | Meaning | Range | Example |
|-------------|---------|-------|---------|
| `promo_rate` | % of sales during promotion | 0-100% | 45.0 = promoting helped 45% |
| `month_sin` / `month_cos` | Seasonal (monthly) component | 0-100% | 32.0 = seasonal pattern explains 32% |
| `sales_lag_1` | Last period's sales influence | 0-100% | 23.0 = yesterday's sales matter 23% |
| `seasonality` | General seasonal effect | 0-100% | 60.0 = seasonality drives 60% |
| `trend` | Long-term direction | 0-100% | 40.0 = upward/downward trend 40% |
| `temporal_patterns` | Neural model temporal learning | 0-100% | 70.0 = model captures 70% of time patterns |
| `recent_history` | Naive model (last value) | 0-100% | 100.0 = using previous value |

---

## Integration Examples

### JavaScript / Frontend
```javascript
// Get factors after model selection
const response = await fetch('/api/forecast/explain/factors', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    session_id: sessionId,
    model: 'best'
  })
});

const data = await response.json();

// Display to user
const factorsList = data.factors
  .map(f => `${f.feature}: ${f.normalized.toFixed(1)}%`)
  .join('\n');

console.log(`Forecast driven by:\n${factorsList}`);
```

### Python Integration
```python
import requests

# Get factors
response = requests.post('http://localhost:8000/api/forecast/explain/factors', json={
    'session_id': session_id,
    'model': 'best'
})

factors = response.json()['factors']

# Create visualization
for factor in factors[:5]:  # Top 5
    bar = '█' * int(factor['normalized'] / 5)
    print(f"{factor['feature']:20s} {bar} {factor['normalized']:.1f}%")
```

### LLM Explanation Enhancement
```python
# In explain_forecast endpoint, can now reference factors:
top_factors = factors[:3]
explanation = f"""
The forecast increase of 4.8% is primarily driven by:
1. {top_factors[0]['feature']}: {top_factors[0]['normalized']:.1f}% influence
2. {top_factors[1]['feature']}: {top_factors[1]['normalized']:.1f}% influence
3. {top_factors[2]['feature']}: {top_factors[2]['normalized']:.1f}% influence
"""
```

---

## Response Status Codes

| Code | Meaning | Example |
|------|---------|---------|
| 200 | Success - factors returned | Model trained, factors available |
| 400 | Bad request | Missing session_id |
| 404 | Not found | Session doesn't exist, or no training results |
| 500 | Server error | Extraction failed (check logs) |

---

## Error Handling

### Session Not Found
```json
{
  "detail": "Upload session not found"
}
```
→ First upload data and train model

### No Training Results
```json
{
  "detail": "No feature importance data found for model 'xgboost'. Please run training first."
}
```
→ Run training endpoint first

### Invalid Model Name
```json
{
  "detail": "No feature importance data found for model 'invalid_model'. Please run training first."
}
```
→ Use model name from training results or "best"

---

## Testing Checklist

- [ ] Upload CSV file to create session
- [ ] Run training with multiple models
- [ ] Check training response includes `feature_importance`
- [ ] Call `/api/forecast/explain/factors` with specific model
- [ ] Call with `model: "best"` to auto-select
- [ ] Verify factors sum to ~100% (normalized)
- [ ] Check top factors make business sense
- [ ] Verify source is "training_results" or "cache"

---

## Model-Specific Factor Examples

### XGBoost
```json
{
  "factors": [
    {"feature": "promo_rate", "normalized": 35.2},
    {"feature": "month_sin", "normalized": 28.1},
    {"feature": "sales_lag_12", "normalized": 22.4},
    {"feature": "nb_dealers_actifs", "normalized": 14.3}
  ]
}
```
→ Real extracted feature importances

### Linear Regression
```json
{
  "factors": [
    {"feature": "trend_index", "normalized": 42.5},
    {"feature": "month_cos", "normalized": 28.3},
    {"feature": "promo_rate", "normalized": 19.2},
    {"feature": "prix_moyen_lag7", "normalized": 10.0}
  ]
}
```
→ Coefficient magnitude-based importance

### Prophet
```json
{
  "factors": [
    {"feature": "trend", "normalized": 50.0},
    {"feature": "seasonality", "normalized": 50.0}
  ]
}
```
→ Heuristic component split

### LSTM / Generative Models
```json
{
  "factors": [
    {"feature": "temporal_patterns", "normalized": 70.0},
    {"feature": "learned_representations", "normalized": 30.0}
  ]
}
```
→ Heuristic for black-box models

---

## Known Limitations

1. **Neural models use heuristics**: LSTM and generative models don't provide real feature importance. These show categorical heuristics for transparency.

2. **XGBoost/Linear Regression only**: Real feature importance only works for these two model types.

3. **Top-K limiting**: Returns top 10 features maximum to prevent overwhelming output.

4. **Cached data can be stale**: If reftraining with same session_id, may need to clear cache.

5. **No SHAP values yet**: More advanced explainability coming in v2.1.

---

## Performance Notes

- Feature extraction happens during training (no latency added)
- Retrieval endpoint is instant (returns cached results)
- Storing importance for each model adds ~500 bytes per training result
- Memory footprint negligible for typical use (<1MB for 100 sessions)

---

## Troubleshooting

### Factors always show heuristic values
→ You're using a model type without real feature extraction (Naive, SARIMA, LSTM, Generative)  
→ Try XGBoost or Linear Regression instead

### Empty factors list
→ Training failed or results not stored  
→ Check training endpoint response for errors

### Different factors than expected
→ Model may have been retrained with different data  
→ Check timestamp of training results

### Normalized doesn't sum to 100%
→ Top-10 selection means some features excluded  
→ This is intentional to focus on top drivers

---

## Contact & Support

For issues or questions about feature importance:
1. Check `/api/forecast/explain/factors` endpoint docs
2. Review training results for `feature_importance` field
3. Verify model type supports feature extraction
4. Check logs for extraction errors
