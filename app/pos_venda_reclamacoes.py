"""
Reclamações e mediações de pós-venda (tópico 'claims' do Mercado Livre) -- aba "Reclamações"
no painel do SELLER e da EQUIPE (29/10). SÓ LEITURA nesta versão: mostramos o que o ML mostra
(motivo, estado, se afeta a reputação, quem precisa agir) e o seller abre a reclamação no ML.
A resposta pelo nosso sistema fica para a parte 2 (a API do ML permite; falta validar na Velasco).

Como funciona:
  - `sincronizar_reclamacoes_conta`: busca as reclamações ABERTAS da conta e as FECHADAS dos
    últimos 10 dias (mais antigas que isso são descartadas) e grava/atualiza uma linha por
    reclamação em `ReclamacaoPosVenda`. Só consulta de novo o que mudou no ML (last_updated);
  - `processar_webhook_claim`: aviso do tópico 'claims' -> atualiza aquela reclamação na hora;
  - leitura TOLERANTE: o formato exato dos campos será conferido com conta real, então tudo é
    lido com .get(); o JSON bruto fica em `dados_brutos` para ajustar sem perder nada;
  - mesma fase de teste das mensagens: só contas liberadas em POSVENDA_CONTAS são consultadas.

GANCHO DA IA (desligada): `classificar` (categoria) e `sugerir_resposta` são o ÚNICO ponto a
mexer para ligar a IA depois -- tela, banco e relatório já leem `categoria`/`resposta_sugerida`.
"""
import json
import logging
import time
from datetime import datetime, timedelta

from app import pos_venda_conversas as pv
from app.database import SessionLocal
from app.ml_client import (
    MLApiError,
    MLAuthError,
    buscar_afeta_reputacao,
    buscar_claim,
    buscar_detalhe_reclamacao,
    buscar_motivo_reclamacao,
    buscar_pedido,
    buscar_reclamacoes,
    garantir_token_valido,
)
from app.models import Conta, ReclamacaoPosVenda

logger = logging.getLogger(__name__)

JANELA_DIAS = pv.JANELA_DIAS          # fechadas só dos últimos 10 dias
MAX_POR_CONTA = 100                   # teto de reclamações lidas por conta em cada varredura
PAGINAS_MAX = 3                       # 3 x 50 por status
PAUSA_SEG = 0.15

# Motivos já conhecidos (cache em memória: o motivo de um código nunca muda).
_CACHE_MOTIVOS: dict[str, str | None] = {}
# Só o mínimo que a própria documentação do ML garante (prefixo do código); o resto vem da API.
_MOTIVO_PELO_PREFIXO = {"PNR": "Produto não recebido", "PDD": "Problema com o produto recebido"}

_TIPOS = {"mediations": "Reclamação", "returns": "Devolução", "cancel_purchase": "Cancelamento", "cancel_sale": "Cancelamento",
          "fulfillment": "Reclamação (Full)", "ml_case": "Caso do Mercado Livre", "change": "Troca", "service": "Serviço"}
_RESOLUCOES = {
    "payment_refunded": "Dinheiro devolvido ao comprador", "refunded": "Dinheiro devolvido ao comprador",
    "partial_refunded": "Reembolso parcial", "item_returned": "Produto devolvido", "product_replaced": "Produto trocado",
    "opened_claim": "Reclamação aberta", "respondent_timeout": "Prazo do vendedor esgotado", "complainant_timeout": "Prazo do comprador esgotado",
    "coverage_decision": "Decisão do Mercado Livre (cobertura)", "low_cost": "Resolvida sem devolução", "expired": "Expirada",
}
_AFETA = {"affected": "Afeta sua reputação", "not_affected": "Não afeta sua reputação"}


