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

# Regras de escrita que valem pra TODA resposta que vai pro comprador.
REGRAS_DE_ESCRITA = (
    "Escreva APENAS o texto final que será enviado ao comprador, pronto, em português do Brasil. "
    "Não comente o que você fez ou vai fazer (nada de 'vou pesquisar', 'deixe-me buscar', "
    "'encontrei', 'minha função'), não fale de fontes, sites ou pesquisa, e não use formatação "
    "(sem negrito, asteriscos, listas, títulos ou links). Nunca sugira ao comprador procurar o "
    "fabricante, outra loja ou outro canal. Se não tiver certeza da resposta, não escreva nada "
    f"além de: {SINALIZADOR_SEM_CONTEXTO}"
)

# Frases que denunciam "raciocínio" da IA ou resposta em dúvida. Se aparecerem,
# a resposta NÃO é enviada: a pergunta vai pra equipe (ver resposta_segura).
_FRASES_BLOQUEADAS = (
    # raciocínio / narração da pesquisa
    "minha funcao", "deixe-me", "deixa eu", "vou buscar", "vou pesquisar", "vou verificar",
    "pesquisei", "pesquisando", "encontrei", "achei a", "achei uma", "localizei", "segundo a pesquisa",
    "site oficial", "ficha tecnica que", "com base na pesquisa", "resultados da busca", "como ia", "como assistente",
    # dúvida / não-resposta
    "nao localizei", "nao encontrei", "nao consegui", "nao tenho essa", "nao tenho informac",
    "nao ha informac", "nao possuo", "nao sei", "nao foi possivel", "sem informac",
    "recomendo consultar", "recomendamos consultar", "consulte o fabricante", "contatar o fabricante",
    "contato com o fabricante", "entre em contato", "contatar diretamente", "site do fabricante",
    SINALIZADOR_SEM_CONTEXTO.lower(), "semcontexto", "sem contexto",
)


