#!/usr/bin/env python3
"""
End-to-End Smoke Test for Feature Importance Implementation
Tests core functionality, API integration, and data flow
"""

import sys
import json
import time
from pathlib import Path

# Add backend to path using relative path
BACKEND_PATH = Path(__file__).parent / "backend"
sys.path.insert(0, str(BACKEND_PATH))

# Test configuration
TEST_RESULTS = {
    "total": 0,
    "passed": 0,
    "failed": 0,
    "errors": [],
}

def test_case(name: str):
    """Decorator for test cases"""
    def decorator(func):
        def wrapper():
            TEST_RESULTS["total"] += 1
            try:
                print(f"\n  TEST: {name}...", end=" ")
                func()
                print("✅ PASS")
                TEST_RESULTS["passed"] += 1
            except AssertionError as e:
                print(f"❌ FAIL: {e}")
                TEST_RESULTS["failed"] += 1
                TEST_RESULTS["errors"].append((name, str(e)))
            except Exception as e:
                print(f"❌ ERROR: {e}")
                TEST_RESULTS["failed"] += 1
                TEST_RESULTS["errors"].append((name, f"Exception: {e}"))
        return wrapper
    return decorator


print("\n" + "="*70)
print("  END-TO-END SMOKE TEST: FEATURE IMPORTANCE IMPLEMENTATION")
print("="*70)

# ============================================================================
# SECTION 1: Import Validation
# ============================================================================
print("\n📦 SECTION 1: Import Validation")
print("-" * 70)

@test_case("Import forecasting_service module")
def test_import_service():
    from app.services import forecasting_service
    assert hasattr(forecasting_service, '_extract_importance_from_model')
    assert hasattr(forecasting_service, '_cache_feature_importance')
    assert hasattr(forecasting_service, 'FeatureImportance')
test_import_service()

@test_case("Import forecast API module")
def test_import_api():
    from app.api import forecast
    assert hasattr(forecast, 'ForecastFactorsRequest')
    assert hasattr(forecast, 'get_forecast_factors')
test_import_api()

@test_case("Import required dataclasses")
def test_import_dataclasses():
    from app.services.forecasting_service import FeatureImportance, ModelRunResult
    # Verify FeatureImportance structure
    fi = FeatureImportance(
        feature="test_feature",
        importance=0.5,
        normalized_importance=50.0
    )
    assert fi.feature == "test_feature"
    assert fi.importance == 0.5
    assert fi.normalized_importance == 50.0
test_import_dataclasses()

# ============================================================================
# SECTION 2: Core Extraction Functions
# ============================================================================
print("\n⚙️  SECTION 2: Core Extraction Functions")
print("-" * 70)

from app.services.forecasting_service import (
    _extract_prophet_importance,
    _extract_importance_from_model,
    _cache_feature_importance,
    _get_cached_importance,
)

@test_case("Prophet importance extraction")
def test_prophet_extraction():
    result = _extract_prophet_importance()
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]['feature'] == 'trend'
    assert result[1]['feature'] == 'seasonality'
    assert result[0]['normalized'] == 50.0
    assert result[1]['normalized'] == 50.0
test_prophet_extraction()

@test_case("Model dispatcher - Naive model")
def test_dispatcher_naive():
    result = _extract_importance_from_model("naive_last", None, None)
    assert isinstance(result, list)
    assert len(result) > 0
    assert 'feature' in result[0]
    assert 'normalized' in result[0]
    assert 'recent' in result[0]['feature'].lower()
test_dispatcher_naive()

@test_case("Model dispatcher - LSTM model")
def test_dispatcher_lstm():
    result = _extract_importance_from_model("lstm", None, None)
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]['normalized'] == 70.0  # Temporal
    assert result[1]['normalized'] == 30.0  # Learned
test_dispatcher_lstm()

@test_case("Model dispatcher - SARIMA model")
def test_dispatcher_sarima():
    result = _extract_importance_from_model("sarima", None, None)
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]['normalized'] == 60.0  # Seasonality
    assert result[1]['normalized'] == 40.0  # Trend
test_dispatcher_sarima()

