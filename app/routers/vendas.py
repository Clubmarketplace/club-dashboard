"""
Tela "Vendas" do painel do seller: equivalente, dentro do nosso próprio
sistema, à Central Financeira (Google Apps Script) que o Washington usa
hoje -- Venda Bruta, Repasse, Taxas e Frete, CMV, Ticket Médio, Lucro
Bruto, Lucro Líquido, Margem Mark-up e Margem de Contribuição, por
período, além da lista venda a venda com a margem classificada como
boa/ruim.

De onde vêm os dados:
  - Cada linha de venda (app/models.py:Venda) é gravada em tempo real
    pelo webhook 'orders_v2' (app/routers/webhook_ml.py), e também pela
    sincronização manual/inicial aqui embaixo (ml_client.
    sincronizar_vendas_recentes) -- pra já ter histórico na primeira
    abertura da tela, e como reforço caso algum webhook se perca.
  - CMV e lucro de cada linha usam o custo já cadastrado em CustoSku
    (mesma tabela de "Produtos" -- um SKU sem custo cadastrado aparece
    separado, igual já acontece em "Produtos > Lista").
  - As despesas que entram no Lucro Líquido são as taxas percentuais
    cadastradas na tela "Taxas" (app/routers/taxas.py -- mesma tabela
    VariavelConta que a extensão usa). Cada taxa tem uma base_calculo
    (venda_bruta/repasse/lucro) e é deduzida em cascata, na mesma ordem
    que a extensão usa pra calcular a margem de cada venda:
      Venda Bruta -> (- taxas base "venda_bruta") -> Repasse (já pronto)
      -> (- taxas base "repasse") -> Lucro Bruto (repasse - CMV)
      -> (- taxas base "lucro") -> Lucro Líquido.
    Valor fixo (ex: "contador R$500/mês") está fora de escopo por ora.

Mesmo padrão de autenticação/isolamento por conta do resto do painel
(custos_painel.py / produtos.py): cookie de sessão do seller,
`_conta_vinculada_do_usuario` garante que uma conta nunca vê dado de
outra.
"""
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import ml_client
from app.database import get_db
from app.ml_client import MLAuthError, MLApiError
from app.models import Conta, Venda, VariavelConta
from app.routers.custos_painel import _seller_logado
from app.routers.cmx import _conta_vinculada_do_usuario

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/painel/vendas", tags=["painel-vendas"])

# Pedido cancelado não é venda de verdade -- fica de fora de todos os
# totais e da lista, igual "Produtos > Lista" já ignora anúncio pausado
# nos totais de "sem custo".
_STATUS_IGNORADOS = {"cancelled"}


