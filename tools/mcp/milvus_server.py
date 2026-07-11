from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any

import httpx
from pymilvus import Collection, DataType, connections, utility

def _resolve_vector_types() -> set[Any]:
    names = [
        "FLOAT_VECTOR",
        "BINARY_VECTOR",
        "FLOAT16_VECTOR",
        "BFLOAT16_VECTOR",
        "SPARSE_FLOAT_VECTOR",
    ]
    resolved: set[Any] = set()
    for name in names:
        value = getattr(DataType, name, None)
        if value is not None:
            resolved.add(value)
    return resolved


VECTOR_TYPES = _resolve_vector_types()
PREFERRED_METADATA_KEYS = [
    "title",
    "source",
    "doc_source",
    "page",
    "page_number",
    "chunk_index",
    "doc_type",
    "service_type",
]
ALLOWED_METRICS = {"COSINE", "IP", "L2"}


def _escape_expr_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


@dataclass
class MilvusMCPConfig:
    milvus_uri: str
    milvus_db: str
    default_collection: str
    ollama_host: str
    ollama_embed_model: str

    @classmethod
    def from_env(cls) -> "MilvusMCPConfig":
        return cls(
            milvus_uri=os.getenv("MILVUS_URI", "http://localhost:19530"),
            milvus_db=os.getenv("MILVUS_DB", "default"),
            default_collection=os.getenv("MILVUS_COLLECTION", "rag_chunks"),
            ollama_host=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
            ollama_embed_model=os.getenv("OLLAMA_EMBED_MODEL", "bge-m3"),
        )


