# Moirai Local Service Setup

This directory contains the HTTP service wrapper for running Salesforce's Moirai forecasting model locally.

## Overview

**Moirai Service** provides a FastAPI HTTP endpoint that wraps the Salesforce/moirai-1.1-R-small generative time-series forecasting model. It runs as a Docker container alongside your forecasting stack (Ollama, PostgreSQL, etc.) and integrates seamlessly with the backend forecasting engine.

## Features

- **Local Execution**: No external API keys required; runs entirely on your infrastructure
- **HTTP Interface**: Compatible with the forecasting service's generative model dispatcher
- **GPU Support**: Automatically detects and uses CUDA if available, falls back to CPU
- **Uncertainty Quantification**: Returns point forecast + percentile quantiles (10th, 90th)
- **Lazy Model Loading**: Model loads on first request to avoid startup delays

## Architecture

```
┌─────────────────────────────┐
│   Forecasting Service       │
│   (training.py/forecast.py) │
└──────────────┬──────────────┘
               │ HTTP POST /forecast
               ▼
┌─────────────────────────────┐
│   Moirai HTTP Service       │ (Port 8001)
│   (moirai_server.py)        │
└──────────────┬──────────────┘
               │ torch.forward()
               ▼
┌─────────────────────────────┐
│   Moirai Model              │
│   (LLM-based forecasting)   │
└─────────────────────────────┘
```

## Prerequisites

- **Docker**: For containerized deployment
- **GPU** (optional): NVIDIA GPU + CUDA for faster inference. CPU works but slower.
- **Python 3.12+**: If running locally without Docker
- **8GB+ RAM**: Minimum; 16GB+ recommended for concurrent training

## Installation & Deployment

### Option A: Docker Compose (Recommended)

The Moirai service is automatically integrated into your docker-compose.yml:

```bash
cd /home/habib/pfe
docker-compose up moirai
```

This will:
1. Build the Moirai Docker image from `docker/Dockerfile.moirai`
2. Start the service on port 8001
3. Perform health checks every 30 seconds

**Verify deployment**:
```bash
curl http://localhost:8001/health
# Expected response: {"status":"ok","service":"moirai-forecast"}
```

### Option B: Local Development (Without Docker)

If you want to run the service locally for development:

```bash
# 1. Create a virtual environment
python3.12 -m venv moirai_env
source moirai_env/bin/activate

# 2. Install dependencies
pip install -r tools/moirai_requirements.txt

# 3. Run the server
python tools/moirai_server.py
```

The service will start on http://localhost:8001

## Configuration

The service is configured via environment variables in `docker-compose.yml`:

| Variable | Value | Purpose |
|----------|-------|---------|
| `CUDA_VISIBLE_DEVICES` | `0` | GPU device ID (0=first GPU, or leave empty for CPU-only) |
| `--shm-size` | `8gb` | Shared memory for PyTorch DataLoaders |

If running locally, the service auto-detects GPU availability.

## API Endpoints

### Health Check
```
GET /health
Response: {"status":"ok","service":"moirai-forecast"}
```

### Forecast
```
POST /forecast
Content-Type: application/json

Request Body:
{
  "history": [10.5, 11.2, 10.8, ...],  // Historical values  
  "freq": "D",                         // "D" (daily) or "MS" (monthly)
  "horizon": 30,                       // Forecast steps ahead
  "num_samples": 100                   // Samples for quantile estimation
}

Response:
{
  "forecast": [11.1, 11.3, 12.0, ...],     // Point forecasts (mean)
  "mean": [11.1, 11.3, 12.0, ...],        // Mean forecast
  "median": [11.0, 11.2, 11.9, ...],      // Median forecast
  "quantile_0_1": [10.2, 10.5, 11.1, ...], // 10th percentile
  "quantile_0_9": [12.1, 12.4, 13.2, ...], // 90th percentile
  "forecast_samples": [...]                 // Full sample matrix (horizon x num_samples)
}
```

## Forecasting Service Integration

The backend forecasting engine (`forecasting_service.py`) automatically routes Moirai requests to this service:

```python
# In forecasting_service.py
def _run_generative_model(model_name: str, y_train: pd.Series, steps: int, freq: str):
    if model_name == "moirai":
        return _run_generative_moirai(y_train, steps, freq)  # → HTTP POST to moirai:8001
    # ... other models
```

**Backend Configuration** (docker-compose.yml):
```yaml
backend:
  environment:
    MOIRAI_API_URL: http://moirai:8001
    MOIRAI_API_KEY: local
```

