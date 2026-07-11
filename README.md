# 🚀 Fibre Forecast System v2.0

Multi-service forecasting platform (Fibre, 5G, Data Bundle, VOD) with RAG-powered intelligent explainability. The core backend, frontend, Milvus-based retrieval, and telemetry endpoints are implemented and runnable in Docker.

## ✨ Features

- **Multi-Service Forecasting**: Support for 4 telecom services with unified star schema
- **11 Forecasting Models**: 6 classical (Prophet, XGBoost, SARIMA, LSTM, etc.) + 1 ensemble + 4 LLM-based (Chronos, TimesFM)
- **RAG Knowledge Base**: Vector DB (Milvus) + Ollama embeddings for intelligent explanations
- **What-If Simulation**: Scenario analysis with LLM-powered impact assessment
- **Real-time Observability**: MLflow experiment tracking + Phoenix distributed tracing
- **Smart ETL**: Auto-detection of service type, geo-normalization, intelligent handling of missing data

## 🏗️ Architecture

```
├── backend/             # FastAPI (Python 3.10+)
│   ├── app/
│   │   ├── main.py      # FastAPI app factory
│   │   ├── api/         # 7 endpoint groups (upload, training, forecast, telemetry, etc.)
│   │   ├── core/        # Config, database, tracing, state
│   │   ├── db/          # PostgreSQL session
│   │   ├── services/    # ETL, forecasting, RAG, Ollama integration
│   │   └── models/      # SQLAlchemy ORM models (minimal scaffold)
│   └── Dockerfile
│
├── frontend/            # React 18 + Vite 5
│   ├── src/
│   │   ├── pages/       # 6 main pages
│   │   ├── App.jsx
│   │   └── main.jsx
│   └── Dockerfile
│
├── docker-compose.yml   # Complete stack orchestration
├── start_all.sh         # One-command system startup
└── scripts/
    ├── init_milvus_collection.py
    └── ...
```

## 🗄️ Database Schema

**Star Schema (PostgreSQL)**
- **Dimensions**: `dim_temps`, `dim_geographie`, `dim_services`, `dim_dealers`, `dim_offres`, `dim_products`, `dim_promotions`
- **Facts**: `fact_ventes`, `fact_stock`
- **Views**: `vw_daily_sales_by_service`, `vw_daily_stock_summary`

**Vector DB (Milvus)**
- Collection: `fibre_forecast_rag` (1024-dim embeddings via bge-m3)
- Metadata: doc_source, doc_type, service_type, region, page_number
- Retrieval: semantic vector search with service_type metadata filtering

## 🚀 Quick Start

### Prerequisite
- Docker & Docker Compose
- 8GB+ VRAM for Ollama (Llama 3.1 8B + bge-m3)

### One-Command Startup

```bash
chmod +x start_all.sh
./start_all.sh
```

Access the system:
- **Frontend**: http://localhost:3000
- **Backend API**: http://localhost:8000 (docs at /docs)
- **MLflow**: http://localhost:5000
- **Phoenix Tracing**: http://localhost:6006
- **Milvus UI (Attu)**: http://localhost:8001

### Manual Docker Startup

```bash
docker-compose up -d

# Initialize databases
docker exec -i fibre_postgres psql -U admin -d fibre_forecast_db < docker/init-scripts/01_create_schemas.sql
docker exec -i fibre_postgres psql -U admin -d fibre_forecast_db < docker/init-scripts/02_create_dimensions.sql
docker exec -i fibre_postgres psql -U admin -d fibre_forecast_db < docker/init-scripts/03_create_facts.sql

# Initialize Milvus
python scripts/init_milvus_collection.py
```

## 📊 Workflow

### 1. Upload CSV
- **Endpoint**: `POST /api/upload`
- Auto-detects service type (FIBRE/5G/DATA_BUNDLE/VOD)
- Validates columns, normalizes dates/GPS
- Returns 5-row data preview + metadata

### 2. Train Models
- **Endpoint**: `POST /api/training`
- Trains 11 models in parallel
- Logs metrics/artifacts to MLflow
- Returns best model + all scores (MAE, RMSE, MAPE)

### 3. Generate Forecast
- **Endpoint**: `POST /api/forecast`
- Uses best/selected model
- Returns historical + forecast + 95% confidence interval

### 4. Explainability (RAG)
- **Endpoint**: `POST /api/explain`
- Retrieves top-5 similar docs from Milvus
- Applies service-type metadata filtering when relevant
- LLM generates natural language explanation

### 5. What-If Simulation
- **Endpoint**: `POST /api/simulation`
- Parses scenario (promo -20%, dealer activation, etc.)
- Adjusts forecast with impact estimation
- Returns baseline + scenario forecasts

## 📁 API Reference

See [backend/app/api/](backend/app/api/) for implementation. Quick overview:

```python
# Upload & ETL
POST /api/upload            # File upload + auto-detection
GET  /api/upload/status     # Validation report

# Training  
POST /api/training/         # Start training job
GET  /api/training/status/{id}  # Poll progress

# Forecasting
POST /api/forecast/         # Generate forecast
POST /api/explain/          # RAG-powered explanation

# Knowledge Base
POST /api/knowledge/upload  # Ingest documents
POST /api/knowledge/qa      # Q&A with RAG

# Simulation
POST /api/simulation/       # What-if scenario

# Telemetry
GET  /api/telemetry/status  # System health
```

## 🧪 Testing

```bash
# Unit tests
cd backend
pytest tests/ -v --cov=app

# E2E tests (Playwright)
cd frontend
npx playwright test
```

## Current Implementation Notes

