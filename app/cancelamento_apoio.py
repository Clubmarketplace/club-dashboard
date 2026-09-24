"""
Apoio às solicitações de cancelamento:

  1. HISTÓRICO -- registrar_evento(): grava cada ação (registrou, assumiu,
     liberou, confirmou...) na tabela solicitacao_eventos. Só acrescenta,
     nunca altera nem apaga. É a base do "ver histórico" e dos relatórios
     por operador.

  2. PRODUTO DO PEDIDO -- preencher_produto_do_pedido() e
     preencher_produtos_em_segundo_plano(): leem no Mercado Livre o SKU e
     o nome do produto de uma venda (order_items do pedido), pra o
     relatório somar os cancelamentos por SKU.

Tudo aqui é "melhor esforço": qualquer falha vai pro log e nunca derruba
quem chamou (registrar uma solicitação ou confirmar tem que funcionar
mesmo se o Mercado Livre estiver fora do ar).
"""
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.contas_util import chave_conta
from app.models import Conta, SolicitacaoCancelamento, SolicitacaoEvento

logger = logging.getLogger(__name__)

TAMANHO_MAX_DETALHE = 500


# ---------------------------------------------------------------------------
# Histórico
# ---------------------------------------------------------------------------
def registrar_evento(db: Session, solicitacao_id: int, tipo: str, usuario=None, detalhe: str | None = None, nome: str | None = None) -> None:
    """
    Acrescenta um evento ao histórico (NÃO faz commit -- entra junto com a
    transação de quem chamou, pra ação e registro ficarem sempre juntos).
    `usuario` é o objeto Usuario logado; `nome` serve pra ações do sistema.
    """
    db.add(SolicitacaoEvento(
        solicitacao_id=solicitacao_id,
        tipo=tipo,
        usuario_id=getattr(usuario, "id", None),
        usuario_nome=getattr(usuario, "nome_exibicao", None) or nome,
        detalhe=(detalhe or None) and detalhe[:TAMANHO_MAX_DETALHE],
        quando=datetime.utcnow(),
    ))


# ---------------------------------------------------------------------------
# Produto (SKU) do pedido no Mercado Livre
# ---------------------------------------------------------------------------
def extrair_produto_do_pedido(pedido: dict) -> tuple[str, str | None]:
    """
    Do pedido do ML devolve (skus, nomes). Pedido com mais de um produto:
    SKUs separados por "," e nomes por " | ", NA MESMA ORDEM (o relatório
    casa cada SKU com o seu nome). SKU "" = pedido lido, sem SKU cadastrado.
    """
    skus, titulos = [], []
    for item_pedido in pedido.get("order_items") or []:
        item = item_pedido.get("item") or {}
        sku = (item.get("seller_sku") or item.get("seller_custom_field") or "").strip().replace(",", " ")
        if sku and sku not in skus:
            skus.append(sku)
            titulos.append((item.get("title") or "").replace("|", "/").strip())
    if not skus:  # sem SKU: guarda ao menos o nome do (primeiro) produto
        primeiro = next((i.get("item", {}).get("title") for i in pedido.get("order_items") or [] if i.get("item", {}).get("title")), None)
        return "", primeiro
    return ",".join(skus), " | ".join(titulos)


def preencher_produto_do_pedido(solicitacao: SolicitacaoCancelamento, pedido: dict) -> None:
    """
    Grava SKU/produto na solicitação se ainda não tiver (não faz commit).
    sku "" (tentado antes e não lido) também é preenchido quando der certo.
    """
    if solicitacao.sku and solicitacao.produto_titulo:
        return
    sku, titulo = extrair_produto_do_pedido(pedido)
    if not solicitacao.sku:
        solicitacao.sku = sku
    if not solicitacao.produto_titulo and titulo:
        solicitacao.produto_titulo = titulo


def conta_da_solicitacao(solicitacao: SolicitacaoCancelamento, db: Session) -> Conta | None:
    """Conta conectada correspondente à solicitação (pela chave do nome)."""
    chave = solicitacao.conta_chave or chave_conta(solicitacao.conta)
    for candidata in db.query(Conta).all():
        if chave_conta(candidata.apelido) == chave:
            return candidata
    return None


def buscar_produto_da_venda(solicitacao: SolicitacaoCancelamento, db: Session) -> str:
    """
    Busca no ML o SKU/produto de uma solicitação do Mercado Livre.
    Devolve "ok" | "sem_conta" | "falha" (nunca levanta exceção).
    """
    from app.ml_client import MLApiError, MLAuthError, buscar_pedido, garantir_token_valido

    if solicitacao.plataforma != "mercado_livre":
        return "falha"
    conta = conta_da_solicitacao(solicitacao, db)
    if conta is None:
        return "sem_conta"
    try:
        token = garantir_token_valido(conta, db)
        pedido = buscar_pedido(token, solicitacao.numero_venda)
    except (MLAuthError, MLApiError) as exc:
        logger.info("Produto da venda %s não lido agora: %s", solicitacao.numero_venda, exc)
        return "falha"
    except Exception:
        logger.exception("Erro inesperado ao ler o produto da venda %s", solicitacao.numero_venda)
        return "falha"
    preencher_produto_do_pedido(solicitacao, pedido)
    return "ok"


def preencher_produtos_em_segundo_plano(ids: list[int]) -> None:
    """
    Chamado DEPOIS de registrar uma solicitação (tarefa em segundo plano),
    pra não deixar o formulário esperando o Mercado Livre responder.
    """
    from app.database import SessionLocal

    try:
        with SessionLocal() as db:
            for solicitacao in db.query(SolicitacaoCancelamento).filter(SolicitacaoCancelamento.id.in_(ids)).all():
                if solicitacao.sku is None:
                    buscar_produto_da_venda(solicitacao, db)
            db.commit()
    except Exception:
        logger.exception("Falha ao preencher o produto das solicitações %s", ids)
