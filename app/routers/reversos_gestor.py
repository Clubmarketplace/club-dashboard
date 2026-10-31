"""
Tela "Reversos" do gestor (27/10) -- visão completa do que voltou para o galpão:
quantos chegaram, quantos no estoque, quantos não, o que está parado,
quem recebeu, quem concluiu e o tempo médio de tratamento.

  GET /api/reversos-gestor/resumo     ?dias=1|7|15|30  (ou ?de=&ate=)  &galpao=  -> JSON
  GET /api/reversos-gestor/card                                                   -> JSON (card do dashboard)
  GET /api/reversos-gestor/relatorio  ?dias=... (ou ?de=&ate=)  &galpao=          -> Excel (.xlsx)
  GET /api/reversos-gestor/lista      ?grupo=&situacao=&dia=&conta=&pagina=...    -> JSON (v22: clique nos cartões)
  GET /api/reversos-gestor/lista/excel  (mesmos filtros, sem paginação)           -> Excel (.xlsx)

Regras:
  - SÓ admin e supervisor (decisão do Washington, 27/10). Os demais perfis recebem 403,
    inclusive gerente de setor e logística. A página (/reversos-gestao) tem a mesma trava.
  - Entram TODOS os reversos: os registrados pelo galpão e os avisados pelo próprio seller.
    "Quem recebeu" lista só quem registrou no galpão (origem "logistica").
  - FOTOS NUNCA são lidas aqui (nem o conteúdo, nem a coluna legada): só números e textos.
    Foi a causa da queda de 26/10; a tela é de gestão e não precisa delas.
  - Datas em horário de Brasília; o banco guarda UTC cru. O "dia" é o do registro (criado_em).
  - "Fila agora" (aguardando / parados) NÃO depende do período: é o que está pendente hoje.
"""
import io
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator
from openpyxl import Workbook
from sqlalchemy import func
from sqlalchemy.orm import Session, defer

from app import auth
from app.database import get_db
from app.models import CustoSku, SolicitacaoCancelamento
from app.routers.painel_galpao import (
    _dia_br,
    _nome_situacao,
    _pct,
    _situacao_reverso,
    _tempo_medio_min,
    formatar_tempo,
)
from app.routers.solicitacoes_cancelamento import (
    FUSO_BR,
    DETALHES_ENVIO_ERRADO,
    GALPOES,
    MOTIVOS_REVERSO,
    PLATAFORMAS,
    TIPO_REVERSO,
    _limites_utc_produtividade,
    _texto_protocolo,
)

router = APIRouter(prefix="/api/reversos-gestor", tags=["reversos-gestor"])

PAPEIS_GESTOR_REVERSOS = ("admin", "supervisor")
PERIODOS_VALIDOS = (1, 7, 15, 30)   # "Hoje", 7, 15 e 30 dias (+ De/Até livre)
MAX_DIAS_INTERVALO = 366            # no máximo ~1 ano por consulta
DIAS_PARADO = 3                     # reverso aguardando há mais que isso = "parado" (alerta)
TOP_CONTAS = 15
# 30/10: filtro "Tudo até a data". A tela manda de=INICIO_REVERSOS (começo do módulo de reversos);
# esse "de" específico não esbarra no limite de 1 ano por consulta, mesmo depois de passar um ano.
INICIO_REVERSOS = date(2026, 10, 1)


# ---------------------------------------------------------------------------
# Acesso e período
# ---------------------------------------------------------------------------
def usuario_gestor_reversos(request: Request, db: Session):
    """Só admin e supervisor. 401 sem sessão, 403 para qualquer outro perfil."""
    usuario = auth.usuario_atual(request, db)
    if usuario is None:
        raise HTTPException(status_code=401, detail="Sessão expirada -- faça login de novo.")
    if usuario.papel not in PAPEIS_GESTOR_REVERSOS:
        raise HTTPException(status_code=403, detail="A tela de reversos é só para admin e supervisor.")
    return usuario


def _intervalo(dias: Optional[int], de: Optional[str], ate: Optional[str]) -> tuple[date, date]:
    """De/Até (livre) tem prioridade; senão usa 'dias' (1 = hoje). Valida tudo com 422."""
    hoje = datetime.now(FUSO_BR).date()
    if de or ate:
        try:
            data_de = date.fromisoformat(de) if de else hoje
            data_ate = date.fromisoformat(ate) if ate else hoje
        except ValueError:
            raise HTTPException(status_code=422, detail="Datas no formato AAAA-MM-DD.")
        if data_ate < data_de:
            raise HTTPException(status_code=422, detail="A data final não pode ser antes da inicial.")
        if data_de != INICIO_REVERSOS and (data_ate - data_de).days > MAX_DIAS_INTERVALO:
            raise HTTPException(status_code=422, detail="Escolha no máximo 1 ano de intervalo.")
        return data_de, data_ate
    dias = 7 if dias is None else dias
    if dias not in PERIODOS_VALIDOS:
        raise HTTPException(status_code=422, detail="Período inválido. Use 1 (hoje), 7, 15 ou 30 dias, ou De/Até.")
    return hoje - timedelta(days=dias - 1), hoje


def _checar_galpao(galpao: Optional[int]) -> None:
    if galpao is not None and galpao not in GALPOES:
        raise HTTPException(status_code=422, detail="Galpão inválido.")


# ---------------------------------------------------------------------------
# Consultas (sem fotos)
# ---------------------------------------------------------------------------
def _base(db: Session):
    return (
        db.query(SolicitacaoCancelamento)
        # Fotos (base64 legado) NÃO são lidas: tela só de números (ver cabeçalho).
        .options(defer(SolicitacaoCancelamento.fotos_base64))
        .filter(SolicitacaoCancelamento.tipo == TIPO_REVERSO)
        .filter(SolicitacaoCancelamento.excluido_em.is_(None))
    )


def _reversos_do_periodo(db: Session, de: date, ate: date, galpao: Optional[int]):
    inicio, fim = _limites_utc_produtividade(de, ate)
    consulta = _base(db).filter(SolicitacaoCancelamento.criado_em >= inicio, SolicitacaoCancelamento.criado_em < fim)
    if galpao is not None:
        consulta = consulta.filter(SolicitacaoCancelamento.galpao == galpao)
    return consulta.order_by(SolicitacaoCancelamento.criado_em.asc()).all()


def _reversos_aguardando(db: Session, galpao: Optional[int]):
    """Fila agora: tudo que ainda espera decisão da equipe, de qualquer data."""
    consulta = _base(db).filter(SolicitacaoCancelamento.confirmado_por.is_(None))
    if galpao is not None:
        consulta = consulta.filter(SolicitacaoCancelamento.galpao == galpao)
    return consulta.order_by(SolicitacaoCancelamento.criado_em.asc()).all()


