#!/usr/bin/env python3
"""Fix FTS duplicate data and optimize the schema."""
import os
from sqlalchemy import create_engine, text
from urllib.parse import quote_plus

engine = create_engine(
    f"postgresql://admin:{quote_plus('SecurePassword123!')}@localhost:5433/fibre_forecast_db",
    echo=False
)

def fix_duplicates():
    """Remove duplicate chunks from lexical search table."""
    with engine.begin() as conn:
        print("=" * 60)
        print("FIX: Removing duplicate chunks")
        print("=" * 60)
        
        # Count duplicates before
        before = conn.execute(text("SELECT COUNT(*) FROM rag_chunks_lexical")).scalar()
        print(f"Total rows before: {before}")
        
        # Find and display duplicates
        dup_query = """
            SELECT text_chunk, COUNT(*) as cnt, 
                   array_agg(id ORDER BY created_at) as ids
            FROM rag_chunks_lexical
            GROUP BY text_chunk
            HAVING COUNT(*) > 1
            ORDER BY cnt DESC
        """
        dups = conn.execute(text(dup_query)).fetchall()
        print(f"Duplicate chunks found: {len(dups)}")
        print(f"Total duplicate rows: {sum(cnt - 1 for _, cnt, _ in dups)}")
        
        # Show top duplicates
        for i, (chunk, cnt, ids) in enumerate(dups[:3]):
            print(f"\n  {i+1}. {cnt} copies of: {chunk[:50]}...")
            print(f"     IDs to keep: {ids[0]}, delete: {ids[1:]}")
        
        # Delete duplicates - keep only the first occurrence of each chunk
        delete_sql = """
            DELETE FROM rag_chunks_lexical
            WHERE id NOT IN (
                SELECT MIN(id)
                FROM rag_chunks_lexical
                GROUP BY text_chunk
            )
        """
        result = conn.execute(text(delete_sql))
        deleted = result.rowcount
        
        # Count after
        after = conn.execute(text("SELECT COUNT(*) FROM rag_chunks_lexical")).scalar()
        print(f"\nDeleted {deleted} duplicate rows")
        print(f"Total rows after: {after}")
        
        # Verify no more duplicates
        dup_count = conn.execute(text("""
            SELECT COUNT(*) FROM (
                SELECT text_chunk, COUNT(*) as cnt
                FROM rag_chunks_lexical
                GROUP BY text_chunk
                HAVING COUNT(*) > 1
            ) t
        """)).scalar()
        print(f"Remaining duplicates: {dup_count}")
        
        if dup_count == 0:
            print("✅ All duplicates removed!")
        else:
            print("⚠️  WARNING: Duplicates still exist!")
        
        return deleted, after

if __name__ == "__main__":
    try:
        fix_duplicates()
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
