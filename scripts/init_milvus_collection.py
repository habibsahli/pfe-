#!/usr/bin/env python3
"""
Initialize Milvus collection for RAG knowledge base
"""
import os
import sys
import logging
from pymilvus import Collection, CollectionSchema, DataType, FieldSchema, connections, utility

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration from environment
MILVUS_HOST = os.getenv("MILVUS_HOST", "localhost")
MILVUS_PORT = int(os.getenv("MILVUS_PORT", 19530))
COLLECTION_NAME = os.getenv("MILVUS_COLLECTION_NAME", "fibre_forecast_rag")
EMBEDDING_DIM = 1024  # bge-m3 embedding dimension


def init_collection() -> bool:
    """Initialize Milvus collection"""
    try:
        logger.info(f"🔌 Connecting to Milvus at {MILVUS_HOST}:{MILVUS_PORT}...")
        connections.connect(host=MILVUS_HOST, port=MILVUS_PORT)
        
        if utility.has_collection(COLLECTION_NAME):
            logger.info(f"✓ Collection {COLLECTION_NAME} already exists")
            return True
        
        logger.info(f"📐 Creating collection schema...")
        fields = [
            FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=EMBEDDING_DIM),
            FieldSchema(name="text_chunk", dtype=DataType.VARCHAR, max_length=5000),
            FieldSchema(name="doc_source", dtype=DataType.VARCHAR, max_length=500),
            FieldSchema(name="doc_type", dtype=DataType.VARCHAR, max_length=100),
            FieldSchema(name="service_type", dtype=DataType.VARCHAR, max_length=50),
            FieldSchema(name="region", dtype=DataType.VARCHAR, max_length=100),
            FieldSchema(name="page_number", dtype=DataType.INT32),
            FieldSchema(name="chunk_index", dtype=DataType.INT32),
        ]
        schema = CollectionSchema(fields=fields, description="RAG knowledge base")
        
        logger.info(f"✓ Creating collection {COLLECTION_NAME}...")
        collection = Collection(name=COLLECTION_NAME, schema=schema)
        
        logger.info("📇 Creating index on embeddings...")
        collection.create_index(
            field_name="embedding",
            index_params={
                "metric_type": "COSINE",
                "index_type": "HNSW",
                "params": {"M": 16, "efConstruction": 200}
            },
        )
        
        collection.load()
        logger.info(f"✅ Collection {COLLECTION_NAME} created and loaded successfully!")
        return True
        
    except Exception as e:
        logger.error(f"❌ Initialization failed: {e}")
        return False
    finally:
        connections.disconnect(alias="default")


if __name__ == "__main__":
    success = init_collection()
    sys.exit(0 if success else 1)
