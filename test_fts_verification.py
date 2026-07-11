#!/usr/bin/env python3
"""FTS Verification Test - Verify lexical search is working correctly."""
import os
from sqlalchemy import create_engine, text
from urllib.parse import quote_plus

engine = create_engine(
    f"postgresql://admin:{quote_plus('SecurePassword123!')}@localhost:5433/fibre_forecast_db",
    echo=False
)

def run_tests():
    """Run FTS verification tests."""
    print("=" * 70)
    print("FTS LEXICAL SEARCH VERIFICATION TEST")
    print("=" * 70)
    
    with engine.begin() as conn:
        # Test 1: Verify no duplicates
        print("\n✓ TEST 1: Verify no duplicates in table")
        print("-" * 70)
        dup_count = conn.execute(text("""
            SELECT COUNT(*) FROM (
                SELECT 1 FROM rag_chunks_lexical
                GROUP BY text_chunk
                HAVING COUNT(*) > 1
            ) t
        """)).scalar()
        row_count = conn.execute(text("SELECT COUNT(*) FROM rag_chunks_lexical")).scalar()
        print(f"  Total rows: {row_count}")
        print(f"  Duplicate chunks: {dup_count}")
        if dup_count == 0:
            print("  ✅ PASS: No duplicates found")
        else:
            print(f"  ❌ FAIL: Found {dup_count} duplicate chunks")
        
        # Test 2: Test search queries
        print("\n✓ TEST 2: Test FTS search queries")
        print("-" * 70)
        
        test_cases = [
            ("stock", "Should find documents about stock management"),
            ("fibre", "Should find documents about fibre/fiber products"),
            ("vente", "Should find documents about sales"),
            ("offre", "Should find documents about offers"),
            ("bridge", "Should find documents about Bridge product"),
        ]
        
        all_passed = True
        for keyword, description in test_cases:
            sql = """
                SELECT COUNT(*) as cnt
                FROM rag_chunks_lexical
                WHERE to_tsvector('simple', text_chunk) @@ websearch_to_tsquery('simple', :query)
            """
            result = conn.execute(text(sql), {"query": keyword}).scalar()
            status = "✅" if result > 0 else "❌"
            print(f"  {status} '{keyword}': {result} hits - {description}")
            if result == 0:
                all_passed = False
        
        # Test 3: Verify DISTINCT works
        print("\n✓ TEST 3: Verify DISTINCT prevents duplicates in results")
        print("-" * 70)
        
        # Query with DISTINCT
        sql_distinct = """
            SELECT COUNT(DISTINCT text_chunk) as cnt
            FROM rag_chunks_lexical
            WHERE to_tsvector('simple', text_chunk) @@ websearch_to_tsquery('simple', :query)
        """
        
        # Query without DISTINCT (for comparison)
        sql_all = """
            SELECT COUNT(*) as cnt
            FROM rag_chunks_lexical
            WHERE to_tsvector('simple', text_chunk) @@ websearch_to_tsquery('simple', :query)
        """
        
        distinct_count = conn.execute(text(sql_distinct), {"query": "stock"}).scalar()
        all_count = conn.execute(text(sql_all), {"query": "stock"}).scalar()
        
        print(f"  DISTINCT results: {distinct_count}")
        print(f"  All results: {all_count}")
        
        if distinct_count == all_count:
            print("  ✅ PASS: No duplicate results")
        else:
            print(f"  ⚠️  WARNING: Duplicates exist ({all_count - distinct_count} extra rows)")
        
        # Test 4: Test ranking/scoring
        print("\n✓ TEST 4: Verify ranking scores are calculated")
        print("-" * 70)
        
        sql = """
            SELECT 
                text_chunk,
                ts_rank_cd(to_tsvector('simple', text_chunk), websearch_to_tsquery('simple', :query)) AS score
            FROM rag_chunks_lexical
            WHERE to_tsvector('simple', text_chunk) @@ websearch_to_tsquery('simple', :query)
            ORDER BY score DESC
            LIMIT 5
        """
        
        results = conn.execute(text(sql), {"query": "stock"}).fetchall()
        print(f"  Top 5 results for 'stock':")
        for i, (chunk, score) in enumerate(results, 1):
            print(f"    {i}. Score {score:.2f}: {chunk[:50]}...")
        
        # Test 5: Verify GIN index exists
        print("\n✓ TEST 5: Verify GIN index for FTS")
        print("-" * 70)
        
        indexes = conn.execute(text("""
            SELECT indexname, indexdef 
            FROM pg_indexes 
            WHERE tablename = 'rag_chunks_lexical'
            ORDER BY indexname
        """)).fetchall()
        
        gin_found = False
        for idx_name, idx_def in indexes:
            if 'GIN' in idx_def and 'tsvector' in idx_def:
                print(f"  ✅ Found GIN FTS index: {idx_name}")
                gin_found = True
            else:
                print(f"  ℹ️  Index: {idx_name}")
        
        if not gin_found:
            print("  ❌ FAIL: No GIN FTS index found")
            all_passed = False
        
        # Summary
        print("\n" + "=" * 70)
        print("SUMMARY")
        print("=" * 70)
        if all_passed and dup_count == 0:
            print("✅ ALL TESTS PASSED - FTS IS WORKING CORRECTLY")
        else:
            print("⚠️  Some tests need attention. See details above.")
        print("=" * 70)

if __name__ == "__main__":
    try:
        run_tests()
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
