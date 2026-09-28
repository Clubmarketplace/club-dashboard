"""
Ajustes ("calibração") da nossa IA de pré-venda.

Os valores vêm da tela "Administração › Calibrar IA" (tabela config_ia). O que
não foi mexido na tela usa o PADRÃO abaixo. Todo o código da IA lê os ajustes
SÓ por aqui, com um cache curto (30 s) pra não consultar o banco a cada pergunta.
Se o banco falhar, a IA segue com os padrões -- nunca para por causa disso.
"""
import json
import logging
import os
import time
from datetime import datetime

logger = logging.getLogger(__name__)

# Escada de fontes da pesquisa na internet: a IA tenta na ordem e para no
# primeiro degrau que responder com segurança.
DEGRAUS = (
    ("fabricante", "1 · site do fabricante"),
    ("pdf", "2 · manual em PDF"),
    ("lojas", "3 · loja grande"),
    ("videos", "4 · vídeo do fabricante"),
    ("tecnicos", "5 · site técnico"),
    ("aberta", "6 · pesquisa aberta"),
)
NOME_DEGRAU = dict(DEGRAUS)

TONS = {"cordial": "cordial e direto", "profissional": "profissional e objetivo", "direto": "direto e simples"}
TAMANHOS = {"curta": "1-2 frases", "normal": "2-3 frases", "detalhada": "4-5 frases"}
MODELOS = ("preciso", "economico")


# A nossa IA SEMPRE responde depois da IA do ML: espera no mínimo 3 minutos.
# Não é ajustável na tela (decisão da empresa); a variável JANELA_IA_ML_MIN
# só pode AUMENTAR esse tempo, nunca deixar a nossa IA responder antes.
JANELA_MINIMA_MIN = 3


def _janela_padrao() -> int:
    try:
        valor = int(os.getenv("JANELA_IA_ML_MIN", str(JANELA_MINIMA_MIN)))
    except ValueError:
        valor = JANELA_MINIMA_MIN
    return max(JANELA_MINIMA_MIN, min(60, valor))


def padroes() -> dict:
    return {
        "ia_ativa": True,
        "janela_ml_min": _janela_padrao(),
        "fonte_respostas_padrao": True,
        "fonte_dados_anuncio": True,
        "fonte_manuais": True,
        "fonte_internet": True,
        # Começa com os degraus 1, 2 e 3 (fabricante, manual em PDF, lojas grandes).
        "degraus": {"fabricante": True, "pdf": True, "lojas": True, "videos": False, "tecnicos": False, "aberta": False},
        "duas_fontes": True,          # a partir do degrau 3: só responde se 2 fontes concordarem
        "buscas_por_degrau": 2,
        "modelo_pesquisa": "preciso",  # preciso (Sonnet) | economico (modelo padrão)
        "tom": "cordial",
        "tamanho": "normal",
    }


def normalizar(dados: dict) -> dict:
    """Valida o que veio da tela; devolve só os ajustes conhecidos, com valores válidos."""
    base = padroes()
    saida = {}
    for chave, valor in (dados or {}).items():
        if chave not in base:
            continue
        if chave in ("ia_ativa", "fonte_respostas_padrao", "fonte_dados_anuncio", "fonte_manuais", "fonte_internet", "duas_fontes"):
            saida[chave] = bool(valor)
        elif chave == "janela_ml_min":
            continue  # fixo (ver JANELA_MINIMA_MIN): não se altera pela tela
        elif chave == "buscas_por_degrau":
            try:
                saida[chave] = max(1, min(5, int(valor)))
            except (TypeError, ValueError):
                raise ValueError("Número de buscas inválido.")
        elif chave == "degraus":
            if not isinstance(valor, dict):
                raise ValueError("Degraus inválidos.")
            saida[chave] = {k: bool(valor.get(k, False)) for k, _ in DEGRAUS}
        elif chave == "modelo_pesquisa":
            if valor not in MODELOS:
                raise ValueError("Modelo inválido.")
            saida[chave] = valor
        elif chave == "tom":
            if valor not in TONS:
                raise ValueError("Tom inválido.")
            saida[chave] = valor
        elif chave == "tamanho":
            if valor not in TAMANHOS:
                raise ValueError("Tamanho inválido.")
            saida[chave] = valor
    return saida


_cache = {"quando": 0.0, "valor": None}
CACHE_SEG = 30


def obter(forcar: bool = False) -> dict:
    """Ajustes em vigor: padrões + o que foi salvo na tela."""
    agora = time.monotonic()
    if not forcar and _cache["valor"] is not None and agora - _cache["quando"] < CACHE_SEG:
        return _cache["valor"]
    config = padroes()  # janela_ml_min vem só daqui (fixa, mínimo 3 min)
    try:
        from app.database import SessionLocal
        from app.models import ConfigIA
        with SessionLocal() as db:
            for linha in db.query(ConfigIA).all():
                try:
                    config.update(normalizar({linha.chave: json.loads(linha.valor)}))
                except (ValueError, TypeError):
                    logger.warning("Ajuste da IA inválido ignorado: %s", linha.chave)
    except Exception:
        logger.exception("Não consegui ler os ajustes da IA -- usando os padrões")
    _cache.update(quando=agora, valor=config)
    return config


def salvar(db, alteracoes: dict, usuario_nome: str | None) -> dict:
    """Grava as alterações (já normalizadas) e o histórico. Devolve os ajustes novos."""
    from app.models import ConfigIA, HistoricoConfigIA
    atual = obter(forcar=True)
    for chave, valor in normalizar(alteracoes).items():
        if atual.get(chave) == valor:
            continue
        linha = db.query(ConfigIA).filter(ConfigIA.chave == chave).first()
        novo = json.dumps(valor, ensure_ascii=False)
        db.add(HistoricoConfigIA(chave=chave, valor_antigo=json.dumps(atual.get(chave), ensure_ascii=False),
                                 valor_novo=novo, alterado_por=usuario_nome))
        if linha:
            linha.valor, linha.atualizado_por, linha.atualizado_em = novo, usuario_nome, datetime.utcnow()
        else:
            db.add(ConfigIA(chave=chave, valor=novo, atualizado_por=usuario_nome))
    db.commit()
    return obter(forcar=True)


# ----- atalhos usados pelo código da IA -----
def degraus_ativos() -> list[str]:
    cfg = obter()
    return [k for k, _ in DEGRAUS if cfg["degraus"].get(k)]


def modelo_pesquisa(modelo_padrao: str) -> str:
    if obter()["modelo_pesquisa"] == "economico":
        return modelo_padrao
    return (os.getenv("CLAUDE_MODEL_PESQUISA") or "claude-sonnet-5").strip()


def estilo() -> str:
    """Trecho do prompt com o tom e o tamanho escolhidos na calibração."""
    cfg = obter()
    return f"num tom {TONS[cfg['tom']]}, em no máximo {TAMANHOS[cfg['tamanho']]}"
