"""
Cliente para as chamadas de integração com o Mercado Livre relacionadas
a OAuth: montar a URL de autorização, trocar o código por tokens, e
renovar o access_token usando o refresh_token.

Erros de rede ou de resposta do Mercado Livre são convertidos em
MLAuthError com uma mensagem clara, em vez de deixar a exceção crua do
httpx subir — isso facilita bastante diagnosticar problema de
configuração (.env errado, Redirect URI não bate, etc.) sem precisar
ficar lendo stack trace.
"""
from datetime import datetime, timedelta
from urllib.parse import urlencode

import logging

import httpx

from app.config import (
    ML_CLIENT_ID,
    ML_CLIENT_SECRET,
    ML_REDIRECT_URI,
    ML_AUTH_BASE_URL,
    ML_TOKEN_URL,
    ML_API_BASE_URL,
)

logger = logging.getLogger(__name__)


class MLAuthError(Exception):
    """Erro ao autenticar ou trocar tokens com o Mercado Livre."""


def montar_url_autorizacao(state: str) -> str:
    """
    Monta a URL pra onde o dono da conta deve ser redirecionado pra
    autorizar o app. 'state' carrega o apelido da conta, pra sabermos
    de quem se trata quando o Mercado Livre chamar o callback de volta.
    """
    if not ML_CLIENT_ID or not ML_REDIRECT_URI:
        raise MLAuthError(
            "ML_CLIENT_ID ou ML_REDIRECT_URI não configurados no .env. "
            "Cadastre o app no Mercado Livre Devs e preencha o .env "
            "antes de tentar conectar uma conta."
        )
    # urlencode escapa espaço e qualquer caractere especial no nome da
    # empresa (ex: "AB HOME" -> "AB+HOME") -- sem isso, um nome com
    # espaço quebrava o link dependendo de onde fosse aberto.
    query = urlencode({
        "response_type": "code",
        "client_id": ML_CLIENT_ID,
        "redirect_uri": ML_REDIRECT_URI,
        "state": state,
    })
    return f"{ML_AUTH_BASE_URL}?{query}"


def trocar_codigo_por_token(code: str) -> dict:
    """Troca o 'code' recebido no callback por access_token/refresh_token."""
    _validar_credenciais()
    payload = {
        "grant_type": "authorization_code",
        "client_id": ML_CLIENT_ID,
        "client_secret": ML_CLIENT_SECRET,
        "code": code,
        "redirect_uri": ML_REDIRECT_URI,
    }
    return _post_token(payload, contexto="trocar o código por token")


def renovar_token(refresh_token: str) -> dict:
    """Usa o refresh_token pra gerar um novo access_token, sem reautorizar."""
    _validar_credenciais()
    payload = {
        "grant_type": "refresh_token",
        "client_id": ML_CLIENT_ID,
        "client_secret": ML_CLIENT_SECRET,
        "refresh_token": refresh_token,
    }
    return _post_token(payload, contexto="renovar o token")


def calcular_expiracao(expires_in_segundos: int) -> datetime:
    """
    Converte o 'expires_in' (segundos) da resposta do Mercado Livre numa
    data absoluta, com 5 minutos de margem de segurança pra renovar
    antes de expirar de verdade (evita corrida contra o relógio).
    """
    margem_segundos = 300
    return datetime.utcnow() + timedelta(seconds=max(expires_in_segundos - margem_segundos, 0))


def _validar_credenciais() -> None:
    if not ML_CLIENT_ID or not ML_CLIENT_SECRET or not ML_REDIRECT_URI:
        raise MLAuthError(
            "ML_CLIENT_ID, ML_CLIENT_SECRET ou ML_REDIRECT_URI não "
            "configurados no .env. Preencha essas três variáveis antes "
            "de conectar uma conta."
        )


