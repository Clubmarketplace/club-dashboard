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
    "empresas_planejadas": {
        "plataformas": "VARCHAR",
        "observacao": "VARCHAR",
        "criado_por": "VARCHAR",
        "removida_em": "TIMESTAMP",
    },
    "contas": {
        "inativa_em": "TIMESTAMP",
        "motivo_inativacao": "VARCHAR",
        "margem_minima": "FLOAT",
        "margem_maxima": "FLOAT",
        # 07/10: token do app "ClubMarketplaceX" (CMX_ML_CLIENT_ID), usado
        # pela extensão pra checar/aderir promoções -- separado do
        # access_token/refresh_token acima (esses são do app "mestre",
        # usado pelo pré/pós-venda). São apps DIFERENTES cadastrados no
        # Mercado Livre Devs; um token emitido por um não serve pro outro.
        # Ver app/cmx.py (rotas /ml/trocar-codigo e /ml/token).
        "cmx_access_token": "TEXT",
        "cmx_refresh_token": "TEXT",
        "cmx_token_expira_em": "TIMESTAMP",
        # 07/10: cada conta vendedora tem seu PRÓPRIO app cadastrado no
        # Mercado Livre Devs (não um app único compartilhado) -- guardado
        # aqui pra não depender de variável de ambiente global.
        "cmx_client_id": "VARCHAR",
        "cmx_client_secret": "TEXT",
    },
    "perguntas": {
        "skus_anuncio": "VARCHAR",
        "titulo_anuncio": "VARCHAR",
        "respondida_por": "VARCHAR",
        "revisao": "VARCHAR",
        "revisado_por": "VARCHAR",
        "revisado_em": "TIMESTAMP",
        "resposta_corrigida": "TEXT",
        "fonte_detalhe": "VARCHAR",
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
        # Tipo da solicitação ("cancelamento" | "reputacao"); nulo = cancelamento.
        "tipo": "VARCHAR",
        # 16/10: exclusão (admin) -- nulo = não excluída.
        "excluido_por": "VARCHAR",
        "excluido_em": "TIMESTAMP",
        "motivo_exclusao": "TEXT",
        # 16/10: evidência (fotos em base64, lista JSON, + link), opcional -- ver app/models.py.
        "fotos_base64": "TEXT",
        "link_evidencia": "VARCHAR",
        # 26/10: Reverso (tipo "reverso") -- ver app/models.py.
        "estado_chegada": "VARCHAR",
        "resultado_reverso": "VARCHAR",
        "liberado_por": "VARCHAR",
        "liberado_em": "TIMESTAMP",
        "checklist_json": "TEXT",
        "observacao_resultado": "TEXT",
    },
    "variaveis_conta": {
        # Sobre qual valor o percentual é aplicado: "venda_bruta" (default,
        # comportamento antigo) | "repasse" | "lucro". Backfill das linhas
        # antigas (NULL) pra "venda_bruta" é feito abaixo, depois do ALTER.
        "base_calculo": "VARCHAR",
    },
    "vendas": {
        # Situação do pagamento ("approved", "refunded"...). Linhas antigas
        # ficam NULL até serem reprocessadas pelo "Atualizar agora".
        "pagamento_status": "VARCHAR",
        # Carrinho (pack) do ML; NULL = pedido avulso ou linha antiga.
        "pack_id": "VARCHAR",
    },
    "produtos_cadastro": {
        # 14/10: origem, quem incluiu e a marcação por marketplace (ver
        # ProdutoCadastro em app/models.py). Linhas antigas ficam NULL --
        # tratado como "automatico" / "sem marcação por marketplace" no código.
        "origem": "VARCHAR",
        "criado_por": "VARCHAR",
        "ml_status": "VARCHAR",
        "ml_por": "VARCHAR",
        "ml_em": "TIMESTAMP",
        "tiktok_status": "VARCHAR",
        "tiktok_por": "VARCHAR",
        "tiktok_em": "TIMESTAMP",
        "shopee_status": "VARCHAR",
        "shopee_por": "VARCHAR",
        "shopee_em": "TIMESTAMP",
        # 14/10: marca "refazer" (cadastro que está sendo refeito).
        "refazer": "BOOLEAN",
        # 14/10: quem assumiu o cadastro e desde quando (tempo de cadastro).
        "assumido_por": "VARCHAR",
        "assumido_em": "TIMESTAMP",
        # 14/10: reabertura de um produto já travado (cadastrado) -- quem, quando
        # e o motivo informado pelo supervisor/admin. Linhas antigas ficam NULL
        # (nunca foram reabertas).
        "reaberto_por": "VARCHAR",
        "reaberto_em": "TIMESTAMP",
        "motivo_reabertura": "TEXT",
        # 15/10: "é kit?" -- só preenchido pela tela de solicitação manual.
        "kit": "BOOLEAN",
        # 15/10: exclusão (admin/supervisor) -- não apaga a linha, só marca e guarda
        # quem excluiu, quando e o motivo (obrigatório), pra consultar depois.
        "excluido_por": "VARCHAR",
        "excluido_em": "TIMESTAMP",
        "motivo_exclusao": "TEXT",
        # 16/10: link do Drive (ou onde estiverem) com as fotos do produto --
        # opcional, por produto, preenchido na hora de solicitar o cadastro
        # (equipe ou seller). Linhas antigas ficam NULL.
        "link_fotos": "VARCHAR",
    },
    "usuarios": {
        # Bloqueio temporário por senha errada repetida (login do painel
        # e da extensão CMX, ver app/auth.py). Linhas antigas ficam com
        # tentativas_login_falhas NULL até o primeiro login -- tratado
        # como 0 no código (nunca lido direto sem fallback).
        "tentativas_login_falhas": "INTEGER",
        "bloqueado_ate": "TIMESTAMP",
        # Setores de "operador"/"gerente_setor" (15/10, ver app/models.py).
        # Nulo pra todo mundo que já existe -- não muda nada do que já está em uso.
        "setores": "VARCHAR",
    },
    "eventos_ia": {
        # 18/10: termômetro de crédito -- registra o que cada chamada à IA
        # consumiu, pra estimar o custo localmente quando não tem chave Admin
        # da Anthropic configurada (ver app/uso_ia_custo.py). Linhas antigas
        # (antes desta mudança) ficam NULL e não entram na estimativa.
        "modelo": "VARCHAR",
        "tokens_entrada": "INTEGER",
        "tokens_saida": "INTEGER",
        "buscas_web": "INTEGER",
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
    ("ix_solicitacoes_cancelamento_tipo", "solicitacoes_cancelamento", "tipo"),
]


