"""
Variant A — RAG-augmented stock recommendation pipeline.

Flow:
  1. Run quantitative engine (StockRecommendationEngine) → per-product thresholds + risk scores
  2. For each product, build a semantic query from its characteristics
  3. Retrieve applicable policy/rule chunks via rag_service (hybrid Milvus + FTS + RRF)
  4. Pass quantitative data + policy context to LLM → natural-language justification
"""
from __future__ import annotations

import contextvars
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes, MessageAttributes

from app.core.config import settings
from app.core.tracing import get_tracer
from app.services.ollama_client import ollama_client
from app.services.rag_service import rag_service
from app.services.stock_recommendation_service import (
    RecommendationInput,
    RecommendationResponse,
    StockRecommendation,
    StockRecommendationEngine,
)

logger = logging.getLogger(__name__)
tracer = get_tracer(__name__)
_KIND = SpanAttributes.OPENINFERENCE_SPAN_KIND
_CHAIN = OpenInferenceSpanKindValues.CHAIN.value
_LLM = OpenInferenceSpanKindValues.LLM.value

_SYSTEM_PROMPT = (
    "Tu es un expert en gestion des stocks telecom (operateur Ooredoo Tunisie). "
    "Analyse les donnees quantitatives fournies et genere une analyse structuree en francais. "
    "REGLE ABSOLUE: la section 'Action requise' DOIT reproduire exactement la DECISION QUANTITATIVE "
    "indiquee dans le prompt — n'invente pas de quantite a commander, ne contredis pas la decision calculee. "
    "Si la decision est NO_ACTION ou qty_to_order=0, confirme qu'aucune commande n'est necessaire. "
    "Si la decision est URGENT ou RECOMMENDED, indique la quantite exacte issue du champ 'Quantite a commander'. "
    "Cite les procedures ou documents sources si disponibles dans le contexte. "
    "Sois concis (120 mots maximum) et structure ta reponse en trois parties numerotees: "
    "1) Action requise (basee uniquement sur la DECISION QUANTITATIVE), "
    "2) Justification chiffree, "
    "3) Reference procedure (si applicable, sinon 'N/A')."
)


def _build_rag_query(rec: StockRecommendation) -> str:
    """Derive a semantic search query from the recommendation's key risk attributes."""
    product_label = rec.product_type.lower().replace("_", " ")
    # Include product name so the retriever can find product-specific policy docs
    parts = [rec.product_name, f"politique stock securite {product_label}"]
    parts.append(f"lead time {rec.lead_time_months} mois reapprovisionnement seuil")

    if rec.rupture_risk in ("CRITICAL", "HIGH"):
        parts.append("rupture stock urgence commande immediate")
    if rec.overstock_risk == "HIGH":
        parts.append("surstock exces inventaire reduction")
    if rec.demand_trend == "INCREASING":
        parts.append("tendance hausse demande croissante ajustement seuil")
    elif rec.demand_trend == "DECREASING":
        parts.append("tendance baisse demande revision objectif stock")
    # Include governorate for site-specific retrieval
    if rec.governorate and rec.governorate != "NATIONAL":
        parts.append(rec.governorate.lower())

    return " ".join(parts)


def _build_policy_retrieval_key(rec: StockRecommendation) -> tuple:
    """Deduplication key for RAG retrieval across products that share the same policy profile.

    Policy documents are written per product type and risk category — not per product
    instance or governorate. Two products that differ only in name or site will retrieve
    the exact same chunks, so one retrieval covers both.
    """
    return (rec.product_type, rec.lead_time_months, rec.rupture_risk, rec.overstock_risk, rec.demand_trend)