def _post_token(payload: dict, contexto: str) -> dict:
    try:
        resposta = httpx.post(ML_TOKEN_URL, data=payload, timeout=15)
    except httpx.RequestError as exc:
        raise MLAuthError(f"Falha de rede ao {contexto}: {exc}") from exc

    if resposta.status_code != 200:
        raise MLAuthError(
            f"Mercado Livre recusou {contexto} (status {resposta.status_code}): {resposta.text}"
        )
    return resposta.json()


def garantir_token_valido(conta, db) -> str:
    """
    Devolve um access_token válido pra essa conta, renovando via
    refresh_token automaticamente se estiver perto de expirar (ou já
    expirado). Atualiza o registro no banco quando renova. Levanta
    MLAuthError se a conta não tiver token nenhum (nunca autorizada)
    ou se o refresh_token tiver sido revogado pelo Mercado Livre.
    """
    if not conta.access_token or not conta.refresh_token:
        raise MLAuthError(
            f"Conta '{conta.apelido}' não tem token salvo — precisa autorizar "
            f"pelo painel (/contas) antes de fazer chamadas à API."
        )

    agora = datetime.utcnow()
    if conta.token_expira_em and conta.token_expira_em > agora:
        return conta.access_token  # ainda válido, nada a fazer

    dados = renovar_token(conta.refresh_token)
    conta.access_token = dados.get("access_token")
    conta.refresh_token = dados.get("refresh_token", conta.refresh_token)
    conta.token_expira_em = calcular_expiracao(dados.get("expires_in", 21600))
    db.commit()
    db.refresh(conta)
    return conta.access_token


class MLApiError(Exception):
    """Erro ao chamar a API pública do Mercado Livre (não relacionado a OAuth)."""


