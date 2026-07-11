"""
Telemetry and observability endpoints
"""
from fastapi import APIRouter
import logging
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import text

from app.core.config import settings
from app.db.session import engine

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/status")
async def get_telemetry_status():
    """
    Get telemetry system status (MLflow, Phoenix, etc.)
    """
    return {
        "phoenix_enabled": True,
        "phoenix_endpoint": settings.PHOENIX_COLLECTOR_ENDPOINT,
        "mlflow_enabled": True,
        "mlflow_endpoint": settings.MLFLOW_TRACKING_URI,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }


@router.get("/health")
async def telemetry_health():
    """
    Detailed health check for all services
    """
    def check_database() -> dict[str, Any]:
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return {"status": "ok"}
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}

    def check_http(url: str, path: str = "") -> dict[str, Any]:
        endpoint = f"{url.rstrip('/')}{path}"
        try:
            with httpx.Client(timeout=5.0) as client:
                response = client.get(endpoint)
            return {"status": "ok" if response.status_code < 400 else "error", "http_code": response.status_code, "endpoint": endpoint}
        except Exception as exc:
            return {"status": "error", "endpoint": endpoint, "detail": str(exc)}

    def check_milvus() -> dict[str, Any]:
        alias = "telemetry"
        try:
            from pymilvus import connections, utility

            connections.connect(alias=alias, host=settings.MILVUS_HOST, port=str(settings.MILVUS_PORT))
            collections = utility.list_collections(using=alias)
            return {
                "status": "ok",
                "collection_count": len(collections),
                "collections": collections,
            }
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}
        finally:
            try:
                connections.disconnect(alias=alias)
            except Exception:
                pass

    return {
        "database": check_database(),
        "milvus": check_milvus(),
        "ollama": check_http(settings.OLLAMA_HOST, "/api/tags"),
        "mlflow": check_http(settings.MLFLOW_TRACKING_URI),
        "phoenix": check_http(settings.PHOENIX_COLLECTOR_ENDPOINT, "/health"),
    }