def _build_user_prompt(rec: StockRecommendation, policy_context: str) -> str:
    """Assemble the LLM user prompt from quantitative data + retrieved policy chunks."""
    no_context_msg = (
        "Aucun document de politique stock n'est encore indexe dans la base vectorielle. "
        "Applique les regles standard de gestion des stocks."
    )

    if rec.qty_to_order == 0:
        decision_block = (
            f"DECISION: {rec.order_urgency}  →  AUCUNE COMMANDE REQUISE (qty=0)\n"
            "INSTRUCTION: Dans la section '1) Action requise', indique UNIQUEMENT qu'aucune commande\n"
            "n'est necessaire. N'invente PAS de quantite a commander."
        )
    else:
        decision_block = (
            f"DECISION: {rec.order_urgency}  →  COMMANDER {rec.qty_to_order:,} unites\n"
            "INSTRUCTION: Dans la section '1) Action requise', indique cette quantite exacte."
        )

    return (
        "=== DECISION QUANTITATIVE (A RESPECTER OBLIGATOIREMENT) ===\n"
        f"{decision_block}\n"
        "\n=== DONNEES PRODUIT ===\n"
        f"Produit       : {rec.product_name} (type: {rec.product_type})\n"
        f"Gouvernorat   : {rec.governorate}\n"
        f"Stock actuel  : {rec.current_stock:,} unites\n"
        f"Couverture    : {rec.coverage_months:.1f} mois ({rec.days_of_supply:.0f} jours)\n"
        f"Demande moy.  : {rec.avg_monthly_demand:.0f} unites/mois (tendance: {rec.demand_trend})\n"
        "\n=== SEUILS CALCULES ===\n"
        f"Stock securite      : {rec.safety_stock:,}\n"
        f"Point reappro (ROP) : {rec.reorder_point:,}\n"
        f"Stock cible 6 mois  : {rec.target_stock:,}\n"
        f"Quantite a commander: {rec.qty_to_order:,}\n"
        f"Lead time fournisseur: {rec.lead_time_months} mois\n"
        "\n=== EVALUATION RISQUES ===\n"
        f"Risque rupture : {rec.rupture_risk}\n"
        f"Risque surstock: {rec.overstock_risk}\n"
        f"Confiance prev.: {rec.forecast_confidence} "
        f"({rec.real_months_count} mois reels / {rec.simulated_months_count} simules)\n"
        "\n=== REGLES METIER (base documentaire) ===\n"
        f"{policy_context if policy_context.strip() else no_context_msg}\n"
        "\nGenere l'analyse structuree en respectant la DECISION QUANTITATIVE ci-dessus."
    )


def _fallback_justification(rec: StockRecommendation) -> str:
    if rec.qty_to_order > 0:
        return (
            f"[LLM indisponible] "
            f"Commander {rec.qty_to_order:,} unites ({rec.order_urgency.replace('_', ' ')}). "
            f"Risque rupture {rec.rupture_risk}, couverture {rec.coverage_months:.1f} mois."
        )
    return (
        f"[LLM indisponible] "
        f"Aucune commande requise. Stock couvre {rec.coverage_months:.1f} mois, risque rupture {rec.rupture_risk}."
    )


