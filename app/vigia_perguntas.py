"""
Vigia da fila de pré-venda.

Problema que resolve: uma pergunta que caiu na fila humana podia ser
respondida DEPOIS por fora (IA nativa do Mercado Livre ou o próprio seller
pelo app) e o sistema nunca ficava sabendo -- ela continuava "esperando"
pra sempre nos painéis.

  - conferir_pergunta(): olha UMA pergunta no ML e atualiza o status:
        ANSWERED            -> "respondida_externamente" (roxo, "ML / app"),
                               guardando o texto e a hora da resposta
        apagada/encerrada   -> "encerrada_no_ml" (sai da fila, cinza)
        ainda sem resposta  -> continua na fila
  - conferir_fila(): passa por todas as perguntas esperando (mais antigas
    primeiro), reaproveitando o token de cada conta.
  - loop_vigia_perguntas(): roda conferir_fila() a cada
    VIGIA_PERGUNTAS_INTERVALO_SEG segundos (padrão 60). Desliga com
    VIGIA_PERGUNTAS_ATIVA=0.

Segurança contra "corrida" com o atendente: a troca de status é feita com
UPDATE condicional (só se a pergunta AINDA estiver esperando). Se o
atendente responder pela nossa tela no mesmo instante, a resposta dele
prevalece e o vigia não sobrescreve.
"""
import asyncio
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.ml_client import MLApiError, MLAuthError, buscar_pergunta, garantir_token_valido
from app.models import Conta, Pergunta

logger = logging.getLogger(__name__)

# Status nossos que significam "ainda esperando resposta".
STATUS_ESPERANDO = ("fila_humana", "pendente")
# "pendente" = o webhook ainda está decidindo. Só confere depois desse
# tempo, pra não atravessar uma resposta automática em andamento.
PENDENTE_TOLERANCIA_MIN = 5
LIMITE_POR_RODADA = 300           # teto de consultas por rodada
PAUSA_ENTRE_CONSULTAS_SEG = 0.15  # gentileza com a API do ML

# Status do ML que tiram a pergunta da fila sem resposta.
_STATUS_ML_ENCERRADA = {"DELETED", "CLOSED_UNANSWERED", "BANNED", "DISABLED"}

_trava = threading.Lock()


def _data_ml_para_utc(texto: str | None) -> datetime | None:
    """'2026-09-25T19:10:00.000-04:00' -> datetime UTC sem fuso (padrão do banco)."""
    if not texto:
        return None
    try:
        data = datetime.fromisoformat(texto.replace("Z", "+00:00"))
    except ValueError:
        return None
    if data.tzinfo is not None:
        data = data.astimezone(timezone.utc).replace(tzinfo=None)
    return data


def _atualizar_se_esperando(db, pergunta_id: int, campos: dict) -> bool:
    """UPDATE condicional: só altera se a pergunta ainda estiver esperando."""
    alteradas = (
        db.query(Pergunta)
        .filter(Pergunta.id == pergunta_id, Pergunta.status.in_(STATUS_ESPERANDO))
        .update(campos, synchronize_session=False)
    )
    db.commit()
    return alteradas > 0


def conferir_pergunta(pergunta: Pergunta, access_token: str, db) -> str:
    """
    Confere UMA pergunta no ML. Devolve:
      "respondida_fora" | "encerrada" | "esperando" | "falha"
    Nunca levanta exceção.
    """
    try:
        dados = buscar_pergunta(access_token, pergunta.ml_question_id)
    except MLApiError as exc:
        # Pergunta apagada costuma voltar 404 -- aí sai da fila.
        if "(status 404)" in str(exc):
            ok = _atualizar_se_esperando(db, pergunta.id, {
                "status": "encerrada_no_ml",
                "camada_resolvida": "encerrada_no_ml",
                "respondida_em": datetime.utcnow(),
            })
            return "encerrada" if ok else "esperando"
        logger.warning("Vigia: falha ao consultar pergunta %s: %s", pergunta.id, exc)
        return "falha"
    except Exception:
        logger.exception("Vigia: erro inesperado na pergunta %s", pergunta.id)
        return "falha"

    status_ml = (dados.get("status") or "").upper()

    if status_ml == "ANSWERED":
        resposta = dados.get("answer") or {}
        ok = _atualizar_se_esperando(db, pergunta.id, {
            "status": "respondida_externamente",
            "camada_resolvida": "externo_ou_ml_nativo",
            "resposta_enviada": (resposta.get("text") or "").strip() or None,
            "respondida_em": _data_ml_para_utc(resposta.get("date_created")) or datetime.utcnow(),
        })
        return "respondida_fora" if ok else "esperando"

    if status_ml in _STATUS_ML_ENCERRADA:
        ok = _atualizar_se_esperando(db, pergunta.id, {
            "status": "encerrada_no_ml",
            "camada_resolvida": "encerrada_no_ml",
            "respondida_em": datetime.utcnow(),
        })
        return "encerrada" if ok else "esperando"

    # UNANSWERED, UNDER_REVIEW (em análise no ML) ou algo novo: continua na fila.
    return "esperando"


def conferir_fila() -> dict:
    """Uma rodada do vigia. Se já houver uma rodando, não começa outra."""
    if not _trava.acquire(blocking=False):
        return {"status": "ja_em_andamento"}
    contagem = {"conferidas": 0, "respondida_fora": 0, "encerrada": 0, "esperando": 0, "falha": 0, "sem_acesso": 0}
    try:
        with SessionLocal() as db:
            limite_pendente = datetime.utcnow() - timedelta(minutes=PENDENTE_TOLERANCIA_MIN)
            perguntas = (
                db.query(Pergunta)
                .filter(
                    (Pergunta.status == "fila_humana")
                    | ((Pergunta.status == "pendente") & (Pergunta.recebida_em < limite_pendente))
                )
                .order_by(Pergunta.recebida_em.asc())
                .limit(LIMITE_POR_RODADA)
                .all()
            )
            tokens: dict[int, str | None] = {}
            for pergunta in perguntas:
                if pergunta.conta_id not in tokens:
                    conta = db.query(Conta).filter(Conta.id == pergunta.conta_id).first()
                    try:
                        tokens[pergunta.conta_id] = garantir_token_valido(conta, db) if conta else None
                    except (MLAuthError, MLApiError) as exc:
                        logger.warning("Vigia: conta %s sem acesso: %s", pergunta.conta_id, exc)
                        tokens[pergunta.conta_id] = None
                token = tokens[pergunta.conta_id]
                if not token:
                    contagem["sem_acesso"] += 1
                    continue
                contagem[conferir_pergunta(pergunta, token, db)] += 1
                contagem["conferidas"] += 1
                time.sleep(PAUSA_ENTRE_CONSULTAS_SEG)
        if contagem["respondida_fora"] or contagem["encerrada"]:
            logger.info("Vigia da fila: %s", contagem)
        return contagem
    finally:
        _trava.release()


async def loop_vigia_perguntas() -> None:
    """Roda conferir_fila() pra sempre, em segundo plano, sem travar o servidor."""
    try:
        intervalo = max(30, int(os.getenv("VIGIA_PERGUNTAS_INTERVALO_SEG", "60")))
    except ValueError:
        intervalo = 60
    await asyncio.sleep(30)  # deixa o sistema subir antes da primeira rodada
    while True:
        try:
            await asyncio.to_thread(conferir_fila)
        except Exception:
            logger.exception("Erro inesperado no vigia da fila de pré-venda")
        await asyncio.sleep(intervalo)
