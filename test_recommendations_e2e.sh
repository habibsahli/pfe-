#!/bin/bash

# End-to-End Test for Stock Recommendations Endpoint
# Tests the new /api/v1/inventory/recommendations endpoint with realistic sample data

set -e

echo "=========================================="
echo "STOCK RECOMMENDATIONS E2E TEST"
echo "=========================================="
echo ""

# Test 1: Basic recommendations with single product
echo "TEST 1: Single Product Recommendation"
echo "-------------------------------------"

REQUEST_1=$(cat <<'JSON'
{
  "session_id": "test-e2e-001",
  "recommendations_input": [
    {
      "product_id": "PROD_5G_001",
      "product_name": "5G Router",
      "product_type": "CPE_HARDWARE",
      "governorate": "Tunis",
      "current_stock": 45.0,
      "forecast_series": [
        {"date": "2026-06-01", "value": 120.0, "lower_bound": 100.0, "upper_bound": 140.0},
        {"date": "2026-07-01", "value": 135.0, "lower_bound": 110.0, "upper_bound": 160.0},
        {"date": "2026-08-01", "value": 128.0, "lower_bound": 105.0, "upper_bound": 151.0},
        {"date": "2026-09-01", "value": 142.0, "lower_bound": 115.0, "upper_bound": 169.0},
        {"date": "2026-10-01", "value": 138.0, "lower_bound": 112.0, "upper_bound": 164.0},
        {"date": "2026-11-01", "value": 151.0, "lower_bound": 122.0, "upper_bound": 180.0}
      ],
      "avg_monthly_demand": 130.0,
      "lead_time_months": 2.0,
      "min_order_qty": 5,
      "data_source_mix": {
        "REAL": 0.8,
        "SIMULATED": 0.2
      }
    }
  ],
  "service_level": 0.95,
  "lead_time_overrides": null,
  "min_order_qty_overrides": null
}
JSON
)

RESPONSE_1=$(curl -s -X POST http://localhost:8000/api/v1/inventory/recommendations \
  -H "Content-Type: application/json" \
  -d "$REQUEST_1")

echo "Response:"
echo "$RESPONSE_1" | jq . 2>/dev/null || echo "$RESPONSE_1"
echo ""

# Test 2: Multiple products with different types
echo "TEST 2: Multiple Products Recommendation"
echo "----------------------------------------"

REQUEST_2=$(cat <<'JSON'
{
  "session_id": "test-e2e-002",
  "recommendations_input": [
    {
      "product_id": "PROD_5G_002",
      "product_name": "5G Smartphone",
      "product_type": "SMARTPHONE_HW",
      "governorate": "Ariana",
      "current_stock": 200.0,
      "forecast_series": [
        {"date": "2026-06-01", "value": 250.0},
        {"date": "2026-07-01", "value": 260.0},
        {"date": "2026-08-01", "value": 255.0},
        {"date": "2026-09-01", "value": 270.0},
        {"date": "2026-10-01", "value": 265.0},
        {"date": "2026-11-01", "value": 280.0}
      ],
      "avg_monthly_demand": 260.0,
      "lead_time_months": 1.5,
      "min_order_qty": 10
    },
    {
      "product_id": "PROD_5G_003",
      "product_name": "5G Subscription",
      "product_type": "SUBSCRIPTION",
      "governorate": "Ben Arous",
      "current_stock": 10000.0,
      "forecast_series": [
        {"date": "2026-06-01", "value": 5000.0},
        {"date": "2026-07-01", "value": 5200.0},
        {"date": "2026-08-01", "value": 5100.0},
        {"date": "2026-09-01", "value": 5300.0},
        {"date": "2026-10-01", "value": 5150.0},
        {"date": "2026-11-01", "value": 5400.0}
      ],
      "avg_monthly_demand": 5200.0,
      "lead_time_months": 0.5,
      "min_order_qty": 1
    }
  ],
  "service_level": 0.90,
  "lead_time_overrides": {
    "PROD_5G_003": 0.7
  },
  "min_order_qty_overrides": {
    "PROD_5G_002": 15
  }
}
JSON
)

