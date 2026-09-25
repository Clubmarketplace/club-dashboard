"""
Leitura da reputação (termômetro) das contas no Mercado Livre.

  - ler_reputacao_da_conta(): consulta UMA conta e grava em reputacao_contas.
  - atualizar_todas(): percorre todas as contas ativas e conectadas.
  - loop_reputacao(): roda atualizar_todas() em segundo plano a cada
    REPUTACAO_INTERVALO_MIN minutos (padrão 90). Desliga com a variável
    de ambiente REPUTACAO_AUTOMATICA_ATIVA=0.
  - classificar(): regra única de "em dia / a verificar / passou".

Resiliência: uma conta com problema (token revogado, ML fora do ar) NUNCA
interrompe as outras -- o erro fica gravado na linha dela e a leitura
anterior (se houver) continua valendo na tela, marcada como antiga.
"""
import asyncio
import json
import logging
import os
import threading
import time
from datetime import datetime

from app.database import SessionLocal
from app.ml_client import MLApiError, MLAuthError, _get, garantir_token_valido
from app.models import Conta, ReputacaoConta

logger = logging.getLogger(__name__)

PAUSA_ENTRE_CONTAS_SEG = 0.4  # gentileza com a API do ML (100+ contas)

# Limites do termômetro verde / MercadoLíder no ML Brasil, em %. Se o ML
# mudar as regras, basta ajustar aqui (a tela lê daqui).
INDICADORES = [
    # chave interna, chave na API do ML, nome, nome curto, limite (%)
    ("reclamacoes", "claims", "Reclamações", "Reclam.", 1.0),
    ("mediacoes", "mediations", "Mediações", "Mediaç.", 0.5),
    ("canceladas", "cancellations", "Canceladas por você", "Cancel.", 0.5),
    ("atrasos", "delayed_handling_time", "Envios com atraso", "Atrasos", 6.0),
]
FAIXA_ATENCAO = 0.8  # a partir de 80% do limite a conta entra em "a verificar"

_trava = threading.Lock()
_progresso = {"em_andamento": False, "feitas": 0, "total": 0, "inicio": None, "fim": None}


def progresso() -> dict:
    return dict(_progresso)


def _int(valor):
    try:
        return int(valor) if valor is not None else None
    except (TypeError, ValueError):
        return None


def _float(valor):
    try:
        return float(valor) if valor is not None else None
    except (TypeError, ValueError):
        return None


def ler_reputacao_da_conta(conta: Conta, db) -> bool:
    """Lê e grava a reputação de uma conta. Devolve True se leu. Nunca levanta exceção."""
    linha = db.query(ReputacaoConta).filter(ReputacaoConta.conta_id == conta.id).first()
    if linha is None:
        linha = ReputacaoConta(conta_id=conta.id)
        db.add(linha)
    linha.tentativa_em = datetime.utcnow()

    try:
        token = garantir_token_valido(conta, db)
        usuario = _get(f"/users/{conta.ml_user_id}", token, "buscar a reputação")
    except (MLAuthError, MLApiError) as exc:
        linha.erro = str(exc)[:300]
        db.commit()
        logger.warning("Reputação de %s não lida: %s", conta.apelido, exc)
        return False
    except Exception as exc:  # qualquer imprevisto: registra e segue pras próximas
        db.rollback()
        logger.exception("Erro inesperado lendo reputação de %s", conta.apelido)
        try:
            linha = db.query(ReputacaoConta).filter(ReputacaoConta.conta_id == conta.id).first()
            if linha is not None:
                linha.tentativa_em = datetime.utcnow()
                linha.erro = f"Erro inesperado: {exc}"[:300]
                db.commit()
        except Exception:
            db.rollback()
        return False

    rep = usuario.get("seller_reputation") or {}
    metricas = rep.get("metrics") or {}
    vendas = metricas.get("sales") or {}

    linha.erro = None
    linha.lido_em = datetime.utcnow()
    linha.level_id = rep.get("level_id")
    linha.power_seller_status = rep.get("power_seller_status")
    linha.vendas = _int(vendas.get("completed"))
    linha.periodo = vendas.get("period")
    for chave, chave_ml, *_ in INDICADORES:
        m = metricas.get(chave_ml) or {}
        setattr(linha, f"{chave}_taxa", _float(m.get("rate")))
        setattr(linha, f"{chave}_qtd", _int(m.get("value")))
    linha.bruto = json.dumps(rep, ensure_ascii=False)[:20000]
    db.commit()
    return True