# ---------------------------------------------------------------------------
# Gancho da IA (hoje: regra simples / nada)
# ---------------------------------------------------------------------------
def classificar(motivo_id: str | None, motivo_texto: str | None, titulo: str | None, descricao: str | None) -> str | None:
    """
    Categoria interna da reclamação. HOJE por palavras-chave (sem IA); para ligar a IA depois,
    troque o corpo desta função -- quem chama já grava o resultado em `categoria`.
    """
    texto = " ".join(x for x in (motivo_texto, titulo, descricao) if x).lower()
    if any(p in texto for p in ("danific", "defeito", "não funciona", "nao funciona", "quebrad", "avaria")):
        return "defeito"
    if "diferente" in texto or "não é o que" in texto or "nao e o que" in texto:
        return "diferente"
    if "arrepend" in texto:
        return "arrependimento"
    if any(p in texto for p in ("não recebi", "nao recebi", "não chegou", "nao chegou", "atras")) or (motivo_id or "").upper().startswith("PNR"):
        return "nao_recebido"
    return "outro" if texto else None


def sugerir_resposta(reclamacao: "ReclamacaoPosVenda") -> str | None:
    """Rascunho de resposta da IA. DESLIGADO por enquanto (devolve None); a resposta será sempre revisada por uma pessoa."""
    return None


# ---------------------------------------------------------------------------
# Leitura do que o ML manda (tolerante)
# ---------------------------------------------------------------------------
def normalizar_reclamacao(dados: dict, seller_id: str) -> dict:
    """Campos que importam de uma reclamação, no formato interno. Nada aqui levanta erro por campo faltando."""
    jogadores = dados.get("players") or []
    vendedor = next((p for p in jogadores if str(p.get("user_id")) == str(seller_id)), None) \
        or next((p for p in jogadores if p.get("role") == "respondent"), None) or {}
    acoes = vendedor.get("available_actions") or []
    nomes = [str(a.get("action") or "") for a in acoes if isinstance(a, dict)]
    prazos = [pv._data_ml(a.get("due_date")) for a in acoes if isinstance(a, dict) and a.get("due_date")]
    prazos = [p for p in prazos if p]
    resolucao = dados.get("resolution") or {}
    recurso = str(dados.get("resource") or "")
    status = str(dados.get("status") or "")
    return {
        "claim_id": str(dados.get("id")),
        "order_id": str(dados.get("resource_id")) if dados.get("resource_id") and recurso in ("order", "") else None,
        "tipo": dados.get("type"),
        "etapa": dados.get("stage"),
        "status_ml": status,
        "motivo_id": dados.get("reason_id"),
        "pode_responder": any(n.startswith("send_message") for n in nomes),
        "precisa_acao": status == "opened" and bool(nomes),
        "prazo_em": min(prazos) if prazos else None,
        "resolucao": resolucao.get("reason") if isinstance(resolucao, dict) else None,
        "criada_ml_em": pv._data_ml(dados.get("date_created")),
        "atualizada_ml_em": pv._data_ml(dados.get("last_updated")),
    }


def _texto_do_motivo(token: str, motivo_id: str | None) -> str | None:
    """Texto do motivo pela API (com memória). Sem resposta, cai no mínimo garantido pelo prefixo do código."""
    if not motivo_id:
        return None
    if motivo_id not in _CACHE_MOTIVOS:
        texto = None
        try:
            r = buscar_motivo_reclamacao(token, motivo_id)
            texto = r.get("detail") or r.get("name")
        except (MLApiError, MLAuthError) as exc:
            logger.info("Reclamações: motivo %s sem texto na API (%s)", motivo_id, exc)
        _CACHE_MOTIVOS[motivo_id] = texto or None
    return _CACHE_MOTIVOS[motivo_id] or _MOTIVO_PELO_PREFIXO.get(motivo_id[:3].upper()) or f"Motivo {motivo_id}"


