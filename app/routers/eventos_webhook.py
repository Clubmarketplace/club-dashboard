"""
Endpoint só de leitura pra consultar o log de tudo que chegou no
webhook do Mercado Livre -- pra diagnosticar sem precisar abrir o
Railway.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import EventoWebhook

router = APIRouter(prefix="/api/eventos-webhook", tags=["eventos-webhook"])


@router.get("/lista")
def listar(db: Session = Depends(get_db), limite: int = 100):
    eventos = (
        db.query(EventoWebhook)
        .order_by(EventoWebhook.recebido_em.desc())
        .limit(limite)
        .all()
    )
    return [
        {
            "id": e.id,
            "topico": e.topico,
            "payload_bruto": e.payload_bruto,
            "resultado": e.resultado,
            "recebido_em": e.recebido_em.isoformat() if e.recebido_em else None,
        }
        for e in eventos
    ]
