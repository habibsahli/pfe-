"""
Upload and ETL endpoints
"""
from pathlib import Path
import csv
from typing import Optional

from fastapi import APIRouter, UploadFile, File, Query, HTTPException, Depends
from sqlalchemy.orm import Session
import logging

from app.core.config import settings
from app.core.state import session_manager
from app.db.session import get_db
from app.services.etl_service import process_upload

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("")
@router.post("/")
async def upload_file(
    file: UploadFile = File(...),
    service_type: Optional[str] = Query(None),
    replace_stock: bool = Query(False),
    db: Session = Depends(get_db),
):
    """
    Upload CSV file for ETL processing
    
    - **file**: CSV file with sales/stock data
    - **service_type**: Optional (auto-detected if not provided)
    - **replace_stock**: If True, truncates existing stock facts before inserting new ones (defaults to False)
    """
    try:
        if not file.filename:
            raise HTTPException(status_code=400, detail="Missing filename")

        landing_dir = Path(settings.DATA_LANDING_DIR)
        landing_dir.mkdir(parents=True, exist_ok=True)
        file_path = landing_dir / file.filename

        content = await file.read()
        file_path.write_bytes(content)

        result = process_upload(file_path=file_path, db=db, forced_service=service_type, replace_stock=replace_stock)
        normalized_file_type = "stock" if str(result.file_type).startswith("stock") else result.file_type

        # Attempt to read original file headers so frontend can fallback to header-based routing
        file_headers = []
        try:
            with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
                sample = handle.read(4096)
                handle.seek(0)
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=[",", ";", "\t", "|"])
                    reader = csv.reader(handle, dialect)
                except csv.Error:
                    handle.seek(0)
                    reader = csv.reader(handle)
                try:
                    raw_headers = next(reader)
                    # Normalize whitespace and preserve original casing for inspection
                    file_headers = [h.strip() for h in raw_headers]
                except StopIteration:
                    file_headers = []
        except Exception:
            file_headers = []
        session_id = session_manager.create_upload_session(
            service_detected=result.service,
            rows_count=result.rows,
            period_start=result.period_start,
            period_end=result.period_end,
            preview=result.preview,
            source_file=result.filename,
            db=db,
        )

        return {
            "status": "completed",
            "session_id": session_id,
            "file": result.filename,
            "file_type": normalized_file_type,
            "file_headers": file_headers,
            "detected_file_type": result.file_type,
            "rows": result.rows,
            "inserted_rows": result.inserted_rows,
            "is_duplicate": result.is_duplicate,
            "service_detected": result.service,
            "period_start": result.period_start,
            "period_end": result.period_end,
            "preview": result.preview,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Upload failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
