"""
Tela "Produtos > Lista" do painel do seller: junta o custo (guardado no
nosso banco, tabela CustoSku) com o estoque de cada SKU -- lido do
Mercado Livre, mas agora passando por um espelho em banco (EstoqueSku)
em vez de consultar o Mercado Livre ao vivo a cada abertura da tela.

CORREÇÃO (02/10): antes, cada abertura dessa tela disparava uma
varredura completa no Mercado Livre (uma chamada por anúncio ativo --
lento com muitos anúncios, e travava a tela se o Mercado Livre
demorasse ou estivesse fora do ar). Agora:
  - A tela sempre LÊ da tabela EstoqueSku (rápido, sem depender do
    Mercado Livre pra carregar).
  - Na primeira vez que uma conta usa a tela (tabela ainda vazia pra
    ela), a varredura roda na hora mesmo (só essa vez é mais lenta).
  - Em toda abertura seguinte, depois de responder com o dado já salvo,
    dispara a varredura de novo EM SEGUNDO PLANO -- a próxima abertura
    já vem com o dado atualizado, sem ninguém esperar.
  - O botão "Atualizar agora" (POST /lista/atualizar-estoque) força a
    varredura na hora e espera terminar, pra quem não quiser esperar a
    próxima abertura.

Também corrigido nessa mesma data, em ml_client.listar_estoque_por_sku:
a quantidade não é mais SOMADA entre anúncios diferentes que usam o
mesmo SKU (anúncios clonados do mesmo produto compartilham o mesmo
estoque físico no Mercado Livre -- vender em um desconta em todos).

Reaproveita:
  - `_conta_vinculada_do_usuario` / `_seller_logado`, iguais ao resto do
    painel (cmx.py / custos_painel.py), pra nunca misturar dado de uma
    conta com outra.
  - `ml_client.sincronizar_estoque_sku`, que faz a varredura e grava em
    EstoqueSku.
"""
import logging
import re

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import Session

from app import ml_client
from app.database import get_db
from app.models import Conta, CustoSku, EstoqueSku
from app.ml_client import MLAuthError, MLApiError
from app.routers.custos_painel import _seller_logado
from app.routers.cmx import _conta_vinculada_do_usuario

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/painel/produtos", tags=["painel-produtos"])

# SKU de kit/combo termina em "-KITn" (ex: 7894855232913-KIT2,
# 51016410-KIT10) ou junta dois SKUs-base com hífen, formando o SKU de
# um "conjunto" (ex: "7894855232968-7894855232913" = mesa + cadeiras).
_KIT_SUFIXO_RE = re.compile(r"-KIT\d*$", re.IGNORECASE)
_COMBO_SKU_RE = re.compile(r"^\d{6,}-\d{6,}$")


def _eh_kit_ou_combo(sku: str) -> bool:
    """
    Kit/combo é um anúncio à parte no Mercado Livre (tem seu próprio
    SKU e sua própria "quantidade" vinda de lá), mas vende o MESMO
    estoque físico dos produtos-base que o compõem -- ex: 6 cadeiras +
    6 mesas cadastradas separadamente, e um "Kit 2 Cadeiras" montado em
    cima das mesmas 6 cadeiras. Somar a quantidade/valor do kit ao dos
    produtos-base no resumo geral conta a mesma peça física duas vezes.

    Por isso esses SKUs continuam aparecendo na lista (pra poder editar
    custo deles normalmente), mas ficam de fora dos cards de total.
    """
    sku_upper = sku.strip().upper()
    if _KIT_SUFIXO_RE.search(sku_upper):
        return True
    if _COMBO_SKU_RE.match(sku_upper):
        return True
    return False


def _sincronizar_estoque_em_segundo_plano(conta_id: int) -> None:
    """
    Mesmo padrão de app/cancelamento_apoio.py:preencher_produtos_em_segundo_plano
    -- roda DEPOIS da resposta já ter sido enviada, com sua própria sessão
    de banco (a da requisição original já fechou). Qualquer falha aqui só
    é registrada no log: ninguém está esperando essa chamada, a tela já
    mostrou o último dado bom conhecido.
    """
    from app.database import SessionLocal

    try:
        with SessionLocal() as db:
            conta = db.query(Conta).filter(Conta.id == conta_id).first()
            if conta is None:
                return
            ml_client.sincronizar_estoque_sku(conta, db)
    except (MLAuthError, MLApiError) as exc:
        logger.warning("Sincronização de estoque em segundo plano falhou pra conta %s: %s", conta_id, exc)
    except Exception:
        logger.exception("Erro inesperado sincronizando estoque em segundo plano (conta %s)", conta_id)


