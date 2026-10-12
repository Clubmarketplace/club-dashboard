"""
API da tela "Dashboard" do painel do seller: visão rápida da conta --
venda bruta/lucro de hoje e do mês, evolução dos últimos 14 dias, e
status dos anúncios (ativos/sem estoque/pausados). Tudo com dado real,
já existente no banco -- nenhuma informação fictícia ou "card bonito
sem conteúdo" (diferente do mockup inicial, que era só visual).

Mesmo padrão de autenticação/isolamento do resto do painel do seller
(_seller_logado + _conta_vinculada_do_usuario, cookie de sessão).
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.contas_util import chave_conta
from app.database import get_db
from app.models import CustoSku, EstoqueSku, MensagemPosVenda, Pergunta, SolicitacaoCancelamento
from app.routers.custos_painel import _seller_logado
from app.routers.cmx import _conta_vinculada_do_usuario
from app.routers.vendas import _montar_resumo, _somente_vendas_validas, _FUSO_BR, Venda

# Perguntas de pré-venda com esse status chegaram a uma resposta de
# verdade -- "pendente"/"fila_humana" ainda não (igual o resto do
# sistema já trata em app/main.py:pagina_dashboard).
_STATUS_PERGUNTA_RESPONDIDA = ("respondida", "respondida_externamente")

router = APIRouter(prefix="/api/painel/dashboard", tags=["painel-dashboard"])


def _inicio_do_dia_e_do_mes_em_utc() -> tuple[datetime, datetime]:
    """'Hoje' e 'este mês' no calendário de Brasília, convertidos pro UTC
    que `Venda.data_venda` usa -- mesmo cuidado de fuso de vendas.py:_periodo."""
    agora_br = datetime.now(_FUSO_BR)
    inicio_dia_br = agora_br.replace(hour=0, minute=0, second=0, microsecond=0)
    inicio_mes_br = inicio_dia_br.replace(day=1)
    return (
        inicio_dia_br.astimezone(timezone.utc).replace(tzinfo=None),
        inicio_mes_br.astimezone(timezone.utc).replace(tzinfo=None),
    )


@router.get("")
def resumo_dashboard(usuario=Depends(_seller_logado), db: Session = Depends(get_db)):
    conta = _conta_vinculada_do_usuario(usuario, db)
    agora_utc = datetime.utcnow()
    inicio_dia_utc, inicio_mes_utc = _inicio_do_dia_e_do_mes_em_utc()

    resumo_hoje = _montar_resumo(conta, db, inicio_dia_utc, agora_utc, 1)
    resumo_mes = _montar_resumo(conta, db, inicio_mes_utc, agora_utc, (agora_utc - inicio_mes_utc).days + 1)

    # Evolução: venda bruta por dia, últimos 14 dias (gráfico de linha).
    evolucao = []
    for i in range(13, -1, -1):
        dia_br = (datetime.now(_FUSO_BR) - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        inicio = dia_br.astimezone(timezone.utc).replace(tzinfo=None)
        fim = (dia_br + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)
        total_dia = (
            _somente_vendas_validas(
                db.query(Venda).filter(Venda.conta_id == conta.id, Venda.data_venda >= inicio, Venda.data_venda < fim)
            )
            .with_entities(Venda.venda_bruta)
            .all()
        )
        evolucao.append({"data": dia_br.date().isoformat(), "venda_bruta": round(sum(v[0] for v in total_dia), 2)})

    # Status dos anúncios: lido do cache (EstoqueSku), igual "Produtos > Lista".
    anuncios = db.query(EstoqueSku).filter(EstoqueSku.conta_id == conta.id).all()
    ativos = sum(1 for a in anuncios if a.ativo and a.quantidade > 0)
    sem_estoque = sum(1 for a in anuncios if a.ativo and a.quantidade <= 0)
    pausados = sum(1 for a in anuncios if not a.ativo)

    # Aderidos ao Precificador: dos anúncios ATIVOS, quantos já têm custo
    # cadastrado (CustoSku) -- é esse cadastro que liga a extensão
    # ClubMarketplaceX (cálculo de margem/injeção na tela de Promoções).
    skus_com_custo = {
        c.sku for c in db.query(CustoSku.sku).filter(CustoSku.conta_id == conta.id).all()
    }
    anuncios_ativos = [a for a in anuncios if a.ativo]
    aderidos = sum(1 for a in anuncios_ativos if a.sku in skus_com_custo)
    nao_aderidos = len(anuncios_ativos) - aderidos

    # Mensagens respondidas: pré-venda (Pergunta) + pós-venda (MensagemPosVenda).
    def _mensagens_respondidas(inicio_utc: datetime) -> int:
        perguntas = (
            db.query(Pergunta)
            .filter(Pergunta.conta_id == conta.id)
            .filter(Pergunta.status.in_(_STATUS_PERGUNTA_RESPONDIDA))
            .filter(Pergunta.respondida_em >= inicio_utc)
            .count()
        )
        pos_venda = (
            db.query(MensagemPosVenda)
            .filter(MensagemPosVenda.conta_id == conta.id)
            .filter(MensagemPosVenda.status == "respondida")
            .filter(MensagemPosVenda.respondida_em >= inicio_utc)
            .count()
        )
        return perguntas + pos_venda

    # Pedidos de cancelamento feitos por essa conta (tela "Minhas
    # solicitações"/"Nova solicitação") -- casados pela chave normalizada
    # do apelido, igual o resto do sistema (app/contas_util.py).
    chave = chave_conta(conta.apelido)

    def _cancelamentos(inicio_utc: datetime) -> dict:
        consulta = (
            db.query(SolicitacaoCancelamento)
            .filter(SolicitacaoCancelamento.conta_chave == chave)
            .filter(SolicitacaoCancelamento.criado_em >= inicio_utc)
            .filter(
                (SolicitacaoCancelamento.tipo.is_(None)) | (SolicitacaoCancelamento.tipo == "cancelamento")
            )
        )
        total = consulta.count()
        confirmados = consulta.filter(SolicitacaoCancelamento.confirmado_por.isnot(None)).count()
        return {"total": total, "confirmados": confirmados, "pendentes": total - confirmados}

    return {
        "conta": conta.apelido,
        "hoje": resumo_hoje,
        "mes": resumo_mes,
        "evolucao_14_dias": evolucao,
        "anuncios": {
            "ativos": ativos,
            "sem_estoque": sem_estoque,
            "pausados": pausados,
            "total": len(anuncios),
            "aderidos_precificador": aderidos,
            "nao_aderidos_precificador": nao_aderidos,
        },
        "mensagens_respondidas": {
            "hoje": _mensagens_respondidas(inicio_dia_utc),
            "mes": _mensagens_respondidas(inicio_mes_utc),
        },
        "cancelamentos": {
            "hoje": _cancelamentos(inicio_dia_utc),
            "mes": _cancelamentos(inicio_mes_utc),
        },
    }
