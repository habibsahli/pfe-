"""
Forecasting endpoints
"""
import csv
import io

from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator
from typing import Literal, Any, Optional
from sqlalchemy.orm import Session
import logging
import json

from app.core.state import session_manager, TrainingStatus
from app.core.tracing import get_tracer
from app.db.session import get_db
from app.services.forecasting_service import (
    generate_forecast as run_forecast_generation,
    resolve_target_value,
    resolve_service_code,
    backtest_score,
)
from app.services.ollama_client import ollama_client
from app.services.rag_service import rag_service
from app.core.config import settings

router = APIRouter()
logger = logging.getLogger(__name__)
qa_tracer = get_tracer(__name__)


def _forecast_cache_key(
    session_id: str,
    service_code: str | None,
    target_level: str,
    target_value: str | None,
) -> str:
    """Cache key identifying *what* was forecast (service + segment).

    Keyed on the forecast identity — not granularity/model/horizon — because the
    consumers (/explain, /export) only know the identity, and "the latest forecast
    of this service/segment" is what they want. Prevents a forecast for one service
    from being read back for another (the old fixed ':forecast:last' key did that).
    """
    return (
        f"{session_id}:forecast:"
        f"svc={(service_code or 'ALL')}:tl={target_level}:tv={(target_value or 'ALL')}"
    )


def _preview_text(value: str, limit: int = 280) -> str:
    """Return a compact preview suitable for telemetry attributes."""
    if not value:
        return ""
    compact = " ".join(value.split())
    return compact[:limit]


def _business_driver_label(feature: str) -> str:
    """Translate model feature names into business-readable driver labels."""
    labels = {
        "sales_roll_3": "momentum commercial recent (moyenne des 3 dernieres periodes)",
        "sales_roll_6": "tendance commerciale sur 6 periodes",
        "sales_roll_12": "tendance annuelle lisse",
        "sales_lag_1": "niveau de ventes de la periode precedente",
        "sales_lag_2": "niveau de ventes deux periodes avant",
        "sales_lag_3": "niveau de ventes trois periodes avant",
        "sales_lag_12": "saisonnalite annuelle",
        "nb_dealers_actifs": "couverture et activite du reseau de dealers",
        "nb_ventes_promo": "volume de ventes sous promotion",
        "pct_ventes_promo": "poids des promotions dans les ventes",
        "prix_moyen": "prix moyen des offres vendues",
        "month": "effet calendrier mensuel",
        "month_sin": "saisonnalite intra-annuelle",
        "month_cos": "saisonnalite intra-annuelle",
        "quarter": "effet trimestre",
        "trend_index": "tendance de fond",
    }
    return labels.get(feature, feature.replace("_", " "))


def _format_business_drivers(raw_drivers: Any) -> tuple[str, list[dict[str, Any]]]:
    """Return a compact driver summary for prompts and response metadata."""
    if not isinstance(raw_drivers, list):
        return "Aucun driver modele disponible.", []

    formatted: list[dict[str, Any]] = []
    for item in raw_drivers[:5]:
        if not isinstance(item, dict):
            continue
        feature = str(item.get("feature") or "")
        if not feature:
            continue
        score = item.get("normalized_importance", item.get("importance", 0))
        try:
            score_float = float(score)
        except (TypeError, ValueError):
            score_float = 0.0
        formatted.append(
            {
                "feature": feature,
                "business_label": _business_driver_label(feature),
                "score": round(score_float, 2),
            }
        )

    if not formatted:
        return "Aucun driver modele disponible.", []

    lines = [
        f"- {driver['business_label']} ({driver['score']}% importance relative; feature={driver['feature']})"
        for driver in formatted
    ]
    return "\n".join(lines), formatted


class ForecastRequest(BaseModel):
    """Forecast request"""
    session_id: str
    model: str = "best"
    horizon: int = 6
    granularity: Literal["daily", "monthly"] = "daily"
    target_level: Literal["service", "product", "category", "region"] = "service"
    target_value: str | None = None
    # Which service to forecast (FIBRE/5G/DATA_BUNDLE/VOD, or 'ALL'/None for all).
    # Derived from the data, not the session — overrides the session's detected service.
    service_type: str | None = None
    include_promotions: bool = True
    include_price: bool = True
    include_calendar: bool = True


