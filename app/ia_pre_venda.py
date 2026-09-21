"""
IA de pré-venda: duas funções, uma pra cada camada que usa o Claude.

- encontrar_indice_resposta_similar: camada 1 (histórico validado) --
  decide se uma pergunta nova já foi respondida antes, entre as
  respostas aprovadas pra esse SKU.
- gerar_resposta_com_manual: camada 2 (manual do SKU) -- formula uma
  resposta nova usando só o manual técnico como fonte.

Design deliberadamente conservador nas duas: QUALQUER falha (sem
chave configurada, API fora do ar, sinalizador de "não sei") faz o
módulo devolver None -- o chamador (pre_venda_logica.decidir_resposta)
trata None como "essa camada não resolveu" e segue pra próxima, ou
cai pra fila humana. Isso garante que nunca vamos mandar uma resposta
incompleta ou inventada pro comprador só porque a IA não tinha certeza.
"""
import logging

from app.config import ANTHROPIC_API_KEY, CLAUDE_MODEL_PRE_VENDA

logger = logging.getLogger("ia_pre_venda")

SINALIZADOR_SEM_CONTEXTO = "SEM_CONTEXTO_SUFICIENTE"

_cliente = None


def _obter_cliente():
    """
    Cria o cliente Anthropic sob demanda (não na importação do módulo),
    pra o resto do sistema continuar funcionando normalmente mesmo sem
    a chave configurada -- só essas camadas específicas ficam inativas.
    """
    global _cliente
    if not ANTHROPIC_API_KEY:
        return None
    if _cliente is None:
        import anthropic  # import local -- evita erro de import se o pacote não estiver instalado ainda
        _cliente = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _cliente


def encontrar_indice_resposta_similar(pergunta_texto: str, historico: list[dict]) -> int | None:
    """
    Camada 1 (histórico validado): recebe uma lista de perguntas já
    respondidas e aprovadas pra esse SKU -- [{"pergunta": str,
    "resposta": str}, ...] -- e pede pro Claude decidir se alguma
    delas já responde a pergunta NOVA com segurança. Devolve o índice
    da que serve, ou None se nenhuma servir -- aí o sistema segue pras
    próximas camadas, em vez de reaproveitar uma resposta errada só
    porque é do mesmo produto.
    """
    if not historico:
        return None

    cliente = _obter_cliente()
    if cliente is None:
        return None

    lista_formatada = "\n".join(
        f"{i}. Pergunta anterior: {item['pergunta']}\n   Resposta que foi usada: {item['resposta']}"
        for i, item in enumerate(historico)
    )
    prompt_sistema = (
        "Você decide se uma pergunta NOVA de um comprador no Mercado Livre já foi "
        "respondida antes, num histórico de perguntas parecidas sobre o mesmo produto. "
        "Se alguma resposta do histórico abaixo responde a pergunta nova com segurança, "
        "sem deixar nada a desejar, responda SOMENTE com o número dela -- por exemplo: 2. "
        "Se nenhuma resposta do histórico responder bem a pergunta nova, responda SOMENTE "
        f"com a palavra NENHUMA.\n\n{lista_formatada}"
    )

    try:
        resposta = cliente.messages.create(
            model=CLAUDE_MODEL_PRE_VENDA,
            max_tokens=10,
            system=prompt_sistema,
            messages=[{"role": "user", "content": f"Pergunta nova: {pergunta_texto}"}],
        )
    except Exception as exc:
        logger.error("Falha ao chamar a API do Claude pra comparar com o histórico: %s", exc)
        return None

    texto = "".join(
        bloco.text for bloco in resposta.content if getattr(bloco, "type", None) == "text"
    ).strip()

    if texto.isdigit() and int(texto) < len(historico):
        return int(texto)
    return None


