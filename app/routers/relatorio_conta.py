from datetime import datetime, timedelta
from collections import Counter
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import AcaoRegistrada, Conta

router = APIRouter(prefix="/api/relatorio-conta", tags=["relatorio-conta"])


@router.get("/{conta_id}")
def relatorio_da_conta(
    conta_id: int,
    dias: int = Query(30, description="Janela de dias pro relatório"),
    db: Session = Depends(get_db),
):
    """
    Resumo do que o sistema fez numa conta específica, no período
    pedido — pensado pro caso descrito: "Velasco: X vendas com impacto
    evitado, Y canceladas por falta de estoque, Z perguntas respondidas
    sobre os SKUs tal/tal/tal".
    """
    conta = db.query(Conta).filter(Conta.id == conta_id).first()
    if not conta:
        return {"erro": "Conta não encontrada."}

    limite_data = datetime.utcnow() - timedelta(days=dias)
    acoes = (
        db.query(AcaoRegistrada)
        .filter(AcaoRegistrada.conta_id == conta_id, AcaoRegistrada.criado_em >= limite_data)
        .all()
    )

    perguntas_respondidas = [a for a in acoes if a.tipo == "pergunta_respondida"]
    pedidos_cancelados = [a for a in acoes if a.tipo == "pedido_cancelado"]
    promocoes_aderidas = [a for a in acoes if a.tipo == "promocao_aderida"]

    skus_perguntas = Counter(a.sku for a in perguntas_respondidas if a.sku)
    valor_cancelado = sum(a.valor_envolvido or 0 for a in pedidos_cancelados)

    return {
        "conta": {"id": conta.id, "apelido": conta.apelido},
        "janela_dias": dias,
        "perguntas_respondidas": {
            "total": len(perguntas_respondidas),
            "por_sku": [{"sku": sku, "quantidade": qtd} for sku, qtd in skus_perguntas.most_common()],
        },
        "pedidos_cancelados": {
            "total": len(pedidos_cancelados),
            "valor_total_envolvido": round(valor_cancelado, 2),
            "motivos": [{"detalhe": a.detalhe, "sku": a.sku, "valor": a.valor_envolvido} for a in pedidos_cancelados],
        },
        "promocoes_aderidas": {"total": len(promocoes_aderidas)},
    }
