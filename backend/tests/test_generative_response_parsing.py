"""Unit tests for external generative response parsing helpers."""

from app.services.forecasting_service import _extract_numeric_series


def test_extract_numeric_series_from_plain_list():
    payload = [1, 2.5, 3]
    values = _extract_numeric_series(payload, ["forecast", "value"])
    assert values == [1.0, 2.5, 3.0]


def test_extract_numeric_series_from_dict_list_key():
    payload = {"forecast": [10, 11, 12]}
    values = _extract_numeric_series(payload, ["forecast", "yhat"])
    assert values == [10.0, 11.0, 12.0]


def test_extract_numeric_series_from_list_of_dicts():
    payload = [{"TimeGPT": 4.2}, {"TimeGPT": 4.8}, {"TimeGPT": 5.0}]
    values = _extract_numeric_series(payload, ["TimeGPT", "value"])
    assert values == [4.2, 4.8, 5.0]


def test_extract_numeric_series_from_nested_results():
    payload = {"results": [{"prediction": 7}, {"prediction": 8}]}
    values = _extract_numeric_series(payload, ["forecast", "prediction"])
    assert values == [7.0, 8.0]
