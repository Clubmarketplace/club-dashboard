"""
Gasto real com a API da Anthropic (termômetro de crédito).

Duas formas de saber o gasto, nesta ordem de preferência:

  1. EXATA -- Cost API oficial da Anthropic (endpoint de admin). Precisa de
     uma CHAVE ADMIN (começa com "sk-ant-admin01-...", é diferente da chave
     normal que a IA usa pra responder), na variável de ambiente
     ANTHROPIC_ADMIN_API_KEY. A Cost API só existe pra organizações do tipo
     Team/Enterprise no Console -- não funciona em conta "Individual".

  2. ESTIMADA -- sem chave Admin (ou se a Cost API falhar), soma os tokens
     que CADA chamada da nossa IA registrou (ver app/saude_ia.py,
     registrar_sucesso_de) e calcula o custo com a tabela de preços oficial
     da Anthropic (PRECOS_USD_POR_MTOK abaixo). Não é centavo a centavo --
     só conta o que foi registrado DEPOIS desta função existir -- mas dá uma
     noção real do gasto sem precisar virar organização Team.

Não existe endpoint de "saldo restante" em nenhum dos dois casos -- o
"resta" é sempre calculado aqui: saldo comprado (tela Calibrar IA) - gasto
do mês.
"""
import logging
import os
import time
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

_URL = "https://api.anthropic.com/v1/organizations/cost_report"
_VERSAO_API = "2023-06-01"

_cache: dict = {"quando": 0.0, "valor": None}
CACHE_SEG = 60  # a Cost API só atualiza a cada ~5 min; não precisa bater mais que isso

# Preço por milhão de tokens (tier padrão, sem cache/batch), em USD.
# Fonte: platform.claude.com/docs/en/about-claude/pricing -- conferir de
# novo se a Anthropic mudar os preços.
PRECOS_USD_POR_MTOK = {
    "claude-haiku-4-5-20251001": (1.00, 5.00),   # (entrada, saída)
    "claude-sonnet-5": (2.00, 10.00),
}
PRECO_BUSCA_WEB_USD = 0.01  # US$ 10 por 1.000 buscas


def _preco_modelo(modelo: str) -> tuple[float, float]:
    if modelo in PRECOS_USD_POR_MTOK:
        return PRECOS_USD_POR_MTOK[modelo]
    # Modelo não cadastrado na tabela (nome novo/diferente): usa o mais caro
    # conhecido como estimativa -- melhor superestimar o gasto do que esconder.
    logger.warning("Preço desconhecido para o modelo %r -- usando a tabela mais cara como estimativa", modelo)
    return max(PRECOS_USD_POR_MTOK.values(), key=lambda par: par[0] + par[1])


def _chave_admin() -> str | None:
    chave = os.getenv("ANTHROPIC_ADMIN_API_KEY")
    chave = chave.strip() if chave else None
    return chave or None


def _inicio_do_mes() -> datetime:
    agora = datetime.now(timezone.utc)
    return agora.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _somar_gasto_desde(inicio: datetime, chave: str) -> float:
    """Soma o campo `amount` (string em CENTAVOS de USD) de todos os buckets, paginando."""
    total_centavos = 0.0
    params = {
        "starting_at": inicio.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bucket_width": "1d",
        "limit": 31,
    }
    headers = {"x-api-key": chave, "anthropic-version": _VERSAO_API}
    with httpx.Client(timeout=10) as cliente:
        while True:
            resp = cliente.get(_URL, params=params, headers=headers)
            resp.raise_for_status()
            corpo = resp.json()
            for balde in corpo.get("data", []):
                for item in balde.get("results", []):
                    try:
                        total_centavos += float(item.get("amount") or 0)
                    except (TypeError, ValueError):
                        continue
            if not corpo.get("has_more"):
                break
            params["page"] = corpo.get("next_page")
            if not params["page"]:
                break
    return round(total_centavos / 100.0, 2)


def _estimativa_local_usd(inicio: datetime) -> float:
    """Soma o consumo que a própria IA registrou (app/saude_ia.registrar_sucesso_de) desde `inicio`."""
    from app.database import SessionLocal
    from app.models import EventoIA

    inicio_sem_fuso = inicio.replace(tzinfo=None)  # ocorrido_em é salvo sem fuso (UTC "naive")
    total = 0.0
    with SessionLocal() as db:
        linhas = (
            db.query(EventoIA)
            .filter(EventoIA.tipo == "sucesso", EventoIA.ocorrido_em >= inicio_sem_fuso)
            .all()
        )
        for linha in linhas:
            if not linha.modelo:
                continue  # evento de antes do termômetro existir -- sem como estimar
            preco_entrada, preco_saida = _preco_modelo(linha.modelo)
            total += (linha.tokens_entrada or 0) / 1_000_000 * preco_entrada
            total += (linha.tokens_saida or 0) / 1_000_000 * preco_saida
            total += (linha.buscas_web or 0) * PRECO_BUSCA_WEB_USD
    return round(total, 2)


def gasto_do_mes_usd(forcar: bool = False) -> dict:
    """
    {"disponivel": True, "exato": True,  "gasto_usd": 19.19, "erro": None}   -- veio da Cost API
    {"disponivel": True, "exato": False, "gasto_usd": 4.37,  "erro": None}   -- estimado localmente
    {"disponivel": False, "exato": False, "gasto_usd": None, "erro": "<mensagem>"}  -- nem um nem outro deu certo
    """
    agora = time.monotonic()
    if not forcar and _cache["valor"] is not None and agora - _cache["quando"] < CACHE_SEG:
        return _cache["valor"]

    chave = _chave_admin()
    if chave:
        try:
            gasto = _somar_gasto_desde(_inicio_do_mes(), chave)
            resultado = {"disponivel": True, "exato": True, "gasto_usd": gasto, "erro": None}
            _cache.update(quando=agora, valor=resultado)
            return resultado
        except Exception:
            logger.exception("Não consegui consultar o gasto pela Cost API -- caindo pra estimativa local")

    try:
        gasto = _estimativa_local_usd(_inicio_do_mes())
        resultado = {"disponivel": True, "exato": False, "gasto_usd": gasto, "erro": None}
    except Exception as exc:
        logger.exception("Não consegui estimar o gasto local da IA")
        resultado = {"disponivel": False, "exato": False, "gasto_usd": None, "erro": str(exc)[:200]}

    _cache.update(quando=agora, valor=resultado)
    return resultado
