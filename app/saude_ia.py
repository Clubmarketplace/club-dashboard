"""
Saúde das chamadas à API do Claude (nossa IA de pré-venda).

Problema que resolve: quando a API falha (sem crédito, limite, chave
inválida, rede...), a pergunta cai pra fila humana e o erro só fica no
log do servidor -- ninguém no painel percebe que a IA parou de responder.

  - registrar_falha(motivo): chamada em toda chamada à API que der erro.
  - registrar_sucesso(): chamada em toda chamada à API que funcionar.
  - falha_atual(): o que a tela "Calibrar IA" usa pra mostrar o aviso
    "⚠️ IA sem crédito/falhando desde HH:MM" -- só enquanto o evento MAIS
    RECENTE registrado for uma falha dentro da janela; some sozinho assim
    que uma chamada funcionar de novo.

Nunca levanta exceção -- um problema aqui não pode derrubar a IA nem o
painel.
"""
import logging
from datetime import datetime, timedelta

from app.database import SessionLocal
from app.models import EventoIA

logger = logging.getLogger(__name__)

JANELA_PADRAO_MIN = 60  # até quando uma falha antiga ainda "conta" pro aviso


def registrar_falha(motivo: str | None) -> None:
    try:
        with SessionLocal() as db:
            db.add(EventoIA(tipo="falha", motivo=(motivo or "")[:300]))
            db.commit()
    except Exception:
        logger.exception("Não consegui registrar a falha da IA")


def registrar_sucesso() -> None:
    try:
        with SessionLocal() as db:
            db.add(EventoIA(tipo="sucesso"))
            db.commit()
    except Exception:
        logger.exception("Não consegui registrar o sucesso da IA")


def falha_atual(janela_min: int = JANELA_PADRAO_MIN) -> dict | None:
    """
    Devolve {"desde": datetime, "motivo": str} se a IA estiver, NESTE
    momento, numa sequência de falhas dentro da janela -- ou None se o
    último evento foi um sucesso, ou se a última falha é mais antiga que
    a janela.
    """
    try:
        with SessionLocal() as db:
            ultimo = db.query(EventoIA).order_by(EventoIA.id.desc()).first()
            if not ultimo or ultimo.tipo != "falha":
                return None
            limite = datetime.utcnow() - timedelta(minutes=janela_min)
            if ultimo.ocorrido_em < limite:
                return None

            # Volta até o começo dessa sequência de falhas seguidas, pra
            # mostrar desde quando a IA está sem responder.
            desde = ultimo.ocorrido_em
            cursor_id = ultimo.id
            while True:
                anterior = (
                    db.query(EventoIA)
                    .filter(EventoIA.id < cursor_id)
                    .order_by(EventoIA.id.desc())
                    .first()
                )
                if not anterior or anterior.tipo != "falha":
                    break
                desde = anterior.ocorrido_em
                cursor_id = anterior.id

            return {"desde": desde, "motivo": ultimo.motivo}
    except Exception:
        logger.exception("Não consegui checar a saúde da IA")
        return None
