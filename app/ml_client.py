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

import httpx

from app.config import (
    ML_CLIENT_ID,
    ML_CLIENT_SECRET,
    ML_REDIRECT_URI,
    ML_AUTH_BASE_URL,
    ML_TOKEN_URL,
    ML_API_BASE_URL,
)


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


def buscar_sku_do_item(access_token: str, item_id: str) -> str | None:
    """
    Busca o SELLER_SKU cadastrado no anúncio, se existir. Nem todo
    anúncio tem SKU preenchido — nesse caso devolve None, e o item_id
    (MLB...) é usado como identificador substituto pelo chamador.
    """
    try:
        item = _get(f"/items/{item_id}", access_token, "buscar o item")
    except MLApiError:
        return None
    for atributo in item.get("attributes", []):
        if atributo.get("id") == "SELLER_SKU" and atributo.get("value_name"):
            return atributo["value_name"]
    return None


def buscar_titulo_do_item(access_token: str, item_id: str) -> str | None:
    """
    Busca o título do anúncio -- usado pra dar contexto de produto
    real (nome/modelo) pra camada de busca no site do fabricante,
    já que o SKU sozinho às vezes não diz muito sobre o que é o produto.
    """
    try:
        item = _get(f"/items/{item_id}", access_token, "buscar o item")
    except MLApiError:
        return None
    return item.get("title")


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