def _sem_acento(texto: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", texto or "") if not unicodedata.combining(c)).lower()


def _texto_final(resposta) -> str:
    """
    Só o texto FINAL da IA. Quando ela usa a busca na web, o conteúdo vem em
    pedaços: comentários antes da busca, a busca, e a resposta depois. Antes
    o sistema juntava tudo e o comprador recebia o "raciocínio" junto.
    Aqui pega só os textos que vêm DEPOIS do último resultado de ferramenta.
    """
    blocos = list(getattr(resposta, "content", None) or [])
    ultimo_resultado = -1
    for i, b in enumerate(blocos):
        tipo = str(getattr(b, "type", "") or "")
        if tipo.endswith("tool_result") or tipo in ("server_tool_use", "tool_use"):
            ultimo_resultado = i
    textos = [getattr(b, "text", "") for b in blocos[ultimo_resultado + 1:] if getattr(b, "type", None) == "text"]
    return "".join(textos).strip()


def limpar_formatacao(texto: str) -> str:
    """Tira markdown (negrito, títulos, listas, links) -- o Mercado Livre mostra texto puro."""
    import re
    t = texto or ""
    t = re.sub(r"\[([^\]]+)\]\((?:https?://)?[^)]+\)", r"\1", t)   # [texto](link) -> texto
    t = re.sub(r"https?://\S+", "", t)                              # links soltos
    t = re.sub(r"[*_`#>]+", "", t)                                   # **negrito**, # título, `código`
    t = re.sub(r"^\s*[-•]\s+", "", t, flags=re.MULTILINE)            # marcadores de lista
    t = re.sub(r"\s+", " ", t)                                       # tudo numa linha só
    return t.strip()


def resposta_segura(texto: str | None) -> str | None:
    """
    Última trava antes de enviar ao comprador. Devolve o texto limpo, ou None
    (= não enviar, vai pra equipe) se estiver vazio, longo demais, com cara de
    raciocínio da IA ou de resposta em dúvida.
    """
    if not texto or SINALIZADOR_SEM_CONTEXTO in texto.upper().replace(" ", "_"):
        return None  # a IA disse que não sabe (conferido ANTES da limpeza, que tira os "_")
    limpo = limpar_formatacao(texto)
    if not limpo or len(limpo) > 2000:
        return None
    normal = _sem_acento(limpo)
    if any(frase in normal for frase in _FRASES_BLOQUEADAS):
        logger.warning("Resposta da IA bloqueada pela trava de segurança: %s", limpo[:200])
        return None
    return limpo

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
        f"SOMENTE com o texto: {SINALIZADOR_SEM_CONTEXTO}\n" + REGRAS_DE_ESCRITA + "\n\n"
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

    # Só o texto final (sem o "raciocínio" da busca), limpo e conferido.
    return resposta_segura(_texto_final(resposta))


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
        f"SOMENTE com o texto: {SINALIZADOR_SEM_CONTEXTO}\n"
        "Se a pergunta for sobre COMPATIBILIDADE com um modelo/veículo específico e o "
        "fabricante não citar exatamente esse modelo, também responda só "
        f"{SINALIZADOR_SEM_CONTEXTO}.\n" + REGRAS_DE_ESCRITA + "\n\n"
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

    # Só o texto final (sem o "raciocínio" da busca), limpo e conferido.
    return resposta_segura(_texto_final(resposta))


def escolher_resposta_padrao(pergunta_texto: str, candidatas: list[dict]) -> int | None:
    """
    Respostas padrão (tela "Respostas padrão"): recebe as respostas ativas
    que valem pra essa pergunta -- [{"tema", "exemplos": [str], "resposta"}] --
    e pede pro Claude escolher a que responde a pergunta pelo SENTIDO
    ("tem NF?" = "vem com nota fiscal?"). Devolve o índice escolhido, ou
    None se nenhuma responder com segurança (ou sem chave / erro de API).
    Conservador de propósito: na dúvida, NENHUMA -- a pergunta segue pras
    próximas camadas ou pra equipe.
    """
    if not candidatas:
        return None
    cliente = _obter_cliente()
    if cliente is None:
        return None

    lista = "\n".join(
        f"{i}. Tema: {c['tema']}\n   Exemplos de pergunta: {' | '.join(c['exemplos']) or '(nenhum)'}\n   Resposta: {c['resposta']}"
        for i, c in enumerate(candidatas)
    )
    prompt_sistema = (
        "Você decide se uma pergunta de um comprador no Mercado Livre é respondida por "
        "alguma das RESPOSTAS PADRÃO abaixo, cadastradas pela loja. Compare pelo sentido, "
        "não pela palavra exata. Só escolha uma resposta se ela responder a pergunta por "
        "completo e sem risco de estar errada para esse caso; se a pergunta tiver um "
        "detalhe que a resposta não cobre, não escolha. Responda SOMENTE com o número da "
        "resposta escolhida (ex.: 2), ou SOMENTE com a palavra NENHUMA.\n\n" + lista
    )
    try:
        resposta = cliente.messages.create(
            model=CLAUDE_MODEL_PRE_VENDA,
            max_tokens=10,
            system=prompt_sistema,
            messages=[{"role": "user", "content": f"Pergunta do comprador: {pergunta_texto}"}],
        )
    except Exception as exc:
        logger.error("Falha ao chamar a API do Claude pra escolher resposta padrão: %s", exc)
        return None

    texto = "".join(b.text for b in resposta.content if getattr(b, "type", None) == "text").strip()
    if texto.isdigit() and int(texto) < len(candidatas):
        return int(texto)
    return None


def responder_com_dados_do_anuncio(pergunta_texto: str, ficha_anuncio: str) -> str | None:
    """
    Dados do anúncio ("manual automático"): responde usando SÓ o que está no
    próprio anúncio -- ficha técnica, variações (cor/tamanho/voltagem), estoque,
    garantia, envio (Full), descrição e compatibilidades. É a fonte mais
    confiável pra dúvida técnica e de compatibilidade (é o que o comprador vê).
    Devolve o texto pronto, ou None (vai pra próxima camada / equipe).
    """
    if not ficha_anuncio:
        return None
    cliente = _obter_cliente()
    if cliente is None:
        return None

    prompt_sistema = (
        "Você responde perguntas de pré-venda de um anúncio no Mercado Livre, num tom cordial "
        "e direto, em no máximo 2-3 frases, começando com 'Olá!'. Use APENAS os DADOS DO "
        "ANÚNCIO abaixo. Regras:\n"
        "- Cor, tamanho, voltagem ou modelo à venda: só os que aparecem nas VARIAÇÕES como "
        "disponível (ou no título/ficha, se o anúncio não tiver variações).\n"
        "- Compatibilidade com veículo/aparelho/modelo: só confirme se o modelo (e o ano, se "
        "perguntado) aparecer EXPLICITAMENTE nos dados (descrição, lista de aplicação, ficha "
        "ou compatibilidades). Se não aparecer, não responda.\n"
        "- Estoque: diga apenas se está disponível; nunca informe quantidade.\n"
        "- Nunca invente medida, material, prazo, garantia ou qualquer dado que não esteja nos "
        "dados. Se a pergunta for sobre outro assunto (desconto, pedido já feito, troca), ou os "
        "dados não responderem com certeza, responda só "
        f"{SINALIZADOR_SEM_CONTEXTO}.\n" + REGRAS_DE_ESCRITA +
        "\n\n--- DADOS DO ANÚNCIO ---\n" + ficha_anuncio
    )
    try:
        resposta = cliente.messages.create(
            model=CLAUDE_MODEL_PRE_VENDA,
            max_tokens=300,
            system=prompt_sistema,
            messages=[{"role": "user", "content": pergunta_texto}],
        )
    except Exception as exc:  # falha de API/rede: segue pras próximas camadas
        logger.error("Falha ao chamar a API do Claude com os dados do anúncio: %s", exc)
        return None
    return resposta_segura(_texto_final(resposta))
