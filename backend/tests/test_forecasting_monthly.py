"""Unit tests for monthly forecasting helpers."""

import pytest
import numpy as np
import pandas as pd
from contextlib import nullcontext

from app.services import forecasting_service
from app.services.forecasting_service import _build_features, _fill_temporal_gaps, _run_seasonal_naive


def test_fill_temporal_gaps_monthly_inserts_missing_month():
    frame = pd.DataFrame(
        {
            "date": ["2024-01-01", "2024-03-01"],
            "service_code": ["FIBRE", "FIBRE"],
            "product_id": ["P1", "P1"],
            "product_name": ["Product 1", "Product 1"],
            "product_category": ["router", "router"],
            "region_key": ["NORD", "NORD"],
            "region_label": ["Nord", "Nord"],
            "nb_ventes": [10, 30],
            "nb_dealers_actifs": [2, 4],
            "nb_ventes_promo": [1, 3],
            "pct_ventes_promo": [10.0, 10.0],
            "prix_moyen": [99.0, 105.0],
        }
    )

    filled = _fill_temporal_gaps(frame, "MS")

    assert len(filled) == 3
    assert filled.loc[1, "date"].strftime("%Y-%m-%d") == "2024-02-01"
    assert filled.loc[1, "nb_ventes"] == 0
    assert filled.loc[1, "prix_moyen"] >= 0


def test_seasonal_naive_repeats_last_season_cycle():
    y_train = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0, 20.0, 21.0])

    forecast = _run_seasonal_naive(y_train, 6, season_length=12)

    assert forecast.tolist() == [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]


def test_build_features_monthly_adds_calendar_and_lag_signals():
    frame = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=13, freq="MS"),
            "service_code": ["FIBRE"] * 13,
            "product_id": ["P1"] * 13,
            "product_name": ["Product 1"] * 13,
            "product_category": ["router"] * 13,
            "region_key": ["NORD"] * 13,
            "region_label": ["Nord"] * 13,
            "nb_ventes": list(range(10, 23)),
            "nb_dealers_actifs": list(range(2, 15)),
            "nb_ventes_promo": [1] * 13,
            "pct_ventes_promo": [10.0] * 13,
            "prix_moyen": list(range(90, 103)),
        }
    )

    x, y = _build_features(frame, granularity="monthly")

    assert len(x) == len(y) == 13
    assert "month_sin" in x.columns
    assert "sales_lag_12" in x.columns
    assert x["sales_lag_12"].iloc[-1] == 10.0


