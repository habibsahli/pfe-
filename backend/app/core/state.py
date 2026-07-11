"""
State management for forecasting sessions with database persistence.

Architecture:
- In-memory cache (fast, local)
- PostgreSQL backend (durable, shared)
- Fallback: if DB unavailable, use memory only
"""
import uuid
import json
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional, Any
from pydantic import BaseModel
import logging
from sqlalchemy import text
from sqlalchemy.orm import Session as SQLSession

logger = logging.getLogger(__name__)


class TrainingStatus(str, Enum):
    """Training job status"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class UploadSession(BaseModel):
    """Represents an upload session"""
    session_id: str
    service_detected: str
    rows_count: int
    period_start: str
    period_end: str
    preview: List[Dict[str, Any]]
    source_file: str
    created_at: datetime


class TrainingState(BaseModel):
    """Represents a model training job"""
    training_id: str
    session_id: str
    status: TrainingStatus = TrainingStatus.PENDING
    progress_pct: int = 0
    models_completed: int = 0
    total_models: int = 0
    results: List[Dict[str, Any]] = []
    best_model: Optional[str] = None
    created_at: datetime = None
    error_message: Optional[str] = None
    # Training request params — used to match a job to a forecast request when
    # model="best" (so a monthly/region job isn't used for a daily/service forecast).
    granularity: Optional[str] = None
    target_level: Optional[str] = None
    target_value: Optional[str] = None
    service_type: Optional[str] = None  # resolved service_code (None = all services)
    
    def __init__(self, **data):
        super().__init__(**data)
        if self.created_at is None:
            self.created_at = datetime.utcnow()


class SessionManager:
    """Manage forecasting sessions with database persistence.
    
    Strategy:
    - Write all operations to database (durable)
    - Keep in-memory cache for performance
    - On startup, load recent sessions from DB into cache
    - If DB unavailable, fall back to memory-only mode
    """
    
    def __init__(self):
        self._sessions: Dict[str, UploadSession] = {}
        self._training_jobs: Dict[str, TrainingState] = {}
        self._forecast_cache: Dict[str, Dict[str, Any]] = {}
        self._db_available = False
        self._memory_only_mode = False
    
    def set_db_available(self, available: bool):
        """Called by backend to indicate if DB is available."""
        self._db_available = available
        if available:
            self._memory_only_mode = False
            logger.info("✓ SessionManager: DB persistence enabled")
        else:
            self._memory_only_mode = True
            logger.warning("⚠ SessionManager: DB unavailable, using memory-only mode")
    
    def _get_db(self):
        """Get a DB session for operations. Must be called from within request context."""
        if not self._db_available:
            return None
        try:
            from app.db.session import SessionLocal
            return SessionLocal()
        except Exception as e:
            logger.warning(f"Failed to get DB session: {e}")
            return None
    
    def _load_from_db(self, db: Optional[SQLSession] = None) -> bool:
        """Load recent sessions from DB to warm up cache. Returns True if successful."""
        if not self._db_available or db is None:
            return False
        
        try:
            # Load upload sessions from last 24 hours
            cutoff = datetime.utcnow() - timedelta(hours=24)
            rows = db.execute(
                text("""
                    SELECT session_id, service_detected, rows_count, period_start, 
                           period_end, preview, source_file, created_at
                    FROM app.upload_sessions
                    WHERE created_at > :cutoff
                    ORDER BY created_at DESC
                """),
                {"cutoff": cutoff}
            ).mappings().all()
            
            for row in rows:
                session_id = str(row['session_id'])
                self._sessions[session_id] = UploadSession(
                    session_id=session_id,
                    service_detected=row['service_detected'],
                    rows_count=row['rows_count'],
                    period_start=str(row['period_start']),
                    period_end=str(row['period_end']),
                    preview=row['preview'] or [],
                    source_file=row['source_file'],
                    created_at=row['created_at'],
                )

            logger.info(f"✓ Loaded {len(self._sessions)} upload sessions from DB")
            return True
        except Exception as e:
            logger.warning(f"Failed to load sessions from DB: {e}")
            return False
    
    def create_upload_session(self, service_detected: str, rows_count: int, 
                            period_start: str, period_end: str, 
                            preview: List[Dict], source_file: str,
                            db: Optional[SQLSession] = None) -> str:
        """Create a new upload session. Persists to DB if available."""
        session_id = str(uuid.uuid4())
        session_obj = UploadSession(
            session_id=session_id,
            service_detected=service_detected,
            rows_count=rows_count,
            period_start=period_start,
            period_end=period_end,
            preview=preview,
            source_file=source_file,
            created_at=datetime.utcnow(),
        )
        
        # Store in memory
        self._sessions[session_id] = session_obj
        
        # Persist to DB if available
        if self._db_available and db:
            try:
                db.execute(
                    text("""
                        INSERT INTO app.upload_sessions 
                        (session_id, service_detected, rows_count, period_start, 
                         period_end, preview, source_file, created_at)
                        VALUES (:session_id, :service_detected, :rows_count, :period_start,
                                :period_end, CAST(:preview AS jsonb), :source_file, :created_at)
                    """),
                    {
                        "session_id": session_id,
                        "service_detected": service_detected,
                        "rows_count": rows_count,
                        "period_start": period_start,
                        "period_end": period_end,
                        "preview": json.dumps(preview, default=str),
                        "source_file": source_file,
                        "created_at": datetime.utcnow(),
                    }
                )
                db.commit()
                logger.info(f"✓ Persisted upload session {session_id} to DB")
            except Exception as e:
                logger.warning(f"Failed to persist session to DB: {e}")
                db.rollback()
        
        logger.info(f"✓ Created upload session {session_id}")
        return session_id
    
    def get_session(self, session_id: str) -> Optional[UploadSession]:
        """Get session by ID. Checks memory first, then DB."""
        # Try memory first
        if session_id in self._sessions:
            return self._sessions[session_id]

        # The DB column is UUID-typed; a malformed id would raise
        # "invalid input syntax for type uuid" and surface as an ERROR span.
        # Bail out early — a non-UUID can never match a persisted session anyway.
        try:
            uuid.UUID(str(session_id))
        except (ValueError, AttributeError, TypeError):
            return None

        # Try DB if available
        if self._db_available:
            db = self._get_db()
            if db:
                try:
                    row = db.execute(
                        text("""
                            SELECT session_id, service_detected, rows_count, period_start,
                                   period_end, preview, source_file, created_at
                            FROM app.upload_sessions
                            WHERE session_id = :session_id
                        """),
                        {"session_id": session_id}
                    ).mappings().first()
                    
                    if row:
                        session_obj = UploadSession(
                            session_id=str(row['session_id']),
                            service_detected=row['service_detected'],
                            rows_count=row['rows_count'],
                            period_start=str(row['period_start']),
                            period_end=str(row['period_end']),
                            preview=row['preview'] or [],
                            source_file=row['source_file'],
                            created_at=row['created_at'],
                        )
                        # Cache it
                        self._sessions[session_id] = session_obj
                        return session_obj
                except Exception as e:
                    logger.warning(f"Failed to fetch session from DB: {e}")
                finally:
                    db.close()
        
        return None
    
    def list_sessions(self) -> List[UploadSession]:
        """List all available upload sessions from memory cache."""
        return list(self._sessions.values())
    
    def create_training_job(self, session_id: str, total_models: int,
                           db: Optional[SQLSession] = None) -> str:
        """Create a new training job. Persists to DB if available."""
        training_id = str(uuid.uuid4())
        job = TrainingState(
            training_id=training_id,
            session_id=session_id,
            total_models=total_models,
            created_at=datetime.utcnow(),
        )
        
        # Store in memory
        self._training_jobs[training_id] = job
        
        # Persist to DB if available
        if self._db_available and db:
            try:
                db.execute(
                    text("""
                        INSERT INTO app.training_jobs
                        (training_id, session_id, status, progress_pct, 
                         models_completed, total_models, created_at)
                        VALUES (:training_id, :session_id, :status, :progress_pct,
                                :models_completed, :total_models, :created_at)
                    """),
                    {
                        "training_id": training_id,
                        "session_id": session_id,
                        "status": "pending",
                        "progress_pct": 0,
                        "models_completed": 0,
                        "total_models": total_models,
                        "created_at": datetime.utcnow(),
                    }
                )
                db.commit()
                logger.info(f"✓ Persisted training job {training_id} to DB")
            except Exception as e:
                logger.warning(f"Failed to persist training job to DB: {e}")
                db.rollback()
        
        logger.info(f"✓ Created training job {training_id}")
        return training_id
    
    def get_training_job(self, training_id: str) -> Optional[TrainingState]:
        """Get training job by ID. Checks memory first, then DB."""
        # Try memory first
        if training_id in self._training_jobs:
            return self._training_jobs[training_id]
        
        # Try DB if available
        if self._db_available:
            db = self._get_db()
            if db:
                try:
                    row = db.execute(
                        text("""
                            SELECT training_id, session_id, status, progress_pct,
                                   models_completed, total_models, results, best_model,
                                   error_message, created_at
                            FROM app.training_jobs
                            WHERE training_id = :training_id
                        """),
                        {"training_id": training_id}
                    ).mappings().first()
                    
                    if row:
                        job = TrainingState(
                            training_id=str(row['training_id']),
                            session_id=str(row['session_id']),
                            status=TrainingStatus(row['status']),
                            progress_pct=row['progress_pct'],
                            models_completed=row['models_completed'],
                            total_models=row['total_models'],
                            results=row['results'] or [],
                            best_model=row['best_model'],
                            created_at=row['created_at'],
                            error_message=row['error_message'],
                        )
                        # Cache it
                        self._training_jobs[training_id] = job
                        return job
                except Exception as e:
                    logger.warning(f"Failed to fetch training job from DB: {e}")
                finally:
                    db.close()
        
        return None
    
    def update_training_job(self, training_id: str, db: Optional[SQLSession] = None, **kwargs) -> bool:
        """Update training job data. Syncs to DB if available."""
        if training_id not in self._training_jobs:
            return False
        
        job = self._training_jobs[training_id]
        updated_fields = {}
        
        # Update in-memory state
        for key, value in kwargs.items():
            if hasattr(job, key):
                setattr(job, key, value)
                updated_fields[key] = value
        
        # Sync to DB if available
        if self._db_available and db and updated_fields:
            try:
                # Build dynamic SQL for update
                set_clauses = []
                params = {"training_id": training_id}
                
                for key, value in updated_fields.items():
                    set_clauses.append(f"{key} = :{key}")
                    if key == "results":
                        set_clauses[-1] = f"{key} = CAST(:{key} AS jsonb)"
                        params[key] = json.dumps(value, default=str)
                    else:
                        params[key] = value
                
                set_clause = ", ".join(set_clauses)
                query = f"UPDATE app.training_jobs SET {set_clause} WHERE training_id = :training_id"
                
                db.execute(text(query), params)
                db.commit()
                logger.debug(f"✓ Updated training job {training_id} in DB: {updated_fields}")
            except Exception as e:
                logger.warning(f"Failed to update training job in DB: {e}")
                db.rollback()
        
        return True
    
    def cache_forecast(self, cache_key: str, data: Dict[str, Any], 
                      session_id: Optional[str] = None,
                      db: Optional[SQLSession] = None):
        """Cache forecast results. Stores in memory and optionally in DB."""
        self._forecast_cache[cache_key] = data
        
        # Optionally persist to DB
        if self._db_available and db and session_id:
            try:
                db.execute(
                    text("""
                        INSERT INTO app.forecast_cache 
                        (cache_key, session_id, forecast_data, expires_at)
                        VALUES (:cache_key, :session_id, CAST(:forecast_data AS jsonb), :expires_at)
                        ON CONFLICT (cache_key) DO UPDATE SET
                            forecast_data = EXCLUDED.forecast_data,
                            expires_at = EXCLUDED.expires_at
                    """),
                    {
                        "cache_key": cache_key,
                        "session_id": session_id,
                        "forecast_data": json.dumps(data, default=str),
                        "expires_at": datetime.utcnow() + timedelta(hours=24),
                    }
                )
                db.commit()
            except Exception as e:
                logger.debug(f"Optional: forecast cache DB persist failed: {e}")
                db.rollback()
    
    def get_cached_forecast(self, cache_key: str) -> Optional[Dict[str, Any]]:
        """Get cached forecast. Checks memory first, then DB."""
        # Try memory first
        if cache_key in self._forecast_cache:
            return self._forecast_cache[cache_key]
        
        # Try DB if available
        if self._db_available:
            db = self._get_db()
            if db:
                try:
                    row = db.execute(
                        text("""
                            SELECT forecast_data FROM app.forecast_cache
                            WHERE cache_key = :cache_key
                            AND (expires_at IS NULL OR expires_at > NOW())
                        """),
                        {"cache_key": cache_key}
                    ).mappings().first()
                    
                    if row:
                        data = row['forecast_data']
                        # Cache it in memory
                        self._forecast_cache[cache_key] = data
                        return data
                except Exception as e:
                    logger.debug(f"Optional: forecast cache DB fetch failed: {e}")
                finally:
                    db.close()
        
        return None
    
    def cleanup_old_data(self, max_age_hours: int = 24, db: Optional[SQLSession] = None):
        """Remove old sessions and jobs. From memory and optionally from DB."""
        cutoff = datetime.utcnow() - timedelta(hours=max_age_hours)
        
        # Clean memory
        old_session_ids = [
            sid for sid, sess in self._sessions.items()
            if sess.created_at < cutoff
        ]
        
        old_job_ids = [
            jid for jid, job in self._training_jobs.items()
            if job.created_at < cutoff
        ]
        
        for sid in old_session_ids:
            del self._sessions[sid]
        
        for jid in old_job_ids:
            del self._training_jobs[jid]
        
        # Clean DB if available
        if self._db_available and db:
            try:
                db.execute(
                    text("DELETE FROM app.forecast_cache WHERE expires_at < NOW()")
                )
                db.execute(
                    text("""
                        DELETE FROM app.training_jobs 
                        WHERE created_at < :cutoff
                    """),
                    {"cutoff": cutoff}
                )
                db.execute(
                    text("""
                        DELETE FROM app.upload_sessions 
                        WHERE created_at < :cutoff
                    """),
                    {"cutoff": cutoff}
                )
                db.commit()
                logger.info(f"✓ Cleaned DB: removed data older than {max_age_hours} hours")
            except Exception as e:
                logger.warning(f"Failed to clean old data from DB: {e}")
                db.rollback()
        
        if old_session_ids or old_job_ids:
            logger.info(f"✓ Cleaned memory: {len(old_session_ids)} sessions, {len(old_job_ids)} jobs")


# Global session manager
session_manager = SessionManager()

# Legacy compatibility exports
UPLOAD_SESSIONS = session_manager._sessions
TRAINING_JOBS = session_manager._training_jobs
FORECAST_CACHE = session_manager._forecast_cache
