from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from openinference.semconv.trace import (
    DocumentAttributes,
    EmbeddingAttributes,
    MessageAttributes,
    OpenInferenceSpanKindValues,
    RerankerAttributes,
    SpanAttributes,
)
from sqlalchemy import text

from app.core.config import settings
from app.core.tracing import get_tracer
from app.db.session import engine
from app.services.ollama_client import ollama_client

logger = logging.getLogger(__name__)
qa_tracer = get_tracer(__name__)

# OpenInference span kind shorthand
_CHAIN = OpenInferenceSpanKindValues.CHAIN.value
_LLM = OpenInferenceSpanKindValues.LLM.value
_RETRIEVER = OpenInferenceSpanKindValues.RETRIEVER.value
_EMBEDDING = OpenInferenceSpanKindValues.EMBEDDING.value
_RERANKER = OpenInferenceSpanKindValues.RERANKER.value
_KIND = SpanAttributes.OPENINFERENCE_SPAN_KIND


def _set_documents(span: Any, docs: list[dict[str, Any]], prefix: str = SpanAttributes.RETRIEVAL_DOCUMENTS) -> None:
    """Write a list of retrieved docs using OpenInference indexed attribute format."""
    for i, doc in enumerate(docs):
        base = f"{prefix}.{i}"
        span.set_attribute(f"{base}.{DocumentAttributes.DOCUMENT_CONTENT}", (doc.get("text") or "")[:2000])
        span.set_attribute(f"{base}.{DocumentAttributes.DOCUMENT_ID}", doc.get("source") or "")
        span.set_attribute(f"{base}.{DocumentAttributes.DOCUMENT_SCORE}", float(doc.get("score") or 0.0))
        meta = {k: v for k, v in doc.items() if k not in ("text", "source", "score", "embedding")}
        if meta:
            span.set_attribute(f"{base}.{DocumentAttributes.DOCUMENT_METADATA}", json.dumps(meta))


@dataclass
class ChunkItem:
    text: str
    source: str
    doc_type: str
    service_type: str
    chunk_index: int
    embedding: list[float]


class InMemoryVectorStore:
    def __init__(self) -> None:
        self.items: list[ChunkItem] = []

    def add(self, item: ChunkItem) -> None:
        self.items.append(item)

    def search(self, query_vector: list[float], top_k: int = 5, service_type: str | None = None) -> list[tuple[ChunkItem, float]]:
        if not self.items:
            return []

        q = np.asarray(query_vector, dtype=float)
        q_norm = np.linalg.norm(q)
        if q_norm == 0:
            return []

        scored: list[tuple[ChunkItem, float]] = []
        for item in self.items:
            if service_type and item.service_type not in {service_type, "MULTI", "UNKNOWN"}:
                continue
            v = np.asarray(item.embedding, dtype=float)
            v_norm = np.linalg.norm(v)
            if v_norm == 0:
                continue
            sim = float(np.dot(q, v) / (q_norm * v_norm))
            scored.append((item, sim))

        scored.sort(key=lambda elem: elem[1], reverse=True)
        return scored[:top_k]


class MilvusVectorStore:
    def __init__(self) -> None:
        self.host = settings.MILVUS_HOST
        self.port = str(settings.MILVUS_PORT)
        self.collection_name = settings.MILVUS_COLLECTION_NAME
        self.dim = settings.MILVUS_EMBEDDING_DIM
        self.available = False
        self._collection = None
        self._init_error: str | None = None
        # Throttle reconnect attempts so a down Milvus doesn't add a connect-timeout
        # to every search; retry at most once per window.
        self._last_reconnect_ts = 0.0
        self._reconnect_throttle_sec = 30.0
        self._initialize()

    def _initialize(self) -> None:
        try:
            from pymilvus import Collection, CollectionSchema, DataType, FieldSchema, connections, utility

            # Disconnect first so pymilvus doesn't reuse a stale broken alias from a prior failed attempt.
            try:
                connections.disconnect("default")
            except Exception:
                pass
            connections.connect(alias="default", host=self.host, port=self.port)

            if not utility.has_collection(self.collection_name):
                fields = [
                    FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),
                    FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=self.dim),
                    FieldSchema(name="text_chunk", dtype=DataType.VARCHAR, max_length=5000),
                    FieldSchema(name="doc_source", dtype=DataType.VARCHAR, max_length=500),
                    FieldSchema(name="doc_type", dtype=DataType.VARCHAR, max_length=100),
                    FieldSchema(name="service_type", dtype=DataType.VARCHAR, max_length=50),
                    FieldSchema(name="region", dtype=DataType.VARCHAR, max_length=100),
                    FieldSchema(name="page_number", dtype=DataType.INT32),
                    FieldSchema(name="chunk_index", dtype=DataType.INT32),
                ]
                schema = CollectionSchema(fields=fields, description="RAG knowledge base")
                collection = Collection(name=self.collection_name, schema=schema)
                collection.create_index(
                    field_name="embedding",
                    index_params={
                        "metric_type": "COSINE",
                        "index_type": "HNSW",
                        "params": {"M": 16, "efConstruction": 200},
                    },
                )
            else:
                collection = Collection(name=self.collection_name)

            collection.load()
            self._collection = collection
            self.available = True
            self._init_error = None
            logger.info("Milvus collection ready: %s", self.collection_name)
        except Exception as exc:
            self._init_error = str(exc)
            self.available = False
            logger.warning("Milvus unavailable, falling back to in-memory store: %s", exc)

    def _ensure_connected(self) -> bool:
        if self.available and self._collection is not None:
            return True
        # Milvus is down; only re-attempt once per throttle window so we don't pay a
        # connect timeout on every call while it's unavailable. This lets the backend
        # self-heal when Milvus comes back (previously it stayed in-memory until a
        # full backend restart).
        now = time.monotonic()
        if now - self._last_reconnect_ts < self._reconnect_throttle_sec:
            return False
        self._last_reconnect_ts = now
        logger.info("Milvus not connected — attempting reconnect...")
        self._initialize()
        return self.available and self._collection is not None

    def add_batch(
        self,
        chunks: list[str],
        embeddings: list[list[float]],
        source: str,
        doc_type: str,
        service_type: str,
    ) -> int:
        if not chunks:
            return 0
        if not self._ensure_connected():
            return 0

        region_col = [""] * len(chunks)
        page_col = [-1] * len(chunks)
        chunk_idx_col = list(range(len(chunks)))

        self._collection.insert(
            [
                embeddings,
                chunks,
                [source] * len(chunks),
                [doc_type] * len(chunks),
                [service_type] * len(chunks),
                region_col,
                page_col,
                chunk_idx_col,
            ]
        )
        self._collection.flush()
        return len(chunks)

    def search(self, query_vector: list[float], top_k: int = 5, service_type: str | None = None) -> list[dict[str, Any]]:
        if not self._ensure_connected():
            return []

        expr = None
        if service_type:
            safe_service = service_type.upper().replace('"', "")
            expr = f'service_type in ["{safe_service}", "MULTI", "UNKNOWN"]'

        results = self._collection.search(
            data=[query_vector],
            anns_field="embedding",
            param={"metric_type": "COSINE", "params": {"ef": 64}},
            limit=top_k,
            expr=expr,
            output_fields=["text_chunk", "doc_source", "doc_type", "service_type", "chunk_index"],
        )

        out: list[dict[str, Any]] = []
        for hit in results[0] if results else []:
            entity = hit.entity
            out.append(
                {
                    "text": entity.get("text_chunk"),
                    "source": entity.get("doc_source"),
                    "doc_type": entity.get("doc_type"),
                    "service_type": entity.get("service_type"),
                    "chunk_index": entity.get("chunk_index"),
                    "score": round(float(hit.score), 4),
                }
            )
        return out

    def count(self) -> int:
        if not self._ensure_connected():
            return 0
        return int(self._collection.num_entities)


