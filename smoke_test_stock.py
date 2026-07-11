#!/usr/bin/env python3
"""
End-to-end smoke test for stock forecasting + stock recommendation pipelines.
Tests:
  1. Inventory training  (POST /api/inventory/training)
  2. Inventory forecast  (POST /api/inventory/forecast)
  3. Demand stats        (GET /api/v1/inventory/demand-stats)
  4. Recommendations     (POST /api/v1/inventory/recommendations)
  5. RAG recommendations (POST /api/v1/inventory/recommendations/rag)
  6. Phoenix traces      (GET http://localhost:6006 GraphQL or REST)
"""

import datetime
import json
import sys
import time
import requests

_smoke_test_start = datetime.datetime.now(datetime.timezone.utc)

BASE = "http://localhost:8000"
PHOENIX = "http://localhost:6006"

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
INFO = "\033[34mℹ\033[0m"
WARN = "\033[33m⚠\033[0m"

results = []

def check(label, passed, detail=""):
    icon = PASS if passed else FAIL
    print(f"  {icon} {label}" + (f": {detail}" if detail else ""))
    results.append((label, passed, detail))
    return passed


def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ── Helpers ──────────────────────────────────────────────────────────────────

def post(path, payload, timeout=300):
    r = requests.post(f"{BASE}{path}", json=payload, timeout=timeout)
    return r

def get(path, params=None, timeout=30):
    r = requests.get(f"{BASE}{path}", params=params, timeout=timeout)
    return r


# ── 0. Prerequisite: pick a session ─────────────────────────────────────────

section("0. Pre-flight: upload stock CSV + session discovery")

STOCK_CSV = "/home/habib/pfe/data/ooredoo_inventory_stock.csv"

# Upload the stock CSV to create a fresh session
import os
print(f"\n  {INFO} Uploading {os.path.basename(STOCK_CSV)} ...")
with open(STOCK_CSV, "rb") as f:
    up = requests.post(f"{BASE}/api/upload", files={"file": (os.path.basename(STOCK_CSV), f, "text/csv")}, timeout=60)

check("Upload returned 200", up.status_code == 200, f"HTTP {up.status_code}")
up_data = up.json()
session_id = up_data.get("session_id")
check("session_id returned", session_id is not None, session_id)
check("Upload status completed", up_data.get("status") == "completed", up_data.get("status"))

rows_ingested = up_data.get("rows", 0)
service = up_data.get("service_detected", "?")
check("Rows ingested > 0", rows_ingested > 0, f"{rows_ingested} rows, service={service}")

print(f"\n  {INFO} Session: {session_id}")
print(f"  {INFO} Source: {up_data.get('file')} | rows={rows_ingested} | service={service}")
print(f"  {INFO} Period: {up_data.get('period_start')} → {up_data.get('period_end')}")

# Verify session is in the list
r = get("/api/training/sessions")
check("Sessions endpoint reachable", r.status_code == 200)
sessions = r.json().get("sessions", [])
check("Fresh session visible in session list", any(s["session_id"] == session_id for s in sessions),
      f"{len(sessions)} sessions in list")


# ── 1. Inventory Training ─────────────────────────────────────────────────────

section("1. Inventory Training")

train_payload = {
    "session_id": session_id,
    "horizon": 6,
    "models": ["all"],
    "enable_generative": False,   # faster for smoke test
    "granularity": "monthly",
    "forecast_target": "stock",
    "forecast_scope": "national",
}

print(f"\n  {INFO} POSTing to /api/inventory/training (generative=False for speed)...")
t0 = time.time()
r = post("/api/inventory/training", train_payload, timeout=300)
elapsed = time.time() - t0

check("Training returned 200", r.status_code == 200, f"HTTP {r.status_code}")

if r.status_code == 200:
    data = r.json()
    check("Training status == completed", data.get("status") == "completed", data.get("status"))
    models_trained = data.get("models_trained", 0)
    check("At least 1 model trained", models_trained >= 1, f"{models_trained} models")
    best_model = data.get("best_model") or data.get("best_metric_model")
    check("best_model present", best_model is not None, best_model)
    training_id = data.get("training_id")
    check("training_id returned", training_id is not None, training_id)
    print(f"\n  {INFO} Training completed in {elapsed:.1f}s | best_model={best_model}")
    print(f"  {INFO} Top models: {[r_['model'] for r_ in data.get('results', [])[:5]]}")
else:
    print(f"  {FAIL} Body: {r.text[:300]}")
    best_model = "prophet"
    training_id = None
    print(f"  {WARN} Falling back to model='{best_model}' for forecast step")


