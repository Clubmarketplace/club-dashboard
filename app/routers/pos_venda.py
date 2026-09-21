"""
Endpoints da fila de pós-venda (mensagens comuns + reclamações). A IA
só sugere (resposta_sugerida) -- quem decide e envia é sempre um
atendente humano, por isso não existe "responder automático" aqui
como existe na pré-venda.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import MensagemPosVenda, Conta
from app.ml_client import MLAuthError, MLApiError, garantir_token_valido, enviar_mensagem_pos_venda, enviar_resposta_claim

router = APIRouter(prefix="/api/pos-venda", tags=["pós-venda"])


class RespostaManual(BaseModel):
    texto: str


@router.get("/fila")
def listar_fila(db: Session = Depends(get_db)):
    """Mensagens/reclamações esperando um atendente, mais recentes primeiro."""
    itens = (
        db.query(MensagemPosVenda)
        .filter(MensagemPosVenda.status == "fila_humana")
        .order_by(MensagemPosVenda.recebida_em.desc())
        .all()
    )
    return [
        {
            "id": i.id,
            "conta": i.conta.apelido if i.conta else "—",
            "tipo": i.tipo,
            "sku": i.sku,
            "order_id": i.order_id,
            "texto": i.texto,
            "resposta_sugerida": i.resposta_sugerida,
            "recebida_em": i.recebida_em.isoformat() if i.recebida_em else None,
        }
        for i in itens
    ]


@router.get("/resolvidas")
def listar_resolvidas(db: Session = Depends(get_db), limite: int = 50):
    """Últimas mensagens/reclamações já respondidas por um atendente."""
    itens = (
        db.query(MensagemPosVenda)
        .filter(MensagemPosVenda.status == "respondida")
        .order_by(MensagemPosVenda.respondida_em.desc())
        .limit(limite)
        .all()
    )
    return [
        {
            "id": i.id,
            "conta": i.conta.apelido if i.conta else "—",
            "tipo": i.tipo,
            "sku": i.sku,
            "texto": i.texto,
            "resposta_enviada": i.resposta_enviada,
            "respondida_em": i.respondida_em.isoformat() if i.respondida_em else None,
        }
        for i in itens
    ]


@router.post("/{item_id}/responder")
def responder_manualmente(item_id: int, corpo: RespostaManual, db: Session = Depends(get_db)):
    """Envia a resposta digitada por um humano (a sugestão da IA é só um ponto de partida, editável)."""
    item = db.query(MensagemPosVenda).filter(MensagemPosVenda.id == item_id).first()
    if item is None:
        raise HTTPException(status_code=404, detail="Mensagem não encontrada")
    if item.status != "fila_humana":
        raise HTTPException(status_code=400, detail="Essa mensagem já foi tratada")

    conta = db.query(Conta).filter(Conta.id == item.conta_id).first()
    if conta is None:
        raise HTTPException(status_code=404, detail="Conta não encontrada")

    try:
        access_token = garantir_token_valido(conta, db)
        if item.tipo == "reclamacao":
            enviar_resposta_claim(access_token, item.ml_recurso_id, corpo.texto)
        else:
            if not item.pack_id:
                raise HTTPException(status_code=400, detail="Essa mensagem não tem pack_id registrado -- verificar manualmente pelo Mercado Livre.")
            enviar_mensagem_pos_venda(access_token, item.pack_id, conta.ml_user_id, corpo.texto)
    except (MLAuthError, MLApiError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    item.status = "respondida"
    item.resposta_enviada = corpo.texto
    item.respondida_em = datetime.utcnow()
    db.commit()

    return {"status": "respondida", "id": item.id}