class MilvusMCPService:
    def __init__(self, config: MilvusMCPConfig) -> None:
        self.config = config
        self.alias = "milvus_mcp"
        self._connected = False

    def _connect(self) -> None:
        if self._connected:
            return
        try:
            connections.connect(
                alias=self.alias,
                uri=self.config.milvus_uri,
                db_name=self.config.milvus_db,
            )
            self._connected = True
        except Exception as exc:  # pragma: no cover - runtime env specific
            raise RuntimeError(
                f"Could not connect to Milvus at {self.config.milvus_uri} (db={self.config.milvus_db}). "
                f"Set MILVUS_URI and MILVUS_DB correctly. Details: {exc}"
            ) from exc

    def _collection_name(self, collection_name: str | None) -> str:
        return (collection_name or self.config.default_collection).strip()

    def _has_collection(self, collection_name: str) -> bool:
        self._connect()
        return bool(utility.has_collection(collection_name, using=self.alias))

    def _get_collection(self, collection_name: str) -> Collection:
        self._connect()
        if not self._has_collection(collection_name):
            raise FileNotFoundError(
                f"Collection '{collection_name}' does not exist in Milvus db '{self.config.milvus_db}'. "
                "Check MILVUS_COLLECTION or pass collection_name explicitly."
            )
        collection = Collection(name=collection_name, using=self.alias)
        collection.load()
        return collection

    @staticmethod
    def _is_vector_field(field: Any) -> bool:
        return getattr(field, "dtype", None) in VECTOR_TYPES

    def _detect_vector_field(self, collection: Collection) -> tuple[str | None, int | None]:
        for field in collection.schema.fields:
            if self._is_vector_field(field):
                params = getattr(field, "params", {}) or {}
                dim = params.get("dim") if isinstance(params, dict) else None
                try:
                    dim = int(dim) if dim is not None else None
                except Exception:
                    dim = None
                return field.name, dim
        return None, None

    @staticmethod
    def _detect_pk_field(collection: Collection) -> tuple[str | None, Any | None]:
        """Detect primary key field name and dtype. Returns (field_name, dtype)."""
        for field in collection.schema.fields:
            if getattr(field, "is_primary", False):
                return field.name, getattr(field, "dtype", None)
        return None, None

    @staticmethod
    def _extract_metric_type(collection: Collection, default: str = "COSINE") -> str:
        try:
            for index in collection.indexes:
                params = getattr(index, "params", {}) or {}
                if isinstance(params, dict):
                    metric_type = params.get("metric_type")
                    if metric_type:
                        return str(metric_type).upper()
                    nested = params.get("params")
                    if isinstance(nested, dict) and nested.get("metric_type"):
                        return str(nested["metric_type"]).upper()
        except Exception:
            pass
        return default

    def _embed_text(self, text: str) -> list[float]:
        base_url = self.config.ollama_host.rstrip("/")
        candidates = [
            (
                f"{base_url}/api/embeddings",
                {"model": self.config.ollama_embed_model, "prompt": text},
                "embedding",
            ),
            (
                f"{base_url}/api/embed",
                {"model": self.config.ollama_embed_model, "input": text},
                "embeddings",
            ),
        ]

        data: dict[str, Any] | None = None
        last_error: str | None = None
        for endpoint, payload, _ in candidates:
            try:
                with httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
                    response = client.post(endpoint, json=payload)
                response.raise_for_status()
                data = response.json()
                break
            except Exception as exc:  # pragma: no cover - runtime env specific
                last_error = f"{endpoint}: {exc}"

        if data is None:
            raise RuntimeError(
                "Ollama embeddings call failed. Ensure Ollama is running and the model is pulled. "
                f"Host: {self.config.ollama_host}. Model: {self.config.ollama_embed_model}. "
                f"Try: ollama pull {self.config.ollama_embed_model}. Details: {last_error}"
            )

        vector = data.get("embedding")
        if vector is None and isinstance(data.get("embeddings"), list):
            embeddings = data.get("embeddings")
            if embeddings:
                vector = embeddings[0]

        if not isinstance(vector, list) or not vector:
            raise RuntimeError(
                "Ollama response did not include a valid 'embedding' array. "
                f"Response keys: {list(data.keys())}"
            )

        try:
            return [float(value) for value in vector]
        except Exception as exc:
            raise RuntimeError(f"Embedding vector contains non-numeric values. Details: {exc}") from exc

    @staticmethod
    def _safe_entity_get(entity: Any, key: str) -> Any:
        try:
            return entity.get(key)
        except Exception:
            return None

    @staticmethod
    def _citation_from_row(row: dict[str, Any]) -> str:
        doc_source = row.get("doc_source") or ""
        page_number = row.get("page_number")
        chunk_index = row.get("chunk_index")
        return f"{doc_source}#p{page_number}-c{chunk_index}"

    @staticmethod
    def _format_hit(row: dict[str, Any], score: float | None) -> dict[str, Any]:
        return {
            "id": row.get("id"),
            "score": score,
            "text_chunk": row.get("text_chunk"),
            "doc_source": row.get("doc_source"),
            "doc_type": row.get("doc_type"),
            "service_type": row.get("service_type"),
            "region": row.get("region"),
            "page_number": row.get("page_number"),
            "chunk_index": row.get("chunk_index"),
            "citation": MilvusMCPService._citation_from_row(row),
        }

    @staticmethod
    def _combine_expr_parts(parts: list[str]) -> str | None:
        cleaned = [part for part in parts if part]
        if not cleaned:
            return None
        if len(cleaned) == 1:
            return cleaned[0]
        return " and ".join(f"({part})" for part in cleaned)

    def _build_structured_expr(
        self,
        doc_type: str | None = None,
        service_type: str | None = None,
        region: str | None = None,
    ) -> str | None:
        clauses: list[str] = []
        if doc_type:
            clauses.append(f'doc_type == "{_escape_expr_value(doc_type.strip())}"')
        if service_type:
            clauses.append(f'service_type == "{_escape_expr_value(service_type.strip())}"')
        if region:
            clauses.append(f'region == "{_escape_expr_value(region.strip())}"')
        return self._combine_expr_parts(clauses)

    def _merge_exprs(self, structured_expr: str | None, legacy_filters: str | None) -> str | None:
        parts = [structured_expr]
        if legacy_filters and legacy_filters.strip():
            parts.append(legacy_filters.strip())
        return self._combine_expr_parts(parts)

    def describe_collection(self, collection_name: str | None = None) -> dict[str, Any]:
        name = self._collection_name(collection_name)
        try:
            collection = self._get_collection(name)
            vector_field, dim = self._detect_vector_field(collection)
            metric_type = self._extract_metric_type(collection, default="COSINE")
            row_count = int(collection.num_entities)

            return {
                "ok": True,
                "collection": name,
                "vector_field": vector_field,
                "dim": dim,
                "metric_type": metric_type,
                "row_count": row_count,
            }
        except FileNotFoundError as exc:
            return {"ok": False, "collection": name, "error": str(exc)}
        except Exception as exc:  # pragma: no cover - runtime env specific
            return {
                "ok": False,
                "collection": name,
                "error": f"Failed to describe collection: {exc}",
            }

    def fetch_by_ids(self, ids: list[int], collection_name: str | None = None) -> list[dict[str, Any]]:
        name = self._collection_name(collection_name)
        if not ids:
            return [{"ok": False, "collection": name, "error": "ids cannot be empty."}]

        try:
            collection = self._get_collection(name)
            pk_field, pk_dtype = self._detect_pk_field(collection)
            if not pk_field:
                return [{"ok": False, "collection": name, "error": "Could not detect primary key field."}]

            output_fields = [
                "text_chunk",
                "doc_source",
                "doc_type",
                "service_type",
                "region",
                "page_number",
                "chunk_index",
            ]

            try:
                from pymilvus import DataType
                is_varchar = pk_dtype == DataType.VARCHAR
            except Exception:
                is_varchar = pk_dtype == 21

            if is_varchar:
                normalized_ids = [str(raw_id) for raw_id in ids]
                escaped_ids = [f'"{_escape_expr_value(vid)}"' for vid in normalized_ids]
                expr = f"{pk_field} in [{','.join(escaped_ids)}]"
                rows_keyed_by = {str(row.get(pk_field)): row for row in collection.query(expr=expr, output_fields=[pk_field, *output_fields])}
                result_ids = normalized_ids
            else:
                normalized_ids = []
                for raw_id in ids:
                    try:
                        normalized_ids.append(int(raw_id))
                    except Exception:
                        continue

                if not normalized_ids:
                    return [{"ok": False, "collection": name, "error": "Could not normalize any ids to integers."}]

                expr = f"{pk_field} in [{','.join(str(value) for value in normalized_ids)}]"
                rows_keyed_by = {int(row.get(pk_field)): row for row in collection.query(expr=expr, output_fields=[pk_field, *output_fields])}
                result_ids = normalized_ids

            packed = [
                self._format_hit(rows_keyed_by[row_id], score=None)
                for row_id in result_ids
                if row_id in rows_keyed_by
            ]

            return packed
        except FileNotFoundError as exc:
            return [{"ok": False, "collection": name, "error": str(exc)}]
        except Exception as exc:  # pragma: no cover - runtime env specific
            return [{"ok": False, "collection": name, "error": f"Failed to fetch ids from Milvus: {exc}"}]

    def search(
        self,
        query_text: str,
        top_k: int = 10,
        filters: str | None = None,
        doc_type: str | None = None,
        service_type: str | None = None,
        region: str | None = None,
        collection_name: str | None = None,
    ) -> list[dict[str, Any]]:
        name = self._collection_name(collection_name)
        normalized_query = (query_text or "").strip()
        if not normalized_query:
            return {"ok": False, "collection": name, "error": "query_text cannot be empty."}

        top_k = max(1, min(int(top_k), 100))

        try:
            vector = self._embed_text(normalized_query)
            collection = self._get_collection(name)
            vector_field, dim = self._detect_vector_field(collection)
            if not vector_field:
                return {
                    "ok": False,
                    "collection": name,
                    "error": "No vector field found in collection schema.",
                }

            if dim is not None and len(vector) != dim:
                return {
                    "ok": False,
                    "collection": name,
                    "error": (
                        f"Embedding dimension mismatch: query dim={len(vector)} but "
                        f"collection field '{vector_field}' expects dim={dim}."
                    ),
                }

            metric_type = self._extract_metric_type(collection, default="COSINE")
            if metric_type not in ALLOWED_METRICS:
                metric_type = "COSINE"

            output_fields = [
                "text_chunk",
                "doc_source",
                "doc_type",
                "service_type",
                "region",
                "page_number",
                "chunk_index",
            ]

            structured_expr = self._build_structured_expr(
                doc_type=doc_type,
                service_type=service_type,
                region=region,
            )
            expr = self._merge_exprs(structured_expr, filters)

            results = collection.search(
                data=[vector],
                anns_field=vector_field,
                param={"metric_type": metric_type, "params": {"ef": 64}},
                limit=top_k,
                expr=expr,
                output_fields=output_fields,
            )

            packed: list[dict[str, Any]] = []
            for hit in results[0] if results else []:
                entity = hit.entity
                row = {
                    "id": getattr(hit, "id", None),
                    "text_chunk": self._safe_entity_get(entity, "text_chunk"),
                    "doc_source": self._safe_entity_get(entity, "doc_source"),
                    "doc_type": self._safe_entity_get(entity, "doc_type"),
                    "service_type": self._safe_entity_get(entity, "service_type"),
                    "region": self._safe_entity_get(entity, "region"),
                    "page_number": self._safe_entity_get(entity, "page_number"),
                    "chunk_index": self._safe_entity_get(entity, "chunk_index"),
                }
                packed.append(self._format_hit(row=row, score=float(getattr(hit, "score", 0.0))))

            return packed
        except FileNotFoundError as exc:
            return [{"ok": False, "collection": name, "error": str(exc)}]
        except RuntimeError as exc:
            return [{"ok": False, "collection": name, "error": str(exc)}]
        except Exception as exc:  # pragma: no cover - runtime env specific
            return [{"ok": False, "collection": name, "error": f"Milvus search failed: {exc}"}]

    def health(self) -> dict[str, Any]:
        collection_name = self.config.default_collection
        info: dict[str, Any] = {
            "milvus_uri": self.config.milvus_uri,
            "milvus_db": self.config.milvus_db,
            "collection": collection_name,
            "milvus_reachable": False,
            "collection_exists": False,
            "row_count": None,
            "error": None,
        }

        try:
            self._connect()
            utility.list_collections(using=self.alias)
            info["milvus_reachable"] = True
        except Exception as exc:  # pragma: no cover - runtime env specific
            info["error"] = str(exc)
            return info

        try:
            exists = self._has_collection(collection_name)
            info["collection_exists"] = exists
            if exists:
                collection = Collection(name=collection_name, using=self.alias)
                collection.load()
                info["row_count"] = int(collection.num_entities)
        except Exception as exc:  # pragma: no cover - runtime env specific
            info["error"] = str(exc)

        return info


