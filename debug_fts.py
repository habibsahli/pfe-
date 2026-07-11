#!/usr/bin/env python3
"""Debug FTS (Full Text Search) lexical search issue."""
import os
import sys
from sqlalchemy import create_engine, text
from urllib.parse import quote_plus

# Get database URL from environment
db_user = os.getenv("DB_USER", "admin")
db_pass = os.getenv("DB_PASSWORD", "SecurePassword123!")
db_host = os.getenv("DB_HOST", "localhost")
db_port = os.getenv("DB_PORT", "5432")
db_name = os.getenv("DB_NAME", "fibre_forecast_db")

# URL-encode password to handle special characters
db_pass_encoded = quote_plus(db_pass)
database_url = f"postgresql://{db_user}:{db_pass_encoded}@{db_host}:{db_port}/{db_name}"

print(f"Connecting to: postgresql://{db_user}:****@{db_host}:{db_port}/{db_name}")
print()

engine = create_engine(database_url, echo=False)

def test_fts():
    """Run comprehensive FTS diagnostics."""
    
    with engine.begin() as conn:
        # 1. Check if table exists
        print("=" * 60)
        print("1. CHECK TABLE EXISTENCE")
        print("=" * 60)
        result = conn.execute(text("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables 
                WHERE table_name = 'rag_chunks_lexical'
            )
        """)).scalar()
        print(f"Table rag_chunks_lexical exists: {result}")
        
        # 2. Count rows in table
        print("\n" + "=" * 60)
        print("2. CHECK DATA IN TABLE")
        print("=" * 60)
        count = conn.execute(text("SELECT COUNT(*) FROM rag_chunks_lexical")).scalar()
        print(f"Total rows in rag_chunks_lexical: {count}")
        
        if count == 0:
            print("WARNING: Table is empty! Need to populate with test data.")
            # Insert test data
            print("\nInserting test data...")
            test_data = [
                ("stock inventory level 2026", "test_doc_1", "CSV", "FORECAST", 0),
                ("fibre optical cable supply", "test_doc_2", "TXT", "FORECAST", 0),
                ("vente sales forecast monthly", "test_doc_3", "CSV", "FORECAST", 0),
            ]
            for text_chunk, source, doc_type, service_type, chunk_idx in test_data:
                conn.execute(text("""
                    INSERT INTO rag_chunks_lexical (text_chunk, doc_source, doc_type, service_type, chunk_index)
                    VALUES (:text_chunk, :doc_source, :doc_type, :service_type, :chunk_index)
                    ON CONFLICT (doc_source, chunk_index, service_type) DO UPDATE
                    SET text_chunk = EXCLUDED.text_chunk
                """), {
                    "text_chunk": text_chunk,
                    "doc_source": source,
                    "doc_type": doc_type,
                    "service_type": service_type,
                    "chunk_index": chunk_idx,
                })
            print("Test data inserted successfully")
        else:
            print(f"Sample data (first 3 rows):")
            rows = conn.execute(text("SELECT text_chunk, doc_source FROM rag_chunks_lexical LIMIT 3")).fetchall()
            for row in rows:
                print(f"  - {row[0][:60]}... ({row[1]})")
        
        # 3. Check GIN index
        print("\n" + "=" * 60)
        print("3. CHECK GIN INDEX")
        print("=" * 60)
        indexes = conn.execute(text("""
            SELECT indexname, indexdef FROM pg_indexes 
            WHERE tablename = 'rag_chunks_lexical' AND indexname LIKE '%fts%'
        """)).fetchall()
        if indexes:
            for idx_name, idx_def in indexes:
                print(f"Index: {idx_name}")
                print(f"Definition: {idx_def}")
        else:
            print("WARNING: No FTS index found!")
        
        # 4. Test different query modes
        print("\n" + "=" * 60)
        print("4. TEST DIFFERENT QUERY MODES")
        print("=" * 60)
        
        test_keywords = ["stock", "fibre", "vente", "inventory", "forecast"]
        
        for keyword in test_keywords:
            print(f"\nTesting keyword: '{keyword}'")
            print("-" * 40)
            
            # Test 1: websearch_to_tsquery
            try:
                result = conn.execute(text("""
                    SELECT COUNT(*) as cnt
                    FROM rag_chunks_lexical
                    WHERE to_tsvector('simple', text_chunk) @@ websearch_to_tsquery('simple', :query)
                """), {"query": keyword}).scalar()
                print(f"  websearch_to_tsquery: {result} hits")
            except Exception as e:
                print(f"  websearch_to_tsquery: ERROR - {e}")
            
            # Test 2: plainto_tsquery
            try:
                result = conn.execute(text("""
                    SELECT COUNT(*) as cnt
                    FROM rag_chunks_lexical
                    WHERE to_tsvector('simple', text_chunk) @@ plainto_tsquery('simple', :query)
                """), {"query": keyword}).scalar()
                print(f"  plainto_tsquery: {result} hits")
            except Exception as e:
                print(f"  plainto_tsquery: ERROR - {e}")
            
            # Test 3: to_tsquery (requires & | ! operators)
            try:
                result = conn.execute(text("""
                    SELECT COUNT(*) as cnt
                    FROM rag_chunks_lexical
                    WHERE to_tsvector('simple', text_chunk) @@ to_tsquery('simple', :query)
                """), {"query": keyword}).scalar()
                print(f"  to_tsquery: {result} hits")
            except Exception as e:
                print(f"  to_tsquery: ERROR - {e}")
            
            # Test 4: ILIKE pattern matching (should always work)
            try:
                result = conn.execute(text("""
                    SELECT COUNT(*) as cnt
                    FROM rag_chunks_lexical
                    WHERE text_chunk ILIKE :query
                """), {"query": f"%{keyword}%"}).scalar()
                print(f"  ILIKE pattern: {result} hits")
            except Exception as e:
                print(f"  ILIKE pattern: ERROR - {e}")
        
        # 5. Test tsvector generation
        print("\n" + "=" * 60)
        print("5. TEST TSVECTOR GENERATION")
        print("=" * 60)
        
        rows = conn.execute(text("""
            SELECT text_chunk, 
                   to_tsvector('simple', text_chunk) as tsv
            FROM rag_chunks_lexical 
            LIMIT 3
        """)).fetchall()
        
        for text_chunk, tsv in rows:
            print(f"\nText: {text_chunk[:60]}...")
            print(f"TSVector: {tsv}")
        
        # 6. Detailed search test with ranking
        print("\n" + "=" * 60)
        print("6. DETAILED SEARCH TEST WITH RANKING")
        print("=" * 60)
        
        test_query = "stock"
        print(f"Testing query: '{test_query}'")
        
        sql = """
            SELECT
                text_chunk,
                doc_source,
                chunk_index,
                ts_rank_cd(to_tsvector('simple', text_chunk), websearch_to_tsquery('simple', :query)) AS score
            FROM rag_chunks_lexical
            WHERE to_tsvector('simple', text_chunk) @@ websearch_to_tsquery('simple', :query)
            ORDER BY score DESC
            LIMIT 10
        """
        
        rows = conn.execute(text(sql), {"query": test_query}).mappings().fetchall()
        print(f"Results: {len(rows)} rows")
        for i, row in enumerate(rows, 1):
            print(f"\n  {i}. Score: {row['score']}")
            print(f"     Text: {row['text_chunk'][:60]}...")
            print(f"     Source: {row['doc_source']}")

if __name__ == "__main__":
    try:
        test_fts()
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
