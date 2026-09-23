"""
Ferramentas de administração da leitura de SKU (só admin e supervisor).

  GET /api/admin/diagnostico-sku/{item_id}
      Mostra o que o Mercado Livre devolve daquele anúncio e ONDE o SKU
      foi encontrado (ou por que não foi). Serve pra conferir com
      anúncios reais antes de confiar na leitura.

  GET /api/admin/preencher-skus?limite=100
      Lê o SKU das perguntas antigas que ainda não têm essa informação
      (em lotes, pra não pesar no ML nem estourar o tempo da requisição).
      Aproveita e coloca no banco de respostas as respostas de atendente
      que ficaram de fora só porque a pergunta estava sem SKU. Pode ser
      chamado várias vezes até "restantes" chegar a 0 -- é seguro repetir.
"""
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app import auth
from app.database import get_db
from app.ml_client import MLApiError, MLAuthError, _extrair_skus, _get, garantir_token_valido, ler_dados_do_anuncio
from app.models import Conta, Pergunta, RespostaValidadaSku
from app.pre_venda_logica import assunto_exige_humano, chave_do_produto

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin-sku"])

PAUSA_ENTRE_CONSULTAS_SEG = 0.2  # gentileza com a API do ML no preenchimento em lote


def _exigir_admin(request: Request, db: Session):
    usuario = auth.usuario_atual(request, db)
    if not auth.papel_permite(usuario, ("admin", "supervisor")):
        raise HTTPException(status_code=403, detail="Só admin ou supervisor.")
    return usuario


def _token_de(conta: Conta, db: Session, cache: dict) -> str | None:
    """Token válido da conta (renova se precisar); None se a conta estiver sem acesso."""
    if conta.id in cache:
        return cache[conta.id]
    try:
        cache[conta.id] = garantir_token_valido(conta, db)
    except (MLAuthError, MLApiError) as exc:
        logger.warning("Conta %s sem token válido: %s", conta.id, exc)
        cache[conta.id] = None
    return cache[conta.id]


@router.get("/diagnostico-sku/{item_id}")
def diagnostico_sku(item_id: str, request: Request, db: Session = Depends(get_db)):
    _exigir_admin(request, db)
    item_id = item_id.strip().upper().replace("-", "")

    # Usa a conta dona de uma pergunta desse anúncio; se não houver, a
    # primeira conta conectada que funcionar (anúncio é público no ML).
    pergunta = db.query(Pergunta).filter(Pergunta.item_id == item_id).first()
    candidatas = []
    if pergunta:
        dona = db.query(Conta).filter(Conta.id == pergunta.conta_id).first()
        if dona:
            candidatas.append(dona)
    candidatas += db.query(Conta).filter(Conta.access_token.isnot(None)).all()

    tokens: dict = {}
    for conta in candidatas:
        token = _token_de(conta, db, tokens)
        if not token:
            continue
        try:
            item = _get(f"/items/{item_id}?include_attributes=all", token, "buscar o anúncio")
        except MLApiError as exc:
            return {"item_id": item_id, "conta_usada": conta.apelido, "erro": str(exc)[:500]}

        skus, origem = _extrair_skus(item)
        variacoes = item.get("variations") or []
        return {
            "item_id": item_id,
            "titulo": item.get("title"),
            "conta_usada": conta.apelido,
            "sku_principal": skus[0] if skus else None,
            "todos_os_skus": skus,
            "onde_foi_encontrado": origem,
            "explicacao": (
                "SKU encontrado." if skus else
                "Nenhum SKU cadastrado neste anúncio (nem no anúncio, nem nas variações). "
                "O sistema vai usar o código do anúncio (MLB) como chave do banco de respostas."
            ),
            "detalhe": {
                "atributo_SELLER_SKU": next((a.get("value_name") for a in item.get("attributes") or [] if a.get("id") == "SELLER_SKU"), None),
                "seller_custom_field": item.get("seller_custom_field"),
                "quantidade_de_variacoes": len(variacoes),
                "variacoes": [
                    {
                        "id": v.get("id"),
                        "combinacao": ", ".join(f"{c.get('name')}: {c.get('value_name')}" for c in v.get("attribute_combinations") or []),
                        "SELLER_SKU": next((a.get("value_name") for a in v.get("attributes") or [] if a.get("id") == "SELLER_SKU"), None),
                        "seller_custom_field": v.get("seller_custom_field"),
                    }
                    for v in variacoes[:30]
                ],
            },
        }
    raise HTTPException(status_code=503, detail="Nenhuma conta conectada com acesso válido ao Mercado Livre.")


@router.get("/preencher-skus")
def preencher_skus(request: Request, limite: int = 100, db: Session = Depends(get_db)):
    _exigir_admin(request, db)
    limite = min(max(limite, 1), 500)

    pendentes = (
        db.query(Pergunta)
        .filter(Pergunta.skus_anuncio.is_(None), Pergunta.item_id.isnot(None))
        .order_by(Pergunta.recebida_em.desc())
        .limit(limite)
        .all()
    )

    contas = {c.id: c for c in db.query(Conta).all()}
    tokens: dict = {}
    lidos: dict[str, dict] = {}
    com_sku = sem_sku = falhas = removidos = adicionadas_ao_banco = 0

    for pergunta in pendentes:
        conta = contas.get(pergunta.conta_id)
        token = _token_de(conta, db, tokens) if conta else None
        if not token:
            falhas += 1
            continue

        if pergunta.item_id not in lidos:
            lidos[pergunta.item_id] = ler_dados_do_anuncio(token, pergunta.item_id)
            time.sleep(PAUSA_ENTRE_CONSULTAS_SEG)
        dados = lidos[pergunta.item_id]
        if dados["erro"]:
            if dados.get("nao_existe"):
                pergunta.skus_anuncio = ""  # anúncio excluído: marca como lido, sem SKU
                removidos += 1
            else:
                falhas += 1  # erro passageiro: fica nulo e tenta de novo na próxima execução
            continue

        pergunta.skus_anuncio = ",".join(dados["skus"])
        if not pergunta.sku and dados["sku"]:
            pergunta.sku = dados["sku"]
        if dados["sku"]:
            com_sku += 1
        else:
            sem_sku += 1

        # Resposta de atendente que ficou fora do banco só por falta de SKU.
        if pergunta.camada_resolvida == "manual" and pergunta.resposta_enviada and not assunto_exige_humano(pergunta.texto):
            chave = chave_do_produto(pergunta.sku, pergunta.item_id)
            ja_existe = (
                db.query(RespostaValidadaSku)
                .filter(
                    RespostaValidadaSku.sku == chave,
                    RespostaValidadaSku.pergunta_exemplo == pergunta.texto,
                    RespostaValidadaSku.resposta == pergunta.resposta_enviada,
                )
                .first()
            )
            if chave and not ja_existe:
                db.add(RespostaValidadaSku(sku=chave, pergunta_exemplo=pergunta.texto, resposta=pergunta.resposta_enviada))
                adicionadas_ao_banco += 1

    db.commit()
    restantes = (
        db.query(Pergunta)
        .filter(Pergunta.skus_anuncio.is_(None), Pergunta.item_id.isnot(None))
        .count()
    )
    return {
        "processadas": len(pendentes),
        "com_sku": com_sku,
        "anuncio_sem_sku": sem_sku,
        "anuncio_excluido_no_ml": removidos,
        "falhas": falhas,
        "respostas_adicionadas_ao_banco": adicionadas_ao_banco,
        "restantes": restantes,
        "dica": "Se 'restantes' for maior que 0, abra este endereço de novo." if restantes else "Concluído.",
    }