# ---------------------------------------------------------------------------
# Cálculos (funções puras -- testáveis sem banco)
# ---------------------------------------------------------------------------
def _idade_min(s: SolicitacaoCancelamento, agora_utc: datetime) -> int:
    return max(int((agora_utc - s.criado_em).total_seconds() // 60), 0) if s.criado_em else 0


def montar_fila(aguardando, agora_utc: Optional[datetime] = None) -> dict:
    """Reversos pendentes agora: quantos, quantos 'parados' (> DIAS_PARADO dias) e o mais antigo."""
    agora_utc = agora_utc or datetime.utcnow()
    idades = [_idade_min(s, agora_utc) for s in aguardando]
    limite_min = DIAS_PARADO * 1440
    return {
        "aguardando": len(aguardando),
        "parados": sum(1 for i in idades if i > limite_min),
        "dias_parado": DIAS_PARADO,
        "mais_antigo_min": max(idades) if idades else None,
    }


TOP_SKUS = 10
SEM_SKU = "__sem__"   # valor enviado pela tela para "Sem SKU informado"
# 27/10: GARANTIA é um destino próprio (a equipe emite a nota). Nos cartões, no gráfico por dia e no
# ranking ela aparece separada de "não voltaram". Nas tabelas por galpão/conta e no Excel,
# "Fora do estoque" = não voltou + garantia (tudo que não foi para o estoque).
GARANTIA = "garantia"
# 31/10: garantia E "sem condição de venda" seguem o MESMO caminho (os dois vão para a fábrica). O resultado que a equipe
# marcou continua guardado em resultado_reverso (é o "tipo": defeito de fábrica x avariado etc.); só o DESTINO é um.
# Não entram: "negociacao_ml" (ainda com o ML) e "descartado" (não segue para a fábrica). Ajustar aqui se mudar.
SITUACOES_FABRICA = (GARANTIA, "avariado", "faltando_peca", "diferente", "outro")
TIPOS_FABRICA = {
    GARANTIA: "Sem condição: defeito de fábrica",
    "avariado": "Sem condição: avariado ou quebrado",
    "faltando_peca": "Sem condição: faltando peça/acessório",
    "diferente": "Sem condição: produto diferente do enviado",
    "outro": "Sem condição: outro motivo",
}


def _vai_fabrica(s) -> bool:
    return _situacao_reverso(s) in SITUACOES_FABRICA


def _destino(s) -> str:
    """Situação para os números do painel: tudo que vai para a fábrica conta como GARANTIA; o resto fica como está."""
    sit = _situacao_reverso(s)
    return GARANTIA if sit in SITUACOES_FABRICA else sit


def _tipo_fabrica(s) -> str:
    return TIPOS_FABRICA.get(_situacao_reverso(s), "")


def _chave_sku(s: SolicitacaoCancelamento) -> str:
    """SKU do reverso: a coluna sku (lida do pedido no ML) e, na falta dela, o campo 'produto'
    (produto_titulo), que no reverso é o SKU digitado. Reversos antigos podem estar sem nenhum
    dos dois = SEM_SKU. Maiúsculo para 'mpr530' e 'MPR530' juntarem."""
    return (s.sku or s.produto_titulo or "").strip().upper() or SEM_SKU


def montar_ranking_sku(linhas) -> list[dict]:
    """Top SKUs com mais reversos no período (função pura): recebidos e o destino de cada um."""
    grupos = defaultdict(list)
    for s in linhas:
        grupos[_chave_sku(s)].append(s)
    ranking = []
    for k, rs in grupos.items():
        c = Counter(_destino(s) for s in rs)
        v, a, g = c.get("voltou", 0), c.get("aguardando", 0), c.get(GARANTIA, 0)
        ranking.append({"sku": k, "nome": "Sem SKU informado" if k == SEM_SKU else k,
                        "recebidos": len(rs), "voltaram": v, "aguardando": a, "garantias": g,
                        "nao_voltaram": len(rs) - v - a - g})
    ranking.sort(key=lambda x: (x["sku"] == SEM_SKU, -x["recebidos"], -x["nao_voltaram"], x["sku"]))
    return ranking


def montar_comparacao(atual: dict, anterior_linhas, de_ant: date, ate_ant: date) -> dict:
    """Período anterior (mesmo tamanho, logo antes) para as setas dos cartões. Função pura."""
    c = Counter(_destino(s) for s in anterior_linhas)
    v, a, g = c.get("voltou", 0), c.get("aguardando", 0), c.get(GARANTIA, 0)
    return {"de": de_ant.isoformat(), "ate": ate_ant.isoformat(), "recebidos": len(anterior_linhas),
            "voltaram": v, "garantias": g, "nao_voltaram": len(anterior_linhas) - v - a - g}


def montar_resumo_gestor(linhas, de: date, ate: date, aguardando_agora, agora_utc: Optional[datetime] = None) -> dict:
    """Números da tela a partir dos reversos do período + a fila de pendentes de agora."""
    sit = Counter(_situacao_reverso(s) for s in linhas)      # por motivo (rosca)
    dest = Counter(_destino(s) for s in linhas)              # por destino (cartões)
    voltaram = dest.get("voltou", 0)
    aguardando = dest.get("aguardando", 0)
    garantias = dest.get(GARANTIA, 0)
    fabrica_garantia = sit.get(GARANTIA, 0)
    nao_voltaram = len(linhas) - voltaram - aguardando - garantias
    concluidos = voltaram + nao_voltaram + garantias
    avisados_seller = sum(1 for s in linhas if s.origem != "logistica")

    # Por dia (todos os dias do período, mesmo vazios), com a situação de cada um
    por_dia = {de + timedelta(days=i): {"recebidos": 0, "voltou": 0, "garantia": 0, "nao_voltou": 0, "aguardando": 0}
               for i in range((ate - de).days + 1)}
    for s in linhas:
        d = _dia_br(s)
        if d in por_dia:
            sit_ = _destino(s)
            por_dia[d]["recebidos"] += 1
            por_dia[d]["voltou" if sit_ == "voltou" else "aguardando" if sit_ == "aguardando" else "garantia" if sit_ == GARANTIA else "nao_voltou"] += 1

    # Resultado (rosca) e motivos de "não voltou ao estoque"
    ordem = ["voltou", GARANTIA, "avariado", "faltando_peca", "diferente", "negociacao_ml", "descartado", "outro", "aguardando"]
    extras = [k for k in sit if k not in ordem]       # legados (negociacao_ressarcido etc.)
    resultado = [{"chave": k, "nome": _nome_situacao(k), "quantidade": sit[k]} for k in ordem + extras if sit.get(k)]

    # Por galpão (os 3 sempre, + "sem galpão" quando o seller avisou) e total
    def bloco(rs):
        c = Counter(_destino(s) for s in rs)
        v, a = c.get("voltou", 0), c.get("aguardando", 0)
        n = len(rs) - v - a            # "fora do estoque" = não voltou + fábrica
        return {"recebidos": len(rs), "voltaram": v, "nao_voltaram": n, "aguardando": a, "garantias": c.get(GARANTIA, 0),
                "pct_voltaram": _pct(v, v + n), "tempo_medio_min": _tempo_medio_min(rs)}

    por_galpao = [{"galpao": n, "nome": nome, **bloco([s for s in linhas if s.galpao == n])} for n, nome in GALPOES.items()]
    sem_galpao = [s for s in linhas if s.galpao not in GALPOES]
    if sem_galpao:
        por_galpao.append({"galpao": None, "nome": "Sem galpão (aviso do seller)", **bloco(sem_galpao)})
    total = bloco(linhas)

    # Quem recebeu (registrou no galpão) e quem concluiu (equipe que decidiu)
    receb = defaultdict(int)
    for s in linhas:
        if s.origem == "logistica":
            receb[(s.solicitado_por or "Não informado", s.galpao)] += 1
    quem_recebeu = sorted(
        ({"nome": n, "galpao": g, "galpao_nome": GALPOES.get(g, "—"), "quantidade": q} for (n, g), q in receb.items()),
        key=lambda x: (-x["quantidade"], x["nome"].lower()),
    )
    por_pessoa = defaultdict(list)
    for s in linhas:
        if s.confirmado_por:
            por_pessoa[s.confirmado_por].append(s)
    quem_concluiu = sorted(
        ({"nome": nome, "concluidos": len(rs),
          "voltaram": sum(1 for s in rs if _situacao_reverso(s) == "voltou"),
          "nao_voltaram": sum(1 for s in rs if _situacao_reverso(s) != "voltou"),
          "tempo_medio_min": _tempo_medio_min(rs)} for nome, rs in por_pessoa.items()),
        key=lambda x: (-x["concluidos"], x["nome"].lower()),
    )

    # Por conta (quais contas mais têm reverso e o que aconteceu com eles)
    por_conta = defaultdict(list)
    nomes = {}
    for s in linhas:
        k = s.conta_chave or (s.conta or "").lower()
        nomes.setdefault(k, s.conta)
        por_conta[k].append(s)
    contas = sorted(({"conta": nomes[k], **bloco(rs)} for k, rs in por_conta.items()),
                    key=lambda x: (-x["recebidos"], x["conta"].lower()))

    tempos = [max((s.confirmado_em - s.criado_em).total_seconds() / 60, 0)
              for s in linhas if s.confirmado_por and s.confirmado_em and s.criado_em]
    return {
        "periodo": {"de": de.isoformat(), "ate": ate.isoformat(), "dias": (ate - de).days + 1},
        "kpis": {
            **total, "avisados_seller": avisados_seller,
            "fabrica_garantia": fabrica_garantia, "fabrica_sem_condicao": garantias - fabrica_garantia,
            "fora_estoque": total["nao_voltaram"],     # não voltou + garantia (as tabelas usam este)
            "nao_voltaram": nao_voltaram,              # só os que NÃO voltaram e não são garantia (cartão)
            "tempo_max_min": round(max(tempos)) if tempos else None,
            "conferidos": concluidos,
        },
        "fila": montar_fila(aguardando_agora, agora_utc),
        "por_dia": [{"data": d.isoformat(), **v} for d, v in sorted(por_dia.items())],
        "resultado": resultado,
        "por_galpao": por_galpao,
        "total_galpoes": total,
        "quem_recebeu": quem_recebeu,
        "quem_concluiu": quem_concluiu,
        "contas": contas[:TOP_CONTAS],
        "total_contas": len(contas),
        "ranking_sku": montar_ranking_sku(linhas)[:TOP_SKUS],
        "total_skus": len({_chave_sku(s) for s in linhas}),
    }


# ---------------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------------
@router.get("/resumo")
def resumo(request: Request, dias: Optional[int] = None, de: Optional[str] = None, ate: Optional[str] = None,
           galpao: Optional[int] = None, db: Session = Depends(get_db)):
    usuario_gestor_reversos(request, db)
    _checar_galpao(galpao)
    d1, d2 = _intervalo(dias, de, ate)
    r = montar_resumo_gestor(_reversos_do_periodo(db, d1, d2, galpao), d1, d2, _reversos_aguardando(db, galpao))
    # Período anterior: mesmo tamanho, imediatamente antes (ex.: 7 dias -> os 7 dias antes). Só para as setas.
    tam = (d2 - d1).days + 1
    a2, a1 = d1 - timedelta(days=1), d1 - timedelta(days=tam)
    r["anterior"] = montar_comparacao(r, _reversos_do_periodo(db, a1, a2, galpao), a1, a2)
    return r


@router.get("/card")
def card(request: Request, db: Session = Depends(get_db)):
    """Resumo de HOJE + fila agora, para o card do dashboard do admin/supervisor."""
    usuario_gestor_reversos(request, db)
    hoje = datetime.now(FUSO_BR).date()
    r = montar_resumo_gestor(_reversos_do_periodo(db, hoje, hoje, None), hoje, hoje, _reversos_aguardando(db, None))
    k = r["kpis"]
    return {"hoje": {"recebidos": k["recebidos"], "voltaram": k["voltaram"], "nao_voltaram": k["nao_voltaram"]},
            "fila": r["fila"], "tempo_medio_min": k["tempo_medio_min"]}


def _hora_br(data_utc: Optional[datetime]) -> str:
    return data_utc.replace(tzinfo=timezone.utc).astimezone(FUSO_BR).strftime("%d/%m/%Y %H:%M") if data_utc else ""


@router.get("/relatorio")
def relatorio(request: Request, dias: Optional[int] = None, de: Optional[str] = None, ate: Optional[str] = None,
              galpao: Optional[int] = None, db: Session = Depends(get_db)):
    """Excel: Resumo, Por conta, Pessoas e Detalhe (uma linha por reverso, sem fotos)."""
    usuario = usuario_gestor_reversos(request, db)
    _checar_galpao(galpao)
    d1, d2 = _intervalo(dias, de, ate)
    linhas = _reversos_do_periodo(db, d1, d2, galpao)
    r = montar_resumo_gestor(linhas, d1, d2, _reversos_aguardando(db, galpao))
    k, f = r["kpis"], r["fila"]
    galpao_txt = GALPOES[galpao] if galpao else "Todos os galpões"

    wb = Workbook()
    aba = wb.active
    aba.title = "Resumo"
    cab_g = ["", "Recebidos", "No estoque", "Fora do estoque", "Aguardando", "% no estoque", "Tempo médio"]
    for linha in (
        ["Relatório de reversos"],
        ["Período", f"{d1.strftime('%d/%m/%Y')} a {d2.strftime('%d/%m/%Y')}"], ["Galpão", galpao_txt],
        ["Gerado em", f"{datetime.now(FUSO_BR).strftime('%d/%m/%Y %H:%M')} por {usuario.nome_exibicao}"], [],
        ["REVERSOS NO PERÍODO"], ["Recebidos", k["recebidos"]], ["No estoque", k["voltaram"]],
        ["Foram para garantia", k["garantias"]],
        ["Sem condição de venda", k["nao_voltaram"]], ["Aguardando a equipe", k["aguardando"]],
        ["% que voltou ao estoque (dos conferidos)", f"{k['pct_voltaram']}%"],
        ["Tempo médio até a equipe decidir", formatar_tempo(k["tempo_medio_min"])],
        ["Avisados pelo próprio seller", k["avisados_seller"]], [],
        ["FILA AGORA (independe do período)"], ["Aguardando a equipe", f["aguardando"]],
        [f"Parados há mais de {f['dias_parado']} dias", f["parados"]],
        ["Mais antigo na fila", formatar_tempo(f["mais_antigo_min"])], [],
        ["RESULTADO DOS REVERSOS"], *([x["nome"], x["quantidade"]] for x in r["resultado"]), [],
        ["POR GALPÃO", *cab_g[1:]],
        *([g["nome"], g["recebidos"], g["voltaram"], g["nao_voltaram"], g["aguardando"], f"{g['pct_voltaram']}%",
           formatar_tempo(g["tempo_medio_min"])] for g in r["por_galpao"]),
        (lambda g: ["TOTAL", g["recebidos"], g["voltaram"], g["nao_voltaram"], g["aguardando"], f"{g['pct_voltaram']}%",
                    formatar_tempo(g["tempo_medio_min"])])(r["total_galpoes"]),
    ):
        aba.append(linha)
    aba.column_dimensions["A"].width = 42
    for col in "BCDEFG":
        aba.column_dimensions[col].width = 18

    aba_c = wb.create_sheet("Por conta")
    aba_c.append(["Conta", "Recebidos", "No estoque", "Fora do estoque", "Aguardando", "% no estoque", "Tempo médio"])
    por_conta = defaultdict(list)
    nomes = {}
    for s in linhas:   # reagrupa SEM o corte do top da tela
        k_ = s.conta_chave or (s.conta or "").lower()
        nomes.setdefault(k_, s.conta)
        por_conta[k_].append(s)
    for k_, rs in sorted(por_conta.items(), key=lambda kv: (-len(kv[1]), nomes[kv[0]].lower())):
        c = Counter(_situacao_reverso(s) for s in rs)
        v, a = c.get("voltou", 0), c.get("aguardando", 0)
        n = len(rs) - v - a
        aba_c.append([nomes[k_], len(rs), v, n, a, f"{_pct(v, v + n)}%", formatar_tempo(_tempo_medio_min(rs))])
    for col, larg in zip("ABCDEFG", (30, 12, 12, 16, 12, 14, 14)):
        aba_c.column_dimensions[col].width = larg

    aba_p = wb.create_sheet("Pessoas")
    aba_p.append(["QUEM RECEBEU NO GALPÃO", "Galpão", "Reversos recebidos"])
    for x in r["quem_recebeu"]:
        aba_p.append([x["nome"], x["galpao_nome"], x["quantidade"]])
    aba_p.append([])
    aba_p.append(["QUEM CONCLUIU (EQUIPE)", "Concluídos", "No estoque", "Fora do estoque", "Tempo médio"])
    for x in r["quem_concluiu"]:
        aba_p.append([x["nome"], x["concluidos"], x["voltaram"], x["nao_voltaram"], formatar_tempo(x["tempo_medio_min"])])
    for col, larg in zip("ABCDE", (32, 14, 14, 16, 14)):
        aba_p.column_dimensions[col].width = larg

    aba_d = wb.create_sheet("Detalhe")
    aba_d.append(["Recebido em", "Galpão", "Conta", "Plataforma", "Nº da venda", "Produto", "Aspecto / motivo", "Situação",
                  "Concluído por", "Concluído em", "Tempo até decidir", "Protocolo", "Observação da equipe", "Registrado por", "Origem", "Motivo do reverso"])
    for s in linhas:
        espera = None
        if s.confirmado_por and s.confirmado_em and s.criado_em:
            espera = max(round((s.confirmado_em - s.criado_em).total_seconds() / 60), 0)
        aba_d.append([
            _hora_br(s.criado_em), GALPOES.get(s.galpao, ""), s.conta, PLATAFORMAS.get(s.plataforma, s.plataforma), s.numero_venda,
            s.produto_titulo or s.sku or "", s.motivo or "", _nome_situacao(_situacao_reverso(s)), s.confirmado_por or "",
            _hora_br(s.confirmado_em), formatar_tempo(espera) if espera is not None else "", _texto_protocolo(s),
            s.observacao_resultado or "", s.solicitado_por or "", "Galpão" if s.origem == "logistica" else "Seller",
            _nome_motivo_reverso(s),
        ])
    for i, larg in enumerate((17, 11, 22, 14, 20, 36, 30, 32, 18, 17, 14, 18, 30, 18, 10, 30), start=1):
        aba_d.column_dimensions[aba_d.cell(row=1, column=i).column_letter].width = larg

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    nome = f"reversos_{d1.isoformat()}_a_{d2.isoformat()}.xlsx"
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nome}"'},
    )


