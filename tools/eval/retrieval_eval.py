from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean
from typing import Any

from tools.mcp.milvus_server import MilvusMCPConfig, MilvusMCPService

DEFAULT_TOP_K = [5, 10, 15, 20]


TEMPLATE_ROWS = [
    {
        "question": "Quelle est la tendance de vente Fibre sur les trois derniers mois ?",
        "expected_sources": ["monthly_fibre_report_q1.pdf"],
    },
    {
        "question": "What promotions affected 5G sales in North region?",
        "expected_sources": ["promo_campaigns_5g.xlsx"],
    },
    {
        "question": "Quels sont les impacts d'une remise de 20% sur les offres Data Bundle ?",
        "expected_sources": ["pricing_simulation_notes.md"],
    },
    {
        "question": "What does the stock anomaly dashboard say about VOD in March?",
        "expected_sources": ["vod_stock_anomaly_march.csv"],
    },
    {
        "question": "Quels facteurs expliquent la baisse de performance de certains dealers ?",
        "expected_sources": ["dealer_performance_review.docx"],
    },
]


def ensure_dataset_exists(dataset_path: Path) -> bool:
    if dataset_path.exists():
        return True

    dataset_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(row, ensure_ascii=True) for row in TEMPLATE_ROWS]
    dataset_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Dataset template created at: {dataset_path}")
    print("Edit expected_sources or switch to expected_ids before running evaluation.")
    return False


def parse_top_k(raw: str) -> list[int]:
    values: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        values.append(max(1, int(chunk)))
    return sorted(set(values)) or DEFAULT_TOP_K


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def compute_recall(expected: set[str], retrieved: list[str], k: int) -> float:
    if not expected:
        return 0.0
    top_hits = set(retrieved[:k])
    return len(expected & top_hits) / len(expected)


def evaluate(dataset_path: Path, top_k_values: list[int], collection_name: str | None = None) -> int:
    if not ensure_dataset_exists(dataset_path):
        return 0

    records = load_jsonl(dataset_path)
    if not records:
        print("Dataset is empty.")
        return 1

    config = MilvusMCPConfig.from_env()
    service = MilvusMCPService(config)

    max_k = max(top_k_values)
    recalls: dict[int, list[float]] = {k: [] for k in top_k_values}

    for idx, row in enumerate(records, start=1):
        question = str(row.get("question", "")).strip()
        if not question:
            print(f"Skipping row {idx}: missing 'question'.")
            continue

        expected_ids = {str(value) for value in row.get("expected_ids", []) if value is not None}
        expected_sources = {
            str(value).strip().lower()
            for value in row.get("expected_sources", [])
            if str(value).strip()
        }

        search_result = service.search(
            query_text=question,
            top_k=max_k,
            filters=None,
            collection_name=collection_name,
        )
        if not search_result.get("ok"):
            print(f"Row {idx} search error: {search_result.get('error')}")
            continue

        result_rows = search_result.get("results", [])
        retrieved_ids = [str(item.get("id")) for item in result_rows if item.get("id") is not None]
        retrieved_sources = [
            str(item.get("source") or item.get("doc_source") or "").strip().lower()
            for item in result_rows
            if str(item.get("source") or item.get("doc_source") or "").strip()
        ]

        if expected_ids:
            expected = expected_ids
            retrieved = retrieved_ids
        else:
            expected = expected_sources
            retrieved = retrieved_sources

        if not expected:
            print(f"Skipping row {idx}: no expected_ids or expected_sources provided.")
            continue

        for k in top_k_values:
            recalls[k].append(compute_recall(expected, retrieved, k))

    print("\nRetrieval evaluation results")
    print("-" * 40)
    for k in top_k_values:
        values = recalls[k]
        score = mean(values) if values else 0.0
        print(f"Recall@{k}: {score:.4f} ({len(values)} samples)")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Simple retrieval Recall@k evaluator")
    parser.add_argument(
        "--dataset",
        default="eval/questions.jsonl",
        help="Path to JSONL dataset with question + expected_ids or expected_sources.",
    )
    parser.add_argument(
        "--top-k",
        default=",".join(str(value) for value in DEFAULT_TOP_K),
        help="Comma-separated list of k values. Example: 5,10,20",
    )
    parser.add_argument(
        "--collection",
        default=None,
        help="Override collection name. Defaults to MILVUS_COLLECTION env var.",
    )

    args = parser.parse_args()
    dataset_path = Path(args.dataset)
    top_k_values = parse_top_k(args.top_k)
    return evaluate(dataset_path=dataset_path, top_k_values=top_k_values, collection_name=args.collection)


if __name__ == "__main__":
    raise SystemExit(main())