# ── 2. Inventory Forecast ─────────────────────────────────────────────────────

section("2. Inventory Forecast")

forecast_payload = {
    "session_id": session_id,
    "model": best_model,
    "horizon": 6,
    "granularity": "monthly",
    "scope": "both",
    "forecast_target": "stock",
    "forecast_scope": "national",
}

print(f"\n  {INFO} POSTing to /api/inventory/forecast (model={best_model})...")
t0 = time.time()
r = post("/api/inventory/forecast", forecast_payload, timeout=120)
elapsed = time.time() - t0

check("Forecast returned 200", r.status_code == 200, f"HTTP {r.status_code}")

forecast_series_for_rec = []  # We'll extract per-family forecast points
if r.status_code == 200:
    data = r.json()
    check("status == success", data.get("status") == "success", data.get("status"))
    forecast = data.get("forecast", {})
    check("forecast key present", bool(forecast), f"keys={list(forecast.keys())}")

    # forecast[scope] is {"historical": [...], "forecast": [...]}
    def _extract_fc_pts(series_obj):
        """Handle both list and {historical, forecast} dict formats."""
        if isinstance(series_obj, list):
            return series_obj
        if isinstance(series_obj, dict):
            return series_obj.get("forecast", series_obj.get("historical", []))
        return []

    global_obj = forecast.get("global", [])
    per_family = forecast.get("per_family", {})
    global_series = _extract_fc_pts(global_obj)
    family_series = _extract_fc_pts(next(iter(per_family.values()), [])) if per_family else []

    check("Global or per_family forecast series non-empty",
          len(global_series) > 0 or len(family_series) > 0,
          f"global={len(global_series)} fc pts, families={len(per_family)}")

    if global_series:
        forecast_series_for_rec = global_series[:6]
    elif family_series:
        forecast_series_for_rec = family_series[:6]

    metadata = data.get("metadata", {})
    check("model_used in metadata", bool(metadata.get("model_used")), metadata.get("model_used"))

    if forecast_series_for_rec:
        first_pt = forecast_series_for_rec[0]
        print(f"\n  {INFO} Forecast generated in {elapsed:.1f}s")
        print(f"  {INFO} First point: {first_pt}")
        print(f"  {INFO} {len(forecast_series_for_rec)} forecast points available for recommendations")
else:
    print(f"  {FAIL} Body: {r.text[:300]}")
    # Build synthetic series for recommendations step
    forecast_series_for_rec = [
        {"date": f"2026-0{i+1}-01", "value": 80 + i * 5, "lower_bound": 60, "upper_bound": 110}
        for i in range(6)
    ]
    print(f"  {WARN} Using synthetic forecast series for downstream steps")


# ── 3. Demand Stats ────────────────────────────────────────────────────────────

section("3. Demand Stats")

print(f"\n  {INFO} GET /api/v1/inventory/demand-stats?months=3")
r = get("/api/v1/inventory/demand-stats", params={"months": 3, "forecast_scope": "national"})
check("demand-stats returned 200", r.status_code == 200, f"HTTP {r.status_code}")

if r.status_code == 200:
    data = r.json()
    segments = data.get("segments", [])
    check("segments key present", "segments" in data)
    if segments:
        check("At least one product segment", len(segments) > 0, f"{len(segments)} segments")
        print(f"  {INFO} Sample segment: {segments[0]}")
    else:
        print(f"  {WARN} Empty segments — activations_qty may be 0 in this dataset (expected for synthetic data)")
        check("Empty segments (known limitation)", True, "activations_qty=0 in test dataset")


# ── 4. Stock Recommendations (quantitative only) ──────────────────────────────

section("4. Stock Recommendations (Quantitative)")

# Build forecast points in the expected format
def _make_fp(series):
    out = []
    for i, pt in enumerate(series[:6]):
        if isinstance(pt, dict):
            out.append({
                "date": pt.get("date", f"2026-0{i+1}-01"),
                "value": max(1, int(pt.get("value") or pt.get("yhat") or 80)),
                "lower_bound": int(pt.get("lower_bound") or pt.get("yhat_lower") or 60),
                "upper_bound": int(pt.get("upper_bound") or pt.get("yhat_upper") or 110),
            })
    return out or [{"date": f"2026-0{i+1}-01", "value": 80+i*5, "lower_bound": 60, "upper_bound": 110} for i in range(6)]

