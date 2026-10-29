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
"resta" é sempre calculado aqui.

MODO SALDO CONFERIDO (o que a tela usa): a pessoa informa o saldo que o
Console da Anthropic mostra e o servidor grava o INSTANTE da conferência.
  resta = saldo conferido - gasto estimado DESDE esse instante
Assim o gasto de um crédito antigo nunca contamina o crédito novo, e o erro
da estimativa fica limitado ao pouco tempo desde a última conferência.
A cada nova conferência o sistema compara o que estimou com o que realmente
saiu do saldo e ajusta a estimativa sozinho (ver nova_conferencia).
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

# Modelos com preço em DUAS faixas, por tamanho do prompt de CADA chamada:
#   (prefixo do nome do modelo, (entrada, saída) até o limiar, (entrada, saída) acima do limiar)
# Casa por PREFIXO para pegar também nomes com data (ex.: "claude-haiku-5-5-2026...").
# Haiku 5.5: US$ 0,10 / 0,50 até 100 mil tokens no prompt; US$ 0,50 / 2,50 acima disso
# (a faixa mais cara vale pra chamada INTEIRA). Preços conferidos com o Console em 29/10/2026
# (o custo diário do Haiku 5.5 bateu com esta tabela) -- reconferir se a Anthropic mudar.
PRECOS_POR_PREFIXO = [
    ("claude-haiku-5-5", (0.10, 0.50), (0.50, 2.50)),
]
LIMIAR_PROMPT_LONGO_TOKENS = 100_000

# A partir deste instante a tabela de preços acima é a confiável. O ajuste automático
# (credito_calibracao) aprendido ANTES disso foi medido com o Haiku 5.5 cobrado como se
# fosse Sonnet (modelo desconhecido -> "o mais caro"), então não vale: usa 1.0 até a
# próxima conferência de saldo, que já calibra com a tabela certa.
TABELA_PRECOS_CONFIAVEL_DESDE = datetime(2026, 10, 29, 19, 0, tzinfo=timezone.utc)


def fator_vigente(config: dict) -> float:
    """Ajuste automático da estimativa a usar AGORA (1.0 se foi aprendido com a tabela antiga)."""
    conferido_em = interpretar_data(config.get("credito_saldo_em"))
    if conferido_em is None or conferido_em < TABELA_PRECOS_CONFIAVEL_DESDE:
        return 1.0
    try:
        return float(config.get("credito_calibracao") or 1.0)
    except (TypeError, ValueError):
        return 1.0


def _preco_modelo(modelo: str, tokens_entrada: int = 0) -> tuple[float, float]:
    """(preço de entrada, preço de saída) por milhão de tokens para ESTA chamada."""
    if modelo in PRECOS_USD_POR_MTOK:
        return PRECOS_USD_POR_MTOK[modelo]
    for prefixo, preco_normal, preco_longo in PRECOS_POR_PREFIXO:
        if modelo.startswith(prefixo):
            return preco_longo if tokens_entrada > LIMIAR_PROMPT_LONGO_TOKENS else preco_normal
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


def _estimativa_local_usd(inicio: datetime, fim: datetime | None = None) -> float:
    """Soma o consumo que a própria IA registrou (app/saude_ia.registrar_sucesso_de) de `inicio` até `fim` (agora, se omitido)."""
    from app.database import SessionLocal
    from app.models import EventoIA

    inicio_sem_fuso = inicio.replace(tzinfo=None)  # ocorrido_em é salvo sem fuso (UTC "naive")
    total = 0.0
    with SessionLocal() as db:
        consulta = db.query(EventoIA).filter(EventoIA.tipo == "sucesso", EventoIA.ocorrido_em >= inicio_sem_fuso)
        if fim is not None:
            consulta = consulta.filter(EventoIA.ocorrido_em <= fim.replace(tzinfo=None))
        linhas = consulta.all()
        for linha in linhas:
            if not linha.modelo:
                continue  # evento de antes do termômetro existir -- sem como estimar
            # tokens_entrada desta chamada decide a faixa de preço (prompt curto x longo)
            preco_entrada, preco_saida = _preco_modelo(linha.modelo, linha.tokens_entrada or 0)
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


