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
        if dados.get("titulo") and not pergunta.titulo_anuncio:
            pergunta.titulo_anuncio = dados["titulo"]
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


@router.get("/preencher-sku-cancelamentos")
def preencher_sku_cancelamentos(request: Request, limite: int = 100, db: Session = Depends(get_db)):
    """
    Lê no Mercado Livre o SKU/produto das solicitações de cancelamento
    antigas (as registradas antes dessa informação existir). Em lotes;
    pode abrir várias vezes até "restantes" chegar a 0 -- é seguro repetir.
    Contas não conectadas ficam como "sem_conta" e são tentadas de novo.
    """
    from app.cancelamento_apoio import buscar_produto_da_venda
    from app.models import SolicitacaoCancelamento

    _exigir_admin(request, db)
    limite = min(max(limite, 1), 300)
    pendentes = (
        db.query(SolicitacaoCancelamento)
        .filter(SolicitacaoCancelamento.plataforma == "mercado_livre", SolicitacaoCancelamento.sku.is_(None))
        .order_by(SolicitacaoCancelamento.criado_em.desc())
        .limit(limite)
        .all()
    )
    contagem = {"ok": 0, "sem_conta": 0, "falha": 0}
    for solicitacao in pendentes:
        resultado = buscar_produto_da_venda(solicitacao, db)
        contagem[resultado] = contagem.get(resultado, 0) + 1
        if resultado != "ok":
            # Marca como "tentado" ("") pra não travar as próximas. A verificação
            # automática ainda preenche depois, se conseguir ler o pedido.
            solicitacao.sku = ""
        db.commit()
        time.sleep(PAUSA_ENTRE_CONSULTAS_SEG)
    restantes = (
        db.query(SolicitacaoCancelamento)
        .filter(SolicitacaoCancelamento.plataforma == "mercado_livre", SolicitacaoCancelamento.sku.is_(None))
        .count()
    )
    return {
        "processadas": len(pendentes),
        "sku_lido": contagem["ok"],
        "conta_nao_conectada": contagem["sem_conta"],
        "falhas": contagem["falha"],
        "restantes": restantes,
        "dica": ("Concluído." if restantes == 0 else
                 "Abra de novo para continuar. Se 'restantes' não diminuir, são contas não conectadas ou vendas que o ML não encontrou."),
    }


# ---------------------------------------------------------------------------
# Diagnóstico da REPUTAÇÃO de uma conta (só consulta, não grava nada)
# ---------------------------------------------------------------------------
# Serve pra conferir, com uma conta real, se o que a API do Mercado Livre
# devolve em /users/{id} (seller_reputation) bate com o painel "Reputação"
# que o seller vê -- ANTES de construir a tela do termômetro em cima disso.
#   GET /api/admin/diagnostico-reputacao?conta=Velasco

def _porcentagem(valor):
    """A API manda as taxas como fração (0.0088 = 0,88%). Mostra do jeito do painel."""
    try:
        return f"{float(valor) * 100:.2f}%".replace(".", ",")
    except (TypeError, ValueError):
        return None


@router.get("/diagnostico-reputacao")
def diagnostico_reputacao(request: Request, conta: str, db: Session = Depends(get_db)):
    from app.contas_util import achar_conta_por_nome

    _exigir_admin(request, db)
    encontrada = achar_conta_por_nome(db, conta)
    if encontrada is None:
        raise HTTPException(status_code=404, detail=f'Não achei a conta "{conta}" em Contas conectadas.')

    token = _token_de(encontrada, db, {})
    if not token:
        raise HTTPException(status_code=502, detail=f"A conta {encontrada.apelido} está sem acesso válido ao Mercado Livre (reconecte a conta).")
    try:
        usuario = _get(f"/users/{encontrada.ml_user_id}", token, "buscar a reputação")
    except MLApiError as exc:
        return {"conta": encontrada.apelido, "erro": str(exc)[:500],
                "dica": "Se aparecer 403/PolicyAgent, falta permissão no aplicativo (DevCenter) pra ler dados do usuário."}

    rep = usuario.get("seller_reputation") or {}
    metricas = rep.get("metrics") or {}
    transacoes = rep.get("transactions") or {}

    def indicador(chave):
        m = metricas.get(chave) or {}
        return {
            "taxa_api": m.get("rate"),
            "taxa_como_no_painel": _porcentagem(m.get("rate")),
            "quantidade": m.get("value"),
            "periodo": m.get("period"),
        }

    return {
        "conta": encontrada.apelido,
        "ml_user_id": encontrada.ml_user_id,
        "apelido_no_ml": usuario.get("nickname"),
        "resumo_pra_comparar_com_o_painel": {
            "medalha (power_seller_status)": rep.get("power_seller_status"),  # silver/gold/platinum ou vazio
            "termometro (level_id)": rep.get("level_id"),                       # ex.: 5_green
            "vendas_no_periodo": (metricas.get("sales") or {}).get("completed"),
            "periodo_das_vendas": (metricas.get("sales") or {}).get("period"),
            "reclamacoes": indicador("claims"),
            "canceladas_por_voce": indicador("cancellations"),
            "envios_com_atraso": indicador("delayed_handling_time"),
            "mediacoes": indicador("mediations"),
        },
        "transacoes": {
            "total": transacoes.get("total"),
            "concluidas": transacoes.get("completed"),
            "canceladas": transacoes.get("canceled"),
            "periodo": transacoes.get("period"),
        },
        # O bloco inteiro como veio do ML, pra conferir campo a campo se algo não bater.
        "seller_reputation_original": rep,
    }


