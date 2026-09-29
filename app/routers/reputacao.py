"""
API da tela "Reputação das contas" (termômetro de todas as contas).

  GET  /api/reputacao/contas     -> todas as contas ativas + última leitura
  POST /api/reputacao/atualizar  -> "Atualizar agora" (admin/supervisor)
  GET  /api/reputacao/status     -> progresso da atualização em andamento

  Atendimento das contas em atenção (mesmo padrão das Solicitações sellers;
  regras em app/reputacao_atendimento.py):
  GET  /api/reputacao/atendimento/{conta_id}           -> histórico, tempos e anotações
  POST /api/reputacao/atendimento/{conta_id}/assumir   -> {forcar: bool}
  POST /api/reputacao/atendimento/{conta_id}/liberar
  POST /api/reputacao/atendimento/{conta_id}/anotar    -> {texto}
  POST /api/reputacao/atendimento/{conta_id}/concluir  -> {texto, protocolo?}

Ver e atender: admin, supervisor e atendente. Disparar leitura: admin e supervisor.
O quadro "Equipe agora" (quem está com o quê) vai só pra admin e supervisor.
A leitura automática roda sozinha (app/reputacao.py -> loop_reputacao).
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import auth, reputacao
from app import reputacao_atendimento as atd
from app.contas_util import chave_conta
from app.database import get_db
from app.models import AtendimentoReputacao, Conta, ReputacaoConta, SolicitacaoCancelamento, Usuario

router = APIRouter(prefix="/api/reputacao", tags=["reputação"])

_MEDALHAS = {"silver": "MercadoLíder", "gold": "MercadoLíder Gold", "platinum": "MercadoLíder Platinum"}
PAPEIS_ATENDEM = ("admin", "supervisor", "atendente")
PAPEIS_SUPERVISAO = ("admin", "supervisor")
FUSO_BR = timezone(timedelta(hours=-3))


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
    usuario = _exigir(request, db, PAPEIS_ATENDEM)

    contas = db.query(Conta).filter(Conta.inativa_em.is_(None)).order_by(Conta.apelido).all()
    leituras = {r.conta_id: r for r in db.query(ReputacaoConta).all()}
    resumos = {c.id: reputacao.classificar(leituras.get(c.id)) for c in contas}

    # Fila de atendimento: abre/reabre/encerra conforme o termômetro de agora.
    atd.sincronizar(db, resumos, reputacao.FAIXA_ATENCAO)
    atendimentos = atd.atual_por_conta(db)
    solicitacoes_pendentes = _solicitacoes_reputacao_pendentes(db)

    itens = []
    ultima = None
    for c in contas:
        r = leituras.get(c.id)
        resumo = resumos[c.id]
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
            "atendimento": atd.resumo_do_cartao(atendimentos.get(c.id)),
            "solicitacoes_reputacao": solicitacoes_pendentes.get(chave_conta(c.apelido or ""), 0),
        })

    supervisao = auth.papel_permite(usuario, PAPEIS_SUPERVISAO)
    return {
        "atualizado_em": _iso_utc(ultima),
        "limites": [{"chave": k, "nome": n, "curto": cu, "limite": l} for k, _, n, cu, l in reputacao.INDICADORES],
        "faixa_atencao": reputacao.FAIXA_ATENCAO,
        "pode_atualizar": auth.papel_permite(usuario, ("admin", "supervisor")),
        "progresso": reputacao.progresso(),
        "contas": itens,
        "eu": {"id": usuario.id, "nome": usuario.nome_exibicao, "papel": usuario.papel},
        "horas_parada": atd.HORAS_PARADA,
        "dias_reabrir": atd.DIAS_REABRIR,
        # Quadro "Equipe agora": só admin e supervisor recebem (o operador vê só os cartões).
        "equipe": _quadro_equipe(db, itens) if supervisao else None,
    }


# ---------------------------------------------------------------------------
# Apoio da listagem
# ---------------------------------------------------------------------------
def _solicitacoes_reputacao_pendentes(db: Session) -> dict[str, int]:
    """{chave da conta: nº de solicitações de REPUTAÇÃO pendentes feitas pelo seller}."""
    contagem: dict[str, int] = {}
    linhas = (db.query(SolicitacaoCancelamento.conta_chave, SolicitacaoCancelamento.conta)
              .filter(SolicitacaoCancelamento.tipo == "reputacao", SolicitacaoCancelamento.confirmado_por.is_(None)).all())
    for chave, nome in linhas:
        k = chave or chave_conta(nome or "")
        contagem[k] = contagem.get(k, 0) + 1
    return contagem


def _inicio_de_hoje_utc() -> datetime:
    agora_br = datetime.now(FUSO_BR)
    return agora_br.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).replace(tzinfo=None)


def _quadro_equipe(db: Session, itens: list[dict]) -> dict:
    """
    "Equipe agora" da supervisão: números do topo + uma linha por operador com
    o que está com ele na Reputação E nas Solicitações sellers (as duas telas).
    """
    agora = datetime.utcnow()
    limite_parada = agora - timedelta(hours=atd.HORAS_PARADA)
    hoje = _inicio_de_hoje_utc()
    nome_conta = {i["id"]: i["nome"] for i in itens}

    na_fila = [i for i in itens if i["atendimento"] and i["atendimento"]["status"] == "aberto"]
    livres = [i for i in na_fila if not i["atendimento"]["em_atendimento_por_id"]]
    em_atd = [i for i in na_fila if i["atendimento"]["em_atendimento_por_id"]]
    concluidos_hoje = (db.query(AtendimentoReputacao)
                       .filter(AtendimentoReputacao.status == "concluido", AtendimentoReputacao.concluido_em >= hoje).all())

    # Solicitações sellers pendentes que estão com alguém (e as confirmadas hoje).
    solic_com = (db.query(SolicitacaoCancelamento)
                 .filter(SolicitacaoCancelamento.confirmado_por.is_(None),
                         SolicitacaoCancelamento.em_atendimento_por_id.isnot(None)).all())
    solic_hoje = (db.query(SolicitacaoCancelamento.confirmado_por)
                  .filter(SolicitacaoCancelamento.confirmado_em >= hoje).all())

    operadores = {}
    for u in db.query(Usuario).filter(Usuario.ativo.is_(True), Usuario.papel.in_(PAPEIS_ATENDEM)).all():
        operadores[u.id] = {"id": u.id, "nome": u.nome_exibicao, "papel": u.papel, "contas": [],
                            "solicitacoes": 0, "solicitacoes_reputacao": 0, "mais_antiga": None,
                            "tratadas_hoje_contas": 0, "tratadas_hoje_solicitacoes": 0}
    por_nome = {o["nome"]: o for o in operadores.values()}

    def anotar_mais_antiga(op, desde, descricao):
        if desde and (op["mais_antiga"] is None or desde < op["_desde"]):
            op["_desde"] = desde
            op["mais_antiga"] = {"desde": atd._iso(desde), "descricao": descricao, "parada": desde < limite_parada}

    for i in em_atd:
        a = i["atendimento"]
        op = operadores.get(a["em_atendimento_por_id"])
        if op is None:
            continue
        op["contas"].append(i["nome"])
        anotar_mais_antiga(op, datetime.fromisoformat(a["em_atendimento_desde"].rstrip("Z")), i["nome"])
    for s in solic_com:
        op = operadores.get(s.em_atendimento_por_id)
        if op is None:
            continue
        op["solicitacoes"] += 1
        if s.tipo == "reputacao":
            op["solicitacoes_reputacao"] += 1
        anotar_mais_antiga(op, s.em_atendimento_desde, f"venda {s.numero_venda}")
    for a in concluidos_hoje:
        op = operadores.get(a.concluido_por_id)
        if op:
            op["tratadas_hoje_contas"] += 1
    for (nome,) in solic_hoje:
        op = por_nome.get(nome)
        if op:
            op["tratadas_hoje_solicitacoes"] += 1

    lista = []
    for op in operadores.values():
        op.pop("_desde", None)
        ocupado = op["contas"] or op["solicitacoes"]
        # Admin sem nada na mão não aparece (é quem administra, não quem atende).
        if op["papel"] == "admin" and not ocupado and not (op["tratadas_hoje_contas"] or op["tratadas_hoje_solicitacoes"]):
            continue
        lista.append(op)
    lista.sort(key=lambda o: (-(len(o["contas"]) + o["solicitacoes"]), o["mais_antiga"]["desde"] if o["mais_antiga"] else "~", o["nome"]))

    return {
        "precisam": len([i for i in itens if i["situacao"] in atd.SITUACOES_NA_FILA]),
        "livres": len(livres),
        "em_atendimento": len(em_atd),
        "paradas": len([i for i in em_atd if i["atendimento"]["parada"]]),
        "tratadas_hoje": len(concluidos_hoje),
        "operadores": lista,
    }


# ---------------------------------------------------------------------------
# Atendimento (assumir / liberar / anotar / concluir)
# ---------------------------------------------------------------------------
class AssumirBody(BaseModel):
    forcar: bool = False  # a tela só manda True depois de perguntar "Assumir no lugar de Fulano?"


class TextoBody(BaseModel):
    texto: str
    protocolo: Optional[str] = None


def _executar(acao):
    """Traduz os erros de regra (ErroAtendimento) em respostas HTTP."""
    try:
        return acao()
    except atd.ErroAtendimento as erro:
        detalhe = {"mensagem": erro.mensagem, **erro.dados} if erro.dados else erro.mensagem
        raise HTTPException(status_code=erro.status, detail=detalhe)


def _conta_ativa(db: Session, conta_id: int) -> Conta:
    conta = db.query(Conta).filter(Conta.id == conta_id, Conta.inativa_em.is_(None)).first()
    if conta is None:
        raise HTTPException(status_code=404, detail="Conta não encontrada.")
    return conta


@router.get("/atendimento/{conta_id}")
def ver_atendimento(conta_id: int, request: Request, db: Session = Depends(get_db)):
    _exigir(request, db, PAPEIS_ATENDEM)
    _conta_ativa(db, conta_id)
    return {"atendimento": atd.detalhe(db, conta_id)}


@router.post("/atendimento/{conta_id}/assumir")
def assumir(conta_id: int, request: Request, corpo: Optional[AssumirBody] = None, db: Session = Depends(get_db)):
    usuario = _exigir(request, db, PAPEIS_ATENDEM)
    _conta_ativa(db, conta_id)
    _executar(lambda: atd.assumir(db, conta_id, usuario, bool(corpo and corpo.forcar)))
    return {"status": "assumido"}


@router.post("/atendimento/{conta_id}/liberar")
def liberar(conta_id: int, request: Request, db: Session = Depends(get_db)):
    usuario = _exigir(request, db, PAPEIS_ATENDEM)
    _conta_ativa(db, conta_id)
    _executar(lambda: atd.liberar(db, conta_id, usuario))
    return {"status": "liberado"}


@router.post("/atendimento/{conta_id}/anotar")
def anotar(conta_id: int, corpo: TextoBody, request: Request, db: Session = Depends(get_db)):
    usuario = _exigir(request, db, PAPEIS_ATENDEM)
    _conta_ativa(db, conta_id)
    _executar(lambda: atd.anotar(db, conta_id, usuario, corpo.texto))
    return {"status": "anotado"}


@router.post("/atendimento/{conta_id}/concluir")
def concluir(conta_id: int, corpo: TextoBody, request: Request, db: Session = Depends(get_db)):
    usuario = _exigir(request, db, PAPEIS_ATENDEM)
    _conta_ativa(db, conta_id)
    leitura = db.query(ReputacaoConta).filter(ReputacaoConta.conta_id == conta_id).first()
    situacao = reputacao.classificar(leitura)["situacao"]
    _executar(lambda: atd.concluir(db, conta_id, usuario, corpo.texto, corpo.protocolo, situacao))
    return {"status": "concluido"}


@router.post("/atualizar")
def atualizar_agora(request: Request, db: Session = Depends(get_db)):
    _exigir(request, db, ("admin", "supervisor"))
    iniciou = reputacao.atualizar_em_segundo_plano()
    return {"iniciou": iniciou, "progresso": reputacao.progresso()}


@router.get("/status")
def status(request: Request, db: Session = Depends(get_db)):
    _exigir(request, db, ("admin", "supervisor", "atendente"))
    return reputacao.progresso()
