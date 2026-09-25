"""
"Nossa IA responde agora": decide e envia a resposta de UMA pergunta.

Usado em dois momentos:
  - pelo vigia (app/vigia_perguntas.py), depois da JANELA DA IA DO ML:
    a pergunta espera alguns minutos pra IA nativa do Mercado Livre
    responder; se o ML não respondeu, a nossa IA tenta;
  - pelo webhook, direto, quando a janela estiver desligada (JANELA_IA_ML_MIN=0).

Ordem de prioridade combinada: IA do ML -> nossa IA -> equipe (fila humana).
"""
import logging
import os
from datetime import datetime

from app.ml_client import MLApiError, enviar_resposta
from app.models import AcaoRegistrada, Pergunta
from app.pre_venda_logica import assunto_exige_humano, decidir_resposta

logger = logging.getLogger(__name__)


def janela_ia_ml_min() -> int:
    """Minutos que a pergunta espera a IA do ML antes da nossa IA agir (0 = não espera)."""
    try:
        return max(0, min(60, int(os.getenv("JANELA_IA_ML_MIN", "3"))))
    except ValueError:
        return 3


def responder_com_nossa_ia(pergunta: Pergunta, access_token: str, db) -> str:
    """
    Roda as camadas da nossa IA e envia a resposta pro ML.
    Devolve "respondida" | "fila_humana". Nunca levanta exceção.

    Quem chama deve ter "reservado" a pergunta antes (status "pendente"),
    pra nunca enviar duas respostas à mesma pergunta.
    """
    # Desconto, troca, defeito, pedido já feito... sempre com a equipe --
    # a nossa IA nunca responde esses assuntos, nem se achar resposta.
    if assunto_exige_humano(pergunta.texto or ""):
        pergunta.status = "fila_humana"
        db.commit()
        return "fila_humana"

    try:
        decisao = decidir_resposta(
            db, pergunta.sku, pergunta.texto or "", pergunta.titulo_anuncio, item_id=pergunta.item_id,
        )
    except Exception:
        logger.exception("Nossa IA falhou ao decidir a pergunta %s -- vai pra equipe", pergunta.id)
        db.rollback()
        pergunta = db.query(Pergunta).filter(Pergunta.id == pergunta.id).first()
        pergunta.status = "fila_humana"
        db.commit()
        return "fila_humana"

    if decisao.get("status") != "respondida":
        pergunta.status = "fila_humana"
        db.commit()
        return "fila_humana"

    try:
        enviar_resposta(access_token, pergunta.ml_question_id, decisao["resposta"])
    except MLApiError as exc:
        # Ex.: o ML/seller respondeu no último segundo, ou a pergunta foi apagada.
        # O vigia confere de novo no próximo minuto e corrige o status.
        logger.error("Falha ao enviar resposta automática (pergunta %s): %s", pergunta.id, exc)
        pergunta.status = "fila_humana"
        db.commit()
        return "fila_humana"

    pergunta.status = "respondida"
    pergunta.camada_resolvida = decisao["camada"]
    pergunta.resposta_enviada = decisao["resposta"]
    pergunta.precisa_auditoria = decisao.get("precisa_auditoria", False)
    pergunta.respondida_em = datetime.utcnow()
    db.add(AcaoRegistrada(
        conta_id=pergunta.conta_id,
        tipo="pergunta_respondida",
        sku=pergunta.sku,
        detalhe=f"Camada: {decisao['camada']}",
    ))
    db.commit()
    return "respondida"
