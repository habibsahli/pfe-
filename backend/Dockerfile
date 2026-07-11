FROM python:3.10-slim

WORKDIR /app

# Use CPU-only PyTorch wheels to keep CI builds reliable and lightweight.
ENV PIP_EXTRA_INDEX_URL=https://download.pytorch.org/whl/cpu

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    make \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --default-timeout=1200 --retries 15 -r requirements.txt
RUN pip install --no-cache-dir --default-timeout=1200 --retries 15 --no-deps \
    autogluon.common==1.5.0 \
    autogluon.core==1.5.0 \
    autogluon.features==1.5.0 \
    autogluon.tabular==1.5.0 \
    autogluon.timeseries==1.5.0
RUN pip install --no-cache-dir --default-timeout=1200 --retries 15 "marshmallow==3.21.3"
RUN python - <<'PY'
import cmdstanpy

try:
    cmdstanpy.cmdstan_path()
except Exception:
    cmdstanpy.install_cmdstan(verbose=False, overwrite=False)
    cmdstanpy.cmdstan_path()
PY

COPY . .

EXPOSE 8000
# IMPORTANT: single worker (default) is REQUIRED, not an oversight. Several stores
# live in process memory and are NOT shared across workers:
#   - inventory async job stores (_forecast_jobs / _rag_jobs in api/inventory.py) have
#     no DB backing — a job created on one worker is invisible to another, so status
#     polls would 404 intermittently.
#   - model="best" reads session_manager._training_jobs directly in memory.
# Blocking handlers are declared `def` so FastAPI runs them in a threadpool (off the
# event loop), which already gives good concurrency within one worker; heavy numeric
# work (numpy/torch/xgboost) releases the GIL so it is not single-core-bound.
# To scale horizontally (--workers >1 or multiple replicas), those in-memory stores
# must first move to shared storage (DB/Redis). Do NOT add --workers before that.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