# ---------------------------------------------------------------------------
# Modo "saldo conferido"
# ---------------------------------------------------------------------------
LIMITE_MIN_CALIBRAR_USD = 1.00   # só ajusta a estimativa se houve gasto suficiente pra comparar (evita ruído)
FATOR_MIN, FATOR_MAX = 0.5, 2.0  # o ajuste automático nunca foge disso

_cache_desde: dict = {}
CACHE_DESDE_SEG = 30


def interpretar_data(texto) -> datetime | None:
    """'2026-10-27T15:40:00Z' (ou sem Z) -> datetime com fuso UTC. None se vazio/inválido."""
    if not texto:
        return None
    try:
        data = datetime.fromisoformat(str(texto).replace("Z", "+00:00"))
    except ValueError:
        return None
    return data if data.tzinfo else data.replace(tzinfo=timezone.utc)


def gasto_desde_usd(inicio: datetime, forcar: bool = False) -> dict:
    """
    Gasto ESTIMADO (bruto, sem o ajuste automático) desde `inicio`. Sempre local, com
    precisão de minutos -- a Cost API só agrupa por dia, o que não serve pra
    "desde a hora em que conferi o saldo".
    {"disponivel": True, "gasto_usd": 0.42, "erro": None}
    """
    chave = inicio.isoformat()
    agora = time.monotonic()
    guardado = _cache_desde.get(chave)
    if not forcar and guardado and agora - guardado[0] < CACHE_DESDE_SEG:
        return guardado[1]
    try:
        resultado = {"disponivel": True, "gasto_usd": _estimativa_local_usd(inicio), "erro": None}
    except Exception as exc:
        logger.exception("Não consegui estimar o gasto da IA desde a última conferência")
        resultado = {"disponivel": False, "gasto_usd": None, "erro": str(exc)[:200]}
    if len(_cache_desde) > 20:
        _cache_desde.clear()
    _cache_desde[chave] = (agora, resultado)
    return resultado


def nova_conferencia(config: dict, saldo_novo: float, agora: datetime | None = None) -> dict:
    """
    Campos a gravar quando alguém informa o saldo do Console:
    {"credito_saldo_usd", "credito_saldo_em", "credito_calibracao"}.

    Calibração: compara o que REALMENTE saiu do saldo desde a conferência anterior
    (saldo anterior - saldo novo) com o que a estimativa BRUTA calculou no mesmo
    período. Se a estimativa vinha errando (preço diferente, tokens não contados...),
    o fator corrige as próximas contas. Só calibra quando:
      - houve gasto estimado suficiente (>= LIMITE_MIN_CALIBRAR_USD), e
      - o saldo não subiu (se subiu, houve compra de crédito e não dá pra comparar).
    Suaviza (média com o fator anterior) e limita entre FATOR_MIN e FATOR_MAX.
    """
    agora = agora or datetime.now(timezone.utc)
    fator = fator_vigente(config)  # ignora ajuste aprendido com a tabela de preços antiga
    saldo_anterior = float(config.get("credito_saldo_usd") or 0.0)
    em_anterior = interpretar_data(config.get("credito_saldo_em"))

    if saldo_anterior > 0 and em_anterior and em_anterior < agora:
        try:
            estimado = _estimativa_local_usd(em_anterior, agora)
            real = round(saldo_anterior - saldo_novo, 2)
            if estimado >= LIMITE_MIN_CALIBRAR_USD and real > 0:
                medido = max(FATOR_MIN, min(FATOR_MAX, real / estimado))
                fator = (fator + medido) / 2
        except Exception:
            logger.exception("Não consegui calibrar a estimativa -- mantendo o ajuste anterior")

    return {
        "credito_saldo_usd": round(float(saldo_novo), 2),
        "credito_saldo_em": agora.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),  # com microssegundos: nenhum evento de antes entra na conta
        "credito_calibracao": round(max(FATOR_MIN, min(FATOR_MAX, fator)), 3),
    }