def test_train_models_raises_when_a_classic_model_fails(monkeypatch):
    frame = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=12, freq="MS"),
            "service_code": ["FIBRE"] * 12,
            "product_id": ["P1"] * 12,
            "product_name": ["Product 1"] * 12,
            "product_category": ["router"] * 12,
            "region_key": ["NORD"] * 12,
            "region_label": ["Nord"] * 12,
            "nb_ventes": list(range(10, 22)),
            "nb_dealers_actifs": list(range(2, 14)),
            "nb_ventes_promo": [1] * 12,
            "pct_ventes_promo": [10.0] * 12,
            "prix_moyen": list(range(90, 102)),
        }
    )

    class DummyDB:
        pass

    monkeypatch.setattr(forecasting_service, "load_monthly_sales", lambda *args, **kwargs: frame.copy())
    monkeypatch.setattr(forecasting_service, "_run_xgboost", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(forecasting_service.mlflow, "set_tracking_uri", lambda *args, **kwargs: None)
    monkeypatch.setattr(forecasting_service.mlflow, "set_experiment", lambda *args, **kwargs: None)
    monkeypatch.setattr(forecasting_service, "_start_mlflow_run", lambda *args, **kwargs: nullcontext())

    with pytest.raises(RuntimeError, match="boom"):
        forecasting_service.train_models(
            db=DummyDB(),
            horizon=6,
            enable_generative=False,
            service_code="FIBRE",
            selected_models=["naive_last", "xgboost"],
            granularity="monthly",
        )


def test_generate_forecast_uses_linear_regression_branch(monkeypatch):
    frame = pd.DataFrame(
        {
            "date": pd.date_range("2023-01-01", periods=18, freq="MS"),
            "service_code": ["FIBRE"] * 18,
            "product_id": ["P1"] * 18,
            "product_name": ["Product 1"] * 18,
            "product_category": ["router"] * 18,
            "region_key": ["NORD"] * 18,
            "region_label": ["Nord"] * 18,
            "nb_ventes": list(range(100, 118)),
            "nb_dealers_actifs": list(range(20, 38)),
            "nb_ventes_promo": [10] * 18,
            "pct_ventes_promo": [10.0] * 18,
            "prix_moyen": list(range(90, 108)),
        }
    )

    call_count = {"value": 0}

    def fake_linear_regression(x_train, y_train, x_test):
        call_count["value"] += 1
        return np.asarray([321.0])

    monkeypatch.setattr(forecasting_service, "load_monthly_sales", lambda *args, **kwargs: frame.copy())
    monkeypatch.setattr(forecasting_service, "_run_linear_regression", fake_linear_regression)

    payload = forecasting_service.generate_forecast(
        db=None,
        best_model_name="linear_regression",
        horizon=3,
        service_code="FIBRE",
        granularity="monthly",
        target_level="service",
        target_value=None,
    )

    values = [row["value"] for row in payload["forecast"]]
    assert values == [321.0, 321.0, 321.0]
    assert call_count["value"] == 3


def test_generate_forecast_uses_xgboost_branch(monkeypatch):
    frame = pd.DataFrame(
        {
            "date": pd.date_range("2023-01-01", periods=18, freq="MS"),
            "service_code": ["FIBRE"] * 18,
            "product_id": ["P1"] * 18,
            "product_name": ["Product 1"] * 18,
            "product_category": ["router"] * 18,
            "region_key": ["NORD"] * 18,
            "region_label": ["Nord"] * 18,
            "nb_ventes": list(range(80, 98)),
            "nb_dealers_actifs": list(range(10, 28)),
            "nb_ventes_promo": [8] * 18,
            "pct_ventes_promo": [10.0] * 18,
            "prix_moyen": list(range(70, 88)),
        }
    )

    call_count = {"value": 0}

    def fake_xgboost(x_train, y_train, x_test):
        call_count["value"] += 1
        return np.asarray([222.0])

    monkeypatch.setattr(forecasting_service, "load_monthly_sales", lambda *args, **kwargs: frame.copy())
    monkeypatch.setattr(forecasting_service, "_run_xgboost", fake_xgboost)

    payload = forecasting_service.generate_forecast(
        db=None,
        best_model_name="xgboost",
        horizon=3,
        service_code="FIBRE",
        granularity="monthly",
        target_level="service",
        target_value=None,
    )

    values = [row["value"] for row in payload["forecast"]]
    assert values == [222.0, 222.0, 222.0]
    assert call_count["value"] == 3


def test_run_prophet_uses_cmdstanpy_backend(monkeypatch):
    y_train = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0, 15.0])
    seen = {}

    class FakeProphet:
        def __init__(self, **kwargs):
            seen["kwargs"] = kwargs

        def fit(self, pdf):
            seen["fit_rows"] = len(pdf)
            return self

        def make_future_dataframe(self, periods, freq):
            return pd.DataFrame(
                {
                    "ds": pd.date_range(start="2024-01-01", periods=len(y_train) + periods, freq=freq)
                }
            )

        def predict(self, future):
            return pd.DataFrame({"yhat": np.arange(len(future), dtype=float)})

    monkeypatch.setattr(forecasting_service, "Prophet", FakeProphet)
    monkeypatch.setattr(forecasting_service, "_ensure_cmdstan_available", lambda: None)

    forecast = forecasting_service._run_prophet(y_train, 3, freq="MS")

    assert seen["kwargs"]["stan_backend"] == "CMDSTANPY"
    assert seen["fit_rows"] == len(y_train)
    assert forecast.tolist() == [6.0, 7.0, 8.0]


@pytest.mark.parametrize(
    "model_name,patched_symbol,expected_value",
    [
        ("naive_last", "_run_naive_last", 101.0),
        ("seasonal_naive", "_run_seasonal_naive", 102.0),
        ("sarima", "_run_sarima", 103.0),
        ("exp_smoothing", "_run_exp_smoothing", 104.0),
        ("prophet", "_run_prophet", 105.0),
        ("lstm", "_run_lstm_proxy", 106.0),
        ("chronos", "_run_generative_model", 107.0),
    ],
)
def test_generate_forecast_routes_supported_models_without_fallback(monkeypatch, model_name, patched_symbol, expected_value):
    frame = pd.DataFrame(
        {
            "date": pd.date_range("2023-01-01", periods=18, freq="MS"),
            "service_code": ["FIBRE"] * 18,
            "product_id": ["P1"] * 18,
            "product_name": ["Product 1"] * 18,
            "product_category": ["router"] * 18,
            "region_key": ["NORD"] * 18,
            "region_label": ["Nord"] * 18,
            "nb_ventes": list(range(100, 118)),
            "nb_dealers_actifs": list(range(20, 38)),
            "nb_ventes_promo": [10] * 18,
            "pct_ventes_promo": [10.0] * 18,
            "prix_moyen": list(range(90, 108)),
        }
    )

    monkeypatch.setattr(forecasting_service, "load_monthly_sales", lambda *args, **kwargs: frame.copy())

    called = {"value": 0}

    def fake_runner(*args, **kwargs):
        called["value"] += 1
        return np.asarray([expected_value, expected_value, expected_value], dtype=float)

    monkeypatch.setattr(forecasting_service, patched_symbol, fake_runner)

    payload = forecasting_service.generate_forecast(
        db=None,
        best_model_name=model_name,
        horizon=3,
        service_code="FIBRE",
        granularity="monthly",
        target_level="service",
        target_value=None,
    )

    values = [row["value"] for row in payload["forecast"]]
    assert values == [expected_value, expected_value, expected_value]
    assert called["value"] == 1