_VALID_QA_SERVICE_TYPES = {"FIBRE", "5G", "DATA", "VOD", "DATA_BUNDLE"}


class ForecastQARequest(BaseModel):
    """Forecast Q&A request"""
    session_id: str
    service_type: Optional[str] = None
    question: str
    target_level: Literal["service", "product", "category", "region"] = "service"
    target_value: str | None = None
    forecast_payload: dict[str, Any] | None = None
    prompt_variant: str | None = "control"

    @field_validator("service_type")
    @classmethod
    def validate_service_type(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v.strip() == "":
            return None   # null / empty → no filter; RAG searches all docs
        upper = v.strip().upper()
        if upper not in _VALID_QA_SERVICE_TYPES:
            raise ValueError(
                f"service_type must be one of {sorted(_VALID_QA_SERVICE_TYPES)} or null/empty "
                f"(got '{v}')"
            )
        return upper


class ForecastFactorsRequest(BaseModel):
    """Request for key factors/feature importance of a forecast"""
    session_id: str
    model: str = "best"


@router.post("")
@router.post("/")
def generate_forecast_endpoint(
    request: ForecastRequest,
    db: Session = Depends(get_db),
):
    """
    Generate forecast using trained model
    
    - **session_id**: Upload session ID
    - **model**: Model to use ("best" or specific model name)
    - **horizon**: Forecast horizon in periods for the selected granularity
    - **granularity**: Daily or monthly forecast output
    - **target_level**: Service, product, category, or region
    - **target_value**: Optional value for product/category/region targeting
    """
    try:
        session = session_manager.get_session(request.session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Upload session not found")

        _max_horizon = 12 if request.granularity == "monthly" else 6
        if not (1 <= request.horizon <= _max_horizon):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"horizon must be between 1 and {_max_horizon} for "
                    f"{request.granularity} granularity (got {request.horizon})."
                ),
            )

        # Resolve the service from the data (Option C), not just the session's detected
        # one — explicit service_type wins; 'UNKNOWN' session → all services.
        try:
            service_code = resolve_service_code(db, request.service_type, session.service_detected)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        best_model = request.model
        if request.model == "best":
            # Only reuse a training job whose params match this forecast request —
            # a monthly/region/5G best_model is meaningless for a daily/service/VOD forecast.
            def _job_matches(job) -> bool:
                if job.granularity is not None and job.granularity != request.granularity:
                    return False
                if job.target_level is not None and job.target_level != request.target_level:
                    return False
                # target_value only disambiguates non-service levels
                if (
                    request.target_level != "service"
                    and job.target_value is not None
                    and job.target_value != request.target_value
                ):
                    return False
                if job.service_type is not None and job.service_type != service_code:
                    return False
                return True

            matching_jobs = [
                job
                for job in session_manager._training_jobs.values()
                if job.session_id == request.session_id
                and job.status == TrainingStatus.COMPLETED
                and _job_matches(job)
            ]
            if matching_jobs:
                latest_job = max(matching_jobs, key=lambda job: job.created_at)
                best_model = latest_job.best_model or "naive_last"
            else:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "No completed training job matching this forecast's granularity/target "
                        "was found for this session. Run training with the same parameters first, "
                        "or pass an explicit model name (e.g. 'naive_last')."
                    ),
                )

        resolved_target_value = resolve_target_value(
            db=db,
            granularity=request.granularity,
            target_level=request.target_level,
            target_value=request.target_value,
            service_code=service_code,
        )

        payload = run_forecast_generation(
            db=db,
            best_model_name=best_model,
            horizon=request.horizon,
            service_code=service_code,
            granularity=request.granularity,
            target_level=request.target_level,
            target_value=resolved_target_value,
            include_promotions=request.include_promotions,
            include_price=request.include_price,
            include_calendar=request.include_calendar,
        )

        payload.setdefault("metadata", {})
        payload["metadata"]["resolved_target_value"] = resolved_target_value
        payload["metadata"]["service_code"] = service_code or "ALL"

        # Cache under the forecast identity (so different services/segments don't clobber
        # each other) AND keep the ':last' pointer for the "most recent forecast" flows.
        identity_key = _forecast_cache_key(
            request.session_id, service_code, request.target_level, resolved_target_value
        )
        session_manager.cache_forecast(identity_key, payload, session_id=request.session_id, db=db)
        session_manager.cache_forecast(
            f"{request.session_id}:forecast:last", payload, session_id=request.session_id, db=db
        )

        return payload
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Forecast failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/explain")
def explain_forecast_qa(request: ForecastQARequest, db: Session = Depends(get_db)):
    """
    Answer questions about a forecast using RAG knowledge base context.
    
    Combines:
    - Cached forecast data (historical and predicted values)
    - Model metadata (model name, trend, performance metrics)
    - Knowledge base context (retrieved via RAG)
    - User's question
    
    Returns explanation grounded in both forecast and knowledge context.
    """
    try:
        with qa_tracer.start_as_current_span("qa.pipeline") as pipeline_span:
            pipeline_span.set_attribute("qa.session_id", request.session_id)
            pipeline_span.set_attribute("qa.service_type", request.service_type)
            pipeline_span.set_attribute("qa.target_level", request.target_level)
            pipeline_span.set_attribute("qa.question", _preview_text(request.question, 500))

            # Prefer the forecast payload sent by the UI, then fall back to cached backend
            # state — the identity-scoped forecast (matching this question's service/segment)
            # first, then the generic most-recent forecast.
            forecast_payload = request.forecast_payload
            if not forecast_payload:
                try:
                    service_code = resolve_service_code(db, request.service_type, None)
                except ValueError:
                    service_code = None
                identity_key = _forecast_cache_key(
                    request.session_id, service_code, request.target_level, request.target_value
                )
                forecast_payload = session_manager.get_cached_forecast(identity_key)
                if not forecast_payload:
                    forecast_payload = session_manager.get_cached_forecast(
                        f"{request.session_id}:forecast:last"
                    )

            if not forecast_payload:
                raise HTTPException(status_code=404, detail="No forecast found for this session. Please generate a forecast first.")

            with qa_tracer.start_as_current_span("qa.context_extraction") as context_span:
                # Extract forecast data for context
                historical = forecast_payload.get("historical", [])
                forecast_points = forecast_payload.get("forecast", [])
                metadata = forecast_payload.get("metadata", {})
                driver_summary, business_drivers = _format_business_drivers(
                    forecast_payload.get("drivers") or metadata.get("drivers")
                )

                historical_values = [float(item.get("value", 0.0)) for item in historical if isinstance(item, dict) and item.get("value") is not None]
                forecast_values_numeric = [float(item.get("value", 0.0)) for item in forecast_points if isinstance(item, dict) and item.get("value") is not None]

                recent_historical = historical[-6:] if len(historical) > 6 else historical
                historical_summary_lines = [
                    f"{item.get('date')}: {item.get('value')}"
                    for item in recent_historical
                    if isinstance(item, dict)
                ]
                forecast_summary_lines = [
                    f"{item.get('date')}: {item.get('value')} (bounds: {item.get('lower_bound')} - {item.get('upper_bound')})"
                    for item in forecast_points
                    if isinstance(item, dict)
                ]

                historical_summary = "Recent historical data:\n" + ("\n".join(historical_summary_lines) if historical_summary_lines else "No historical points available.")
                forecast_summary = "Forecast predictions:\n" + ("\n".join(forecast_summary_lines) if forecast_summary_lines else "No forecast points available.")

                if historical_values:
                    hist_min = min(historical_values)
                    hist_max = max(historical_values)
                    hist_mean = sum(historical_values) / len(historical_values)
                    hist_span = hist_max - hist_min
                else:
                    hist_min = hist_max = hist_mean = hist_span = 0.0

                if forecast_values_numeric:
                    forecast_min = min(forecast_values_numeric)
                    forecast_max = max(forecast_values_numeric)
                    forecast_mean = sum(forecast_values_numeric) / len(forecast_values_numeric)
                    forecast_span = forecast_max - forecast_min
                    flat_forecast = forecast_span <= max(1.0, abs(forecast_mean) * 0.03)
                    stable_forecast = flat_forecast or len(set(round(v, 2) for v in forecast_values_numeric)) <= 2
                else:
                    forecast_min = forecast_max = forecast_mean = forecast_span = 0.0
                    stable_forecast = False

                context_span.set_attribute("qa.historical_points", len(historical))
                context_span.set_attribute("qa.forecast_points", len(forecast_points))
                context_span.set_attribute("qa.forecast_span", round(float(forecast_span), 3))
                context_span.set_attribute("qa.stable_forecast", stable_forecast)

            model_info = f"Model Used: {metadata.get('model_used', 'unknown')}"
            if metadata.get('trend'):
                model_info += f"\nTrend: {metadata.get('trend')}"
            if metadata.get('change_pct') is not None:
                model_info += f"\nPredicted Change: {metadata.get('change_pct')}%"
            model_info += (
                f"\nHistorical mean={round(hist_mean, 3)} min={round(hist_min, 3)} max={round(hist_max, 3)} span={round(hist_span, 3)}"
            )
            model_info += (
                f"\nForecast mean={round(forecast_mean, 3)} min={round(forecast_min, 3)} max={round(forecast_max, 3)} span={round(forecast_span, 3)}"
            )
            if stable_forecast:
                model_info += "\nForecast shape heuristic: stable/flat over the horizon"
            model_info += f"\nBusiness-readable model drivers:\n{driver_summary}"

            # Retrieve knowledge base context via RAG
            with qa_tracer.start_as_current_span("qa.rag_context_retrieval") as retrieval_span:
                rag_context = rag_service.retrieve(
                    query=request.question,
                    service_type=request.service_type,
                    top_k=5,
                )
                retrieval_span.set_attribute("qa.rag_chunks", len(rag_context))

            retrieved_docs = ""
            retrieval_sources = []
            if rag_context:
                retrieved_docs = "\n".join([f"[{r.get('source')}] {r.get('text')}" for r in rag_context])
                retrieval_sources = [r.get('source') for r in rag_context if r.get('source')]

            # Select prompt variant (must define before using in conditions)
            requested_variant = (request.prompt_variant or "control").lower()
            if requested_variant in {"control", "c", "variant_c", "concise", "default"}:
                variant = "c"
            elif requested_variant in {"a", "variant_a", "evidence"}:
                variant = "a"
            elif requested_variant in {"b", "variant_b", "numeric"}:
                variant = "b"
            elif requested_variant in {"legacy_control", "original", "baseline"}:
                variant = "legacy_control"
            else:
                variant = "c"

            # Defined here so it is always bound regardless of which variant branch runs.
            # Only variant "c" sets this to True; all others leave it False.
            strict_json_requested = False

            if variant == "a":
                system_prompt = (
                    "Tu es un analyste telecom expert. Reponds en francais de facon claire et detaillee. "
                    "Pour chaque affirmation, cite jusqu'a trois sources du contexte documentaire en utilisant [SOURCE_NAME]. "
                    "Donne d'abord une courte synthese (2-3 phrases), puis une analyse pas-a-pas, enfin des recommandations actionnables."
                )
                prompt = f"""CONTEXTE NUMERIQUE:

{historical_summary}

{forecast_summary}

{model_info}

CONTEXTE DOCUMENTAIRE (extraits pertinents):
{retrieved_docs if retrieved_docs else 'Aucun document trouve dans la base de connaissances.'}

QUESTION: {request.question}

INSTRUCTIONS:
- Reponds en francais.
- Debute par une synthese tres concise (1-3 lignes).
- Fournis ensuite une analyse pas-a-pas qui explique les motifs statistiques et documentaires.
- Cite les sources utilisees entre crochets, par ex. [doc_name].
- Termine par 3 recommandations pratiques."""

            elif variant == "b":
                system_prompt = (
                    "Tu es un analyste telecom. Priorise les donnees numeriques et la comparaison historique. "
                    "Reponds en francais: commence par un resume numerique (moyennes, ecarts, pourcentages), puis interprete." 
                )
                prompt = f"""RESUME NUMERIQUE:

- Historique recents: {', '.join(historical_summary_lines) if historical_summary_lines else 'aucun'}
- Moyenne historique: {round(hist_mean,2)} | Moyenne prevision: {round(forecast_mean,2)} | Changement: {metadata.get('change_pct','n/a')}%
- Volatilite historique (span): {round(hist_span,2)} | Volatilite forecast (span): {round(forecast_span,2)}

CONTEXTE DOCUMENTAIRE:
{retrieved_docs if retrieved_docs else 'Aucun document trouve'}

QUESTION: {request.question}

EXPLICATION: Donne une interpretation claire des nombres, indique si la constance indique incertitude, manque d'information ou contrainte metier. Propose 3 actions concretes."""

            elif variant == "c":
                system_prompt = (
                    "Tu es un directeur commercial telecom qui explique les previsions a des equipes business. "
                    "Reponds en francais avec un style concret, decisionnel et non technique. "
                    "Base-toi d'abord sur les chiffres fournis, puis relie-les a des causes business plausibles: dynamique recente, saisonnalite, reseau de dealers, promotions, prix/offres, zone cible et contexte documentaire. "
                    "Explique le 'pourquoi business' avant le 'comment modele'. Evite les termes techniques comme lag, rolling, regression, coefficient, feature, sauf si tu les traduis immediatement en langage metier. "
                    "Si l'utilisateur impose une structure fixe ou du JSON, respecte-la exactement sans texte additionnel. "
                    "Quand le format JSON est demande, retourne uniquement un objet JSON valide avec les cles patterns, facteurs_cles, opportunites, risques et insights, chacune contenant une liste de chaines. "
                    "N'ajoute ni markdown, ni explication, ni texte avant ou apres le JSON. "
                    "N'invente jamais de chiffres; si une information manque, signale-le clairement. "
                    "Si la hausse/baisse est faible, explique qu'il s'agit d'une variation moderee et donne les leviers business a surveiller."
                )
                question_lower = (request.question or "").lower()
                strict_json_requested = any(token in question_lower for token in ["json", "format json", "retourne un json", "return json"])
                json_schema_example = '{"patterns": ["..."], "facteurs_cles": ["..."], "opportunites": ["..."], "risques": ["..."], "insights": ["..."]}'
                prompt = (
                    "EXECUTIVE ANALYSIS:\n\n"
                    f"{historical_summary}\n\n"
                    f"{forecast_summary}\n\n"
                    f"{model_info}\n\n"
                    "CONTEXTE METIER:\n"
                    "- Priorise les faits verifies et les tendances quantitatives.\n"
                    "- Relie les variations de ventes aux actions commerciales ou aux evenements mentionnes dans le contexte documentaire.\n"
                    "- Si la serie est stable ou peu volatile, explique si cela suggere une inertie commerciale, une saisonnalite faible, une base clients mature ou un reseau de vente stable.\n"
                    "- Traduis les drivers techniques en langage business: momentum recent, ventes precedentes, couverture dealers, promotion, saisonnalite, prix/offre.\n"
                    "- Ne te limite pas a dire que le modele est stable: explique ce que cela signifie pour les ventes, la demande, la zone et les actions commerciales.\n\n"
                    "DRIVERS BUSINESS DU MODELE:\n"
                    f"{driver_summary}\n\n"
                    "EVIDENCE:\n"
                    f"{retrieved_docs if retrieved_docs else 'Aucun document trouve'}\n\n"
                    f"QUESTION: {request.question}\n\n"
                    "INSTRUCTIONS DE REPONSE:\n"
                    "- Reponds en francais.\n"
                    "- Commence par une reponse directe en 1 phrase: la raison business la plus probable de la variation.\n"
                    "- Ensuite, fournis 3 sections courtes: Lecture business, Pourquoi cela arrive, Actions recommandees.\n"
                    "- Dans Lecture business, cite les dates, le pourcentage de changement et le niveau de variation.\n"
                    "- Dans Pourquoi cela arrive, relie les drivers a des phenomenes business concrets.\n"
                    "- Dans Actions recommandees, propose 3 actions commerciales mesurables.\n"
                    "- Si la question demande une analyse strategique, fournis les 5 blocs suivants dans l'ordre: patterns, facteurs_cles, opportunites, risques, insights.\n"
                    "- Dans chaque bloc, utilise des formulations courtes mais precises, ancrees dans les donnees.\n"
                    "- Cite le contexte documentaire uniquement si cela renforce l'explication.\n"
                    "- Si la question impose un format JSON, retourne uniquement le JSON attendu, avec les cles demandees et sans prose additionnelle.\n"
                    "- Le JSON attendu doit respecter exactement cette structure:\n"
                    f"    {json_schema_example}\n"
                    "- Chaque liste doit contenir exactement le nombre d'elements demande par la question; ne duplique pas des idees.\n"
                    "- Si une information est incertaine ou absente, indique-le explicitement au lieu d'inventer."
                )

            else:
                # control: existing (original) prompt
                system_prompt = (
                    "Tu es un analyste telecom expert. Reponds en francais, de facon concise et structuree. "
                    "Base ta reponse sur le contexte fourni (donnees previsionnelles, context metier). "
                    "Si le contexte est insuffisant, dis-le clairement."
                )
                prompt = f"""Voici le contexte de la prevision:

{historical_summary}

{forecast_summary}

{model_info}

Contexte documentaire (si disponible):
{retrieved_docs if retrieved_docs else "Aucun document trouvé dans la base de connaissances."}

Question utilisateur: {request.question}

Explique les previsions en te basant sur les donnees historiques, les valeurs predites, et le contexte documentaire disponible. 
Fournis une explication structuree avec: 1) Analyse des donnees 2) Facteurs explicatifs 3) Recommandations si pertinent."""

            # Record which prompt variant was used and the system prompt
            pipeline_span.set_attribute("qa.prompt_variant_requested", requested_variant)
            pipeline_span.set_attribute("qa.prompt_variant", variant)
            pipeline_span.set_attribute("qa.prompt_length", len(prompt))
            pipeline_span.set_attribute("qa.system_prompt", _preview_text(system_prompt, 500))

            heuristic_answer = (
                "La variation prevue est moderee et semble surtout portee par la dynamique commerciale recente, "
                "pas par une rupture forte du marche.\n\n"
                f"Lecture business: la prevision indique une tendance {metadata.get('trend', 'stable')} "
                f"avec un changement estime de {metadata.get('change_pct', 'n/a')}%. "
                f"La moyenne historique est d'environ {round(hist_mean, 2)} ventes, contre {round(forecast_mean, 2)} en prevision.\n\n"
                "Pourquoi cela arrive: les principaux signaux disponibles sont:\n"
                f"{driver_summary}\n"
                "Cela signifie que le modele s'appuie surtout sur le rythme recent des ventes et la repetition des niveaux precedents. "
                "Business-wise, cela correspond a une zone ou la demande reste reguliere, avec une progression legere plutot qu'un changement brutal.\n\n"
                "Actions recommandees: 1) verifier si les campagnes ou offres fibre couvrent bien la zone cible; "
                "2) suivre l'activite des dealers pendant les deux premieres periodes de prevision; "
                "3) comparer les ventes reelles au forecast chaque mois pour detecter rapidement une rupture positive ou negative."
            )

            with qa_tracer.start_as_current_span("qa.answer_generation") as generation_span:
                generation_span.set_attribute("qa.prompt_length", len(prompt))
                generation_span.set_attribute("qa.prompt_preview", _preview_text(prompt, 500))
                generation_span.set_attribute("qa.system_prompt", _preview_text(system_prompt, 500))
                generation_span.set_attribute("qa.stable_forecast", stable_forecast)
                generation_span.set_attribute("qa.answer_mode", "llm")
                try:
                    answer = ollama_client.generate(
                        prompt=prompt,
                        system_prompt=system_prompt,
                        model=settings.OLLAMA_LLM_MODEL,
                        temperature=0.2,
                        max_tokens=1100,
                    )
                except Exception as ollama_err:
                    logger.warning(f"Ollama generation failed: {ollama_err}, returning structured fallback")
                    generation_span.set_attribute("qa.answer_mode", "heuristic_fallback")
                    answer = heuristic_answer

                generation_span.set_attribute("qa.response_preview", _preview_text(answer))

            # Validate strict JSON only when the user explicitly asks for JSON.
            if variant == "c" and strict_json_requested:
                try:
                    # Ensure answer is a JSON object matching the expected schema
                    parsed = json.loads(answer) if isinstance(answer, str) else answer
                    if not isinstance(parsed, dict):
                        raise ValueError("parsed JSON is not an object")

                    required_keys = ["patterns", "facteurs_cles", "opportunites", "risques", "insights"]
                    missing = [k for k in required_keys if k not in parsed]
                    if missing:
                        raise ValueError(f"missing keys: {missing}")

                    for k in required_keys:
                        if not isinstance(parsed[k], list) or not all(isinstance(x, str) for x in parsed[k]):
                            raise ValueError(f"key '{k}' must be a list of strings")

                    pipeline_span.set_attribute("qa.json_valid", True)
                    # Normalize answer to compact JSON string (UTF-8 friendly)
                    answer = json.dumps(parsed, ensure_ascii=False)

                except Exception as parse_err:
                    pipeline_span.set_attribute("qa.json_valid", False)
                    pipeline_span.set_attribute("qa.json_parse_error", str(parse_err))
                    raise HTTPException(status_code=422, detail=f"LLM output is not valid JSON for variant 'c': {parse_err}")

            # Confidence is derived from RAG retrieval scores only.
            # Without retrieved documents there is no evidence basis, so confidence
            # is 0.0 regardless of forecast stability. Returning a non-zero value
            # when no docs exist would imply evidence that doesn't exist.
            confidence = 0.0
            confidence_source = "no_rag_docs"
            if rag_context:
                scores = [r.get('score', 0) for r in rag_context if isinstance(r.get('score'), (int, float))]
                if scores:
                    confidence = min(1.0, max(0.0, sum(scores) / len(scores)))
                    confidence_source = "rag_retrieval_scores"

            return {
                "answer": answer,
                "sources": retrieval_sources,
                "confidence": round(confidence, 3),
                "confidence_source": confidence_source,
                "retrieval_scores": [r.get('score', 0) for r in rag_context],
                "forecast_context": {
                    "model_used": metadata.get('model_used', 'unknown'),
                    "target_level": request.target_level,
                    "target_value": request.target_value,
                    "historical_points": len(historical),
                    "forecast_points": len(forecast_points),
                    "forecast_stable": stable_forecast,
                    "forecast_span": round(float(forecast_span), 3),
                    "business_drivers": business_drivers,
                },
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Forecast Q&A failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/explain/factors")
async def get_forecast_factors(request: ForecastFactorsRequest):
    """
    Get key factors (feature importance) explaining a forecast
    
    - **session_id**: Upload session ID
    - **model**: Model name ("best" or specific model name)
    
    Returns:
    - Top factors driving the forecast with normalized importance scores
    - Each factor has: feature name, importance value, normalized importance (0-100)
    """
    try:
        from app.services.forecasting_service import _get_cached_importance
        
        session = session_manager.get_session(request.session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Upload session not found")
        
        # Get the model name to retrieve factors for
        model_name = request.model
        if request.model == "best":
            matching_jobs = [
                job
                for job in session_manager._training_jobs.values()
                if job.session_id == request.session_id and job.status == TrainingStatus.COMPLETED
            ]
            if matching_jobs:
                latest_job = max(matching_jobs, key=lambda job: job.created_at)
                model_name = latest_job.best_model or "naive_last"
                # Get factors from the job results
                for result in (latest_job.results or []):
                    if result.get("model") == model_name:
                        factors = result.get("feature_importance", [])
                        return {
                            "session_id": request.session_id,
                            "model": model_name,
                            "factors": factors,
                            "source": "training_results",
                        }
            else:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "No completed training job found for this session. "
                        "Run training first, or pass model='naive_last' explicitly to retrieve baseline factors."
                    ),
                )
        
        # Fallback: try to retrieve from cache
        cached_factors = _get_cached_importance(request.session_id, model_name)
        if cached_factors:
            return {
                "session_id": request.session_id,
                "model": model_name,
                "factors": cached_factors,
                "source": "cache",
            }
        
        # No factors available
        raise HTTPException(
            status_code=404,
            detail=f"No feature importance data found for model '{model_name}'. Please run training first."
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get forecast factors: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


class BacktestRequest(BaseModel):
    """Backtest request"""
    session_id: str
    model: str = "linear_regression"
    granularity: Literal["daily", "monthly"] = "daily"
    target_level: Literal["service", "product", "category", "region"] = "service"
    target_value: str | None = None
    include_promotions: bool = True
    include_price: bool = True
    include_calendar: bool = True


@router.post("/backtest")
async def run_backtest(
    request: BacktestRequest,
    db: Session = Depends(get_db),
):
    """
    Time-series cross-validation backtesting for a specific model.

    Runs 2–3 rolling folds and returns per-fold actuals vs predicted series
    (for visual overlay) plus aggregate metrics: MAE, RMSE, MAPE, SMAPE, Bias.

    Bias > 0 means the model systematically over-predicts.
    Bias < 0 means the model systematically under-predicts.
    """
    try:
        session = session_manager.get_session(request.session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Upload session not found")

        result = backtest_score(
            db=db,
            model_name=request.model,
            service_code=session.service_detected,
            granularity=request.granularity,
            target_level=request.target_level,
            target_value=request.target_value,
            include_promotions=request.include_promotions,
            include_price=request.include_price,
            include_calendar=request.include_calendar,
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Backtest failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


class ForecastExportRequest(BaseModel):
    session_id: str
    include_historical: bool = True
    # Optional: export a specific forecast identity. When omitted, exports the most
    # recent forecast for the session (unchanged behaviour).
    service_type: str | None = None
    target_level: Literal["service", "product", "category", "region"] = "service"
    target_value: str | None = None


@router.post("/export")
async def export_forecast_csv(request: ForecastExportRequest, db: Session = Depends(get_db)):
    """
    Export a generated forecast for a session as a CSV file. Targets a specific
    forecast identity (service/segment) when those params are supplied, otherwise
    the most recent forecast. Returns historical values (optional) and forecast
    values with confidence bounds.
    """
    try:
        payload = None
        if request.service_type or request.target_value or request.target_level != "service":
            try:
                service_code = resolve_service_code(db, request.service_type, None)
            except ValueError:
                service_code = None
            identity_key = _forecast_cache_key(
                request.session_id, service_code, request.target_level, request.target_value
            )
            payload = session_manager.get_cached_forecast(identity_key)
        if not payload:
            payload = session_manager.get_cached_forecast(f"{request.session_id}:forecast:last")
        if not payload:
            raise HTTPException(
                status_code=404,
                detail="No forecast found for this session. Please generate a forecast first.",
            )

        metadata = payload.get("metadata", {})
        model_used = metadata.get("model_used", "unknown")

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["date", "type", "value", "lower_bound", "upper_bound", "model"])

        if request.include_historical:
            for row in payload.get("historical", []):
                writer.writerow([row.get("date"), "historical", row.get("value"), "", "", model_used])

        for row in payload.get("forecast", []):
            writer.writerow([
                row.get("date"),
                "forecast",
                row.get("value"),
                row.get("lower_bound", ""),
                row.get("upper_bound", ""),
                model_used,
            ])

        output.seek(0)
        filename = f"forecast_{request.session_id}_{model_used}.csv"
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Forecast export failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