# Tempo máximo (segundos) que UM comando de estrutura (ALTER/CREATE INDEX) espera
# pela tabela ficar livre no Postgres, e quantas vezes tenta de novo. Sem isso, se
# outra conexão estiver usando a tabela (ex.: a versão antiga ainda no ar durante o
# deploy), o ALTER espera PARA SEMPRE -- a subida trava ("fica só rodando") e, pior,
# o ALTER na fila bloqueia também as consultas novas na tabela, derrubando a versão
# antiga. Com limite, ele desiste rápido, libera a fila e tenta de novo.
_LOCK_TIMEOUT_S = 5
_TENTATIVAS_DDL = 6
_PAUSA_ENTRE_TENTATIVAS_S = 3


def _executar_ddl(sql: str) -> None:
    """
    Roda UM comando de estrutura (ALTER TABLE / CREATE INDEX) numa transação
    própria, com limite de espera por lock no Postgres e novas tentativas.
    No SQLite (uso local) roda direto, sem limite. Se esgotar as tentativas,
    levanta o erro (a subida falha com mensagem clara no log em vez de travar).
    """
    import time
    postgres = engine.dialect.name == "postgresql"
    for tentativa in range(1, _TENTATIVAS_DDL + 1):
        try:
            with engine.begin() as conexao:
                if postgres:
                    # SET LOCAL vale só pra esta transação.
                    conexao.execute(text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT_S}s'"))
                conexao.execute(text(sql))
            return
        except Exception as erro:
            if tentativa >= _TENTATIVAS_DDL:
                logger.error("Estrutura do banco: desisti de '%s' após %d tentativas: %s", sql, tentativa, erro)
                raise
            logger.warning("Estrutura do banco: tabela ocupada (tentativa %d/%d) em '%s'; tentando de novo...",
                           tentativa, _TENTATIVAS_DDL, sql)
            time.sleep(_PAUSA_ENTRE_TENTATIVAS_S)


def garantir_estrutura_atualizada() -> None:
    """
    Adiciona as colunas/índices novos se ainda não existirem e preenche
    a conta_chave dos registros antigos. Chamada uma vez na subida do
    sistema, logo depois do create_all. Se falhar, registra no log e
    interrompe a subida -- melhor o deploy falhar (e o Railway manter a
    versão anterior no ar) do que rodar com o banco pela metade.
    Cada ALTER/CREATE INDEX roda isolado, com limite de espera (ver
    _executar_ddl): coluna já criada numa tentativa anterior não é refeita.
    """
    try:
        inspetor = inspect(engine)
        for tabela, colunas in _COLUNAS_ADICIONAIS.items():
            if not inspetor.has_table(tabela):
                continue  # tabela nova: o create_all já criou completa
            existentes = {c["name"] for c in inspetor.get_columns(tabela)}
            for nome, tipo in colunas.items():
                if nome not in existentes:
                    # "IF NOT EXISTS" só no Postgres (o SQLite não aceita nesse comando).
                    ine = "IF NOT EXISTS " if engine.dialect.name == "postgresql" else ""
                    _executar_ddl(f"ALTER TABLE {tabela} ADD COLUMN {ine}{nome} {tipo}")
                    logger.info("Coluna adicionada: %s.%s", tabela, nome)
        for nome_indice, tabela, coluna in _INDICES_ADICIONAIS:
            # Só cria se ainda não existe: evita pegar lock na tabela à toa em todo boot.
            if inspetor.has_table(tabela) and nome_indice in {i["name"] for i in inspetor.get_indexes(tabela)}:
                continue
            _executar_ddl(f"CREATE INDEX IF NOT EXISTS {nome_indice} ON {tabela} ({coluna})")

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

        # base_calculo das taxas antigas (cadastradas antes deste campo
        # existir) vira NULL pelo ALTER ADD COLUMN -- preenche como
        # "venda_bruta" pra manter o cálculo exatamente igual ao de antes.
        if inspetor.has_table("variaveis_conta"):
            with engine.begin() as conexao:
                conexao.execute(
                    text("UPDATE variaveis_conta SET base_calculo = 'venda_bruta' WHERE base_calculo IS NULL")
                )
    except Exception:
        logger.exception("Falha ao atualizar a estrutura do banco")
        raise
