"""
Conversas de pós-venda (mensagens do comprador depois da compra) no painel do
SELLER e no painel da EQUIPE -- uma fila só, as duas telas leem as mesmas linhas
de `MensagemPosVenda` (tipo "mensagem").

O que este módulo faz (28/10):
  - lê a conversa no Mercado Livre e decide se ela espera resposta
    (última mensagem é do comprador) ou já foi respondida (do vendedor);
  - mantém UMA linha por conversa esperando (mensagem nova do comprador
    atualiza a linha em vez de duplicar);
  - TRAVA "em atendimento": quem abre primeiro segura, o outro só vê. Solta
    sozinha depois de ATENDIMENTO_EXPIRA_MIN minutos;
  - envia a resposta pela API do Mercado Livre (o comprador vê como mensagem
    normal do vendedor) com as regras do ML (350 caracteres, sem link nem dado
    pessoal) conferidas ANTES de enviar;
  - busca as conversas dos últimos JANELA_DIAS dias (backfill) e confere as
    abertas para saber se alguém respondeu direto no Mercado Livre.

Princípios (mesmos do resto do pós-venda): a IA só SUGERE, nunca envia sozinha;
tudo que muda estado é UPDATE condicional (duas pessoas ao mesmo tempo -> só uma
vence); erro numa conta ou conversa é registrado e o resto continua.
"""
import asyncio
import logging
import os
import re
import time
from datetime import datetime, timedelta

from sqlalchemy import or_, update

from app.contas_util import chave_conta
from app.database import SessionLocal
from app.ml_client import (
    MLApiError,
    MLAuthError,
    buscar_conversa_pos_venda,
    enviar_mensagem_pos_venda,
    garantir_token_valido,
    listar_pedidos_para_conversas,
)
from app.models import Conta, MensagemPosVenda

logger = logging.getLogger(__name__)

LIMITE_CARACTERES = 350       # limite do Mercado Livre por mensagem
JANELA_DIAS = 10              # só conversas dos últimos 10 dias
ATENDIMENTO_EXPIRA_MIN = 20   # trava solta sozinha depois disso
PAUSA_ENTRE_CONSULTAS_SEG = 0.15
MAX_CONVERSAS_POR_CONTA = 200  # teto de conversas consultadas por conta em cada varredura

STATUS_ABERTA = "fila_humana"

# (27/10, decisão do Washington) O pós-venda começa a valer na SEGUNDA-FEIRA 02/11/2026 (00:00 de
# Brasília = 03:00 UTC): conversas anteriores a isso não entram. Os 10 dias de trás só vêm se a
# equipe pedir ("Buscar mensagens anteriores"). Dá pra mudar a data com POSVENDA_INICIO=AAAA-MM-DD.
INICIO_PADRAO = "2026-11-02"


def inicio_utc() -> datetime:
    try:
        d = datetime.strptime(os.getenv("POSVENDA_INICIO", INICIO_PADRAO).strip(), "%Y-%m-%d")
    except ValueError:
        d = datetime.strptime(INICIO_PADRAO, "%Y-%m-%d")
    return d + timedelta(hours=3)  # meia-noite de Brasília em UTC


# ---------------------------------------------------------------------------
# Regras do texto (o ML recusa link e dado pessoal; melhor avisar antes de enviar)
# ---------------------------------------------------------------------------
_RE_LINK = re.compile(r"(https?://|www\.|\b[\w-]+\.(com|com\.br|br|net|org|me|io|app|shop|store)\b)", re.I)
_RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+", re.I)
_RE_NUMERO = re.compile(r"\+?\d[\d\s().-]{6,}\d")


def validar_texto(texto: str | None) -> str | None:
    """Devolve a mensagem de erro (em português) se o texto não pode ser enviado; None se está ok."""
    t = (texto or "").strip()
    if not t:
        return "Escreva a resposta antes de enviar."
    if len(t) > LIMITE_CARACTERES:
        return f"A mensagem passou de {LIMITE_CARACTERES} caracteres ({len(t)})."
    if _RE_EMAIL.search(t):
        return "Não inclua e-mail ou redes sociais: o Mercado Livre não aceita."
    if _RE_LINK.search(t):
        return "O Mercado Livre não aceita links na mensagem."
    for achado in _RE_NUMERO.findall(t):
        digitos = re.sub(r"\D", "", achado)
        # 16+ dígitos = número de pedido/venda (pode); 8 a 15 = parece telefone/documento.
        if 8 <= len(digitos) <= 15:
            return "Não inclua telefone ou outros dados pessoais: o Mercado Livre não aceita."
    return None


