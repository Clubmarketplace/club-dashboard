"""
API da tela "Administração › Calibrar IA" (só admin e supervisor).

  GET /api/calibrar-ia   -> ajustes em vigor, opções da tela e histórico recente
  PUT /api/calibrar-ia   -> salva os ajustes alterados (grava histórico)

Os ajustes valem em até 30 segundos (cache em app/config_ia.py).
"""
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app import auth, config_ia, saude_ia, uso_ia_custo
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


# Abaixo disso o termômetro manda comprar mais (é o mesmo piso da recarga automática do Console).
ALERTA_SALDO_BAIXO_USD = 5.0
ALERTA_DIAS_RESTANTES = 3.0
MIN_HORAS_PARA_RITMO = 6.0  # antes disso a média diária seria chute


def _credito(config: dict) -> dict:
    """
    Termômetro de crédito.

    Modo "saldo": a pessoa conferiu o saldo no Console (valor + instante);
      resta = saldo conferido - gasto estimado desde aquele instante (com o ajuste automático).
    Modo "legado" (ninguém informou o saldo ainda): saldo comprado - gasto do mês.
    """
    saldo = float(config.get("credito_saldo_usd") or 0.0)
    conferido_em = uso_ia_custo.interpretar_data(config.get("credito_saldo_em"))

    if saldo > 0 and conferido_em:
        bruto = uso_ia_custo.gasto_desde_usd(conferido_em)
        if not bruto["disponivel"]:
            return {"modo": "saldo", "disponivel": False, "erro": bruto["erro"]}
        fator = uso_ia_custo.fator_vigente(config)  # 1.0 se o ajuste foi aprendido com a tabela de preços antiga
        gasto = round(bruto["gasto_usd"] * fator, 2)
        resta = round(max(saldo - gasto, 0.0), 2)
        horas = max((datetime.now(timezone.utc) - conferido_em).total_seconds() / 3600.0, 0.0)
        ritmo = round(gasto / horas * 24.0, 2) if (horas >= MIN_HORAS_PARA_RITMO and gasto > 0) else None
        dias = round(resta / ritmo, 1) if ritmo else None
        comprar = resta < ALERTA_SALDO_BAIXO_USD or (dias is not None and dias < ALERTA_DIAS_RESTANTES)
        return {
            "modo": "saldo",
            "disponivel": True,
            "exato": False,
            "saldo_usd": saldo,
            "conferido_em": conferido_em.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "horas_desde": round(horas, 1),
            "gasto_usd": gasto,
            "resta_usd": resta,
            "ritmo_dia_usd": ritmo,
            "dias_restantes": dias,
            "fator": fator,
            "comprar_mais": comprar,
        }

    gasto = uso_ia_custo.gasto_do_mes_usd()
    comprado = config.get("credito_comprado_usd") or 0.0
    resta = round(comprado - gasto["gasto_usd"], 2) if gasto["disponivel"] else None
    return {
        "modo": "legado",
        "disponivel": gasto["disponivel"],
        "exato": gasto["exato"],
        "erro": gasto["erro"],
        "gasto_usd": gasto["gasto_usd"],
        "comprado_usd": comprado,
        "resta_usd": resta,
    }


def _resposta(db: Session) -> dict:
    config = config_ia.obter(forcar=True)
    return {
        "config": config,
        "padroes": config_ia.padroes(),
        "degraus": [{"chave": k, "nome": n} for k, n in config_ia.DEGRAUS],
        "historico": _historico(db),
        "saude": _saude(),
        "credito": _credito(config),
    }


@router.get("")
def ler(request: Request, db: Session = Depends(get_db)):
    _exigir(request, db)
    return _resposta(db)


@router.put("")
def salvar(request: Request, dados: dict = Body(...), db: Session = Depends(get_db)):
    usuario = _exigir(request, db)
    # A data da conferência e o ajuste da estimativa são do SERVIDOR: nunca vêm da tela
    # (senão dava pra "voltar no tempo" e desmontar a conta). O saldo só entra por /saldo.
    dados = {k: v for k, v in dados.items() if k not in ("credito_saldo_usd", "credito_saldo_em", "credito_calibracao")}
    try:
        config_ia.salvar(db, dados, getattr(usuario, "nome_exibicao", None))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _resposta(db)


@router.post("/saldo")
def conferir_saldo(request: Request, dados: dict = Body(...), db: Session = Depends(get_db)):
    """
    Informa o saldo que o Console da Anthropic mostra AGORA. Grava o valor e o instante,
    e a partir daí o gasto passa a ser contado desse momento (ver app/uso_ia_custo.py).
    Corpo: {"saldo_usd": 19.99}
    """
    usuario = _exigir(request, db)
    try:
        saldo = float(dados.get("saldo_usd"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Informe o saldo em dólares (ex.: 19.99).")
    if not (0 < saldo <= 100000):
        raise HTTPException(status_code=400, detail="Saldo inválido: informe um valor maior que zero.")
    campos = uso_ia_custo.nova_conferencia(config_ia.obter(forcar=True), saldo)
    try:
        config_ia.salvar(db, campos, getattr(usuario, "nome_exibicao", None))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    uso_ia_custo._cache_desde.clear()
    return _resposta(db)