def enriquecer(token: str, base: dict, dados: dict) -> dict:
    """
    Completa a reclamação com o que exige consulta própria: texto do motivo, detalhe (título,
    descrição, quem deve agir), se afeta a reputação, comprador e produto do pedido.
    CADA consulta é independente: se uma falhar, a reclamação entra mesmo assim.
    """
    extra = {"motivo_texto": _texto_do_motivo(token, base.get("motivo_id")), "titulo": None, "descricao": None,
             "acao_responsavel": None, "afeta_reputacao": None, "comprador_nome": None, "produto_titulo": None, "sku": None}
    try:
        d = buscar_detalhe_reclamacao(token, base["claim_id"])
        extra["titulo"] = d.get("title")
        extra["descricao"] = d.get("description") or d.get("problem")
        extra["acao_responsavel"] = d.get("action_responsible")
        if d.get("due_date") and not base.get("prazo_em"):
            base["prazo_em"] = pv._data_ml(d.get("due_date"))
    except (MLApiError, MLAuthError) as exc:
        logger.info("Reclamações: sem detalhe da %s (%s)", base["claim_id"], exc)
    try:
        extra["afeta_reputacao"] = buscar_afeta_reputacao(token, base["claim_id"]).get("affects_reputation")
    except (MLApiError, MLAuthError) as exc:
        logger.info("Reclamações: sem 'afeta reputação' da %s (%s)", base["claim_id"], exc)
    if base.get("order_id"):
        try:
            pedido = buscar_pedido(token, base["order_id"])
            extra["comprador_nome"] = (pedido.get("buyer") or {}).get("nickname")
            itens = pedido.get("order_items") or []
            item = (itens[0].get("item") if itens else None) or {}
            extra["produto_titulo"] = item.get("title")
            extra["sku"] = item.get("seller_sku") or item.get("seller_custom_field")
        except (MLApiError, MLAuthError) as exc:
            logger.info("Reclamações: sem pedido da %s (%s)", base["claim_id"], exc)
    return extra


def aplicar_reclamacao(db, conta: Conta, base: dict, extra: dict | None, bruto: dict | None) -> str:
    """Grava ou atualiza UMA linha por reclamação. Devolve 'criada', 'atualizada' ou 'igual'."""
    r = db.query(ReclamacaoPosVenda).filter(ReclamacaoPosVenda.claim_id == base["claim_id"]).first()
    novo = r is None
    if novo:
        r = ReclamacaoPosVenda(conta_id=conta.id, claim_id=base["claim_id"])
        db.add(r)
    elif extra is None and r.atualizada_ml_em == base["atualizada_ml_em"] and r.status_ml == base["status_ml"]:
        return "igual"
    for k, v in base.items():
        if k != "claim_id":
            setattr(r, k, v)
    for k, v in (extra or {}).items():
        if v is not None or novo:       # não apaga o que já sabíamos se a consulta extra falhou
            setattr(r, k, v)
    if bruto is not None:
        r.dados_brutos = json.dumps(bruto, ensure_ascii=False, default=str)[:20000]
    r.sincronizada_em = datetime.utcnow()
    r.categoria = classificar(r.motivo_id, r.motivo_texto, r.titulo, r.descricao)
    if r.resposta_sugerida is None and r.status_ml == "opened":
        r.resposta_sugerida = sugerir_resposta(r)
    db.commit()
    return "criada" if novo else "atualizada"


# ---------------------------------------------------------------------------
# Busca no ML
# ---------------------------------------------------------------------------
def sincronizar_reclamacoes_conta(conta: Conta, db, dias: int = JANELA_DIAS) -> dict:
    r = {"conta": conta.apelido, "lidas": 0, "criadas": 0, "atualizadas": 0, "erro": None}
    try:
        token = garantir_token_valido(conta, db)
    except MLAuthError as exc:
        pv._marcar_auth(db, conta, True)
        r["erro"] = "reconectar"
        logger.warning("Reclamações: conta %s precisa reconectar: %s", conta.apelido, exc)
        return r
    except MLApiError as exc:
        r["erro"] = "ml"
        return r
    if conta.auth_erro_em:
        pv._marcar_auth(db, conta, False)

    limite = datetime.utcnow() - timedelta(days=dias)
    vistos: set[str] = set()
    try:
        for status in ("opened", "closed"):
            for pagina in range(PAGINAS_MAX):
                resp = buscar_reclamacoes(token, conta.ml_user_id, status, offset=pagina * 50, limite=50)
                lista = resp.get("data") or resp.get("results") or []
                fim = not lista
                for dados in lista:
                    base = normalizar_reclamacao(dados, conta.ml_user_id)
                    if not base["claim_id"] or base["claim_id"] in vistos:
                        continue
                    # fechada e antiga: descartada (e, como vêm da mais nova para a mais velha, a busca para aqui)
                    if status == "closed" and base["atualizada_ml_em"] and base["atualizada_ml_em"] < limite:
                        fim = True
                        break
                    vistos.add(base["claim_id"])
                    if len(vistos) > MAX_POR_CONTA:
                        fim = True
                        break
                    r["lidas"] += 1
                    existente = db.query(ReclamacaoPosVenda).filter(ReclamacaoPosVenda.claim_id == base["claim_id"]).first()
                    mudou = (existente is None or existente.atualizada_ml_em != base["atualizada_ml_em"] or existente.status_ml != base["status_ml"])
                    extra = enriquecer(token, base, dados) if mudou else None
                    res = aplicar_reclamacao(db, conta, base, extra, dados if mudou else None)
                    if res == "criada":
                        r["criadas"] += 1
                    elif res == "atualizada":
                        r["atualizadas"] += 1
                    if mudou:
                        time.sleep(PAUSA_SEG)
                if fim or len(lista) < 50:
                    break
    except MLAuthError:
        pv._marcar_auth(db, conta, True)
        r["erro"] = "reconectar"
    except MLApiError as exc:
        r["erro"] = "ml"
        logger.warning("Reclamações: conta %s: %s", conta.apelido, exc)
    return r