# ---------------------------------------------------------------------------
# Excluir conta de teste / conta que saiu do Club (SÓ ADMIN)
# ---------------------------------------------------------------------------
# A exclusão normal (DELETE /contas/{id}) recusa contas que têm histórico,
# de propósito. Esta ferramenta existe para os casos em que o histórico
# também deve sumir (conta de teste, ou seller que saiu do Club).
#
# Segurança:
#   - só papel "admin" (supervisor NÃO);
#   - localiza a conta SOMENTE pelo ml_user_id exato (nunca pelo nome,
#     porque existem contas com nomes parecidos, ex.: "Velasco"/"VELASCO");
#   - recusa conta que ainda está conectada (com token) -- desconecte antes;
#   - sem "confirmar=SIM" só mostra o que seria apagado (nada é alterado);
#   - tudo numa transação única: se algo falhar, nada é apagado.
# Não mexe em SolicitacaoCancelamento (gravadas pelo nome da conta) nem em
# usuários/operadores.

def _exigir_somente_admin(request: Request, db: Session):
    usuario = auth.usuario_atual(request, db)
    if not auth.papel_permite(usuario, ("admin",)):
        raise HTTPException(status_code=403, detail="Só o admin pode excluir contas.")
    return usuario


@router.get("/excluir-conta-teste")
def excluir_conta_teste(
    request: Request,
    ml_user_id: str,
    confirmar: str = "",
    db: Session = Depends(get_db),
):
    from app.models import AcaoRegistrada, Devolucao, MensagemPosVenda, PedidoCancelamento, ReputacaoConta

    usuario = _exigir_somente_admin(request, db)

    ml_user_id = (ml_user_id or "").strip()
    if not ml_user_id.isdigit():
        raise HTTPException(status_code=400, detail="Informe o ml_user_id numérico exato da conta.")

    conta = db.query(Conta).filter(Conta.ml_user_id == ml_user_id).first()
    if conta is None:
        raise HTTPException(status_code=404, detail=f"Nenhuma conta com ml_user_id {ml_user_id}.")

    if conta.access_token:
        raise HTTPException(
            status_code=409,
            detail=(
                f"A conta '{conta.apelido}' (ml_user_id {ml_user_id}) ainda está conectada. "
                "Por segurança, desconecte-a em Contas conectadas antes de excluir."
            ),
        )

    # Ordem importa: pedidos de cancelamento apontam para mensagens pós-venda.
    tabelas = [
        ("pedidos_cancelamento", PedidoCancelamento),
        ("mensagens_pos_venda", MensagemPosVenda),
        ("perguntas", Pergunta),
        ("acoes_registradas", AcaoRegistrada),
        ("devolucoes", Devolucao),
        ("reputacao_contas", ReputacaoConta),
    ]
    contagem = {nome: db.query(modelo).filter(modelo.conta_id == conta.id).count() for nome, modelo in tabelas}

    info_conta = {"id": conta.id, "apelido": conta.apelido, "ml_user_id": conta.ml_user_id}

    if confirmar != "SIM":
        return {
            "modo": "pré-visualização (nada foi apagado)",
            "conta": info_conta,
            "seria_apagado": contagem,
            "para_confirmar": f"repita a mesma URL acrescentando &confirmar=SIM",
        }

    try:
        apagado = {}
        for nome, modelo in tabelas:
            apagado[nome] = (
                db.query(modelo).filter(modelo.conta_id == conta.id).delete(synchronize_session=False)
            )
        db.delete(conta)
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception("Falha ao excluir conta %s", ml_user_id)
        raise HTTPException(status_code=500, detail=f"Nada foi apagado (erro: {exc}).")

    logger.warning(
        "Conta excluída por %s: %s (ml_user_id %s) -- %s",
        getattr(usuario, "nome_exibicao", "?"), info_conta["apelido"], ml_user_id, apagado,
    )
    return {"modo": "excluída", "conta": info_conta, "apagado": apagado}
