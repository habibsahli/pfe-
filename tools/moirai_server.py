"""
Moirai Local HTTP Server
Exposes Salesforce/moirai-1.1-R-small as a FastAPI service
Compatible with forecasting_service.py HTTP dispatch pattern
"""
import json
import logging
from typing import List, Optional
from contextlib import asynccontextmanager

import numpy as np
import torch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

# Suppress model loading warnings
logging.getLogger("transformers.modeling_utils").setLevel(logging.ERROR)

logger = logging.getLogger(__name__)


# ============================================================================
# Request/Response Models
# ============================================================================

class TimeSeriesInput(BaseModel):
    """Input for Moirai forecast request"""
    history: List[float]
    freq: str = "D"  # "D" for daily, "MS" for monthly
    horizon: int = 30
    num_samples: int = 100  # Number of samples for uncertainty quantification


class MoiraiResponse(BaseModel):
    """Output from Moirai forecast"""
    forecast: List[float]
    forecast_samples: Optional[List[List[float]]] = None  # shape: (horizon, num_samples)
    mean: List[float]
    median: List[float]
    quantile_0_1: List[float]
    quantile_0_9: List[float]


# ============================================================================
# Global Model Holder
# ============================================================================

class ModelHolder:
    """Lazy load model on first request to avoid startup delay"""
    
    def __init__(self):
        self.model = None
        self.device = None
    
    def get_model(self):
        if self.model is None:
            logger.info("Loading Moirai model (first request)...")
            device = "cuda" if torch.cuda.is_available() else "cpu"
            logger.info(f"Using device: {device}")
            
            try:
                from moirai.model import MoiraiSmall
                self.model = MoiraiSmall.pretrained()
                self.model = self.model.to(device)
                self.model.eval()
                self.device = device
                logger.info("✓ Moirai model loaded successfully")
            except ImportError:
                logger.error("moirai package not found. Install with: pip install moirai")
                raise RuntimeError("Moirai not installed")
            except Exception as e:
                logger.error(f"Failed to load Moirai model: {e}")
                raise
        
        return self.model, self.device


model_holder = ModelHolder()


# ============================================================================
# FastAPI Startup/Shutdown
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle management: warm up model on startup"""
    logger.info("Moirai server starting...")
    # Lazy load happens on first /forecast call
    yield
    logger.info("Moirai server shutting down...")


app = FastAPI(
    title="Moirai Forecast Server",
    description="Local HTTP service for Salesforce Moirai generative forecasting",
    version="1.0.0",
    lifespan=lifespan
)


# ============================================================================
# Health Check
# ============================================================================

@app.get("/health")
async def health():
    """Health check endpoint"""
    return {"status": "ok", "service": "moirai-forecast"}


# ============================================================================
# Forecast Endpoint
# ============================================================================

@app.post("/forecast", response_model=MoiraiResponse)
async def forecast(request: TimeSeriesInput) -> MoiraiResponse:
    """
    Generate forecast using Moirai
    
    Args:
        request: TimeSeriesInput with history, horizon, freq
    
    Returns:
        MoiraiResponse with forecast point estimate and quantiles
    """
    try:
        if not request.history or len(request.history) < 2:
            raise ValueError("History must have at least 2 data points")
        
        if request.horizon <= 0 or request.horizon > 1000:
            raise ValueError("Horizon must be between 1 and 1000")
        
        # Map freq to Moirai convention
        freq_map = {
            "D": 7,      # Daily with 7-day seasonality
            "MS": 12,    # Monthly with 12-month seasonality (annual)
        }
        freq_moirai = freq_map.get(request.freq, 7)
        
        model, device = model_holder.get_model()
        
        logger.info(f"Forecasting: horizon={request.horizon}, freq={request.freq}, history_len={len(request.history)}")
        
        # Convert to torch tensor
        history_tensor = torch.tensor(
            request.history, 
            dtype=torch.float32, 
            device=device
        ).unsqueeze(0)  # Add batch dimension: (1, seq_len)
        
        # Forward pass with no_grad for inference
        with torch.no_grad():
            # Moirai returns samples: (batch, horizon, num_samples)
            forecast_samples = model.forecast(
                target_tensor=history_tensor,
                freq=freq_moirai,
                prediction_length=request.horizon,
                num_samples=request.num_samples
            )
        
        # Remove batch dimension and convert to numpy
        forecast_samples_np = forecast_samples.squeeze(0).cpu().numpy()  # (horizon, num_samples)
        
        # Compute statistics
        mean_forecast = np.mean(forecast_samples_np, axis=1).tolist()
        median_forecast = np.median(forecast_samples_np, axis=1).tolist()
        quantile_10 = np.percentile(forecast_samples_np, 10, axis=1).tolist()
        quantile_90 = np.percentile(forecast_samples_np, 90, axis=1).tolist()
        
        logger.info(f"✓ Forecast generated: mean={mean_forecast[:3]}... (first 3)")
        
        return MoiraiResponse(
            forecast=mean_forecast,
            forecast_samples=forecast_samples_np.tolist(),
            mean=mean_forecast,
            median=median_forecast,
            quantile_0_1=quantile_10,
            quantile_0_9=quantile_90
        )
    
    except ValueError as e:
        logger.error(f"Validation error: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Forecast error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Forecast failed: {str(e)}")


# ============================================================================
# Run Server
# ============================================================================

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8001,
        log_level="info"
    )
