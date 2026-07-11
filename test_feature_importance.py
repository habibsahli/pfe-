#!/usr/bin/env python3
"""
Test script to verify feature importance extraction from trained models.
Tests the new feature importance functionality added to forecasting_service.py
"""

import sys
from pathlib import Path
import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression
from xgboost import XGBRegressor

# Add backend to path using relative path
BACKEND_PATH = Path(__file__).parent / "backend"
sys.path.insert(0, str(BACKEND_PATH))

from app.services.forecasting_service import (
    _extract_xgboost_importance,
    _extract_linear_importance,
    _extract_prophet_importance,
    _extract_importance_from_model,
)


def test_xgboost_importance():
    """Test XGBoost feature importance extraction"""
    print("=" * 60)
    print("TEST 1: XGBoost Feature Importance")
    print("=" * 60)
    
    # Create sample data
    X = pd.DataFrame({
        'promo_rate': np.random.rand(100),
        'month_sin': np.random.rand(100),
        'sales_lag_1': np.random.rand(100),
        'price_mean': np.random.rand(100),
        'dealers_active': np.random.rand(100),
    })
    y = X['promo_rate'] * 0.5 + X['month_sin'] * 0.3 + X['sales_lag_1'] * 0.2 + np.random.normal(0, 0.1, 100)
    
    # Train model
    model = XGBRegressor(n_estimators=50, max_depth=3, random_state=42)
    model.fit(X, y)
    
    # Extract importance
    importance_list = _extract_xgboost_importance(model, X.columns.tolist())
    
    print(f"\nExtracted {len(importance_list)} features")
    for item in importance_list:
        print(f"  {item['feature']:20s} | raw={item['importance']:8.4f} | normalized={item['normalized']:6.2f}%")
    
    assert len(importance_list) > 0, "Should extract at least one feature"
    assert all(0 <= item['normalized'] <= 100 for item in importance_list), "Normalized should be 0-100"
    print("✅ PASSED\n")


def test_linear_importance():
    """Test Linear Regression feature importance extraction"""
    print("=" * 60)
    print("TEST 2: Linear Regression Feature Importance")
    print("=" * 60)
    
    # Create sample data with clear coefficient magnitudes
    X = pd.DataFrame({
        'feature_1': np.random.rand(100),
        'feature_2': np.random.rand(100) * 0.1,  # Small importance
        'feature_3': np.random.rand(100),
    })
    y = X['feature_1'] * 5.0 + X['feature_2'] * 0.5 + X['feature_3'] * 3.0 + np.random.normal(0, 0.1, 100)
    
    # Train model
    model = LinearRegression()
    model.fit(X, y)
    
    # Extract importance
    importance_list = _extract_linear_importance(model, X.columns.tolist())
    
    print(f"\nExtracted {len(importance_list)} features")
    for item in importance_list:
        print(f"  {item['feature']:20s} | raw={item['importance']:8.4f} | normalized={item['normalized']:6.2f}%")
    
    assert len(importance_list) == 3, "Should extract all 3 features"
    assert importance_list[0]['normalized'] > importance_list[1]['normalized'], "First feature should be more important"
    print("✅ PASSED\n")


def test_prophet_importance():
    """Test Prophet component importance (heuristic)"""
    print("=" * 60)
    print("TEST 3: Prophet Component Importance (Heuristic)")
    print("=" * 60)
    
    importance_list = _extract_prophet_importance()
    
    print(f"\nExtracted {len(importance_list)} components")
    for item in importance_list:
        print(f"  {item['feature']:20s} | raw={item['importance']:8.4f} | normalized={item['normalized']:6.2f}%")
    
    assert len(importance_list) == 2, "Prophet heuristic should return trend + seasonality"
    assert all(item['normalized'] == 50.0 for item in importance_list), "Components should be equally weighted"
    print("✅ PASSED\n")


def test_model_dispatcher():
    """Test the _extract_importance_from_model dispatcher"""
    print("=" * 60)
    print("TEST 4: Model Type Dispatcher")
    print("=" * 60)
    
    # Test heuristic models
    test_cases = [
        ("naive_last", None, "recent_history"),
        ("seasonal_naive", None, "seasonality"),
        ("lstm", None, "temporal_patterns"),
        ("chronos", None, "temporal_patterns"),
    ]
    
    for model_name, model_obj, expected_feature in test_cases:
        importance = _extract_importance_from_model(model_name, model_obj, None)
        print(f"  {model_name:20s} → {len(importance)} factors, first: {importance[0]['feature']}")
        assert any(expected_feature in item['feature'].lower() for item in importance), \
            f"Expected '{expected_feature}' in {model_name} importance"
    
    print("✅ PASSED\n")


def test_normalization():
    """Test that importance scores normalize to ~100%"""
    print("=" * 60)
    print("TEST 5: Importance Normalization")
    print("=" * 60)
    
    # Create XGBoost model
    X = pd.DataFrame({
        'f1': np.random.rand(50),
        'f2': np.random.rand(50),
        'f3': np.random.rand(50),
    })
    y = X['f1'] * 0.6 + X['f2'] * 0.3 + X['f3'] * 0.1 + np.random.normal(0, 0.05, 50)
    
    model = XGBRegressor(n_estimators=20, max_depth=2, random_state=42)
    model.fit(X, y)
    
    importance_list = _extract_xgboost_importance(model, X.columns.tolist())
    total_normalized = sum(item['normalized'] for item in importance_list)
    
    print(f"\nTotal normalized importance: {total_normalized:.2f}%")
    print("Feature breakdown:")
    for item in importance_list:
        print(f"  {item['feature']:20s} {item['normalized']:6.2f}%")
    
    # Normalized scores should sum to ~100 (or close to it for top-k selection)
    assert 95 < total_normalized <= 100, f"Should normalize close to 100%, got {total_normalized:.2f}%"
    print("✅ PASSED\n")


if __name__ == "__main__":
    print("\n" + "="*60)
    print("FEATURE IMPORTANCE EXTRACTION TEST SUITE")
    print("="*60 + "\n")
    
    try:
        test_xgboost_importance()
        test_linear_importance()
        test_prophet_importance()
        test_model_dispatcher()
        test_normalization()
        
        print("\n" + "="*60)
        print("✅ ALL TESTS PASSED!")
        print("="*60 + "\n")
        print("Feature importance extraction is working correctly.")
        print("\nNext: Test API endpoint integration with training pipeline")
        
    except AssertionError as e:
        print(f"\n❌ TEST FAILED: {e}\n")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ ERROR: {e}\n")
        import traceback
        traceback.print_exc()
        sys.exit(1)