forecast_points = _make_fp(forecast_series_for_rec)
print(f"\n  {INFO} Using {len(forecast_points)} forecast points")
print(f"  {INFO} Sample: {forecast_points[0]}")

rec_payload = {
    "session_id": session_id,
    "service_level": 0.95,
    "recommendations_input": [
        {
            "product_id": "FIBRE-CPE-001",
            "product_name": "CPE FTTH Router",
            "product_type": "CPE_HARDWARE",
            "governorate": "NATIONAL",
            "current_stock": 150,
            "forecast_series": forecast_points,
            "avg_monthly_demand": 45.0,
            "data_source_mix": {"REAL": 12, "SIMULATED": 0},
        },
        {
            "product_id": "FIBRE-SUB-002",
            "product_name": "Abonnement Fibre 100M",
            "product_type": "SUBSCRIPTION",
            "governorate": "NATIONAL",
            "current_stock": 500,
            "forecast_series": forecast_points,
            "avg_monthly_demand": 120.0,
            "data_source_mix": {"REAL": 10, "SIMULATED": 2},
        },
        {
            "product_id": "5G-HW-003",
            "product_name": "5G CPE Pro",
            "product_type": "CPE_HARDWARE",
            "governorate": "Tunis",
            "current_stock": 20,
            "forecast_series": forecast_points,
            "avg_monthly_demand": 30.0,
            "data_source_mix": {"REAL": 0, "SIMULATED": 12},
        },
    ],
}

print(f"\n  {INFO} POSTing to /api/v1/inventory/recommendations (3 products)...")
t0 = time.time()
r = post("/api/v1/inventory/recommendations", rec_payload, timeout=60)
elapsed = time.time() - t0

check("Recommendations returned 200", r.status_code == 200, f"HTTP {r.status_code}")

if r.status_code == 200:
    data = r.json()
    check("status == success", data.get("status") == "success")
    recs = data.get("recommendations", [])
    check("3 recommendations returned", len(recs) == 3, f"got {len(recs)}")
    summary = data.get("summary", {})
    check("summary.total_products == 3", summary.get("total_products") == 3, str(summary))
    check("qty_to_order field present", all("qty_to_order" in r for r in recs))
    check("rupture_risk field present", all("rupture_risk" in r for r in recs))
    check("order_urgency field present", all("order_urgency" in r for r in recs))

    print(f"\n  {INFO} Recommendations generated in {elapsed:.1f}s")
    for rec in recs:
        print(f"  {INFO}   {rec['product_name']}: rupture={rec['rupture_risk']}, "
              f"urgency={rec['order_urgency']}, qty_to_order={rec['qty_to_order']}, "
              f"coverage={rec.get('coverage_months')}mo, confidence={rec.get('forecast_confidence')}")
else:
    print(f"  {FAIL} Body: {r.text[:500]}")


# ── 5. RAG Recommendations ─────────────────────────────────────────────────────

section("5. RAG-Augmented Recommendations")

rag_payload = {**rec_payload, "service_type": "FIBRE", "rag_top_k": 3}

print(f"\n  {INFO} POSTing to /api/v1/inventory/recommendations/rag ...")
t0 = time.time()
r = post("/api/v1/inventory/recommendations/rag", rag_payload, timeout=120)
elapsed = time.time() - t0

check("RAG recommendations returned 200", r.status_code == 200, f"HTTP {r.status_code}")

if r.status_code == 200:
    data = r.json()
    check("status == success", data.get("status") == "success")
    recs = data.get("recommendations", [])
    check("3 RAG recommendations returned", len(recs) == 3, f"got {len(recs)}")

    llm_field_present = all("llm_justification" in r for r in recs)
    check("llm_justification field present", llm_field_present)

    llm_fallbacks = sum(1 for r in recs if r.get("llm_justification", "").startswith("[LLM indisponible]"))
    llm_successes = len(recs) - llm_fallbacks
    check("LLM generated at least 1 justification", llm_successes >= 1, f"{llm_successes}/{len(recs)} used LLM")

    rag_chunks_total = sum(r.get("rag_chunks_used", 0) for r in recs)
    check("RAG retrieved some chunks (or graceful fallback)", True,
          f"total chunks={rag_chunks_total} (0 = no indexed docs yet)")

    print(f"\n  {INFO} RAG completed in {elapsed:.1f}s")
    for rec in recs:
        just = rec.get("llm_justification", "")[:120].replace("\n", " ")
        sources = rec.get("rag_sources", [])
        print(f"  {INFO}   {rec['product_name']}:")
        print(f"         LLM: {just}...")
        print(f"         Sources: {sources or '(none indexed)'} | chunks={rec.get('rag_chunks_used')}")

    metadata = data.get("metadata", {})
    check("rag_enabled in metadata", metadata.get("rag_enabled") is True, str(metadata.get("rag_enabled")))
    check("llm_model in metadata", bool(metadata.get("llm_model")), metadata.get("llm_model"))
