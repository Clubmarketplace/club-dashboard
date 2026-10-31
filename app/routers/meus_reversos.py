"""
"Meus reversos" (31/10) -- painel do SELLER com tudo sobre os reversos e garantias da PRÓPRIA conta.

Regras:
  * A conta vem SEMPRE do usuário logado (conta vinculada); nenhum parâmetro de conta é aceito, então um seller
    nunca vê outra conta. Papéis: seller e suporte com conta vinculada (os mesmos do painel do seller).
  * Só leitura. Fotos nunca são lidas aqui (a lista de fotos continua em /meus-cancelamentos/fotos).
  * Mesma lógica de números do painel do gestor: custo exato do cadastro da conta (ver reversos_gestor),
    remessas à fábrica (ver remessas_garantia). Garantia sem custo fica fora do valor e é contada à parte.
"""
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import auth
from app.contas_util import chave_conta
from app.database import get_db
from app.models import RemessaGarantia
from app.routers.painel_galpao import _nome_situacao, _situacao_reverso
from app.routers.remessas_garantia import (
    STATUS_ANALISE, STATUS_ENCERRADA, _hoje_br, remessa_para_json, remessas_dos_reversos,
)
from app.routers.reversos_gestor import (
    GARANTIA, CorrigirSkuBody, CustoManualBody, _base, _custos_dos_reversos, _intervalo, _item_garantia,
    corrigir_sku_reverso, gravar_custo_manual, montar_por_conta, montar_resumo_gestor, skus_da_conta,
)
from app.routers.solicitacoes_cancelamento import FUSO_BR, _limites_utc_produtividade
from app.models import SolicitacaoCancelamento

router = APIRouter(prefix="/api/meus-reversos", tags=["meus-reversos"])

PAPEIS_SELLER = ("seller", "suporte")
MAX_REMESSAS = 200


def usuario_seller(request: Request, db: Session):
    usuario = auth.usuario_atual(request, db)
    if usuario is None:
        raise HTTPException(status_code=401, detail="Sessão expirada -- faça login de novo.")
    if usuario.papel not in PAPEIS_SELLER or not usuario.conta_vinculada:
        raise HTTPException(status_code=403, detail="Esta tela é só para o seller, da conta vinculada a ele.")
    return usuario


def _pct(a: float, b: float) -> Optional[int]:
    return round(a * 100 / b) if b else None


def montar_valores(remessas_analise, remessas_encerradas, remessas_enviadas, de: date, ate: date) -> dict:
    """Valores das remessas (função pura). Dia de cada remessa = dia de envio, no fuso do Brasil."""
    ana = {"remessas": len(remessas_analise), "itens": sum(r.qtd_itens for r in remessas_analise),
           "valor": round(sum(r.valor_total for r in remessas_analise), 2)}
    enc = {"remessas": len(remessas_encerradas), "enviado": 0.0, "ressarcido": 0.0, "credito": 0.0, "trocado": 0.0, "perdido": 0.0}
    for r in remessas_encerradas:
        enc["enviado"] += r.valor_total; enc["ressarcido"] += r.ressarcido; enc["credito"] += r.credito
        enc["trocado"] += r.trocado; enc["perdido"] += r.perdido
    enc = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in enc.items()}
    enc["recuperado"] = round(enc["ressarcido"] + enc["credito"] + enc["trocado"], 2)
    enc["pct_recuperado"] = _pct(enc["recuperado"], enc["enviado"])
    enc["pct_perdido"] = _pct(enc["perdido"], enc["enviado"])
    dias = {de + timedelta(days=i): {"analise": 0.0, "recuperado": 0.0, "perdido": 0.0} for i in range((ate - de).days + 1)}
    for r in remessas_enviadas:
        d = r.data_envio.date()
        if d in dias:
            if r.status == STATUS_ANALISE:
                dias[d]["analise"] += r.valor_total
            else:
                dias[d]["recuperado"] += r.ressarcido + r.credito + r.trocado
                dias[d]["perdido"] += r.perdido
    por_dia = [{"data": d.isoformat(), **{k: round(v, 2) for k, v in x.items()}} for d, x in sorted(dias.items())]
    return {"em_analise": ana, "encerradas": enc, "por_dia": por_dia}