# ---------------------------------------------------------------------------
# Lista detalhada de um grupo (v22, 27/10): o gestor clica num cartão, numa barra
# do gráfico, numa fatia da rosca, num motivo ou numa conta e vê QUAIS reversos são,
# com conta, motivo, dia, quem recebeu, quem decidiu e quanto tempo levou.
# Continua SEM fotos (só texto e números), pelo mesmo motivo da queda de 26/10.
# ---------------------------------------------------------------------------
GRUPOS_LISTA = {
    "recebidos": "Todos os recebidos no período",
    "voltaram": "No estoque",
    "nao_voltaram": "Sem condição de venda",
    "garantias": "Foram para garantia",
    "aguardando": "Aguardando a equipe (recebidos no período)",
    "fila": "Fila agora: aguardando a equipe",
    "parados": "Parados na fila há mais de %d dias" % DIAS_PARADO,
}
GRUPOS_SEM_PERIODO = ("fila", "parados")   # são o que está pendente HOJE, de qualquer data
POR_PAGINA_PADRAO = 50
POR_PAGINA_MAX = 200
TOP_RESUMO_LISTA = 8


def _espera_min(s: SolicitacaoCancelamento) -> Optional[int]:
    """Minutos entre o recebimento e a decisão da equipe (None se ainda não decidiu).
    Defensivo: data faltando ou fuso misturado não derruba a lista."""
    if s.confirmado_por and s.confirmado_em and s.criado_em:
        try:
            return max(round((s.confirmado_em - s.criado_em).total_seconds() / 60), 0)
        except TypeError:
            return None
    return None


def _nome_motivo_reverso(s: SolicitacaoCancelamento) -> str:
    """Motivo do reverso (v31) em texto: 'Envio errado (voltagem errada)'. Vazio nos reversos antigos."""
    if not s.motivo_reverso:
        return ""
    nome = MOTIVOS_REVERSO.get(s.motivo_reverso, s.motivo_reverso)
    if s.detalhe_envio:
        nome += f" ({DETALHES_ENVIO_ERRADO.get(s.detalhe_envio, s.detalhe_envio).lower()})"
    return nome


def _item_lista(s: SolicitacaoCancelamento, agora_utc: datetime) -> dict:
    sit = _situacao_reverso(s)
    return {
        "id": s.id,
        "recebido_em": _hora_br(s.criado_em),
        "dia": _dia_br(s).isoformat() if s.criado_em else None,
        "galpao_nome": GALPOES.get(s.galpao, "Sem galpão"),
        "conta": s.conta or "—",
        "plataforma": PLATAFORMAS.get(s.plataforma, s.plataforma or ""),
        "numero_venda": s.numero_venda or "",
        "produto": s.produto_titulo or s.sku or "",
        "sku": s.sku or "",
        "aspecto": s.motivo or "",                       # o que o galpão/seller relatou ao receber
        "situacao": sit,
        "situacao_nome": _nome_situacao(sit),
        "observacao": s.observacao_resultado or "",
        "concluido_por": s.confirmado_por or "",
        "concluido_em": _hora_br(s.confirmado_em),
        "espera_min": _espera_min(s),                    # recebimento -> decisão
        "parado_ha_min": _idade_min(s, agora_utc) if sit == "aguardando" else None,
        "registrado_por": s.solicitado_por or "",
        "origem": "Galpão" if s.origem == "logistica" else "Seller",
        "protocolo": _texto_protocolo(s),
        "motivo_reverso": s.motivo_reverso or "",
        "motivo_reverso_nome": _nome_motivo_reverso(s),
        "em_analise": bool(s.em_analise_em and not s.confirmado_por),
        "sku_atual": _chave_sku(s) if _skus_do_reverso(s) else "",   # SKU que vale (o digitado no galpão ou o do pedido)
        "sku_original": getattr(s, "sku_original", None) or "",
        "sku_corrigido_por": getattr(s, "sku_corrigido_por", None) or "",
        "sku_corrigido_papel": getattr(s, "sku_corrigido_papel", None) or "",
        "sku_corrigido_em": _hora_br(getattr(s, "sku_corrigido_em", None)),
    }