class PostgresLexicalStore:
    def __init__(self) -> None:
        self.available = False
        self._init_error: str | None = None
        self._initialize()

    def _initialize(self) -> None:
        ddl = """
        CREATE TABLE IF NOT EXISTS rag_chunks_lexical (
            id BIGSERIAL PRIMARY KEY,
            text_chunk TEXT NOT NULL,
            doc_source VARCHAR(500) NOT NULL,
            doc_type VARCHAR(100) NOT NULL,
            service_type VARCHAR(50) NOT NULL,
            chunk_index INTEGER NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (doc_source, chunk_index, service_type)
        );
        CREATE INDEX IF NOT EXISTS idx_rag_chunks_lexical_service ON rag_chunks_lexical(service_type);
        CREATE INDEX IF NOT EXISTS idx_rag_chunks_lexical_fts_french
            ON rag_chunks_lexical USING GIN (to_tsvector('french', text_chunk));
        """
        try:
            with engine.begin() as conn:
                conn.execute(text(ddl))
                # One-time migration: drop the old 'simple'-dictionary index if it
                # still exists (created before the 'french' dictionary upgrade).
                conn.execute(text(
                    "DROP INDEX IF EXISTS idx_rag_chunks_lexical_fts"
                ))
            self.available = True
            self._init_error = None
        except Exception as exc:
            self.available = False
            self._init_error = str(exc)
            logger.warning("Postgres lexical store unavailable: %s", exc)

    def upsert_batch(self, chunks: list[str], source: str, doc_type: str, service_type: str) -> int:
        if not chunks:
            return 0
        if not self.available:
            self._initialize()
        if not self.available:
            return 0

        rows = [
            {
                "text_chunk": chunk,
                "doc_source": source,
                "doc_type": doc_type,
                "service_type": service_type,
                "chunk_index": idx,
            }
            for idx, chunk in enumerate(chunks)
        ]

        stmt = text(
            """
            INSERT INTO rag_chunks_lexical (text_chunk, doc_source, doc_type, service_type, chunk_index)
            VALUES (:text_chunk, :doc_source, :doc_type, :service_type, :chunk_index)
            ON CONFLICT (doc_source, chunk_index, service_type)
            DO UPDATE SET
                text_chunk = EXCLUDED.text_chunk,
                doc_type = EXCLUDED.doc_type
            """
        )
        try:
            with engine.begin() as conn:
                conn.execute(stmt, rows)
            return len(rows)
        except Exception as exc:
            logger.warning("Lexical upsert failed: %s", exc)
            return 0

    def search(self, query_text: str, top_k: int = 20, service_type: str | None = None) -> list[dict[str, Any]]:
        if not query_text.strip():
            return []
        if not self.available:
            self._initialize()
        if not self.available:
            return []

        sql = """
SELECT DISTINCT
    text_chunk,
    doc_source,
    doc_type,
    service_type,
    chunk_index,
    ts_rank_cd(to_tsvector('french', text_chunk), websearch_to_tsquery('french', :query)) AS score
FROM rag_chunks_lexical
WHERE to_tsvector('french', text_chunk) @@ websearch_to_tsquery('french', :query)
  AND (:service_type IS NULL OR service_type IN (:service_type, 'MULTI', 'UNKNOWN'))
ORDER BY score DESC
LIMIT :top_k
"""
        try:
            with engine.begin() as conn:
                rows = conn.execute(
                    text(sql),
                    {
                        "query": query_text,
                        "service_type": service_type.upper() if service_type else None,
                        "top_k": int(top_k),
                    },
                ).mappings()

                return [
                    {
                        "text": row["text_chunk"],
                        "source": row["doc_source"],
                        "doc_type": row["doc_type"],
                        "service_type": row["service_type"],
                        "chunk_index": row["chunk_index"],
                        "score": round(float(row["score"]), 4),
                    }
                    for row in rows
                ]
        except Exception as exc:
            logger.warning("Lexical search failed: %s", exc)
            return []

    def count(self) -> int:
        if not self.available:
            self._initialize()
        if not self.available:
            return 0
        try:
            with engine.begin() as conn:
                value = conn.execute(text("SELECT COUNT(*) FROM rag_chunks_lexical")).scalar()
                return int(value or 0)
        except Exception:
            return 0


# ── QA pipeline helpers ───────────────────────────────────────────────────────

