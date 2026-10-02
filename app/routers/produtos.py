"""
Tela "Produtos > Lista" do painel do seller: junta o custo (guardado no
nosso banco, tabela CustoSku) com o estoque ATUAL de cada SKU, lido ao
vivo da API do Mercado Livre -- nunca guardamos quantidade nenhuma no
nosso banco, é sempre um espelho em tempo real do que está lá.

Reaproveita:
  - `_conta_vinculada_do_usuario` / `_seller_logado`, iguais ao resto do
    painel (cmx.py / custos_painel.py), pra nunca misturar dado de uma
    conta com outra.
  - `ml_client.listar_estoque_por_sku`, que faz a varredura no Mercado
    Livre e soma a quantidade por SKU (somando variações, ex: 127V e
    220V do mesmo produto).
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app import ml_client
from app.database import get_db
from app.models import CustoSku
from app.ml_client import MLAuthError, MLApiError
from app.routers.custos_painel import _seller_logado
from app.routers.cmx import _conta_vinculada_do_usuario

router = APIRouter(prefix="/api/painel/produtos", tags=["painel-produtos"])


@router.get("/lista")
def listar_produtos(
    sku: str = "",
    usuario=Depends(_seller_logado),
    db: Session = Depends(get_db),
):
    """
    Devolve, para a conta do seller logado:
      - itens: lista de {sku, nome_produto, custo, quantidade, valor_total}
      - resumo: totais pra exibir nos cards (estoque total, valor total,
        quantidade de SKUs sem custo, sem estoque)
      - estoque_indisponivel: true se não deu pra consultar o Mercado
        Livre agora (conta sem token, ou erro de rede) -- nesse caso os
        itens vêm só com o custo, quantidade fica null, e o aviso deve
        aparecer na tela em vez de travar a página inteira.

    `sku`: filtro opcional (contém), igual à busca que já existe em
    "Custos" -- aqui filtra a lista já combinada com o estoque.
    """
    conta = _conta_vinculada_do_usuario(usuario, db)

    custos = db.query(CustoSku).filter(CustoSku.conta_id == conta.id).all()
    custos_por_sku = {c.sku: c for c in custos}

    estoque_indisponivel = False
    aviso_estoque = None
    estoque_por_sku: dict = {}
    try:
        estoque_por_sku = ml_client.listar_estoque_por_sku(conta, db)
    except MLAuthError as exc:
        estoque_indisponivel = True
        aviso_estoque = str(exc)
    except MLApiError as exc:
        estoque_indisponivel = True
        aviso_estoque = f"Não consegui consultar o estoque no Mercado Livre agora: {exc}"

    # União dos SKUs que têm custo cadastrado com os que têm anúncio no
    # ML (pode ter anúncio sem custo cadastrado ainda, ou custo
    # cadastrado de um produto que não tem mais anúncio ativo).
    todos_skus = set(custos_por_sku.keys()) | set(estoque_por_sku.keys())

    itens = []
    estoque_total = 0
    valor_total = 0.0
    sem_custo = 0
    sem_estoque = 0

    for sku_atual in sorted(todos_skus):
        custo_registro = custos_por_sku.get(sku_atual)
        estoque_registro = estoque_por_sku.get(sku_atual)

        custo = custo_registro.custo if custo_registro else None
        quantidade = estoque_registro["quantidade"] if estoque_registro else (None if estoque_indisponivel else 0)
        nome_produto = (custo_registro.nome_produto if custo_registro else None) or (
            estoque_registro["titulo"] if estoque_registro else None
        )

        valor_item = (custo * quantidade) if (custo is not None and quantidade is not None) else None

        if custo is None:
            sem_custo += 1
        if quantidade == 0:
            sem_estoque += 1
        if quantidade:
            estoque_total += quantidade
        if valor_item:
            valor_total += valor_item

        itens.append(
            {
                "sku": sku_atual,
                "nome_produto": nome_produto,
                "custo": custo,
                "quantidade": quantidade,
                "valor_total": valor_item,
            }
        )

    if sku.strip():
        termo = sku.strip().lower()
        itens = [i for i in itens if termo in i["sku"].lower()]

    # DIAGNÓSTICO (02/10): lista SKUs que vieram de mais de um anúncio
    # diferente no Mercado Livre -- cada um desses tem a quantidade somada
    # de todos os anúncios que usam esse SKU, o que só está certo se forem
    # variações do MESMO produto (ex: 127V/220V). Se forem anúncios
    # distintos (ex: cores diferentes) usando o mesmo SKU por engano de
    # cadastro, a soma aqui está inflando a quantidade (e o valor) desse
    # SKU. Não muda nenhum cálculo existente -- só expõe o que já está
    # acontecendo, pra investigar antes de decidir o que fazer.
    skus_em_multiplos_anuncios = [
        {
            "sku": sku_atual,
            "quantidade_somada": info["quantidade"],
            "qtd_por_anuncio": info.get("qtd_por_anuncio", {}),
        }
        for sku_atual, info in estoque_por_sku.items()
        if len(info.get("anuncios", [])) > 1
    ]

    return {
        "itens": itens,
        "resumo": {
            "total_skus": len(todos_skus),
            "estoque_total": estoque_total,
            "valor_total_estoque": round(valor_total, 2),
            "sem_custo": sem_custo,
            "sem_estoque": sem_estoque,
        },
        "estoque_indisponivel": estoque_indisponivel,
        "aviso_estoque": aviso_estoque,
        "skus_em_multiplos_anuncios": skus_em_multiplos_anuncios,
    }