def _selecionar_lista(db: Session, grupo: str, situacao: Optional[str], dia: Optional[str], conta: Optional[str],
                      dias: Optional[int], de: Optional[str], ate: Optional[str], galpao: Optional[int],
                      agora_utc: Optional[datetime] = None, sku: Optional[str] = None,
                      motivo_reverso: Optional[str] = None):
    """Devolve (linhas, d1, d2). d1/d2 = None nos grupos que não dependem do período."""
    if grupo not in GRUPOS_LISTA:
        raise HTTPException(status_code=422, detail="Grupo inválido.")
    _checar_galpao(galpao)
    dia_d = None
    if dia:
        try:
            dia_d = date.fromisoformat(dia)
        except ValueError:
            raise HTTPException(status_code=422, detail="Dia no formato AAAA-MM-DD.")
    agora_utc = agora_utc or datetime.utcnow()

    d1 = d2 = None
    if grupo in GRUPOS_SEM_PERIODO:
        linhas = _reversos_aguardando(db, galpao)
        if grupo == "parados":
            limite = DIAS_PARADO * 1440
            linhas = [s for s in linhas if _idade_min(s, agora_utc) > limite]
    else:
        d1, d2 = _intervalo(dias, de, ate)
        linhas = _reversos_do_periodo(db, d1, d2, galpao)
        if grupo == "voltaram":
            linhas = [s for s in linhas if _situacao_reverso(s) == "voltou"]
        elif grupo == "nao_voltaram":
            linhas = [s for s in linhas if _destino(s) not in ("voltou", "aguardando", GARANTIA)]
        elif grupo == "garantias":
            linhas = [s for s in linhas if _vai_fabrica(s)]
        elif grupo == "aguardando":
            linhas = [s for s in linhas if _situacao_reverso(s) == "aguardando"]

    if situacao:
        linhas = [s for s in linhas if _situacao_reverso(s) == situacao]
    if dia_d:
        linhas = [s for s in linhas if s.criado_em and _dia_br(s) == dia_d]
    if conta:
        q = conta.strip().lower()
        linhas = [s for s in linhas if (s.conta_chave or "").lower() == q or (s.conta or "").lower() == q]

    if sku:
        alvo = SEM_SKU if sku.strip().lower() == SEM_SKU else sku.strip().upper()
        linhas = [s for s in linhas if _chave_sku(s) == alvo]

    if motivo_reverso:
        chave_m = motivo_reverso.strip().lower()
        linhas = [s for s in linhas if (s.motivo_reverso or "sem_motivo") == chave_m]

    # Fila: o que espera há mais tempo primeiro. Demais: o mais recente primeiro.
    linhas = sorted(linhas, key=lambda s: s.criado_em or datetime.min, reverse=grupo not in GRUPOS_SEM_PERIODO)
    return linhas, d1, d2


def resumo_da_lista(linhas, agora_utc: Optional[datetime] = None) -> dict:
    """Resumo do que está na lista (função pura): motivos, contas, dias, galpões e quem decidiu."""
    agora_utc = agora_utc or datetime.utcnow()
    sit = Counter(_situacao_reverso(s) for s in linhas)
    voltaram, aguardando = sit.get("voltou", 0), sit.get("aguardando", 0)

    ordem = ["voltou", GARANTIA, "avariado", "faltando_peca", "diferente", "negociacao_ml", "descartado", "outro", "aguardando"]
    chaves = ordem + [k for k in sit if k not in ordem]
    por_motivo = [{"chave": k, "nome": _nome_situacao(k), "quantidade": sit[k]} for k in chaves if sit.get(k)]

    por_conta = defaultdict(list)
    nomes = {}
    for s in linhas:
        k = s.conta_chave or (s.conta or "").lower()
        nomes.setdefault(k, s.conta or "—")
        por_conta[k].append(s)
    contas = sorted(
        ({"conta": nomes[k], "quantidade": len(rs),
          "voltaram": sum(1 for s in rs if _situacao_reverso(s) == "voltou"),
          "aguardando": sum(1 for s in rs if _situacao_reverso(s) == "aguardando"),
          "nao_voltaram": sum(1 for s in rs if _situacao_reverso(s) not in ("voltou", "aguardando"))}
         for k, rs in por_conta.items()),
        key=lambda x: (-x["quantidade"], x["conta"].lower()))

    mot = Counter(s.motivo_reverso or "sem_motivo" for s in linhas)
    por_motivo_reverso = [{"chave": k, "nome": MOTIVOS_REVERSO.get(k, "Sem motivo informado (reverso antigo)"), "quantidade": mot[k]}
                          for k in [*MOTIVOS_REVERSO, "sem_motivo"] if mot.get(k)]
    por_dia = Counter(_dia_br(s) for s in linhas if s.criado_em)
    dias_lista = [{"data": d.isoformat(), "quantidade": q} for d, q in sorted(por_dia.items(), reverse=True)]

    por_galpao = Counter(GALPOES.get(s.galpao, "Sem galpão") for s in linhas)
    decidiu = Counter(s.confirmado_por for s in linhas if s.confirmado_por)
    idades = [_idade_min(s, agora_utc) for s in linhas if _situacao_reverso(s) == "aguardando"]
    return {
        "total": len(linhas), "voltaram": voltaram, "aguardando": aguardando,
        "nao_voltaram": len(linhas) - voltaram - aguardando,
        "tempo_medio_min": _tempo_medio_min(linhas),
        "mais_antigo_min": max(idades) if idades else None,
        "avisados_seller": sum(1 for s in linhas if s.origem != "logistica"),
        "por_motivo": por_motivo,
        "por_motivo_reverso": por_motivo_reverso,
        "contas": contas[:TOP_RESUMO_LISTA], "total_contas": len(contas),
        "dias": dias_lista[:31], "total_dias": len(dias_lista),
        "por_galpao": [{"nome": n, "quantidade": q} for n, q in por_galpao.most_common()],
        "quem_decidiu": [{"nome": n, "quantidade": q} for n, q in decidiu.most_common(TOP_RESUMO_LISTA)],
    }


@router.get("/lista")
def lista(request: Request, grupo: str = "recebidos", situacao: Optional[str] = None, dia: Optional[str] = None,
          conta: Optional[str] = None, dias: Optional[int] = None, de: Optional[str] = None, ate: Optional[str] = None,
          galpao: Optional[int] = None, pagina: int = 1, por_pagina: int = POR_PAGINA_PADRAO,
          sku: Optional[str] = None, motivo_reverso: Optional[str] = None, db: Session = Depends(get_db)):
    """Lista paginada dos reversos de um grupo + resumo do grupo inteiro (não só da página)."""
    usuario_gestor_reversos(request, db)
    if pagina < 1 or por_pagina < 10 or por_pagina > POR_PAGINA_MAX:
        raise HTTPException(status_code=422, detail=f"Página inválida (por página: 10 a {POR_PAGINA_MAX}).")
    agora = datetime.utcnow()
    linhas, d1, d2 = _selecionar_lista(db, grupo, situacao, dia, conta, dias, de, ate, galpao, agora, sku, motivo_reverso)
    total = len(linhas)
    paginas = max((total + por_pagina - 1) // por_pagina, 1)
    pagina = min(pagina, paginas)
    ini = (pagina - 1) * por_pagina
    return {
        "grupo": grupo, "titulo": GRUPOS_LISTA[grupo],
        "usa_periodo": d1 is not None,
        "periodo": {"de": d1.isoformat(), "ate": d2.isoformat()} if d1 else None,
        "filtros": {"situacao": situacao, "dia": dia, "conta": conta, "sku": sku, "motivo_reverso": motivo_reverso},
        "total": total, "pagina": pagina, "paginas": paginas, "por_pagina": por_pagina,
        "resumo": resumo_da_lista(linhas, agora),
        "itens": [_item_lista(s, agora) for s in linhas[ini:ini + por_pagina]],
    }


@router.get("/lista/excel")
def lista_excel(request: Request, grupo: str = "recebidos", situacao: Optional[str] = None, dia: Optional[str] = None,
                conta: Optional[str] = None, dias: Optional[int] = None, de: Optional[str] = None,
                ate: Optional[str] = None, galpao: Optional[int] = None, sku: Optional[str] = None,
                motivo_reverso: Optional[str] = None, db: Session = Depends(get_db)):
    """Excel só do grupo clicado (mesmos filtros da lista, sem paginação)."""
    usuario = usuario_gestor_reversos(request, db)
    agora = datetime.utcnow()
    linhas, d1, d2 = _selecionar_lista(db, grupo, situacao, dia, conta, dias, de, ate, galpao, agora, sku, motivo_reverso)
    r = resumo_da_lista(linhas, agora)

    wb = Workbook()
    aba = wb.active
    aba.title = "Resumo"
    filtros = [x for x in (f"situação: {_nome_situacao(situacao)}" if situacao else "",
                           f"dia: {date.fromisoformat(dia).strftime('%d/%m/%Y')}" if dia else "",
                           f"conta: {conta}" if conta else "",
                           f"SKU: {'sem SKU informado' if sku == SEM_SKU else sku}" if sku else "",
                           f"motivo do reverso: {MOTIVOS_REVERSO.get(motivo_reverso, 'sem motivo informado')}" if motivo_reverso else "") if x]
    for linha in (
        ["Lista de reversos"], ["Grupo", GRUPOS_LISTA[grupo]],
        ["Período", f"{d1.strftime('%d/%m/%Y')} a {d2.strftime('%d/%m/%Y')}" if d1 else "Fila de hoje (qualquer data)"],
        ["Galpão", GALPOES[galpao] if galpao else "Todos os galpões"],
        ["Filtros", "; ".join(filtros) or "nenhum"],
        ["Gerado em", f"{datetime.now(FUSO_BR).strftime('%d/%m/%Y %H:%M')} por {usuario.nome_exibicao}"], [],
        ["Total na lista", r["total"]], ["No estoque", r["voltaram"]],
        ["Sem condição de venda", r["nao_voltaram"]], ["Aguardando a equipe", r["aguardando"]],
        ["Tempo médio até decidir", formatar_tempo(r["tempo_medio_min"])], [],
        ["POR SITUAÇÃO / MOTIVO"], *([x["nome"], x["quantidade"]] for x in r["por_motivo"]), [],
        ["POR CONTA", "Reversos", "No estoque", "Fora", "Aguardando"],
    ):
        aba.append(linha)
    # Excel traz TODAS as contas (a tela mostra só as primeiras)
    por_conta = defaultdict(list)
    nomes = {}
    for s in linhas:
        k = s.conta_chave or (s.conta or "").lower()
        nomes.setdefault(k, s.conta or "—")
        por_conta[k].append(s)
    for k, rs in sorted(por_conta.items(), key=lambda kv: (-len(kv[1]), nomes[kv[0]].lower())):
        c = Counter(_situacao_reverso(s) for s in rs)
        v, a = c.get("voltou", 0), c.get("aguardando", 0)
        aba.append([nomes[k], len(rs), v, len(rs) - v - a, a])
    aba.column_dimensions["A"].width = 40
    for col in "BCDE":
        aba.column_dimensions[col].width = 16

    aba_l = wb.create_sheet("Lista")
    aba_l.append(["Recebido em", "Galpão", "Conta", "Plataforma", "Nº da venda", "Produto", "SKU", "Aspecto / motivo relatado",
                  "Situação", "Observação da equipe", "Concluído por", "Concluído em", "Tempo até decidir",
                  "Parado há", "Registrado por", "Origem", "Protocolo", "Motivo do reverso"])
    for it in (_item_lista(s, agora) for s in linhas):
        aba_l.append([it["recebido_em"], it["galpao_nome"], it["conta"], it["plataforma"], it["numero_venda"], it["produto"],
                      it["sku"], it["aspecto"], it["situacao_nome"], it["observacao"], it["concluido_por"], it["concluido_em"],
                      formatar_tempo(it["espera_min"]) if it["espera_min"] is not None else "",
                      formatar_tempo(it["parado_ha_min"]) if it["parado_ha_min"] is not None else "",
                      it["registrado_por"], it["origem"], it["protocolo"], it["motivo_reverso_nome"]])
    for i, larg in enumerate((17, 12, 22, 14, 20, 36, 16, 30, 32, 30, 18, 17, 14, 12, 18, 10, 18, 30), start=1):
        aba_l.column_dimensions[aba_l.cell(row=1, column=i).column_letter].width = larg

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    nome = f"reversos_{grupo}_{datetime.now(FUSO_BR).strftime('%Y-%m-%d')}.xlsx"
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nome}"'},
    )