# Minimum top-1 retrieval score required to call the LLM.
# Below this threshold the retrieved chunks are probably noise (off-topic question),
# so we return a "je ne sais pas" response instead of letting the LLM answer from its
# own parametric knowledge while citing irrelevant docs. Calibrated from observed top
# scores: in-domain questions land ≥0.65, clearly off-topic (e.g. "capital of France")
# land ~0.33 — 0.20 was too permissive and let those through. Overridable via settings.
_LOW_CONFIDENCE_THRESHOLD = settings.RAG_LOW_CONFIDENCE_THRESHOLD

# Intent keyword map — used to tag questions and route live-data injection.
# Multiple intents can match; the first match wins.
_INTENT_KEYWORDS: dict[str, list[str]] = {
    "stock":     ["stock", "rupture", "inventaire", "disponib", "approvisionnement",
                  "livraison", "réappro", "reappro", "entrepôt", "entrepot"],
    "vente":     ["vente", "ventes", "vendre", "commercial", "chiffre", "revenu",
                  "ca ", "c.a.", "souscription", "activation", "abonnement"],
    "prevision": ["prévision", "prevision", "forecast", "tendance", "prédiction",
                  "prediction", "horizon", "futur", "projection"],
    "procedure": ["procédure", "procedure", "comment", "étapes", "etapes",
                  "traitement", "processus", "comment faire", "démarche"],
}


def _detect_intent(question: str) -> str:
    """Return the dominant intent tag for a question, or 'general' if none match."""
    lower = question.lower()
    for intent, keywords in _INTENT_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return intent
    return "general"


def _fetch_live_kpis(service_type: str | None) -> str:
    """
    Query the mart for recent sales and current stock figures.

    Returns a formatted string ready to inject into the LLM prompt.
    Gracefully returns an empty string if the mart tables are unavailable.
    Fulfils spec §: "données agrégées (option)" for the chatbot.
    """
    lines: list[str] = []

    # Recent 3-month sales by service
    try:
        svc_filter = ""
        params: dict[str, Any] = {}
        if service_type:
            svc_filter = "WHERE UPPER(ds.service_code) = :svc"
            params["svc"] = service_type.upper()
        with engine.begin() as conn:
            rows = conn.execute(
                text(f"""
                    SELECT ds.service_code,
                           ROUND(AVG(mv.nb_ventes)::numeric, 0) AS avg_monthly,
                           SUM(mv.nb_ventes)                    AS total_3m
                    FROM mart.vw_monthly_sales_forecasting mv
                    JOIN mart.dim_services ds ON mv.service_id = ds.service_id
                    WHERE mv.month_start >= NOW() - INTERVAL '3 months'
                    {svc_filter}
                    GROUP BY ds.service_code
                    ORDER BY ds.service_code
                """),
                params,
            ).mappings().all()
        if rows:
            lines.append("=== Ventes récentes (3 derniers mois) ===")
            for r in rows:
                lines.append(
                    f"  {r['service_code']}: moyenne mensuelle {r['avg_monthly']} unités "
                    f"(total 3 mois: {r['total_3m']})"
                )
    except Exception as exc:
        logger.debug("Live KPI sales query skipped: %s", exc)

    # Current stock snapshot
    try:
        svc_stock_filter = ""
        stock_params: dict[str, Any] = {}
        if service_type:
            svc_stock_filter = "AND UPPER(ds.service_code) = :svc"
            stock_params["svc"] = service_type.upper()
        with engine.begin() as conn:
            rows = conn.execute(
                text(f"""
                    SELECT dp.product_family,
                           SUM(fs.available_qty)  AS stock_dispo,
                           MAX(fs.snapshot_date)  AS snapshot
                    FROM mart.fact_stock fs
                    JOIN mart.dim_products dp  ON fs.product_id = dp.product_id
                    JOIN mart.dim_services  ds ON dp.service_id  = ds.service_id
                    WHERE fs.snapshot_date = (SELECT MAX(snapshot_date) FROM mart.fact_stock)
                    {svc_stock_filter}
                    GROUP BY dp.product_family
                    ORDER BY stock_dispo DESC
                    LIMIT 10
                """),
                stock_params,
            ).mappings().all()
        if rows:
            snapshot_date = rows[0]["snapshot"] if rows else "?"
            lines.append(f"=== Stock disponible (snapshot du {snapshot_date}) ===")
            for r in rows:
                lines.append(f"  {r['product_family']}: {int(r['stock_dispo'] or 0)} unités")
    except Exception as exc:
        logger.debug("Live KPI stock query skipped: %s", exc)

    return "\n".join(lines)


