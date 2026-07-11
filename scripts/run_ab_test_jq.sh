#!/bin/bash
#
# Comprehensive A/B test for prompt variants
# Runs control, A, B, C variants with both stable and dynamic forecasts
# Collects: confidence, answer length, variant, forecast type, response time
#

BASE_URL="http://localhost:8000"
OUTPUT_FILE="/home/habib/pfe/ab_test_full_results.json"

echo "=============================================================================="
echo "COMPREHENSIVE PROMPT VARIANT A/B TEST"
echo "=============================================================================="
echo ""

# Create results array
results_json="[]"

# Function to run a single test
run_test() {
    local variant=$1
    local forecast_type=$2
    local session_id="ab_test_${variant}_${forecast_type}"
    
    # Build payload based on forecast type
    if [ "$forecast_type" = "stable" ]; then
        payload=$(cat <<EOF
{
  "session_id": "$session_id",
  "service_type": "FIBRE",
  "question": "Pourquoi les valeurs demeurent stables?",
  "target_level": "service",
  "prompt_variant": "$variant",
  "forecast_payload": {
    "historical": [{"date":"2026-01-01","value":100},{"date":"2026-02-01","value":100},{"date":"2026-03-01","value":100},{"date":"2026-04-01","value":100},{"date":"2026-05-01","value":100},{"date":"2026-06-01","value":100}],
    "forecast": [{"date":"2026-07-01","value":100,"lower_bound":95,"upper_bound":105},{"date":"2026-08-01","value":100,"lower_bound":95,"upper_bound":105},{"date":"2026-09-01","value":100,"lower_bound":95,"upper_bound":105},{"date":"2026-10-01","value":100,"lower_bound":95,"upper_bound":105},{"date":"2026-11-01","value":100,"lower_bound":95,"upper_bound":105},{"date":"2026-12-01","value":100,"lower_bound":95,"upper_bound":105}],
    "metadata": {"model_used":"linear_regression","trend":"flat","change_pct":0}
  }
}
EOF
)
    else  # dynamic
        payload=$(cat <<EOF
{
  "session_id": "$session_id",
  "service_type": "FIBRE",
  "question": "Pourquoi la tendance semble monter fortement?",
  "target_level": "service",
  "prompt_variant": "$variant",
  "forecast_payload": {
    "historical": [{"date":"2026-01-01","value":80},{"date":"2026-02-01","value":85},{"date":"2026-03-01","value":90},{"date":"2026-04-01","value":95},{"date":"2026-05-01","value":100},{"date":"2026-06-01","value":105}],
    "forecast": [{"date":"2026-07-01","value":110,"lower_bound":105,"upper_bound":115},{"date":"2026-08-01","value":120,"lower_bound":115,"upper_bound":125},{"date":"2026-09-01","value":130,"lower_bound":125,"upper_bound":135},{"date":"2026-10-01","value":140,"lower_bound":135,"upper_bound":145},{"date":"2026-11-01","value":150,"lower_bound":145,"upper_bound":155},{"date":"2026-12-01","value":160,"lower_bound":155,"upper_bound":165}],
    "metadata": {"model_used":"linear_regression","trend":"up","change_pct":52.38}
  }
}
EOF
)
    fi
    
    # Run the test with timeout and measure time
    start_time=$(date +%s%N)
    
    response=$(curl -s -w "\n%{http_code}" -X POST "$BASE_URL/api/forecast/explain" \
        -H "Content-Type: application/json" \
        -d "$payload" \
        --max-time 120)
    
    end_time=$(date +%s%N)
    elapsed_ms=$(( (end_time - start_time) / 1000000 ))
    
    # Extract HTTP status and body
    http_status=$(echo "$response" | tail -1)
    response_body=$(echo "$response" | sed '$d')
    
    if [ "$http_status" = "200" ]; then
        # Parse response with jq
        confidence=$(echo "$response_body" | jq -r '.confidence // "N/A"' 2>/dev/null)
        answer_length=$(echo "$response_body" | jq -r '.answer | length // 0' 2>/dev/null)
        sources_count=$(echo "$response_body" | jq -r '.sources | length // 0' 2>/dev/null)
        answer_mode=$(echo "$response_body" | jq -r '.forecast_context.forecast_stable // "unknown"' 2>/dev/null)
        status="success"
    else
        confidence="N/A"
        answer_length=0
        sources_count=0
        answer_mode="error"
        status="error_${http_status}"
    fi
    
    # Create result record
    result=$(cat <<EOF
{
  "variant": "$variant",
  "forecast_type": "$forecast_type",
  "status": "$status",
  "http_status": $http_status,
  "confidence": "$confidence",
  "answer_length": $answer_length,
  "sources_count": $sources_count,
  "forecast_stable": "$answer_mode",
  "elapsed_ms": $elapsed_ms,
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
)
    
    # Add to results
    results_json=$(echo "$results_json" | jq ". += [$result]")
    
    # Print progress
    printf "  %-8s %-10s: %s (conf=%-6s time=%5dms)\n" \
        "$variant" "$forecast_type" "$status" "$confidence" "$elapsed_ms"
}

# Run tests for each variant and forecast type combination
echo "Running tests (this may take a few minutes)..."
echo ""

for variant in control A B C; do
    echo "Variant: $variant"
    for forecast_type in stable dynamic; do
        run_test "$variant" "$forecast_type"
        sleep 1  # Small delay between requests
    done
    echo ""
done

echo "=============================================================================="
echo "RESULTS SUMMARY"
echo "=============================================================================="
echo ""

# Save results
echo "$results_json" | jq '.' > "$OUTPUT_FILE"
echo "✓ Full results saved to: $OUTPUT_FILE"
echo ""

# Print summary table
echo "By Variant:"
echo "$results_json" | jq -r '.[] | select(.status == "success") | "\(.variant) (\(.forecast_type)): conf=\(.confidence | @json) len=\(.answer_length) ms=\(.elapsed_ms)"'
echo ""

echo "By Forecast Type:"
echo "Stable forecasts (heuristic path):"
echo "$results_json" | jq -r '.[] | select(.forecast_type == "stable" and .status == "success") | "  \(.variant): conf=\(.confidence) len=\(.answer_length) ms=\(.elapsed_ms)"'
echo ""
echo "Dynamic forecasts (LLM path):"
echo "$results_json" | jq -r '.[] | select(.forecast_type == "dynamic" and .status == "success") | "  \(.variant): conf=\(.confidence) len=\(.answer_length) ms=\(.elapsed_ms)"'
echo ""

# Summary stats
echo "Summary:"
echo "$results_json" | jq -r '"  Total runs: \(length) | Success: \(map(select(.status == "success")) | length) | Errors: \(map(select(.status != "success")) | length)"'

echo ""
echo "=============================================================================="
echo "Next steps:"
echo "  1. Review results: jq . < $OUTPUT_FILE"
echo "  2. For each variant, check variance in confidence and latency"
echo "  3. Identify which variant has best confidence + low latency"
echo "  4. Inspect Phoenix traces at http://localhost:6006 for chosen variant"
echo "=============================================================================="
