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
from app.pre_venda_logica import assunto_exige_humano, chave_do_produto

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
    if p.status == "encerrada_no_ml":
        return "Encerrada no ML"
    if p.status == "aguardando_ml":
        return "Aguardando IA do ML"
    return "—"


def _utc_para_br(data_utc_naive: datetime) -> datetime:
    """Converte um datetime UTC 'cru' (sem fuso, como vem do banco) pro horário de Brasília."""
    return data_utc_naive.replace(tzinfo=timezone.utc).astimezone(FUSO_BR)

# Camadas que respondem sozinhas (sem humano) — usado só pra classificar
# estatística no painel de TV; não muda a lógica de decisão em si (essa
# continua em pre_venda_logica.py).
CAMADAS_AUTOMATICAS = {"resposta_validada", "resposta_padrao", "manual_sku_ia", "politica_geral", "busca_site_fabricante"}


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

    # Resposta do atendente entra no banco validado pra próximas perguntas
    # parecidas saírem automáticas (ver pre_venda_logica.py). Chave: SKU
    # (vale pra todas as contas do produto) ou, sem SKU, o código do
    # anúncio (MLB). EXCEÇÃO: assuntos que sempre exigem humano (desconto,
    # troca, defeito, pedido...) nunca entram -- a IA não pode reaproveitar.
    chave = chave_do_produto(pergunta.sku, pergunta.item_id)
    if chave and not assunto_exige_humano(pergunta.texto):
        db.add(RespostaValidadaSku(
            sku=chave,
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


# ---------------------------------------------------------------------------
# Painel Geral (versão "como estamos indo?") -- tendência e resultado.
# Endpoint separado do /painel-geral de propósito: o Painel da Fila usa
# o /painel-geral e não pode ser afetado por mudanças aqui.
# ---------------------------------------------------------------------------
def _desfecho(p: Pergunta) -> str:
    """Em qual grupo a pergunta entra nos gráficos: ml | ia | equipe | pendente | outros."""
    if p.camada_resolvida in CAMADAS_AUTOMATICAS:
        return "ia"
    if p.camada_resolvida == "manual":
        return "equipe"
    if p.status == "respondida_externamente":
        return "ml"
    if p.status in ("fila_humana", "pendente", "aguardando_ml"):  # aguardando_ml = janela da IA do ML
        return "pendente"
    return "outros"


def _dia_br(data_utc_naive: datetime):
    return _utc_para_br(data_utc_naive).date()


LIMITE_DIAS_PERSONALIZADO = 60  # período "Datas" no máximo 60 dias (gráfico legível e consulta leve)


def _resolver_periodo(periodo: str | None, dias: int | None, de: str | None, ate: str | None, hoje_br):
    """
    Devolve (tipo, inicio_grafico, fim_grafico, inicio_produtos, fim_produtos, rótulo).
      hoje  -> gráfico dos últimos 7 dias (contexto); produtos só de hoje
      7/30  -> gráfico e produtos dos últimos 7/30 dias
      datas -> gráfico e produtos do intervalo escolhido (máx. 60 dias)
    Aceita também o parâmetro antigo "dias" (7/30), por compatibilidade.
    """
    if periodo not in ("hoje", "7", "30", "datas"):
        periodo = "7" if (dias or 30) <= 7 else "30"

    if periodo == "datas":
        try:
            inicio = datetime.strptime(de, "%Y-%m-%d").date() if de else hoje_br - timedelta(days=29)
            fim = datetime.strptime(ate, "%Y-%m-%d").date() if ate else hoje_br
        except ValueError:
            raise HTTPException(status_code=400, detail="Data inválida -- use o formato AAAA-MM-DD.")
        if fim > hoje_br:
            fim = hoje_br
        if inicio > fim:
            inicio, fim = fim, inicio
        if (fim - inicio).days + 1 > LIMITE_DIAS_PERSONALIZADO:
            inicio = fim - timedelta(days=LIMITE_DIAS_PERSONALIZADO - 1)
        rotulo = f"{inicio.strftime('%d/%m')} a {fim.strftime('%d/%m')}"
        return "datas", inicio, fim, inicio, fim, rotulo
    if periodo == "hoje":
        return "hoje", hoje_br - timedelta(days=6), hoje_br, hoje_br, hoje_br, "Hoje"
    n = int(periodo)
    inicio = hoje_br - timedelta(days=n - 1)
    return periodo, inicio, hoje_br, inicio, hoje_br, f"Últimos {n} dias"


@router.get("/painel-resumo")
def painel_resumo(
    db: Session = Depends(get_db),
    conta: str | None = None,
    periodo: str | None = None,   # hoje | 7 | 30 | datas
    de: str | None = None,        # AAAA-MM-DD (só com periodo=datas)
    ate: str | None = None,
    dias: int | None = None,      # compatibilidade com a versão anterior
):
    """
    Dados do Painel Geral: indicadores com comparação à semana anterior,
    últimos N dias por desfecho, hoje (por desfecho e por hora) e os
    produtos que mais chegam para a equipe. Filtro opcional por conta
    (apelido, sem diferenciar maiúsculas). Datas no horário de Brasília.
    """
    agora = datetime.utcnow()
    hoje_br = _utc_para_br(agora).date()
    tipo, ini_graf, fim_graf, ini_prod, fim_prod, rotulo = _resolver_periodo(periodo, dias, de, ate, hoje_br)

    # Janela da consulta: cobre o período escolhido e as 2 semanas da comparação.
    inicio_janela = min(ini_graf, hoje_br - timedelta(days=13))
    inicio_br = datetime.combine(inicio_janela, datetime.min.time()).replace(tzinfo=FUSO_BR)
    inicio_utc = inicio_br.astimezone(timezone.utc).replace(tzinfo=None)

    contas = db.query(Conta).order_by(Conta.apelido).all()
    conta_filtrada = None
    if conta:
        alvo = conta.strip().lower()
        conta_filtrada = next((c for c in contas if (c.apelido or "").strip().lower() == alvo), None)

    consulta = db.query(Pergunta).filter(Pergunta.recebida_em >= inicio_utc)
    if conta:
        consulta = consulta.filter(Pergunta.conta_id == (conta_filtrada.id if conta_filtrada else -1))
    perguntas = consulta.all()

    # --- Por dia (período do gráfico) ---
    total_dias = (fim_graf - ini_graf).days + 1
    por_dia_mapa = {ini_graf + timedelta(days=i): {"ml": 0, "ia": 0, "equipe": 0, "pendente": 0, "outros": 0} for i in range(total_dias)}
    for p in perguntas:
        if not p.recebida_em:
            continue
        dia = _dia_br(p.recebida_em)
        if dia in por_dia_mapa:
            por_dia_mapa[dia][_desfecho(p)] += 1
    por_dia = [
        {"data": d.isoformat(), **v, "total": sum(v.values())}
        for d, v in sorted(por_dia_mapa.items())
    ]

    # --- Hoje: por desfecho e por hora ---
    de_hoje = [p for p in perguntas if p.recebida_em and _dia_br(p.recebida_em) == hoje_br]
    hoje = {"ml": 0, "ia": 0, "equipe": 0, "pendente": 0, "outros": 0}
    por_hora = [{"hora": h, "ml": 0, "ia": 0, "equipe": 0, "pendente": 0, "outros": 0} for h in range(24)]
    for p in de_hoje:
        grupo = _desfecho(p)
        hoje[grupo] += 1
        por_hora[_utc_para_br(p.recebida_em).hour][grupo] += 1

    # --- Indicadores: últimos 7 dias x 7 dias anteriores ---
    def semana(ini: int, fim: int):
        """Perguntas de (hoje - fim) até (hoje - ini) dias atrás, inclusive."""
        dias_semana = {hoje_br - timedelta(days=i) for i in range(ini, fim + 1)}
        return [p for p in perguntas if p.recebida_em and _dia_br(p.recebida_em) in dias_semana]

    def pct_sem_equipe(lista):
        total = len(lista)
        if not total:
            return None
        return round(100 * sum(1 for p in lista if _desfecho(p) in ("ml", "ia")) / total)

    def tempo_medio_equipe(lista):
        tempos = [
            (p.respondida_em - p.recebida_em).total_seconds() / 60
            for p in lista
            if _desfecho(p) == "equipe" and p.respondida_em and p.recebida_em and p.respondida_em >= p.recebida_em
        ]
        return round(sum(tempos) / len(tempos), 1) if tempos else None

    atual, anterior = semana(0, 6), semana(7, 13)

    # Pendentes agora: de qualquer data (não só da janela), respeitando o filtro de conta.
    pendentes_q = db.query(Pergunta).filter(Pergunta.status == "fila_humana")
    if conta:
        pendentes_q = pendentes_q.filter(Pergunta.conta_id == (conta_filtrada.id if conta_filtrada else -1))
    pendentes = pendentes_q.all()
    contas_criticas = len({
        p.conta_id for p in pendentes
        if p.recebida_em and (agora - p.recebida_em).total_seconds() > 30 * 60
    })

    dias_anteriores = [d for d in por_dia if d["data"] != hoje_br.isoformat()]
    media_dia = round(sum(d["total"] for d in dias_anteriores) / len(dias_anteriores), 1) if dias_anteriores else None

    # --- Top 10 produtos que mais chegam para a equipe (no período escolhido) ---
    contagem: dict[str, dict] = {}
    for p in perguntas:
        if not p.recebida_em or not (ini_prod <= _dia_br(p.recebida_em) <= fim_prod):
            continue
        if _desfecho(p) not in ("equipe", "pendente"):
            continue
        chave = chave_do_produto(p.sku, p.item_id)
        if not chave:
            continue
        item = contagem.setdefault(chave, {"chave": chave, "sku": p.sku, "item_id": p.item_id, "titulo": None, "quantidade": 0})
        item["quantidade"] += 1
        if p.titulo_anuncio and not item["titulo"]:
            item["titulo"] = p.titulo_anuncio  # aparece sozinho quando o ML liberar a leitura dos anúncios
    produtos = sorted(contagem.values(), key=lambda x: -x["quantidade"])[:10]

    return {
        "filtro": {
            "conta": conta_filtrada.apelido if conta_filtrada else None,
            "conta_nao_encontrada": bool(conta and not conta_filtrada),
            "periodo": tipo,
            "de": ini_prod.isoformat(),
            "ate": fim_prod.isoformat(),
            "rotulo": rotulo,
            "grafico_de": ini_graf.isoformat(),
            "grafico_ate": fim_graf.isoformat(),
        },
        "contas": [c.apelido for c in contas if c.apelido],
        "hora_atual": _utc_para_br(agora).hour,
        "indicadores": {
            "sem_equipe_pct": pct_sem_equipe(atual),
            "sem_equipe_pct_anterior": pct_sem_equipe(anterior),
            "tempo_medio_equipe_min": tempo_medio_equipe(atual),
            "tempo_medio_equipe_min_anterior": tempo_medio_equipe(anterior),
            "pendentes_agora": len(pendentes),
            "contas_criticas": contas_criticas,
            "perguntas_hoje": len(de_hoje),
            "media_por_dia": media_dia,
        },
        "por_dia": por_dia,
        "hoje": hoje,
        "por_hora": por_hora,
        "produtos": produtos,
    }
