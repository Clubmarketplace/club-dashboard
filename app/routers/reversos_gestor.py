"""
Tela "Reversos" do gestor (27/10) -- visão completa do que voltou para o galpão:
quantos chegaram, quantos voltaram ao estoque, quantos não, o que está parado,
quem recebeu, quem concluiu e o tempo médio de tratamento.

  GET /api/reversos-gestor/resumo     ?dias=1|7|15|30  (ou ?de=&ate=)  &galpao=  -> JSON
  GET /api/reversos-gestor/card                                                   -> JSON (card do dashboard)
  GET /api/reversos-gestor/relatorio  ?dias=... (ou ?de=&ate=)  &galpao=          -> Excel (.xlsx)

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
from openpyxl import Workbook
from sqlalchemy.orm import Session, defer

from app import auth
from app.database import get_db
from app.models import SolicitacaoCancelamento
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
    GALPOES,
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
        if (data_ate - data_de).days > MAX_DIAS_INTERVALO:
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


def montar_resumo_gestor(linhas, de: date, ate: date, aguardando_agora, agora_utc: Optional[datetime] = None) -> dict:
    """Números da tela a partir dos reversos do período + a fila de pendentes de agora."""
    sit = Counter(_situacao_reverso(s) for s in linhas)
    voltaram = sit.get("voltou", 0)
    aguardando = sit.get("aguardando", 0)
    nao_voltaram = len(linhas) - voltaram - aguardando
    concluidos = voltaram + nao_voltaram
    avisados_seller = sum(1 for s in linhas if s.origem != "logistica")

    # Por dia (todos os dias do período, mesmo vazios), com a situação de cada um
    por_dia = {de + timedelta(days=i): {"recebidos": 0, "voltou": 0, "nao_voltou": 0, "aguardando": 0}
               for i in range((ate - de).days + 1)}
    for s in linhas:
        d = _dia_br(s)
        if d in por_dia:
            sit_ = _situacao_reverso(s)
            por_dia[d]["recebidos"] += 1
            por_dia[d]["voltou" if sit_ == "voltou" else "aguardando" if sit_ == "aguardando" else "nao_voltou"] += 1

    # Resultado (rosca) e motivos de "não voltou ao estoque"
    ordem = ["voltou", "avariado", "faltando_peca", "diferente", "negociacao_ml", "descartado", "outro", "aguardando"]
    extras = [k for k in sit if k not in ordem]       # legados (negociacao_ressarcido etc.)
    resultado = [{"chave": k, "nome": _nome_situacao(k), "quantidade": sit[k]} for k in ordem + extras if sit.get(k)]

    # Por galpão (os 3 sempre, + "sem galpão" quando o seller avisou) e total
    def bloco(rs):
        c = Counter(_situacao_reverso(s) for s in rs)
        v, a = c.get("voltou", 0), c.get("aguardando", 0)
        n = len(rs) - v - a
        return {"recebidos": len(rs), "voltaram": v, "nao_voltaram": n, "aguardando": a,
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
    return montar_resumo_gestor(_reversos_do_periodo(db, d1, d2, galpao), d1, d2, _reversos_aguardando(db, galpao))


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
        ["REVERSOS NO PERÍODO"], ["Recebidos", k["recebidos"]], ["Voltaram ao estoque", k["voltaram"]],
        ["Não voltaram ao estoque", k["nao_voltaram"]], ["Aguardando a equipe", k["aguardando"]],
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
                  "Concluído por", "Concluído em", "Tempo até decidir", "Protocolo", "Observação da equipe", "Registrado por", "Origem"])
    for s in linhas:
        espera = None
        if s.confirmado_por and s.confirmado_em and s.criado_em:
            espera = max(round((s.confirmado_em - s.criado_em).total_seconds() / 60), 0)
        aba_d.append([
            _hora_br(s.criado_em), GALPOES.get(s.galpao, ""), s.conta, PLATAFORMAS.get(s.plataforma, s.plataforma), s.numero_venda,
            s.produto_titulo or s.sku or "", s.motivo or "", _nome_situacao(_situacao_reverso(s)), s.confirmado_por or "",
            _hora_br(s.confirmado_em), formatar_tempo(espera) if espera is not None else "", _texto_protocolo(s),
            s.observacao_resultado or "", s.solicitado_por or "", "Galpão" if s.origem == "logistica" else "Seller",
        ])
    for i, larg in enumerate((17, 11, 22, 14, 20, 36, 30, 32, 18, 17, 14, 18, 30, 18, 10), start=1):
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
