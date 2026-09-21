"""
Fila de pedidos de cancelamento -- detectados por palavra-chave dentro
de mensagens/reclamações (ver pos_venda_logica.py). Por decisão de
negócio, NENHUMA ação de cancelar é automática aqui: a IA só sugere um
texto de resposta (resposta_sugerida), e um humano decide e registra
o desfecho (aprovado, negado, orientado etc.) em observacao_humano.
Esse histórico serve de base pra, no futuro, treinar a IA a
reconhecer o padrão de resposta esperado e agir sozinha.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import PedidoCancelamento

router = APIRouter(prefix="/api/cancelamentos", tags=["cancelamentos"])


class TratarEntrada(BaseModel):
    observacao: str


@router.get("/lista")
def listar(db: Session = Depends(get_db), status: str = "pendente"):
    """status: 'pendente' (padrão) ou 'tratado'."""
    pedidos = (
        db.query(PedidoCancelamento)
        .filter(PedidoCancelamento.status == status)
        .order_by(PedidoCancelamento.criado_em.desc())
        .all()
    )
    return [
        {
            "id": p.id,
            "conta": p.conta.apelido if p.conta else "—",
            "order_id": p.order_id,
            "sku": p.sku,
            "texto_pedido": p.texto_pedido,
            "resposta_sugerida": p.resposta_sugerida,
            "observacao_humano": p.observacao_humano,
            "criado_em": p.criado_em.isoformat() if p.criado_em else None,
            "tratado_em": p.tratado_em.isoformat() if p.tratado_em else None,
        }
        for p in pedidos
    ]


@router.post("/{pedido_id}/tratar")
def tratar(pedido_id: int, corpo: TratarEntrada, db: Session = Depends(get_db)):
    """
    Marca o pedido como tratado, com a observação de quem cuidou (o
    que foi decidido/feito -- aprovado, negado, orientado, etc.). Não
    envia nada nem cancela nada pela API -- isso continua manual, pelo
    Mercado Livre direto, nesse primeiro momento.
    """
    pedido = db.query(PedidoCancelamento).filter(PedidoCancelamento.id == pedido_id).first()
    if pedido is None:
        raise HTTPException(status_code=404, detail="Pedido de cancelamento não encontrado")
    if pedido.status == "tratado":
        raise HTTPException(status_code=400, detail="Esse pedido já foi tratado")

    pedido.status = "tratado"
    pedido.observacao_humano = corpo.observacao
    pedido.tratado_em = datetime.utcnow()
    db.commit()

    return {"status": "tratado", "id": pedido.id}