def _get(path: str, access_token: str, contexto: str) -> dict:
    try:
        resposta = httpx.get(
            f"{ML_API_BASE_URL}{path}",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
    except httpx.RequestError as exc:
        raise MLApiError(f"Falha de rede ao {contexto}: {exc}") from exc
    if resposta.status_code != 200:
        raise MLApiError(f"Mercado Livre recusou {contexto} (status {resposta.status_code}): {resposta.text}")
    return resposta.json()


def buscar_pergunta(access_token: str, question_id: str) -> dict:
    """Busca o texto e o item_id de uma pergunta específica."""
    return _get(f"/questions/{question_id}", access_token, "buscar a pergunta")


# ---------------------------------------------------------------------------
# Leitura do anúncio (SKU + título) -- UMA consulta, com memória rápida
# ---------------------------------------------------------------------------
# O SKU de um anúncio quase nunca muda: guarda o resultado por algumas
# horas pra não consultar o ML de novo a cada pergunta no mesmo MLB.
# Só guarda leituras bem-sucedidas (erro nunca fica "preso" na memória).
_CACHE_ANUNCIO: dict[str, tuple[datetime, dict]] = {}
_CACHE_VALIDADE = timedelta(hours=6)
_CACHE_MAX_ITENS = 5000


def _extrair_skus(item: dict) -> tuple[list[str], str]:
    """
    Procura o SKU nos três lugares onde o ML pode guardar:
      1. atributo SELLER_SKU do anúncio
      2. campo seller_custom_field do anúncio
      3. dentro de cada VARIAÇÃO (cor, voltagem...): atributo SELLER_SKU
         ou seller_custom_field da variação -- caso mais comum em anúncio
         com variações, e que a versão antiga não lia.
    Devolve (lista de SKUs sem repetição, onde foi achado).
    """
    skus: list[str] = []
    origem = "nenhum"

    def add(valor, de_onde):
        nonlocal origem
        valor = (valor or "").strip() if isinstance(valor, str) else ""
        if valor and valor not in skus:
            skus.append(valor)
            if origem == "nenhum":
                origem = de_onde

    for atributo in item.get("attributes") or []:
        if atributo.get("id") == "SELLER_SKU":
            add(atributo.get("value_name"), "atributo")
    add(item.get("seller_custom_field"), "seller_custom_field")
    for variacao in item.get("variations") or []:
        for atributo in variacao.get("attributes") or []:
            if atributo.get("id") == "SELLER_SKU":
                add(atributo.get("value_name"), "variacao")
        add(variacao.get("seller_custom_field"), "variacao")
    return skus, origem


def ler_dados_do_anuncio(access_token: str, item_id: str, usar_cache: bool = True) -> dict:
    """
    Lê o anúncio uma vez só e devolve:
      {"titulo", "sku" (principal), "skus" (todos), "origem_sku", "erro"}
    SKU principal: o do anúncio; se só existir nas variações, o primeiro
    delas (todos ficam em "skus"). Nunca levanta erro -- em falha devolve
    "erro" preenchido, pra quem chama registrar e seguir em frente.
    """
    vazio = {"titulo": None, "sku": None, "skus": [], "origem_sku": "nenhum", "erro": None, "nao_existe": False}
    if not item_id:
        return {**vazio, "erro": "pergunta sem item_id"}

    agora = datetime.utcnow()
    if usar_cache:
        em_cache = _CACHE_ANUNCIO.get(item_id)
        if em_cache and em_cache[0] > agora:
            return em_cache[1]

    try:
        # include_attributes=all traz os atributos DE CADA VARIAÇÃO (onde
        # costuma estar o SELLER_SKU); sem ele a variação vem incompleta.
        item = _get(f"/items/{item_id}?include_attributes=all", access_token, "buscar o anúncio")
    except MLApiError as exc:
        logger.warning("Não consegui ler o anúncio %s: %s", item_id, exc)
        # 404 = anúncio excluído/inexistente: não adianta tentar de novo.
        return {**vazio, "erro": str(exc)[:300], "nao_existe": "(status 404)" in str(exc)}

    skus, origem = _extrair_skus(item)
    dados = {
        "titulo": item.get("title"),
        "sku": skus[0] if skus else None,
        "skus": skus,
        "origem_sku": origem,
        "erro": None,
        "nao_existe": False,
    }
    if len(_CACHE_ANUNCIO) >= _CACHE_MAX_ITENS:
        _CACHE_ANUNCIO.clear()  # simples e seguro: recomeça a memória
    _CACHE_ANUNCIO[item_id] = (agora + _CACHE_VALIDADE, dados)
    return dados


def buscar_sku_do_item(access_token: str, item_id: str) -> str | None:
    """Compatibilidade: SKU principal do anúncio (ver ler_dados_do_anuncio)."""
    return ler_dados_do_anuncio(access_token, item_id)["sku"]


def buscar_titulo_do_item(access_token: str, item_id: str) -> str | None:
    """Compatibilidade: título do anúncio (ver ler_dados_do_anuncio)."""
    return ler_dados_do_anuncio(access_token, item_id)["titulo"]


def enviar_resposta(access_token: str, question_id: str, texto: str) -> dict:
    """Envia a resposta de uma pergunta de pré-venda pro Mercado Livre."""
    try:
        resposta = httpx.post(
            f"{ML_API_BASE_URL}/answers",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"question_id": int(question_id), "text": texto},
            timeout=15,
        )
    except httpx.RequestError as exc:
        raise MLApiError(f"Falha de rede ao enviar a resposta: {exc}") from exc
    if resposta.status_code not in (200, 201):
        raise MLApiError(f"Mercado Livre recusou a resposta (status {resposta.status_code}): {resposta.text}")
    return resposta.json()


def buscar_mensagem(access_token: str, message_id: str) -> dict:
    """Busca o conteúdo de uma mensagem de pós-venda (tópico 'messages')."""
    return _get(f"/messages/{message_id}?tag=post_sale", access_token, "buscar a mensagem")


def buscar_claim(access_token: str, claim_id: str) -> dict:
    """Busca os detalhes de uma reclamação/devolução (tópico 'claims')."""
    return _get(f"/post-purchase/v1/claims/{claim_id}", access_token, "buscar a reclamação")