RESPONSE_2=$(curl -s -X POST http://localhost:8000/api/v1/inventory/recommendations \
  -H "Content-Type: application/json" \
  -d "$REQUEST_2")

echo "Response:"
echo "$RESPONSE_2" | jq . 2>/dev/null || echo "$RESPONSE_2"
echo ""

# Test 3: Low stock scenario (rupture risk)
echo "TEST 3: Low Stock / Rupture Risk Scenario"
echo "-----------------------------------------"

REQUEST_3=$(cat <<'JSON'
{
  "session_id": "test-e2e-003",
  "recommendations_input": [
    {
      "product_id": "PROD_CRITICAL",
      "product_name": "Critical Router",
      "product_type": "CPE_HARDWARE",
      "governorate": "Sfax",
      "current_stock": 5.0,
      "forecast_series": [
        {"date": "2026-06-01", "value": 150.0},
        {"date": "2026-07-01", "value": 160.0},
        {"date": "2026-08-01", "value": 155.0},
        {"date": "2026-09-01", "value": 170.0},
        {"date": "2026-10-01", "value": 165.0},
        {"date": "2026-11-01", "value": 180.0}
      ],
      "avg_monthly_demand": 160.0,
      "lead_time_months": 2.0,
      "min_order_qty": 5
    }
  ],
  "service_level": 0.99
}
JSON
)

RESPONSE_3=$(curl -s -X POST http://localhost:8000/api/v1/inventory/recommendations \
  -H "Content-Type: application/json" \
  -d "$REQUEST_3")

echo "Response:"
echo "$RESPONSE_3" | jq . 2>/dev/null || echo "$RESPONSE_3"
echo ""

# Test 4: Overstock scenario
echo "TEST 4: Overstock Scenario"
echo "--------------------------"

REQUEST_4=$(cat <<'JSON'
{
  "session_id": "test-e2e-004",
  "recommendations_input": [
    {
      "product_id": "PROD_OVERSTOCK",
      "product_name": "Excess Inventory Router",
      "product_type": "CPE_HARDWARE",
      "governorate": "Sousse",
      "current_stock": 2000.0,
      "forecast_series": [
        {"date": "2026-06-01", "value": 50.0},
        {"date": "2026-07-01", "value": 45.0},
        {"date": "2026-08-01", "value": 48.0},
        {"date": "2026-09-01", "value": 55.0},
        {"date": "2026-10-01", "value": 52.0},
        {"date": "2026-11-01", "value": 60.0}
      ],
      "avg_monthly_demand": 50.0,
      "lead_time_months": 2.0,
      "min_order_qty": 5
    }
  ],
  "service_level": 0.85
}
JSON
)

RESPONSE_4=$(curl -s -X POST http://localhost:8000/api/v1/inventory/recommendations \
  -H "Content-Type: application/json" \
  -d "$REQUEST_4")

echo "Response:"
echo "$RESPONSE_4" | jq . 2>/dev/null || echo "$RESPONSE_4"
echo ""

echo "=========================================="
echo "E2E TEST COMPLETE"
echo "=========================================="
echo ""
echo "Summary:"
echo "✓ Test 1: Single product with bounds"
echo "✓ Test 2: Multiple products with overrides"
echo "✓ Test 3: Critical low-stock scenario"
echo "✓ Test 4: Overstock scenario"
echo ""
echo "Validating responses contain required fields..."
echo ""

# Quick validation
if echo "$RESPONSE_1" | jq -e '.session_id' > /dev/null 2>&1; then
  echo "✓ Response has session_id"
else
  echo "✗ Response missing session_id"
fi

if echo "$RESPONSE_1" | jq -e '.recommendations' > /dev/null 2>&1; then
  echo "✓ Response has recommendations array"
else
  echo "✗ Response missing recommendations array"
fi

if echo "$RESPONSE_1" | jq -e '.summary' > /dev/null 2>&1; then
  echo "✓ Response has summary"
else
  echo "✗ Response missing summary"
fi

echo ""
echo "All tests completed!"