def _montar_resposta(conta: Conta, db: Session, sku: str) -> dict:
    """
    Monta a resposta de /lista a partir do que já está salvo (CustoSku +
    EstoqueSku) -- não faz nenhuma chamada ao Mercado Livre aqui; quem
    chama decide separadamente se precisa sincronizar antes ou depois.
    """
    custos = db.query(CustoSku).filter(CustoSku.conta_id == conta.id).all()
    custos_por_sku = {c.sku: c for c in custos}

    estoque_rows = db.query(EstoqueSku).filter(EstoqueSku.conta_id == conta.id).all()
    estoque_por_sku = {e.sku: e for e in estoque_rows}

    # União dos SKUs que têm custo cadastrado com os que têm anúncio no
    # ML (pode ter anúncio sem custo cadastrado ainda, ou custo
    # cadastrado de um produto que não tem mais anúncio ativo).
    todos_skus = set(custos_por_sku.keys()) | set(estoque_por_sku.keys())

    itens = []
    estoque_total = 0
    valor_total = 0.0
    sem_custo = 0
    sem_estoque = 0
    atualizado_em = None

    for sku_atual in sorted(todos_skus):
        custo_registro = custos_por_sku.get(sku_atual)
        estoque_registro = estoque_por_sku.get(sku_atual)

        custo = custo_registro.custo if custo_registro else None
        # Sem nenhum registro de estoque ainda pra esse SKU (ex: custo
        # cadastrado manualmente antes de existir anúncio) conta como 0,
        # igual a um SKU com anúncio zerado.
        quantidade = estoque_registro.quantidade if estoque_registro else 0
        nome_produto = (custo_registro.nome_produto if custo_registro else None) or (
            estoque_registro.titulo if estoque_registro else None
        )

        if estoque_registro and (atualizado_em is None or estoque_registro.atualizado_em > atualizado_em):
            atualizado_em = estoque_registro.atualizado_em

        valor_item = (custo * quantidade) if (custo is not None and quantidade is not None) else None
        eh_kit = _eh_kit_ou_combo(sku_atual)

        if custo is None:
            sem_custo += 1
        if quantidade == 0:
            sem_estoque += 1

        # Kit/combo fica de fora da soma dos cards (ver _eh_kit_ou_combo)
        # -- senão a mesma peça física entra duas vezes no total.
        if not eh_kit:
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
                "eh_kit": eh_kit,
            }
        )

    if sku.strip():
        termo = sku.strip().lower()
        itens = [i for i in itens if termo in i["sku"].lower()]

    return {
        "itens": itens,
        "resumo": {
            "total_skus": len(todos_skus),
            "estoque_total": estoque_total,
            "valor_total_estoque": round(valor_total, 2),
            "sem_custo": sem_custo,
            "sem_estoque": sem_estoque,
        },
        "estoque_indisponivel": not estoque_rows,
        "aviso_estoque": None,
        "estoque_atualizado_em": atualizado_em.isoformat() if atualizado_em else None,
    }


@router.get("/lista")
def listar_produtos(
    background_tasks: BackgroundTasks,
    sku: str = "",
    usuario=Depends(_seller_logado),
    db: Session = Depends(get_db),
):
    """
    Devolve, para a conta do seller logado:
      - itens: lista de {sku, nome_produto, custo, quantidade, valor_total}
      - resumo: totais pra exibir nos cards (estoque total, valor total,
        quantidade de SKUs sem custo, sem estoque)
      - estoque_indisponivel: true só na primeira abertura da conta, se a
        varredura inicial falhar (conta sem token, ou erro de rede) --
        depois disso, a tela sempre tem pelo menos o último dado bom
        conhecido, mesmo que o Mercado Livre esteja fora do ar agora.
      - estoque_atualizado_em: quando o estoque foi sincronizado pela
        última vez (pra mostrar "atualizado há X min" na tela).

    `sku`: filtro opcional (contém), igual à busca que já existe em
    "Custos" -- aqui filtra a lista já combinada com o estoque.
    """
    conta = _conta_vinculada_do_usuario(usuario, db)

    tem_estoque_salvo = db.query(EstoqueSku).filter(EstoqueSku.conta_id == conta.id).first() is not None

    if not tem_estoque_salvo:
        # Primeira vez que essa conta abre a tela: ainda não tem nada em
        # cache, então não tem o que mostrar sem varrer agora mesmo (só
        # essa vez é lenta -- as próximas já vêm do banco).
        try:
            ml_client.sincronizar_estoque_sku(conta, db)
        except MLAuthError as exc:
            resposta = _montar_resposta(conta, db, sku)
            resposta["aviso_estoque"] = str(exc)
            return resposta
        except MLApiError as exc:
            resposta = _montar_resposta(conta, db, sku)
            resposta["aviso_estoque"] = f"Não consegui consultar o estoque no Mercado Livre agora: {exc}"
            return resposta
    else:
        # Já tem dado salvo: responde na hora com ele e atualiza por
        # trás, sem fazer a pessoa esperar o Mercado Livre responder.
        background_tasks.add_task(_sincronizar_estoque_em_segundo_plano, conta.id)

    return _montar_resposta(conta, db, sku)


@router.post("/lista/atualizar-estoque")
def atualizar_estoque_agora(
    usuario=Depends(_seller_logado),
    db: Session = Depends(get_db),
):
    """
    Botão "Atualizar agora": igual à sincronização em segundo plano, só
    que aqui quem chamou espera terminar antes de receber a resposta já
    atualizada -- pra quem não quiser esperar a próxima abertura da tela.
    """
    conta = _conta_vinculada_do_usuario(usuario, db)

    try:
        ml_client.sincronizar_estoque_sku(conta, db)
    except MLAuthError as exc:
        resposta = _montar_resposta(conta, db, "")
        resposta["aviso_estoque"] = str(exc)
        return resposta
    except MLApiError as exc:
        resposta = _montar_resposta(conta, db, "")
        resposta["aviso_estoque"] = f"Não consegui consultar o estoque no Mercado Livre agora: {exc}"
        return resposta

    return _montar_resposta(conta, db, "")
