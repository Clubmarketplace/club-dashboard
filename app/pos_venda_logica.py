"""
Lógica de pós-venda: recebe uma mensagem (tópico 'messages') ou
reclamação (tópico 'claims') já buscada do Mercado Livre, decide se é
um pedido de cancelamento (fica numa fila própria, PedidoCancelamento)
ou uma mensagem/reclamação comum (fila_humana de MensagemPosVenda) --
em ambos os casos, a IA só RASCUNHA uma sugestão, nunca envia nem
executa nada sozinha.

Detecção de cancelamento é por palavra-chave, de propósito (igual
política geral da pré-venda) -- previsível e fácil de auditar, sem
depender de uma chamada de IA só pra classificar.
"""
from app.models import MensagemPosVenda, PedidoCancelamento
from app.ia_pos_venda import gerar_sugestao_pos_venda, gerar_sugestao_cancelamento

PALAVRAS_CANCELAMENTO = [
    "cancelar", "cancelamento", "desistir da compra", "desistência",
    "não quero mais o produto", "quero desfazer a compra", "quero estornar",
]


def eh_pedido_cancelamento(texto: str) -> bool:
    texto_lower = texto.lower()
    return any(palavra in texto_lower for palavra in PALAVRAS_CANCELAMENTO)


def processar_mensagem_pos_venda(db, conta_id: int, ml_recurso_id: str, tipo: str, texto: str, sku: str | None = None, pack_id: str | None = None, order_id: str | None = None) -> dict:
    """
    tipo: "mensagem" (topic messages) ou "reclamacao" (topic claims).
    Devolve {"destino": "cancelamento" | "pos_venda", "id": int}.
    """
    if eh_pedido_cancelamento(texto):
        sugestao = gerar_sugestao_cancelamento(texto)
        pedido = PedidoCancelamento(
            conta_id=conta_id,
            order_id=order_id,
            sku=sku,
            texto_pedido=texto,
            resposta_sugerida=sugestao,
            status="pendente",
        )
        db.add(pedido)
        db.commit()
        db.refresh(pedido)
        return {"destino": "cancelamento", "id": pedido.id}

    sugestao = gerar_sugestao_pos_venda(texto, tipo)
    mensagem = MensagemPosVenda(
        conta_id=conta_id,
        ml_recurso_id=ml_recurso_id,
        tipo=tipo,
        pack_id=pack_id,
        order_id=order_id,
        sku=sku,
        texto=texto,
        resposta_sugerida=sugestao,
        status="fila_humana",
    )
    db.add(mensagem)
    db.commit()
    db.refresh(mensagem)
    return {"destino": "pos_venda", "id": mensagem.id}
