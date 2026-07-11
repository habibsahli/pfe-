-- ===== SESSION AND JOB PERSISTENCE =====
-- Tables for storing upload sessions and training jobs to enable durability across restarts
-- In-memory cache in the app will fall back to these for data recovery

CREATE SCHEMA IF NOT EXISTS app;
GRANT ALL PRIVILEGES ON SCHEMA app TO admin;

-- Upload sessions table
CREATE TABLE IF NOT EXISTS app.upload_sessions (
    session_id UUID PRIMARY KEY,
    service_detected VARCHAR(50) NOT NULL,
    rows_count INTEGER NOT NULL,
    period_start DATE,
    period_end DATE,
    preview JSONB,
    source_file VARCHAR(500),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_upload_sessions_created_at 
    ON app.upload_sessions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_upload_sessions_service 
    ON app.upload_sessions(service_detected);

COMMENT ON TABLE app.upload_sessions IS 'Durable storage for data upload sessions';
COMMENT ON COLUMN app.upload_sessions.session_id IS 'Unique session identifier';
COMMENT ON COLUMN app.upload_sessions.preview IS 'First 5 rows of uploaded data as JSON';

-- Training jobs table
CREATE TABLE IF NOT EXISTS app.training_jobs (
    training_id UUID PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES app.upload_sessions(session_id) ON DELETE CASCADE,
    status VARCHAR(50) NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed')),
    progress_pct INTEGER DEFAULT 0 CHECK (progress_pct >= 0 AND progress_pct <= 100),
    models_completed INTEGER DEFAULT 0,
    total_models INTEGER DEFAULT 0,
    results JSONB,
    best_model VARCHAR(100),
    error_message TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_training_jobs_session_id 
    ON app.training_jobs(session_id);
CREATE INDEX IF NOT EXISTS idx_training_jobs_status 
    ON app.training_jobs(status);
CREATE INDEX IF NOT EXISTS idx_training_jobs_created_at 
    ON app.training_jobs(created_at DESC);

COMMENT ON TABLE app.training_jobs IS 'Durable storage for model training job state';
COMMENT ON COLUMN app.training_jobs.status IS 'Job status: pending, running, completed, failed';
COMMENT ON COLUMN app.training_jobs.results IS 'Training results for all models as JSON';

-- Forecast cache table (optional, for caching expensive forecast computations)
CREATE TABLE IF NOT EXISTS app.forecast_cache (
    cache_key VARCHAR(500) PRIMARY KEY,
    session_id UUID NOT NULL,
    forecast_data JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ,
    FOREIGN KEY (session_id) REFERENCES app.upload_sessions(session_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_forecast_cache_session_id 
    ON app.forecast_cache(session_id);
CREATE INDEX IF NOT EXISTS idx_forecast_cache_expires_at 
    ON app.forecast_cache(expires_at);

COMMENT ON TABLE app.forecast_cache IS 'Optional cache for expensive forecast computations';
COMMENT ON COLUMN app.forecast_cache.expires_at IS 'TTL for automatic cleanup; NULL means no expiry';

-- Function to update updated_at timestamp
CREATE OR REPLACE FUNCTION app.update_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Triggers for automatic timestamp updates
CREATE TRIGGER trg_upload_sessions_update
    BEFORE UPDATE ON app.upload_sessions
    FOR EACH ROW
    EXECUTE FUNCTION app.update_timestamp();

CREATE TRIGGER trg_training_jobs_update
    BEFORE UPDATE ON app.training_jobs
    FOR EACH ROW
    EXECUTE FUNCTION app.update_timestamp();
