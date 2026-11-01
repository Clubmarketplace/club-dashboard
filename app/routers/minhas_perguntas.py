"""
Painel de perguntas de pré-venda DO SELLER (31/10) -- o mesmo painel da TV, só com a conta dele.

Mostra quem respondeu cada pergunta (IA do ML / app, nossa IA, equipe ou o próprio seller, ainda pendente) e deixa o
seller responder as que caíram na fila humana (as que nem a IA do ML nem a nossa resolveram).

Segurança:
  * A conta vem SEMPRE do usuário logado (conta_vinculada), nunca de parâmetro: o seller só enxerga e responde as
    perguntas da conta dele. Pergunta de outra conta = 404.
  * Reaproveita os cálculos do painel da TV (painel_resumo / painel_detalhe), então os números sempre batem com ele;
    aqui só se REMOVE o que é da equipe (lista de todas as contas, ranking de produtos, revisão da IA).
  * Só o papel "seller" responde; o "suporte" (que navega pelo painel do seller) só consulta.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import auth
from app.contas_util import chave_conta
from app.database import get_db
from app.models import Conta
from app.routers import pre_venda

router = APIRouter(prefix="/api/minhas-perguntas", tags=["minhas-perguntas"])

PAPEIS_VEEM = ("seller", "suporte")
PERIODOS = ("hoje", "7", "30")
ESCOPOS_PERMITIDOS = ("hoje", "periodo", "hora", "dia", "pendentes_agora")   # sem "produto"/"revisar" (são da equipe)


def _usuario_e_conta(request: Request, db: Session):
    """(usuario, Conta do ML ligada ao seller ou None se ainda não houver conta conectada)."""
    usuario = auth.usuario_atual(request, db)
    if usuario is None:
        raise HTTPException(status_code=401, detail="Sessão expirada -- faça login de novo.")
    if usuario.papel not in PAPEIS_VEEM or not usuario.conta_vinculada:
        raise HTTPException(status_code=403, detail="Esta tela é só para o seller, da conta vinculada a ele.")
    alvo = chave_conta(usuario.conta_vinculada)
    conta = next((c for c in db.query(Conta).all() if chave_conta(c.apelido or "") == alvo), None)
    return usuario, conta


def _periodo(periodo: str | None) -> str:
    return periodo if periodo in PERIODOS else "hoje"


@router.get("/resumo")
def resumo(request: Request, periodo: str = "hoje", db: Session = Depends(get_db)):
    """Números do painel (período Hoje / 7 / 30 dias) só da conta do seller."""
    usuario, conta = _usuario_e_conta(request, db)
    vazio = {"conectada": False, "pode_responder": usuario.papel == "seller"}
    if conta is None:
        return vazio
    d = pre_venda.painel_resumo(db=db, conta=conta.apelido, periodo=_periodo(periodo))
    ind = d["indicadores"]
    return {
        "conectada": True,
        "pode_responder": usuario.papel == "seller",
        "rotulo": d["filtro"]["rotulo"],
        "periodo": d["filtro"]["periodo"],
        "hora_atual": d["hora_atual"],
        "indicadores": {
            "sem_equipe_pct": ind["sem_equipe_pct"],
            "sem_equipe_ml_pct": ind["sem_equipe_ml_pct"],
            "sem_equipe_ia_pct": ind["sem_equipe_ia_pct"],
            "pendentes_agora": ind["pendentes_agora"],
            "perguntas_periodo": ind["perguntas_periodo"],
            "perguntas_hoje": ind["perguntas_hoje"],
            "tempo_medio_equipe_min": ind["tempo_medio_equipe_min"],
        },
        "periodo_desfecho": d["periodo_desfecho"],
        "por_hora_periodo": d["por_hora_periodo"],
        "por_dia": d["por_dia"],
    }


@router.get("/detalhe")
def detalhe(request: Request, escopo: str = "periodo", grupo: str | None = None, hora: int | None = None,
            dia: str | None = None, periodo: str = "hoje", db: Session = Depends(get_db)):
    """Perguntas por trás de um pedaço do painel (uma cor, uma hora, ou as pendentes), só da conta do seller."""
    _usuario, conta = _usuario_e_conta(request, db)
    if conta is None:
        return {"titulo": "", "total": 0, "mostrando": 0, "perguntas": []}
    if escopo not in ESCOPOS_PERMITIDOS:
        raise HTTPException(status_code=400, detail="Escopo inválido.")
    d = pre_venda.painel_detalhe(db=db, escopo=escopo, grupo=grupo, hora=hora, dia=dia, faixa="periodo", conta=conta.apelido,
                                 periodo=_periodo(periodo))
    perguntas = [p for bloco in d["contas"] for p in bloco["perguntas"]]
    for p in perguntas:   # nada da equipe: revisão da IA e o nome da conta (já é a dele)
        for campo in ("revisar", "revisao", "revisado_por", "resposta_corrigida", "conta"):
            p.pop(campo, None)
    return {"titulo": d["titulo"], "grupo": d["grupo"], "total": d["total"], "mostrando": d["mostrando"], "perguntas": perguntas}


class RespostaBody(BaseModel):
    texto: str


@router.post("/{pergunta_id}/responder")
def responder(pergunta_id: int, corpo: RespostaBody, request: Request, db: Session = Depends(get_db)):
    """O seller responde uma pergunta da fila humana da conta dele (vai direto para o comprador no ML)."""
    usuario, conta = _usuario_e_conta(request, db)
    if usuario.papel != "seller":
        raise HTTPException(status_code=403, detail="Só o próprio seller responde.")
    if conta is None:
        raise HTTPException(status_code=404, detail="Pergunta não encontrada")
    return pre_venda.enviar_resposta_manual(db, pergunta_id, corpo.texto, usuario, conta_id_exigida=conta.id)
