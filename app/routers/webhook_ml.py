"""
Webhook que recebe as notificações do Mercado Livre. O Mercado Livre
manda um POST pra essa URL toda vez que algo relevante acontece (nova
pergunta, nova mensagem, novo pedido, reclamação, etc.), identificado
pelo campo 'topic' do payload.

Importante: o app do Mercado Livre é COMPARTILHADO com a Central
Financeira (Google Apps Script) -- só existe uma URL de notificação
cadastrada lá, então tudo chega aqui primeiro.
- 'questions' (pré-venda) é tratado por esse sistema.
- 'messages' (pós-venda) e 'claims' (reclamações/devoluções) também
  são tratados aqui -- caem numa fila humana com sugestão da IA, ou
  na fila própria de cancelamento quando identificados como pedido de
  cancelar (ver pos_venda_logica.py). A IA nunca envia nem executa
  nada sozinha nesses dois tópicos, só sugere.
- 'orders_v2' (pedidos, usado pra GMV/vendas) é repassado, sem alterar
  nada, pro Google Apps Script -- a Central Financeira continua
  recebendo exatamente o que recebia antes, só que por esse
  intermediário.
- Qualquer outro tópico fica só registrado, sem repassar e sem
  processar.

Essa URL de notificações é cadastrada separadamente do Redirect URI,
na aba de notificações/webhooks do app no Mercado Livre Devs -- as
duas coisas não são a mesma configuração.
"""
import json
import logging
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Conta, Pergunta, MensagemPosVenda, AcaoRegistrada, EventoWebhook
from app.config import GOOGLE_APPS_SCRIPT_WEBHOOK_URL
from app.ml_client import (
    MLAuthError,
    MLApiError,
    garantir_token_valido,
    buscar_pergunta,
    buscar_sku_do_item,
    buscar_titulo_do_item,
    buscar_mensagem,
    buscar_claim,
    enviar_resposta,
)
from app.pre_venda_logica import decidir_resposta
from app.pos_venda_logica import processar_mensagem_pos_venda

logger = logging.getLogger("webhook_ml")

router = APIRouter(prefix="/webhook", tags=["webhook"])

# Só esses tópicos vão pra Central Financeira (é só o que o Apps Script
# dela realmente usa, pra GMV/vendas). Qualquer coisa fora dessa lista
# E fora de 'questions'/'messages'/'claims' fica só registrada aqui,
# sem repassar.
TOPICOS_REPASSAR_PARA_APPS_SCRIPT = ["orders_v2"]


@router.post("/mercado-livre")
async def receber_notificacao(payload: dict, db: Session = Depends(get_db)):
    """
    Ponto de entrada único pra todas as notificações do Mercado Livre.
    Sempre responde 200 rapidamente (mesmo quando ignora ou falha
    internamente) — o Mercado Livre reenvia notificações que não
    recebem 200. TODA notificação é registrada em EventoWebhook, com o
    payload bruto e o resultado -- é o log de auditoria, consultável
    pelo dashboard sem precisar abrir o Railway.
    """
    topic = payload.get("topic")
    resultado: dict = {}

    try:
        if topic == "questions":
            resultado = await _processar_pergunta(payload, db)
        elif topic == "messages":
            resultado = await _processar_mensagem(payload, db)
        elif topic == "claims":
            resultado = await _processar_claim(payload, db)
        elif topic in TOPICOS_REPASSAR_PARA_APPS_SCRIPT:
            resultado = await _repassar_para_apps_script(payload, topic)
        else:
            logger.info("Tópico '%s' recebido, ainda sem tratamento.", topic)
            resultado = {"status": "ignorado", "motivo": f"tópico '{topic}' ainda não tem módulo próprio"}
    except (MLAuthError, MLApiError) as exc:
        logger.error("Falha ao processar notificação (tópico '%s'): %s", topic, exc)
        resultado = {"status": "erro_interno", "detalhe": str(exc)}
    except Exception as exc:  # nunca deixa uma falha inesperada derrubar o registro do evento
        logger.error("Erro inesperado ao processar notificação (tópico '%s'): %s", topic, exc)
        resultado = {"status": "erro_inesperado", "detalhe": str(exc)}

    try:
        db.add(EventoWebhook(topico=topic, payload_bruto=json.dumps(payload, ensure_ascii=False), resultado=json.dumps(resultado, ensure_ascii=False)))
        db.commit()
    except Exception as exc:
        logger.error("Falha ao registrar EventoWebhook: %s", exc)

    return resultado