def atualizar_todas() -> dict:
    """
    Lê a reputação de todas as contas ativas e conectadas. Se já houver uma
    atualização rodando, não começa outra (devolve o progresso atual).
    """
    if not _trava.acquire(blocking=False):
        return progresso()
    try:
        with SessionLocal() as db:
            contas = (
                db.query(Conta)
                .filter(Conta.inativa_em.is_(None), Conta.access_token.isnot(None))
                .order_by(Conta.apelido)
                .all()
            )
            _progresso.update(em_andamento=True, feitas=0, total=len(contas),
                              inicio=datetime.utcnow().isoformat(), fim=None)
            lidas = 0
            for conta in contas:
                if ler_reputacao_da_conta(conta, db):
                    lidas += 1
                _progresso["feitas"] += 1
                time.sleep(PAUSA_ENTRE_CONTAS_SEG)
        logger.info("Reputação atualizada: %s de %s contas lidas", lidas, len(contas))
    finally:
        _progresso.update(em_andamento=False, fim=datetime.utcnow().isoformat())
        _trava.release()
    return progresso()


def atualizar_em_segundo_plano() -> bool:
    """Dispara atualizar_todas() numa thread. False se já havia uma rodando."""
    if _progresso["em_andamento"] or _trava.locked():
        return False
    threading.Thread(target=atualizar_todas, name="reputacao-agora", daemon=True).start()
    return True


async def loop_reputacao() -> None:
    """Atualiza a reputação de todas as contas periodicamente, sem travar o servidor."""
    try:
        intervalo_min = max(15, int(os.getenv("REPUTACAO_INTERVALO_MIN", "90")))
    except ValueError:
        intervalo_min = 90
    await asyncio.sleep(90)  # deixa o sistema subir por completo antes da 1ª leitura
    while True:
        try:
            await asyncio.to_thread(atualizar_todas)
        except Exception:
            logger.exception("Erro inesperado no loop de reputação")
        await asyncio.sleep(intervalo_min * 60)


# ---------------------------------------------------------------------------
# Classificação (regra única, usada pela API da tela)
# ---------------------------------------------------------------------------

def _nivel(level_id):
    """'5_green' -> 5; vazio/estranho -> None."""
    try:
        return int(str(level_id).split("_", 1)[0])
    except (TypeError, ValueError):
        return None


def classificar(linha: ReputacaoConta | None) -> dict:
    """
    Monta o resumo de uma conta pra tela:
      situacao: "critico" (passou algum limite ou termômetro abaixo do amarelo-claro),
                "atencao" (algum indicador >= 80% do limite, ou termômetro verde-claro),
                "sem"     (sem medalha, mas indicadores em dia),
                "ok"      (em dia), ou
                "sem_dados" (nunca lida).
    """
    if linha is None or linha.lido_em is None:
        return {"situacao": "sem_dados", "metricas": [], "nivel": None}

    metricas = []
    pior_uso = 0.0
    for chave, _chave_ml, nome, curto, limite in INDICADORES:
        taxa = getattr(linha, f"{chave}_taxa")
        pct = round(taxa * 100, 2) if taxa is not None else None
        uso = (pct / limite) if pct is not None and limite else None
        if uso is not None:
            pior_uso = max(pior_uso, uso)
        metricas.append({
            "chave": chave, "nome": nome, "curto": curto, "limite": limite,
            "taxa": pct, "qtd": getattr(linha, f"{chave}_qtd"),
            "uso": round(uso, 3) if uso is not None else None,
        })

    nivel = _nivel(linha.level_id)
    if pior_uso >= 1 or (nivel is not None and nivel <= 3):
        situacao = "critico"
    elif pior_uso >= FAIXA_ATENCAO or nivel == 4:
        situacao = "atencao"
    elif not linha.power_seller_status:
        situacao = "sem"
    else:
        situacao = "ok"
    return {"situacao": situacao, "metricas": metricas, "nivel": nivel}
