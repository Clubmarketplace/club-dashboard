"""
API da tela "Reputação das contas" (termômetro de todas as contas).

  GET  /api/reputacao/contas     -> todas as contas ativas + última leitura
  POST /api/reputacao/atualizar  -> "Atualizar agora" (admin/supervisor)
  GET  /api/reputacao/status     -> progresso da atualização em andamento

Ver: admin, supervisor e atendente. Disparar leitura: admin e supervisor.
A leitura automática roda sozinha (app/reputacao.py -> loop_reputacao).
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app import auth, reputacao
from app.database import get_db
from app.models import Conta, ReputacaoConta

router = APIRouter(prefix="/api/reputacao", tags=["reputação"])

_MEDALHAS = {"silver": "MercadoLíder", "gold": "MercadoLíder Gold", "platinum": "MercadoLíder Platinum"}


def _exigir(request: Request, db: Session, papeis: tuple):
    usuario = auth.usuario_atual(request, db)
    if not auth.papel_permite(usuario, papeis):
        raise HTTPException(status_code=403, detail="Sem permissão para esta ação.")
    return usuario


def _iso_utc(dt):
    # Datas gravadas em UTC sem fuso: marca com "Z" pra tela mostrar no horário de Brasília.
    return dt.isoformat() + "Z" if dt else None


@router.get("/contas")
def listar(request: Request, db: Session = Depends(get_db)):
    usuario = _exigir(request, db, ("admin", "supervisor", "atendente"))

    contas = db.query(Conta).filter(Conta.inativa_em.is_(None)).order_by(Conta.apelido).all()
    leituras = {r.conta_id: r for r in db.query(ReputacaoConta).all()}

    itens = []
    ultima = None
    for c in contas:
        r = leituras.get(c.id)
        resumo = reputacao.classificar(r)
        if r and r.lido_em and (ultima is None or r.lido_em > ultima):
            ultima = r.lido_em
        itens.append({
            "id": c.id,
            "nome": c.apelido,
            "conectada": bool(c.access_token),
            "situacao": resumo["situacao"],
            "nivel": resumo["nivel"],
            "metricas": resumo["metricas"],
            "medalha": _MEDALHAS.get((r.power_seller_status or "").lower()) if r else None,
            "vendas": r.vendas if r else None,
            "periodo": r.periodo if r else None,
            "lido_em": _iso_utc(r.lido_em) if r else None,
            "erro": r.erro if r else None,
        })

    return {
        "atualizado_em": _iso_utc(ultima),
        "limites": [{"chave": k, "nome": n, "curto": cu, "limite": l} for k, _, n, cu, l in reputacao.INDICADORES],
        "faixa_atencao": reputacao.FAIXA_ATENCAO,
        "pode_atualizar": auth.papel_permite(usuario, ("admin", "supervisor")),
        "progresso": reputacao.progresso(),
        "contas": itens,
    }


@router.post("/atualizar")
def atualizar_agora(request: Request, db: Session = Depends(get_db)):
    _exigir(request, db, ("admin", "supervisor"))
    iniciou = reputacao.atualizar_em_segundo_plano()
    return {"iniciou": iniciou, "progresso": reputacao.progresso()}


@router.get("/status")
def status(request: Request, db: Session = Depends(get_db)):
    _exigir(request, db, ("admin", "supervisor", "atendente"))
    return reputacao.progresso()