def _periodo(inicio: str, fim: str) -> tuple[datetime, datetime, int]:
    """
    Converte 'YYYY-MM-DD'/'YYYY-MM-DD' em (início do dia, fim do dia,
    quantidade de dias) -- sem nenhum dos dois, usa os últimos 7 dias
    (padrão da tela ao abrir pela primeira vez).
    """
    try:
        data_fim = datetime.strptime(fim, "%Y-%m-%d") if fim.strip() else datetime.utcnow()
    except ValueError:
        raise HTTPException(status_code=400, detail="Data final inválida (use AAAA-MM-DD).")
    try:
        data_inicio = (
            datetime.strptime(inicio, "%Y-%m-%d") if inicio.strip() else data_fim - timedelta(days=6)
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Data inicial inválida (use AAAA-MM-DD).")

    inicio_dt = data_inicio.replace(hour=0, minute=0, second=0, microsecond=0)
    fim_dt = data_fim.replace(hour=23, minute=59, second=59, microsecond=999999)
    if fim_dt < inicio_dt:
        raise HTTPException(status_code=400, detail="Data final não pode ser antes da inicial.")
    dias = max((fim_dt.date() - inicio_dt.date()).days + 1, 1)
    return inicio_dt, fim_dt, dias


def _sincronizar_vendas_em_segundo_plano(conta_id: int, dias: int) -> None:
    """Mesmo padrão de app/routers/produtos.py:_sincronizar_estoque_em_segundo_plano."""
    from app.database import SessionLocal

    try:
        with SessionLocal() as db:
            conta = db.query(Conta).filter(Conta.id == conta_id).first()
            if conta is None:
                return
            ml_client.sincronizar_vendas_recentes(conta, db, dias=dias)
    except (MLAuthError, MLApiError) as exc:
        logger.warning("Sincronização de vendas em segundo plano falhou pra conta %s: %s", conta_id, exc)
    except Exception:
        logger.exception("Erro inesperado sincronizando vendas em segundo plano (conta %s)", conta_id)


def _classificar_margem(conta: Conta, margem_percentual: float | None) -> str | None:
    """
    'boa' / 'ruim' / 'neutra' com base na faixa configurada pelo seller
    (Conta.margem_minima / margem_maxima). Sem faixa configurada, ou sem
    margem calculada (SKU sem custo), devolve None -- a tela mostra só o
    número, sem selo de cor.
    """
    if margem_percentual is None:
        return None
    if conta.margem_minima is not None and margem_percentual < conta.margem_minima:
        return "ruim"
    if conta.margem_maxima is not None and margem_percentual > conta.margem_maxima:
        return "ruim"
    if conta.margem_minima is None and conta.margem_maxima is None:
        return None
    return "boa"


def _cascata_taxas(taxas: list[VariavelConta], venda_bruta: float, repasse: float, lucro_bruto: float) -> tuple[list[dict], float]:
    """
    Aplica as taxas percentuais cadastradas em cascata -- Venda Bruta ->
    Repasse -> Lucro -- e devolve (lista de deduções aplicadas,
    lucro_líquido resultante). Usada tanto pro resumo agregado do
    período (_montar_resumo) quanto pra mostrar, venda a venda, quais
    taxas entraram na conta de cada linha (listar_vendas).
    """
    taxas_venda_bruta = [t for t in taxas if (t.base_calculo or "venda_bruta") == "venda_bruta"]
    taxas_repasse = [t for t in taxas if t.base_calculo == "repasse"]
    taxas_lucro = [t for t in taxas if t.base_calculo == "lucro"]

    aplicadas = []

    total_venda_bruta = 0.0
    for t in taxas_venda_bruta:
        valor = round(venda_bruta * t.percentual / 100, 2)
        total_venda_bruta += valor
        aplicadas.append({"nome": t.nome, "percentual": t.percentual, "base_calculo": "venda_bruta", "valor": valor})

    total_repasse = 0.0
    for t in taxas_repasse:
        valor = round(repasse * t.percentual / 100, 2)
        total_repasse += valor
        aplicadas.append({"nome": t.nome, "percentual": t.percentual, "base_calculo": "repasse", "valor": valor})

    lucro_antes_taxas_lucro = lucro_bruto - total_venda_bruta - total_repasse

    total_lucro = 0.0
    for t in taxas_lucro:
        valor = round(lucro_antes_taxas_lucro * t.percentual / 100, 2)
        total_lucro += valor
        aplicadas.append({"nome": t.nome, "percentual": t.percentual, "base_calculo": "lucro", "valor": valor})

    lucro_liquido = lucro_antes_taxas_lucro - total_lucro
    return aplicadas, lucro_liquido


def _montar_resumo(conta: Conta, db: Session, inicio_dt: datetime, fim_dt: datetime, dias: int) -> dict:
    consulta = (
        db.query(Venda)
        .filter(Venda.conta_id == conta.id)
        .filter(Venda.data_venda >= inicio_dt, Venda.data_venda <= fim_dt)
        .filter(~Venda.status_pedido.in_(_STATUS_IGNORADOS))
    )
    linhas = consulta.all()

    pedidos = {l.ml_order_id for l in linhas}
    unidades = sum(l.quantidade for l in linhas)
    venda_bruta = sum(l.venda_bruta for l in linhas)
    repasse = sum(l.repasse for l in linhas)
    taxa_ml = sum(l.taxa_ml for l in linhas)
    frete = sum(l.frete for l in linhas)
    cmv = sum(l.custo_total for l in linhas if l.custo_total is not None)
    qtd_sem_custo = sum(1 for l in linhas if l.custo_total is None)

    lucro_bruto = repasse - cmv

    # Taxas percentuais cadastradas em "Taxas" (app/routers/taxas.py),
    # deduzidas em cascata na mesma ordem usada pela extensão pra
    # calcular a margem de cada venda -- ver docstring do módulo.
    taxas = db.query(VariavelConta).filter(VariavelConta.conta_id == conta.id).all()
    despesas_taxas, lucro_liquido = _cascata_taxas(taxas, venda_bruta, repasse, lucro_bruto)

    margem_markup = (lucro_liquido / cmv * 100) if cmv else None
    margem_contribuicao = (lucro_liquido / venda_bruta * 100) if venda_bruta else None

    return {
        "periodo": {"inicio": inicio_dt.date().isoformat(), "fim": fim_dt.date().isoformat(), "dias": dias},
        "vendas": len(pedidos),
        "unidades": unidades,
        "venda_bruta": round(venda_bruta, 2),
        "repasse": round(repasse, 2),
        "taxas_e_frete": round(taxa_ml + frete, 2),
        "cmv": round(cmv, 2),
        "qtd_sem_custo": qtd_sem_custo,
        "ticket_medio": round(venda_bruta / len(pedidos), 2) if pedidos else 0.0,
        "lucro_bruto": round(lucro_bruto, 2),
        "lucro_liquido": round(lucro_liquido, 2),
        "margem_markup": round(margem_markup, 2) if margem_markup is not None else None,
        "margem_contribuicao": round(margem_contribuicao, 2) if margem_contribuicao is not None else None,
        "despesas_taxas": despesas_taxas,
        "margem_minima": conta.margem_minima,
        "margem_maxima": conta.margem_maxima,
    }


@router.get("/resumo")
def resumo_vendas(
    background_tasks: BackgroundTasks,
    inicio: str = "",
    fim: str = "",
    usuario=Depends(_seller_logado),
    db: Session = Depends(get_db),
):
    """
    Cards da tela: vendas, venda bruta, repasse, taxas e frete, CMV,
    ticket médio, lucro bruto, lucro líquido, margem mark-up e margem
    de contribuição, no período pedido (padrão: últimos 7 dias).
    """
    conta = _conta_vinculada_do_usuario(usuario, db)
    inicio_dt, fim_dt, dias = _periodo(inicio, fim)

    tem_venda_salva = db.query(Venda).filter(Venda.conta_id == conta.id).first() is not None
    resposta = _montar_resumo(conta, db, inicio_dt, fim_dt, dias)

    if not tem_venda_salva:
        # CORREÇÃO (03/10): a primeira busca (30 dias de pedidos, um a um
        # no Mercado Livre) rodava na hora, esperando terminar -- numa
        # conta com muitos pedidos isso estourava o tempo da requisição e
        # a tela só mostrava "não consegui carregar", sem nunca terminar.
        # Agora, igual "Produtos > Lista": responde JÁ (ainda zerado) e
        # busca os 30 dias em segundo plano -- a próxima vez que abrir a
        # tela (ou clicar "Atualizar agora"), já vem preenchido.
        background_tasks.add_task(_sincronizar_vendas_em_segundo_plano, conta.id, 30)
        resposta["aviso"] = (
            "Buscando suas vendas dos últimos 30 dias pela primeira vez -- "
            "isso roda em segundo plano e pode levar alguns minutos numa conta "
            "com bastante pedido. Atualize a página daqui a pouco pra ver preenchido."
        )
    else:
        # Reforço silencioso: cobre qualquer pedido recente cujo webhook
        # não tenha chegado. Período curto (2 dias) pra ser rápido.
        background_tasks.add_task(_sincronizar_vendas_em_segundo_plano, conta.id, 2)

    return resposta


@router.get("/lista")
def listar_vendas(
    inicio: str = "",
    fim: str = "",
    margem: str = "",
    usuario=Depends(_seller_logado),
    db: Session = Depends(get_db),
):
    """
    Venda a venda do período, mais recente primeiro -- com a margem já
    classificada (boa/ruim/None) pra pintar a linha na tela.
    `margem`: filtro opcional ("boa" ou "ruim"), igual ao card clicável
    de "Produtos > Lista".
    """
    conta = _conta_vinculada_do_usuario(usuario, db)
    inicio_dt, fim_dt, _ = _periodo(inicio, fim)

    linhas = (
        db.query(Venda)
        .filter(Venda.conta_id == conta.id)
        .filter(Venda.data_venda >= inicio_dt, Venda.data_venda <= fim_dt)
        .filter(~Venda.status_pedido.in_(_STATUS_IGNORADOS))
        .order_by(Venda.data_venda.desc())
        .all()
    )

    # Taxas cadastradas em "Taxas" -- buscadas uma vez só e aplicadas em
    # cascata linha a linha, pra mostrar na lista exatamente quais taxas
    # entraram na conta de cada venda até chegar na margem/lucro líquido
    # exibido (o mesmo pedido que o Washington fez: "poderia colocar na
    # tela de visualização de venda a venda as taxas aplicadas e usadas
    # para chegar na margem positiva").
    taxas = db.query(VariavelConta).filter(VariavelConta.conta_id == conta.id).all()

    itens = []
    for l in linhas:
        classificacao = _classificar_margem(conta, l.margem_percentual)
        if margem.strip() and classificacao != margem.strip():
            continue

        if l.custo_total is not None and l.lucro is not None:
            taxas_aplicadas, lucro_liquido = _cascata_taxas(taxas, l.venda_bruta, l.repasse, l.lucro)
            lucro_liquido = round(lucro_liquido, 2)
        else:
            # Sem custo cadastrado não dá pra calcular lucro nenhum
            # (bruto ou líquido) -- mesma regra já usada pra "sem custo".
            taxas_aplicadas, lucro_liquido = [], None

        itens.append(
            {
                "id": l.id,
                "ml_order_id": l.ml_order_id,
                "item_id": l.item_id,
                "sku": l.sku,
                "titulo": l.titulo,
                "quantidade": l.quantidade,
                "preco_unitario": l.preco_unitario,
                "venda_bruta": l.venda_bruta,
                "taxa_ml": l.taxa_ml,
                "frete": l.frete,
                "repasse": l.repasse,
                "custo_total": l.custo_total,
                "lucro": l.lucro,
                "taxas_aplicadas": taxas_aplicadas,
                "lucro_liquido": lucro_liquido,
                "margem_percentual": l.margem_percentual,
                "margem_classificacao": classificacao,
                "status_pedido": l.status_pedido,
                "data_venda": l.data_venda.isoformat() if l.data_venda else None,
            }
        )

    return {"itens": itens}


@router.post("/sincronizar")
def sincronizar_vendas_agora(
    dias: int = 30,
    usuario=Depends(_seller_logado),
    db: Session = Depends(get_db),
):
    """Botão "Atualizar agora": busca de novo no Mercado Livre e espera terminar."""
    conta = _conta_vinculada_do_usuario(usuario, db)
    try:
        total = ml_client.sincronizar_vendas_recentes(conta, db, dias=min(max(dias, 1), 90))
    except MLAuthError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except MLApiError as exc:
        raise HTTPException(status_code=502, detail=f"Não consegui consultar as vendas no Mercado Livre agora: {exc}")
    return {"pedidos_processados": total}


# ---------------------------------------------------------------------------
# Configuração: faixa de margem boa/ruim + despesas (percentuais e fixas)
# ---------------------------------------------------------------------------

class MargemEntrada(BaseModel):
    margem_minima: float | None = None
    margem_maxima: float | None = None


@router.get("/margem")
def obter_margem(usuario=Depends(_seller_logado), db: Session = Depends(get_db)):
    conta = _conta_vinculada_do_usuario(usuario, db)
    return {"margem_minima": conta.margem_minima, "margem_maxima": conta.margem_maxima}


@router.post("/margem")
def definir_margem(dados: MargemEntrada, usuario=Depends(_seller_logado), db: Session = Depends(get_db)):
    conta = _conta_vinculada_do_usuario(usuario, db)
    conta.margem_minima = dados.margem_minima
    conta.margem_maxima = dados.margem_maxima
    db.commit()
    return {"margem_minima": conta.margem_minima, "margem_maxima": conta.margem_maxima}


#  As taxas percentuais (VariavelConta) agora têm tela própria --
#  ver app/routers/taxas.py (GET/POST/DELETE /api/painel/taxas).
#  Despesa fixa mensal (DespesaFixaConta) saiu de escopo por decisão
#  do cliente; o modelo continua no banco (sem uso) e pode voltar.