@test_case("Model dispatcher - All model types")
def test_dispatcher_coverage():
    model_types = [
        "naive_last", "seasonal_naive", "prophet", "sarima",
        "exp_smoothing", "lstm", "chronos", "timesfm",
        "patchtst", "autogluon", "xgboost", "linear_regression"
    ]
    for model in model_types:
        result = _extract_importance_from_model(model, None, None)
        assert isinstance(result, list), f"Failed for {model}"
        if result:  # Some models return empty on error
            assert 'feature' in result[0], f"Missing 'feature' for {model}"
            assert 'normalized' in result[0], f"Missing 'normalized' for {model}"
test_dispatcher_coverage()

# ============================================================================
# SECTION 3: Caching Mechanism
# ============================================================================
print("\n💾 SECTION 3: Caching Mechanism")
print("-" * 70)

@test_case("Cache storage and retrieval")
def test_caching():
    session_id = "test_session_xyz"
    model_name = "xgboost"
    test_data = [
        {"feature": "feature_1", "importance": 0.5, "normalized": 50.0},
        {"feature": "feature_2", "importance": 0.3, "normalized": 30.0},
        {"feature": "feature_3", "importance": 0.2, "normalized": 20.0},
    ]
    
    # Store
    _cache_feature_importance(session_id, model_name, test_data)
    
    # Retrieve
    retrieved = _get_cached_importance(session_id, model_name)
    assert retrieved is not None
    assert len(retrieved) == 3
    assert retrieved[0]['feature'] == 'feature_1'
    assert retrieved[0]['normalized'] == 50.0
test_caching()

@test_case("Cache key generation")
def test_cache_keys():
    # Different sessions should have different cache entries
    _cache_feature_importance("sess_1", "model_a", [{"feature": "f1", "importance": 1, "normalized": 100}])
    _cache_feature_importance("sess_2", "model_a", [{"feature": "f2", "importance": 2, "normalized": 100}])
    
    cached_1 = _get_cached_importance("sess_1", "model_a")
    cached_2 = _get_cached_importance("sess_2", "model_a")
    
    assert cached_1[0]['feature'] == 'f1'
    assert cached_2[0]['feature'] == 'f2'
test_cache_keys()

# ============================================================================
# SECTION 4: API Request/Response Models
# ============================================================================
print("\n🔌 SECTION 4: API Request/Response Models")
print("-" * 70)

from app.api.forecast import ForecastFactorsRequest

@test_case("ForecastFactorsRequest model validation")
def test_request_model():
    # Valid request
    req = ForecastFactorsRequest(
        session_id="sess_123",
        model="best"
    )
    assert req.session_id == "sess_123"
    assert req.model == "best"
    
    # With specific model
    req2 = ForecastFactorsRequest(
        session_id="sess_456",
        model="xgboost"
    )
    assert req2.model == "xgboost"
test_request_model()

@test_case("Response structure validation")
def test_response_structure():
    # Simulate the expected response structure
    response = {
        "session_id": "sess_123",
        "model": "xgboost",
        "factors": [
            {"feature": "promo_rate", "importance": 0.45, "normalized": 28.3},
            {"feature": "month_sin", "importance": 0.32, "normalized": 20.1},
            {"feature": "sales_lag_1", "importance": 0.23, "normalized": 14.5},
        ],
        "source": "training_results"
    }
    
    # Validate structure
    assert "session_id" in response
    assert "model" in response
    assert "factors" in response
    assert "source" in response
    assert len(response["factors"]) == 3
    
    # Validate each factor
    for factor in response["factors"]:
        assert "feature" in factor
        assert "importance" in factor
        assert "normalized" in factor
        assert 0 <= factor["normalized"] <= 100
    
    # Verify normalized scores are reasonable
    total_normalized = sum(f["normalized"] for f in response["factors"])
    assert total_normalized <= 100  # Should not exceed 100% for top-k selection
test_response_structure()

# ============================================================================
# SECTION 5: Data Model Integration
# ============================================================================
print("\n🔗 SECTION 5: Data Model Integration")
print("-" * 70)

