"""SQLAlchemy session and engine; external PostgreSQL in production."""
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from app.config import get_settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str):
    options = {'pool_pre_ping': True, 'pool_recycle': 1800}
    if url.startswith('sqlite'):
        options['connect_args'] = {'check_same_thread': False, 'timeout': 30}
    else:
        options.update(pool_size=10, max_overflow=20, pool_timeout=20)
    return create_engine(url, **options)


engine = make_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    with SessionLocal() as db:
        yield db