def enviar_mensagem_pos_venda(access_token: str, pack_id: str, seller_id: str, texto: str) -> dict:
    """
    Envia uma mensagem de pós-venda pro comprador, dentro da conversa
    do pacote (pack) do pedido -- endpoint diferente do de responder
    pergunta de pré-venda (que é vinculado ao item, não ao pedido).
    """
    try:
        resposta = httpx.post(
            f"{ML_API_BASE_URL}/messages/packs/{pack_id}/sellers/{seller_id}?tag=post_sale",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"text": texto},
            timeout=15,
        )
    except httpx.RequestError as exc:
        raise MLApiError(f"Falha de rede ao enviar a mensagem de pós-venda: {exc}") from exc
    if resposta.status_code not in (200, 201):
        raise MLApiError(f"Mercado Livre recusou a mensagem (status {resposta.status_code}): {resposta.text}")
    return resposta.json()


def enviar_resposta_claim(access_token: str, claim_id: str, texto: str) -> dict:
    """Envia uma resposta dentro de uma reclamação/devolução (tópico 'claims')."""
    try:
        resposta = httpx.post(
            f"{ML_API_BASE_URL}/post-purchase/v1/claims/{claim_id}/actions/send-message",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"message": texto, "attachments": []},
            timeout=15,
        )
    except httpx.RequestError as exc:
        raise MLApiError(f"Falha de rede ao enviar a resposta da reclamação: {exc}") from exc
    if resposta.status_code not in (200, 201):
        raise MLApiError(f"Mercado Livre recusou a resposta (status {resposta.status_code}): {resposta.text}")
    return resposta.json()


# ---------------------------------------------------------------------------
# Verificação de cancelamento + reposição de estoque (solicitações manuais)
# ---------------------------------------------------------------------------
def buscar_pedido(access_token: str, order_id: str) -> dict:
    """
    Busca os dados completos de um pedido -- usado pra conferir se uma
    solicitação de cancelamento manual já foi efetivada de verdade no
    Mercado Livre. Inclui `status` (vira "cancelled" quando cancelado)
    e `cancel_detail` (group/code/description/requested_by -- o motivo
    e a classificação do cancelamento, preenchidos automaticamente
    pelo Mercado Livre, sem precisar de nenhum atendimento).
    """
    return _get(f"/orders/{order_id}", access_token, "buscar o pedido")


def repor_estoque_item(access_token: str, item_id: str, quantidade_a_somar: int, variation_id: str | None = None) -> dict:
    """
    Soma `quantidade_a_somar` ao estoque disponível de um anúncio (ou
    de uma variação específica, quando o pedido tiver variation_id) --
    usado pra repor o estoque automaticamente depois de confirmar que
    uma venda foi cancelada. A API do Mercado Livre não tem um
    "incrementar", só "definir o valor final" -- por isso lê o estoque
    atual primeiro, pra não sobrescrever com um número errado se ele já
    tiver mudado por outro motivo entre a leitura e a escrita.
    """
    item_atual = _get(f"/items/{item_id}", access_token, "buscar o anúncio pra repor estoque")

    if variation_id:
        variacoes = item_atual.get("variations") or []
        variacao_atual = next((v for v in variacoes if str(v.get("id")) == str(variation_id)), None)
        estoque_atual = (variacao_atual or {}).get("available_quantity", 0) or 0
        payload = {"variations": [{"id": variation_id, "available_quantity": estoque_atual + quantidade_a_somar}]}
    else:
        estoque_atual = item_atual.get("available_quantity", 0) or 0
        payload = {"available_quantity": estoque_atual + quantidade_a_somar}

    try:
        resposta = httpx.put(
            f"{ML_API_BASE_URL}/items/{item_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            json=payload,
            timeout=15,
        )
    except httpx.RequestError as exc:
        raise MLApiError(f"Falha de rede ao repor o estoque: {exc}") from exc
    if resposta.status_code != 200:
        raise MLApiError(f"Mercado Livre recusou a reposição de estoque (status {resposta.status_code}): {resposta.text}")
    return resposta.json()