def sincronizar_todas(dias: int = JANELA_DIAS) -> list[dict]:
    """Todas as contas conectadas e liberadas (segundo plano ou botão da equipe). Erro numa conta não para as outras."""
    resultados = []
    with SessionLocal() as db:
        contas = db.query(Conta).filter(Conta.ativa.is_(True), Conta.inativa_em.is_(None), Conta.access_token.isnot(None)).all()
        for conta in contas:
            if not pv.conta_liberada(conta.apelido):
                continue
            try:
                resultados.append(sincronizar_reclamacoes_conta(conta, db, dias=dias))
            except Exception:
                logger.exception("Reclamações: erro inesperado na conta %s", conta.apelido)
                db.rollback()
    return resultados


def processar_webhook_claim(db, conta: Conta, claim_id: str) -> dict:
    """Aviso do tópico 'claims': lê só aquela reclamação e atualiza a linha."""
    if not pv.conta_liberada(conta.apelido):
        return {"status": "conta_fora_do_teste", "conta": conta.apelido}
    token = garantir_token_valido(conta, db)
    dados = buscar_claim(token, claim_id)
    logger.info("Reclamação recebida (id=%s): %s", claim_id, dados)
    base = normalizar_reclamacao(dados, conta.ml_user_id)
    base["claim_id"] = str(claim_id)
    res = aplicar_reclamacao(db, conta, base, enriquecer(token, base, dados), dados)
    return {"status": "processada", "reclamacao": res, "claim_id": str(claim_id)}


# ---------------------------------------------------------------------------
# Para a tela, o menu e o relatório
# ---------------------------------------------------------------------------
def _visiveis(db, contas_ids: list[int] | None, conta: str | None = None) -> list[ReclamacaoPosVenda]:
    """Abertas + fechadas nos últimos 10 dias (o resto é descartado)."""
    desde = datetime.utcnow() - timedelta(days=JANELA_DIAS)
    q = db.query(ReclamacaoPosVenda)
    if contas_ids is not None:
        q = q.filter(ReclamacaoPosVenda.conta_id.in_(contas_ids or [0]))
    linhas = [r for r in q.all() if r.status_ml == "opened" or (r.atualizada_ml_em and r.atualizada_ml_em >= desde)]
    if conta:
        linhas = [r for r in linhas if r.conta and r.conta.apelido == conta]
    def _ts(d):  # segundos desde 1970 (sem depender do fuso/plataforma); sem data vai para o fim
        return (d - datetime(1970, 1, 1)).total_seconds() if d else 0

    return sorted(linhas, key=lambda r: (not r.precisa_acao, -_ts(r.atualizada_ml_em)))


def estado_texto(r: ReclamacaoPosVenda) -> str:
    if r.status_ml == "closed":
        return "Finalizada"
    if r.precisa_acao or r.acao_responsavel == "seller":
        return "Aguardando a sua ação"
    if r.etapa == "dispute" or r.acao_responsavel == "mediator":
        return "Em mediação com o Mercado Livre"
    if r.acao_responsavel == "buyer":
        return "Aguardando o comprador"
    return "Em andamento"


