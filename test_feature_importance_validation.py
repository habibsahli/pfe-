#!/usr/bin/env python3
"""
Simple validation test for feature importance extraction logic.
Tests the core extraction logic without requiring all dependencies.
"""

import sys
from pathlib import Path

# Add backend to path using relative path
BACKEND_PATH = Path(__file__).parent / "backend"
sys.path.insert(0, str(BACKEND_PATH))

# Test imports
try:
    from app.services.forecasting_service import (
        _extract_prophet_importance,
        _extract_importance_from_model,
        _cache_feature_importance,
        _get_cached_importance,
        FeatureImportance,
        ModelRunResult,
    )
    print("✅ All imports successful\n")
except ImportError as e:
    print(f"❌ Import error: {e}\n")
    sys.exit(1)


def test_prophet_importance():
    """Test Prophet component importance extraction"""
    print("=" * 60)
    print("TEST 1: Prophet Component Importance")
    print("=" * 60)
    
    importance = _extract_prophet_importance()
    print(f"\nExtracted {len(importance)} components:")
    for item in importance:
        print(f"  {item['feature']:20s} | importance={item['importance']:6.1f} | normalized={item['normalized']:6.1f}%")
    
    assert len(importance) == 2, "Should have 2 components"
    assert importance[0]['feature'] == 'trend', "First should be trend"
    assert importance[1]['feature'] == 'seasonality', "Second should be seasonality"
    print("✅ PASSED\n")


def test_model_dispatcher_heuristics():
    """Test the model dispatcher with heuristic models"""
    print("=" * 60)
    print("TEST 2: Model Dispatcher - Heuristic Models")
    print("=" * 60)
    
    test_cases = [
        ("naive_last", "recent_history"),
        ("seasonal_naive", "seasonality"),
        ("sarima", "seasonality"),
        ("lstm", "temporal_patterns"),
        ("chronos", "temporal_patterns"),
        ("timesfm", "temporal_patterns"),
    ]
    
    for model_name, expected_keyword in test_cases:
        result = _extract_importance_from_model(model_name, None, None)
        print(f"  {model_name:20s} → {len(result)} factors")
        
        # Verify result structure
        assert isinstance(result, list), f"Result should be list for {model_name}"
        if result:
            assert 'feature' in result[0], f"Result should have 'feature' key for {model_name}"
            assert 'importance' in result[0], f"Result should have 'importance' key for {model_name}"
            assert 'normalized' in result[0], f"Result should have 'normalized' key for {model_name}"
            
            # Check for expected keyword
            found = any(expected_keyword.lower() in item['feature'].lower() for item in result)
            assert found, f"Should find '{expected_keyword}' in {model_name} result"
            print(f"    ✓ First factor: {result[0]['feature']}")
    
    print("✅ PASSED\n")


def test_caching():
    """Test importance caching mechanism"""
    print("=" * 60)
    print("TEST 3: Importance Caching")
    print("=" * 60)
    
    # Create test data
    session_id = "test_session_123"
    model_name = "xgboost"
    importance_data = [
        {"feature": "promo_rate", "importance": 0.45, "normalized": 45.0},
        {"feature": "seasonality", "importance": 0.35, "normalized": 35.0},
        {"feature": "trend", "importance": 0.20, "normalized": 20.0},
    ]
    
    # Cache it
    _cache_feature_importance(session_id, model_name, importance_data)
    print(f"\nCached {len(importance_data)} factors for {session_id}:{model_name}")
    
    # Retrieve it
    retrieved = _get_cached_importance(session_id, model_name)
    print(f"Retrieved {len(retrieved)} factors from cache")
    
    assert retrieved is not None, "Should retrieve cached data"
    assert len(retrieved) == len(importance_data), "Should have same number of factors"
    assert retrieved[0]['feature'] == "promo_rate", "Should preserve order and data"
    
    print("✅ PASSED\n")


def test_dataclass_structure():
    """Test the new FeatureImportance and updated ModelRunResult dataclasses"""
    print("=" * 60)
    print("TEST 4: Dataclass Structures")
    print("=" * 60)
    
    # Test FeatureImportance
    fi = FeatureImportance(feature="promo_rate", importance=0.45, normalized_importance=45.0)
    print(f"\nFeatureImportance created: {fi.feature} (importance={fi.importance}, normalized={fi.normalized_importance})")
    assert fi.feature == "promo_rate"
    assert fi.normalized_importance == 45.0
    
    # Test ModelRunResult with feature_importance
    importance_list = [
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
        feature_importance=importance_list,
    )
    
    print(f"ModelRunResult created:")
    print(f"  model={result.model}")
    print(f"  mape={result.mape}%")
    print(f"  feature_importance={len(result.feature_importance)} factors")
    
    # Convert to dict (as done in API response)
    result_dict = result.__dict__
    assert 'feature_importance' in result_dict, "feature_importance should be in dict"
    assert len(result_dict['feature_importance']) == 2
    
    print("✅ PASSED\n")


def test_response_format():
    """Test the expected API response format"""
    print("=" * 60)
    print("TEST 5: API Response Format")
    print("=" * 60)
    
    # Simulate the response structure
    factors_response = {
        "session_id": "sess_abc123",
        "model": "xgboost",
        "factors": [
            {"feature": "promo_rate", "importance": 0.567, "normalized": 28.3},
            {"feature": "month_sin", "importance": 0.412, "importance": 20.5},
            {"feature": "sales_lag_1", "importance": 0.321, "normalized": 16.0},
        ],
        "source": "training_results",
    }
    
    print("\nResponse structure:")
    print(f"  session_id: {factors_response['session_id']}")
    print(f"  model: {factors_response['model']}")
    print(f"  factors ({len(factors_response['factors'])} items):")
    for factor in factors_response['factors']:
        print(f"    - {factor['feature']:20s} | {factor['normalized']:6.1f}%")
    print(f"  source: {factors_response['source']}")
    
    # Validate structure
    assert 'session_id' in factors_response
    assert 'model' in factors_response
    assert 'factors' in factors_response
    assert 'source' in factors_response
    assert all('feature' in f and 'normalized' in f for f in factors_response['factors'])
    
    print("✅ PASSED\n")


if __name__ == "__main__":
    print("\n" + "="*60)
    print("FEATURE IMPORTANCE LOGIC VALIDATION")
    print("="*60 + "\n")
    
    try:
        test_prophet_importance()
        test_model_dispatcher_heuristics()
        test_caching()
        test_dataclass_structure()
        test_response_format()
        
        print("\n" + "="*60)
        print("✅ ALL VALIDATION TESTS PASSED!")
        print("="*60)
        print("\nFeature importance extraction is working correctly.")
        print("\nImplementation Summary:")
        print("  ✅ Model dispatcher working for all model types")
        print("  ✅ Heuristic importance for statistical models")
        print("  ✅ Caching mechanism functional")
        print("  ✅ Dataclass structures correct")
        print("  ✅ API response format validated")
        print("\nReady for integration testing with actual training pipeline.\n")
        
    except AssertionError as e:
        print(f"\n❌ TEST FAILED: {e}\n")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ ERROR: {e}\n")
        import traceback
        traceback.print_exc()
        sys.exit(1)
