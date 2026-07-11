"""
Tests for API endpoints
"""
import pytest
from fastapi import FastAPI

from app.api import forecast as forecast_api
from app.api import training as training_api
from app.core.state import TrainingStatus, session_manager


def test_health(client):
    """Test health check endpoint"""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_root(client):
    """Test root endpoint"""
    response = client.get("/")
    assert response.status_code == 200
    assert "Fibre Forecast" in response.json()["message"]


def test_upload_missing_file(client):
    """Test upload without file"""
    response = client.post("/api/upload/")
    assert response.status_code in [400, 422]  # Missing file


def test_training_status_not_found(client):
    """Test training status for non-existent job"""
    response = client.get("/api/training/status/non-existent")
    assert response.status_code == 404


def test_telemetry_status(client):
    """Test telemetry endpoint"""
    response = client.get("/api/telemetry/status")
    assert response.status_code == 200
    data = response.json()
    assert "phoenix_enabled" in data
    assert "mlflow_enabled" in data


def test_forecast_best_uses_latest_completed_training_model(client, monkeypatch):
    """`model=best` should resolve to best_model from latest completed training job."""
    session_manager._sessions.clear()
    session_manager._training_jobs.clear()

    session_id = session_manager.create_upload_session(
        service_detected="FIBRE",
        rows_count=100,
        period_start="2024-01-01",
        period_end="2024-12-31",
        preview=[],
        source_file="sample.csv",
    )

    old_job_id = session_manager.create_training_job(session_id, total_models=8)
    session_manager.update_training_job(
        old_job_id,
        status=TrainingStatus.COMPLETED,
        best_model="naive_last",
    )

    new_job_id = session_manager.create_training_job(session_id, total_models=8)
    session_manager.update_training_job(
        new_job_id,
        status=TrainingStatus.COMPLETED,
        best_model="linear_regression",
    )

    captured: dict[str, str] = {}

    def fake_generate_forecast(**kwargs):
        captured["best_model_name"] = kwargs.get("best_model_name", "")
        return {
            "historical": [{"date": "2024-12-01", "value": 100.0}],
            "forecast": [{"date": "2025-01-01", "value": 105.0, "lower_bound": 90.0, "upper_bound": 120.0}],
            "metadata": {"model_used": kwargs.get("best_model_name")},
        }

    monkeypatch.setattr(forecast_api, "run_forecast_generation", fake_generate_forecast)

    response = client.post(
        "/api/forecast/",
        json={
            "session_id": session_id,
            "model": "best",
            "horizon": 6,
            "granularity": "monthly",
            "target_level": "service",
            "target_value": None,
        },
    )

    assert response.status_code == 200
    assert captured.get("best_model_name") == "linear_regression"
    assert response.json()["metadata"]["model_used"] == "linear_regression"


def test_training_resolves_close_target_value_typo(client, monkeypatch):
    """Training should auto-resolve close target value typos from UI input."""
    session_manager._sessions.clear()
    session_manager._training_jobs.clear()

    session_id = session_manager.create_upload_session(
        service_detected="FIBRE",
        rows_count=100,
        period_start="2024-01-01",
        period_end="2024-12-31",
        preview=[],
        source_file="sample.csv",
    )

    monkeypatch.setattr(training_api, "resolve_target_value", lambda **kwargs: "ben arous")

    captured: dict[str, str] = {}

    def fake_train_models(**kwargs):
        captured["target_value"] = kwargs.get("target_value")
        return [{"model": "naive_last", "mape": 10.0}]

    monkeypatch.setattr(training_api, "train_models", fake_train_models)

    response = client.post(
        "/api/training/",
        json={
            "session_id": session_id,
            "horizon": 6,
            "models": ["naive_last"],
            "enable_generative": False,
            "granularity": "monthly",
            "target_level": "region",
            "target_value": "ben arouas",
        },
    )

    assert response.status_code == 200
    assert captured.get("target_value") == "ben arous"
    assert response.json().get("resolved_target_value") == "ben arous"


def test_forecast_resolves_close_target_value_typo(client, monkeypatch):
    """Forecast should use resolved target value and expose it in metadata."""
    session_manager._sessions.clear()
    session_manager._training_jobs.clear()

    session_id = session_manager.create_upload_session(
        service_detected="FIBRE",
        rows_count=100,
        period_start="2024-01-01",
        period_end="2024-12-31",
        preview=[],
        source_file="sample.csv",
    )

    monkeypatch.setattr(forecast_api, "resolve_target_value", lambda **kwargs: "ben arous")

    captured: dict[str, str] = {}

    def fake_generate_forecast(**kwargs):
        captured["target_value"] = kwargs.get("target_value")
        return {
            "historical": [{"date": "2024-12-01", "value": 100.0}],
            "forecast": [{"date": "2025-01-01", "value": 101.0, "lower_bound": 90.0, "upper_bound": 120.0}],
            "metadata": {"model_used": kwargs.get("best_model_name")},
        }

    monkeypatch.setattr(forecast_api, "run_forecast_generation", fake_generate_forecast)

    response = client.post(
        "/api/forecast/",
        json={
            "session_id": session_id,
            "model": "naive_last",
            "horizon": 6,
            "granularity": "monthly",
            "target_level": "region",
            "target_value": "ben arouas",
        },
    )

    assert response.status_code == 200
    assert captured.get("target_value") == "ben arous"
    assert response.json().get("metadata", {}).get("resolved_target_value") == "ben arous"