def grupo(r: ReclamacaoPosVenda) -> str:
    """Aba da tela: 'acao' (precisa do seller) | 'andamento' | 'finalizada'."""
    if r.status_ml == "closed":
        return "finalizada"
    return "acao" if (r.precisa_acao or r.acao_responsavel == "seller") else "andamento"


def para_tela(r: ReclamacaoPosVenda) -> dict:
    return {
        "id": r.id,
        "claim_id": r.claim_id,
        "conta": r.conta.apelido if r.conta else "—",
        "conta_reconectar": bool(r.conta and r.conta.auth_erro_em),
        "pedido": r.order_id,
        "comprador": r.comprador_nome or "Comprador",
        "produto": r.produto_titulo,
        "sku": r.sku,
        "tipo": _TIPOS.get(r.tipo or "", "Reclamação"),
        "motivo": r.motivo_texto or "Motivo não informado",
        "motivo_id": r.motivo_id,
        "categoria": r.categoria,
        "titulo": r.titulo,
        "descricao": r.descricao,
        "estado": estado_texto(r),
        "grupo": grupo(r),
        "afeta_reputacao": r.afeta_reputacao,
        "afeta_texto": _AFETA.get(r.afeta_reputacao or "", ""),
        "pode_responder": bool(r.pode_responder),
        "prazo": quando_prazo(r),
        "resolucao": _RESOLUCOES.get(r.resolucao or "", r.resolucao or ""),
        "criada": pv.quando_texto(r.criada_ml_em),
        "atualizada": pv.quando_texto(r.atualizada_ml_em),
        "atualizada_em": (r.atualizada_ml_em.isoformat() + "Z") if r.atualizada_ml_em else None,
        # Abre a venda no painel do vendedor do ML, onde a reclamação aparece (a resposta aqui é a parte 2).
        "link_ml": f"https://www.mercadolivre.com.br/vendas/{r.order_id}/detalhe" if r.order_id else "https://www.mercadolivre.com.br/vendas/omni/lista",
    }


def quando_prazo(r: ReclamacaoPosVenda) -> dict | None:
    return pv.quando_texto(r.prazo_em) if r.prazo_em and r.status_ml == "opened" else None


def lista_para_tela(db, contas_ids: list[int] | None, conta: str | None = None) -> dict:
    itens = [para_tela(r) for r in _visiveis(db, contas_ids, conta)]
    motivos: dict[str, int] = {}
    for i in itens:
        motivos[i["motivo"]] = motivos.get(i["motivo"], 0) + 1
    return {
        "itens": itens,
        "motivos": [{"motivo": m, "total": n} for m, n in sorted(motivos.items(), key=lambda x: -x[1])],
        "contagem": {g: sum(1 for i in itens if i["grupo"] == g) for g in ("acao", "andamento", "finalizada")},
        "janela_dias": JANELA_DIAS,
    }


def contar_precisam_acao(db, contas_ids: list[int] | None) -> int:
    """Quantas reclamações abertas esperam o seller (menu e dashboard)."""
    q = db.query(ReclamacaoPosVenda).filter(ReclamacaoPosVenda.status_ml == "opened", ReclamacaoPosVenda.precisa_acao.is_(True))
    if contas_ids is not None:
        q = q.filter(ReclamacaoPosVenda.conta_id.in_(contas_ids or [0]))
    return q.count()


def resumo_do_relatorio(db, contas_ids: list[int] | None, conta: str | None = None) -> dict:
    linhas = _visiveis(db, contas_ids, conta)
    por_motivo: dict[str, dict] = {}
    for r in linhas:
        m = por_motivo.setdefault(r.motivo_texto or "Motivo não informado", {"motivo": r.motivo_texto or "Motivo não informado", "total": 0, "abertas": 0})
        m["total"] += 1
        m["abertas"] += r.status_ml == "opened"
    return {
        "total": len(linhas),
        "abertas": sum(1 for r in linhas if r.status_ml == "opened"),
        "precisam_acao": sum(1 for r in linhas if r.precisa_acao),
        "finalizadas": sum(1 for r in linhas if r.status_ml == "closed"),
        "afetam_reputacao": sum(1 for r in linhas if r.afeta_reputacao == "affected"),
        "por_motivo": sorted(por_motivo.values(), key=lambda x: -x["total"]),
        "itens": [para_tela(r) for r in linhas[:500]],
    }
