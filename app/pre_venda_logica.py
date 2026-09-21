"""
Lógica de decisão de pré-venda: decide como responder (ou não) uma
pergunta recebida, seguindo a ordem de camadas combinada:

1. Histórico de respostas já validadas pra esse SKU -- o Claude
   confere se alguma pergunta anterior parecida já tem uma resposta
   que serve pra essa também. Esse histórico cresce sozinho: toda vez
   que um atendente responde manualmente uma pergunta na fila humana,
   ela entra aqui automaticamente (ver routers/pre_venda.py) -- assim
   a próxima pergunta parecida sobre o mesmo produto já sai
   automática, sem precisar de humano nem chamar a camada 2 de novo.
2. Manual técnico do SKU -> a IA (Claude) tenta formular uma resposta
   usando SÓ o conteúdo do manual. Se não tiver confiança suficiente
   (ou a chave não estiver configurada, ou a chamada falhar), cai pra
   camada 4. Quando responde, marca "precisa_auditoria" pra aparecer
   destacada na tela de resolvidas.
3. Política geral (palavra-chave) -> responde automático
4. Nada encontrado (ou nenhuma camada anterior teve confiança) -> fila humana
"""
from app.models import RespostaValidadaSku, ManualSku, PoliticaGeral
from app.ia_pre_venda import (
    gerar_resposta_com_manual,
    encontrar_indice_resposta_similar,
    buscar_resposta_no_site_fabricante,
)

LIMITE_HISTORICO_CONSULTADO = 20  # não deixa a chamada de IA crescer sem limite pra SKUs com muito histórico


def buscar_resposta_validada(db, sku: str | None, texto_pergunta: str) -> RespostaValidadaSku | None:
    """
    Busca no histórico validado desse SKU (mais recentes primeiro) e
    pede pro Claude decidir se alguma pergunta anterior parecida já
    responde a pergunta atual. Devolve None se não tiver histórico,
    se a IA não estiver disponível, ou se nada do histórico servir.
    """
    if not sku:
        return None

    historico = (
        db.query(RespostaValidadaSku)
        .filter(RespostaValidadaSku.sku == sku)
        .order_by(RespostaValidadaSku.criado_em.desc())
        .limit(LIMITE_HISTORICO_CONSULTADO)
        .all()
    )
    if not historico:
        return None

    itens = [{"pergunta": h.pergunta_exemplo or "", "resposta": h.resposta} for h in historico]
    indice = encontrar_indice_resposta_similar(texto_pergunta, itens)
    if indice is None:
        return None
    return historico[indice]


def buscar_manual_por_sku(db, sku: str | None) -> ManualSku | None:
    if not sku:
        return None
    return (
        db.query(ManualSku)
        .filter(ManualSku.sku == sku)
        .order_by(ManualSku.criado_em.desc())
        .first()
    )


def buscar_politica_geral(db, texto_pergunta: str) -> PoliticaGeral | None:
    """
    Busca simples por palavra-chave: se alguma palavra-chave cadastrada
    aparece no texto da pergunta (case-insensitive), casa com aquela
    política. Não é semântico — é literal, de propósito, pra ser
    previsível e fácil de auditar.
    """
    texto_lower = texto_pergunta.lower()
    politicas = db.query(PoliticaGeral).all()
    for politica in politicas:
        palavras = [p.strip().lower() for p in politica.palavras_chave.split(",") if p.strip()]
        if any(palavra in texto_lower for palavra in palavras):
            return politica
    return None


def decidir_resposta(db, sku: str | None, texto_pergunta: str, titulo_produto: str | None = None) -> dict:
    """
    Roda as camadas em ordem e devolve o resultado da decisão:
    {"resposta": str | None, "camada": str | None, "status": str, "precisa_auditoria": bool}
    """
    resposta_validada = buscar_resposta_validada(db, sku, texto_pergunta)
    if resposta_validada:
        return {
            "resposta": resposta_validada.resposta,
            "camada": "resposta_validada",
            "status": "respondida",
            "precisa_auditoria": False,
        }

    manual = buscar_manual_por_sku(db, sku)
    if manual:
        resposta_ia = gerar_resposta_com_manual(texto_pergunta, manual.titulo, manual.conteudo)
        if resposta_ia:
            return {
                "resposta": resposta_ia,
                "camada": "manual_sku_ia",
                "status": "respondida",
                "precisa_auditoria": True,  # candidata da IA -- revisar depois, mesmo já enviada
            }
        # IA sem confiança suficiente (ou API indisponível) -- cai pra
        # fila humana, com o manual disponível pra quem for responder.
        return {"resposta": None, "camada": None, "status": "fila_humana", "precisa_auditoria": False}

    # Não tem manual próprio cadastrado -- antes de ir pra política geral
    # ou fila humana, tenta buscar a especificação direto no site do
    # fabricante. Só entra aqui quando NÃO existe manual (se existir mas
    # a IA não teve confiança nele, já vai pra fila humana acima -- mais
    # seguro que tentar a internet depois de uma fonte própria falhar).
    resposta_fabricante = buscar_resposta_no_site_fabricante(texto_pergunta, titulo_produto, sku)
    if resposta_fabricante:
        return {
            "resposta": resposta_fabricante,
            "camada": "busca_site_fabricante",
            "status": "respondida",
            "precisa_auditoria": True,  # veio da internet -- sempre revisar depois
        }

    politica = buscar_politica_geral(db, texto_pergunta)
    if politica:
        return {
            "resposta": politica.resposta,
            "camada": "politica_geral",
            "status": "respondida",
            "precisa_auditoria": False,
        }

    return {"resposta": None, "camada": None, "status": "fila_humana", "precisa_auditoria": False}
