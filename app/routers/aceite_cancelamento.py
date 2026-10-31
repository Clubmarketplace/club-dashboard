"""
Aceite do seller (31/10) -- cancelamento pedido pelo galpão, logística ou equipe (supervisor/admin).

Regra: o pedido nasce "aguardando" (ver solicitacoes_cancelamento.precisa_aceite). O operador NÃO pode assumir
nem confirmar enquanto o seller não ACEITAR; se o seller RECUSAR (com motivo), o pedido fica encerrado como
"recusado" e o galpão vê o motivo. Pedido do próprio seller não passa por aqui.

Segurança:
  * A conta vem SEMPRE do usuário logado (nunca de parâmetro): um seller só enxerga/decide os pedidos da sua conta.
  * Só o papel "seller" aceita ou recusa. O "suporte" (que navega pelo painel do seller) só consulta.
  * A troca de estado é um UPDATE condicional (WHERE aceite_status = 'aguardando'): clique duplo ou duas abas não
    decidem duas vezes, e o que já foi decidido não muda mais.
  * Tudo vai para o histórico do pedido (quem, quando, motivo).
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import update
from sqlalchemy.orm import Session

from app import auth
from app.cancelamento_apoio import registrar_evento
from app.contas_util import chave_conta
from app.database import get_db
from app.models import SolicitacaoCancelamento
from app.routers.solicitacoes_cancelamento import (
    ACEITE_ACEITO, ACEITE_AGUARDANDO, ACEITE_RECUSADO, GALPOES, PLATAFORMAS,
)

router = APIRouter(prefix="/api/meus-aceites", tags=["aceite-cancelamento"])

PAPEIS_VEEM = ("seller", "suporte")
MAX_MOTIVO = 300
MAX_LISTA = 200


def _usuario_da_conta(request: Request, db: Session):
    usuario = auth.usuario_atual(request, db)
    if usuario is None:
        raise HTTPException(status_code=401, detail="Sessão expirada -- faça login de novo.")
    if usuario.papel not in PAPEIS_VEEM or not usuario.conta_vinculada:
        raise HTTPException(status_code=403, detail="Esta tela é só para o seller, da conta vinculada a ele.")
    return usuario


def _linha(s: SolicitacaoCancelamento) -> dict:
    origem = {"logistica": GALPOES.get(s.galpao, "Galpão") if s.galpao else "Galpão", "equipe": "Equipe"}.get(s.origem, s.origem or "—")
    return {
        "id": s.id,
        "numero_venda": s.numero_venda,
        "plataforma": s.plataforma,
        "plataforma_nome": PLATAFORMAS.get(s.plataforma, s.plataforma),
        "motivo": s.motivo,
        "sku": s.sku,
        "produto": s.produto_titulo,
        "pedido_por": s.solicitado_por,
        "origem": origem,
        "criado_em": s.criado_em.isoformat() if s.criado_em else None,
        "horas_esperando": round((datetime.utcnow() - s.criado_em).total_seconds() / 3600, 1) if s.criado_em else None,
        "status": s.aceite_status,
        "decidido_por": s.aceite_por,
        "decidido_papel": s.aceite_papel,
        "decidido_em": s.aceite_em.isoformat() if s.aceite_em else None,
        "motivo_recusa": s.aceite_motivo,
    }


def _da_conta(db: Session, usuario):
    return (db.query(SolicitacaoCancelamento)
            .filter(SolicitacaoCancelamento.conta_chave == chave_conta(usuario.conta_vinculada))
            .filter(SolicitacaoCancelamento.excluido_em.is_(None)))


@router.get("/resumo")
def resumo(request: Request, db: Session = Depends(get_db)):
    """Só o número (alimenta a bolinha do menu e a faixa do painel)."""
    usuario = _usuario_da_conta(request, db)
    q = _da_conta(db, usuario).filter(SolicitacaoCancelamento.aceite_status == ACEITE_AGUARDANDO,
                                      SolicitacaoCancelamento.confirmado_por.is_(None))
    n = q.count()
    antigo = q.order_by(SolicitacaoCancelamento.criado_em.asc()).first()
    horas = round((datetime.utcnow() - antigo.criado_em).total_seconds() / 3600, 1) if antigo and antigo.criado_em else None
    return {"aguardando": n, "mais_antigo_horas": horas, "pode_decidir": usuario.papel == "seller"}


@router.get("")
def listar(request: Request, db: Session = Depends(get_db)):
    """Aguardando (todos) + os últimos já decididos (aceitos/recusados) da conta."""
    usuario = _usuario_da_conta(request, db)
    base = _da_conta(db, usuario)
    aguardando = (base.filter(SolicitacaoCancelamento.aceite_status == ACEITE_AGUARDANDO)
                  .order_by(SolicitacaoCancelamento.criado_em.asc()).limit(MAX_LISTA).all())
    decididos = (base.filter(SolicitacaoCancelamento.aceite_status.in_([ACEITE_ACEITO, ACEITE_RECUSADO]))
                 .order_by(SolicitacaoCancelamento.aceite_em.desc()).limit(30).all())
    return {"aguardando": [_linha(s) for s in aguardando], "decididos": [_linha(s) for s in decididos],
            "pode_decidir": usuario.papel == "seller"}


class RecusaBody(BaseModel):
    motivo: str


def _decidir(db: Session, request: Request, sid: int, novo: str, motivo: Optional[str]) -> dict:
    usuario = _usuario_da_conta(request, db)
    if usuario.papel != "seller":
        raise HTTPException(status_code=403, detail="Só o próprio seller aceita ou recusa um cancelamento.")
    s = _da_conta(db, usuario).filter(SolicitacaoCancelamento.id == sid).first()   # outra conta = 404
    if s is None:
        raise HTTPException(status_code=404, detail="Solicitação não encontrada.")
    if s.aceite_status != ACEITE_AGUARDANDO:
        raise HTTPException(status_code=409, detail="Este pedido não está aguardando o seu aceite (já foi decidido).")
    if s.confirmado_por:
        raise HTTPException(status_code=409, detail="Este pedido já foi concluído.")
    agora = datetime.utcnow()
    r = db.execute(
        update(SolicitacaoCancelamento)
        .where(SolicitacaoCancelamento.id == sid, SolicitacaoCancelamento.aceite_status == ACEITE_AGUARDANDO,
               SolicitacaoCancelamento.confirmado_por.is_(None))
        .values(aceite_status=novo, aceite_por=usuario.nome_exibicao, aceite_por_id=usuario.id, aceite_papel="seller", aceite_em=agora,
                aceite_motivo=motivo)
        .execution_options(synchronize_session=False))
    if r.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail="Este pedido acabou de ser decidido por outra tela. Atualize a página.")
    registrar_evento(db, sid, "aceitou" if novo == ACEITE_ACEITO else "recusou", usuario=usuario,
                     detalhe=("Seller aceitou o cancelamento" if novo == ACEITE_ACEITO else f"Seller recusou o cancelamento: {motivo}"))
    db.commit()
    db.refresh(s)
    return _linha(s)


@router.post("/{solicitacao_id}/aceitar")
def aceitar(solicitacao_id: int, request: Request, db: Session = Depends(get_db)):
    return _decidir(db, request, solicitacao_id, ACEITE_ACEITO, None)


@router.post("/{solicitacao_id}/recusar")
def recusar(solicitacao_id: int, corpo: RecusaBody, request: Request, db: Session = Depends(get_db)):
    motivo = " ".join((corpo.motivo or "").split())
    if len(motivo) < 3:
        raise HTTPException(status_code=422, detail="Escreva o motivo da recusa (ex.: já despachei).")
    return _decidir(db, request, solicitacao_id, ACEITE_RECUSADO, motivo[:MAX_MOTIVO])
