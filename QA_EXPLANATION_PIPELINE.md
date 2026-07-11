# Stock Forecast Explanation Pipeline

This document describes the live end-to-end pipeline used to produce and explain a stock forecast in this repository. It is written for an AI agent that needs the exact control flow, request/response contracts, and decision points.

## Scope

The pipeline has two separate phases:

1. Forecast production: build a forecast from the trained model and cache the result for the session.
2. Forecast explanation: answer a user question about that forecast by combining the cached forecast, numeric heuristics, retrieved knowledge-base context, and optional LLM generation.

The explanation endpoint does not train models and does not regenerate the forecast. It only explains an existing forecast.

## Canonical Runtime Flow

```text
CSV upload / session creation
  -> training endpoint selects the best model
  -> forecast endpoint generates historical + forecast series
  -> forecast is cached under the session
  -> user asks a question in the frontend
  -> /api/forecast/explain returns a grounded explanation
```

## Stage 1: Upload and Session Setup

The user first uploads stock data through the normal upload flow. The backend creates or updates a session and stores the uploaded dataset and detected service type in session state.

What matters for the explanation pipeline is that a session exists and can later be used to locate the generated forecast.

## Stage 2: Train Models

The training endpoint builds and evaluates forecasting models for the current session. The output of training is a best-model decision plus model metrics. That result is used later when the forecast is generated.

This stage is upstream of the explanation flow. The explanation endpoint never retrains models.

## Stage 3: Generate the Forecast

The frontend requests a forecast after training has completed. The request includes:

```json
{
  "session_id": "...",
  "model": "best",
  "horizon": 30,
  "granularity": "daily",
  "target_level": "service",
  "target_value": null
}
```

The backend resolves the target value, runs forecast generation, and returns a payload with at least:

```json
{
  "historical": [{"date": "...", "value": 123.0}],
  "forecast": [{"date": "...", "value": 130.0, "lower_bound": 120.0, "upper_bound": 140.0}],
  "metadata": {
    "model_used": "...",
    "trend": "up|down|stable",
    "change_pct": 12.5,
    "resolved_target_value": "..."
  }
}
```

Immediately after generation, the backend caches this payload with the key:

```text
<session_id>:forecast:last
```

This cache is the fallback source for the explanation endpoint.

## Stage 4: Frontend Explanation Request

The active UI entry point is `runForecastExplanation()` in `frontend/components/sections/forecasting.tsx`.

The frontend checks two things before sending the request:

1. The user typed a non-empty question.
2. A forecast exists in the component state.

If both checks pass, it sends a POST request to:

```text
/api/forecast/explain
```

The request body is:

```json
{
  "session_id": "...",
  "service_type": "FIBRE",
  "question": "Why is the forecast flat?",
  "target_level": "service",
  "target_value": null,
  "forecast_payload": {
    "historical": [...],
    "forecast": [...],
    "metadata": {...}
  }
}
```

The frontend prefers sending the live forecast payload so the explanation stays aligned with the state currently shown to the user.

## Stage 5: Backend Explanation Endpoint

The canonical implementation is `backend/app/api/forecast.py` at `POST /api/forecast/explain`.

### Request contract

```python
class ForecastQARequest(BaseModel):
    session_id: str
    service_type: str
    question: str
    target_level: Literal["service", "product", "category", "region"] = "service"
    target_value: str | None = None
    forecast_payload: dict[str, Any] | None = None
```

### Step 5.1: Resolve the forecast source

The backend first uses `request.forecast_payload` if it was provided by the frontend.

If that field is missing, it falls back to the cached payload at `<session_id>:forecast:last`.

If neither source exists, the endpoint returns `404` with the message that no forecast exists for the session.

This means the explanation endpoint is stateless at the request level, but it can still recover from the session cache when the UI omits the payload.

### Step 5.2: Extract the forecast context

The endpoint extracts:

```text
historical = forecast_payload["historical"]
forecast_points = forecast_payload["forecast"]
metadata = forecast_payload["metadata"]
```

Only dictionary items with numeric `value` fields are used for the numeric analysis.

The backend also builds compact summaries:

1. The last 6 historical points, or fewer if the series is shorter.
2. All forecast points, including confidence bounds.

### Step 5.3: Compute numeric heuristics

The endpoint computes descriptive statistics for both historical and forecast values:

