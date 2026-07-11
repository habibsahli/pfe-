"""
Application configuration using Pydantic Settings
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings from environment variables"""
    
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"
    
    # PostgreSQL
    DATABASE_URL: str = "postgresql://admin:SecurePassword123!@localhost:5432/fibre_forecast_db"
    DATABASE_ECHO: bool = False
    
    # Milvus Vector DB
    MILVUS_HOST: str = "localhost"
    MILVUS_PORT: int = 19530
    MILVUS_COLLECTION_NAME: str = "fibre_forecast_rag"
    MILVUS_EMBEDDING_DIM: int = 1024  # bge-m3 dimension
    
    # Ollama LLM
    OLLAMA_HOST: str = "http://localhost:11434"
    OLLAMA_EMBEDDING_MODEL: str = "bge-m3"
    OLLAMA_LLM_MODEL: str = "llama3.1:8b"
    OLLAMA_TIMEOUT: int = 300

    # External generative forecasting models
    MOIRAI_API_URL: str = ""          # empty = disabled; set to running Moirai HTTP service URL
    MOIRAI_API_KEY: str = "local"
    MOIRAI_MODEL_NAME: str = "Salesforce/moirai-1.1-R-small"
    TIMEGPT_API_KEY: str = ""
    TIMEGPT_API_URL: str = "https://dashboard.nixtla.io"
    TIMEGPT_MODEL_NAME: str = "timegpt-1"
    TIMESFM_API_URL: str = ""         # empty = disabled; set to running TimesFM HTTP service URL
    TIMESFM_API_KEY: str = ""
    GENERATIVE_HTTP_TIMEOUT: int = 120

    # Prophet / CmdStan
    # Set to the CmdStan installation directory to skip auto-discovery.
    # Example: /home/user/.cmdstan/cmdstan-2.33.1
    CMDSTAN_PATH: str = ""

    # Hybrid retrieval (dense + lexical + fusion)
    RAG_DENSE_TOP_K: int = 40
    RAG_LEXICAL_TOP_K: int = 40
    RAG_FUSED_TOP_K: int = 20
    RAG_RRF_K: int = 60
    RAG_DENSE_WEIGHT: float = 0.60
    RAG_LEXICAL_WEIGHT: float = 0.40
    RAG_RERANK_OVERLAP_WEIGHT: float = 0.20
    RAG_CHUNK_TOKENS: int = 320
    RAG_CHUNK_OVERLAP_TOKENS: int = 64
    # Q&A guardrail: reject (return "I don't know") when the top retrieval score is
    # below this. Calibrated so off-topic questions (~0.33) are refused while
    # in-domain (>=0.65) pass. Raise toward 0.5 to be stricter, lower to be laxer.
    RAG_LOW_CONFIDENCE_THRESHOLD: float = 0.40
    
    # Agentic layer (app.agents)
    AGENT_ENABLED: bool = True
    AGENT_MAX_ITERATIONS: int = 4  # LLM<->tool round-trips before a final answer is forced
    AGENT_MODEL: str = ""          # empty = use OLLAMA_LLM_MODEL; set to override the agent model

    # MLflow
    MLFLOW_TRACKING_URI: str = "http://localhost:5000"
    MLFLOW_EXPERIMENT_NAME: str = "Fibre_Forecast"
    MLFLOW_ENABLE_REGISTRY: bool = True
    
    # Phoenix Tracing
    PHOENIX_COLLECTOR_ENDPOINT: str = "http://localhost:6006"
    PHOENIX_ENABLE_TRACING: bool = True
    
    # Forecasting parameters
    FORECAST_HORIZON_DEFAULT: int = 6          # max daily horizon (H+6)
    FORECAST_HORIZON_MONTHLY_DEFAULT: int = 12  # max monthly horizon (H+12)
    FORECAST_TRAIN_SIZE: float = 0.8
    FORECAST_MIN_SAMPLES: int = 5  # Minimum historical samples to train (lowered to support sparse regions like Sfax)
    
    # ETL configuration
    ETL_MAX_WORKERS: int = 4
    ETL_BATCH_SIZE: int = 1000
    ETL_CHUNK_SIZE: int = 10000
    
    # Data paths
    DATA_LANDING_DIR: str = "/data/landing"
    DATA_ARCHIVE_DIR: str = "/data/archive"
    DATA_KNOWLEDGE_DIR: str = "/data/knowledge"


# Global settings instance
settings = Settings()