# ---------------------------------------------------------------------------
# GARANTIAS (27/10): os reversos que a equipe marcou como "foi para garantia", por conta e
# período, já com o CUSTO de cada produto (cadastro de custos da própria conta: CustoSku),
# para a equipe tirar a nota e fazer o procedimento. Só admin/supervisor, sem fotos.
#   GET /api/reversos-gestor/garantias        ?conta=&dias=|&de=&ate=&galpao=  -> JSON
#   GET /api/reversos-gestor/garantias/excel  (mesmos filtros)                 -> Excel
# Custo: casa pela conta + SKU (maiúsculo). Sem custo cadastrado = "sem custo" (nunca inventa
# valor); o total soma só os que têm custo e informa quantos ficaram de fora.
# ---------------------------------------------------------------------------
MAX_ITENS_GARANTIA_TELA = 300   # a tela mostra os mais recentes; o Excel traz todos


import re as _re
from difflib import get_close_matches as _parecidos


def _norm_sku(sku: str) -> str:
    """SKU só com letras e números, maiúsculo: 'liq200/220' e 'LIQ-200 220' viram 'LIQ200220'."""
    return _re.sub(r"[^A-Z0-9]", "", (sku or "").upper())


class CatalogoCustos:
    """
    Custos de uma ou mais contas para achar o custo de cada SKU de reverso (31/10).

    REGRA CENTRAL: o custo é SEMPRE o do cadastro (Custos) da PRÓPRIA conta; nada é procurado em outra conta
    (mesmo SKU pode custar diferente de uma conta para outra). Nada é gravado: só leitura.

    VALOR EXATO (decisão do Washington, 31/10): o custo só é usado quando o SKU do reverso é IGUAL ao SKU do cadastro
    da conta (ignora só maiúscula/minúscula e espaços das pontas). SKU parecido NUNCA vira custo: serve apenas de
    pista no diagnóstico ("SKU não encontrado; parecido no cadastro: X") para a pessoa conferir e corrigir.
    """

    def __init__(self, contas: dict):
        # contas: {conta_chave: {"achada": bool, "custos": {SKU_MAIUSCULO: custo}}}
        self._contas = contas
        self._indices = {}

    # compatível com o formato antigo {(conta_chave, SKU): custo}
    def get(self, chave_sku, default=None):
        custo = self.resolver(chave_sku[0], chave_sku[1])["custo"]
        return default if custo is None else custo

    def _indice(self, chave):
        if chave not in self._indices:
            custos = (self._contas.get(chave) or {}).get("custos") or {}
            por_norm, por_norm0, lista = {}, {}, []
            for sku, custo in custos.items():
                n = _norm_sku(sku); n0 = n.lstrip("0")
                lista.append((sku, n0, custo))
                por_norm.setdefault(n, []).append((sku, custo))
                if n0:
                    por_norm0.setdefault(n0, []).append((sku, custo))
            self._indices[chave] = (custos, por_norm, por_norm0, lista)
        return self._indices[chave]

    def resolver(self, chave: str, sku: str) -> dict:
        r = {"custo": None, "origem": None, "motivo": None, "sku_cadastro": None, "parecidos": []}
        conta = self._contas.get(chave)
        if not conta or not conta.get("achada"):
            r["motivo"] = "Conta não encontrada em Contas conectadas."
            return r
        custos, por_norm, por_norm0, lista = self._indice(chave)
        if not custos:
            r["motivo"] = "A conta não tem custos cadastrados."
            return r
        u = (sku or "").strip().upper()
        if u in custos:
            r.update(custo=custos[u], origem="exato", sku_cadastro=u,
                     produto=((conta.get("nomes") or {}).get(u) or ""))
            return r
        # Não achou igual: NÃO usa nenhum custo. Só procura pistas (SKUs parecidos da própria conta) para explicar.
        n = _norm_sku(u); n0 = n.lstrip("0")
        pistas = []
        if n:
            pistas += [s for s, _c in por_norm.get(n, [])]
            if len(n0) >= 3:
                pistas += [s for s, _c in por_norm0.get(n0, [])]
        if len(n0) >= 4:
            pistas += [s for s, n0c, _c in lista if n0c and len(n0c) >= 4 and (n0c.startswith(n0) or n0.startswith(n0c))]
        if n0 and not pistas:
            proximos = _parecidos(n0, [x[1] for x in lista if x[1]], n=3, cutoff=0.8)
            pistas += [s for s, n0c, _c in lista if n0c in proximos]
        pistas = list(dict.fromkeys(pistas))[:3]
        r["parecidos"] = pistas
        r["motivo"] = ("SKU não encontrado no cadastro da conta" +
                       (f"; parecido no cadastro: {', '.join(f'{s} (R$ {custos[s]:.2f})' for s in pistas)} — confira se é o mesmo produto e corrija o SKU." if pistas else "."))
        return r


def _custos_dos_reversos(db: Session, linhas) -> "CatalogoCustos":
    """Catálogo de custos das contas que aparecem nos reversos (só leitura; cada conta com o seu cadastro)."""
    from app.contas_util import achar_conta_por_nome
    ids_conta = {}
    for s in linhas:
        chave = s.conta_chave or (s.conta or "").lower()
        if chave not in ids_conta:
            conta = achar_conta_por_nome(db, chave)
            ids_conta[chave] = conta.id if conta else None
    contas = {k: {"achada": i is not None, "custos": {}, "nomes": {}} for k, i in ids_conta.items()}
    validos = {i for i in ids_conta.values() if i}
    if validos:
        por_id = {i: k for k, i in ids_conta.items() if i}
        for c in db.query(CustoSku).filter(CustoSku.conta_id.in_(validos)).all():
            if c.custo is not None and (c.sku or "").strip():
                contas[por_id[c.conta_id]]["custos"][(c.sku or "").strip().upper()] = float(c.custo)
                contas[por_id[c.conta_id]]["nomes"][(c.sku or "").strip().upper()] = (getattr(c, "nome_produto", None) or "").strip()
    return CatalogoCustos(contas)


def _skus_do_reverso(s: SolicitacaoCancelamento) -> list[str]:
    chave = _chave_sku(s)
    return [] if chave == SEM_SKU else [x.strip() for x in chave.split(",") if x.strip()]


def _resolver_sku(custos, chave: str, sku: str) -> dict:
    if hasattr(custos, "resolver"):
        return custos.resolver(chave, sku)
    v = custos.get((chave, sku))      # formato antigo {(conta, SKU): custo} (testes)
    return {"custo": v, "origem": "exato" if v is not None else None, "motivo": None, "sku_cadastro": None, "parecidos": []}


def calcular_custo_exato(s: SolicitacaoCancelamento, custos) -> dict:
    """Custo EXATO do reverso no cadastro da PRÓPRIA conta (soma se houver vários SKUs; todos precisam existir)."""
    chave = s.conta_chave or (s.conta or "").lower()
    skus = _skus_do_reverso(s)
    res = [_resolver_sku(custos, chave, k) for k in skus]
    completo = bool(skus) and all(r["custo"] is not None for r in res)
    falhou = next((r for r in res if r["custo"] is None), None)
    if not skus:
        motivo = "Reverso sem SKU informado."
    else:
        motivo = (falhou or {}).get("motivo") if not completo else None
    nomes = [r.get("produto") for r in res if r.get("produto")] if completo else []
    return {"custo": round(sum(r["custo"] for r in res), 2) if completo else None,
            "motivo": motivo, "produto": " + ".join(nomes) if nomes else ""}


def fixar_custo_no_reverso(db: Session, s: SolicitacaoCancelamento, usuario_nome: Optional[str] = None,
                           custos=None, substituir: bool = False, papel: Optional[str] = None,
                           usuario_id: Optional[int] = None) -> bool:
    """
    Grava no reverso o custo da garantia, buscado SÓ no cadastro de Custos da conta do reverso (valor exato).
    Não mexe em custo informado manualmente (nem sobrescreve um já gravado, salvo `substituir`). Sem custo exato = não grava.
    Não faz commit (quem chama decide). True se gravou.
    """
    from app.models import LogCustoReverso
    if s.custo_garantia is not None and (s.custo_garantia_origem == "manual" or not substituir):
        return False
    if custos is None:
        custos = _custos_dos_reversos(db, [s])
    r = calcular_custo_exato(s, custos)
    if r["custo"] is None:
        return False
    anterior = s.custo_garantia
    s.custo_garantia = r["custo"]
    s.custo_garantia_origem = "cadastro"
    s.custo_garantia_por = usuario_nome
    s.custo_garantia_em = datetime.utcnow()
    s.custo_garantia_sku = _chave_sku(s)
    s.custo_garantia_produto = r["produto"] or None
    s.custo_garantia_papel = papel
    db.add(LogCustoReverso(solicitacao_id=s.id, custo_anterior=anterior, custo_novo=r["custo"], origem="cadastro",
                           usuario=usuario_nome, usuario_id=usuario_id, papel=papel,
                           detalhe=f"SKU {s.custo_garantia_sku} · conta {s.conta or '—'}"))
    return True