class RAGService:
    def __init__(self) -> None:
        self.memory_store = InMemoryVectorStore()
        self.milvus_store = MilvusVectorStore()
        self.lexical_store = PostgresLexicalStore()
        self.total_documents = 0
        self.total_chunks = 0
        self.last_ingestion: str | None = None

    def hydrate_memory_store(self) -> int:
        """Re-embed all chunks from PostgreSQL lexical store into the in-memory vector store.

        Called at startup so dense vector search works after a server restart without
        requiring Milvus. Skips re-embedding if the in-memory store already has items.
        """
        if self.memory_store.items:
            return 0
        if not self.lexical_store.available:
            return 0
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    text("SELECT text_chunk, doc_source, doc_type, service_type, chunk_index FROM rag_chunks_lexical ORDER BY doc_source, chunk_index")
                ).fetchall()
        except Exception as exc:
            logger.warning("Memory store hydration failed (DB read): %s", exc)
            return 0

        if not rows:
            return 0

        texts = [r[0] for r in rows]
        try:
            embeddings = ollama_client.embed_batch(texts)
        except Exception as exc:
            logger.warning("Memory store hydration failed (embed): %s", exc)
            return 0

        count = 0
        for row, emb in zip(rows, embeddings):
            self.memory_store.add(ChunkItem(
                text=row[0],
                source=row[1],
                doc_type=row[2],
                service_type=row[3],
                chunk_index=row[4],
                embedding=emb,
            ))
            count += 1

        self.total_chunks = max(self.total_chunks, count)
        logger.info("Memory store hydrated with %d chunks from PostgreSQL", count)
        return count

    @staticmethod
    def _doc_key(item: dict[str, Any]) -> str:
        return f"{item.get('source', '')}::{item.get('chunk_index', -1)}"

    @staticmethod
    def _tokenize(text_value: str) -> set[str]:
        return {tok for tok in re.findall(r"[a-z0-9]{2,}", text_value.lower())}

    def _fuse_weighted_rrf(self, dense: list[dict[str, Any]], lexical: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
        k_const = max(1, int(settings.RAG_RRF_K))
        dense_weight = float(settings.RAG_DENSE_WEIGHT)
        lexical_weight = float(settings.RAG_LEXICAL_WEIGHT)

        fused: dict[str, dict[str, Any]] = {}
        ranks: dict[str, dict[str, int]] = {}

        for rank, item in enumerate(dense, start=1):
            key = self._doc_key(item)
            fused.setdefault(key, dict(item))
            ranks.setdefault(key, {})["dense"] = rank

        for rank, item in enumerate(lexical, start=1):
            key = self._doc_key(item)
            fused.setdefault(key, dict(item))
            ranks.setdefault(key, {})["lexical"] = rank

        ranked: list[dict[str, Any]] = []
        for key, item in fused.items():
            dense_rank = ranks.get(key, {}).get("dense")
            lexical_rank = ranks.get(key, {}).get("lexical")
            dense_rrf = dense_weight / (k_const + dense_rank) if dense_rank else 0.0
            lexical_rrf = lexical_weight / (k_const + lexical_rank) if lexical_rank else 0.0
            score = dense_rrf + lexical_rrf

            merged = dict(item)
            merged["score"] = round(float(score), 6)
            merged["dense_rank"] = dense_rank
            merged["lexical_rank"] = lexical_rank
            ranked.append(merged)

        ranked.sort(key=lambda item: float(item.get("score", 0.0)), reverse=True)
        return ranked[:top_k]

    def _light_rerank(self, query: str, candidates: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
        if not candidates:
            return []

        q_tokens = self._tokenize(query)
        overlap_weight = float(settings.RAG_RERANK_OVERLAP_WEIGHT)

        rescored: list[dict[str, Any]] = []
        for item in candidates:
            c_tokens = self._tokenize(item.get("text", ""))
            overlap_ratio = 0.0
            if q_tokens:
                overlap_ratio = len(q_tokens & c_tokens) / len(q_tokens)

            base_score = float(item.get("score", 0.0))
            rerank_score = base_score + overlap_weight * overlap_ratio

            updated = dict(item)
            updated["score"] = round(rerank_score, 6)
            rescored.append(updated)

        rescored.sort(key=lambda item: float(item.get("score", 0.0)), reverse=True)
        return rescored[:top_k]

    @staticmethod
    def _tokenize_text(text_value: str) -> list[str]:
        # Split on whitespace to preserve all characters including Unicode/accented letters.
        # The ASCII-only regex alternative dropped é, è, à, ç, etc., causing words like
        # "sécurité" to be reconstructed as "s curit" after chunk reassembly.
        return text_value.split()

    @staticmethod
    def _is_heading_block(block: str) -> bool:
        stripped = block.strip()
        if not stripped:
            return False

        if "\n" in stripped:
            return False

        words = stripped.split()
        if len(words) > 14:
            return False

        if stripped.startswith("#"):
            return True

        if re.match(r"^\d+(\.\d+)*[\)\.]?\s+", stripped):
            return True

        if stripped.endswith(":"):
            return True

        letters = re.sub(r"[^A-Za-z]", "", stripped)
        if letters and letters.isupper() and len(letters) >= 4:
            return True

        return False

    @staticmethod
    def _structure_blocks(text_value: str) -> list[str]:
        normalized = text_value.replace("\r\n", "\n").replace("\r", "\n")
        rough_blocks = re.split(r"\n\s*\n+", normalized)
        blocks = [" ".join(block.split()) for block in rough_blocks if block and block.strip()]

        merged: list[str] = []
        pending_heading: str | None = None
        for block in blocks:
            if RAGService._is_heading_block(block):
                if pending_heading:
                    merged.append(pending_heading)
                pending_heading = block
                continue

            if pending_heading:
                merged.append(f"{pending_heading}\n{block}")
                pending_heading = None
            else:
                merged.append(block)

        if pending_heading:
            merged.append(pending_heading)

        return merged

    @staticmethod
    def _chunk_text(
        text_value: str,
        chunk_tokens: int | None = None,
        overlap_tokens: int | None = None,
    ) -> list[str]:
        if not text_value.strip():
            return []

        configured_chunk_tokens = settings.RAG_CHUNK_TOKENS if chunk_tokens is None else chunk_tokens
        configured_overlap_tokens = settings.RAG_CHUNK_OVERLAP_TOKENS if overlap_tokens is None else overlap_tokens

        chunk_tokens = max(80, int(configured_chunk_tokens))
        overlap_tokens = max(0, min(int(configured_overlap_tokens), chunk_tokens // 2))
        blocks = RAGService._structure_blocks(text_value)
        if not blocks:
            return []

        chunks: list[str] = []
        current_parts: list[str] = []
        current_tokens: list[str] = []

        def flush_chunk() -> None:
            nonlocal current_parts, current_tokens
            if not current_parts:
                return
            chunk_text = "\n\n".join(part for part in current_parts if part).strip()
            if chunk_text:
                chunks.append(chunk_text)

            if overlap_tokens > 0 and current_tokens:
                overlap_tail = current_tokens[-overlap_tokens:]
                current_tokens = overlap_tail.copy()
                current_parts = [" ".join(overlap_tail)] if overlap_tail else []
            else:
                current_tokens = []
                current_parts = []

        for block in blocks:
            block_tokens = RAGService._tokenize_text(block)
            if not block_tokens:
                continue

            start = 0
            while start < len(block_tokens):
                end = min(start + chunk_tokens, len(block_tokens))
                piece_tokens = block_tokens[start:end]
                piece_text = " ".join(piece_tokens)

                if current_tokens and len(current_tokens) + len(piece_tokens) > chunk_tokens:
                    flush_chunk()

                current_parts.append(piece_text)
                current_tokens.extend(piece_tokens)
                start = end

                if len(current_tokens) >= chunk_tokens:
                    flush_chunk()

        if current_parts:
            chunk_text = "\n\n".join(part for part in current_parts if part).strip()
            chunks.append(chunk_text)

        # Remove accidental duplicates introduced by overlap in small corpora.
        unique_chunks: list[str] = []
        seen: set[str] = set()
        for chunk in chunks:
            key = chunk.strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            unique_chunks.append(chunk)

        return unique_chunks

    @staticmethod
    def _read_document(path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix in {".txt", ".md", ".csv", ".log"}:
            return path.read_text(encoding="utf-8", errors="ignore")

        if suffix == ".pdf":
            try:
                from pypdf import PdfReader

                reader = PdfReader(str(path))
                return "\n".join((page.extract_text() or "") for page in reader.pages)
            except Exception:
                return ""

        if suffix in {".docx", ".doc"}:
            try:
                from docx import Document

                doc = Document(str(path))
                return "\n".join(p.text for p in doc.paragraphs)
            except Exception:
                return ""

        return ""

    def ingest_documents(self, file_paths: list[Path], service_type: str | None = None) -> dict[str, Any]:
        ingested_docs = 0
        ingested_chunks = 0

        for file_path in file_paths:
            text_value = self._read_document(file_path)
            if not text_value.strip():
                continue

            chunks = self._chunk_text(text_value)
            if not chunks:
                continue

            inferred_service = (service_type or "MULTI").upper()
            embeddings = ollama_client.embed_batch(chunks)
            doc_type = file_path.suffix.lower().lstrip(".")

            self.lexical_store.upsert_batch(
                chunks=chunks,
                source=file_path.name,
                doc_type=doc_type,
                service_type=inferred_service,
            )

            inserted_vector = 0
            if self.milvus_store.available:
                inserted_vector = self.milvus_store.add_batch(
                    chunks=chunks,
                    embeddings=embeddings,
                    source=file_path.name,
                    doc_type=doc_type,
                    service_type=inferred_service,
                )

            if inserted_vector == 0:
                for idx, (chunk, emb) in enumerate(zip(chunks, embeddings)):
                    self.memory_store.add(
                        ChunkItem(
                            text=chunk,
                            source=file_path.name,
                            doc_type=doc_type,
                            service_type=inferred_service,
                            chunk_index=idx,
                            embedding=emb,
                        )
                    )

            ingested_chunks += len(chunks)

            ingested_docs += 1

        self.total_documents += ingested_docs
        self.total_chunks += ingested_chunks
        self.last_ingestion = datetime.utcnow().isoformat()

        return {
            "status": "completed",
            "documents_ingested": ingested_docs,
            "chunks_ingested": ingested_chunks,
            "total_documents": self.total_documents,
            "total_chunks": self.total_chunks,
            "last_ingestion": self.last_ingestion,
        }

    def retrieve(self, query: str, service_type: str | None = None, top_k: int = 5) -> list[dict[str, Any]]:
        if not query.strip():
            return []

        with qa_tracer.start_as_current_span("RAG Knowledge Base Retrieval") as span:
            span.set_attribute(_KIND, _RETRIEVER)
            span.set_attribute(SpanAttributes.INPUT_VALUE, query)
            span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "text/plain")
            span.set_attribute("retrieval.service_type", service_type or "MULTI")
            span.set_attribute("retrieval.top_k_requested", top_k)
            span.set_attribute("retrieval.strategy", "hybrid_rrf + light_rerank")
            span.set_attribute("retrieval.dense_weight", float(settings.RAG_DENSE_WEIGHT))
            span.set_attribute("retrieval.lexical_weight", float(settings.RAG_LEXICAL_WEIGHT))
            span.set_attribute("retrieval.rrf_k", int(settings.RAG_RRF_K))

            with qa_tracer.start_as_current_span("Embed Question") as embed_span:
                embed_span.set_attribute(_KIND, _EMBEDDING)
                embed_span.set_attribute(SpanAttributes.EMBEDDING_MODEL_NAME, settings.OLLAMA_EMBEDDING_MODEL)
                embed_span.set_attribute(SpanAttributes.INPUT_VALUE, query)
                query_vector = ollama_client.embed_text(query)
                embed_span.set_attribute(
                    f"{SpanAttributes.EMBEDDING_EMBEDDINGS}.0.{EmbeddingAttributes.EMBEDDING_TEXT}", query
                )
                embed_span.set_attribute("embedding.dimension", len(query_vector))

            dense_k = max(top_k, int(settings.RAG_DENSE_TOP_K))
            lexical_k = max(top_k, int(settings.RAG_LEXICAL_TOP_K))
            fused_k = max(top_k, int(settings.RAG_FUSED_TOP_K))

            dense_results: list[dict[str, Any]] = []
            with qa_tracer.start_as_current_span("Dense Vector Search") as dense_span:
                dense_span.set_attribute(_KIND, _RETRIEVER)
                dense_span.set_attribute(SpanAttributes.INPUT_VALUE, query)
                # Prefer Milvus only when it's reachable AND actually populated; an
                # empty/just-recovered collection must not shadow the in-memory store
                # (which is hydrated from Postgres at startup).
                milvus_up = self.milvus_store._ensure_connected()
                use_milvus = milvus_up and self.milvus_store.count() > 0
                backend = "milvus" if use_milvus else "in_memory_fallback"
                dense_span.set_attribute("dense.backend", backend)
                dense_span.set_attribute("dense.milvus_reachable", milvus_up)
                dense_span.set_attribute("dense.top_k", dense_k)
                dense_span.set_attribute("dense.metric", "COSINE")
                if use_milvus:
                    dense_results = self.milvus_store.search(
                        query_vector=query_vector,
                        top_k=dense_k,
                        service_type=(service_type or None),
                    )
                else:
                    fallback = self.memory_store.search(
                        query_vector=query_vector,
                        top_k=dense_k,
                        service_type=(service_type or None),
                    )
                    dense_results = [
                        {
                            "text": item.text,
                            "source": item.source,
                            "doc_type": item.doc_type,
                            "service_type": item.service_type,
                            "chunk_index": item.chunk_index,
                            "score": round(float(score), 4),
                        }
                        for item, score in fallback
                    ]
                dense_span.set_attribute("dense.hits_returned", len(dense_results))
                _set_documents(dense_span, dense_results)

            with qa_tracer.start_as_current_span("Lexical Search · PostgreSQL FTS") as lexical_span:
                lexical_span.set_attribute(_KIND, _RETRIEVER)
                lexical_span.set_attribute(SpanAttributes.INPUT_VALUE, query)
                lexical_span.set_attribute("lexical.backend", "postgresql_fts")
                lexical_span.set_attribute("lexical.top_k", lexical_k)
                lexical_results = self.lexical_store.search(query_text=query, top_k=lexical_k, service_type=(service_type or None))
                lexical_span.set_attribute("lexical.hits_returned", len(lexical_results))
                _set_documents(lexical_span, lexical_results)

            if dense_results and lexical_results:
                with qa_tracer.start_as_current_span("Hybrid Fusion · RRF") as fusion_span:
                    fusion_span.set_attribute(_KIND, _CHAIN)
                    fusion_span.set_attribute("fusion.algorithm", "Reciprocal Rank Fusion (RRF)")
                    fusion_span.set_attribute("fusion.dense_weight", float(settings.RAG_DENSE_WEIGHT))
                    fusion_span.set_attribute("fusion.lexical_weight", float(settings.RAG_LEXICAL_WEIGHT))
                    fusion_span.set_attribute("fusion.rrf_k_constant", int(settings.RAG_RRF_K))
                    fusion_span.set_attribute("fusion.dense_candidates", len(dense_results))
                    fusion_span.set_attribute("fusion.lexical_candidates", len(lexical_results))
                    fused = self._fuse_weighted_rrf(dense_results, lexical_results, top_k=fused_k)
                    fusion_span.set_attribute("fusion.output_candidates", len(fused))

                with qa_tracer.start_as_current_span("Re-rank Results") as rerank_span:
                    rerank_span.set_attribute(_KIND, _RERANKER)
                    rerank_span.set_attribute(RerankerAttributes.RERANKER_QUERY, query)
                    rerank_span.set_attribute(RerankerAttributes.RERANKER_TOP_K, top_k)
                    rerank_span.set_attribute(RerankerAttributes.RERANKER_MODEL_NAME, "token_overlap_boost")
                    rerank_span.set_attribute("reranker.overlap_weight", float(settings.RAG_RERANK_OVERLAP_WEIGHT))
                    _set_documents(rerank_span, fused, prefix=RerankerAttributes.RERANKER_INPUT_DOCUMENTS)
                    reranked = self._light_rerank(query, fused, top_k=top_k)
                    _set_documents(rerank_span, reranked, prefix=RerankerAttributes.RERANKER_OUTPUT_DOCUMENTS)

                _set_documents(span, reranked)
                span.set_attribute(SpanAttributes.OUTPUT_VALUE, json.dumps([r.get("source") for r in reranked]))
                span.set_attribute("retrieval.final_chunks", len(reranked))
                span.set_attribute("retrieval.mode", "hybrid")
                return reranked

            if dense_results:
                # Deduplicate by doc key before reranking (same chunk may appear multiple
                # times in Milvus results when lexical path returns nothing and RRF is skipped)
                seen_keys: dict[str, dict] = {}
                for item in dense_results:
                    k = self._doc_key(item)
                    if k not in seen_keys:
                        seen_keys[k] = item
                deduped_dense = list(seen_keys.values())

                with qa_tracer.start_as_current_span("Re-rank Results") as rerank_span:
                    rerank_span.set_attribute(_KIND, _RERANKER)
                    rerank_span.set_attribute(RerankerAttributes.RERANKER_QUERY, query)
                    rerank_span.set_attribute(RerankerAttributes.RERANKER_TOP_K, top_k)
                    rerank_span.set_attribute(RerankerAttributes.RERANKER_MODEL_NAME, "token_overlap_boost")
                    _set_documents(rerank_span, deduped_dense[:fused_k], prefix=RerankerAttributes.RERANKER_INPUT_DOCUMENTS)
                    reranked = self._light_rerank(query, deduped_dense[:fused_k], top_k=top_k)
                    _set_documents(rerank_span, reranked, prefix=RerankerAttributes.RERANKER_OUTPUT_DOCUMENTS)

                _set_documents(span, reranked)
                span.set_attribute(SpanAttributes.OUTPUT_VALUE, json.dumps([r.get("source") for r in reranked]))
                span.set_attribute("retrieval.final_chunks", len(reranked))
                span.set_attribute("retrieval.mode", "dense_only")
                return reranked

            if lexical_results:
                # Deduplicate by doc key before reranking
                seen_keys_lex: dict[str, dict] = {}
                for item in lexical_results:
                    k = self._doc_key(item)
                    if k not in seen_keys_lex:
                        seen_keys_lex[k] = item
                deduped_lexical = list(seen_keys_lex.values())

                with qa_tracer.start_as_current_span("Re-rank Results") as rerank_span:
                    rerank_span.set_attribute(_KIND, _RERANKER)
                    rerank_span.set_attribute(RerankerAttributes.RERANKER_QUERY, query)
                    rerank_span.set_attribute(RerankerAttributes.RERANKER_TOP_K, top_k)
                    rerank_span.set_attribute(RerankerAttributes.RERANKER_MODEL_NAME, "token_overlap_boost")
                    _set_documents(rerank_span, deduped_lexical[:fused_k], prefix=RerankerAttributes.RERANKER_INPUT_DOCUMENTS)
                    reranked = self._light_rerank(query, deduped_lexical[:fused_k], top_k=top_k)
                    _set_documents(rerank_span, reranked, prefix=RerankerAttributes.RERANKER_OUTPUT_DOCUMENTS)

                _set_documents(span, reranked)
                span.set_attribute(SpanAttributes.OUTPUT_VALUE, json.dumps([r.get("source") for r in reranked]))
                span.set_attribute("retrieval.final_chunks", len(reranked))
                span.set_attribute("retrieval.mode", "lexical_only")
                return reranked

            span.set_attribute(SpanAttributes.OUTPUT_VALUE, "[]")
            span.set_attribute("retrieval.final_chunks", 0)
            span.set_attribute("retrieval.mode", "no_results")
            return []

    def answer_question(self, question: str, service_type: str | None = None) -> dict[str, Any]:
        with qa_tracer.start_as_current_span("Sales Forecast Q&A Pipeline") as root:
            root.set_attribute(_KIND, _CHAIN)
            root.set_attribute(SpanAttributes.INPUT_VALUE, question)
            root.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "text/plain")
            root.set_attribute("pipeline.service_type", service_type or "MULTI")
            root.set_attribute("pipeline.steps", "retrieve → assemble_context → build_prompt → llm_generate")

            intent = _detect_intent(question)
            root.set_attribute("qa.intent", intent)

            retrieved = self.retrieve(question, service_type=service_type, top_k=5)

            if not retrieved:
                root.set_attribute(SpanAttributes.OUTPUT_VALUE, "no_context_available")
                root.set_attribute("retrieval.chunks_found", 0)
                return {
                    "answer": "Aucun contexte n'est encore indexé dans la base de connaissances.",
                    "sources": [],
                    "confidence": 0.0,
                    "confidence_source": "no_docs",
                    "intent": intent,
                    "retrieval_scores": [],
                    "context_used": {"chunks": 0, "service_type": service_type or "MULTI"},
                }

            # Low-confidence guardrail — avoid hallucination on near-zero retrieval scores.
            top_score = max(r["score"] for r in retrieved)
            if top_score < _LOW_CONFIDENCE_THRESHOLD:
                root.set_attribute(SpanAttributes.OUTPUT_VALUE, "low_confidence_gate")
                root.set_attribute("qa.top_score", top_score)
                return {
                    "answer": (
                        "Je ne dispose pas d'informations suffisamment fiables dans la base de "
                        "connaissances pour répondre à cette question avec certitude. "
                        "Veuillez reformuler votre question ou enrichir la base documentaire."
                    ),
                    "sources": [],
                    "confidence": 0.0,
                    "confidence_source": "low_retrieval_score",
                    "intent": intent,
                    "retrieval_scores": [r["score"] for r in retrieved],
                    "context_used": {"chunks": len(retrieved), "service_type": service_type or "MULTI"},
                }

            with qa_tracer.start_as_current_span("Assemble Context from Retrieved Chunks") as ctx_span:
                ctx_span.set_attribute(_KIND, _CHAIN)
                context = "\n\n".join([f"[SOURCE: {r['source']}] {r['text']}" for r in retrieved])
                sources = [r["source"] for r in retrieved]
                ctx_span.set_attribute(SpanAttributes.INPUT_VALUE, json.dumps([r.get("source") for r in retrieved]))
                ctx_span.set_attribute(SpanAttributes.OUTPUT_VALUE, context[:2000])
                ctx_span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "text/plain")
                ctx_span.set_attribute("context.chunks_count", len(retrieved))
                ctx_span.set_attribute("context.total_chars", len(context))
                ctx_span.set_attribute("context.sources", ", ".join(dict.fromkeys(sources)))
                ctx_span.set_attribute("context.top_chunk_score", retrieved[0]["score"])

            # Fetch live KPIs for stock/vente questions (spec §: "données agrégées").
            live_kpi_block = ""
            if intent in ("stock", "vente"):
                kpis = _fetch_live_kpis(service_type)
                if kpis:
                    live_kpi_block = f"\nDONNÉES TEMPS RÉEL (base de données):\n{kpis}\n"

            intent_labels = {
                "stock": "gestion des stocks / ruptures",
                "vente": "performance commerciale / ventes",
                "prevision": "prévisions / tendances",
                "procedure": "procédures / processus opérationnels",
                "general": "question générale",
            }

            system_prompt = (
                "Tu es un analyste telecom expert. Réponds en français, de façon concise et structurée. "
                f"La question porte sur : {intent_labels.get(intent, 'question générale')}. "
                "Appuie-toi en priorité sur les données temps réel fournies (si disponibles), "
                "puis sur le contexte documentaire. Si le contexte est insuffisant, dis-le clairement."
            )
            full_prompt = (
                f"Question utilisateur: {question}\n"
                f"Service cible: {service_type or 'MULTI'}\n"
                f"Intention détectée: {intent}\n"
                f"{live_kpi_block}"
                "Contexte documentaire:\n"
                f"{context}\n\n"
                "Donne une réponse structurée avec: 1) Réponse directe 2) Points de vigilance 3) Actions recommandées."
            )

            with qa_tracer.start_as_current_span("Generate LLM Response") as llm_span:
                llm_span.set_attribute(_KIND, _LLM)
                llm_span.set_attribute(SpanAttributes.LLM_MODEL_NAME, settings.OLLAMA_LLM_MODEL)
                llm_span.set_attribute(SpanAttributes.LLM_INVOCATION_PARAMETERS, json.dumps({
                    "temperature": 0.2,
                    "max_tokens": 700,
                    "provider": "ollama",
                }))
                # System prompt — shown separately in Phoenix's chat view
                llm_span.set_attribute(SpanAttributes.LLM_SYSTEM, system_prompt)
                # Input messages (system + user) — Phoenix renders these as a chat thread
                llm_span.set_attribute(
                    f"{SpanAttributes.LLM_INPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_ROLE}", "system"
                )
                llm_span.set_attribute(
                    f"{SpanAttributes.LLM_INPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_CONTENT}", system_prompt
                )
                llm_span.set_attribute(
                    f"{SpanAttributes.LLM_INPUT_MESSAGES}.1.{MessageAttributes.MESSAGE_ROLE}", "user"
                )
                llm_span.set_attribute(
                    f"{SpanAttributes.LLM_INPUT_MESSAGES}.1.{MessageAttributes.MESSAGE_CONTENT}", full_prompt
                )
                llm_span.set_attribute(SpanAttributes.INPUT_VALUE, full_prompt)
                llm_span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "text/plain")

                answer, llm_meta = ollama_client.generate_with_meta(
                    prompt=full_prompt,
                    system_prompt=system_prompt,
                    model=settings.OLLAMA_LLM_MODEL,
                )

                # Output message — Phoenix renders this as the assistant reply
                llm_span.set_attribute(
                    f"{SpanAttributes.LLM_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_ROLE}", "assistant"
                )
                llm_span.set_attribute(
                    f"{SpanAttributes.LLM_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_CONTENT}", answer
                )
                llm_span.set_attribute(SpanAttributes.OUTPUT_VALUE, answer)
                llm_span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "text/plain")
                # Token usage — powers Phoenix's per-trace/project usage rollups and
                # lets us correlate prompt size (retrieved context) with latency.
                if llm_meta.get("prompt_tokens") is not None:
                    llm_span.set_attribute(SpanAttributes.LLM_TOKEN_COUNT_PROMPT, llm_meta["prompt_tokens"])
                if llm_meta.get("completion_tokens") is not None:
                    llm_span.set_attribute(SpanAttributes.LLM_TOKEN_COUNT_COMPLETION, llm_meta["completion_tokens"])
                if llm_meta.get("total_tokens") is not None:
                    llm_span.set_attribute(SpanAttributes.LLM_TOKEN_COUNT_TOTAL, llm_meta["total_tokens"])
                if llm_meta.get("total_duration_ms") is not None:
                    llm_span.set_attribute("llm.total_duration_ms", round(llm_meta["total_duration_ms"], 1))

            confidence = round(float(np.mean([r["score"] for r in retrieved])), 3)
            root.set_attribute(SpanAttributes.OUTPUT_VALUE, answer)
            root.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "text/plain")
            root.set_attribute("output.confidence", confidence)
            root.set_attribute("output.chunks_retrieved", len(retrieved))
            root.set_attribute("output.sources", ", ".join(dict.fromkeys(sources)))
            root.set_attribute("qa.intent", intent)

            return {
                "answer": answer,
                "sources": sources,
                "confidence": confidence,
                "confidence_source": "rag_retrieval_scores",
                "intent": intent,
                "retrieval_scores": [r["score"] for r in retrieved],
                "context_used": {
                    "chunks": len(retrieved),
                    "service_type": service_type or "MULTI",
                    "live_kpis_injected": bool(live_kpi_block),
                },
            }

    def explain_forecast(self, service_type: str, forecast_payload: dict[str, Any]) -> dict[str, Any]:
        with qa_tracer.start_as_current_span("Forecast Explain Pipeline") as root:
            root.set_attribute(_KIND, _CHAIN)
            root.set_attribute(SpanAttributes.INPUT_VALUE, json.dumps({
                "service_type": service_type,
                "metadata": (forecast_payload or {}).get("metadata", {}),
            }))
            root.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "application/json")
            root.set_attribute("pipeline.steps", "extract_forecast → build_question → rag_qa_pipeline")

            with qa_tracer.start_as_current_span("Extract Forecast Data") as ext_span:
                ext_span.set_attribute(_KIND, _CHAIN)
                metadata = forecast_payload.get("metadata", {}) if forecast_payload else {}
                forecast_points = forecast_payload.get("forecast", []) if forecast_payload else []

                model_used = metadata.get("model_used", "unknown")
                trend = metadata.get("trend", "unknown")
                change_pct = metadata.get("change_pct", "n/a")

                ext_span.set_attribute(SpanAttributes.INPUT_VALUE, json.dumps(metadata))
                ext_span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "application/json")
                ext_span.set_attribute("forecast.model_used", str(model_used))
                ext_span.set_attribute("forecast.trend", str(trend))
                ext_span.set_attribute("forecast.change_pct", str(change_pct))
                ext_span.set_attribute("forecast.num_points", len(forecast_points))

                summary = ""
                if forecast_points:
                    first_value = forecast_points[0].get("value")
                    last_value = forecast_points[-1].get("value")
                    ext_span.set_attribute("forecast.first_value", str(first_value))
                    ext_span.set_attribute("forecast.last_value", str(last_value))
                    summary = f"Valeur initiale previsionnelle={first_value}, valeur finale={last_value}."

                ext_span.set_attribute(SpanAttributes.OUTPUT_VALUE, summary or "no_forecast_points")

            with qa_tracer.start_as_current_span("Build Question from Forecast") as q_span:
                q_span.set_attribute(_KIND, _CHAIN)
                question = (
                    f"Explique la prevision du service {service_type}. "
                    f"Modele={model_used} trend={trend} "
                    f"variation={change_pct}%. {summary}"
                )
                q_span.set_attribute(SpanAttributes.INPUT_VALUE, json.dumps({
                    "service_type": service_type, "model_used": model_used,
                    "trend": trend, "change_pct": str(change_pct), "summary": summary,
                }))
                q_span.set_attribute(SpanAttributes.OUTPUT_VALUE, question)
                root.set_attribute("forecast.auto_question", question)

            response = self.answer_question(question=question, service_type=service_type)

            root.set_attribute(SpanAttributes.OUTPUT_VALUE, response["answer"])
            root.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "text/plain")
            root.set_attribute("output.confidence", response.get("confidence", 0.0))
            root.set_attribute("output.sources_count", len(response["sources"]))

            return {
                "explanation": response["answer"],
                "sources": response["sources"],
                "context_used": response.get(
                    "context_used",
                    {
                        "chunks": 0,
                        "service_type": service_type or "MULTI",
                    },
                ),
                "retrieval_scores": response.get("retrieval_scores", []),
                "llm_model": settings.OLLAMA_LLM_MODEL,
            }

    def get_status(self) -> dict[str, Any]:
        # Probe (throttled) so status reflects a Milvus that has since recovered,
        # rather than a stale flag from the last failed attempt.
        milvus_up = self.milvus_store._ensure_connected()
        return {
            "total_documents": self.total_documents,
            "total_chunks": self.total_chunks,
            "last_ingestion": self.last_ingestion,
            "embedding_model": settings.OLLAMA_EMBEDDING_MODEL,
            "llm_model": settings.OLLAMA_LLM_MODEL,
            "vector_backend": "milvus" if milvus_up else "in_memory",
            "milvus_available": milvus_up,
            "milvus_collection": settings.MILVUS_COLLECTION_NAME,
            "milvus_entities": self.milvus_store.count() if milvus_up else 0,
            "milvus_init_error": self.milvus_store._init_error,
            "lexical_backend": "postgres_fts",
            "lexical_available": self.lexical_store.available,
            "lexical_entities": self.lexical_store.count(),
            "lexical_init_error": self.lexical_store._init_error,
            "memory_store_items": len(self.memory_store.items),
            "retrieval_mode": "hybrid_rrf",
            "chunk_tokens": int(settings.RAG_CHUNK_TOKENS),
            "chunk_overlap_tokens": int(settings.RAG_CHUNK_OVERLAP_TOKENS),
        }


rag_service = RAGService()
