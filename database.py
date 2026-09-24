import os
import logging

from sqlalchemy import create_engine, inspect, text
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


logger = logging.getLogger(__name__)

# Colunas acrescentadas em tabelas que JÁ existiam. O create_all só cria
# tabelas novas -- não adiciona coluna em tabela existente --, então
# esta lista é aplicada na inicialização. Regras: só ADICIONAR (nunca
# apagar/renomear), sempre aceitando nulo, e seguro rodar várias vezes.
_COLUNAS_ADICIONAIS = {
    "perguntas": {
        "skus_anuncio": "VARCHAR",
        "titulo_anuncio": "VARCHAR",
    },
    "solicitacoes_cancelamento": {
        "origem": "VARCHAR",
        "solicitado_por": "VARCHAR",
        "galpao": "INTEGER",
        "conta_chave": "VARCHAR",
        "resultado_impacto": "VARCHAR",
        "protocolo": "VARCHAR",
        "em_atendimento_por": "VARCHAR",
        "em_atendimento_por_id": "INTEGER",
        "em_atendimento_desde": "TIMESTAMP",
        "assumido_primeiro_por": "VARCHAR",
        "assumido_primeiro_em": "TIMESTAMP",
        "sku": "VARCHAR",
        "produto_titulo": "VARCHAR",
    },
}

# Índices pra filtros/busca continuarem rápidos com o volume crescendo.
_INDICES_ADICIONAIS = [
    ("ix_solic_canc_criado_em", "solicitacoes_cancelamento", "criado_em"),
    ("ix_solic_canc_numero_venda", "solicitacoes_cancelamento", "numero_venda"),
    # mesmo nome que o SQLAlchemy dá ao index=True do modelo: em banco novo já
    # existe (IF NOT EXISTS ignora); em banco antigo é criado aqui.
    ("ix_solicitacoes_cancelamento_conta_chave", "solicitacoes_cancelamento", "conta_chave"),
    ("ix_solicitacoes_cancelamento_sku", "solicitacoes_cancelamento", "sku"),
]


def garantir_estrutura_atualizada() -> None:
    """
    Adiciona as colunas/índices novos se ainda não existirem e preenche
    a conta_chave dos registros antigos. Chamada uma vez na subida do
    sistema, logo depois do create_all. Se falhar, registra no log e
    interrompe a subida -- melhor o deploy falhar (e o Railway manter a
    versão anterior no ar) do que rodar com o banco pela metade.
    """
    try:
        inspetor = inspect(engine)
        with engine.begin() as conexao:
            for tabela, colunas in _COLUNAS_ADICIONAIS.items():
                if not inspetor.has_table(tabela):
                    continue  # tabela nova: o create_all já criou completa
                existentes = {c["name"] for c in inspetor.get_columns(tabela)}
                for nome, tipo in colunas.items():
                    if nome not in existentes:
                        conexao.execute(text(f"ALTER TABLE {tabela} ADD COLUMN {nome} {tipo}"))
                        logger.info("Coluna adicionada: %s.%s", tabela, nome)
            for nome_indice, tabela, coluna in _INDICES_ADICIONAIS:
                conexao.execute(text(f"CREATE INDEX IF NOT EXISTS {nome_indice} ON {tabela} ({coluna})"))

        # Preenche a chave dos registros antigos (só os que ainda não têm).
        from app.contas_util import chave_conta  # import local: evita ciclo na carga
        with engine.begin() as conexao:
            pendentes = conexao.execute(
                text("SELECT id, conta FROM solicitacoes_cancelamento WHERE conta_chave IS NULL")
            ).fetchall()
            for linha in pendentes:
                conexao.execute(
                    text("UPDATE solicitacoes_cancelamento SET conta_chave = :c WHERE id = :i"),
                    {"c": chave_conta(linha.conta), "i": linha.id},
                )
            if pendentes:
                logger.info("conta_chave preenchida em %d solicitações antigas", len(pendentes))
    except Exception:
        logger.exception("Falha ao atualizar a estrutura do banco")
        raise