def _erro_reverso_travado(db: Session, s: SolicitacaoCancelamento) -> None:
    from app.routers.remessas_garantia import remessas_dos_reversos
    if _vai_fabrica(s) and remessas_dos_reversos(db, [s.id]).get(s.id):
        raise HTTPException(status_code=409, detail="Essa garantia já está numa remessa para a fábrica; SKU e custo ficam como foram enviados.")


def gravar_custo_manual(db: Session, s: SolicitacaoCancelamento, custo: float, usuario) -> dict:
    """Custo informado à mão numa garantia (admin, supervisor ou o seller dono da conta). Marcado como manual, com
    quem estava logado, o perfil e quando; NUNCA altera o cadastro de Custos. Não faz commit."""
    from app.models import LogCustoReverso
    if not _vai_fabrica(s):
        raise HTTPException(status_code=404, detail="Garantia não encontrada.")
    _erro_reverso_travado(db, s)
    anterior = s.custo_garantia
    s.custo_garantia = custo
    s.custo_garantia_origem = "manual"
    s.custo_garantia_por = usuario.nome_exibicao
    s.custo_garantia_papel = usuario.papel
    s.custo_garantia_em = datetime.utcnow()
    s.custo_garantia_sku = _chave_sku(s)
    db.add(LogCustoReverso(solicitacao_id=s.id, custo_anterior=anterior, custo_novo=custo, origem="manual",
                           usuario=usuario.nome_exibicao, usuario_id=usuario.id, papel=usuario.papel,
                           detalhe=f"SKU {s.custo_garantia_sku} · conta {s.conta or '—'}"))
    return {"status": "ok", "id": s.id, "custo": custo, "custo_origem": "manual"}


def corrigir_sku_reverso(db: Session, s: SolicitacaoCancelamento, sku_novo: str, usuario, sku_antigo: str = "") -> dict:
    """
    Corrige o SKU de um reverso para um SKU que EXISTE no cadastro da PRÓPRIA conta (igual, sem aproximação).
    Guarda o SKU original, quem corrigiu (nome, perfil, id) e quando; se for garantia, o custo é buscado de novo no
    cadastro (substitui custo anterior, inclusive manual). Garantia já em remessa não muda (409). Não faz commit.
    """
    from app.models import LogCustoReverso
    novo = (sku_novo or "").strip().upper()
    if not novo:
        raise HTTPException(status_code=422, detail="Escolha o SKU correto.")
    _erro_reverso_travado(db, s)
    custos = _custos_dos_reversos(db, [s])
    chave = s.conta_chave or (s.conta or "").lower()
    r = _resolver_sku(custos, chave, novo)
    if r["custo"] is None:
        raise HTTPException(status_code=422, detail=f"O SKU {novo} não existe no cadastro de Custos da conta {s.conta or '—'}.")
    canonico = r.get("sku_cadastro") or novo
    tokens = _skus_do_reverso(s)
    antigo = (sku_antigo or "").strip().upper()
    if not tokens:
        tokens = [canonico]; idx = 0
    elif antigo:
        idx = next((i for i, x in enumerate(tokens) if x.upper() == antigo), None)
        if idx is None:
            raise HTTPException(status_code=422, detail="O SKU a corrigir não está neste reverso.")
    elif len(tokens) == 1:
        idx = 0
    else:
        raise HTTPException(status_code=422, detail="Este reverso tem vários SKUs; diga qual deles corrigir.")
    anterior_txt = (s.sku or s.produto_titulo or "").strip() or SEM_SKU
    tokens[idx] = canonico
    novo_txt = ",".join(tokens)
    if novo_txt == (s.sku or s.produto_titulo or "").strip():
        raise HTTPException(status_code=422, detail="Esse já é o SKU do reverso.")
    if not s.sku_original:
        s.sku_original = (s.sku or s.produto_titulo or "").strip() or None
    if (s.sku or "").strip():
        s.sku = novo_txt
    else:
        s.produto_titulo = novo_txt
    s.sku_corrigido_por = usuario.nome_exibicao
    s.sku_corrigido_papel = usuario.papel
    s.sku_corrigido_em = datetime.utcnow()
    custo_anterior = s.custo_garantia
    db.add(LogCustoReverso(solicitacao_id=s.id, custo_anterior=custo_anterior, custo_novo=None, origem="sku",
                           usuario=usuario.nome_exibicao, usuario_id=usuario.id, papel=usuario.papel,
                           sku_anterior=anterior_txt, sku_novo=novo_txt,
                           detalhe=f"Correção de SKU · conta {s.conta or '—'}"))
    custo_novo = None
    if _vai_fabrica(s):
        s.custo_garantia = None; s.custo_garantia_origem = None; s.custo_garantia_por = None
        s.custo_garantia_em = None; s.custo_garantia_sku = None; s.custo_garantia_produto = None
        s.custo_garantia_papel = None
        fixar_custo_no_reverso(db, s, usuario.nome_exibicao, custos=_custos_dos_reversos(db, [s]), papel=usuario.papel,
                               usuario_id=usuario.id)
        custo_novo = s.custo_garantia
    return {"status": "ok", "id": s.id, "sku": novo_txt, "sku_anterior": anterior_txt, "custo": custo_novo,
            "custo_origem": s.custo_garantia_origem}


def skus_da_conta(db: Session, conta: str, q: str = "") -> dict:
    """SKUs do cadastro da conta (com nome e custo) para o seletor de correção. Só leitura."""
    from app.contas_util import achar_conta_por_nome
    conta_db = achar_conta_por_nome(db, conta) if (conta or "").strip() else None
    if conta_db is None:
        return {"conta_achada": False, "itens": []}
    alvo = " ".join((q or "").split()).upper()
    linhas = db.query(CustoSku.sku, CustoSku.nome_produto, CustoSku.custo).filter(CustoSku.conta_id == conta_db.id).all()
    itens = [(s.strip(), (n or "").strip(), c) for s, n, c in linhas if s and c is not None]
    if alvo:
        itens = [x for x in itens if alvo in x[0].upper() or alvo in x[1].upper()]
    itens.sort(key=lambda x: (not x[0].upper().startswith(alvo), x[0].upper()))
    return {"conta_achada": True, "itens": [{"sku": a, "nome": b, "custo": c} for a, b, c in itens[:30]]}


def _parecidos_do_reverso(s: SolicitacaoCancelamento, custos) -> list:
    """Pistas (SKUs parecidos do cadastro da própria conta) do primeiro SKU do reverso que não bate. Nunca viram custo."""
    chave = s.conta_chave or (s.conta or "").lower()
    for k in _skus_do_reverso(s):
        r = _resolver_sku(custos, chave, k)
        if r["custo"] is None:
            return list(r.get("parecidos") or [])
    return []


def _item_garantia(s: SolicitacaoCancelamento, custos) -> dict:
    gravado = getattr(s, "custo_garantia", None)
    if gravado is not None:
        custo, origem, motivo = round(float(gravado), 2), s.custo_garantia_origem or "cadastro", None
        produto = s.custo_garantia_produto or ""
        custo_por, custo_em = s.custo_garantia_por or "", _hora_br(s.custo_garantia_em)
    else:
        r = calcular_custo_exato(s, custos)
        custo, motivo, produto = r["custo"], r["motivo"], r["produto"]
        origem = "exato" if custo is not None else None
        custo_por, custo_em = "", ""
    return {
        "id": s.id,
        "dia": _dia_br(s).isoformat() if s.criado_em else None,
        "recebido_em": _hora_br(s.criado_em),
        "conta": s.conta or "—",
        "plataforma": PLATAFORMAS.get(s.plataforma, s.plataforma or ""),
        "numero_venda": s.numero_venda or "",
        "sku": _chave_sku(s) if _skus_do_reverso(s) else "",
        "produto": ((s.produto_titulo or "") if (s.produto_titulo or "").strip().upper() != _chave_sku(s) else ""),
        "custo": custo,
        # "cadastro" (gravado do cadastro da conta) | "manual" (informado por admin/supervisor) | "exato" (ainda não gravado) | None
        "custo_origem": origem,
        "custo_motivo": motivo,                       # por que está sem custo
        "custo_produto": produto,                     # nome do produto no cadastro de Custos (conferência)
        "custo_por": custo_por, "custo_em": custo_em,
        "tipo": _tipo_fabrica(s), "tipo_chave": _situacao_reverso(s),
        "custo_papel": getattr(s, "custo_garantia_papel", None) or "",
        "parecidos": _parecidos_do_reverso(s, custos) if custo is None else [],
        "sku_original": getattr(s, "sku_original", None) or "",
        "sku_corrigido_por": getattr(s, "sku_corrigido_por", None) or "",
        "sku_corrigido_papel": getattr(s, "sku_corrigido_papel", None) or "",
        "sku_corrigido_em": _hora_br(getattr(s, "sku_corrigido_em", None)),
        "marcado_por": s.confirmado_por or "",
        "marcado_em": _hora_br(s.confirmado_em),
        "recebido_por": s.solicitado_por or "",
        "observacao": s.observacao_resultado or "",
        "protocolo": _texto_protocolo(s),
    }


