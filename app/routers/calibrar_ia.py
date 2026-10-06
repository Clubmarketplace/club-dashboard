"""
API da tela "Administração › Calibrar IA" (só admin e supervisor).

  GET /api/calibrar-ia   -> ajustes em vigor, opções da tela e histórico recente
  PUT /api/calibrar-ia   -> salva os ajustes alterados (grava histórico)

Os ajustes valem em até 30 segundos (cache em app/config_ia.py).
"""
import json

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app import auth, config_ia, saude_ia
from app.database import get_db
from app.models import HistoricoConfigIA

router = APIRouter(prefix="/api/calibrar-ia", tags=["calibrar-ia"])

PAPEIS = ("admin", "supervisor")


def _exigir(request: Request, db: Session):
    usuario = auth.usuario_atual(request, db)
    if not auth.papel_permite(usuario, PAPEIS):
        raise HTTPException(status_code=403, detail="Só admin e supervisor calibram a IA.")
    return usuario


def _historico(db: Session, limite: int = 40) -> list[dict]:
    linhas = db.query(HistoricoConfigIA).order_by(HistoricoConfigIA.alterado_em.desc()).limit(limite).all()
    saida = []
    for h in linhas:
        def ler(v):
            try:
                return json.loads(v) if v is not None else None
            except ValueError:
                return v
        saida.append({
            "chave": h.chave, "antes": ler(h.valor_antigo), "depois": ler(h.valor_novo),
            "por": h.alterado_por, "em": (h.alterado_em.isoformat() + "Z") if h.alterado_em else None,
        })
    return saida


def _saude() -> dict | None:
    falha = saude_ia.falha_atual()
    if not falha:
        return None
    return {"desde": falha["desde"].isoformat() + "Z", "motivo": falha["motivo"]}


def _resposta(db: Session) -> dict:
    return {
        "config": config_ia.obter(forcar=True),
        "padroes": config_ia.padroes(),
        "degraus": [{"chave": k, "nome": n} for k, n in config_ia.DEGRAUS],
        "historico": _historico(db),
        "saude": _saude(),
    }


@router.get("")
def ler(request: Request, db: Session = Depends(get_db)):
    _exigir(request, db)
    return _resposta(db)


@router.put("")
def salvar(request: Request, dados: dict = Body(...), db: Session = Depends(get_db)):
    usuario = _exigir(request, db)
    try:
        config_ia.salvar(db, dados, getattr(usuario, "nome_exibicao", None))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _resposta(db)
