# Hybrid Retrieval Strategy (Dense + Lexical + Fusion)

## Objective

Improve RAG recall and answer grounding by combining:

- Dense semantic retrieval (Milvus vector search)
- Lexical retrieval (PostgreSQL Full-Text Search)
- Rank fusion (Weighted Reciprocal Rank Fusion)
- Lightweight reranking before context packing

## Why Hybrid

Dense retrieval is strong for semantic similarity, but can miss:

- Exact keywords and domain acronyms
- Rare identifiers, product codes, and short terms
- Queries with strict lexical intent

Lexical retrieval is strong for exact term matching, but can miss:

- Paraphrases and semantic equivalents
- Cross-lingual and contextual variation

Hybrid retrieval combines both signals and is more robust in production QA settings.

## Implemented Components

### 1. Dense Retrieval

- Backend: Milvus HNSW index with cosine metric
- Query: embedding from `bge-m3` via Ollama
- Output: top-K chunk candidates with metadata and score

Fallback behavior:

- If Milvus is unavailable, the service uses the in-memory vector store.

### 2. Lexical Retrieval

- Backend: PostgreSQL table `rag_chunks_lexical`
- Text index: `GIN(to_tsvector('simple', text_chunk))`
- Query function: `websearch_to_tsquery('simple', :query)`
- Ranking: `ts_rank_cd(...)`

Ingestion behavior:

- Every chunk is upserted in lexical storage during document ingestion.
- Uniqueness key: `(doc_source, chunk_index, service_type)`.

### 3. Fusion: Weighted RRF

RRF score is computed per candidate as:

$$
\mathrm{RRF}(d) = \frac{w_{dense}}{k + r_{dense}(d)} + \frac{w_{lex}}{k + r_{lex}(d)}
$$

Where:

- $r_{dense}(d)$ is rank in dense results
- $r_{lex}(d)$ is rank in lexical results
- $w_{dense}$ and $w_{lex}$ are configurable weights
- $k$ is the smoothing constant (`RAG_RRF_K`)

This favors documents that rank well in either or both channels and avoids scale mismatch between raw score types.

### 4. Lightweight Reranking

After fusion, a small rerank bonus is applied based on query-token overlap with chunk text:

$$
\mathrm{score}_{final}(d) = \mathrm{score}_{rrf}(d) + \lambda \cdot \mathrm{overlap}(q, d)
$$

Where:

- overlap is token intersection ratio over query tokens
- $\lambda$ is `RAG_RERANK_OVERLAP_WEIGHT`

This step improves precision without introducing heavy external rerankers.

## Configuration Knobs

Added in settings:

- `RAG_DENSE_TOP_K`
- `RAG_LEXICAL_TOP_K`
- `RAG_FUSED_TOP_K`
- `RAG_RRF_K`
- `RAG_DENSE_WEIGHT`
- `RAG_LEXICAL_WEIGHT`
- `RAG_RERANK_OVERLAP_WEIGHT`

These allow controlled tuning per workload and latency budget.

## Expected Gains

- Better recall on domain-specific terms and acronyms
- More stable retrieval when semantic-only search misses exact wording
- Improved answer grounding due to more diverse yet relevant evidence
- Safer production behavior via backend fallbacks (Milvus down scenario)

## Operational Notes

- The lexical table is auto-created on service startup.
- Retrieval status now exposes lexical backend availability and entity count.
- Hybrid mode is reported as `hybrid_rrf` in status.

## Future Enhancements

- Swap lightweight rerank with cross-encoder reranker for higher precision
- Add query intent classification for dynamic dense/lexical weighting
- Add deduplication by semantic similarity before context packing
- Add offline retrieval evaluation (Recall@K, MRR, nDCG) on `eval/questions.jsonl`
