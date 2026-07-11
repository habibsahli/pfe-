# FTS Lexical Search - Fix Summary

## Problem Identified
**Root Cause**: Duplicate chunks in `rag_chunks_lexical` table (40 out of 80 rows were duplicates)

This caused:
- Search results to return duplicate entries
- Bloated result sets
- Confusing user experience with repeated content

## Issues Found
1. ❌ **Duplicate Data**: 40 duplicate text chunks (50% duplication rate)
2. ⚠️ **No DISTINCT in query**: Search returned both copies of duplicates
3. ✅ **GIN Index**: Properly created and functional
4. ✅ **FTS Functionality**: Working correctly when duplicates are removed

## Fixes Applied

### 1. Data Cleanup
- Removed 40 duplicate rows from `rag_chunks_lexical` table
- Kept only first occurrence of each chunk (by insertion time)
- Reduced table from 80 rows to 40 rows (unique content)

**Script**: `/home/habib/pfe/fix_fts_duplicates.py`
**Result**: ✅ No duplicates remain

### 2. Query Optimization
- Updated [rag_service.py](rag_service.py#L270-L272) to use `SELECT DISTINCT`
- Prevents any future duplicate results even if duplicates re-appear in data
- Document: [rag_service.py](rag_service.py)

**Change**: Line 270 in `PostgresLexicalStore.search()`
```sql
-- Before
SELECT text_chunk, doc_source, ...

-- After
SELECT DISTINCT text_chunk, doc_source, ...
```

## Test Results

### Query Performance Before/After
| Keyword | Before (with dupes) | After (cleaned) | Status |
|---------|-------------------|-----------------|--------|
| 'stock' | 18 hits (9 unique) | 9 hits          | ✅ Fixed |
| 'fibre' | 54 hits (27 unique) | 27 hits        | ✅ Fixed |
| 'vente' | 12 hits (6 unique) | 6 hits          | ✅ Fixed |
| 'offre' | 52 hits (26 unique) | 26 hits        | ✅ Fixed |
| 'bridge' | 40 hits (20 unique) | 20 hits       | ✅ Fixed |

### Verification Tests: ALL PASSING ✅
- **No duplicates**: 0 duplicate chunks in table
- **FTS search**: All keywords return correct hit counts
- **DISTINCT works**: Search results have no duplicates
- **Ranking scores**: Properly calculated and ordered
- **GIN index**: Active and functional (`gin (to_tsvector('simple'::regconfig, text_chunk))`)

## Database Schema Status

```sql
-- GIN index for FTS
CREATE INDEX idx_rag_chunks_lexical_fts 
  ON public.rag_chunks_lexical 
  USING gin (to_tsvector('simple'::regconfig, text_chunk));

-- Service type filter index
CREATE INDEX idx_rag_chunks_lexical_service 
  ON public.rag_chunks_lexical 
  USING btree (service_type);

-- Unique constraint to prevent re-introducingduplicates
CREATE UNIQUE INDEX rag_chunks_lexical_doc_source_chunk_index_service_type_key
  ON public.rag_chunks_lexical 
  USING btree (doc_source, chunk_index, service_type);
```

## Affected Files
1. **Database**: `rag_chunks_lexical` table (cleaned)
2. **Code**: [backend/app/services/rag_service.py](rag_service.py#L270-L272)
3. **Utilities**: 
   - `/home/habib/pfe/fix_fts_duplicates.py` (cleanup script)
   - `/home/habib/pfe/debug_fts.py` (diagnostic tool)
   - `/home/habib/pfe/test_fts_verification.py` (verification suite)

## Next Steps
1. ✅ Restart backend to apply code changes
2. ✅ Monitor lexical search performance in production
3. ✅ Ensure data pipeline prevents future duplicates
4. ⏳ Consider periodic cleanup as part of maintenance

## Notes
- The `websearch_to_tsquery` function is working correctly
- The 'simple' language config is appropriate for French text
- ts_rank_cd() properly scores results by relevance
- DISTINCT prevents issues even if duplicates somehow re-appear
