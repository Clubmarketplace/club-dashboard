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
  - As despesas que entram no Lucro Líquido são as que o Washington já
    configura: percentuais (VariavelConta -- imposto, CLUB etc, a mesma
    tabela que a extensão usa) e fixas mensais (DespesaFixaConta --
    Contabilidade, Bling etc, ratedas pelos dias do período filtrado).

Mesmo padrão de autenticação/isolamento por conta do resto do painel
(custos_painel.py / produtos.py): cookie de sessão do seller,
`_conta_vinculada_do_usuario` garante que uma conta nunca vê dado de
outra.
"""
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app import ml_client
from app.database import get_db
from app.ml_client import MLAuthError, MLApiError
from app.models import Conta, DespesaFixaConta, Venda, VariavelConta
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

    percentuais = db.query(VariavelConta).filter(VariavelConta.conta_id == conta.id).all()
    despesas_percentuais = [
        {"nome": v.nome, "percentual": v.percentual, "valor": round(venda_bruta * v.percentual / 100, 2)}
        for v in percentuais
    ]
    total_despesas_percentuais = sum(d["valor"] for d in despesas_percentuais)

    fixas = db.query(DespesaFixaConta).filter(DespesaFixaConta.conta_id == conta.id).all()
    despesas_fixas = [
        {
            "nome": d.nome,
            "valor_mensal": d.valor_mensal,
            # Rateio simples: valor_mensal / 30 dias x dias do período filtrado.
            "valor_periodo": round(d.valor_mensal / 30 * dias, 2),
        }
        for d in fixas
    ]
    total_despesas_fixas = sum(d["valor_periodo"] for d in despesas_fixas)

    lucro_liquido = lucro_bruto - total_despesas_percentuais - total_despesas_fixas

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
        "despesas_percentuais": despesas_percentuais,
        "despesas_fixas": despesas_fixas,
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
    if not tem_venda_salva:
        # Primeira vez que a conta abre a tela: busca os últimos 30 dias
        # na hora (só essa vez é mais lenta) pra não abrir vazia.
        try:
            ml_client.sincronizar_vendas_recentes(conta, db, dias=30)
        except MLAuthError as exc:
            resposta = _montar_resumo(conta, db, inicio_dt, fim_dt, dias)
            resposta["aviso"] = str(exc)
            return resposta
        except MLApiError as exc:
            resposta = _montar_resumo(conta, db, inicio_dt, fim_dt, dias)
            resposta["aviso"] = f"Não consegui consultar as vendas no Mercado Livre agora: {exc}"
            return resposta
    else:
        # Reforço silencioso: cobre qualquer pedido recente cujo webhook
        # não tenha chegado. Período curto (2 dias) pra ser rápido.
        background_tasks.add_task(_sincronizar_vendas_em_segundo_plano, conta.id, 2)

    return _montar_resumo(conta, db, inicio_dt, fim_dt, dias)


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

    itens = []
    for l in linhas:
        classificacao = _classificar_margem(conta, l.margem_percentual)
        if margem.strip() and classificacao != margem.strip():
            continue
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


class DespesaFixaEntrada(BaseModel):
    nome: str
    valor_mensal: float

    @field_validator("nome")
    @classmethod
    def _nome_nao_vazio(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("Nome não pode ser vazio.")
        return v

    @field_validator("valor_mensal")
    @classmethod
    def _valor_nao_negativo(cls, v: float) -> float:
        if v < 0:
            raise ValueError("Valor mensal não pode ser negativo.")
        return v


@router.get("/despesas-fixas")
def listar_despesas_fixas(usuario=Depends(_seller_logado), db: Session = Depends(get_db)):
    conta = _conta_vinculada_do_usuario(usuario, db)
    despesas = db.query(DespesaFixaConta).filter(DespesaFixaConta.conta_id == conta.id).order_by(DespesaFixaConta.nome).all()
    return {
        "itens": [
            {"nome": d.nome, "valor_mensal": d.valor_mensal, "atualizado_por": d.atualizado_por}
            for d in despesas
        ]
    }


@router.post("/despesas-fixas")
def gravar_despesa_fixa(dados: DespesaFixaEntrada, usuario=Depends(_seller_logado), db: Session = Depends(get_db)):
    conta = _conta_vinculada_do_usuario(usuario, db)
    registro = (
        db.query(DespesaFixaConta)
        .filter(DespesaFixaConta.conta_id == conta.id, DespesaFixaConta.nome == dados.nome)
        .first()
    )
    if registro is None:
        registro = DespesaFixaConta(conta_id=conta.id, nome=dados.nome)
        db.add(registro)
    registro.valor_mensal = dados.valor_mensal
    registro.atualizado_por = usuario.nome_exibicao
    db.commit()
    return {"nome": registro.nome, "valor_mensal": registro.valor_mensal}


@router.delete("/despesas-fixas/{nome}")
def remover_despesa_fixa(nome: str, usuario=Depends(_seller_logado), db: Session = Depends(get_db)):
    conta = _conta_vinculada_do_usuario(usuario, db)
    registro = (
        db.query(DespesaFixaConta)
        .filter(DespesaFixaConta.conta_id == conta.id, DespesaFixaConta.nome == nome)
        .first()
    )
    if registro is None:
        raise HTTPException(status_code=404, detail="Despesa não encontrada nessa conta.")
    db.delete(registro)
    db.commit()
    return {"removida": nome}


class VariavelEntradaPainel(BaseModel):
    nome: str
    percentual: float

    @field_validator("nome")
    @classmethod
    def _nome_nao_vazio(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("Nome não pode ser vazio.")
        return v

    @field_validator("percentual")
    @classmethod
    def _percentual_valido(cls, v: float) -> float:
        if v < 0:
            raise ValueError("Percentual não pode ser negativo.")
        return v


@router.get("/variaveis")
def listar_variaveis_painel(usuario=Depends(_seller_logado), db: Session = Depends(get_db)):
    """
    Mesma tabela VariavelConta que a extensão usa (imposto, CLUB etc) --
    exposta aqui também pelo painel (cookie de sessão), pra configurar
    pelo "Vendas" sem precisar abrir a extensão.
    """
    conta = _conta_vinculada_do_usuario(usuario, db)
    variaveis = db.query(VariavelConta).filter(VariavelConta.conta_id == conta.id).order_by(VariavelConta.nome).all()
    return {"itens": [{"nome": v.nome, "percentual": v.percentual} for v in variaveis]}


@router.post("/variaveis")
def gravar_variavel_painel(dados: VariavelEntradaPainel, usuario=Depends(_seller_logado), db: Session = Depends(get_db)):
    conta = _conta_vinculada_do_usuario(usuario, db)
    registro = (
        db.query(VariavelConta)
        .filter(VariavelConta.conta_id == conta.id, VariavelConta.nome == dados.nome)
        .first()
    )
    if registro is None:
        registro = VariavelConta(conta_id=conta.id, nome=dados.nome)
        db.add(registro)
    registro.percentual = dados.percentual
    registro.atualizado_por = usuario.nome_exibicao
    db.commit()
    return {"nome": registro.nome, "percentual": registro.percentual}


@router.delete("/variaveis/{nome}")
def remover_variavel_painel(nome: str, usuario=Depends(_seller_logado), db: Session = Depends(get_db)):
    conta = _conta_vinculada_do_usuario(usuario, db)
    registro = (
        db.query(VariavelConta)
        .filter(VariavelConta.conta_id == conta.id, VariavelConta.nome == nome)
        .first()
    )
    if registro is None:
        raise HTTPException(status_code=404, detail="Variável não encontrada nessa conta.")
    db.delete(registro)
    db.commit()
    return {"removida": nome}
