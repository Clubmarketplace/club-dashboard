from datetime import datetime, timedelta
from collections import Counter
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.database import get_db
from app.models import Devolucao, Conta

router = APIRouter(prefix="/api/devolucoes", tags=["devolucoes"])

STATUS_ABERTOS = ("opened", "shipped", "delivered", "not_delivered")
STATUS_ENCERRADOS = ("closed", "cancelled", "failed", "expired")


@router.get("/resumo")
def resumo(
    dias: int = Query(30, description="Janela de dias pra considerar no resumo"),
    conta_id: int | None = Query(None, description="Filtra por uma conta específica; se vazio, soma todas"),
    db: Session = Depends(get_db),
):
    """
    KPIs agregados pro cabeçalho do painel: total de devoluções, quantas
    em aberto, valor envolvido, custo de frete de retorno, e o motivo
    mais comum — tudo dentro da janela de dias pedida.
    """
    limite_data = datetime.utcnow() - timedelta(days=dias)
    query = db.query(Devolucao).filter(Devolucao.data_criacao >= limite_data)
    if conta_id:
        query = query.filter(Devolucao.conta_id == conta_id)
    devolucoes = query.all()

    total = len(devolucoes)
    abertas = sum(1 for d in devolucoes if d.status in STATUS_ABERTOS)
    valor_envolvido = sum(d.valor or 0 for d in devolucoes)
    custo_frete = sum(d.custo_frete_retorno or 0 for d in devolucoes)

    contagem_motivos = Counter(d.motivo for d in devolucoes if d.motivo)
    motivo_mais_comum = contagem_motivos.most_common(1)
    motivo_mais_comum = motivo_mais_comum[0] if motivo_mais_comum else None

    contagem_condicao = Counter(d.product_condition for d in devolucoes if d.product_condition)

    return {
        "janela_dias": dias,
        "total_devolucoes": total,
        "devolucoes_abertas": abertas,
        "valor_envolvido": round(valor_envolvido, 2),
        "custo_frete_retorno": round(custo_frete, 2),
        "impacto_total": round(valor_envolvido + custo_frete, 2),
        "motivo_mais_comum": {"motivo": motivo_mais_comum[0], "quantidade": motivo_mais_comum[1]} if motivo_mais_comum else None,
        "condicao_produto": dict(contagem_condicao),
    }


@router.get("/por-motivo")
def por_motivo(
    dias: int = Query(30),
    conta_id: int | None = Query(None),
    db: Session = Depends(get_db),
):
    """Contagem de devoluções por motivo — alimenta o gráfico de barras/pizza."""
    limite_data = datetime.utcnow() - timedelta(days=dias)
    query = db.query(Devolucao.motivo, func.count(Devolucao.id)).filter(Devolucao.data_criacao >= limite_data)
    if conta_id:
        query = query.filter(Devolucao.conta_id == conta_id)
    resultado = query.group_by(Devolucao.motivo).all()
    return [{"motivo": motivo or "Não informado", "quantidade": qtd} for motivo, qtd in resultado]


@router.get("/tendencia-mensal")
def tendencia_mensal(
    meses: int = Query(6, description="Quantos meses pra trás mostrar"),
    conta_id: int | None = Query(None),
    db: Session = Depends(get_db),
):
    """Total de devoluções por mês — alimenta o gráfico de linha/tendência."""
    limite_data = datetime.utcnow() - timedelta(days=meses * 30)
    query = db.query(Devolucao).filter(Devolucao.data_criacao >= limite_data)
    if conta_id:
        query = query.filter(Devolucao.conta_id == conta_id)
    devolucoes = query.all()

    por_mes: dict[str, int] = {}
    for d in devolucoes:
        if not d.data_criacao:
            continue
        chave = d.data_criacao.strftime("%Y-%m")
        por_mes[chave] = por_mes.get(chave, 0) + 1

    meses_ordenados = sorted(por_mes.keys())
    return [{"mes": m, "quantidade": por_mes[m]} for m in meses_ordenados]


@router.get("/lista")
def lista(
    dias: int = Query(30),
    conta_id: int | None = Query(None),
    status: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """Lista detalhada de devoluções pra tabela do painel."""
    limite_data = datetime.utcnow() - timedelta(days=dias)
    query = db.query(Devolucao).filter(Devolucao.data_criacao >= limite_data)
    if conta_id:
        query = query.filter(Devolucao.conta_id == conta_id)
    if status:
        query = query.filter(Devolucao.status == status)
    devolucoes = query.order_by(Devolucao.data_criacao.desc()).all()

    return [
        {
            "id": d.id,
            "claim_id": d.claim_id,
            "sku": d.sku,
            "nome_produto": d.nome_produto,
            "motivo": d.motivo,
            "status": d.status,
            "product_condition": d.product_condition,
            "valor": d.valor,
            "custo_frete_retorno": d.custo_frete_retorno,
            "data_criacao": d.data_criacao.isoformat() if d.data_criacao else None,
            "data_entrega": d.data_entrega.isoformat() if d.data_entrega else None,
        }
        for d in devolucoes
    ]


@router.get("/contas")
def listar_contas(db: Session = Depends(get_db)):
    """Lista as contas conectadas, pro seletor de filtro do painel."""
    contas = db.query(Conta).filter(Conta.ativa == True).all()  # noqa: E712
    return [{"id": c.id, "apelido": c.apelido} for c in contas]
