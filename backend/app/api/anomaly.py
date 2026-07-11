"""
Anomaly detection API endpoints.

GET  /api/anomaly/detect  — run detection, return scored anomaly list + summary
POST /api/anomaly/review  — acknowledge or dismiss a detected anomaly
"""
from __future__ import annotations

import datetime as _dt
import json as _json
import logging
import re as _re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text as _sql_text
from sqlalchemy.orm import Session

from app.db.session import get_db, SessionLocal
from app.services.anomaly_service import (
    AnomalyRecord,
    detect_anomalies,
    get_summary_stats,
    get_timeseries,
)
from app.services.ollama_client import OllamaClient, ollama_client
from app.services.rag_service import rag_service

# LLM generation on CPU needs much more time than embeddings
_explain_ollama = OllamaClient(timeout=300)

router = APIRouter(prefix="/api/anomaly", tags=["Anomaly Detection"])
logger = logging.getLogger(__name__)

# Background thread pool used by /detect to pre-warm the explanation cache.
# 2 workers: enough parallelism without saturating a local Ollama instance.
# daemon=True so workers don't block clean shutdown.
_prefetch_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="anomaly-prefetch")

# Tracks anomaly IDs currently being explained in the background so /detect
# doesn't submit the same item twice on rapid successive calls.
_prefetch_in_flight: set[str] = set()


