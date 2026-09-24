from sqlalchemy import create_engine, inspect, text
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

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# 已有 SQLite 库的列级补丁:create_all 不会为旧表补列,这里显式 ALTER。
_SCHEMA_PATCHES = {
    "ponds": {
        "active_from": "DATE",
        "active_to": "DATE",
        "capacity": "INTEGER DEFAULT 1",
    },
    "batches": {
        "version": "INTEGER NOT NULL DEFAULT 1",
        "data_quality": "VARCHAR(20) NOT NULL DEFAULT 'ok'",
    },
}

def ensure_schema():
    """创建新表并为旧库补齐状态机/版本控制所需的列,不删除任何既有数据。"""
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in _SCHEMA_PATCHES.items():
            if table not in existing_tables:
                continue
            present = {col["name"] for col in inspector.get_columns(table)}
            for name, ddl in columns.items():
                if name not in present:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
        # 回填旧行可能缺失的时间戳与版本号,避免读取视图序列化失败
        if "batches" in existing_tables:
            conn.execute(text(
                "UPDATE batches SET version = 1 WHERE version IS NULL"))
        for table in ("ponds", "batches"):
            if table in existing_tables:
                cols = {c["name"] for c in inspector.get_columns(table)}
                if "created_at" in cols:
                    conn.execute(text(
                        f"UPDATE {table} SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"))
                if "updated_at" in cols:
                    conn.execute(text(
                        f"UPDATE {table} SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL"))
