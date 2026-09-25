"""
Verificação automática do status das SOLICITAÇÕES MANUAIS de
cancelamento (ver routers/solicitacoes_cancelamento.py) e reposição
de estoque quando a venda realmente foi cancelada.

Como funciona: o cancelamento em si continua sendo feito por fora --
pelo painel do Mercado Livre ou pelo atendimento deles (ver os botões
"Copiar mensagem" / "Abrir atendimento ML" na tela). Esse módulo só
CONSULTA (GET /orders/{id}, leitura pura, sem risco nenhum) se a venda
já foi cancelada, e se sim, lê o campo cancel_detail que o próprio
Mercado Livre preenche sozinho pra saber se pesou ou não na
reputação -- sem precisar de nenhuma confirmação manual.

Mapeamento cancel_detail.group -> resultado_impacto:
  "seller"                                    -> com_impacto (culpa do vendedor)
  "buyer" / "shipment" / "fiscal" /
  "mediations" / "fraud"                      -> sem_impacto (fora do controle do vendedor)
  qualquer outro valor, ou ausente             -> aguardando_confirmacao (não arrisca
                                                   adivinhar; fica pra revisão manual)

IMPORTANTE: esse mapeamento é baseado na documentação oficial do
Mercado Livre, mas nunca foi confirmado contra uma venda cancelada de
verdade. Teste com 1-2 solicitações reais (usando o botão "🔄
Verificar agora" na tela) antes de confiar nele em volume.
"""
import asyncio
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.contas_util import chave_conta
from app.database import SessionLocal
from app.ml_client import MLApiError, MLAuthError, buscar_pedido, garantir_token_valido, repor_estoque_item
from app.cancelamento_apoio import preencher_produto_do_pedido, registrar_evento
from app.models import Conta, SolicitacaoCancelamento

logger = logging.getLogger("verificacao_cancelamento")

# A cada 12 min -- dentro da faixa de 10-15 min pedida.
INTERVALO_SEGUNDOS = 12 * 60

GRUPOS_SEM_IMPACTO = {"buyer", "shipment", "fiscal", "mediations", "fraud"}
GRUPOS_COM_IMPACTO = {"seller"}


def _classificar_resultado(cancel_detail: dict | None) -> str:
    grupo = (cancel_detail or {}).get("group")
    if grupo in GRUPOS_COM_IMPACTO:
        return "com_impacto"
    if grupo in GRUPOS_SEM_IMPACTO:
        return "sem_impacto"
    return "aguardando_confirmacao"


def _achar_conta(solicitacao: SolicitacaoCancelamento, db: Session) -> Conta | None:
    """Acha a Conta cadastrada em /contas correspondente a essa solicitação, pela chave normalizada do nome."""
    # Usa a busca única (ignora contas inativas e, havendo nomes repetidos,
    # prefere a conectada) -- ver contas_util.achar_conta_por_nome.
    from app.contas_util import achar_conta_por_nome
    return achar_conta_por_nome(db, solicitacao.conta_chave or solicitacao.conta)


def _repor_estoque_do_pedido(access_token: str, pedido: dict) -> None:
    """Soma de volta, em cada anúncio do pedido cancelado, a quantidade vendida."""
    for item_pedido in pedido.get("order_items") or []:
        item = item_pedido.get("item") or {}
        item_id = item.get("id")
        variation_id = item.get("variation_id")
        quantidade = item_pedido.get("quantity") or 0
        if not item_id or not quantidade:
            continue
        try:
            repor_estoque_item(access_token, item_id, quantidade, variation_id)
        except MLApiError as exc:
            logger.warning("Falha ao repor estoque do item %s (pedido %s): %s", item_id, pedido.get("id"), exc)


