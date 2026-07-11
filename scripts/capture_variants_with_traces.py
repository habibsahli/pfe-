#!/usr/bin/env python3
"""
Capture Q&A responses for all prompt variants and then fetch corresponding Phoenix traces.
"""
import json
import requests
import time
from datetime import datetime

BASE_URL = "http://localhost:8000"
PHOENIX_URL = "http://localhost:6006"

# Payload with upward trend - should force LLM generation (not stable)
payload_base = {
    "session_id": "ab_test_variant_run",
    "service_type": "FIBRE",
    "question": "Pourquoi la tendance semble monter fortement ?",
    "target_level": "service",
    "forecast_payload": {
        "historical": [
            {"date": "2026-01-01", "value": 80},
            {"date": "2026-02-01", "value": 85},
            {"date": "2026-03-01", "value": 90},
            {"date": "2026-04-01", "value": 95},
            {"date": "2026-05-01", "value": 100},
            {"date": "2026-06-01", "value": 105}
        ],
        "forecast": [
            {"date": "2026-07-01", "value": 110, "lower_bound": 105, "upper_bound": 115},
            {"date": "2026-08-01", "value": 120, "lower_bound": 115, "upper_bound": 125},
            {"date": "2026-09-01", "value": 130, "lower_bound": 125, "upper_bound": 135},
            {"date": "2026-10-01", "value": 140, "lower_bound": 135, "upper_bound": 145},
            {"date": "2026-11-01", "value": 150, "lower_bound": 145, "upper_bound": 155},
            {"date": "2026-12-01", "value": 160, "lower_bound": 155, "upper_bound": 165}
        ],
        "metadata": {"model_used": "linear_regression", "trend": "up", "change_pct": 52.38}
    }
}

variants = ["control", "A", "B", "C"]
results = {}

print("=" * 80)
print("RUNNING VARIANT COMPARISON TEST")
print("=" * 80)

# Step 1: Run all variants
for variant in variants:
    print(f"\n[{datetime.now().isoformat()}] Running variant: {variant}")
    payload = dict(payload_base)
    payload["prompt_variant"] = variant
    
    try:
        start_time = time.time()
        response = requests.post(
            f"{BASE_URL}/api/forecast/explain",
            json=payload,
            timeout=60
        )
        elapsed = time.time() - start_time
        
        if response.status_code == 200:
            data = response.json()
            results[variant] = {
                "status": "success",
                "response": data,
                "elapsed_seconds": round(elapsed, 2),
                "timestamp": datetime.now().isoformat()
            }
            print(f"  ✓ Success ({elapsed:.2f}s)")
            print(f"    Answer preview: {data.get('answer', '')[:150]}...")
            print(f"    Confidence: {data.get('confidence', 'N/A')}")
            print(f"    Sources: {len(data.get('sources', []))} found")
        else:
            results[variant] = {
                "status": "error",
                "status_code": response.status_code,
                "error": response.text[:500]
            }
            print(f"  ✗ Error: {response.status_code}")
    except Exception as e:
        results[variant] = {
            "status": "exception",
            "error": str(e)
        }
        print(f"  ✗ Exception: {e}")
    
    # Small delay between requests
    time.sleep(1)

# Step 2: Try to fetch Phoenix traces
print("\n" + "=" * 80)
print("FETCHING PHOENIX TRACES")
print("=" * 80)

try:
    # First, let's try the Phoenix search endpoint to get recent traces
    print("\nAttempting to fetch traces from Phoenix...")
    
    # Try a simple GET to see what endpoints are available
    traces_response = requests.get(
        f"{PHOENIX_URL}/api/v1/projects/default/traces",
        timeout=10
    )
    
    if traces_response.status_code == 200:
        traces_data = traces_response.json()
        print(f"✓ Phoenix traces endpoint available")
        print(f"  Found {len(traces_data) if isinstance(traces_data, list) else 'unknown count'} traces")
        
        # Save raw traces for inspection
        with open("phoenix_traces_raw.json", "w") as f:
            json.dump(traces_data, f, indent=2)
        print("  Saved to phoenix_traces_raw.json")
    else:
        print(f"✗ Phoenix endpoint returned {traces_response.status_code}")
        print(f"  Response: {traces_response.text[:500]}")
        
except Exception as e:
    print(f"✗ Could not fetch Phoenix traces: {e}")

# Save results to file
with open("ab_test_results.json", "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)

print("\n" + "=" * 80)
print("SUMMARY")
print("=" * 80)

for variant, data in results.items():
    status = data.get("status", "unknown")
    if status == "success":
        resp = data.get("response", {})
        print(f"\n{variant}:")
        print(f"  Status: ✓ Success")
        print(f"  Time: {data.get('elapsed_seconds')}s")
        print(f"  Confidence: {resp.get('confidence', 'N/A')}")
        print(f"  Answer length: {len(resp.get('answer', ''))} chars")
        print(f"  Sources: {len(resp.get('sources', []))}")
        answer_preview = resp.get('answer', '')[:100].replace('\n', ' ')
        print(f"  Answer: {answer_preview}...")
    else:
        print(f"\n{variant}:")
        print(f"  Status: ✗ {status}")
        print(f"  Error: {data.get('error', 'unknown')[:200]}")

print("\n✓ Results saved to ab_test_results.json")
print("\nNow visit http://localhost:6006 to inspect Phoenix traces manually")
print("or use the tools below to query Phoenix programmatically.")
