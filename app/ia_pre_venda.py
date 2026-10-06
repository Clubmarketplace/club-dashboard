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
    f"além de: {SINALIZADOR_SEM_CONTEXTO}. "
    "IMPORTANTE: se a informação pedida NÃO estiver nos dados, NUNCA responda dizendo que ela não "
    "consta, não é informada ou não foi encontrada, e NUNCA mande o comprador ver a embalagem, o "
    "manual, o site ou falar com o fabricante: nesse caso responda somente "
    f"{SINALIZADOR_SEM_CONTEXTO} (assim o sistema procura em outra fonte)."
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


# Padrões (regex, no texto sem acento e minúsculo) que pegam a IDEIA de "não sei /
# procure em outro lugar", mesmo escrita de outro jeito. Caso real que passou pela
# lista de frases: "Os dados do anúncio não informam a cobertura (...) recomendo
# verificar na embalagem ou entrar em contato diretamente com o fabricante".
_PADROES_BLOQUEADOS = tuple(__import__("re").compile(p) for p in (
    # "não informa / não consta / não menciona..." -> a IA está dizendo que não sabe
    r"\bnao (informa|informam|informado|informada|consta|constam|menciona|mencionam|mencionado"
    r"|especifica|especificam|especificado|detalha|detalham|indica|indicam|traz|trazem|apresenta|apresentam"
    r"|disponibiliza|disponibilizam|possui informac|possuimos informac|temos informac|temos essa|temos esse)\b",
    r"\bnao (ha|existe|existem) (essa |esta |a |o )?(informac|dado|detalhe|mencao)",
    r"\b(dados|ficha|descricao|informacoes) do anuncio\b",          # fala das próprias fontes
    r"\b(anuncio|descricao|ficha) (nao|nada)\b",
    # manda o comprador procurar em outro lugar
    r"\b(verificar|verifique|confira|conferir|consultar|consulte|checar|cheque|olhar|veja)\b.{0,50}"
    r"\b(embalagem|rotulo|fabricante|manual|site|fornecedor)\b",
    r"\b(entrar|entre|entrando) em contato\b",
    r"\bcontato (direto|diretamente)\b",
    r"\bpara (maior |mais )?(precisao|seguranca na informacao)\b",
    # "não consigo confirmar / não posso ajudar / desculpe não poder ajudar"
    r"\bnao (consigo|conseguimos|posso|podemos) (confirmar|informar|ajudar|responder|garantir|dizer|afirmar)\b",
    r"\bnao poder (ajudar|informar|confirmar|responder)\b",
    r"\bdesculpe\b",
))


def _estilo() -> str:
    """Tom e tamanho escolhidos em Calibrar IA (padrão: cordial e direto, 2-3 frases)."""
    try:
        from app import config_ia
        return config_ia.estilo()
    except Exception:
        return "num tom cordial e direto, em no máximo 2-3 frases"


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
    if any(frase in normal for frase in _FRASES_BLOQUEADAS) or any(p.search(normal) for p in _PADROES_BLOQUEADOS):
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
        from app import saude_ia
        saude_ia.registrar_falha(str(exc))
        return None
    from app import saude_ia
    saude_ia.registrar_sucesso()

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
        f"em português do Brasil, {_estilo()}. "
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
        from app import saude_ia
        saude_ia.registrar_falha(str(exc))
        return None
    from app import saude_ia
    saude_ia.registrar_sucesso()

    # Só o texto final (sem o "raciocínio" da busca), limpo e conferido.
    return resposta_segura(_texto_final(resposta))


# O que cada degrau da escada de pesquisa pode usar (ver config_ia.DEGRAUS).
_FONTES_DO_DEGRAU = {
    "fabricante": "SOMENTE o site oficial do fabricante/marca do produto (domínio da própria marca).",
    "pdf": ("SOMENTE manual, ficha técnica, catálogo ou boletim técnico em PDF do fabricante, deste "
            "mesmo modelo -- o PDF pode estar hospedado em qualquer site, inclusive de loja."),
    "lojas": ("SOMENTE a página do produto (descrição/ficha técnica) em lojas grandes e conhecidas do Brasil, "
              "como Leroy Merlin, Magazine Luiza, Casas Bahia, Amazon, Americanas ou Carrefour "
              "(só a ficha técnica/descrição, nunca as perguntas de compradores)."),
    "videos": ("SOMENTE vídeos do canal oficial do fabricante (título, descrição ou transcrição). "
               "Se a informação só aparecer na imagem do vídeo, você não tem como saber: não responda."),
    "tecnicos": ("SOMENTE sites técnicos especializados no tipo de produto: catálogos de autopeças "
                 "(aplicação, código original), distribuidores e portais técnicos."),
    "aberta": "Qualquer site confiável.",
}
_DEGRAUS_QUE_EXIGEM_DUAS_FONTES = ("lojas", "videos", "tecnicos", "aberta")


