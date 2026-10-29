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
from sqlalchemy.orm import Session, defer

from app.contas_util import chave_conta
from app.database import get_db
from app.models import EstoqueSku, EventoAdesaoCmx, MensagemPosVenda, Pergunta, SolicitacaoCancelamento
from app.routers.custos_painel import _seller_logado
from app.routers.cmx import _conta_vinculada_do_usuario
from app.routers.vendas import _evolucao_3_meses, _montar_resumo, _somente_vendas_validas, _FUSO_BR, Venda

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

    # Mesma janela de 14 dias, só que do período ANTERIOR (os 14 dias de
    # antes) -- pra sobrepor no gráfico como linha de comparação, alinhada
    # posição a posição (dia 1 do período atual vs. dia 1 do anterior).
    evolucao_14_dias_anterior = []
    for i in range(27, 13, -1):
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
        evolucao_14_dias_anterior.append(
            {"data": dia_br.date().isoformat(), "venda_bruta": round(sum(v[0] for v in total_dia), 2)}
        )

    # Evolução do mês corrente (dia 1 até hoje) -- pro filtro "Este mês"
    # da tela, alternativa aos "últimos 14 dias".
    evolucao_mes = []
    dia_cursor_br = datetime.now(_FUSO_BR).replace(hour=0, minute=0, second=0, microsecond=0).replace(day=1)
    hoje_br = datetime.now(_FUSO_BR).replace(hour=0, minute=0, second=0, microsecond=0)
    while dia_cursor_br <= hoje_br:
        inicio = dia_cursor_br.astimezone(timezone.utc).replace(tzinfo=None)
        fim = (dia_cursor_br + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)
        total_dia = (
            _somente_vendas_validas(
                db.query(Venda).filter(Venda.conta_id == conta.id, Venda.data_venda >= inicio, Venda.data_venda < fim)
            )
            .with_entities(Venda.venda_bruta)
            .all()
        )
        evolucao_mes.append({"data": dia_cursor_br.date().isoformat(), "venda_bruta": round(sum(v[0] for v in total_dia), 2)})
        dia_cursor_br += timedelta(days=1)

    # Comparação com o mês anterior: mesmo número de dias decorridos (pra
    # ser justo -- dia 12 deste mês contra os primeiros 12 dias do mês
    # passado, não o mês passado inteiro).
    dias_decorridos = len(evolucao_mes)
    inicio_mes_anterior_br = (inicio_mes_utc.replace(tzinfo=timezone.utc).astimezone(_FUSO_BR) - timedelta(days=1)).replace(day=1)
    fim_mes_anterior_comparavel_br = inicio_mes_anterior_br + timedelta(days=dias_decorridos)
    inicio_mes_anterior_utc = inicio_mes_anterior_br.astimezone(timezone.utc).replace(tzinfo=None)
    fim_mes_anterior_comparavel_utc = fim_mes_anterior_comparavel_br.astimezone(timezone.utc).replace(tzinfo=None)
    vendido_mes_anterior_comparavel = (
        _somente_vendas_validas(
            db.query(Venda).filter(
                Venda.conta_id == conta.id,
                Venda.data_venda >= inicio_mes_anterior_utc,
                Venda.data_venda < fim_mes_anterior_comparavel_utc,
            )
        )
        .with_entities(Venda.venda_bruta)
        .all()
    )
    valor_mes_anterior = round(sum(v[0] for v in vendido_mes_anterior_comparavel), 2)
    valor_mes_atual = resumo_mes["venda_bruta"]

    # Mesma série, dia a dia, do mês anterior (só os dias comparáveis) --
    # pra sobrepor no gráfico igual a linha dos "14 dias", alinhada por
    # posição (dia 1 do mês atual vs. dia 1 do mês passado).
    evolucao_mes_anterior = []
    for i in range(dias_decorridos):
        dia_ant_br = inicio_mes_anterior_br + timedelta(days=i)
        inicio = dia_ant_br.astimezone(timezone.utc).replace(tzinfo=None)
        fim = (dia_ant_br + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)
        total_dia = (
            _somente_vendas_validas(
                db.query(Venda).filter(Venda.conta_id == conta.id, Venda.data_venda >= inicio, Venda.data_venda < fim)
            )
            .with_entities(Venda.venda_bruta)
            .all()
        )
        evolucao_mes_anterior.append(
            {"data": dia_ant_br.date().isoformat(), "venda_bruta": round(sum(v[0] for v in total_dia), 2)}
        )
    variacao_pct = (
        round((valor_mes_atual - valor_mes_anterior) / valor_mes_anterior * 100, 1) if valor_mes_anterior else None
    )
    comparacao_mes_anterior = {
        "valor_mes_atual": valor_mes_atual,
        "valor_mes_anterior_mesmo_periodo": valor_mes_anterior,
        "dias_comparados": dias_decorridos,
        "variacao_pct": variacao_pct,
    }

    # Status dos anúncios: lido do cache (EstoqueSku), igual "Produtos > Lista".
    anuncios = db.query(EstoqueSku).filter(EstoqueSku.conta_id == conta.id).all()
    ativos = sum(1 for a in anuncios if a.ativo and a.quantidade > 0)
    sem_estoque = sum(1 for a in anuncios if a.ativo and a.quantidade <= 0)
    pausados = sum(1 for a in anuncios if not a.ativo)

    # Promoções aderidas AUTOMATICAMENTE pelo Precificador (extensão
    # ClubMarketplaceX) -- NÃO é "quantos anúncios têm custo cadastrado
    # agora" (isso é só um retrato do momento). É a CONTAGEM REAL de
    # adesões que aconteceram no período, uma por uma (EventoAdesaoCmx,
    # gravado pela extensão a cada adesão bem-sucedida) -- 20 hoje + 4
    # amanhã + 5 depois = 29 no mês, por exemplo.
    def _promocoes_aderidas(inicio_utc: datetime) -> int:
        return (
            db.query(EventoAdesaoCmx)
            .filter(
                EventoAdesaoCmx.conta_id == conta.id,
                EventoAdesaoCmx.sucesso.is_(True),
                EventoAdesaoCmx.data_adesao >= inicio_utc,
            )
            .count()
        )

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

    # Atendimento (card "O que o Club fez pela sua conta"): cobertura real
    # (% do que chegou nesse período e já foi respondido -- não é só a
    # contagem de respondidas, por isso é uma consulta à parte) e tempo
    # médio de resposta (recebida_em -> respondida_em), em minutos.
    def _atendimento(inicio_utc: datetime) -> dict:
        perguntas_recebidas = (
            db.query(Pergunta).filter(Pergunta.conta_id == conta.id, Pergunta.recebida_em >= inicio_utc).all()
        )
        mensagens_recebidas = (
            db.query(MensagemPosVenda)
            .filter(MensagemPosVenda.conta_id == conta.id, MensagemPosVenda.recebida_em >= inicio_utc)
            .all()
        )
        total_recebido = len(perguntas_recebidas) + len(mensagens_recebidas)
        respondidas_perguntas = [p for p in perguntas_recebidas if p.status in _STATUS_PERGUNTA_RESPONDIDA]
        respondidas_mensagens = [m for m in mensagens_recebidas if m.status == "respondida"]
        total_respondido = len(respondidas_perguntas) + len(respondidas_mensagens)

        tempos_minutos = [
            (item.respondida_em - item.recebida_em).total_seconds() / 60
            for item in (respondidas_perguntas + respondidas_mensagens)
            if item.respondida_em and item.recebida_em
        ]
        tempo_medio = round(sum(tempos_minutos) / len(tempos_minutos)) if tempos_minutos else None

        return {
            "total_recebido": total_recebido,
            "total_respondido": total_respondido,
            "cobertura_pct": round(total_respondido / total_recebido * 100, 1) if total_recebido else None,
            "tempo_medio_minutos": tempo_medio,
        }

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

    def _reversos(inicio_utc: datetime) -> dict:
        """Reversos (produto que voltou) dessa conta no período, por situação:
        chegaram / em andamento (galpão ainda conferindo ou equipe tratando) /
        liberados pra venda / resolvidos com o ML / descartados."""
        linhas = (
            db.query(SolicitacaoCancelamento)
            .options(defer(SolicitacaoCancelamento.fotos_base64))  # só conta: não carrega as fotos
            .filter(SolicitacaoCancelamento.conta_chave == chave)
            .filter(SolicitacaoCancelamento.tipo == "reverso")
            .filter(SolicitacaoCancelamento.criado_em >= inicio_utc)
            .all()
        )
        r = {"chegaram": len(linhas), "em_andamento": 0, "liberados_venda": 0, "nao_voltaram": 0, "garantias": 0}
        for x in linhas:
            res = x.resultado_reverso or ""
            if not x.confirmado_por:
                r["em_andamento"] += 1
            elif res == "liberado_venda":      # = voltou para o estoque
                r["liberados_venda"] += 1
            elif res == "garantia":            # (27/10) foi para o procedimento de garantia
                r["garantias"] += 1
            else:
                r["nao_voltaram"] += 1
        return r

    def _pos_venda_aguardando() -> dict:
        """Conversas de pós-venda esperando resposta (28/10): vira destaque no topo do dashboard e item do bloco escuro."""
        q = db.query(MensagemPosVenda).filter(
            MensagemPosVenda.conta_id == conta.id, MensagemPosVenda.tipo == "mensagem",
            MensagemPosVenda.status.in_(("fila_humana", "enviando")),
        )
        pend = q.order_by(MensagemPosVenda.recebida_em.asc()).all()
        mais_antiga_h = None
        if pend and pend[0].recebida_em:
            mais_antiga_h = round((datetime.utcnow() - pend[0].recebida_em).total_seconds() / 3600, 1)
        from app.pos_venda_reclamacoes import contar_precisam_acao
        return {"aguardando": len(pend), "mais_antiga_horas": mais_antiga_h, "reclamacoes": contar_precisam_acao(db, [conta.id])}

    return {
        "conta": conta.apelido,
        "pos_venda": _pos_venda_aguardando(),
        "hoje": resumo_hoje,
        "mes": resumo_mes,
        "evolucao_14_dias": evolucao,
        "evolucao_14_dias_anterior": evolucao_14_dias_anterior,
        "evolucao_mes": evolucao_mes,
        "evolucao_mes_anterior": evolucao_mes_anterior,
        "evolucao_3_meses": _evolucao_3_meses(db, conta.id),
        "comparacao_mes_anterior": comparacao_mes_anterior,
        "anuncios": {
            "ativos": ativos,
            "sem_estoque": sem_estoque,
            "pausados": pausados,
            "total": len(anuncios),
        },
        "promocoes_aderidas": {
            "hoje": _promocoes_aderidas(inicio_dia_utc),
            "mes": _promocoes_aderidas(inicio_mes_utc),
        },
        "mensagens_respondidas": {
            "hoje": _mensagens_respondidas(inicio_dia_utc),
            "mes": _mensagens_respondidas(inicio_mes_utc),
        },
        "cancelamentos": {
            "hoje": _cancelamentos(inicio_dia_utc),
            "mes": _cancelamentos(inicio_mes_utc),
        },
        "atendimento_mes": _atendimento(inicio_mes_utc),
        "reversos": {
            "hoje": _reversos(inicio_dia_utc),
            "mes": _reversos(inicio_mes_utc),
        },
    }