```text
min, max, mean, span
```

The forecast is considered stable when either of the following is true:

```text
forecast_span <= max(1.0, abs(forecast_mean) * 0.03)
OR
there are 2 or fewer distinct rounded forecast values
```

This stability test is the key control signal for the fast path.

### Step 5.4: Retrieve knowledge-base context

The endpoint calls:

```python
rag_service.retrieve(query=request.question, service_type=request.service_type, top_k=5)
```

This retrieval is used to ground the answer in the knowledge base.

Important implementation note: this endpoint is not doing the fusion itself. It delegates to `rag_service.retrieve`, which embeds the question, runs dense search in Milvus or the in-memory fallback, runs lexical search in Postgres, fuses the two result sets with weighted RRF when both are available, and then light-reranks the merged results with the `service_type` filter applied throughout.

The returned chunks are concatenated into a context string and the source names are collected into `sources`.

### Step 5.5: Build the LLM prompt

The backend creates a French system prompt that instructs the model to answer concisely and structure the response.

The user prompt contains:

1. Recent historical data.
2. Forecast predictions with bounds.
3. Model metadata and numeric statistics.
4. Retrieved knowledge-base context.
5. The original user question.

The model is explicitly asked to provide:

1. Data analysis.
2. Explanatory factors.
3. Recommendations if relevant.

### Step 5.6: Choose heuristic vs LLM answer

If the forecast is stable, the endpoint returns a deterministic heuristic answer immediately.

If the forecast is not stable, the endpoint calls:

```python
ollama_client.generate(
    prompt=prompt,
    system_prompt=system_prompt,
    model=settings.OLLAMA_LLM_MODEL,
    temperature=0.2,
    max_tokens=900,
)
```

If Ollama generation fails, the endpoint falls back to the same heuristic answer.

So there are three possible answer paths:

1. Stable forecast -> heuristic answer.
2. Dynamic forecast -> LLM answer.
3. LLM failure -> heuristic fallback.

### Step 5.7: Compute confidence

Confidence is derived from retrieval scores when documents were retrieved.

If retrieval scores exist, the confidence is the average score clamped to the range `0.0` to `1.0`.

If no documents were retrieved, confidence falls back to a simple heuristic:

```text
0.35 if the forecast is stable
0.25 otherwise
```

This confidence is a retrieval-quality signal, not a calibrated probability that the textual answer is correct.

### Step 5.8: Return the response

The endpoint returns:

```json
{
  "answer": "...",
  "sources": ["doc_a", "doc_b"],
  "confidence": 0.72,
  "retrieval_scores": [0.81, 0.63],
  "forecast_context": {
    "model_used": "...",
    "target_level": "service",
    "target_value": null,
    "historical_points": 24,
    "forecast_points": 30,
    "forecast_stable": false,
    "forecast_span": 18.4
  }
}
```

## Legacy Explain Route

There is also `backend/app/api/explain.py`, which exposes a simpler legacy route:

```text
POST /api/explain
```

That route loads the cached forecast from the session and delegates to `rag_service.explain_forecast(...)`.

For new client integrations, the canonical route is `POST /api/forecast/explain` because it exposes the full request shape and the richer response contract.

## Internal RAG Helper

The helper `rag_service.explain_forecast(service_type, forecast_payload)` is a thin wrapper that:

1. Reads `model_used`, `trend`, and `change_pct` from the payload metadata.
2. Builds a short natural-language question about the forecast.
3. Calls `rag_service.answer_question(...)`.
4. Returns the generated explanation plus sources and retrieval scores.

That helper is useful as a fallback or compatibility layer, but it is not the main orchestration path for the current frontend.

## What This Pipeline Does Not Do

The explanation endpoint does not:

1. Train the forecasting model.
2. Recompute the forecast.
3. Persist Q&A history to the database.
4. Run a hybrid lexical-plus-vector retrieval merge in the endpoint itself.
5. Stream tokens to the client.

## Execution Summary

In one sentence: the user asks a question about an already-generated stock forecast, the frontend sends that question plus the forecast payload to `/api/forecast/explain`, the backend either reuses the payload or loads the cached forecast, computes stability heuristics, retrieves supporting knowledge-base chunks, and then returns either a heuristic explanation or an Ollama-generated explanation with sources and confidence.
