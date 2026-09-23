from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os
from pathlib import Path

SQLALCHEMY_DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///./aquaculture.db"
)

if SQLALCHEMY_DATABASE_URL.startswith("sqlite:///"):
    db_path = SQLALCHEMY_DATABASE_URL.replace("sqlite:///", "")
    db_dir = Path(db_path).parent
    db_dir.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

# 既有 SQLite 库的增量列：(表, 列, 列定义)
_SQLITE_COLUMN_UPGRADES = [
    ("ponds", "capacity", "INTEGER DEFAULT 1"),
    ("ponds", "active_from", "DATE"),
    ("ponds", "active_until", "DATE"),
    ("batches", "version", "INTEGER DEFAULT 1"),
]

def run_sqlite_migrations():
    """为已存在的 SQLite 数据库补充新列（create_all 不会修改已有表）。"""
    if not SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
        return
    with engine.begin() as conn:
        for table, column, definition in _SQLITE_COLUMN_UPGRADES:
            rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            if rows and column not in [r[1] for r in rows]:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        conn.exec_driver_sql("UPDATE batches SET version = 1 WHERE version IS NULL")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