@router.get("/painel")
def painel(request: Request, dias: Optional[int] = None, de: Optional[str] = None, ate: Optional[str] = None,
           db: Session = Depends(get_db)):
    usuario = usuario_seller(request, db)
    chave = chave_conta(usuario.conta_vinculada)
    d1, d2 = _intervalo(dias, de, ate)
    inicio, fim = _limites_utc_produtividade(d1, d2)

    base = (_base(db).filter(SolicitacaoCancelamento.conta_chave == chave))
    reversos = (base.filter(SolicitacaoCancelamento.criado_em >= inicio, SolicitacaoCancelamento.criado_em < fim)
                .order_by(SolicitacaoCancelamento.criado_em.asc()).all())
    aguardando_agora = base.filter(SolicitacaoCancelamento.confirmado_por.is_(None)).all()
    resumo = montar_resumo_gestor(reversos, d1, d2, aguardando_agora)

    garantias = [s for s in reversos if _situacao_reverso(s) == GARANTIA]
    custos = _custos_dos_reversos(db, garantias)
    no_envio = remessas_dos_reversos(db, [s.id for s in garantias])
    conta = (montar_por_conta(reversos, custos, set(no_envio))["contas"] or [None])[0] or \
        {"retornaram": 0, "voltaram": 0, "garantias": 0, "custo_garantia": 0.0, "sem_custo": 0, "enviadas": 0, "custo_enviado": 0.0}
    parado_valor = round(max(0.0, conta["custo_garantia"] - conta["custo_enviado"]), 2)

    rem = db.query(RemessaGarantia).filter(func.lower(RemessaGarantia.conta_chave) == chave)
    analise = rem.filter(RemessaGarantia.status == STATUS_ANALISE).all()
    encerradas = rem.filter(RemessaGarantia.status == STATUS_ENCERRADA, RemessaGarantia.encerrada_em >= inicio,
                            RemessaGarantia.encerrada_em < fim).all()
    enviadas = rem.filter(RemessaGarantia.status.in_((STATUS_ANALISE, STATUS_ENCERRADA)),
                          RemessaGarantia.data_envio >= inicio, RemessaGarantia.data_envio < fim).all()
    valores = montar_valores(analise, encerradas, enviadas, d1, d2)

    lista = (rem.filter(RemessaGarantia.status.in_((STATUS_ANALISE, STATUS_ENCERRADA)))
             .order_by(RemessaGarantia.data_envio.desc()).limit(MAX_REMESSAS).all())
    hoje = _hoje_br()
    return {
        "conta": usuario.conta_vinculada,
        "periodo": resumo["periodo"],
        "cartoes": {
            "retornaram": conta["retornaram"], "voltaram": conta["voltaram"], "garantias": conta["garantias"],
            "pct_voltaram": _pct(conta["voltaram"], conta["retornaram"]),
            "pct_garantias": _pct(conta["garantias"], conta["retornaram"]),
            "valor_garantia": conta["custo_garantia"], "sem_custo": conta["sem_custo"],
            "parado_qtd": conta["garantias"] - conta["enviadas"], "parado_valor": parado_valor,
            "enviado_itens": conta["enviadas"], "enviado_valor": conta["custo_enviado"],
            "pct_enviado": _pct(conta["custo_enviado"], conta["custo_garantia"]),
            "aguardando_equipe": resumo["kpis"]["aguardando"],
        },
        "valores": valores,
        "por_dia": resumo["por_dia"],
        "resultado": resumo["resultado"],
        "remessas": [remessa_para_json(r, hoje, com_itens=True) for r in lista],
    }


@router.get("/garantias")
def minhas_garantias(request: Request, dias: Optional[int] = None, de: Optional[str] = None, ate: Optional[str] = None,
                     db: Session = Depends(get_db)):
    """Lista das garantias da própria conta (com custo e nota da remessa, se já foi para a fábrica)."""
    usuario = usuario_seller(request, db)
    chave = chave_conta(usuario.conta_vinculada)
    d1, d2 = _intervalo(dias, de, ate)
    inicio, fim = _limites_utc_produtividade(d1, d2)
    linhas = (_base(db).filter(SolicitacaoCancelamento.conta_chave == chave,
                               SolicitacaoCancelamento.criado_em >= inicio, SolicitacaoCancelamento.criado_em < fim)
              .order_by(SolicitacaoCancelamento.criado_em.desc()).all())
    linhas = [s for s in linhas if _situacao_reverso(s) == GARANTIA]
    custos = _custos_dos_reversos(db, linhas)
    no_envio = remessas_dos_reversos(db, [s.id for s in linhas])
    itens = []
    for s in linhas[:500]:
        i = _item_garantia(s, custos)
        itens.append({"id": i["id"], "recebido_em": i["recebido_em"], "numero_venda": i["numero_venda"], "sku": i["sku"],
                      "produto": i["produto"] or i.get("custo_produto") or "", "custo": i["custo"],
                      "remessa": no_envio.get(s.id)})
    return {"total": len(linhas), "itens": itens}


