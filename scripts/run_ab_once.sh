#!/usr/bin/env bash
set -euo pipefail
MODE="$1"
OUT_PREFIX="$2"

# Configurable per-step timeouts (seconds) for CI/local stability.
AB_TIMEOUT_DEFAULT="${AB_TIMEOUT_DEFAULT:-180}"
AB_TIMEOUT_UPLOAD="${AB_TIMEOUT_UPLOAD:-60}"
AB_TIMEOUT_KNOWLEDGE_UPLOAD="${AB_TIMEOUT_KNOWLEDGE_UPLOAD:-90}"
AB_TIMEOUT_FORECAST="${AB_TIMEOUT_FORECAST:-120}"
AB_TIMEOUT_QA="${AB_TIMEOUT_QA:-${AB_TIMEOUT_DEFAULT}}"
AB_TIMEOUT_EXPLAIN="${AB_TIMEOUT_EXPLAIN:-${AB_TIMEOUT_DEFAULT}}"

UPLOAD=$(curl -sS --max-time "$AB_TIMEOUT_UPLOAD" -X POST 'http://localhost:8000/api/upload' -F 'file=@data/landing/test.csv')
SESSION_ID=$(printf '%s' "$UPLOAD" | python3 -c 'import sys,json; print(json.load(sys.stdin)["session_id"])')
SERVICE=$(printf '%s' "$UPLOAD" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("service_detected","FIBRE"))')

curl -sS --max-time "$AB_TIMEOUT_KNOWLEDGE_UPLOAD" -X POST 'http://localhost:8000/api/knowledge/upload' -F 'files=@data/knowledge/milvus_test.txt' > "/tmp/${OUT_PREFIX}_knowledge_upload.json"
curl -sS --max-time "$AB_TIMEOUT_FORECAST" -X POST 'http://localhost:8000/api/forecast/' -H 'Content-Type: application/json' -d '{"session_id":"'"$SESSION_ID"'","model":"best","horizon":14}' > "/tmp/${OUT_PREFIX}_forecast.json"
curl -sS --max-time "$AB_TIMEOUT_QA" -X POST 'http://localhost:8000/api/knowledge/qa' -H 'Content-Type: application/json' -d '{"question":"Quels facteurs influencent la capacite et la surcharge du service fibre ?","service_type":"'"$SERVICE"'"}' > "/tmp/${OUT_PREFIX}_qa.json"
curl -sS --max-time "$AB_TIMEOUT_EXPLAIN" -X POST 'http://localhost:8000/api/explain/' -H 'Content-Type: application/json' -d '{"session_id":"'"$SESSION_ID"'","service_type":"'"$SERVICE"'"}' > "/tmp/${OUT_PREFIX}_explain.json"

python3 - <<PY
import json
qa=json.load(open('/tmp/${OUT_PREFIX}_qa.json'))
ex=json.load(open('/tmp/${OUT_PREFIX}_explain.json'))
out={
  'mode':'${MODE}',
  'session_id':'${SESSION_ID}',
  'service':'${SERVICE}',
  'qa_confidence':qa.get('confidence'),
  'qa_sources_count':len(qa.get('sources',[])),
  'qa_scores':qa.get('retrieval_scores',[]),
  'qa_answer':qa.get('answer',''),
  'explain_sources_count':len(ex.get('sources',[])),
  'explain_scores':ex.get('retrieval_scores',[]),
  'explain_text':ex.get('explanation','')
}
print(json.dumps(out, ensure_ascii=True, indent=2))
open('/tmp/${OUT_PREFIX}_summary.json','w').write(json.dumps(out, ensure_ascii=True, indent=2))
PY
