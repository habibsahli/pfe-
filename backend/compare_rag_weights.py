import json
from app.services.rag_service import rag_service
from app.services.ollama_client import ollama_client
from app.core.config import settings

questions = [
    {"question": "Quelle est la tendance de vente Fibre sur les trois derniers mois ?", "expected_sources": ["monthly_fibre_report_q1.pdf"]},
    {"question": "What promotions affected 5G sales in North region?", "expected_sources": ["promo_campaigns_5g.xlsx"]},
    {"question": "Quels sont les impacts d'une remise de 20% sur les offres Data Bundle ?", "expected_sources": ["pricing_simulation_notes.md"]},
    {"question": "What does the stock anomaly dashboard say about VOD in March?", "expected_sources": ["vod_stock_anomaly_march.csv"]},
    {"question": "Quels facteurs expliquent la baisse de performance de certains dealers ?", "expected_sources": ["dealer_performance_review.docx"]},
]

cases = [("old_65_35", 0.65, 0.35), ("new_60_40", 0.60, 0.40)]
orig_dense = settings.RAG_DENSE_WEIGHT
orig_lex = settings.RAG_LEXICAL_WEIGHT
results = []
error = None

try:
    for item in questions:
        q = item["question"]
        expected = set(item["expected_sources"])
        query_vector = ollama_client.embed_text(q)

        if rag_service.milvus_store.available:
            dense = rag_service.milvus_store.search(query_vector=query_vector, top_k=int(settings.RAG_DENSE_TOP_K), service_type=None)
        else:
            fallback = rag_service.memory_store.search(query_vector=query_vector, top_k=int(settings.RAG_DENSE_TOP_K), service_type=None)
            dense = [
                {
                    "text": chunk.text,
                    "source": chunk.source,
                    "doc_type": chunk.doc_type,
                    "service_type": chunk.service_type,
                    "chunk_index": chunk.chunk_index,
                    "score": round(float(score), 4),
                }
                for chunk, score in fallback
            ]

        lexical = rag_service.lexical_store.search(query_text=q, top_k=int(settings.RAG_LEXICAL_TOP_K), service_type=None)
        row = {"question": q, "expected_sources": sorted(expected), "cases": {}}

        for label, dense_weight, lexical_weight in cases:
            settings.RAG_DENSE_WEIGHT = dense_weight
            settings.RAG_LEXICAL_WEIGHT = lexical_weight
            fused = rag_service._fuse_weighted_rrf(dense, lexical, top_k=int(settings.RAG_FUSED_TOP_K))
            reranked = rag_service._light_rerank(q, fused, top_k=5)
            top_sources = [r.get("source") for r in reranked[:5]]
            row["cases"][label] = {
                "top_sources": top_sources,
                "top_scores": [round(float(r.get("score", 0.0)), 6) for r in reranked[:5]],
                "hit_count_top5": len(expected.intersection(top_sources)),
                "best_expected_rank": next((i + 1 for i, r in enumerate(reranked[:5]) if r.get("source") in expected), None),
            }

        results.append(row)
finally:
    settings.RAG_DENSE_WEIGHT = orig_dense
    settings.RAG_LEXICAL_WEIGHT = orig_lex

payload = {"results": results, "error": error}
with open("/app/rag_compare_results.json", "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2)
print("wrote /app/rag_compare_results.json")