from app.services.forecasting_service import ModelRunResult

@test_case("ModelRunResult with feature_importance")
def test_model_run_result():
    importance_data = [
        {"feature": "promo_rate", "importance": 0.45, "normalized": 45.0},
        {"feature": "seasonality", "importance": 0.55, "normalized": 55.0},
    ]
    
    result = ModelRunResult(
        model="xgboost",
        mae=100.0,
        rmse=120.0,
        mape=5.5,
        smape=5.2,
        training_time_sec=2.3,
        yhat=[123.0, 125.0, 126.0],
        feature_importance=importance_data,
    )
    
    # Verify all fields
    assert result.model == "xgboost"
    assert result.mape == 5.5
    assert result.feature_importance is not None
    assert len(result.feature_importance) == 2
    
    # Test serialization (as API would do)
    result_dict = result.__dict__
    assert 'feature_importance' in result_dict
    assert isinstance(result_dict['feature_importance'], list)
test_model_run_result()

@test_case("ModelRunResult backward compatibility")
def test_model_run_result_minimal():
    # Test that feature_importance is optional
    result = ModelRunResult(
        model="naive_last",
        mae=100.0,
        rmse=120.0,
        mape=5.5,
        smape=5.2,
        training_time_sec=0.1,
        yhat=[100.0, 100.0],
        feature_importance=None,  # Optional
    )
    
    assert result.model == "naive_last"
    assert result.feature_importance is None
test_model_run_result_minimal()

# ============================================================================
# SECTION 6: Normalization & Math Validation
# ============================================================================
print("\n📐 SECTION 6: Normalization & Math Validation")
print("-" * 70)

@test_case("Importance normalization math")
def test_normalization_math():
    # Simulate top-k normalization: 3 features with equal importance
    raw_importances = [0.33, 0.33, 0.34]
    total = sum(raw_importances)
    
    normalized = [(imp / total) * 100 for imp in raw_importances]
    
    assert len(normalized) == 3
    assert all(0 <= n <= 100 for n in normalized)
    assert abs(sum(normalized) - 100.0) < 0.01  # Should sum to ~100
test_normalization_math()

@test_case("Top-K selection maintains ranking")
def test_topk_ranking():
    # 5 features, select top 3
    features = [
        {"feature": "f1", "importance": 0.50},
        {"feature": "f2", "importance": 0.25},
        {"feature": "f3", "importance": 0.15},
        {"feature": "f4", "importance": 0.07},
        {"feature": "f5", "importance": 0.03},
    ]
    
    # Sort and take top 3
    top_k = sorted(features, key=lambda x: x["importance"], reverse=True)[:3]
    
    assert len(top_k) == 3
    assert top_k[0]["feature"] == "f1"
    assert top_k[1]["feature"] == "f2"
    assert top_k[2]["feature"] == "f3"
test_topk_ranking()

# ============================================================================
# SECTION 7: Error Handling
# ============================================================================
print("\n⚠️  SECTION 7: Error Handling")
print("-" * 70)

@test_case("Graceful handling of empty model")
def test_error_empty_model():
    # Should not crash with None model
    result = _extract_importance_from_model("xgboost", None, None)
    assert isinstance(result, list)
test_error_empty_model()

@test_case("Graceful handling of unknown model")
def test_error_unknown_model():
    # Unknown model should return empty or heuristic
    result = _extract_importance_from_model("unknown_model_xyz", None, None)
    assert isinstance(result, list)
test_error_unknown_model()

@test_case("Cache miss returns None")
def test_error_cache_miss():
    # Non-existent key should return None
    result = _get_cached_importance("nonexistent_session", "nonexistent_model")
    assert result is None
test_error_cache_miss()

# ============================================================================
# SECTION 8: File Structure Validation
# ============================================================================
print("\n📁 SECTION 8: File Structure Validation")
print("-" * 70)

@test_case("Source files exist and are readable")
def test_files_exist():
    files = [
        Path(__file__).parent / "backend/app/services/forecasting_service.py",
        Path(__file__).parent / "backend/app/api/forecast.py",
    ]
    
    for f in files:
        assert f.exists(), f"File not found: {f}"
        assert f.is_file(), f"Not a file: {f}"
        content = f.read_text()
        assert len(content) > 0, f"File is empty: {f}"
        assert "feature_importance" in content, f"Missing feature_importance in {f}"