def enrich_with_rag(
    rec: StockRecommendation,
    service_type: str | None = None,
    top_k: int = 4,
    prefetched_chunks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Enrich one StockRecommendation with RAG retrieval + LLM justification.

    When `prefetched_chunks` is provided the retrieval step is skipped entirely —
    the caller is responsible for supplying the relevant policy chunks (e.g. from a
    deduplicated batch retrieval phase). The LLM call always runs per product because
    the quantitative data in the prompt is unique to each product instance.

    Returns all original recommendation fields plus:
      - llm_justification   : LLM-generated natural-language recommendation
      - rag_sources         : deduplicated list of cited document names
      - rag_chunks_used     : number of chunks retrieved
      - retrieval_scores    : relevance scores per chunk
      - rag_query           : the query that would be sent to the vector store
    """
    enrichment_span_name = f"RAG Enrichment · {rec.product_name} ({rec.governorate})"
    with tracer.start_as_current_span(enrichment_span_name) as span:
        span.set_attribute(_KIND, _CHAIN)
        span.set_attribute("inventory.product_name", rec.product_name)
        span.set_attribute("inventory.product_type", rec.product_type)
        span.set_attribute("inventory.governorate", rec.governorate)
        span.set_attribute("inventory.rupture_risk", rec.rupture_risk)
        span.set_attribute("inventory.top_k", top_k)
        span.set_attribute("inventory.chunks_prefetched", prefetched_chunks is not None)

        query = _build_rag_query(rec)
        span.set_attribute("inventory.rag_query", query)
        span.set_attribute(SpanAttributes.INPUT_VALUE, json.dumps({
            "product_name": rec.product_name,
            "product_type": rec.product_type,
            "governorate": rec.governorate,
            "rupture_risk": rec.rupture_risk,
            "rag_query": query,
            "chunks_prefetched": prefetched_chunks is not None,
        }))
        span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "application/json")

        if prefetched_chunks is not None:
            chunks = prefetched_chunks
        else:
            try:
                chunks = rag_service.retrieve(query, service_type=service_type, top_k=top_k)
            except Exception as exc:
                logger.warning("RAG retrieval failed for '%s': %s", rec.product_name, exc)
                chunks = []

        span.set_attribute("inventory.rag_chunks_retrieved", len(chunks))

        if chunks:
            policy_context = "\n\n".join(
                f"[SOURCE: {c.get('source', 'unknown')}]\n{c.get('text', '')}" for c in chunks
            )
            sources = list(dict.fromkeys(c.get("source", "unknown") for c in chunks))
            retrieval_scores = [round(float(c.get("score", 0.0)), 4) for c in chunks]
        else:
            policy_context = ""
            sources = []
            retrieval_scores = []

        span.set_attribute("inventory.rag_sources_count", len(sources))

        user_prompt = _build_user_prompt(rec, policy_context)

        llm_used = False
        llm_span_name = f"LLM Generation · {rec.product_name}"
        with tracer.start_as_current_span(llm_span_name) as llm_span:
            llm_span.set_attribute(_KIND, _LLM)
            llm_span.set_attribute(SpanAttributes.LLM_MODEL_NAME, settings.OLLAMA_LLM_MODEL)
            llm_span.set_attribute(SpanAttributes.LLM_SYSTEM, _SYSTEM_PROMPT)
            llm_span.set_attribute(SpanAttributes.INPUT_VALUE, user_prompt)
            llm_span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "text/plain")
            llm_span.set_attribute(f"{SpanAttributes.LLM_INPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_ROLE}", "system")
            llm_span.set_attribute(f"{SpanAttributes.LLM_INPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_CONTENT}", _SYSTEM_PROMPT)
            llm_span.set_attribute(f"{SpanAttributes.LLM_INPUT_MESSAGES}.1.{MessageAttributes.MESSAGE_ROLE}", "user")
            llm_span.set_attribute(f"{SpanAttributes.LLM_INPUT_MESSAGES}.1.{MessageAttributes.MESSAGE_CONTENT}", user_prompt)
            try:
                justification, llm_meta = ollama_client.generate_with_meta(
                    prompt=user_prompt,
                    system_prompt=_SYSTEM_PROMPT,
                    model=settings.OLLAMA_LLM_MODEL,
                    temperature=0.1,
                    max_tokens=150,
                    timeout=90,
                    retries=0,
                )
                llm_used = True
                llm_span.set_attribute(f"{SpanAttributes.LLM_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_ROLE}", "assistant")
                llm_span.set_attribute(f"{SpanAttributes.LLM_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_CONTENT}", justification)
                llm_span.set_attribute(SpanAttributes.OUTPUT_VALUE, justification)
                llm_span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "text/plain")
                # Token usage — powers Phoenix's per-trace/project usage rollups and
                # lets us correlate prompt size (driven by rag_top_k) with latency.
                if llm_meta.get("prompt_tokens") is not None:
                    llm_span.set_attribute(SpanAttributes.LLM_TOKEN_COUNT_PROMPT, llm_meta["prompt_tokens"])
                if llm_meta.get("completion_tokens") is not None:
                    llm_span.set_attribute(SpanAttributes.LLM_TOKEN_COUNT_COMPLETION, llm_meta["completion_tokens"])
                if llm_meta.get("total_tokens") is not None:
                    llm_span.set_attribute(SpanAttributes.LLM_TOKEN_COUNT_TOTAL, llm_meta["total_tokens"])
                if llm_meta.get("total_duration_ms") is not None:
                    llm_span.set_attribute("llm.total_duration_ms", round(llm_meta["total_duration_ms"], 1))
                span.set_attribute("inventory.llm_used", True)
                span.set_attribute("inventory.llm_model", settings.OLLAMA_LLM_MODEL)
            except Exception as exc:
                logger.warning("LLM generation failed for '%s': %s", rec.product_name, exc)
                justification = _fallback_justification(rec)
                llm_span.set_attribute(f"{SpanAttributes.LLM_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_ROLE}", "assistant")
                llm_span.set_attribute(f"{SpanAttributes.LLM_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_CONTENT}", justification)
                llm_span.set_attribute(SpanAttributes.OUTPUT_VALUE, justification)
                llm_span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "text/plain")
                llm_span.set_attribute("llm.fallback", True)
                llm_span.set_attribute("llm.error", str(exc))
                span.set_attribute("inventory.llm_used", False)
                span.set_attribute("inventory.llm_fallback", True)

        span.set_attribute(SpanAttributes.OUTPUT_VALUE, json.dumps({
            "rag_chunks_retrieved": len(chunks),
            "rag_sources_count": len(sources),
            "llm_used": llm_used,
        }))
        span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "application/json")

        return {
            **vars(rec),
            "llm_justification": justification,
            "rag_sources": sources,
            "rag_chunks_used": len(chunks),
            "retrieval_scores": retrieval_scores,
            "rag_query": query,
        }


def generate_recommendations_with_rag(
    inputs: list[RecommendationInput],
    z_score: float = 1.65,
    service_type: str | None = None,
    top_k_per_product: int = 4,
) -> dict[str, Any]:
    """
    Full Variant A pipeline for a batch of products.

    Steps:
      1. Quantitative engine → StockRecommendation per product
      2. Deduplicated batch retrieval → policy chunks shared across products
         with the same (product_type, lead_time, rupture_risk, overstock_risk, demand_trend)
      3. Parallel LLM enrichment → per-product justification using pre-fetched chunks
    """
    with tracer.start_as_current_span("Quantitative Engine + RAG Enrichment") as root_span:
        root_span.set_attribute(_KIND, _CHAIN)
        root_span.set_attribute("inventory.product_count", len(inputs))
        root_span.set_attribute("inventory.z_score", z_score)
        root_span.set_attribute("inventory.top_k_per_product", top_k_per_product)
        root_span.set_attribute("inventory.llm_model", settings.OLLAMA_LLM_MODEL)

        quant_response: RecommendationResponse = StockRecommendationEngine.generate_recommendations(
            inputs=inputs,
            z_score=z_score,
        )
        recs = quant_response.recommendations

        # ── Phase 1: deduplicated batch retrieval ──────────────────────────────
        # Group products by their policy retrieval key. Products that share the same
        # (product_type, lead_time, risk profile) retrieve the same policy documents,
        # so one Milvus+Postgres call covers all of them.
        rec_keys = [_build_policy_retrieval_key(rec) for rec in recs]
        unique_key_to_query: dict[tuple, str] = {}
        for rec, key in zip(recs, rec_keys):
            if key not in unique_key_to_query:
                unique_key_to_query[key] = _build_rag_query(rec)

        n_products = len(recs)
        n_unique = len(unique_key_to_query)
        root_span.set_attribute("inventory.retrieval_calls_total", n_products)
        root_span.set_attribute("inventory.retrieval_calls_deduplicated", n_unique)
        logger.info(
            "RAG batch retrieval: %d products → %d unique retrieval keys (saved %d calls)",
            n_products, n_unique, n_products - n_unique,
        )

        chunks_by_key: dict[tuple, list[dict[str, Any]]] = {}
        with tracer.start_as_current_span("RAG Batch Retrieval") as retrieval_span:
            retrieval_span.set_attribute(_KIND, _CHAIN)
            retrieval_span.set_attribute("inventory.unique_queries", n_unique)

            with ThreadPoolExecutor(max_workers=min(n_unique, 8)) as pool:
                key_futures = {
                    pool.submit(
                        contextvars.copy_context().run,
                        rag_service.retrieve,
                        query,
                        service_type,
                        top_k_per_product,
                    ): key
                    for key, query in unique_key_to_query.items()
                }
                for future in as_completed(key_futures):
                    key = key_futures[future]
                    try:
                        chunks_by_key[key] = future.result()
                    except Exception as exc:
                        logger.warning("RAG retrieval failed for key %s: %s", key, exc)
                        chunks_by_key[key] = []

            retrieval_span.set_attribute("inventory.keys_retrieved", len(chunks_by_key))

        # ── Phase 2: parallel LLM enrichment with pre-fetched chunks ───────────
        # Each product gets its own LLM call (quantitative data is per-instance),
        # but the retrieval step inside enrich_with_rag is bypassed.
        enriched_map: dict[int, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=min(4, n_products or 1)) as pool:
            futures = {
                pool.submit(
                    contextvars.copy_context().run,
                    enrich_with_rag,
                    rec,
                    service_type,
                    top_k_per_product,
                    chunks_by_key.get(rec_keys[idx], []),
                ): idx
                for idx, rec in enumerate(recs)
            }
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    enriched_map[idx] = future.result()
                except Exception as exc:
                    logger.warning("Enrichment failed for product index %d: %s", idx, exc)
                    rec = recs[idx]
                    enriched_map[idx] = {
                        **vars(rec),
                        "llm_justification": _fallback_justification(rec),
                        "rag_sources": [], "rag_chunks_used": 0,
                        "retrieval_scores": [], "rag_query": "",
                    }
        enriched = [enriched_map[i] for i in sorted(enriched_map)]

        root_span.set_attribute("inventory.enriched_count", len(enriched))
        llm_success = sum(1 for e in enriched if not e.get("llm_justification", "").startswith("[LLM indisponible]"))
        root_span.set_attribute("inventory.llm_success_count", llm_success)

        return {
            "recommendations": enriched,
            "summary": quant_response.summary,
            "metadata": {
                **quant_response.metadata,
                "rag_enabled": True,
                "llm_model": settings.OLLAMA_LLM_MODEL,
                "rag_top_k_per_product": top_k_per_product,
            },
        }
