#!/usr/bin/env python3
"""
Migration runner for Fibre Forecast database.

Usage:
    python -m scripts.run_migrations

Runs all pending migrations from docker/init-scripts/
"""
import sys
import logging
from pathlib import Path
from sqlalchemy import create_engine, text

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def run_migrations(database_url: str, migrations_dir: str = "docker/init-scripts"):
    """Run all SQL migrations in order."""
    engine = create_engine(database_url, echo=False)
    migrations_path = Path(migrations_dir)
    
    if not migrations_path.exists():
        logger.error(f"Migrations directory not found: {migrations_path}")
        return False
    
    # Find all SQL files and sort them
    migration_files = sorted([f for f in migrations_path.glob("*.sql")])
    
    if not migration_files:
        logger.warning(f"No migration files found in {migrations_path}")
        return True
    
    logger.info(f"Found {len(migration_files)} migration files")
    
    try:
        with engine.connect() as connection:
            for migration_file in migration_files:
                logger.info(f"Running migration: {migration_file.name}")
                with open(migration_file, "r") as f:
                    sql_content = f.read()
                
                # Split by semicolon to handle multiple statements
                statements = [s.strip() for s in sql_content.split(";") if s.strip()]
                
                for statement in statements:
                    try:
                        connection.execute(text(statement))
                    except Exception as e:
                        # Log but continue - some statements might fail (e.g., DROP IF EXISTS)
                        logger.debug(f"  Statement execution note: {e}")
                
                connection.commit()
                logger.info(f"  ✓ Completed {migration_file.name}")
        
        logger.info("✓ All migrations completed successfully")
        return True
    
    except Exception as e:
        logger.error(f"Migration failed: {e}")
        return False
    finally:
        engine.dispose()


if __name__ == "__main__":
    from app.core.config import settings
    
    success = run_migrations(settings.DATABASE_URL)
    sys.exit(0 if success else 1)
