"""
Endpoints da tela de Pré-venda: listar a fila (pendente/fila_humana),
e permitir que um humano responda manualmente uma pergunta que caiu
na fila.
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Pergunta, Conta, AcaoRegistrada, RespostaValidadaSku
from app.ml_client import MLAuthError, MLApiError, garantir_token_valido, buscar_pergunta, enviar_resposta

router = APIRouter(prefix="/api/pre-venda", tags=["pré-venda"])

# Fuso usado nas estatísticas "de hoje" e "por hora" do painel de TV.
# O banco guarda tudo em UTC (datetime.utcnow); só a apresentação usa o
# horário de Brasília. Se a base de fusos (tzdata) não estiver disponível
# no servidor, cai pra UTC-3 fixo -- o Brasil não tem horário de verão
# desde 2019, então o resultado é o mesmo.
try:
    from zoneinfo import ZoneInfo
    FUSO_BR = ZoneInfo("America/Sao_Paulo")
except Exception:  # ZoneInfoNotFoundError ou Python sem zoneinfo
    FUSO_BR = timezone(timedelta(hours=-3), "BRT")


def _resolvida_por(p: Pergunta) -> str:
    """Rótulo curto de quem resolveu a pergunta, pros painéis de TV."""
    if p.camada_resolvida in CAMADAS_AUTOMATICAS:
        return "IA"
    if p.camada_resolvida == "manual":
        return "Atendente"
    if p.status == "respondida_externamente":
        return "Por fora (ML/app)"
    return "—"


def _utc_para_br(data_utc_naive: datetime) -> datetime:
    """Converte um datetime UTC 'cru' (sem fuso, como vem do banco) pro horário de Brasília."""
    return data_utc_naive.replace(tzinfo=timezone.utc).astimezone(FUSO_BR)

# Camadas que respondem sozinhas (sem humano) — usado só pra classificar
# estatística no painel de TV; não muda a lógica de decisão em si (essa
# continua em pre_venda_logica.py).
CAMADAS_AUTOMATICAS = {"resposta_validada", "manual_sku_ia", "politica_geral"}


class RespostaManual(BaseModel):
    texto: str


@router.get("/fila")
def listar_fila(db: Session = Depends(get_db)):
    """Lista as perguntas que precisam de atenção humana, mais recentes primeiro."""
    perguntas = (
        db.query(Pergunta)
        .filter(Pergunta.status == "fila_humana")
        .order_by(Pergunta.recebida_em.desc())
        .all()
    )
    return [
        {
            "id": p.id,
            "conta": p.conta.apelido if p.conta else "—",
            "item_id": p.item_id,
            "sku": p.sku,
            "texto": p.texto,
            "recebida_em": p.recebida_em.isoformat() if p.recebida_em else None,
        }
        for p in perguntas
    ]


@router.get("/resolvidas")
def listar_resolvidas(db: Session = Depends(get_db), limite: int = 50):
    """
    Lista as últimas perguntas já resolvidas -- por camada automática,
    por atendente (manual), ou respondidas por fora (assistente nativo
    do Mercado Livre ou o próprio vendedor, antes da gente processar).
    """
    perguntas = (
        db.query(Pergunta)
        .filter(Pergunta.status.in_(["respondida", "respondida_externamente"]))
        .order_by(Pergunta.respondida_em.desc())
        .limit(limite)
        .all()
    )
    return [
        {
            "id": p.id,
            "conta": p.conta.apelido if p.conta else "—",
            "sku": p.sku,
            "texto": p.texto,
            "resposta_enviada": p.resposta_enviada,
            "camada_resolvida": p.camada_resolvida,
            "precisa_auditoria": p.precisa_auditoria,
            "respondida_em": p.respondida_em.isoformat() if p.respondida_em else None,
        }
        for p in perguntas
    ]


@router.post("/{pergunta_id}/responder")
def responder_manualmente(pergunta_id: int, corpo: RespostaManual, db: Session = Depends(get_db)):
    """
    Envia a resposta digitada por um humano pro Mercado Livre, e marca
    a pergunta como resolvida (sem camada automática associada).
    """
    pergunta = db.query(Pergunta).filter(Pergunta.id == pergunta_id).first()
    if pergunta is None:
        raise HTTPException(status_code=404, detail="Pergunta não encontrada")
    if pergunta.status != "fila_humana":
        raise HTTPException(status_code=400, detail="Essa pergunta já foi respondida")

    conta = db.query(Conta).filter(Conta.id == pergunta.conta_id).first()
    if conta is None:
        raise HTTPException(status_code=404, detail="Conta da pergunta não encontrada")

    try:
        access_token = garantir_token_valido(conta, db)

        # Enquanto ficou esperando na fila, alguém (o assistente nativo
        # do Mercado Livre, ou até o próprio vendedor pelo app) pode ter
        # respondido essa pergunta por fora -- confere antes de tentar
        # enviar, pra não bater 403 "Action not allowed" à toa.
        dados_atuais = buscar_pergunta(access_token, pergunta.ml_question_id)
        if dados_atuais.get("status") == "ANSWERED":
            pergunta.status = "respondida_externamente"
            pergunta.camada_resolvida = "externo_ou_ml_nativo"
            pergunta.respondida_em = datetime.utcnow()
            db.commit()
            raise HTTPException(status_code=409, detail="Essa pergunta já foi respondida por outro canal (Mercado Livre não deixa responder de novo).")

        enviar_resposta(access_token, pergunta.ml_question_id, corpo.texto)
    except (MLAuthError, MLApiError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    pergunta.status = "respondida"
    pergunta.camada_resolvida = "manual"
    pergunta.resposta_enviada = corpo.texto
    pergunta.respondida_em = datetime.utcnow()
    db.add(AcaoRegistrada(
        conta_id=conta.id,
        tipo="pergunta_respondida",
        sku=pergunta.sku,
        detalhe="Camada: manual (atendente)",
    ))

    # Toda resposta dada por um atendente é confiável por definição --
    # entra no histórico validado desse SKU pra próximas perguntas
    # parecidas já saírem automáticas (ver pre_venda_logica.py). Só
    # promove quando a pergunta tem SKU (sem SKU não dá pra reaproveitar
    # com segurança pra outro anúncio).
    if pergunta.sku:
        db.add(RespostaValidadaSku(
            sku=pergunta.sku,
            pergunta_exemplo=pergunta.texto,
            resposta=corpo.texto,
        ))

    db.commit()

    return {"status": "respondida", "pergunta_id": pergunta.id}


@router.get("/painel-geral")
def painel_geral(db: Session = Depends(get_db)):
    """
    Dados agregados pro painel de TV (Tela 1 — Visão Geral): totais do
    dia, ranking das contas com mais pendência/maior espera, e volume
    de perguntas por hora.

    Só leitura — não altera nenhuma pergunta nem nenhum outro dado.
    Pensado pra ser consultado a cada alguns segundos por uma tela
    fixa, então evita qualquer escrita e qualquer chamada externa
    (Mercado Livre, IA) — só consulta o banco local.
    """
    agora = datetime.utcnow()
    # "Hoje" começa à meia-noite de Brasília (antes começava à meia-noite
    # UTC = 21h de Brasília, e os números zeravam no meio da noite).
    agora_br = _utc_para_br(agora)
    inicio_do_dia_br = agora_br.replace(hour=0, minute=0, second=0, microsecond=0)
    inicio_do_dia = inicio_do_dia_br.astimezone(timezone.utc).replace(tzinfo=None)  # volta pra UTC cru, igual ao banco

    perguntas_hoje = (
        db.query(Pergunta)
        .filter(Pergunta.recebida_em >= inicio_do_dia)
        .all()
    )
    total_hoje = len(perguntas_hoje)
    total_ia = sum(1 for p in perguntas_hoje if p.camada_resolvida in CAMADAS_AUTOMATICAS)
    total_humano = sum(1 for p in perguntas_hoje if p.camada_resolvida == "manual")
    total_externo = sum(1 for p in perguntas_hoje if p.status == "respondida_externamente")

    # Pendências: considera TODAS as em aberto agora, não só as de hoje
    # — uma pergunta de ontem ainda não respondida continua pendente.
    pendentes = db.query(Pergunta).filter(Pergunta.status == "fila_humana").all()

    por_conta: dict[str, dict] = {}
    for p in pendentes:
        nome = p.conta.apelido if p.conta else "—"
        espera_min = round((agora - p.recebida_em).total_seconds() / 60, 1) if p.recebida_em else 0.0
        registro = por_conta.setdefault(nome, {"conta": nome, "pendentes": 0, "maior_espera_min": 0.0})
        registro["pendentes"] += 1
        registro["maior_espera_min"] = max(registro["maior_espera_min"], espera_min)

    ranking_pendencias = sorted(
        por_conta.values(), key=lambda c: c["maior_espera_min"], reverse=True
    )[:20]
    contas_criticas = sum(1 for c in por_conta.values() if c["maior_espera_min"] > 30)

    # Volume por hora (horário de Brasília), separado por desfecho pro
    # gráfico empilhado: IA resolveu / humano respondeu / pendente / outros
    # (respondida por fora ou ainda em processamento).
    por_hora = [
        {"hora": h, "quantidade": 0, "ia": 0, "humano": 0, "pendente": 0, "outros": 0}
        for h in range(24)
    ]
    for p in perguntas_hoje:
        if not p.recebida_em:
            continue
        linha = por_hora[_utc_para_br(p.recebida_em).hour]
        linha["quantidade"] += 1
        if p.camada_resolvida in CAMADAS_AUTOMATICAS:
            linha["ia"] += 1
        elif p.camada_resolvida == "manual":
            linha["humano"] += 1
        elif p.status == "fila_humana":
            linha["pendente"] += 1
        else:
            linha["outros"] += 1

    # Tempo médio que um humano levou pra responder (perguntas de hoje
    # respondidas manualmente). None quando ainda não há nenhuma.
    tempos_humanos = [
        (p.respondida_em - p.recebida_em).total_seconds() / 60
        for p in perguntas_hoje
        if p.camada_resolvida == "manual" and p.respondida_em and p.recebida_em
        and p.respondida_em >= p.recebida_em
    ]
    tempo_medio_humano_min = (
        round(sum(tempos_humanos) / len(tempos_humanos), 1) if tempos_humanos else None
    )

    # Contas que tiveram pergunta hoje -- os painéis mostram essas em
    # verde ("em dia") quando não sobra nada pendente.
    contas_hoje: dict[str, dict] = {}
    for p in perguntas_hoje:
        nome = p.conta.apelido if p.conta else "—"
        registro = contas_hoje.setdefault(nome, {"conta": nome, "total_hoje": 0, "respondidas_hoje": 0})
        registro["total_hoje"] += 1
        if p.status in ("respondida", "respondida_externamente"):
            registro["respondidas_hoje"] += 1

    # Últimas perguntas resolvidas hoje (IA, atendente ou por fora), mais
    # recentes primeiro -- aparecem em verde na lista dos painéis.
    resolvidas_hoje = (
        db.query(Pergunta)
        .filter(Pergunta.status.in_(["respondida", "respondida_externamente"]))
        .filter(Pergunta.respondida_em >= inicio_do_dia)
        .order_by(Pergunta.respondida_em.desc())
        .limit(30)
        .all()
    )
    respondidas_recentes = [
        {
            "id": p.id,
            "conta": p.conta.apelido if p.conta else "—",
            "texto": p.texto,
            "resposta": p.resposta_enviada,
            "resolvida_por": _resolvida_por(p),
            "recebida_em": p.recebida_em.isoformat() if p.recebida_em else None,
            "respondida_em": p.respondida_em.isoformat() if p.respondida_em else None,
        }
        for p in resolvidas_hoje
    ]

    return {
        "geral": {
            "total_hoje": total_hoje,
            "ia": total_ia,
            "humano": total_humano,
            "respondida_por_fora": total_externo,
            "pendentes": len(pendentes),
            "contas_criticas": contas_criticas,
            "tempo_medio_humano_min": tempo_medio_humano_min,
        },
        "hora_atual": agora_br.hour,
        "ranking_pendencias": ranking_pendencias,
        "contas_hoje": sorted(contas_hoje.values(), key=lambda c: c["conta"].lower()),
        "respondidas_recentes": respondidas_recentes,
        "por_hora": por_hora,
    }