def build_mcp_server(service: MilvusMCPService):
    try:
        from mcp.server.fastmcp import FastMCP
    except Exception as exc:  # pragma: no cover - dependency/runtime specific
        raise RuntimeError(
            "The 'mcp' package is required to run this server. "
            "Install with: pip install -r tools/mcp/requirements.txt"
        ) from exc

    mcp = FastMCP("milvus-mcp")

    @mcp.tool(name="milvus.health")
    def milvus_health() -> dict[str, Any]:
        """Return connectivity and collection status for Milvus."""
        return service.health()

    @mcp.tool(name="milvus.describe_collection")
    def milvus_describe_collection(collection_name: str | None = None) -> dict[str, Any]:
        """Describe collection schema and stats (vector field, dim, metric, row count)."""
        return service.describe_collection(collection_name=collection_name)

    @mcp.tool(name="milvus.search")
    def milvus_search(
        query_text: str,
        top_k: int = 10,
        filters: str | None = None,
        doc_type: str | None = None,
        service_type: str | None = None,
        region: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search Milvus by embedding a query with Ollama and running vector similarity search."""
        return service.search(
            query_text=query_text,
            top_k=top_k,
            filters=filters,
            doc_type=doc_type,
            service_type=service_type,
            region=region,
        )

    @mcp.tool(name="milvus.fetch_by_ids")
    def milvus_fetch_by_ids(ids: list[int]) -> list[dict[str, Any]]:
        """Fetch Milvus records by primary key and return the same LLM-ready fields as search."""
        return service.fetch_by_ids(ids=ids)

    return mcp


def run_self_test(service: MilvusMCPService) -> int:
    print("[self-test] milvus-mcp configuration")
    print(
        json.dumps(
            {
                "milvus_uri": service.config.milvus_uri,
                "milvus_db": service.config.milvus_db,
                "collection": service.config.default_collection,
                "ollama_host": service.config.ollama_host,
                "ollama_embed_model": service.config.ollama_embed_model,
            },
            indent=2,
        )
    )

    health = service.health()
    print("[self-test] health")
    print(json.dumps(health, indent=2))

    print("[self-test] ollama embedding probe")
    try:
        vector = service._embed_text("mcp self test")
        print(json.dumps({"ok": True, "embedding_dim": len(vector)}, indent=2))
        ollama_ok = True
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        ollama_ok = False

    print("[self-test] describe_collection")
    description = service.describe_collection()
    print(json.dumps(description, indent=2))

    print("[self-test] search probe")
    search_results = service.search(query_text="sales forecast", top_k=3)
    if search_results and isinstance(search_results, list) and search_results[0].get("ok") is False:
        print(json.dumps(search_results[0], indent=2))
        search_ok = False
    else:
        first_hit = search_results[0] if search_results else None
        print(json.dumps(first_hit, indent=2))
        search_ok = bool(first_hit)

    print("[self-test] fetch_by_ids probe")
    fetch_ok = False
    if search_ok and search_results:
        first_hit = search_results[0]
        hit_id = first_hit.get("id")
        if hit_id is not None:
            fetch_results = service.fetch_by_ids([hit_id])
            if (
                isinstance(fetch_results, list)
                and len(fetch_results) == 1
                and fetch_results[0].get("id") == hit_id
                and fetch_results[0].get("citation") == first_hit.get("citation")
            ):
                print(json.dumps(fetch_results[0], indent=2))
                fetch_ok = True
            else:
                print(json.dumps({"ok": False, "error": f"fetch_by_ids returned unexpected data: {fetch_results}"}, indent=2))
        else:
            print(json.dumps({"ok": False, "error": "search hit missing id"}, indent=2))
    else:
        print(json.dumps({"ok": False, "error": "skipped (search probe failed)"}, indent=2))

    milvus_ok = bool(health.get("milvus_reachable"))
    return 0 if milvus_ok and ollama_ok and search_ok and fetch_ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Milvus MCP stdio server")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run local connectivity checks instead of starting the MCP stdio server.",
    )
    args = parser.parse_args()

    config = MilvusMCPConfig.from_env()
    service = MilvusMCPService(config)

    if args.self_test:
        return run_self_test(service)

    try:
        server = build_mcp_server(service)
        server.run()
        return 0
    except Exception as exc:
        print(f"Failed to start milvus-mcp server: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