def _garantias_selecionadas(db: Session, conta: Optional[str], dias, de, ate, galpao):
    _checar_galpao(galpao)
    d1, d2 = _intervalo(dias, de, ate)
    reversos = _reversos_do_periodo(db, d1, d2, galpao)     # todos do período (base da relação por conta)
    todas = [s for s in reversos if _vai_fabrica(s)]
    # Lista de contas (com quantas garantias cada uma) NÃO depende do filtro de conta escolhido
    por_conta = Counter(s.conta or "—" for s in todas)
    contas = [{"conta": n, "quantidade": q} for n, q in sorted(por_conta.items(), key=lambda kv: kv[0].lower())]
    linhas = todas
    if conta:
        q = conta.strip().lower()
        linhas = [s for s in todas if (s.conta_chave or "").lower() == q or (s.conta or "").lower() == q]
    linhas = sorted(linhas, key=lambda s: s.criado_em or datetime.min, reverse=True)
    return linhas, contas, d1, d2, reversos, todas


def montar_por_conta(reversos, custos: dict, em_remessa=None) -> dict:
    """
    Relação por conta (30/10, função pura): para cada conta com reverso no período, quantos
    retornaram, quantos foram para o estoque, quantos para garantia e o VALOR (custo) das
    garantias. O custo vem de `custos` ({(conta_chave, SKU): custo}, base de Custos da própria
    conta). Garantia sem custo cadastrado NÃO entra no valor: conta em `sem_custo` (nunca
    inventa número). Ordena pelo maior valor em garantia.
    31/10: `em_remessa` = ids dos reversos que já foram numa remessa para a fábrica. Para cada conta
    `enviadas`/`custo_enviado` contam só esses; o "parado no galpão" é a diferença (garantias - enviadas).
    """
    em_remessa = em_remessa or set()
    contas: dict = {}
    for s in reversos:
        chave = s.conta_chave or (s.conta or "").lower()
        c = contas.setdefault(chave, {"conta": s.conta or "—", "retornaram": 0, "voltaram": 0, "garantias": 0,
                                      "custo_garantia": 0.0, "sem_custo": 0, "enviadas": 0, "custo_enviado": 0.0,
                                      "nao_voltaram": 0, "aguardando": 0, "fab_garantia": 0, "fab_sem_condicao": 0})
        c["retornaram"] += 1
        situacao = _destino(s)
        if situacao == "voltou":
            c["voltaram"] += 1
        elif situacao == "aguardando":
            c["aguardando"] += 1
        elif situacao != GARANTIA:
            c["nao_voltaram"] += 1          # sem condição de venda (avariado, faltando peça etc.)
        elif situacao == GARANTIA:
            c["garantias"] += 1
            c["fab_garantia" if _situacao_reverso(s) == GARANTIA else "fab_sem_condicao"] += 1
            custo = _item_garantia(s, custos)["custo"]
            if custo is None:
                c["sem_custo"] += 1
            else:
                c["custo_garantia"] += custo
            if s.id in em_remessa:
                c["enviadas"] += 1
                if custo is not None:
                    c["custo_enviado"] += custo
    linhas = sorted(contas.values(), key=lambda c: (-c["custo_garantia"], -c["garantias"], c["conta"].lower()))
    for c in linhas:
        c["custo_garantia"] = round(c["custo_garantia"], 2)
        c["custo_enviado"] = round(c["custo_enviado"], 2)
    total = {
        "retornaram": sum(c["retornaram"] for c in linhas), "voltaram": sum(c["voltaram"] for c in linhas),
        "garantias": sum(c["garantias"] for c in linhas), "sem_custo": sum(c["sem_custo"] for c in linhas),
        "custo_garantia": round(sum(c["custo_garantia"] for c in linhas), 2),
        "enviadas": sum(c["enviadas"] for c in linhas), "custo_enviado": round(sum(c["custo_enviado"] for c in linhas), 2),
        "nao_voltaram": sum(c["nao_voltaram"] for c in linhas), "aguardando": sum(c["aguardando"] for c in linhas),
        "fab_garantia": sum(c["fab_garantia"] for c in linhas), "fab_sem_condicao": sum(c["fab_sem_condicao"] for c in linhas),
    }
    return {"contas": linhas, "total": total}


def montar_garantias(linhas, custos: dict) -> dict:
    """KPIs e itens das garantias (função pura)."""
    itens = [_item_garantia(s, custos) for s in linhas]
    com_custo = [i["custo"] for i in itens if i["custo"] is not None]
    total = round(sum(com_custo), 2)
    return {
        "kpis": {"quantidade": len(itens), "custo_total": total, "com_custo": len(com_custo),
                 "sem_custo": len(itens) - len(com_custo),
                 "custo_medio": round(total / len(com_custo), 2) if com_custo else None},
        "itens": itens,
    }


@router.get("/garantias")
def garantias(request: Request, conta: Optional[str] = None, dias: Optional[int] = None, de: Optional[str] = None,
              ate: Optional[str] = None, galpao: Optional[int] = None, db: Session = Depends(get_db)):
    usuario_gestor_reversos(request, db)
    linhas, contas, d1, d2, reversos, todas = _garantias_selecionadas(db, conta, dias, de, ate, galpao)
    custos = _custos_dos_reversos(db, todas)            # custos de TODAS as garantias (relação por conta + detalhe)
    r = montar_garantias(linhas, custos)
    from app.routers.remessas_garantia import remessas_dos_reversos   # import tardio: o módulo de remessas importa este
    no_envio = remessas_dos_reversos(db, [s.id for s in todas])        # {id do reverso: dados da remessa}
    r["por_conta"] = montar_por_conta(reversos, custos, set(no_envio))  # independe do filtro de conta escolhido
    r["total_itens"] = len(r["itens"])
    r["itens"] = r["itens"][:MAX_ITENS_GARANTIA_TELA]
    for i in r["itens"]:
        i["remessa"] = no_envio.get(i["id"])
    return {"periodo": {"de": d1.isoformat(), "ate": d2.isoformat()}, "conta": conta or None, "contas": contas, **r}


class CustoManualBody(BaseModel):
    custo: float

    @field_validator("custo")
    @classmethod
    def _positivo(cls, v):
        if v is None or v != v or v <= 0 or v > 1_000_000:
            raise ValueError("Informe um custo maior que zero.")
        return round(float(v), 2)


@router.post("/garantias/{solicitacao_id}/custo")
def informar_custo_manual(solicitacao_id: int, corpo: CustoManualBody, request: Request, db: Session = Depends(get_db)):
    """Custo informado à mão (só admin/supervisor), quando o SKU não está no cadastro. Fica marcado como manual,
    com quem/quando, e NUNCA altera o cadastro de Custos do seller."""
    usuario = usuario_gestor_reversos(request, db)
    s = db.query(SolicitacaoCancelamento).filter(SolicitacaoCancelamento.id == solicitacao_id).first()
    if s is None or not _vai_fabrica(s):
        raise HTTPException(status_code=404, detail="Garantia não encontrada.")
    r = gravar_custo_manual(db, s, corpo.custo, usuario)
    db.commit()
    return r


class CorrigirSkuBody(BaseModel):
    sku_novo: str
    sku_antigo: str = ""


@router.post("/reversos/{solicitacao_id}/sku")
def corrigir_sku_gestor(solicitacao_id: int, corpo: CorrigirSkuBody, request: Request, db: Session = Depends(get_db)):
    """Corrige o SKU de qualquer reverso (admin/supervisor) para um SKU do cadastro da conta dele; fica registrado."""
    usuario = usuario_gestor_reversos(request, db)
    s = _base(db).filter(SolicitacaoCancelamento.id == solicitacao_id).first()
    if s is None:
        raise HTTPException(status_code=404, detail="Reverso não encontrado.")
    r = corrigir_sku_reverso(db, s, corpo.sku_novo, usuario, corpo.sku_antigo)
    db.commit()
    return r


@router.get("/skus-conta")
def skus_conta_gestor(request: Request, conta: str = "", q: str = "", db: Session = Depends(get_db)):
    usuario_gestor_reversos(request, db)
    return skus_da_conta(db, conta, q)