def _pesquisar_degrau(cliente, degrau: str, pergunta_texto: str, identificador: str, detalhes: str) -> str | None:
    """Uma tentativa de pesquisa, limitada às fontes de UM degrau. Devolve o texto pronto ou None."""
    from app import config_ia
    cfg = config_ia.obter()
    duas = (cfg["duas_fontes"] and degrau in _DEGRAUS_QUE_EXIGEM_DUAS_FONTES)
    prompt_sistema = (
        "Você responde perguntas de pré-venda de um anúncio no Mercado Livre, "
        f"em português do Brasil, {config_ia.estilo()}. "
        "Você tem acesso a busca na web. Procure a informação deste produto EXATO (mesma marca e "
        f"modelo) usando {_FONTES_DO_DEGRAU[degrau]} "
        "Nunca use como fonte comentários, avaliações, perguntas de outros compradores ou fóruns. "
        + ("Só responda se pelo menos DUAS fontes diferentes disserem a mesma coisa. " if duas else "")
        + "Nunca invente especificação, compatibilidade, cor, tamanho, voltagem ou qualquer dado "
        "que não esteja explicitamente na fonte. Se não achar nessas fontes, se o produto exato não "
        "bater com o que achou, ou se a informação não for suficiente pra responder com segurança, "
        f"responda EXATA e SOMENTE com o texto: {SINALIZADOR_SEM_CONTEXTO}\n"
        "Se a pergunta for sobre COMPATIBILIDADE com um modelo/veículo específico e a fonte não "
        f"citar exatamente esse modelo, também responda só {SINALIZADOR_SEM_CONTEXTO}.\n"
        + REGRAS_DE_ESCRITA + "\n\n"
        f"--- PRODUTO: {identificador} ---" + detalhes
    )
    parametros = dict(
        max_tokens=1024,
        system=prompt_sistema,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": cfg["buscas_por_degrau"]}],
        messages=[{"role": "user", "content": pergunta_texto}],
    )
    modelo = config_ia.modelo_pesquisa(CLAUDE_MODEL_PRE_VENDA)
    from app import saude_ia
    try:
        resposta = cliente.messages.create(model=modelo, **parametros)
    except Exception as exc:
        if modelo == CLAUDE_MODEL_PRE_VENDA:
            logger.error("Falha na pesquisa na internet (degrau %s): %s", degrau, exc)
            saude_ia.registrar_falha(str(exc))
            return None
        # Modelo "mais preciso" indisponível (nome errado, conta sem acesso...): tenta
        # com o modelo padrão, pra pesquisa nunca parar por causa disso.
        logger.warning("Modelo de pesquisa %s falhou (%s) -- tentando com %s", modelo, exc, CLAUDE_MODEL_PRE_VENDA)
        try:
            resposta = cliente.messages.create(model=CLAUDE_MODEL_PRE_VENDA, **parametros)
        except Exception as exc2:
            logger.error("Falha na pesquisa na internet (degrau %s): %s", degrau, exc2)
            saude_ia.registrar_falha(str(exc2))
            return None
    saude_ia.registrar_sucesso()
    return resposta_segura(_texto_final(resposta))


def pesquisar_na_internet(pergunta_texto: str, titulo_produto: str | None, sku: str | None,
                          dados_produto: str | None = None) -> tuple[str | None, str | None]:
    """
    Pesquisa em ESCADA (ver Administração › Calibrar IA): tenta os degraus
    ligados, na ordem, e para no primeiro que responder com segurança.
    Devolve (resposta, "2 · manual em PDF") ou (None, None).

    Mais arriscado que o manual próprio -- por isso quem chama sempre marca a
    resposta como "precisa_auditoria" (⚠ revisar).
    """
    from app import config_ia
    if not titulo_produto:
        return None, None  # sem nome/modelo do produto, não dá pra pesquisar direito
    cliente = _obter_cliente()
    if cliente is None:
        return None, None
    identificador = f"{titulo_produto}" + (f" (SKU/código: {sku})" if sku else "")
    # Marca/modelo da ficha do anúncio ajudam a achar o produto EXATO.
    detalhes = f"\n--- DADOS DO PRODUTO (do anúncio) ---\n{dados_produto}" if dados_produto else ""
    for degrau in config_ia.degraus_ativos():
        texto = _pesquisar_degrau(cliente, degrau, pergunta_texto, identificador, detalhes)
        if texto:
            return texto, config_ia.NOME_DEGRAU[degrau]
    return None, None


def buscar_resposta_no_site_fabricante(pergunta_texto: str, titulo_produto: str | None, sku: str | None,
                                       dados_produto: str | None = None) -> str | None:
    """Compatibilidade com o código antigo: só o texto da pesquisa em escada."""
    return pesquisar_na_internet(pergunta_texto, titulo_produto, sku, dados_produto)[0]


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
        from app import saude_ia
        saude_ia.registrar_falha(str(exc))
        return None
    from app import saude_ia
    saude_ia.registrar_sucesso()

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
        f"Você responde perguntas de pré-venda de um anúncio no Mercado Livre, {_estilo()}, "
        "começando com 'Olá!'. Use APENAS os DADOS DO "
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
        from app import saude_ia
        saude_ia.registrar_falha(str(exc))
        return None
    from app import saude_ia
    saude_ia.registrar_sucesso()
    return resposta_segura(_texto_final(resposta))
