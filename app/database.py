import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# Em produção, defina DATABASE_URL apontando pro Postgres (o Railway
# preenche isso sozinho quando você adiciona o addon de Postgres ao
# projeto). Se não definido, cai num arquivo SQLite local — só pra
# rodar e testar essa base sem precisar instalar um servidor Postgres.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./club_dashboard_local.db")

# Alguns provedores (Railway incluso) às vezes fornecem a URL como
# "postgres://" -- versões mais novas do SQLAlchemy exigem
# "postgresql://". Corrige isso automaticamente, sem precisar mexer
# na variável de ambiente.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