# ---------------------------------------------------------------------------------------------------------------
# Dinheiro parado (31/10): custo de tudo que NÃO pode ser vendido e ainda não foi resolvido -- é um ESTADO de agora
# (não depende de período). Entra: garantia/quebrado ainda no galpão + o que já foi à fábrica e aguarda solução.
# Não entra: "em análise pela equipe" (ainda sem decisão: aparece à parte), voltou ao estoque, recuperado, perdido.
# ---------------------------------------------------------------------------------------------------------------
def montar_dinheiro_parado(db: Session, chave: Optional[str] = None, com_itens: bool = False) -> dict:
    from app.models import RemessaGarantia, RemessaGarantiaItem
    from app.routers.remessas_garantia import STATUS_ANALISE, STATUS_ENCERRADA, remessas_dos_reversos
    agora = datetime.utcnow()
    q = _base(db)
    if chave:
        q = q.filter(SolicitacaoCancelamento.conta_chave == chave)
    todas = q.all()
    fab = [s for s in todas if _vai_fabrica(s)]
    agu = [s for s in todas if _situacao_reverso(s) == "aguardando"]
    custos = _custos_dos_reversos(db, fab + agu)
    no_envio = remessas_dos_reversos(db, [s.id for s in fab]) if fab else {}
    contas: dict = {}

    def conta_de(k, nome):
        return contas.setdefault(k, {
            "conta_chave": k, "conta": nome or "—",
            "galpao_valor": 0.0, "galpao_qtd": 0, "galpao_garantia": 0, "galpao_quebrado": 0, "galpao_sem_custo": 0,
            "fabrica_valor": 0.0, "fabrica_qtd": 0, "analise_qtd": 0, "analise_valor": 0.0, "analise_sem_custo": 0,
            "recuperado": 0.0, "perdido": 0.0, "parado_total": 0.0, "itens": []})

    def dias(s):
        base = s.confirmado_em or s.criado_em
        return max((agora - base).days, 0) if base else 0

    for s in fab:
        if s.id in no_envio:
            continue                                  # já na fábrica: entra pela remessa, abaixo
        k = s.conta_chave or (s.conta or "").lower()
        c = conta_de(k, s.conta)
        i = _item_garantia(s, custos)
        c["galpao_qtd"] += 1
        c["galpao_garantia" if _situacao_reverso(s) == GARANTIA else "galpao_quebrado"] += 1
        if i["custo"] is None:
            c["galpao_sem_custo"] += 1
        else:
            c["galpao_valor"] += i["custo"]
        if com_itens:
            c["itens"].append({"situacao": "galpao", "tipo": i["tipo"], "tipo_chave": i["tipo_chave"], "sku": i["sku"],
                               "produto": i["produto"] or i.get("custo_produto") or "", "numero_venda": i["numero_venda"],
                               "custo": i["custo"], "dias": dias(s), "nota": ""})
    for s in agu:
        k = s.conta_chave or (s.conta or "").lower()
        c = conta_de(k, s.conta)
        r = calcular_custo_exato(s, custos)
        c["analise_qtd"] += 1
        if r["custo"] is None:
            c["analise_sem_custo"] += 1
        else:
            c["analise_valor"] += r["custo"]

    rq = db.query(RemessaGarantia).filter(RemessaGarantia.status.in_((STATUS_ANALISE, STATUS_ENCERRADA)))
    if chave:
        rq = rq.filter(func.lower(RemessaGarantia.conta_chave) == chave)
    remessas = rq.all()
    em_analise = {r.id: r for r in remessas if r.status == STATUS_ANALISE}
    for r in remessas:
        k = (r.conta_chave or (r.conta or "")).lower()
        c = conta_de(k, r.conta)
        if r.status == STATUS_ANALISE:
            c["fabrica_valor"] += float(r.valor_total or 0); c["fabrica_qtd"] += int(r.qtd_itens or 0)
        else:
            c["recuperado"] += float((r.ressarcido or 0) + (r.credito or 0) + (r.trocado or 0)); c["perdido"] += float(r.perdido or 0)
    if com_itens and em_analise:
        itens = db.query(RemessaGarantiaItem).filter(RemessaGarantiaItem.remessa_id.in_(list(em_analise))).all()
        sol = {s.id: s for s in db.query(SolicitacaoCancelamento).filter(
            SolicitacaoCancelamento.id.in_([x.solicitacao_id for x in itens])).options(defer(SolicitacaoCancelamento.fotos_base64)).all()}
        for x in itens:
            r = em_analise[x.remessa_id]
            s = sol.get(x.solicitacao_id)
            c = conta_de((r.conta_chave or (r.conta or "")).lower(), r.conta)
            c["itens"].append({"situacao": "fabrica", "tipo": _tipo_fabrica(s) if s else "", "tipo_chave": _situacao_reverso(s) if s else "",
                               "sku": x.sku or "", "produto": x.produto or "", "numero_venda": x.numero_venda or "",
                               "custo": float(x.custo or 0), "dias": max((agora - r.data_envio).days, 0) if r.data_envio else 0,
                               "nota": r.nota_numero or ""})
    linhas = list(contas.values())
    for c in linhas:
        for k in ("galpao_valor", "fabrica_valor", "analise_valor", "recuperado", "perdido"):
            c[k] = round(c[k], 2)
        c["parado_total"] = round(c["galpao_valor"] + c["fabrica_valor"], 2)
        c["itens"].sort(key=lambda i: (i["situacao"] != "galpao", -(i["custo"] or 0)))
        if not com_itens:
            c.pop("itens")
    linhas = [c for c in linhas if c["parado_total"] or c["galpao_qtd"] or c["fabrica_qtd"] or c["analise_qtd"] or c["recuperado"] or c["perdido"]]
    linhas.sort(key=lambda c: (-c["parado_total"], -c["galpao_qtd"], c["conta"].lower()))
    soma = lambda k: round(sum(c[k] for c in linhas), 2)
    total = {k: soma(k) for k in ("galpao_valor", "fabrica_valor", "analise_valor", "recuperado", "perdido", "parado_total")}
    total.update({k: sum(c[k] for c in linhas) for k in ("galpao_qtd", "galpao_garantia", "galpao_quebrado", "galpao_sem_custo",
                                                          "fabrica_qtd", "analise_qtd", "analise_sem_custo")})
    return {"contas": linhas, "total": total, "gerado_em": _hora_br(agora)}


@router.get("/dinheiro-parado")
def dinheiro_parado(request: Request, conta: Optional[str] = None, db: Session = Depends(get_db)):
    """Dinheiro parado de todas as contas (ou de uma: `conta` = chave; aí vem a lista de produtos). Só leitura."""
    usuario_gestor_reversos(request, db)
    from app.contas_util import chave_conta
    chave = chave_conta(conta) if (conta or "").strip() else None      # aceita o nome ou a chave da conta
    return montar_dinheiro_parado(db, chave, com_itens=bool(chave))


@router.post("/garantias/fixar-custos")
def fixar_custos_antigos(request: Request, conta: Optional[str] = None, dias: Optional[int] = None,
                         de: Optional[str] = None, ate: Optional[str] = None, galpao: Optional[int] = None,
                         db: Session = Depends(get_db)):
    """Grava o custo exato (cadastro da própria conta) nas garantias antigas ainda sem custo gravado."""
    usuario = usuario_gestor_reversos(request, db)
    linhas, _contas, _d1, _d2, _rev, todas = _garantias_selecionadas(db, conta, dias, de, ate, galpao)
    pendentes = [s for s in linhas if s.custo_garantia is None]
    custos = _custos_dos_reversos(db, pendentes)
    gravadas = sum(1 for s in pendentes if fixar_custo_no_reverso(db, s, usuario.nome_exibicao, custos))
    db.commit()
    return {"status": "ok", "analisadas": len(pendentes), "gravadas": gravadas, "sem_custo": len(pendentes) - gravadas}


@router.get("/garantias/excel")
def garantias_excel(request: Request, conta: Optional[str] = None, dias: Optional[int] = None, de: Optional[str] = None,
                    ate: Optional[str] = None, galpao: Optional[int] = None, db: Session = Depends(get_db)):
    usuario = usuario_gestor_reversos(request, db)
    linhas, _contas, d1, d2, reversos, todas = _garantias_selecionadas(db, conta, dias, de, ate, galpao)
    custos = _custos_dos_reversos(db, todas)
    r = montar_garantias(linhas, custos)
    pc = montar_por_conta(reversos, custos)
    k = r["kpis"]
    wb = Workbook()
    aba = wb.active
    aba.title = "Resumo"
    for linha in (
        ["Garantias de reversos"], ["Conta", conta or "Todas as contas"],
        ["Período", f"{d1.strftime('%d/%m/%Y')} a {d2.strftime('%d/%m/%Y')}"],
        ["Galpão", GALPOES[galpao] if galpao else "Todos os galpões"],
        ["Gerado em", f"{datetime.now(FUSO_BR).strftime('%d/%m/%Y %H:%M')} por {usuario.nome_exibicao}"], [],
        ["Reversos em garantia", k["quantidade"]], ["Custo total (só os que têm custo)", k["custo_total"]],
        ["Sem custo cadastrado", k["sem_custo"]], ["Custo médio por garantia", k["custo_medio"] if k["custo_medio"] is not None else ""],
    ):
        aba.append(linha)
    aba.column_dimensions["A"].width = 38
    aba.column_dimensions["B"].width = 26

    aba_c = wb.create_sheet("Por conta")   # 30/10: relação de todas as contas (não depende do filtro de conta)
    aba_c.append(["Conta", "Retornaram", "No estoque", "Garantias", "Valor em garantia - custo (R$)", "Garantias sem custo"])
    for c in pc["contas"]:
        aba_c.append([c["conta"], c["retornaram"], c["voltaram"], c["garantias"], c["custo_garantia"], c["sem_custo"]])
    t = pc["total"]
    aba_c.append(["TOTAL GERAL", t["retornaram"], t["voltaram"], t["garantias"], t["custo_garantia"], t["sem_custo"]])
    for n, larg in enumerate((28, 12, 12, 12, 30, 20), start=1):
        aba_c.column_dimensions[aba_c.cell(row=1, column=n).column_letter].width = larg

    aba_g = wb.create_sheet("Garantias")
    aba_g.append(["Recebido em", "Conta", "Plataforma", "Nº da venda", "SKU", "Produto", "Custo (R$)", "Marcado por",
                  "Marcado em", "Recebido no galpão por", "Observação", "Protocolo", "Origem do custo", "Motivo (sem custo)", "Tipo"])
    for i in r["itens"]:
        aba_g.append([i["recebido_em"], i["conta"], i["plataforma"], i["numero_venda"], i["sku"], i["produto"],
                      i["custo"] if i["custo"] is not None else "sem custo", i["marcado_por"], i["marcado_em"],
                      i["recebido_por"], i["observacao"], i["protocolo"],
                      {"exato": "cadastro (SKU igual)", "cadastro": "cadastro da conta", "manual": "informado manualmente"}.get(i["custo_origem"] or "", ""),
                      i["custo_motivo"] or "", i["tipo"]])
    for n, larg in enumerate((17, 22, 14, 20, 18, 36, 12, 18, 17, 20, 34, 18, 30, 60, 40), start=1):
        aba_g.column_dimensions[aba_g.cell(row=1, column=n).column_letter].width = larg

    aba_s = wb.create_sheet("Por SKU")   # resumo para a nota: quantas unidades de cada SKU, por conta
    aba_s.append(["Conta", "SKU", "Quantidade", "Custo unitário (R$)", "Custo total (R$)"])
    grupos = defaultdict(list)
    for i in r["itens"]:
        grupos[(i["conta"], i["sku"] or "(sem SKU)")].append(i)
    for (cta, sku), its in sorted(grupos.items(), key=lambda kv: (kv[0][0].lower(), kv[0][1])):
        valores = [x["custo"] for x in its if x["custo"] is not None]
        unit = round(valores[0], 2) if valores and len(set(valores)) == 1 else ""
        aba_s.append([cta, sku, len(its), unit if unit != "" else ("sem custo" if not valores else "varia"),
                      round(sum(valores), 2) if valores else "sem custo"])
    for col, larg in zip("ABCDE", (24, 20, 12, 20, 18)):
        aba_s.column_dimensions[col].width = larg

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    nome = f"garantias_{(conta or 'todas').replace(' ', '_')}_{d1.strftime('%Y%m%d')}_{d2.strftime('%Y%m%d')}.xlsx"
    return StreamingResponse(
        buffer, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nome}"'})