def verificar_uma_solicitacao(solicitacao: SolicitacaoCancelamento, db: Session) -> dict:
    """
    Confere se essa solicitação pendente já foi cancelada de verdade
    no Mercado Livre. Se sim, confirma sozinha (com o resultado real
    vindo da API) e repõe o estoque.

    Devolve um dicionário com o resultado E O MOTIVO -- importante pra
    diagnóstico: sem isso, qualquer falha (conta não encontrada, token
    inválido, erro de rede) ficava indistinguível de "ainda não foi
    cancelada", e ninguém conseguia saber por que não funcionou:
        {"confirmou": bool, "situacao": str, "detalhe": str}
    situacao é um de: "confirmado", "ainda_pendente",
    "conta_nao_encontrada", "erro_token", "erro_consulta".
    Nunca levanta exceção.
    """
    if solicitacao.plataforma != "mercado_livre":
        return {"confirmou": False, "situacao": "plataforma_nao_suportada", "detalhe": "Verificação automática só existe pra Mercado Livre."}
    if solicitacao.confirmado_por:
        return {"confirmou": False, "situacao": "ja_confirmada", "detalhe": "Essa solicitação já foi confirmada antes."}

    conta = _achar_conta(solicitacao, db)
    if conta is None:
        return {
            "confirmou": False,
            "situacao": "conta_nao_encontrada",
            "detalhe": (
                f'Não achei nenhuma conta cadastrada em /contas com o nome "{solicitacao.conta}" '
                f"(mesmo ignorando maiúscula/espaço). Confira se o apelido bate exatamente com o "
                f"cadastrado lá."
            ),
        }

    try:
        access_token = garantir_token_valido(conta, db)
    except MLAuthError as exc:
        logger.info("Erro de token ao checar a venda %s: %s", solicitacao.numero_venda, exc)
        return {"confirmou": False, "situacao": "erro_token", "detalhe": f"Problema com o token da conta '{conta.apelido}': {exc}"}

    try:
        pedido = buscar_pedido(access_token, solicitacao.numero_venda)
    except MLApiError as exc:
        logger.info("Erro ao consultar a venda %s: %s", solicitacao.numero_venda, exc)
        return {"confirmou": False, "situacao": "erro_consulta", "detalhe": f"O Mercado Livre recusou a consulta: {exc}"}

    # Aproveita a consulta pra guardar o SKU/produto da venda (relatório por SKU).
    preencher_produto_do_pedido(solicitacao, pedido)

    status_pedido = pedido.get("status")
    if status_pedido != "cancelled":
        db.commit()  # grava o SKU, se foi lido agora
        return {
            "confirmou": False,
            "situacao": "ainda_pendente",
            "detalhe": f'O Mercado Livre ainda mostra o status "{status_pedido}" pra essa venda -- ainda não foi cancelada por lá.',
        }

    solicitacao.resultado_impacto = _classificar_resultado(pedido.get("cancel_detail"))
    solicitacao.confirmado_por = "Sistema (verificação automática)"
    solicitacao.confirmado_em = datetime.utcnow()
    # Saiu do "em atendimento" (se alguém estava com o pedido) e fica no histórico.
    solicitacao.em_atendimento_por = None
    solicitacao.em_atendimento_por_id = None
    solicitacao.em_atendimento_desde = None
    registrar_evento(db, solicitacao.id, "confirmou_automatico", nome="Sistema",
                     detalhe=f"Mercado Livre: {(pedido.get('cancel_detail') or {}).get('description') or 'pedido cancelado'}")
    db.commit()

    aviso_estoque = ""
    try:
        _repor_estoque_do_pedido(access_token, pedido)
    except Exception:
        logger.exception("Falha ao repor estoque da venda %s", solicitacao.numero_venda)
        aviso_estoque = " (obs: não consegui repor o estoque automaticamente -- confira manualmente)"

    return {"confirmou": True, "situacao": "confirmado", "detalhe": f"Cancelamento confirmado no Mercado Livre!{aviso_estoque}"}


def verificar_pendentes() -> None:
    """Passa por todas as solicitações pendentes do Mercado Livre e confere cada uma."""
    db = SessionLocal()
    try:
        pendentes = (
            db.query(SolicitacaoCancelamento)
            .filter(SolicitacaoCancelamento.plataforma == "mercado_livre")
            .filter(SolicitacaoCancelamento.confirmado_por.is_(None))
            .all()
        )
        confirmadas = 0
        situacoes: dict[str, int] = {}
        for solicitacao in pendentes:
            resultado = verificar_uma_solicitacao(solicitacao, db)
            situacoes[resultado["situacao"]] = situacoes.get(resultado["situacao"], 0) + 1
            if resultado["confirmou"]:
                confirmadas += 1
        if pendentes:
            logger.info(
                "Verificação de cancelamentos: %d pendente(s) checada(s), %d confirmada(s). Detalhe: %s",
                len(pendentes), confirmadas, situacoes,
            )
    finally:
        db.close()


async def loop_verificacao_cancelamentos() -> None:
    """Roda verificar_pendentes() pra sempre, em segundo plano, sem travar o servidor."""
    while True:
        try:
            await asyncio.to_thread(verificar_pendentes)
        except Exception:
            logger.exception("Erro inesperado no loop de verificação de cancelamentos")
        await asyncio.sleep(INTERVALO_SEGUNDOS)
