#!/usr/bin/env python
"""
Compare Q&A retrieval quality before and after RAG weight change.
Tests with both old (65/35) and new (60/40) weights and captures metrics.
"""

import json
from app.core.config import settings
from app.services.rag_service import rag_service
from app.services.ollama_client import ollama_client
from app.core.tracing import get_tracer

qa_tracer = get_tracer(__name__)

QUESTIONS = [
    "Quelle est la tendance de vente Fibre sur les trois derniers mois ?",
    "Quelles promotions ont influence les ventes 5G dans la region Nord ?",
    "Quels sont les impacts d'une remise de 20 % sur les offres Data Bundle ?",
]

WEIGHT_CASES = [
    ("old_65_35", 0.65, 0.35),
    ("new_60_40", 0.60, 0.40),
]


def test_retrieval_with_weights(question: str, dense_weight: float, lex_weight: float, label: str) -> dict:
    """
    Test retrieval for a question with specific weights.
    Traces the retrieval pipeline.
    """
    
    # Temporarily set weights
    original_dense = settings.RAG_DENSE_WEIGHT
    original_lex = settings.RAG_LEXICAL_WEIGHT
    settings.RAG_DENSE_WEIGHT = dense_weight
    settings.RAG_LEXICAL_WEIGHT = lex_weight

    result = {
        "label": label,
        "question": question,
        "dense_weight": dense_weight,
        "lex_weight": lex_weight,
        "retrieval_results": None,
        "error": None,
    }

    try:
        # Embed and retrieve with current weights
        query_vector = ollama_client.embed_text(question)

        # Dense retrieval
        if rag_service.milvus_store.available:
            dense_results = rag_service.milvus_store.search(
                query_vector=query_vector,
                top_k=int(settings.RAG_DENSE_TOP_K),
                service_type=None,
            )
            backend_used = "milvus"
        else:
            fallback = rag_service.memory_store.search(
                query_vector=query_vector,
                top_k=int(settings.RAG_DENSE_TOP_K),
                service_type=None,
            )
            dense_results = [
                {
                    "text": chunk.text,
                    "source": chunk.source,
                    "doc_type": chunk.doc_type,
                    "score": round(float(score), 4),
                }
                for chunk, score in fallback
            ]
            backend_used = "in_memory"

        # Lexical retrieval
        lexical_results = rag_service.lexical_store.search(
            query_text=question,
            top_k=int(settings.RAG_LEXICAL_TOP_K),
            service_type=None,
        )

        # Fusion
        fused = rag_service._fuse_weighted_rrf(
            dense_results,
            lexical_results,
            top_k=int(settings.RAG_FUSED_TOP_K),
        )

        # Rerank
        reranked = rag_service._light_rerank(
            question,
            fused,
            top_k=5,
        )

        result["retrieval_results"] = {
            "dense_backend": backend_used,
            "dense_count": len(dense_results),
            "lexical_count": len(lexical_results),
            "fused_count": len(fused),
            "final_top_5": [
                {
                    "source": item.get("source"),
                    "score": round(float(item.get("score", 0)), 4),
                    "dense_rank": item.get("dense_rank"),
                    "lexical_rank": item.get("lexical_rank"),
                }
                for item in reranked[:5]
            ],
        }

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {str(e)}"

    finally:
        # Restore original weights
        settings.RAG_DENSE_WEIGHT = original_dense
        settings.RAG_LEXICAL_WEIGHT = original_lex

    return result


def main() -> None:
    """Run weight comparison tests."""
    comparison_results = []

    for question in QUESTIONS:
        qa_question_results = {"question": question, "weight_cases": {}}

        for label, dense_w, lex_w in WEIGHT_CASES:
            print(f"\nTesting: {label}")
            print(f"  Question: {question[:60]}...")
            
            result = test_retrieval_with_weights(question, dense_w, lex_w, label)
            qa_question_results["weight_cases"][label] = result

            if result["error"]:
                print(f"  ERROR: {result['error']}")
            else:
                ret = result["retrieval_results"]
                print(f"  Dense: {ret['dense_count']}, Lexical: {ret['lexical_count']}, Final: {ret['final_top_5']}")

        comparison_results.append(qa_question_results)

    # Write results
    output_path = "/app/weight_comparison_results.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(comparison_results, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}")
    print(f"Results written to: {output_path}")
    print(json.dumps(comparison_results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
