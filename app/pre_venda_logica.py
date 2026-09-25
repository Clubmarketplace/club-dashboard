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
1.5 Respostas padrão (tela "Respostas padrão") -> a IA escolhe, pelo
   SENTIDO, a resposta cadastrada que responde a pergunta (as de "todas
   as contas" + as do próprio produto). Cadastradas pela equipe, então
   não precisam de auditoria.
2. Manual técnico do SKU -> a IA (Claude) tenta formular uma resposta
   usando SÓ o conteúdo do manual. Se não tiver confiança suficiente
   (ou a chave não estiver configurada, ou a chamada falhar), cai pra
   camada 4. Quando responde, marca "precisa_auditoria" pra aparecer
   destacada na tela de resolvidas.
3. Política geral (palavra-chave) -> responde automático
4. Nada encontrado (ou nenhuma camada anterior teve confiança) -> fila humana
"""
from app.models import RespostaValidadaSku, ManualSku, PoliticaGeral, RespostaPadrao
from app.ia_pre_venda import (
    escolher_resposta_padrao,
    gerar_resposta_com_manual,
    encontrar_indice_resposta_similar,
    buscar_resposta_no_site_fabricante,
)

LIMITE_HISTORICO_CONSULTADO = 20  # não deixa a chamada de IA crescer sem limite pra SKUs com muito histórico


# Assuntos que NÃO podem virar resposta automática (desconto, troca,
# defeito, pedido já feito...). Proteção provisória por palavra-chave até
# a triagem por IA entrar: resposta de atendente pra esses assuntos não
# vai pro banco, então a IA nunca a reaproveita pra outro cliente.
_PALAVRAS_ASSUNTO_HUMANO = (
    "desconto", "descont", "precinho", "preco melhor", "melhor preco", "abaixa", "baixar o preco",
    "negocia", "cupom", "a vista", "pix",
    "troca", "trocar", "devolu", "defeito", "quebrad", "trincad", "estragad", "nao funciona",
    "reclama", "procon", "reclame aqui",
    "meu pedido", "nao chegou", "rastreio", "rastreamento", "cancelar", "cancelamento",
)


def _sem_acento_minusculo(texto: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", texto or "") if not unicodedata.combining(c)).lower()


def assunto_exige_humano(texto_pergunta: str) -> bool:
    """True se a pergunta é de um assunto que sempre deve ficar com humano."""
    texto = _sem_acento_minusculo(texto_pergunta)
    return any(p in texto for p in _PALAVRAS_ASSUNTO_HUMANO)


def chave_do_produto(sku: str | None, item_id: str | None) -> str | None:
    """
    Chave usada no banco de respostas: o SKU (vale pra todas as contas que
    vendem o produto) ou, se o anúncio não tiver SKU, o código do anúncio
    (MLB) -- aí vale só pra aquele anúncio, mas o aprendizado não para.
    """
    return (sku or "").strip() or (item_id or "").strip() or None


def buscar_resposta_validada(db, sku: str | None, texto_pergunta: str, item_id: str | None = None) -> RespostaValidadaSku | None:
    """
    Busca no histórico validado desse SKU (mais recentes primeiro) e
    pede pro Claude decidir se alguma pergunta anterior parecida já
    responde a pergunta atual. Devolve None se não tiver histórico,
    se a IA não estiver disponível, ou se nada do histórico servir.
    """
    # Procura pelo SKU e também pelo código do anúncio (respostas de
    # anúncios sem SKU ficam guardadas pelo MLB).
    chaves = [c for c in {(sku or "").strip(), (item_id or "").strip()} if c]
    if not chaves:
        return None

    historico = (
        db.query(RespostaValidadaSku)
        .filter(RespostaValidadaSku.sku.in_(chaves))
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


LIMITE_RESPOSTAS_PADRAO_CONSULTADAS = 60  # teto pra chamada de IA não crescer sem limite


def respostas_padrao_candidatas(db, sku: str | None, item_id: str | None) -> list[RespostaPadrao]:
    """As ativas que valem pra essa pergunta: as do próprio produto primeiro, depois as gerais."""
    chaves = [c for c in {(sku or "").strip(), (item_id or "").strip()} if c]
    do_produto = []
    if chaves:
        do_produto = (
            db.query(RespostaPadrao)
            .filter(RespostaPadrao.ativa.is_(True), RespostaPadrao.alcance == "produto",
                    RespostaPadrao.produto_chave.in_(chaves))
            .order_by(RespostaPadrao.id.desc())
            .all()
        )
    gerais = (
        db.query(RespostaPadrao)
        .filter(RespostaPadrao.ativa.is_(True), RespostaPadrao.alcance == "geral")
        .order_by(RespostaPadrao.usada.desc(), RespostaPadrao.id.desc())
        .all()
    )
    return (do_produto + gerais)[:LIMITE_RESPOSTAS_PADRAO_CONSULTADAS]


def para_ia(r: RespostaPadrao) -> dict:
    return {
        "tema": r.tema,
        "exemplos": [e.strip() for e in (r.exemplos or "").splitlines() if e.strip()],
        "resposta": r.resposta,
    }


def buscar_resposta_padrao(db, sku: str | None, texto_pergunta: str, item_id: str | None = None) -> RespostaPadrao | None:
    candidatas = respostas_padrao_candidatas(db, sku, item_id)
    if not candidatas:
        return None
    indice = escolher_resposta_padrao(texto_pergunta, [para_ia(r) for r in candidatas])
    return candidatas[indice] if indice is not None else None


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


def decidir_resposta(db, sku: str | None, texto_pergunta: str, titulo_produto: str | None = None, item_id: str | None = None) -> dict:
    """
    Roda as camadas em ordem e devolve o resultado da decisão:
    {"resposta": str | None, "camada": str | None, "status": str, "precisa_auditoria": bool}
    """
    resposta_validada = buscar_resposta_validada(db, sku, texto_pergunta, item_id=item_id)
    if resposta_validada:
        return {
            "resposta": resposta_validada.resposta,
            "camada": "resposta_validada",
            "status": "respondida",
            "precisa_auditoria": False,
        }

    padrao = buscar_resposta_padrao(db, sku, texto_pergunta, item_id=item_id)
    if padrao:
        padrao.usada = (padrao.usada or 0) + 1  # quem chama faz o commit
        return {
            "resposta": padrao.resposta,
            "camada": "resposta_padrao",
            "status": "respondida",
            "precisa_auditoria": False,  # texto escrito pela própria equipe
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
