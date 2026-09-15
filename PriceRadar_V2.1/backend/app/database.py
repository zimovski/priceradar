import os
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


def _database_url() -> str:
    # DATABASE_URL remains the preferred setting. The aliases make the app more
    # tolerant of how a Render/Postgres connection may have been named manually.
    value = (
        os.getenv("DATABASE_URL")
        or os.getenv("POSTGRES_URL")
        or os.getenv("POSTGRESQL_URL")
        or os.getenv("DATABASE_INTERNAL_URL")
        or os.getenv("DB_URL")
    )
    if value:
        value = value.strip()
        # SQLAlchemy 2 expects the postgresql:// scheme.
        if value.startswith("postgres://"):
            value = "postgresql://" + value[len("postgres://"):]
        return value
    return "sqlite:///./precos.db"


DATABASE_URL = _database_url()
DATABASE_BACKEND = "postgresql" if DATABASE_URL.startswith("postgresql") else "sqlite"
DATABASE_PERSISTENT = DATABASE_BACKEND == "postgresql"

connect_args = {"check_same_thread": False} if DATABASE_BACKEND == "sqlite" else {}
engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    future=True,
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