def gerar_resposta_com_manual(pergunta_texto: str, manual_titulo: str | None, manual_conteudo: str) -> str | None:
    """
    Camada 2 (manual do SKU): devolve o texto de resposta pronto pra
    enviar, ou None quando não deve responder automaticamente (sem
    chave, erro de API, ou o próprio Claude sinalizou que o manual não
    é suficiente).
    """
    cliente = _obter_cliente()
    if cliente is None:
        return None

    prompt_sistema = (
        "Você responde perguntas de pré-venda de um anúncio no Mercado Livre, "
        "em português do Brasil, num tom cordial e direto, em no máximo 2-3 frases. "
        "Use APENAS as informações do manual técnico abaixo -- nunca invente "
        "especificação, prazo, compatibilidade, cor, tamanho ou qualquer dado que "
        "não esteja explicitamente no manual. Se o manual não contiver informação "
        "suficiente pra responder essa pergunta com segurança, responda EXATA e "
        f"SOMENTE com o texto: {SINALIZADOR_SEM_CONTEXTO}\n\n"
        f"--- MANUAL TÉCNICO: {manual_titulo or '(sem título)'} ---\n{manual_conteudo}"
    )

    try:
        resposta = cliente.messages.create(
            model=CLAUDE_MODEL_PRE_VENDA,
            max_tokens=300,
            system=prompt_sistema,
            messages=[{"role": "user", "content": pergunta_texto}],
        )
    except Exception as exc:  # qualquer falha de API/rede -- nunca deixa a pergunta sem tratamento
        logger.error("Falha ao chamar a API do Claude pra pré-venda: %s", exc)
        return None

    texto = "".join(
        bloco.text for bloco in resposta.content if getattr(bloco, "type", None) == "text"
    ).strip()

    if not texto or SINALIZADOR_SEM_CONTEXTO in texto:
        return None
    return texto


def buscar_resposta_no_site_fabricante(pergunta_texto: str, titulo_produto: str | None, sku: str | None) -> str | None:
    """
    Camada 2.5 (busca no site do fabricante): só é chamada quando NÃO
    existe manual próprio cadastrado pra esse SKU. Deixa o Claude usar
    a ferramenta de busca na web (restrita, ele decide quais sites
    abrir) pra achar a especificação técnica real do produto e
    responder com base nisso.

    Mais arriscado que o manual próprio (site errado, página tirada do
    ar, versão errada do produto) -- por isso quem chama essa função
    (pre_venda_logica.decidir_resposta) sempre marca a resposta como
    "precisa_auditoria", nunca dispensa revisão humana depois.

    Devolve None (cai pra próxima camada) se não tiver produto
    suficiente pra pesquisar, se a API falhar, ou se o Claude não
    achar informação confiável o bastante.
    """
    if not titulo_produto:
        return None  # sem nome/modelo do produto, não dá pra pesquisar direito

    cliente = _obter_cliente()
    if cliente is None:
        return None

    identificador = f"{titulo_produto}" + (f" (SKU/código: {sku})" if sku else "")
    prompt_sistema = (
        "Você responde perguntas de pré-venda de um anúncio no Mercado Livre, "
        "em português do Brasil, num tom cordial e direto, em no máximo 2-3 frases. "
        "Você tem acesso a busca na web -- use-a pra procurar a ficha técnica, manual "
        "ou página oficial do FABRICANTE do produto abaixo (não do próprio Mercado "
        "Livre nem de lojas revendedoras -- a fonte tem que ser o fabricante). "
        "Baseie a resposta SOMENTE no que encontrar no site oficial do fabricante -- "
        "nunca invente especificação, compatibilidade, cor, tamanho, voltagem ou "
        "qualquer dado que não esteja explicitamente lá. Se não encontrar o site do "
        "fabricante, se o produto exato não bater com o que achou, ou se a informação "
        "encontrada não for suficiente pra responder com segurança, responda EXATA e "
        f"SOMENTE com o texto: {SINALIZADOR_SEM_CONTEXTO}\n\n"
        f"--- PRODUTO: {identificador} ---"
    )

    try:
        resposta = cliente.messages.create(
            model=CLAUDE_MODEL_PRE_VENDA,
            max_tokens=500,
            system=prompt_sistema,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
            messages=[{"role": "user", "content": pergunta_texto}],
        )
    except Exception as exc:
        logger.error("Falha ao chamar a API do Claude pra busca no site do fabricante: %s", exc)
        return None

    texto = "".join(
        bloco.text for bloco in resposta.content if getattr(bloco, "type", None) == "text"
    ).strip()

    if not texto or SINALIZADOR_SEM_CONTEXTO in texto:
        return None
    return texto
