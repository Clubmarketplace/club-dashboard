"""
Rotas de autorização OAuth com o Mercado Livre.

Fluxo:
1. GET /auth/conectar/{apelido} -> gera a URL de autorização e
   redireciona o dono da conta pra lá.
2. O Mercado Livre redireciona de volta pra GET /auth/ml/callback com
   um 'code' (e o 'state', que devolvemos do jeito que enviamos — usamos
   isso pra saber qual apelido estava sendo conectado).
3. Trocamos o code por access_token/refresh_token e salvamos/atualizamos
   a linha correspondente na tabela `contas`.
"""
from datetime import datetime
import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy.orm import Session

from app import auth
from app.database import get_db
from app.models import Conta, EmpresaPlanejada, Devolucao, AcaoRegistrada
from app.ml_client import (
    MLAuthError,
    montar_url_autorizacao,
    trocar_codigo_por_token,
    calcular_expiracao,
)

router = APIRouter(prefix="/auth", tags=["autorização"])
logger = logging.getLogger("auth_ml")

# Tempo padrão de expiração do access_token do Mercado Livre, usado só
# como fallback caso a resposta não traga 'expires_in' (não deveria
# acontecer, mas evita quebrar o fluxo por causa de um campo ausente).
EXPIRACAO_PADRAO_SEGUNDOS = 21600  # 6 horas


@router.get("/contas")
def listar_contas_conectadas(db: Session = Depends(get_db)):
    """
    Lista todas as contas cadastradas (conectadas ou não), pra tela
    'Contas conectadas' do painel. Diferente de /api/devolucoes/contas
    (que só lista as ativas, pro seletor de filtro), essa aqui mostra
    o status de conexão de cada uma.
    """
    contas = db.query(Conta).order_by(Conta.apelido).all()
    agora = datetime.utcnow()
    return [
        {
            "id": c.id,
            "apelido": c.apelido,
            "ml_user_id": c.ml_user_id,
            "ativa": c.ativa,
            "conectada": bool(c.access_token),
            "token_expirado": bool(c.token_expira_em and c.token_expira_em < agora),
            "conectada_em": c.conectada_em.isoformat() if c.conectada_em else None,
            "token_expira_em": c.token_expira_em.isoformat() if c.token_expira_em else None,
            "inativa": c.inativa_em is not None,
            "inativa_em": c.inativa_em.isoformat() if c.inativa_em else None,
            "motivo_inativacao": c.motivo_inativacao,
        }
        for c in contas
    ]


@router.get("/progresso")
def progresso_autorizacao(db: Session = Depends(get_db)):
    """
    Cruza a lista de empresas planejadas (dados/empresas.txt, importada
    via app/importar_empresas.py) com as contas já conectadas, pra
    tela acompanhar quais faltam autorizar. Cruza por nome, ignorando
    maiúsculas/minúsculas.
    """
    empresas = db.query(EmpresaPlanejada).order_by(EmpresaPlanejada.nome).all()
    contas_por_nome = {c.apelido.strip().lower(): c for c in db.query(Conta).all()}

    resultado = []
    for empresa in empresas:
        conta = contas_por_nome.get(empresa.nome.strip().lower())
        resultado.append({
            "nome": empresa.nome,
            "autorizada": bool(conta and conta.access_token),
            "conectada_em": conta.conectada_em.isoformat() if conta and conta.conectada_em else None,
        })

    total = len(resultado)
    autorizadas = sum(1 for r in resultado if r["autorizada"])
    return {"total": total, "autorizadas": autorizadas, "empresas": resultado}