async def _repassar_para_apps_script(payload: dict, topic: str | None) -> dict:
    """
    Repassa a notificação (sem alterar nada nela) pro Google Apps
    Script da Central Financeira, que é quem realmente sabe tratar
    esse tópico (ex: atualizar GMV a partir de 'orders_v2'). Se a URL
    não estiver configurada, ou o repasse falhar, só loga -- nunca
    deixa cair o processamento da notificação de pré-venda por causa
    disso, já que são responsabilidades separadas.
    """
    if not GOOGLE_APPS_SCRIPT_WEBHOOK_URL:
        logger.warning("Tópico '%s' recebido mas GOOGLE_APPS_SCRIPT_WEBHOOK_URL não configurada -- notificação descartada.", topic)
        return {"status": "ignorado", "motivo": f"tópico '{topic}' não é pra esse sistema, e não há URL de repasse configurada"}

    try:
        async with httpx.AsyncClient(timeout=10) as cliente:
            resposta = await cliente.post(GOOGLE_APPS_SCRIPT_WEBHOOK_URL, json=payload)
        return {"status": "repassado", "topico": topic, "status_apps_script": resposta.status_code}
    except Exception as exc:
        logger.error("Falha ao repassar notificação (tópico '%s') pro Apps Script: %s", topic, exc)
        return {"status": "erro_ao_repassar", "topico": topic, "detalhe": str(exc)}


async def _processar_pergunta(payload: dict, db: Session) -> dict:
    resource = payload.get("resource", "")
    ml_user_id = str(payload.get("user_id", ""))
    question_id = resource.rstrip("/").split("/")[-1]

    if not question_id.isdigit():
        return {"status": "ignorado", "motivo": f"resource inesperado: {resource}"}

    conta = db.query(Conta).filter(Conta.ml_user_id == ml_user_id).first()
    if conta is None or not conta.access_token:
        return {"status": "conta_nao_conectada", "ml_user_id": ml_user_id}

    # Evita duplicar se o Mercado Livre reenviar a mesma notificação
    ja_existe = db.query(Pergunta).filter(Pergunta.ml_question_id == question_id).first()
    if ja_existe:
        return {"status": "ja_processada", "pergunta_id": ja_existe.id}

    access_token = garantir_token_valido(conta, db)
    dados_pergunta = buscar_pergunta(access_token, question_id)

    texto = dados_pergunta.get("text", "")
    item_id = dados_pergunta.get("item_id")
    sku = buscar_sku_do_item(access_token, item_id) if item_id else None
    titulo_produto = buscar_titulo_do_item(access_token, item_id) if item_id else None

    pergunta = Pergunta(
        conta_id=conta.id,
        ml_question_id=question_id,
        item_id=item_id,
        sku=sku,
        texto=texto,
        status="pendente",
    )
    db.add(pergunta)
    db.commit()
    db.refresh(pergunta)

    # Já tinha resposta no Mercado Livre antes da gente processar --
    # normalmente o assistente nativo do próprio ML foi mais rápido.
    # Não tem erro nenhum aqui, só não tentamos responder de novo (o
    # Mercado Livre recusaria com 403 "Action not allowed").
    if dados_pergunta.get("status") == "ANSWERED":
        pergunta.status = "respondida_externamente"
        pergunta.camada_resolvida = "externo_ou_ml_nativo"
        pergunta.respondida_em = datetime.utcnow()
        db.commit()
        return {"status": "ja_respondida_externamente", "pergunta_id": pergunta.id}

    decisao = decidir_resposta(db, sku, texto, titulo_produto)

    if decisao["status"] == "respondida":
        try:
            enviar_resposta(access_token, question_id, decisao["resposta"])
        except MLApiError as exc:
            # A decisão foi automática, mas o envio falhou de verdade
            # (ex: pergunta já foi apagada) — não perde a pergunta,
            # só deixa marcada pra revisão humana em vez de travar.
            pergunta.status = "fila_humana"
            db.commit()
            logger.error("Falha ao enviar resposta automática (pergunta %s): %s", pergunta.id, exc)
            return {"status": "erro_ao_responder", "pergunta_id": pergunta.id, "detalhe": str(exc)}

        pergunta.status = "respondida"
        pergunta.camada_resolvida = decisao["camada"]
        pergunta.resposta_enviada = decisao["resposta"]
        pergunta.precisa_auditoria = decisao.get("precisa_auditoria", False)
        pergunta.respondida_em = datetime.utcnow()
        db.add(AcaoRegistrada(
            conta_id=conta.id,
            tipo="pergunta_respondida",
            sku=sku,
            detalhe=f"Camada: {decisao['camada']}",
        ))
    else:
        pergunta.status = "fila_humana"

    db.commit()

    return {
        "status": "processada",
        "pergunta_id": pergunta.id,
        "resultado": pergunta.status,
        "camada": pergunta.camada_resolvida,
    }