- The backend services are implemented and wired into the FastAPI routes.
- Retrieval is vector-first, with a service_type filter, not BM25/keyword hybrid search.
- Milvus is the active vector store when available; in-memory fallback remains for resilience.
- The frontend is functional and connected to the API, but advanced client-state features remain intentionally light.

## 🔧 Configuration

**Environment variables** (.env):
```bash
# Database
DATABASE_URL=postgresql://admin:SecurePassword123!@localhost:5432/fibre_forecast_db

# Milvus
MILVUS_HOST=localhost
MILVUS_PORT=19530

# Ollama
OLLAMA_HOST=http://localhost:11434
OLLAMA_EMBEDDING_MODEL=bge-m3
OLLAMA_LLM_MODEL=llama3.1:8b

# MLflow & Phoenix
MLFLOW_TRACKING_URI=http://localhost:5000
PHOENIX_COLLECTOR_ENDPOINT=http://localhost:6006
```

## 🧰 MCP Setup (VS Code Agent Mode)

This repo now includes a local MCP server that exposes Milvus tools to Copilot Agent Mode.

### Files Added

- `.vscode/mcp.json` - workspace MCP registration
- `tools/mcp/milvus_server.py` - stdio MCP server (`milvus-mcp`)
- `tools/mcp/requirements.txt` - MCP server dependency (`mcp` package)
- `tools/eval/retrieval_eval.py` - simple Recall@k retrieval evaluator
- `eval/questions.jsonl` - starter evaluation dataset template (FR/EN)

### 1) Start Local Services

Use your existing stack (`docker-compose up -d` or `./start_all.sh`) and ensure:

- Milvus is reachable at `http://localhost:19530`
- Ollama is running at `http://localhost:11434`
- MLflow is optional for this MCP server (usually `http://localhost:5000`)

If needed, pull the embedding model:

```bash
ollama pull bge-m3
```

### 2) Install MCP Server Dependency

In your Python environment:

```bash
pip install -r tools/mcp/requirements.txt
```

(`pymilvus` and `httpx` are already present in `backend/requirements.txt`.)

### 3) VS Code MCP Registration

Workspace config is in `.vscode/mcp.json` with server name `milvus-mcp`.

Configured environment defaults:

- `MILVUS_URI=http://localhost:19530`
- `MILVUS_DB=default`
- `MILVUS_COLLECTION=rag_chunks` (code default)
- `OLLAMA_HOST=http://localhost:11434`
- `OLLAMA_EMBED_MODEL=bge-m3`

Workspace override in `.vscode/mcp.json` is currently set to:

- `MILVUS_COLLECTION=fibre_forecast_rag`

This matches the collection used by this project out of the box.

If your VS Code build expects a slightly different MCP schema key than `servers`, keep this file as-is and mirror the same `milvus-mcp` block under the expected key (commonly `mcpServers`).

### 4) Verify with Self-Test (Smoke Test)

```bash
python -m tools.mcp.milvus_server --self-test
```

If you want to test against the project collection explicitly:

```bash
MILVUS_COLLECTION=fibre_forecast_rag OLLAMA_EMBED_MODEL=bge-m3 python -m tools.mcp.milvus_server --self-test
```

Expected checks:

- Milvus connectivity (`milvus_reachable`)
- Ollama embeddings call (`embedding_dim`)
- `describe_collection` output for your default collection

To start the stdio MCP server directly:

```bash
python -m tools.mcp.milvus_server
```

### 5) Optional Retrieval Evaluation

Run a quick Recall@k evaluation:

```bash
python -m tools.eval.retrieval_eval --dataset eval/questions.jsonl --top-k 5,10,15,20
```

The script uses `expected_ids` or `expected_sources` from each JSONL row and prints Recall@k.

## 🛠️ MCP Troubleshooting

- **Milvus not running / connection refused**
    - Verify container/service status and `MILVUS_URI`.
    - Check that port `19530` is exposed.

- **Collection not found**
    - Update `MILVUS_COLLECTION` in `.vscode/mcp.json` or create the collection.
    - Existing project default may be `fibre_forecast_rag`; MCP default is `rag_chunks`.

- **Ollama embeddings error**
    - Start Ollama and confirm `OLLAMA_HOST`.
    - Pull model: `ollama pull bge-m3`.
    - A `404` can also mean the model is missing (not only wrong endpoint). Verify installed models with `curl http://localhost:11434/api/tags`.

- **Embedding dimension mismatch**
    - Your collection vector dim must match the Ollama embedding model output dim.
    - Recreate collection/index or switch embedding model.

- **MCP tools do not appear in Agent Mode**
    - Reload VS Code window after editing `.vscode/mcp.json`.
    - Confirm Python used by VS Code can import `mcp`.
    - If schema key mismatch exists in your VS Code version, duplicate the server block under `mcpServers`.

## 📈 Monitoring

**MLflow** (http://localhost:5000):
- Tracks all training runs
- Stores model artifacts
- Maintains experiment history

**Phoenix** (http://localhost:6006):
- Distributed traces for ETL, forecasting, RAG
- Performance profiling
- Error tracking

## 📚 Documentation

- [SETUP.md](docs/SETUP.md) - Detailed installation
- [ARCHITECTURE.md](docs/ARCHITECTURE.md) - System design
- [API.md](docs/API.md) - Full API reference  
- [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) - Common issues

## 🛑 Shutdown

```bash
docker-compose down

# Remove volumes (data cleanup)
docker-compose down -v
```

## 🤝 Contributing

See [CONTRIBUTING.md](docs/CONTRIBUTING.md)

## 📝 License

Proprietary - All Rights Reserved

---

**Built with ❤️ for telecom forecasting** | 2026
