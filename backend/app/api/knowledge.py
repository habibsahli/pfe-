"""
Knowledge base management endpoints
"""
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, UploadFile, File, HTTPException
from pydantic import BaseModel
import logging

from app.core.config import settings
from app.core.tracing import flush_tracing
from app.services.rag_service import rag_service

router = APIRouter()
logger = logging.getLogger(__name__)


class QARequest(BaseModel):
    """Q&A request"""
    question: str
    service_type: Optional[str] = None


@router.post("/upload")
async def upload_documents(files: list[UploadFile] = File(...)):
    """
    Upload knowledge base documents (PDF, Word, TXT)
    """
    try:
        if not files:
            raise HTTPException(status_code=400, detail="No files provided")

        base_dir = Path(settings.DATA_KNOWLEDGE_DIR)
        base_dir.mkdir(parents=True, exist_ok=True)

        saved_paths: list[Path] = []
        for upload in files:
            if not upload.filename:
                continue
            target = base_dir / upload.filename
            content = await upload.read()
            target.write_bytes(content)
            saved_paths.append(target)

        result = rag_service.ingest_documents(saved_paths)
        return {
            "status": "completed",
            "files_count": len(saved_paths),
            **result,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Upload failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/qa")
def answer_question(request: QARequest):
    """
    Answer question using RAG knowledge base
    """
    try:
        result = rag_service.answer_question(
            question=request.question,
            service_type=request.service_type,
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Q&A failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # Drain the Q&A trace (root + retrieval + LLM spans) to Phoenix promptly.
        # Without this, spans rely on passive BatchSpanProcessor flushing, which was
        # observed to stall on a long-running process and silently drop the whole
        # Q&A trace until a backend restart.
        flush_tracing()


@router.get("/status")
async def get_knowledge_status():
    """
    Get knowledge base status
    """
    return rag_service.get_status()