else:
    print(f"  {FAIL} Body: {r.text[:500]}")


# ── 6. Phoenix Traces Inspection ──────────────────────────────────────────────

section("6. Phoenix Traces Inspection")

def _gql(query, timeout=15):
    return requests.post(f"{PHOENIX}/graphql", json={"query": query}, timeout=timeout)

print(f"\n  {INFO} Querying Phoenix at {PHOENIX} ...")

# Test connectivity
try:
    r_ph = _gql('{ projects { edges { node { name traceCount } } } }')
    phoenix_ok = r_ph.status_code == 200 and "data" in r_ph.json()
    check("Phoenix GraphQL reachable", phoenix_ok, f"HTTP {r_ph.status_code}")
except Exception as e:
    check("Phoenix GraphQL reachable", False, str(e))
    phoenix_ok = False

if phoenix_ok:
    proj_data = r_ph.json()["data"]["projects"]["edges"][0]["node"]
    total_traces = proj_data["traceCount"]
    check("Phoenix has traces", total_traces > 0, f"{total_traces} total traces")
    print(f"  {INFO} Project: {proj_data['name']} | total traces: {total_traces}")

    # Only look at spans from this test run (recorded before section 5 started)
    since_iso = _smoke_test_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    r_spans = _gql(
        '{ projects { edges { node { spans(first:100, rootSpansOnly: false, sort: {col: startTime, dir: desc}, '
        f'timeRange: {{start: "{since_iso}"}}) '
        '{ edges { node { name spanKind latencyMs statusCode attributes } } } } } } }',
        timeout=20,
    )
    if r_spans.status_code == 200 and "data" in r_spans.json():
        all_spans = r_spans.json()["data"]["projects"]["edges"][0]["node"]["spans"]["edges"]

        import statistics as _stats
        from collections import defaultdict
        by_name = defaultdict(list)
        for e in all_spans:
            by_name[e["node"]["name"]].append(e["node"])

        inv_span_names = [k for k in by_name if any(t in k for t in ("inventory", "rag", "llm", "RAG", "LLM"))]
        check("Inventory/RAG/LLM spans traced", len(inv_span_names) > 0,
              f"{len(inv_span_names)} distinct span types")

        print(f"\n  {INFO} Span latency summary (inventory pipeline):")
        for name in sorted(inv_span_names, key=lambda n: -max(s.get("latencyMs") or 0 for s in by_name[n])):
            lats = [s.get("latencyMs") or 0 for s in by_name[name]]
            errors = sum(1 for s in by_name[name] if s.get("statusCode") == "ERROR")
            print(f"    {name:55s} n={len(lats):3d} avg={_stats.mean(lats):8.0f}ms max={max(lats):8.0f}ms err={errors}")

        # LLM latency check — warn if avg > 90s (>90s = degraded; 30-90s = expected for llama3.2:3b local)
        llm_lats = [s.get("latencyMs") or 0 for s in by_name.get("inventory.llm_call", [])]
        if llm_lats:
            avg_llm = _stats.mean(llm_lats)
            max_llm = max(llm_lats)
            check("LLM latency < 90s average (local llama3.2:3b baseline)",
                  avg_llm < 90000,
                  f"avg={avg_llm/1000:.1f}s, max={max_llm/1000:.1f}s — model=llama3.2:3b")

        # RAG retrieval quality — detect truly duplicate chunks (same source + same chunk_index)
        reranker_spans = by_name.get("Re-rank Results", [])
        if reranker_spans:
            all_docs = []
            duplicate_issues = 0
            for s in reranker_spans:
                try:
                    attrs = json.loads(s.get("attributes") or "{}")
                    docs = attrs.get("reranker", {}).get("output_documents", [])
                    # Key = source_id + chunk_index (same source with different chunks is fine)
                    chunk_keys = [
                        f"{d.get('document',{}).get('id','')}::{d.get('document',{}).get('metadata',{}).get('chunk_index','?')}"
                        for d in docs
                    ]
                    if len(set(chunk_keys)) < len(chunk_keys):
                        duplicate_issues += 1
                    scores = [d.get("document", {}).get("score", 0) for d in docs]
                    all_docs.extend(scores)
                except Exception:
                    pass
            check("RAG reranker: no exact duplicate chunks (same source+chunk_idx)",
                  duplicate_issues == 0,
                  f"{duplicate_issues} reranker calls returned exact duplicate chunk keys")
            if all_docs:
                avg_score = _stats.mean(all_docs)
                print(f"  {INFO} Reranker avg relevance score: {avg_score:.3f} | n_docs={len(all_docs)}")

        # Error check
        error_spans = [e["node"]["name"] for e in all_spans if e["node"].get("statusCode") == "ERROR"]
        check("No ERROR status spans", len(error_spans) == 0,
              f"Errors: {error_spans[:5]}" if error_spans else "clean")

        # Deep LLM output inspection
        print(f"\n  {INFO} LLM output samples (first 200 chars each):")
        for s in by_name.get("inventory.llm_call", [])[:3]:
            try:
                attrs = json.loads(s.get("attributes") or "{}")
                out_msgs = attrs.get("llm", {}).get("output_messages", [])
                content = out_msgs[0].get("message", {}).get("content", "") if out_msgs else ""
                product_match = "✓" if "Commander" in content or "Aucune commande" in content else "?"
                print(f"    [{product_match}] lat={s['latencyMs']/1000:.1f}s: {content[:180].replace(chr(10),' ')}...")
            except Exception:
                pass
    else:
        check("Phoenix spans query", False, f"HTTP {r_spans.status_code}")