def ensure_anomaly_reviews_table(db: Session) -> None:
    """Create the anomaly_reviews table if it does not already exist.

    Called once at application startup so the table is guaranteed to exist
    before any request hits /review or /detect. Using IF NOT EXISTS makes
    this idempotent — safe to run on every boot without a migration tool.
    """
    db.execute(_sql_text("""
        CREATE TABLE IF NOT EXISTS public.anomaly_reviews (
            anomaly_id   TEXT PRIMARY KEY,
            action       TEXT NOT NULL
                             CHECK (action IN ('reviewed', 'dismissed', 'escalated')),
            note         TEXT,
            reviewed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))
    db.commit()
    logger.info("✓ anomaly_reviews table ready")


def ensure_anomaly_explanations_table(db: Session) -> None:
    """Create the anomaly_explanations table and hydrate the in-memory cache.

    Explanations are separate from reviews — an anomaly can be explained
    without being reviewed. Storing them in their own table avoids coupling
    two independent workflows.

    This is called at startup so:
      1. The table exists before any /explain request.
      2. _EXPLANATION_CACHE is pre-loaded, meaning /detect returns
         rag_explanation immediately without requiring a prior /explain call
         in the current process lifetime.
    """
    db.execute(_sql_text("""
        CREATE TABLE IF NOT EXISTS public.anomaly_explanations (
            anomaly_id   TEXT PRIMARY KEY,
            cause        TEXT NOT NULL,
            sources      JSONB NOT NULL DEFAULT '[]',
            explained_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))
    db.commit()
    logger.info("✓ anomaly_explanations table ready")

    # Hydrate the in-memory cache from persisted rows so /detect can
    # return rag_explanation for anomalies explained in prior sessions.
    rows = db.execute(
        _sql_text("SELECT anomaly_id, cause, sources FROM public.anomaly_explanations")
    ).fetchall()
    for anomaly_id, cause, sources in rows:
        _EXPLANATION_CACHE[anomaly_id] = {
            "cause": cause,
            "sources": sources if isinstance(sources, list) else [],
        }
    if rows:
        logger.info("✓ Explanation cache: hydrated %d entries from DB", len(rows))


# ── lightweight explanation cache ──────────────────────────────────────────────
# Keyed by anomaly_id → {"cause": str, "sources": list[str]}.
# Written by /explain, read by /detect to pre-populate rag_explanation.
# In-memory only; cleared on restart (explanations are cheap to regenerate).
_EXPLANATION_CACHE: dict[str, dict] = {}


# ── response schemas ───────────────────────────────────────────────────────────

class AnomalyItem(BaseModel):
    id: str
    service_code: str
    region_label: str
    detected_date: str
    anomaly_type: str
    severity: str
    expected: float
    actual: float
    variance_pct: float
    anomaly_score: float
    z_score: float
    possible_cause: str
    action_recommended: str
    detection_method: str
    rag_explanation: Optional[str] = None
    rag_sources: list[str] = []


class SummaryStats(BaseModel):
    total: int
    high_severity: int
    medium_severity: int
    spikes: int
    drops: int
    data_quality: int
    detection_accuracy_pct: float


class DetectResponse(BaseModel):
    anomalies: list[AnomalyItem]
    summary: SummaryStats
    granularity: str
    filters_applied: dict


class ReviewRequest(BaseModel):
    anomaly_id: str
    action: str   # "reviewed" | "dismissed" | "escalated"
    note: Optional[str] = None


class ReviewResponse(BaseModel):
    anomaly_id: str
    action: str
    message: str


# ── background prefetch ───────────────────────────────────────────────────────

def _prefetch_explanations(records: list[AnomalyRecord]) -> None:
    """Generate and cache explanations for a list of anomalies in the background.

    Called from /detect for anomalies that have no cached explanation yet.
    Each record is explained sequentially within the worker thread so a single
    prefetch job doesn't spawn further threads. The in-flight set is cleared
    per-item as soon as it finishes (success or failure) so a later /detect
    call can re-submit if needed.
    """
    for r in records:
        try:
            req = AnomalyExplainRequest(
                anomaly_id=r.id,
                service_code=r.service_code,
                region_label=r.region_label,
                detected_date=r.detected_date,
                anomaly_type=r.anomaly_type,
                actual=r.actual,
                expected=r.expected,
                variance_pct=r.variance_pct,
                z_score=r.z_score,
            )
            _explain_one(req)
            logger.debug("Background prefetch completed for anomaly %s", r.id)
        except Exception as exc:
            logger.debug("Background prefetch failed for anomaly %s: %s", r.id, exc)
        finally:
            _prefetch_in_flight.discard(r.id)


# ── endpoints ─────────────────────────────────────────────────────────────────

@router.get("/detect", response_model=DetectResponse)
def detect(
    service_code: Optional[str] = Query(None, description="Filter by service code (e.g. FIBRE, 5G)"),
    region: Optional[str]       = Query(None, description="Filter by region label (partial match)"),
    severity: Optional[str]     = Query(None, description="Filter by severity: high | medium"),
    anomaly_type: Optional[str] = Query(None, description="Filter by type: spike | drop | data_quality | gradual"),
    granularity: str            = Query("monthly", pattern="^(monthly|daily)$", description="Time granularity"),
    limit: int                  = Query(50, ge=1, le=200, description="Max anomalies returned"),
    z_threshold: float          = Query(2.5, ge=1.0, le=5.0, description="Z-score threshold (1.0–5.0, default 2.5)"),
    if_contamination: float     = Query(0.08, ge=0.01, le=0.5, description="Isolation Forest contamination (0.01–0.5, default 0.08)"),
    db: Session                 = Depends(get_db),
):
    """
    Run the two-stage anomaly detection pipeline (Z-score + Isolation Forest)
    on the sales data mart and return scored, classified results.
    """
    # Normalise type filter to full label
    type_map = {
        "spike":       "Unexpected Spike",
        "drop":        "Unexpected Drop",
        "data_quality": "Data Quality Issue",
        "gradual":     "Gradual Anomaly",
    }
    type_filter = type_map.get(anomaly_type or "", anomaly_type) if anomaly_type else None

    try:
        records: list[AnomalyRecord] = detect_anomalies(
            db=db,
            service_code=service_code,
            region=region,
            severity_filter=severity,
            anomaly_type_filter=type_filter,
            granularity=granularity,
            limit=limit,
            z_threshold=z_threshold,
            if_contamination=if_contamination,
        )
    except Exception as exc:
        logger.exception("Anomaly detection failed")
        raise HTTPException(status_code=500, detail=f"Detection failed: {exc}") from exc

    # Exclude anomalies already reviewed or dismissed
    reviewed_ids: set[str] = set()
    try:
        rows = db.execute(
            _sql_text("SELECT anomaly_id FROM public.anomaly_reviews WHERE action IN ('reviewed', 'dismissed')")
        ).fetchall()
        reviewed_ids = {row[0] for row in rows}
    except Exception as exc:
        logger.warning("Could not load reviewed anomaly IDs — table may be unavailable: %s", exc)

    items = []
    for r in records:
        if r.id in reviewed_ids:
            continue
        cached = _EXPLANATION_CACHE.get(r.id)
        items.append(AnomalyItem(
            id=r.id,
            service_code=r.service_code,
            region_label=r.region_label,
            detected_date=r.detected_date,
            anomaly_type=r.anomaly_type,
            severity=r.severity,
            expected=r.expected,
            actual=r.actual,
            variance_pct=r.variance_pct,
            anomaly_score=r.anomaly_score,
            z_score=r.z_score,
            possible_cause=r.possible_cause,
            action_recommended=r.action_recommended,
            detection_method=r.detection_method,
            rag_explanation=cached["cause"] if cached else r.rag_explanation,
            rag_sources=cached["sources"] if cached else (r.rag_sources or []),
        ))

    stats = get_summary_stats(records)

    # Trigger background explanation for anomalies not yet in the cache.
    # High-severity anomalies are processed first. Capped at 10 per detect
    # call to avoid queuing a large backlog. Items already in-flight are
    # skipped so rapid /detect polling doesn't duplicate work.
    uncached = [
        r for r in records
        if r.id not in reviewed_ids
        and r.id not in _EXPLANATION_CACHE
        and r.id not in _prefetch_in_flight
    ]
    uncached.sort(key=lambda r: (0 if r.severity == "high" else 1))
    to_prefetch = uncached[:10]
    if to_prefetch:
        for r in to_prefetch:
            _prefetch_in_flight.add(r.id)
        _prefetch_executor.submit(_prefetch_explanations, to_prefetch)

    return DetectResponse(
        anomalies=items,
        summary=SummaryStats(**stats),
        granularity=granularity,
        filters_applied={
            "service_code": service_code,
            "region": region,
            "severity": severity,
            "anomaly_type": anomaly_type,
            "z_threshold": z_threshold,
            "if_contamination": if_contamination,
        },
    )


class TimeseriesPoint(BaseModel):
    date: str
    nb_ventes: float
    is_anomaly: bool
    anomaly_type: Optional[str] = None
    severity: Optional[str] = None
    z_score: Optional[float] = None
    expected: Optional[float] = None


class TimeseriesResponse(BaseModel):
    series: list[TimeseriesPoint]
    granularity: str
    service_code: Optional[str]
    region: Optional[str]


@router.get("/timeseries", response_model=TimeseriesResponse)
def timeseries(
    service_code: Optional[str] = Query(None, description="Filter to a single service (e.g. FIBRE)"),
    region: Optional[str]       = Query(None, description="Filter to a single region (partial match)"),
    granularity: str            = Query("monthly", pattern="^(monthly|daily)$"),
    db: Session                 = Depends(get_db),
):
    """Return full sales time series with per-point anomaly flags for chart rendering."""
    try:
        points = get_timeseries(db, service_code=service_code, region=region, granularity=granularity)
    except Exception as exc:
        logger.exception("Timeseries fetch failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return TimeseriesResponse(
        series=[TimeseriesPoint(**p) for p in points],
        granularity=granularity,
        service_code=service_code,
        region=region,
    )


class AnomalyExplainRequest(BaseModel):
    anomaly_id: str
    service_code: str
    region_label: str
    detected_date: str
    anomaly_type: str
    actual: float
    expected: float
    variance_pct: float
    z_score: float


class AnomalyExplainResponse(BaseModel):
    anomaly_id: str
    cause_probable: str
    procedure_traitement: str
    rag_sources: list[str] = []
    confidence: float = 0.0


# ── constants ──────────────────────────────────────────────────────────────────

_DIRECTION_MAP = {
    "Unexpected Spike":   "augmentation anormale",
    "Unexpected Drop":    "baisse anormale",
    "Data Quality Issue": "problème de qualité des données",
    "Gradual Anomaly":    "anomalie progressive",
}

# Rule-based fallback causes used when no RAG docs are available (spec §B)
_FALLBACK_CAUSES = {
    "Unexpected Spike":   "Hausse anormale des ventes — promotion flash, activation en masse de dealers ou effet viral probable.",
    "Unexpected Drop":    "Baisse anormale des ventes — rupture de stock, incident réseau ou perturbation logistique probable.",
    "Data Quality Issue": "Ventes nulles enregistrées alors que la période voisine est non nulle — erreur ETL ou panne système POS probable.",
    "Gradual Anomaly":    "Dérive progressive des ventes — pression concurrentielle, changement tarifaire ou churn saisonnier probable.",
}

_NO_DOCS_PROCEDURE = (
    "Aucun document de référence disponible dans la base vectorielle. "
    "Procéder selon le protocole standard : "
    "1. Vérifier les données source dans le système POS. "
    "2. Contacter le responsable régional pour confirmation. "
    "3. Escalader au service logistique si rupture confirmée."
)

# ── §B helper: stock rupture check ────────────────────────────────────────────

def _check_stock_rupture(db: Session, service_code: str, detected_date: str) -> bool:
    """Return True if fact_stock shows a rupture signal around the anomaly date."""
    try:
        # Approximate: check the whole month of the anomaly date
        month_start = detected_date[:7] + "-01"
        row = db.execute(
            _sql_text(
                """
                SELECT COUNT(*) FROM mart.fact_stock fs
                JOIN mart.dim_temps t ON fs.date_id = t.date_id
                WHERE t.date BETWEEN :date_start AND :date_end
                  AND (
                    fs.is_rupture = true
                    OR fs.has_zero_stock = true
                    OR COALESCE(fs.available_qty, 1) = 0
                  )
                """
            ),
            {"date_start": month_start, "date_end": detected_date},
        ).scalar()
        return int(row or 0) > 0
    except Exception as exc:  # pragma: no cover
        logger.warning("Stock rupture check failed: %s", exc)
        return False


# ── §B helper: Tunisian calendar events ───────────────────────────────────────

# Each entry is (date_pattern, event_name).
# Patterns are either YYYY-MM-DD (specific year) or MM-DD (annual, tested against the anomaly year).
_TUNISIAN_EVENTS: list[tuple[str, str]] = [
    # Fixed national holidays
    ("01-01", "Jour de l'An"),
    ("03-20", "Fête de l'Indépendance"),
    ("04-09", "Jour des Martyrs"),
    ("05-01", "Fête du Travail"),
    ("07-25", "Fête de la République"),
    ("08-13", "Fête de la Femme"),
    ("10-15", "Fête de l'Évacuation"),
    ("11-07", "Anniversaire du Changement"),
    # School year boundaries (commercial traffic spikes around these)
    ("09-05", "Rentrée scolaire"),
    ("06-15", "Fin d'année scolaire"),
    # Lunar events (approximate Gregorian dates, year-specific)
    ("2024-03-11", "Début Ramadan 2024"),
    ("2024-04-10", "Aïd el-Fitr 2024"),
    ("2024-06-17", "Aïd el-Adha 2024"),
    ("2025-03-01", "Début Ramadan 2025"),
    ("2025-03-30", "Aïd el-Fitr 2025"),
    ("2025-06-07", "Aïd el-Adha 2025"),
    ("2026-02-18", "Début Ramadan 2026"),
    ("2026-03-20", "Aïd el-Fitr 2026"),
    ("2026-05-27", "Aïd el-Adha 2026"),
    ("2027-02-07", "Début Ramadan 2027"),
    ("2027-03-09", "Aïd el-Fitr 2027"),
    ("2027-05-17", "Aïd el-Adha 2027"),
    ("2028-01-28", "Début Ramadan 2028"),
    ("2028-02-26", "Aïd el-Fitr 2028"),
    ("2028-05-05", "Aïd el-Adha 2028"),
]


def _find_calendar_event(date_str: str, window_days: int = 15) -> str | None:
    """Return the nearest Tunisian event within ±window_days of date_str, or None."""
    try:
        anomaly_date = _dt.date.fromisoformat(date_str[:10])
        year = anomaly_date.year
    except (ValueError, TypeError):
        return None

    closest: tuple[int, str] | None = None

    for pattern, name in _TUNISIAN_EVENTS:
        if len(pattern) == 10:
            try:
                event_date = _dt.date.fromisoformat(pattern)
            except ValueError:
                continue
        else:
            # MM-DD — resolve against anomaly year
            try:
                event_date = _dt.date(year, int(pattern[:2]), int(pattern[3:]))
            except ValueError:
                continue

        delta = abs((anomaly_date - event_date).days)
        if delta <= window_days and (closest is None or delta < closest[0]):
            closest = (delta, name)

    return closest[1] if closest else None


def _coerce_to_str(value: object) -> str:
    """Convert an LLM field value to a readable string.

    Handles the case where the LLM returns a list of step-dicts instead of a
    plain numbered string, e.g. [{"étape": 1, "description": "..."}, ...]
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        lines = []
        for i, item in enumerate(value, start=1):
            if isinstance(item, dict):
                desc = (
                    item.get("description")
                    or item.get("action")
                    or item.get("étape")
                    or item.get("step")
                    or str(item)
                )
                lines.append(f"{i}. {desc}")
            else:
                lines.append(f"{i}. {item}")
        return "\n".join(lines)
    return str(value).strip()


_CAUSE_KEYS = ("cause_probable", "cause", "cause_probable_fr", "cause_racine", "diagnosis")
_PROC_KEYS  = ("procedure_traitement", "procedure", "procédure", "procedure_traitement_fr",
               "action", "recommended_action", "resolution", "steps")


def _extract_cause_proc(data: dict) -> tuple[str, str]:
    """Pull cause + procedure from a parsed dict regardless of key spelling."""
    cause = ""
    for k in _CAUSE_KEYS:
        v = _coerce_to_str(data.get(k, ""))
        if v:
            cause = v
            break
    proc = ""
    for k in _PROC_KEYS:
        v = _coerce_to_str(data.get(k, ""))
        if v:
            proc = v
            break
    return cause, proc


def _parse_structured_llm_response(raw: str) -> tuple[str, str]:
    """Extract cause_probable and procedure_traitement from an LLM response.

    Strategy (in order):
    1. Strip markdown fences and try direct JSON parse.
    2. Greedy scan for the largest {...} block (handles prose around JSON).
    3. Plain-text section extraction — look for labelled lines/paragraphs.
    4. Return entire raw text as cause with empty procedure.
    """
    # 1. Strip code fences, try direct parse
    clean = _re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()
    for candidate in (clean, raw):
        try:
            data = _json.loads(candidate)
            if isinstance(data, dict):
                cause, proc = _extract_cause_proc(data)
                if cause or proc:
                    return cause, proc
        except (_json.JSONDecodeError, AttributeError, TypeError):
            pass

    # 2. Greedy scan — find ALL {...} spans and try them largest-first
    brace_spans = []
    depth = 0
    start = -1
    for i, ch in enumerate(raw):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                brace_spans.append(raw[start : i + 1])
                start = -1
    for span in sorted(brace_spans, key=len, reverse=True):
        try:
            data = _json.loads(span)
            if isinstance(data, dict):
                cause, proc = _extract_cause_proc(data)
                if cause or proc:
                    return cause, proc
        except (_json.JSONDecodeError, AttributeError, TypeError):
            pass

    # 3. Plain-text section extraction
    cause_pattern = _re.compile(
        r"(?:cause[_\s]?(?:probable|racine)?|diagnostic)\s*[:\-]\s*(.+?)(?=\n\n|\n[A-Z]|procédure|procedure|étapes|$)",
        _re.IGNORECASE | _re.DOTALL,
    )
    proc_pattern = _re.compile(
        r"(?:procédure|procedure|étapes|action[s]?\s*(?:recommandée[s]?)?)\s*[:\-]\s*(.+)",
        _re.IGNORECASE | _re.DOTALL,
    )
    cause_m = cause_pattern.search(raw)
    proc_m  = proc_pattern.search(raw)
    if cause_m or proc_m:
        cause = cause_m.group(1).strip() if cause_m else ""
        proc  = proc_m.group(1).strip()  if proc_m  else ""
        return cause, proc

    # 4. Full-text fallback
    return raw.strip(), ""


@router.post("/explain", response_model=AnomalyExplainResponse)
def explain_anomaly(body: AnomalyExplainRequest, db: Session = Depends(get_db)):
    """
    Generate a structured RAG-based root cause explanation for a single anomaly.

    §A — primary path: dense + lexical hybrid RAG → LLM with strict JSON prompt.
    §B — enrichment signals always computed:
         • stock rupture check  (mart.fact_stock)
         • keyword fallback search when main RAG returns nothing
         • Tunisian calendar event proximity (±15 days)
    Falls back to rule-based text when the knowledge base is empty.
    """
    direction = _DIRECTION_MAP.get(body.anomaly_type, "anomalie")

    # ── §B: fast rule-based signals (DB + calendar, no LLM) ──────────────────
    # Stock check only meaningful when sales drop to zero
    is_rupture = (
        _check_stock_rupture(db, body.service_code, body.detected_date)
        if body.actual == 0
        else False
    )
    calendar_event = _find_calendar_event(body.detected_date)

    # ── §A: primary RAG retrieval ─────────────────────────────────────────────
    # Numeric values stripped — minus signs in e.g. "-62.5%" are FTS negation operators.
    query = (
        f"{direction} {body.service_code} {body.region_label} "
        f"ventes anomalie {body.anomaly_type.lower()}"
    )
    try:
        chunks = rag_service.retrieve(query, service_type=body.service_code, top_k=5)
    except Exception as exc:
        logger.warning("RAG retrieve failed for anomaly %s: %s", body.anomaly_id, exc)
        chunks = []

    # ── §B: keyword fallback when primary search returns nothing ─────────────
    if not chunks:
        try:
            chunks = rag_service.lexical_store.search(
                query_text="incident panne rupture retard livraison stock zéro",
                top_k=3,
                service_type=body.service_code,
            )
        except Exception as exc:
            logger.warning("Keyword fallback search failed: %s", exc)
            chunks = []

    sources = list({c["source"] for c in chunks})

    # ── §B: pure rule-based fallback when KB is truly empty ──────────────────
    if not chunks:
        if is_rupture:
            cause = (
                f"Rupture de stock confirmée pour {body.service_code} — "
                f"les données de stock indiquent un niveau zéro à cette période."
            )
        elif calendar_event:
            base = _FALLBACK_CAUSES.get(body.anomaly_type, "Anomalie détectée.")
            cause = f"{base} Coïncide avec l'événement : {calendar_event}."
        else:
            cause = _FALLBACK_CAUSES.get(body.anomaly_type, "Cause indéterminée — investigation manuelle requise.")

        return AnomalyExplainResponse(
            anomaly_id=body.anomaly_id,
            cause_probable=cause,
            procedure_traitement=_NO_DOCS_PROCEDURE,
            rag_sources=[],
            confidence=0.0,
        )

    # ── §A + §B: LLM with enriched context ───────────────────────────────────
    context_block = "\n\n".join(f"[SOURCE: {c['source']}]\n{c['text']}" for c in chunks)

    # Inject confirmed §B signals into the prompt so the LLM can use them
    extra_lines: list[str] = []
    if is_rupture:
        extra_lines.append(
            "DONNÉE CONFIRMÉE : le stock était à zéro à cette date (rupture fournisseur probable)."
        )
    if calendar_event:
        extra_lines.append(
            f"CONTEXTE CALENDAIRE : l'anomalie coïncide avec '{calendar_event}' (±15 jours)."
        )
    extra_context = ("\n".join(extra_lines) + "\n\n") if extra_lines else ""

    system_prompt = (
        "Tu es un analyste telecom senior. "
        "Réponds UNIQUEMENT en JSON valide, sans markdown ni texte autour. "
        'Format obligatoire : {"cause_probable": "...", "procedure_traitement": "..."}'
    )
    user_prompt = (
        f"Anomalie détectée :\n"
        f"- Service : {body.service_code} | Région : {body.region_label} | Date : {body.detected_date}\n"
        f"- Type : {body.anomaly_type} | Variance : {body.variance_pct:+.1f}% | Z-score : {body.z_score:+.2f}\n"
        f"- Ventes réelles : {int(body.actual)} | Attendues : {int(body.expected)}\n\n"
        f"{extra_context}"
        f"Documents de référence :\n{context_block}\n\n"
        f"En t'appuyant sur les documents et le contexte ci-dessus, fournis :\n"
        f"1. cause_probable : cause la plus probable de cette anomalie (2-3 phrases).\n"
        f"2. procedure_traitement : procédure concrète à suivre, sous forme d'étapes numérotées.\n"
        f"JSON uniquement."
    )

    try:
        raw = _explain_ollama.generate(
            prompt=user_prompt,
            system_prompt=system_prompt,
            temperature=0.1,
            max_tokens=600,
        )
        cause, procedure = _parse_structured_llm_response(raw)
        if not procedure:
            procedure = _NO_DOCS_PROCEDURE
    except Exception as exc:
        logger.exception("LLM generation failed for anomaly %s", body.anomaly_id)
        cause = _FALLBACK_CAUSES.get(body.anomaly_type, f"Erreur LLM : {exc}")
        if is_rupture:
            cause = f"Rupture de stock confirmée. {cause}"
        if calendar_event:
            cause += f" Coïncide avec : {calendar_event}."
        procedure = "Génération automatique indisponible. Procéder à une analyse manuelle."

    confidence = round(sum(c.get("score", 0.0) for c in chunks) / len(chunks), 3)

    # Persist the explanation so it survives restarts and /detect can
    # pre-populate rag_explanation without a prior /explain call.
    _EXPLANATION_CACHE[body.anomaly_id] = {"cause": cause, "sources": sources}
    try:
        db.execute(
            _sql_text("""
                INSERT INTO public.anomaly_explanations (anomaly_id, cause, sources, explained_at)
                VALUES (:anomaly_id, :cause, :sources, NOW())
                ON CONFLICT (anomaly_id) DO UPDATE
                    SET cause        = EXCLUDED.cause,
                        sources      = EXCLUDED.sources,
                        explained_at = NOW()
            """),
            {"anomaly_id": body.anomaly_id, "cause": cause, "sources": _json.dumps(sources)},
        )
        db.commit()
    except Exception as exc:
        logger.warning("Failed to persist explanation for %s: %s", body.anomaly_id, exc)
        db.rollback()

    return AnomalyExplainResponse(
        anomaly_id=body.anomaly_id,
        cause_probable=cause,
        procedure_traitement=procedure,
        rag_sources=sources,
        confidence=confidence,
    )


_BATCH_MAX = 50
_BATCH_CONCURRENCY = 4


class BatchExplainRequest(BaseModel):
    anomalies: list[AnomalyExplainRequest] = Field(
        ..., min_length=1, max_length=_BATCH_MAX,
        description=f"List of anomalies to explain (1–{_BATCH_MAX}).",
    )


class BatchExplainItem(BaseModel):
    anomaly_id: str
    cause_probable: str
    procedure_traitement: str
    rag_sources: list[str] = []
    confidence: float = 0.0
    cache_hit: bool = False
    error: Optional[str] = None


class BatchExplainResponse(BaseModel):
    results: list[BatchExplainItem]
    total: int
    cache_hits: int
    llm_calls: int
    errors: int


def _explain_one(req: AnomalyExplainRequest) -> BatchExplainItem:
    """Run explain_anomaly for a single item using its own DB session.

    Each worker thread must own its session — SQLAlchemy sessions are not
    thread-safe and cannot be shared across threads.
    """
    db = SessionLocal()
    try:
        result = explain_anomaly(body=req, db=db)
        return BatchExplainItem(
            anomaly_id=result.anomaly_id,
            cause_probable=result.cause_probable,
            procedure_traitement=result.procedure_traitement,
            rag_sources=result.rag_sources,
            confidence=result.confidence,
            cache_hit=False,
        )
    except Exception as exc:
        logger.error("Batch explain failed for %s: %s", req.anomaly_id, exc)
        return BatchExplainItem(
            anomaly_id=req.anomaly_id,
            cause_probable="",
            procedure_traitement="",
            error=str(exc),
        )
    finally:
        db.close()


@router.post("/explain/batch", response_model=BatchExplainResponse)
def explain_anomaly_batch(body: BatchExplainRequest):
    """
    Generate RAG-based explanations for multiple anomalies in one call.

    Cache-first: anomalies already in the explanation cache are returned
    immediately without an LLM call. Only uncached anomalies hit Ollama,
    running up to {_BATCH_CONCURRENCY} in parallel.

    Returns partial results — a failed item sets `error` rather than
    failing the whole request with a 500.
    """
    results: list[BatchExplainItem] = []
    to_process: list[AnomalyExplainRequest] = []

    for req in body.anomalies:
        cached = _EXPLANATION_CACHE.get(req.anomaly_id)
        if cached:
            results.append(BatchExplainItem(
                anomaly_id=req.anomaly_id,
                cause_probable=cached["cause"],
                procedure_traitement=_NO_DOCS_PROCEDURE,
                rag_sources=cached["sources"],
                confidence=0.0,
                cache_hit=True,
            ))
        else:
            to_process.append(req)

    if to_process:
        with ThreadPoolExecutor(max_workers=_BATCH_CONCURRENCY) as pool:
            futures = {pool.submit(_explain_one, req): req for req in to_process}
            for future in as_completed(futures):
                results.append(future.result())

    cache_hits = sum(1 for r in results if r.cache_hit)
    errors = sum(1 for r in results if r.error is not None)

    return BatchExplainResponse(
        results=results,
        total=len(results),
        cache_hits=cache_hits,
        llm_calls=len(results) - cache_hits - errors,
        errors=errors,
    )


@router.post("/review", response_model=ReviewResponse)
def review_anomaly(body: ReviewRequest, db: Session = Depends(get_db)):
    """Acknowledge, dismiss, or escalate an anomaly. Persisted across page reloads."""
    valid_actions = {"reviewed", "dismissed", "escalated"}
    if body.action not in valid_actions:
        raise HTTPException(
            status_code=422,
            detail=f"action must be one of: {', '.join(valid_actions)}",
        )

    try:
        db.execute(
            _sql_text("""
                INSERT INTO public.anomaly_reviews (anomaly_id, action, note)
                VALUES (:anomaly_id, :action, :note)
                ON CONFLICT (anomaly_id) DO UPDATE
                    SET action = EXCLUDED.action,
                        note = EXCLUDED.note,
                        reviewed_at = NOW()
            """),
            {"anomaly_id": body.anomaly_id, "action": body.action, "note": body.note},
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("Failed to persist anomaly review: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to save review") from exc

    logger.info("Anomaly %s marked as %s", body.anomaly_id, body.action)
    return ReviewResponse(
        anomaly_id=body.anomaly_id,
        action=body.action,
        message=f"Anomaly {body.anomaly_id} successfully marked as '{body.action}'.",
    )