@router.get("/link/{apelido}")
def gerar_link_autorizacao(apelido: str):
    """
    Devolve o link de autorização pronto (sem redirecionar) -- pra
    copiar e mandar pra quem for autorizar a conta (ex: por WhatsApp
    ou e-mail), sem precisar que o link seja aberto no SEU navegador.
    Quem abrir esse link vai direto pro login do Mercado Livre; depois
    de autorizar, o próprio Mercado Livre chama nosso /auth/ml/callback
    (que precisa estar acessível publicamente -- ngrok ou domínio real
    -- nesse momento, mesmo que o clique inicial já tenha acontecido
    antes).
    """
    try:
        url = montar_url_autorizacao(state=apelido)
    except MLAuthError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"apelido": apelido, "link": url}


@router.get("/conectar/{apelido}")
def conectar_conta(apelido: str):
    """
    Gera o link de autorização do Mercado Livre pra essa conta e
    redireciona o navegador pra lá. O 'apelido' vai no 'state' pra
    identificarmos a conta quando o Mercado Livre chamar o callback.
    """
    try:
        url = montar_url_autorizacao(state=apelido)
    except MLAuthError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return RedirectResponse(url)


def _pagina_resultado(titulo: str, mensagem: str, sucesso: bool) -> HTMLResponse:
    """Tela simples de resultado -- quem autoriza é a empresa parceira, não deveria ver um JSON cru."""
    cor = "#2f6f4f" if sucesso else "#c1502e"
    html = f"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="UTF-8">