async def _processar_mensagem(payload: dict, db: Session) -> dict:
    """
    Tópico 'messages' -- mensagem de pós-venda. Sempre cai em fila
    humana (com sugestão da IA) ou na fila de cancelamento, nunca
    responde sozinho.
    """
    resource = payload.get("resource", "")
    ml_user_id = str(payload.get("user_id", ""))
    message_id = resource.rstrip("/").split("/")[-1]

    conta = db.query(Conta).filter(Conta.ml_user_id == ml_user_id).first()
    if conta is None or not conta.access_token:
        return {"status": "conta_nao_conectada", "ml_user_id": ml_user_id}

    ja_existe = db.query(MensagemPosVenda).filter(MensagemPosVenda.ml_recurso_id == message_id).first()
    if ja_existe:
        return {"status": "ja_processada", "id": ja_existe.id}

    access_token = garantir_token_valido(conta, db)
    dados = buscar_mensagem(access_token, message_id)

    # Campo exato ainda a confirmar com um teste real -- registrando o
    # payload bruto no log pra ajustar rápido se o formato vier diferente.
    logger.info("Mensagem de pós-venda recebida (id=%s): %s", message_id, dados)
    texto = (dados.get("text") or {}).get("plain", "") if isinstance(dados.get("text"), dict) else str(dados.get("text", ""))

    resultado = processar_mensagem_pos_venda(
        db, conta_id=conta.id, ml_recurso_id=message_id, tipo="mensagem", texto=texto,
    )
    return {"status": "processada", **resultado}


async def _processar_claim(payload: dict, db: Session) -> dict:
    """
    Tópico 'claims' -- reclamação/devolução. Mesma regra da mensagem:
    sempre fila humana (ou cancelamento), nunca resolve sozinho.
    """
    resource = payload.get("resource", "")
    ml_user_id = str(payload.get("user_id", ""))
    claim_id = resource.rstrip("/").split("/")[-1]

    conta = db.query(Conta).filter(Conta.ml_user_id == ml_user_id).first()
    if conta is None or not conta.access_token:
        return {"status": "conta_nao_conectada", "ml_user_id": ml_user_id}

    ja_existe = db.query(MensagemPosVenda).filter(MensagemPosVenda.ml_recurso_id == claim_id).first()
    if ja_existe:
        return {"status": "ja_processada", "id": ja_existe.id}

    access_token = garantir_token_valido(conta, db)
    dados = buscar_claim(access_token, claim_id)

    # Formato exato do payload de claim também a confirmar com teste real.
    logger.info("Reclamação recebida (id=%s): %s", claim_id, dados)
    texto = dados.get("reason_detail") or dados.get("reason") or str(dados)
    order_id = dados.get("resource_id") or dados.get("order_id")

    resultado = processar_mensagem_pos_venda(
        db, conta_id=conta.id, ml_recurso_id=claim_id, tipo="reclamacao", texto=texto, order_id=order_id,
    )
    return {"status": "processada", **resultado}
