"""
FastAPI Application Factory & Main Entry Point
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import logging

from app.core.config import settings
from app.core.tracing import setup_tracing, flush_tracing

# Configure logging first, then tracing — both must happen before any service imports
logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL))
logger = logging.getLogger(__name__)
setup_tracing()

from app.core.state import session_manager
from app.db.session import SessionLocal
from app.api import upload, training, forecast, explain, knowledge, simulation, telemetry, inventory, promo_simulation, anomaly, agent


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown events"""
    logger.info("🚀 Starting Fibre Forecast Backend...")

    # Initialize database persistence for session manager
    try:
        db = SessionLocal()
        session_manager.set_db_available(True)
        session_manager._load_from_db(db)
        db.close()
        logger.info("✓ Session persistence: database backend enabled")
    except Exception as e:
        logger.warning(f"⚠ Session persistence: database unavailable, using memory-only mode: {e}")
        session_manager.set_db_available(False)

    # Guarantee anomaly tables exist and hydrate the explanation cache (idempotent).
    try:
        from app.api.anomaly import ensure_anomaly_reviews_table, ensure_anomaly_explanations_table
        db = SessionLocal()
        ensure_anomaly_reviews_table(db)
        ensure_anomaly_explanations_table(db)
        db.close()
    except Exception as e:
        logger.warning(f"⚠ Anomaly table setup failed: {e}")

    try:
        from app.services.stock_recommendation_service import ensure_sku_thresholds_table
        ensure_sku_thresholds_table()
        logger.info("✓ SKU thresholds table: ready")
    except Exception as e:
        logger.warning(f"⚠ SKU thresholds table setup failed: {e}")

    # Pre-load feature importance cache from disk so it survives restarts.
    try:
        from app.services.forecasting_service import _load_feature_importance_cache
        _load_feature_importance_cache()
        logger.info("✓ Feature importance cache: loaded from disk")
    except Exception as e:
        logger.warning(f"⚠ Feature importance cache load failed: {e}")

    # Validate Prophet / CmdStan availability at startup (fail fast with a clear message).
    try:
        from app.services.forecasting_service import _ensure_cmdstan_available
        _ensure_cmdstan_available()
        logger.info("✓ Prophet / CmdStan: ready")
    except Exception as e:
        logger.warning(
            f"⚠ Prophet / CmdStan unavailable: {e}. "
            "Prophet forecasting will fail at runtime. "
            "Set CMDSTAN_PATH or pre-install CmdStan to fix this."
        )

    # Re-hydrate in-memory vector store from PostgreSQL so dense search works after restart
    try:
        from app.services.rag_service import rag_service
        hydrated = rag_service.hydrate_memory_store()
        if hydrated:
            logger.info(f"✓ RAG memory store: hydrated {hydrated} chunks from PostgreSQL")
        else:
            logger.info("✓ RAG memory store: no chunks to hydrate (knowledge base empty or already loaded)")
    except Exception as e:
        logger.warning(f"⚠ RAG memory store hydration failed: {e}")

    yield
    logger.info("🛑 Shutting down Fibre Forecast Backend...")
    flush_tracing()


# Create FastAPI app
app = FastAPI(
    title="Fibre Forecast API",
    description="Multi-service forecasting system with RAG insights",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Health check endpoint
@app.get("/health")
async def health_check():
    """Health check for liveness probes"""
    return {
        "status": "ok",
        "service": "fibre-forecast-api",
        "version": "1.0.0"
    }


# Root endpoint
@app.get("/")
async def root():
    """API root with basic info"""
    return {
        "message": "Fibre Forecast System API",
        "docs": "/docs",
        "openapi": "/openapi.json"
    }


# Include API routers
app.include_router(upload.router, prefix="/api/upload", tags=["Upload & ETL"])
app.include_router(training.router, prefix="/api/training", tags=["Training"])
app.include_router(forecast.router, prefix="/api/forecast", tags=["Forecast"])
app.include_router(inventory.router)  # Existing /api/inventory routes
app.include_router(inventory.recommendation_router)  # /api/v1/inventory/recommendations
app.include_router(explain.router, prefix="/api/explain", tags=["Explainability"])
app.include_router(knowledge.router, prefix="/api/knowledge", tags=["Knowledge Base"])
app.include_router(simulation.router, prefix="/api/simulation", tags=["What-If"])
app.include_router(telemetry.router, prefix="/api/telemetry", tags=["Telemetry"])
app.include_router(promo_simulation.router)
app.include_router(anomaly.router)
app.include_router(agent.router)  # /api/agent/chat — agentic layer (Phase 1: Stock specialist)


# Global exception handlers
@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Global error handler"""
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc), "type": type(exc).__name__},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.API_HOST, port=settings.API_PORT)