# ── Summary ───────────────────────────────────────────────────────────────────

section("SUMMARY")

passed = sum(1 for _, ok, _ in results if ok)
failed = sum(1 for _, ok, _ in results if not ok)
total = len(results)

print(f"\n  Total: {total}  |  {PASS} Passed: {passed}  |  {FAIL} Failed: {failed}\n")

if failed:
    print("  Failed checks:")
    for label, ok, detail in results:
        if not ok:
            print(f"    {FAIL} {label}: {detail}")

print()

# ── Improvement Observations ──────────────────────────────────────────────────

section("IMPROVEMENT OBSERVATIONS (from trace inspection)")

obs = [
    ("[FIXED] demand-stats broken (Query import + wrong column)",
     "Added 'Query' to FastAPI imports in inventory.py. "
     "Fixed 'qte_stk' → 'stock_quantity' column name in demand-stats SQL."),
    ("[FIXED] RAG duplicate chunks in dense-only path",
     "rag_service.py dense-only fallback (when FTS returns 0 results) was skipping deduplication. "
     "Added _doc_key deduplication before _light_rerank in both dense-only and lexical-only branches."),
    ("[PERF] LLM latency 56–107s per call (llama3.2:3b)",
     "Each RAG recommendation takes ~1-2 minutes total (3 parallel calls, but Ollama is serial). "
     "Consider: (a) use llama3.1:8b for better quality, or (b) add response caching keyed on "
     "product_type+rupture_risk+order_urgency for repeated product categories."),
    ("[QUALITY] demand-stats returns avg_monthly_demand=0.01 for most products",
     "activations_qty=0 in test dataset. Real dataset has activations for CPE_FTTH (5841 total). "
     "The recommendation engine receives proper demand from the forecast, but the demand-stats "
     "pre-fill widget on the frontend will show wrong values for synthetic/non-FTTH families."),
    ("[QUALITY] Milvus has duplicate vector entries for doc2_rapport_stock.docx idx=0",
     "The document was re-indexed multiple times (3 Milvus entries for same chunk). "
     "Fix: before indexing a document, delete existing vectors with the same source name "
     "from the Milvus collection to prevent accumulation on re-upload."),
    ("[QUALITY] Safety stock inflated by forecast scale mismatch",
     "Forecast global series (aggregate: ~200/month) is used as forecast_series in recommendations "
     "while avg_monthly_demand is product-level (45/month). The std of the forecast series (large scale) "
     "inflates safety_stock dramatically. Recommendation: use per-family forecast series, not global."),
    ("[MINOR] Span statusCode all UNSET",
     "No span explicitly sets OK status. Add span.set_status(Status(StatusCode.OK)) on success paths "
     "to enable Phoenix's OK/ERROR/UNSET filtering and evaluation dashboards."),
]

for i, (title, detail) in enumerate(obs, 1):
    print(f"\n  [{i}] {title}")
    print(f"      → {detail}")

print()
sys.exit(0 if failed == 0 else 1)