# ---------------------------------------------------------------------------
# Leitura da conversa (formato do ML, tolerante a variações)
# ---------------------------------------------------------------------------
def _data_ml(texto) -> datetime | None:
    if not texto:
        return None
    try:
        d = datetime.fromisoformat(str(texto).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is not None:
        d = (d - d.utcoffset()).replace(tzinfo=None)
    return d


def normalizar_conversa(dados: dict, seller_id: str) -> list[dict]:
    """
    Lista cronológica de {id, de: "comprador"|"vendedor", comprador_id, texto, quando}.
    Aceita `text` como texto puro ou como {"plain": ...} e a data em `message_date`
    (created/received) -- campos conferidos com o formato público do ML.
    """
    saida = []
    for m in (dados or {}).get("messages") or []:
        texto = m.get("text")
        texto = texto.get("plain") if isinstance(texto, dict) else texto
        texto = (texto or "").strip()
        de_id = str((m.get("from") or {}).get("user_id") or "")
        datas = m.get("message_date") or {}
        quando = _data_ml(datas.get("created") or datas.get("received") or datas.get("available"))
        saida.append({
            "id": str(m.get("id") or ""),
            "de": "vendedor" if de_id == str(seller_id) else "comprador",
            "comprador_id": de_id if de_id and de_id != str(seller_id) else None,
            "texto": texto,
            "quando": quando,
        })
    saida.sort(key=lambda x: (x["quando"] or datetime.min))
    return saida


def _sequencia_do_comprador(msgs: list[dict]) -> list[dict]:
    """As mensagens do comprador no FINAL da conversa (depois da última do vendedor)."""
    seq = []
    for m in reversed(msgs):
        if m["de"] != "comprador":
            break
        seq.append(m)
    return list(reversed(seq))


# ---------------------------------------------------------------------------
# Gravação: uma linha por conversa esperando
# ---------------------------------------------------------------------------
def _fechar_externamente(db, conta_id: int, pack_id: str, quando: datetime | None) -> int:
    """Alguém respondeu direto no Mercado Livre: fecha a linha aberta (sem sobrescrever resposta nossa)."""
    r = db.execute(
        update(MensagemPosVenda)
        .where(
            MensagemPosVenda.conta_id == conta_id,
            MensagemPosVenda.pack_id == pack_id,
            MensagemPosVenda.tipo == "mensagem",
            MensagemPosVenda.status.in_((STATUS_ABERTA, "pendente")),
        )
        .values(
            status="respondida", respondida_em=quando or datetime.utcnow(), respondida_origem="ml",
            respondida_por="Mercado Livre", em_atendimento_por_id=None, em_atendimento_por=None, em_atendimento_em=None,
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return r.rowcount


def aplicar_conversa(db, conta: Conta, pack_id: str, msgs: list[dict], order_id: str | None = None,
                     comprador_nome: str | None = None, produto_titulo: str | None = None, sku: str | None = None,
                     desde: datetime | None = None) -> str:
    """
    Atualiza o banco com o estado atual de UMA conversa. Devolve:
      "vazia"      -> sem mensagens;
      "respondida" -> última é do vendedor (fecha a linha aberta, se houver);
      "antiga"     -> o comprador escreveu antes da janela de dias (não entra);
      "criada" / "atualizada" -> há mensagem do comprador esperando resposta.
    """
    if not msgs:
        return "vazia"
    ultima = msgs[-1]
    if ultima["de"] == "vendedor":
        _fechar_externamente(db, conta.id, pack_id, ultima["quando"])
        return "respondida"

    seq = _sequencia_do_comprador(msgs)
    espera_desde = seq[0]["quando"] or datetime.utcnow()
    texto = " / ".join(m["texto"] for m in seq if m["texto"])[:2000] or ultima["texto"] or "(mensagem sem texto)"
    aberta = (
        db.query(MensagemPosVenda)
        .filter(
            MensagemPosVenda.conta_id == conta.id, MensagemPosVenda.pack_id == pack_id,
            MensagemPosVenda.tipo == "mensagem", MensagemPosVenda.status == STATUS_ABERTA,
        )
        .first()
    )
    if aberta is not None:
        mudou = aberta.texto != texto
        aberta.texto = texto
        aberta.recebida_em = espera_desde
        aberta.comprador_nome = comprador_nome or aberta.comprador_nome
        aberta.produto_titulo = produto_titulo or aberta.produto_titulo
        aberta.order_id = order_id or aberta.order_id
        aberta.sku = sku or aberta.sku
        if mudou:
            aberta.resposta_sugerida = None   # a sugestão antiga era de outra pergunta
        db.commit()
        return "atualizada"

    if desde is not None and espera_desde < desde:
        return "antiga"
    recurso = ultima["id"] or f"pack:{pack_id}:{int(espera_desde.timestamp())}"
    if db.query(MensagemPosVenda.id).filter(MensagemPosVenda.ml_recurso_id == recurso).first():
        return "atualizada"  # já tratada antes (respondida): não reabre a mesma mensagem
    db.add(MensagemPosVenda(
        conta_id=conta.id, ml_recurso_id=recurso, tipo="mensagem", pack_id=pack_id, order_id=order_id, sku=sku,
        texto=texto, status=STATUS_ABERTA, recebida_em=espera_desde,
        comprador_nome=comprador_nome, produto_titulo=produto_titulo,
    ))
    db.commit()
    return "criada"


def processar_webhook_mensagem(db, conta: Conta, message_id: str) -> dict:
    """
    Aviso do tópico 'messages': lê a mensagem, acha a conversa (pack) e atualiza a fila.
    Sem pack_id no aviso, devolve {"status": "sem_pack"} e quem chamou cai no fluxo antigo.
    """
    from app.ml_client import buscar_mensagem, buscar_pedido
    from app.pos_venda_logica import criar_pedido_cancelamento, eh_pedido_cancelamento

    token = garantir_token_valido(conta, db)
    dados = buscar_mensagem(token, message_id)
    m = ((dados.get("messages") or [None])[0] if isinstance(dados.get("messages"), list) else None) or dados
    pack_id = None
    for rec in m.get("message_resources") or []:
        if rec.get("name") == "packs" and rec.get("id"):
            pack_id = str(rec["id"])
    if not pack_id:
        return {"status": "sem_pack", "dados": dados}

    msgs = normalizar_conversa(buscar_conversa_pos_venda(token, pack_id, conta.ml_user_id), conta.ml_user_id)
    nome = produto = sku = order_id = None
    try:  # nome do comprador e produto: se falhar, a conversa entra mesmo assim
        pedido = buscar_pedido(token, pack_id)
        order_id = str(pedido.get("id") or pack_id)
        nome = (pedido.get("buyer") or {}).get("nickname")
        itens = pedido.get("order_items") or []
        item = (itens[0].get("item") if itens else None) or {}
        produto, sku = item.get("title"), item.get("seller_sku") or item.get("seller_custom_field")
    except (MLApiError, MLAuthError):
        order_id = pack_id
    res = aplicar_conversa(db, conta, pack_id, msgs, order_id=order_id, comprador_nome=nome, produto_titulo=produto, sku=sku, desde=inicio_utc())
    if res == "criada" and msgs and eh_pedido_cancelamento(msgs[-1]["texto"]):
        criar_pedido_cancelamento(db, conta.id, order_id, sku, msgs[-1]["texto"])
    return {"status": "processada", "conversa": res, "pack_id": pack_id}


# ---------------------------------------------------------------------------
# Varredura: últimos 10 dias (backfill) e conferência das abertas
# ---------------------------------------------------------------------------
def _marcar_auth(db, conta: Conta, com_erro: bool) -> None:
    conta.auth_erro_em = datetime.utcnow() if com_erro else None
    db.commit()


def sincronizar_conta(conta: Conta, db, dias: int = JANELA_DIAS, max_conversas: int = MAX_CONVERSAS_POR_CONTA, anteriores: bool = False) -> dict:
    """
    Busca as conversas de UMA conta e grava as que esperam resposta. Normalmente só conta a partir da
    data de início (inicio_utc); com `anteriores=True` (botão da equipe) volta até `dias` dias.
    """
    r = {"conta": conta.apelido, "consultadas": 0, "criadas": 0, "atualizadas": 0, "respondidas": 0, "erro": None}
    try:
        token = garantir_token_valido(conta, db)
        pedidos = listar_pedidos_para_conversas(token, conta.ml_user_id, dias=dias)
    except MLAuthError as exc:
        _marcar_auth(db, conta, True)
        r["erro"] = "reconectar"
        logger.warning("Pós-venda: conta %s precisa reconectar: %s", conta.apelido, exc)
        return r
    except MLApiError as exc:
        r["erro"] = "ml"
        logger.warning("Pós-venda: não consegui listar pedidos da conta %s: %s", conta.apelido, exc)
        return r
    if conta.auth_erro_em:
        _marcar_auth(db, conta, False)

    desde = datetime.utcnow() - timedelta(days=dias)
    if not anteriores:
        desde = max(desde, inicio_utc())
    vistos = set()
    for p in pedidos:
        if p["pack_id"] in vistos:
            continue
        vistos.add(p["pack_id"])
        if len(vistos) > max_conversas:
            break
        try:
            dados = buscar_conversa_pos_venda(token, p["pack_id"], conta.ml_user_id)
        except MLApiError as exc:
            if "404" not in str(exc):
                logger.warning("Pós-venda: conversa %s da conta %s: %s", p["pack_id"], conta.apelido, exc)
            continue
        r["consultadas"] += 1
        res = aplicar_conversa(
            db, conta, p["pack_id"], normalizar_conversa(dados, conta.ml_user_id), order_id=p["order_id"],
            comprador_nome=p["comprador_nome"], produto_titulo=p["produto_titulo"], sku=p["sku"], desde=desde,
        )
        if res == "criada":
            r["criadas"] += 1
        elif res == "atualizada":
            r["atualizadas"] += 1
        elif res == "respondida":
            r["respondidas"] += 1
        time.sleep(PAUSA_ENTRE_CONSULTAS_SEG)
    return r


def sincronizar_todas(dias: int = JANELA_DIAS, anteriores: bool = False) -> list[dict]:
    """Varredura de todas as contas conectadas e ativas (segundo plano ou botão da equipe)."""
    resultados = []
    with SessionLocal() as db:
        contas = db.query(Conta).filter(Conta.ativa.is_(True), Conta.inativa_em.is_(None), Conta.access_token.isnot(None)).all()
        for conta in contas:
            try:
                resultados.append(sincronizar_conta(conta, db, dias=dias, anteriores=anteriores))
            except Exception:
                logger.exception("Pós-venda: erro inesperado na conta %s", conta.apelido)
                db.rollback()
    return resultados


def conferir_abertas() -> int:
    """
    Confere no ML as conversas que o Club ainda tem como "esperando": se alguém respondeu
    direto no Mercado Livre, fecha aqui (e a tela some com ela). Mais antigas primeiro.
    Devolve quantas foram fechadas.
    """
    fechadas = 0
    with SessionLocal() as db:
        abertas = (
            db.query(MensagemPosVenda)
            .filter(MensagemPosVenda.tipo == "mensagem", MensagemPosVenda.status == STATUS_ABERTA, MensagemPosVenda.pack_id.isnot(None))
            .order_by(MensagemPosVenda.recebida_em.asc())
            .limit(300)
            .all()
        )
        tokens: dict[int, str | None] = {}
        for item in abertas:
            conta = item.conta
            if conta is None:
                continue
            if conta.id not in tokens:
                try:
                    tokens[conta.id] = garantir_token_valido(conta, db)
                    if conta.auth_erro_em:
                        _marcar_auth(db, conta, False)
                except MLAuthError:
                    tokens[conta.id] = None
                    _marcar_auth(db, conta, True)
            if not tokens[conta.id]:
                continue
            try:
                dados = buscar_conversa_pos_venda(tokens[conta.id], item.pack_id, conta.ml_user_id)
            except MLApiError as exc:
                logger.info("Pós-venda: não consegui conferir %s: %s", item.pack_id, exc)
                continue
            res = aplicar_conversa(db, conta, item.pack_id, normalizar_conversa(dados, conta.ml_user_id))
            if res == "respondida":
                fechadas += 1
            time.sleep(PAUSA_ENTRE_CONSULTAS_SEG)
    return fechadas


async def loop_pos_venda() -> None:
    """
    Segundo plano: confere as abertas a cada POSVENDA_CONFERIR_MIN (padrão 10) e faz a
    varredura dos últimos 10 dias a cada POSVENDA_VARREDURA_HORAS (padrão 6; a primeira
    logo ao subir, para já aparecer nas telas). Desliga com POSVENDA_SYNC_ATIVA=0.
    """
    try:
        conferir_seg = max(120, int(os.getenv("POSVENDA_CONFERIR_MIN", "10")) * 60)
        varredura_seg = max(1800, int(float(os.getenv("POSVENDA_VARREDURA_HORAS", "6")) * 3600))
    except ValueError:
        conferir_seg, varredura_seg = 600, 6 * 3600
    await asyncio.sleep(90)
    proxima_varredura = 0.0
    while True:
        try:
            agora = time.time()
            if datetime.utcnow() < inicio_utc():
                pass  # ainda não é a data de início: não gasta consulta nenhuma
            elif agora >= proxima_varredura:
                await asyncio.to_thread(sincronizar_todas)
                proxima_varredura = time.time() + varredura_seg
            else:
                await asyncio.to_thread(conferir_abertas)
        except Exception:
            logger.exception("Erro inesperado no segundo plano de pós-venda")
        await asyncio.sleep(conferir_seg)


# ---------------------------------------------------------------------------
# Trava "em atendimento" (UPDATE condicional: só uma pessoa segura por vez)
# ---------------------------------------------------------------------------
def nome_de_quem_atende(usuario) -> str:
    sufixo = "seller" if usuario.papel in ("seller", "suporte") else "equipe"
    return f"{usuario.nome_exibicao} ({sufixo})"


def assumir(db, item_id: int, usuario, forcar: bool = False) -> bool:
    """
    Segura a conversa para `usuario`. True se agora é dele (já era dele, estava livre, a trava
    expirou ou `forcar`); False se outra pessoa está atendendo.
    """
    agora = datetime.utcnow()
    limite = agora - timedelta(minutes=ATENDIMENTO_EXPIRA_MIN)
    livre = or_(
        MensagemPosVenda.em_atendimento_por_id.is_(None),
        MensagemPosVenda.em_atendimento_por_id == usuario.id,
        MensagemPosVenda.em_atendimento_em < limite,
    )
    condicao = [MensagemPosVenda.id == item_id, MensagemPosVenda.status == STATUS_ABERTA]
    if not forcar:
        condicao.append(livre)
    r = db.execute(
        update(MensagemPosVenda).where(*condicao)
        .values(em_atendimento_por_id=usuario.id, em_atendimento_por=nome_de_quem_atende(usuario), em_atendimento_em=agora)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return r.rowcount == 1


def liberar(db, item_id: int, usuario) -> None:
    """Solta a trava (só se for dele) -- ao fechar a conversa sem responder."""
    db.execute(
        update(MensagemPosVenda)
        .where(MensagemPosVenda.id == item_id, MensagemPosVenda.em_atendimento_por_id == usuario.id, MensagemPosVenda.status == STATUS_ABERTA)
        .values(em_atendimento_por_id=None, em_atendimento_por=None, em_atendimento_em=None)
        .execution_options(synchronize_session=False)
    )
    db.commit()


def trava_ativa(item: MensagemPosVenda) -> bool:
    return bool(
        item.em_atendimento_por_id and item.em_atendimento_em
        and item.em_atendimento_em > datetime.utcnow() - timedelta(minutes=ATENDIMENTO_EXPIRA_MIN)
    )


# ---------------------------------------------------------------------------
# Resposta pelo Club (vai pela API do ML)
# ---------------------------------------------------------------------------
class RespostaRecusada(Exception):
    """Erro "de negócio" na resposta: carrega o status HTTP e a mensagem para a tela."""

    def __init__(self, status: int, mensagem: str):
        super().__init__(mensagem)
        self.status = status
        self.mensagem = mensagem


def responder(db, item: MensagemPosVenda, usuario, texto: str) -> None:
    """
    Envia a resposta ao comprador pelo Mercado Livre e fecha a conversa.
    Levanta RespostaRecusada (409 trava/já respondida, 400 texto, 502 ML, 409 reconectar).
    Ordem de segurança:  texto ok -> trava minha -> "enviando" (impede clique duplo) ->
    confere no ML se alguém já respondeu -> envia -> fecha. Falhou antes de enviar: volta pra fila.
    """
    erro = validar_texto(texto)
    if erro:
        raise RespostaRecusada(400, erro)
    if item.status != STATUS_ABERTA:
        raise RespostaRecusada(409, "Essa mensagem já foi respondida.")
    if not item.pack_id:
        raise RespostaRecusada(400, "Essa mensagem não tem a conversa registrada -- responda direto pelo Mercado Livre.")
    if not assumir(db, item.id, usuario):
        db.refresh(item)
        raise RespostaRecusada(409, f"Em atendimento por {item.em_atendimento_por or 'outra pessoa'}.")

    # "enviando": só uma requisição passa daqui (clique duplo / duas abas).
    r = db.execute(
        update(MensagemPosVenda)
        .where(MensagemPosVenda.id == item.id, MensagemPosVenda.status == STATUS_ABERTA, MensagemPosVenda.em_atendimento_por_id == usuario.id)
        .values(status="enviando")
        .execution_options(synchronize_session=False)
    )
    db.commit()
    if r.rowcount != 1:
        raise RespostaRecusada(409, "Essa mensagem já está sendo respondida.")

    def _voltar_para_fila():
        db.execute(
            update(MensagemPosVenda).where(MensagemPosVenda.id == item.id, MensagemPosVenda.status == "enviando")
            .values(status=STATUS_ABERTA).execution_options(synchronize_session=False)
        )
        db.commit()

    conta = item.conta
    try:
        token = garantir_token_valido(conta, db)
        if conta.auth_erro_em:
            _marcar_auth(db, conta, False)
        msgs = normalizar_conversa(buscar_conversa_pos_venda(token, item.pack_id, conta.ml_user_id), conta.ml_user_id)
        if msgs and msgs[-1]["de"] == "vendedor":
            _voltar_para_fila()
            _fechar_externamente(db, conta.id, item.pack_id, msgs[-1]["quando"])
            raise RespostaRecusada(409, "Essa conversa já foi respondida no Mercado Livre.")
        comprador_id = next((m["comprador_id"] for m in reversed(msgs) if m["comprador_id"]), None)
        enviar_mensagem_pos_venda(token, item.pack_id, conta.ml_user_id, texto.strip(), comprador_id=comprador_id)
    except RespostaRecusada:
        raise
    except MLAuthError:
        _voltar_para_fila()
        _marcar_auth(db, conta, True)
        raise RespostaRecusada(409, f"A conta {conta.apelido} precisa ser reconectada ao Mercado Livre antes de responder.")
    except MLApiError as exc:
        _voltar_para_fila()
        logger.warning("Pós-venda: ML recusou a resposta da conversa %s: %s", item.pack_id, exc)
        raise RespostaRecusada(502, f"O Mercado Livre não aceitou a mensagem: {exc}")

    db.execute(
        update(MensagemPosVenda).where(MensagemPosVenda.id == item.id)
        .values(
            status="respondida", resposta_enviada=texto.strip(), respondida_em=datetime.utcnow(),
            respondida_por=usuario.nome_exibicao, respondida_origem="seller" if usuario.papel in ("seller", "suporte") else "equipe",
            em_atendimento_por_id=None, em_atendimento_por=None, em_atendimento_em=None,
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()


def historico_da_conversa(db, item: MensagemPosVenda) -> list[dict]:
    """Conversa completa lida do ML na hora de abrir. Levanta RespostaRecusada se o ML/ a conta falhar."""
    conta = item.conta
    if not item.pack_id:
        return [{"de": "comprador", "texto": item.texto, "quando": item.recebida_em.isoformat() if item.recebida_em else None}]
    try:
        token = garantir_token_valido(conta, db)
        if conta.auth_erro_em:
            _marcar_auth(db, conta, False)
        msgs = normalizar_conversa(buscar_conversa_pos_venda(token, item.pack_id, conta.ml_user_id), conta.ml_user_id)
    except MLAuthError:
        _marcar_auth(db, conta, True)
        raise RespostaRecusada(409, f"A conta {conta.apelido} precisa ser reconectada ao Mercado Livre.")
    except MLApiError as exc:
        raise RespostaRecusada(502, f"Não consegui carregar a conversa no Mercado Livre: {exc}")
    return [{"de": m["de"], "texto": m["texto"], "quando": (m["quando"].isoformat() + "Z") if m["quando"] else None} for m in msgs]


def sugestao_da_ia(db, item: MensagemPosVenda) -> str | None:
    """Rascunho da IA (só sugestão, editável). Gerado ao abrir a conversa -- não na varredura, para não gastar IA à toa."""
    if item.resposta_sugerida:
        return item.resposta_sugerida
    from app.ia_pos_venda import gerar_sugestao_pos_venda
    sugestao = gerar_sugestao_pos_venda(item.texto, "mensagem")
    if sugestao:
        item.resposta_sugerida = sugestao[:LIMITE_CARACTERES]
        db.commit()
    return item.resposta_sugerida


# ---------------------------------------------------------------------------
# Serialização para as telas
# ---------------------------------------------------------------------------
def para_tela(item: MensagemPosVenda, eu_id: int | None = None) -> dict:
    from app.pos_venda_logica import eh_pedido_cancelamento
    ativa = trava_ativa(item)
    return {
        "id": item.id,
        "conta": item.conta.apelido if item.conta else "—",
        "conta_reconectar": bool(item.conta and item.conta.auth_erro_em),
        "comprador": item.comprador_nome or "Comprador",
        "pedido": item.order_id,
        "produto": item.produto_titulo,
        "sku": item.sku,
        "ultima": item.texto,
        "recebida_em": (item.recebida_em.isoformat() + "Z") if item.recebida_em else None,
        "status": item.status,
        "em_atendimento_por": item.em_atendimento_por if ativa else None,
        "em_atendimento_eu": bool(ativa and eu_id and item.em_atendimento_por_id == eu_id),
        "pedido_cancelamento": eh_pedido_cancelamento(item.texto or ""),
        "respondida_por": item.respondida_por,
        "respondida_origem": item.respondida_origem,
        "respondida_em": (item.respondida_em.isoformat() + "Z") if item.respondida_em else None,
        "resposta_enviada": item.resposta_enviada,
    }


def contas_ids_do_seller(db, usuario) -> list[int]:
    """Contas do seller logado (pode haver duplicatas de nome: pega todas as ativas com a mesma chave)."""
    if not usuario or not usuario.conta_vinculada:
        return []
    alvo = chave_conta(usuario.conta_vinculada)
    return [c.id for c in db.query(Conta).filter(Conta.inativa_em.is_(None)).all() if chave_conta(c.apelido) == alvo]


# ---------------------------------------------------------------------------
# Relatório: quantas mensagens chegaram e quantas o NOSSO sistema intermediou (28/10)
# ---------------------------------------------------------------------------
from zoneinfo import ZoneInfo  # noqa: E402

FUSO_BR = ZoneInfo("America/Sao_Paulo")
_DIAS_SEMANA = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]


def _br(d: datetime | None) -> datetime | None:
    """Datas do banco são UTC sem fuso -> horário de Brasília."""
    return d.replace(tzinfo=ZoneInfo("UTC")).astimezone(FUSO_BR) if d else None


def quando_texto(d: datetime | None) -> dict:
    b = _br(d)
    if not b:
        return {"data": "", "dia": "", "hora": ""}
    return {"data": b.strftime("%d/%m/%Y"), "dia": _DIAS_SEMANA[b.weekday()], "hora": b.strftime("%H:%M")}


def linhas_do_relatorio(db, contas_ids: list[int] | None, dias: int, conta: str | None = None) -> list[MensagemPosVenda]:
    desde = datetime.utcnow() - timedelta(days=dias)
    q = db.query(MensagemPosVenda).filter(MensagemPosVenda.tipo == "mensagem", MensagemPosVenda.recebida_em >= desde)
    if contas_ids is not None:
        q = q.filter(MensagemPosVenda.conta_id.in_(contas_ids or [0]))
    linhas = q.order_by(MensagemPosVenda.recebida_em.desc()).all()
    if conta:
        linhas = [i for i in linhas if i.conta and i.conta.apelido == conta]
    return linhas


def resumo_do_relatorio(linhas: list[MensagemPosVenda]) -> dict:
    sistema = [i for i in linhas if i.status == "respondida" and i.respondida_origem in ("seller", "equipe")]
    no_ml = [i for i in linhas if i.status == "respondida" and i.respondida_origem == "ml"]
    aguardando = [i for i in linhas if i.status in (STATUS_ABERTA, "enviando")]
    tempos = [(i.respondida_em - i.recebida_em).total_seconds() / 60 for i in sistema if i.respondida_em and i.recebida_em]
    por_dia: dict[str, dict] = {}
    for i in linhas:
        k = _br(i.recebida_em).strftime("%Y-%m-%d") if i.recebida_em else "—"
        d = por_dia.setdefault(k, {"dia": k, "recebidas": 0, "pelo_sistema": 0})
        d["recebidas"] += 1
        if i in sistema:
            d["pelo_sistema"] += 1
    por_conta: dict[str, dict] = {}
    for i in linhas:
        c = por_conta.setdefault(i.conta.apelido if i.conta else "—", {"conta": i.conta.apelido if i.conta else "—", "recebidas": 0, "pelo_sistema": 0, "no_ml": 0, "aguardando": 0})
        c["recebidas"] += 1
        c["pelo_sistema"] += i in sistema
        c["no_ml"] += i in no_ml
        c["aguardando"] += i in aguardando
    return {
        "recebidas": len(linhas),
        "pelo_sistema": len(sistema),
        "pelo_sistema_seller": len([i for i in sistema if i.respondida_origem == "seller"]),
        "pelo_sistema_equipe": len([i for i in sistema if i.respondida_origem == "equipe"]),
        "direto_no_ml": len(no_ml),
        "aguardando": len(aguardando),
        "pct_pelo_sistema": round(len(sistema) / len(linhas) * 100, 1) if linhas else None,
        "tempo_medio_min": round(sum(tempos) / len(tempos)) if tempos else None,
        "por_dia": sorted(por_dia.values(), key=lambda x: x["dia"], reverse=True),
        "por_conta": sorted(por_conta.values(), key=lambda x: -x["recebidas"]),
    }


def item_do_historico(i: MensagemPosVenda) -> dict:
    """Uma linha do histórico/relatório: quando chegou, o que o comprador disse, quem respondeu, o quê e quando."""
    tempo = None
    if i.respondida_em and i.recebida_em:
        tempo = round((i.respondida_em - i.recebida_em).total_seconds() / 60)
    return {
        "id": i.id,
        "conta": i.conta.apelido if i.conta else "—",
        "comprador": i.comprador_nome or "",
        "pedido": i.order_id or "",
        "produto": i.produto_titulo or "",
        "mensagem": i.texto,
        "recebida": quando_texto(i.recebida_em),
        "status": "Aguardando resposta" if i.status in (STATUS_ABERTA, "enviando") else "Respondida",
        "respondida": quando_texto(i.respondida_em),
        "respondida_por": i.respondida_por or "",
        "origem": {"seller": "Pelo sistema (seller)", "equipe": "Pelo sistema (equipe)", "ml": "Direto no Mercado Livre"}.get(i.respondida_origem or "", ""),
        "resposta": i.resposta_enviada or "",
        "tempo_min": tempo,
    }
