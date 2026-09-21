"""
IA de pós-venda: diferente da pré-venda, aqui a IA NUNCA envia nada
sozinha -- só gera uma sugestão de texto (resposta_sugerida) pra
agilizar o atendente, que decide se usa, edita ou ignora. Isso vale
tanto pra mensagens/reclamações comuns quanto pra pedidos de
cancelamento.

Mesmo design conservador da pré-venda: qualquer falha (sem chave, API
fora do ar) devolve None, e quem chama trata como "sem sugestão" --
o atendente responde do zero nesse caso, nunca fica sem poder agir.
"""
import logging

from app.config import ANTHROPIC_API_KEY, CLAUDE_MODEL_PRE_VENDA

logger = logging.getLogger("ia_pos_venda")

_cliente = None


def _obter_cliente():
    global _cliente
    if not ANTHROPIC_API_KEY:
        return None
    if _cliente is None:
        import anthropic
        _cliente = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _cliente


def gerar_sugestao_pos_venda(texto_mensagem: str, tipo: str) -> str | None:
    """
    Rascunho de resposta pra uma mensagem de pós-venda comum ou
    reclamação/devolução -- SEMPRE só uma sugestão, nunca enviada
    sozinha. tipo: "mensagem" ou "reclamacao".
    """
    cliente = _obter_cliente()
    if cliente is None:
        return None

    contexto = (
        "uma reclamação/devolução aberta pelo comprador (tópico 'claims')"
        if tipo == "reclamacao"
        else "uma mensagem enviada pelo comprador depois da compra (tópico 'messages')"
    )
    prompt_sistema = (
        f"Você rascunha uma sugestão de resposta pra {contexto} de um pedido no "
        "Mercado Livre, em português do Brasil, tom cordial e profissional, em "
        "no máximo 3-4 frases. Essa sugestão será revisada por um atendente "
        "humano antes de ser enviada -- nunca vai sozinha. Não prometa prazo, "
        "reembolso, troca ou qualquer ação concreta que você não tem certeza "
        "que a empresa pode cumprir -- prefira frases como 'vou verificar e já "
        "te retorno' quando não tiver informação suficiente pra prometer algo "
        "específico."
    )

    try:
        resposta = cliente.messages.create(
            model=CLAUDE_MODEL_PRE_VENDA,
            max_tokens=300,
            system=prompt_sistema,
            messages=[{"role": "user", "content": texto_mensagem}],
        )
    except Exception as exc:
        logger.error("Falha ao gerar sugestão de pós-venda: %s", exc)
        return None

    texto = "".join(
        bloco.text for bloco in resposta.content if getattr(bloco, "type", None) == "text"
    ).strip()
    return texto or None


def gerar_sugestao_cancelamento(texto_pedido: str) -> str | None:
    """
    Rascunho de resposta pra um pedido de cancelamento -- SEMPRE só
    orienta o comprador sobre o processo (nunca confirma o
    cancelamento em si, já que isso ainda depende de decisão humana).
    Serve também de material acumulado pra, no futuro, treinar a IA a
    reconhecer o padrão de resposta esperado.
    """
    cliente = _obter_cliente()
    if cliente is None:
        return None

    prompt_sistema = (
        "Você rascunha uma sugestão de resposta pra um pedido de cancelamento "
        "de compra no Mercado Livre, em português do Brasil, tom cordial. "
        "IMPORTANTE: você NUNCA confirma que o cancelamento foi feito ou será "
        "feito -- isso depende de um atendente humano decidir e executar. "
        "Sua resposta deve reconhecer o pedido e explicar que a equipe vai "
        "avaliar e retornar em breve, no máximo 2-3 frases. Essa sugestão "
        "será revisada por um atendente antes de qualquer envio."
    )

    try:
        resposta = cliente.messages.create(
            model=CLAUDE_MODEL_PRE_VENDA,
            max_tokens=200,
            system=prompt_sistema,
            messages=[{"role": "user", "content": texto_pedido}],
        )
    except Exception as exc:
        logger.error("Falha ao gerar sugestão de cancelamento: %s", exc)
        return None

    texto = "".join(
        bloco.text for bloco in resposta.content if getattr(bloco, "type", None) == "text"
    ).strip()
    return texto or None
