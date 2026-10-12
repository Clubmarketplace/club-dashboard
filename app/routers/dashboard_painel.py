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

from app.database import get_db
from app.models import EstoqueSku
from app.routers.custos_painel import _seller_logado
from app.routers.cmx import _conta_vinculada_do_usuario
from app.routers.vendas import _montar_resumo, _somente_vendas_validas, _FUSO_BR, Venda

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
        },
    }