def _reverso_da_conta(db: Session, usuario, solicitacao_id: int) -> SolicitacaoCancelamento:
    """Reverso pedido pelo seller: só se for da conta dele (senão 404, sem revelar que existe)."""
    s = (_base(db).filter(SolicitacaoCancelamento.id == solicitacao_id,
                          SolicitacaoCancelamento.conta_chave == chave_conta(usuario.conta_vinculada)).first())
    if s is None:
        raise HTTPException(status_code=404, detail="Reverso não encontrado.")
    return s


@router.get("/reversos")
def meus_reversos(request: Request, situacao: str = "todos", dias: Optional[int] = None, de: Optional[str] = None,
                  ate: Optional[str] = None, db: Session = Depends(get_db)):
    """Todos os reversos da própria conta no período (garantia, voltou ao estoque, outros, aguardando), com SKU, custo
    e quem ajustou. `situacao`: todos | garantia | voltou | outros | aguardando. Só leitura."""
    usuario = usuario_seller(request, db)
    chave = chave_conta(usuario.conta_vinculada)
    d1, d2 = _intervalo(dias, de, ate)
    inicio, fim = _limites_utc_produtividade(d1, d2)
    linhas = (_base(db).filter(SolicitacaoCancelamento.conta_chave == chave,
                               SolicitacaoCancelamento.criado_em >= inicio, SolicitacaoCancelamento.criado_em < fim)
              .order_by(SolicitacaoCancelamento.criado_em.desc()).all())

    def grupo(s):
        sit = _situacao_reverso(s)
        return sit if sit in ("garantia", "voltou", "aguardando") else "outros"

    contagem = {"todos": len(linhas), "garantia": 0, "voltou": 0, "outros": 0, "aguardando": 0}
    for s in linhas:
        contagem[grupo(s)] += 1
    if situacao in ("garantia", "voltou", "outros", "aguardando"):
        linhas = [s for s in linhas if grupo(s) == situacao]
    total = len(linhas)
    linhas = linhas[:500]
    custos = _custos_dos_reversos(db, linhas)
    no_envio = remessas_dos_reversos(db, [s.id for s in linhas if _situacao_reverso(s) == GARANTIA])
    itens = []
    for s in linhas:
        i = _item_garantia(s, custos)
        g = grupo(s)
        remessa = no_envio.get(s.id)
        itens.append({
            "id": s.id, "recebido_em": i["recebido_em"], "numero_venda": i["numero_venda"], "sku": i["sku"],
            "produto": i["produto"] or i.get("custo_produto") or "", "grupo": g,
            "situacao": _nome_situacao(_situacao_reverso(s)), "observacao": i["observacao"],
            "custo": i["custo"] if g == "garantia" else None, "custo_origem": i["custo_origem"] if g == "garantia" else None,
            "custo_motivo": i["custo_motivo"] if g == "garantia" else None,
            "custo_por": i["custo_por"], "custo_papel": i["custo_papel"], "custo_em": i["custo_em"],
            "parecidos": i["parecidos"] if g == "garantia" else [],
            "sku_original": i["sku_original"], "sku_corrigido_por": i["sku_corrigido_por"],
            "sku_corrigido_papel": i["sku_corrigido_papel"], "sku_corrigido_em": i["sku_corrigido_em"],
            "remessa": remessa, "editavel": not remessa and g != "aguardando",
        })
    return {"conta": usuario.conta_vinculada, "contagem": contagem, "total": total, "itens": itens}


@router.get("/skus")
def meus_skus(request: Request, q: str = "", db: Session = Depends(get_db)):
    """SKUs do cadastro de Custos da PRÓPRIA conta (para o seller escolher o SKU correto)."""
    usuario = usuario_seller(request, db)
    return skus_da_conta(db, usuario.conta_vinculada, q)


@router.post("/reversos/{solicitacao_id}/sku")
def corrigir_meu_sku(solicitacao_id: int, corpo: CorrigirSkuBody, request: Request, db: Session = Depends(get_db)):
    usuario = usuario_seller(request, db)
    s = _reverso_da_conta(db, usuario, solicitacao_id)
    if _situacao_reverso(s) == "aguardando":
        raise HTTPException(status_code=409, detail="Esse reverso ainda aguarda a equipe do galpão.")
    r = corrigir_sku_reverso(db, s, corpo.sku_novo, usuario, corpo.sku_antigo)
    db.commit()
    return r


@router.post("/garantias/{solicitacao_id}/custo")
def ajustar_meu_custo(solicitacao_id: int, corpo: CustoManualBody, request: Request, db: Session = Depends(get_db)):
    usuario = usuario_seller(request, db)
    s = _reverso_da_conta(db, usuario, solicitacao_id)
    r = gravar_custo_manual(db, s, corpo.custo, usuario)
    db.commit()
    return r
