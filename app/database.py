import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./smartmeter.db")

if DATABASE_URL == "sqlite:///:memory:":
    engine = create_engine(
        DATABASE_URL, connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
elif DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# 既有 alerts 表上新增的可空列（仅新增列/表，不改动旧数据，保证兼容现有库）
_ALERT_ADDED_COLUMNS = {
    "strategy_version_id": "INTEGER",
    "strategy_snapshot_json": "TEXT",
    "strategy_scope": "VARCHAR(16)",
}


def ensure_schema_compatible() -> None:
    """建表 + 为历史数据库补齐新列（幂等，不删除/修改既有列与数据）。"""
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)
    if "alerts" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("alerts")}
    with engine.begin() as conn:
        for name, col_type in _ALERT_ADDED_COLUMNS.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE alerts ADD COLUMN {name} {col_type}"))