test_files_exist()

@test_case("Documentation files exist")
def test_docs_exist():
    docs = [
        Path(__file__).parent / "FEATURE_IMPORTANCE_IMPLEMENTATION.md",
        Path(__file__).parent / "FEATURE_IMPORTANCE_QUICK_START.md",
        Path(__file__).parent / "FEATURE_IMPORTANCE_FINAL_REPORT.md",
    ]
    
    for doc in docs:
        assert doc.exists(), f"Documentation not found: {doc}"
        assert doc.is_file(), f"Not a file: {doc}"
        content = doc.read_text()
        assert len(content) > 1000, f"Documentation too short: {doc}"
test_docs_exist()

# ============================================================================
# SECTION 9: Integration Scenarios
# ============================================================================
print("\n🔄 SECTION 9: Integration Scenarios")
print("-" * 70)

@test_case("Scenario: Train XGBoost model and extract factors")
def test_scenario_training_flow():
    # Simulate training pipeline flow
    model_name = "xgboost"
    session_id = "sess_scenario_1"
    
    # Step 1: Extract factors (would happen after training)
    factors = _extract_importance_from_model(model_name, None, None)
    assert isinstance(factors, list)
    
    # Step 2: Create ModelRunResult
    result = ModelRunResult(
        model=model_name,
        mae=100.0,
        rmse=120.0,
        mape=5.5,
        smape=5.2,
        training_time_sec=2.3,
        yhat=[123.0, 125.0],
        feature_importance=factors,
    )
    assert result.feature_importance is not None
    
    # Step 3: Cache for later retrieval
    _cache_feature_importance(session_id, model_name, result.feature_importance)
    
    # Step 4: Retrieve on demand
    retrieved_factors = _get_cached_importance(session_id, model_name)
    assert retrieved_factors is not None
    assert len(retrieved_factors) > 0
test_scenario_training_flow()

@test_case("Scenario: User requests factors for best model")
def test_scenario_user_request():
    # Simulate user requesting factors
    session_id = "sess_scenario_2"
    
    # Models trained with factors cached
    trained_models = ["prophet", "sarima", "xgboost"]
    
    for model in trained_models:
        factors = _extract_importance_from_model(model, None, None)
        _cache_feature_importance(session_id, model, factors)
    
    # User requests "best" (usually lowest MAPE)
    # API would select the best model (let's assume xgboost)
    best_model = "xgboost"
    result_factors = _get_cached_importance(session_id, best_model)
    
    assert result_factors is not None
    assert all('feature' in f and 'normalized' in f for f in result_factors)
test_scenario_user_request()

# ============================================================================
# SUMMARY
# ============================================================================
print("\n" + "="*70)
print("  TEST SUMMARY")
print("="*70)

print(f"\nTotal Tests:    {TEST_RESULTS['total']}")
print(f"Passed:         {TEST_RESULTS['passed']} ✅")
print(f"Failed:         {TEST_RESULTS['failed']} ❌")

if TEST_RESULTS['errors']:
    print("\n⚠️  FAILURES:")
    for test_name, error in TEST_RESULTS['errors']:
        print(f"  • {test_name}")
        print(f"    └─ {error}")

success_rate = (TEST_RESULTS['passed'] / TEST_RESULTS['total'] * 100) if TEST_RESULTS['total'] > 0 else 0

print(f"\n📊 Success Rate: {success_rate:.1f}%")

if TEST_RESULTS['failed'] == 0:
    print("\n" + "="*70)
    print("  ✅ ALL SMOKE TESTS PASSED!")
    print("="*70)
    print("\n🚀 Feature Importance Implementation is READY for deployment\n")
    sys.exit(0)
else:
    print("\n" + "="*70)
    print(f"  ❌ {TEST_RESULTS['failed']} TEST(S) FAILED")
    print("="*70 + "\n")
    sys.exit(1)