## Monitoring & Troubleshooting

### Check Service Status
```bash
# Docker
docker ps | grep moirai
docker logs fibre_moirai

# Local
curl http://localhost:8001/health
```

### Common Issues

**Issue**: "ModuleNotFoundError: No module named 'moirai'"
- **Solution**: The moirai package must be installed from GitHub:
  ```bash
  pip install "moirai[torch] @ git+https://github.com/SalesforceAI/moirai.git@main"
  ```

**Issue**: Service starting but /forecast returns 500 error
- **Check logs**: `docker logs fibre_moirai`
- **Common cause**: Model download timeout on first request (normal, let it complete)
- **Solution**: Increase `GENERATIVE_HTTP_TIMEOUT` in backend config (default 120s)

**Issue**: Out of memory errors
- **Cause**: Insufficient GPU memory or shared memory
- **Solutions**:
  - Reduce `num_samples` in forecast requests (from 100 to 50)
  - Increase `--shm-size` in docker-compose.yml (8gb → 16gb)
  - Use CPU-only mode: Remove CUDA_VISIBLE_DEVICES or set to empty

**Issue**: Slow forecasts
- **If CPU-only**: Normal (can be 10-30s per forecast). GPU mode: 1-3s
- **Solution**: Configure NVIDIA GPU access in docker-compose.yml

### Performance Characteristics

| Mode | Latency | Notes |
|------|---------|-------|
| GPU (CUDA) | 1-3s | Requires NVIDIA GPU + docker --gpus all |
| CPU | 10-30s | Slow but functional |
| Model load | ~30-60s | One-time on first request (lazy load) |

## Environment Variables Reference

### Required
- `MOIRAI_API_URL`: Must be set to `http://moirai:8001` in docker-compose context

### Optional
- `CUDA_VISIBLE_DEVICES`: GPU device ID (auto-detected if not set)

### Defaults (Built-in)
- Port: 8001
- Model: Salesforce/moirai-1.1-R-small (auto-downloaded from HuggingFace)
- Timeout: 120 seconds (can be overridden via `GENERATIVE_HTTP_TIMEOUT` in backend)

## Testing the Service

Once running, test end-to-end via the backend:

```bash
# 1. Upload data and train with moirai
curl -X POST http://localhost:8000/api/training \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "test-moirai",
    "granularity": "monthly",
    "horizon": 6,
    "models": ["moirai"],
    "target_level": "region",
    "target_value": "Tunis"
  }'

# 2. Check results - Moirai should appear with metrics (MAE, RMSE, MAPE, SMAPE)
```

## Model Details

- **Model Name**: Salesforce/moirai-1.1-R-small
- **Type**: Tokenized LLM-based time-series forecasting
- **Context**: ~200 tokens (configurable, ~300-500 data points typical)
- **Capabilities**: Univariate + multivariate, handles missing data, learns seasonality
- **Paper**: [Moirai: A Time Series Foundation Model for Forecasting and Representation Learning](https://arxiv.org/abs/2402.02592)
- **License**: Salesforce Research (check terms)

## Maintenance

### Updating the Moirai Model
To use a different Moirai variant, edit `moirai_requirements.txt` and rebuild:

```bash
# Edit tools/moirai_requirements.txt
# Then rebuild
docker-compose build moirai
docker-compose up moirai
```

### Clearing Model Cache
The model is cached in the container. To force re-download:

```bash
docker-compose down moirai
docker system prune -a
docker-compose up moirai
```

## Architecture Notes

- **Why HTTP wrapper?**: Provides consistent interface for all generative models (Ollama/chronos/timesfm use same pattern)
- **Why lazy loading?**: Avoids long startup times; model only loads when needed
- **Why separate container?**: Isolates GPU memory, enables independent scaling
- **Response parsing**: Backend uses `_extract_numeric_series()` to handle JSON response variance

## Next Steps

1. **Deploy**: `docker-compose up moirai` (takes 5-10 min on first run for model download)
2. **Verify**: `curl http://localhost:8001/health`
3. **Train**: Run backend training with `models: ["moirai"]`
4. **Monitor**: Check backend logs for Moirai forecast metrics
5. **Compare**: A/B test Moirai vs chronos/timesfm/timegpt on your data

## Support

For Moirai model questions: https://github.com/SalesforceAI/moirai
For integration issues: Check backend logs (`docker logs fibre_backend`) and Moirai service logs (`docker logs fibre_moirai`)