<title>{titulo}</title>
<style>
  body {{ font-family: -apple-system, 'Inter', sans-serif; background: #f6f7f9; display: flex;
         align-items: center; justify-content: center; height: 100vh; margin: 0; }}
  .cartao {{ background: #fff; border-radius: 14px; padding: 40px 48px; box-shadow: 0 8px 24px -12px rgba(0,0,0,0.15);
             text-align: center; max-width: 420px; }}
  h1 {{ color: {cor}; font-size: 22px; margin-bottom: 12px; }}
  p {{ color: #444; font-size: 15px; line-height: 1.5; }}
</style></head>
<body><div class="cartao"><h1>{titulo}</h1><p>{mensagem}</p></div></body></html>"""
    return HTMLResponse(content=html)


@router.get("/ml/callback")
def callback(
    code: str = Query(..., description="Código de autorização devolvido pelo Mercado Livre"),
    state: str = Query(..., description="Apelido da conta que estava sendo conectada"),
    db: Session = Depends(get_db),
):
    """
    Recebe o retorno do Mercado Livre após o dono da conta autorizar,
    troca o code por tokens, e salva/atualiza a conta no banco.
    """
    try:
        dados_token = trocar_codigo_por_token(code)
    except MLAuthError as exc:
        logger.error("Falha ao trocar código por token (apelido=%s): %s", state, exc)
        return _pagina_resultado(
            "Não foi possível concluir",
            "Houve um problema ao confirmar a autorização com o Mercado Livre. "
            "Peça pra quem enviou esse link tentar de novo.",
            sucesso=False,
        )

    ml_user_id = str(dados_token.get("user_id", ""))
    access_token = dados_token.get("access_token")
    refresh_token = dados_token.get("refresh_token")
    expires_in = dados_token.get("expires_in", EXPIRACAO_PADRAO_SEGUNDOS)

    if not ml_user_id or not access_token:
        return _pagina_resultado(
            "Não foi possível concluir",
            "O Mercado Livre não devolveu os dados esperados. "
            "Peça pra quem enviou esse link tentar de novo.",
            sucesso=False,
        )

    conta = db.query(Conta).filter(Conta.ml_user_id == ml_user_id).first()

    if conta is not None and conta.apelido != state:
        # A conta do Mercado Livre que acabou de autorizar já está salva
        # aqui sob OUTRO nome -- sinal quase certo de que quem abriu esse
        # link estava logado no Mercado Livre com a conta errada (ex:
        # tentou autorizar "JC Machado" mas o navegador ainda estava
        # logado como "Velasco"). Em vez de renomear silenciosamente,
        # para aqui e avisa -- essa é a causa mais comum desse erro.
        return _pagina_resultado(
            "Confira antes de continuar ⚠️",
            f'Essa conta do Mercado Livre que acabou de autorizar já está '
            f'conectada aqui como "{conta.apelido}". Pra autorizar '
            f'"{state}", é preciso logar no Mercado Livre com a conta '
            f'vendedora de "{state}" -- não a mesma sessão de "{conta.apelido}". '
            f"Tente numa aba anônima, ou depois de sair da conta atual no "
            f"próprio site do Mercado Livre.",
            sucesso=False,
        )

    if conta is None:
        # Trava de segurança: se já existe uma conta com esse MESMO apelido
        # mas ligada a um usuário DIFERENTE do Mercado Livre, é sinal de que
        # a pessoa errada pode ter aberto esse link (ex: o link do "Cliente A"
        # foi aberto por alguém logado como "Cliente B"). Em vez de criar uma
        # segunda linha ambígua com o mesmo nome, para aqui e avisa.
        conflito = (
            db.query(Conta)
            .filter(Conta.apelido == state, Conta.ml_user_id != ml_user_id)
            .first()
        )
        if conflito:
            return _pagina_resultado(
                "Confira antes de continuar ⚠️",
                f'Já existe uma conta chamada "{state}" conectada com um usuário '
                f"diferente do Mercado Livre. Essa nova autorização NÃO foi salva "
                f"pra evitar confusão -- avise quem te enviou esse link antes de "
                f"tentar de novo.",
                sucesso=False,
            )
        conta = Conta(ml_user_id=ml_user_id, apelido=state)
        db.add(conta)

    conta.apelido = state or conta.apelido
    conta.access_token = access_token
    conta.refresh_token = refresh_token
    conta.token_expira_em = calcular_expiracao(expires_in)
    conta.conectada_em = datetime.utcnow()
    conta.ativa = True
    if conta.inativa_em is not None:
        # Seller que tinha saído do Club autorizou de novo: volta a ser ativa.
        logger.info("Conta %s reativada automaticamente ao reconectar", conta.apelido)
        conta.inativa_em = None
        conta.motivo_inativacao = None

    db.commit()
    db.refresh(conta)

    return _pagina_resultado(
        "Conta conectada com sucesso! ✅",
        f'A conta "{conta.apelido}" foi autorizada. Pode fechar esta janela -- '
        f"o Club Marketplace já está liberado pra responder pré-vendas e "
        f"pós-vendas dela.",
        sucesso=True,
    )


@router.post("/desconectar/{conta_id}")
def desconectar_conta(conta_id: int, db: Session = Depends(get_db)):
    """
    Remove os tokens salvos de uma conta e marca como inativa — útil
    pra encerrar uma conexão de teste antes de reautorizar com o dono
    real, sem deixar token velho guardado por engano. A linha da conta
    continua existindo (histórico de apelido/ml_user_id preservado);
    só os dados de sessão são limpos.
    """
    conta = db.query(Conta).filter(Conta.id == conta_id).first()
    if conta is None:
        raise HTTPException(status_code=404, detail="Conta não encontrada")

    conta.access_token = None
    conta.refresh_token = None
    conta.token_expira_em = None
    conta.ativa = False
    db.commit()

    return {"status": "desconectada", "apelido": conta.apelido}


@router.delete("/contas/{conta_id}")
def excluir_conta(conta_id: int, db: Session = Depends(get_db)):
    """
    Apaga uma conta cadastrada por completo -- id, apelido, tokens,
    tudo. Diferente de "Desconectar" (que só limpa o token e mantém a
    linha, preservando o histórico de devoluções/ações dela), isso
    remove o registro inteiro. Serve pra casos como uma conta
    duplicada ou criada com o ml_user_id errado por engano, que não
    deveria continuar aparecendo na lista.

    Por segurança, só permite excluir contas que nunca tiveram
    devolução ou ação registrada -- se já existe histórico real
    vinculado a essa conta, pede pra usar "Desconectar" em vez de
    excluir, pra não apagar dado nenhum sem querer.
    """
    conta = db.query(Conta).filter(Conta.id == conta_id).first()
    if conta is None:
        raise HTTPException(status_code=404, detail="Conta não encontrada")

    tem_devolucoes = db.query(Devolucao).filter(Devolucao.conta_id == conta_id).first() is not None
    tem_acoes = db.query(AcaoRegistrada).filter(AcaoRegistrada.conta_id == conta_id).first() is not None
    if tem_devolucoes or tem_acoes:
        raise HTTPException(
            status_code=400,
            detail=(
                f'A conta "{conta.apelido}" já tem histórico registrado (devoluções e/ou ações) -- '
                f'excluir apagaria esses dados. Use "Desconectar" em vez disso: limpa o token, mas '
                f'preserva o histórico.'
            ),
        )

    apelido = conta.apelido
    # A leitura de reputação é só uma "foto" do termômetro (não é histórico).
    from app.models import ReputacaoConta
    db.query(ReputacaoConta).filter(ReputacaoConta.conta_id == conta_id).delete(synchronize_session=False)
    db.delete(conta)
    db.commit()

    return {"status": "excluida", "apelido": apelido}


# ---------------------------------------------------------------------------
# Inativar / reativar conta (seller que saiu do Club) -- SÓ ADMIN
# ---------------------------------------------------------------------------
# Inativar NÃO apaga nada: a conta some das listas e filtros, o token é
# limpo (não faz sentido continuar acessando o ML de quem saiu) e o
# histórico continua nos relatórios. Reativar devolve a conta às listas;
# se ela estiver sem token, basta mandar o link de autorização de sempre
# (o próprio callback também reativa ao reconectar).

def _exigir_admin(request: Request, db: Session):
    usuario = auth.usuario_atual(request, db)
    if not auth.papel_permite(usuario, ("admin",)):
        raise HTTPException(status_code=403, detail="Só o admin pode inativar ou reativar contas.")
    return usuario


def _conta_ou_404(conta_id: int, db: Session) -> Conta:
    conta = db.query(Conta).filter(Conta.id == conta_id).first()
    if conta is None:
        raise HTTPException(status_code=404, detail="Conta não encontrada")
    return conta


@router.post("/contas/{conta_id}/inativar")
def inativar_conta(
    conta_id: int,
    request: Request,
    dados: dict = Body(default={}),
    db: Session = Depends(get_db),
):
    usuario = _exigir_admin(request, db)
    conta = _conta_ou_404(conta_id, db)
    if conta.inativa_em is not None:
        return {"status": "ja_inativa", "apelido": conta.apelido}

    motivo = str((dados or {}).get("motivo") or "").strip()[:200] or "Saiu do Club"
    conta.inativa_em = datetime.utcnow()
    conta.motivo_inativacao = motivo
    conta.access_token = None
    conta.refresh_token = None
    conta.token_expira_em = None
    conta.ativa = False
    db.commit()
    logger.warning("Conta %s (ml %s) inativada por %s: %s",
                   conta.apelido, conta.ml_user_id, getattr(usuario, "nome_exibicao", "?"), motivo)
    return {"status": "inativada", "apelido": conta.apelido}


@router.post("/contas/{conta_id}/reativar")
def reativar_conta(conta_id: int, request: Request, db: Session = Depends(get_db)):
    usuario = _exigir_admin(request, db)
    conta = _conta_ou_404(conta_id, db)
    conta.inativa_em = None
    conta.motivo_inativacao = None
    db.commit()
    logger.warning("Conta %s (ml %s) reativada por %s",
                   conta.apelido, conta.ml_user_id, getattr(usuario, "nome_exibicao", "?"))
    return {"status": "reativada", "apelido": conta.apelido, "precisa_reconectar": not conta.access_token}
