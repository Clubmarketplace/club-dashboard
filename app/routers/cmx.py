"""
Rotas da extensão ClubMarketplaceX (CMX) -- autenticação.

Diferente do login do painel (main.py, cookie de navegador), aqui a
extensão manda usuário/senha em JSON e recebe de volta um TOKEN, que ela
guarda em chrome.storage.local e reenvia em todo request seguinte no
cabeçalho "Authorization: Bearer <token>". É o mesmo mecanismo de
assinatura (auth.criar_token_sessao / auth.ler_usuario_id_da_sessao),
só muda onde o token viaja -- não é cookie, não é OAuth, não é JWT.

Regra central destas rotas: só usuário com papel "seller" E com
conta_vinculada preenchida consegue logar aqui -- é essa vinculação que
diz "qual conta essa pessoa representa", automaticamente, sem o seller
escolher nada. Se o admin desativar o usuário (Usuario.ativo = False)
ou remover a conta vinculada, o próximo /api/cmx/validar já nega, e a
extensão para de funcionar pra essa pessoa.
"""
from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, field_validator
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import auth
from app.contas_util import chave_conta
from app.custos_log import registrar_log_custo
from app.database import get_db
from app.models import Conta, CustoSku, EventoAdesaoCmx, Usuario, VariavelConta

CMX_ML_TOKEN_URL = "https://api.mercadolibre.com/oauth/token"

# Razão (para mais ou para menos) a partir da qual uma mudança de custo é
# marcada como "suspeita" na resposta -- não bloqueia a gravação (quem
# decide é sempre quem está do outro lado, extensão ou painel), só avisa,
# igual ao mesmo alerta que já existe na importação de planilha do popup.js.
RAZAO_CUSTO_SUSPEITO = 5

router = APIRouter(prefix="/api/cmx", tags=["cmx-extensao"])


class LoginCmxEntrada(BaseModel):
    usuario: str
    senha: str


def _buscar_usuario_por_login(db: Session, login: str) -> Usuario | None:
    """
    Mesma regra de busca usada no login do painel (ver
    main.py:_buscar_usuario_por_login): ignora maiúsculas/minúsculas e só
    considera usuários ativos. Mantida separada aqui (em vez de
    importada de main.py) pra evitar import circular -- main.py importa
    os routers, não o contrário.
    """
    digitado = (login or "").strip()
    if not digitado:
        return None
    candidatos = (
        db.query(Usuario)
        .filter(func.lower(Usuario.usuario) == digitado.lower(), Usuario.ativo.is_(True))
        .order_by(Usuario.id)
        .all()
    )
    for candidato in candidatos:
        if candidato.usuario == digitado:
            return candidato
    return candidatos[0] if candidatos else None


def _conta_vinculada_do_usuario(usuario: Usuario, db: Session) -> Conta:
    """
    Resolve a Conta (ml_user_id, apelido) vinculada ao usuário logado.
    Levanta 403/404 nos casos em que a extensão não tem o que fazer:
    usuário sem vínculo de conta, conta que não existe (mais) no
    cadastro, ou conta desativada.
    """
    # "suporte" = perfil interno que usa o painel com a conta de um seller
    # (ver PAPEIS_PAINEL_SELLER em main.py); resolve a conta do mesmo jeito.
    if usuario.papel not in ("seller", "suporte") or not usuario.conta_vinculada:
        raise HTTPException(status_code=403, detail="Este usuário não está vinculado a uma conta de seller.")

    chave_procurada = chave_conta(usuario.conta_vinculada)
    conta = next(
        (c for c in db.query(Conta).all() if chave_conta(c.apelido) == chave_procurada),
        None,
    )
    if conta is None:
        raise HTTPException(status_code=404, detail="A conta vinculada a este usuário não foi encontrada.")
    if not conta.ativa or conta.inativa_em is not None:
        raise HTTPException(status_code=403, detail="Esta conta está inativa no Club Marketplace.")
    return conta


def _serializar_conta(conta: Conta) -> dict:
    return {
        "id": conta.id,
        "apelido": conta.apelido,
        "ml_user_id": conta.ml_user_id,
    }


def _token_do_cabecalho(authorization: str | None = Header(default=None)) -> str:
    """Extrai o token de "Authorization: Bearer <token>". 401 se vier faltando/mal formatado."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Token ausente. Faça login na extensão novamente.")
    return authorization.split(" ", 1)[1].strip()


def usuario_logado_cmx(token: str = Depends(_token_do_cabecalho), db: Session = Depends(get_db)) -> Usuario:
    """
    Dependency usada por toda rota protegida da extensão (login já feito).
    Se o token for inválido, vencido, ou o usuário tiver sido desativado
    nesse meio tempo, cai aqui como 401 -- é o gatilho que faz a extensão
    parar de funcionar quando alguém é removido do cadastro.
    """
    usuario = auth.usuario_por_token(token, db)
    if not usuario:
        raise HTTPException(status_code=401, detail="Sessão inválida ou expirada. Faça login novamente.")
    return usuario


@router.post("/login")
def login_extensao(dados: LoginCmxEntrada, db: Session = Depends(get_db)):
    """
    Login da extensão: usuário + senha -> token + dados da conta vinculada.
    A extensão nunca escolhe/digita qual conta é -- ela já vem pronta
    aqui, resolvida a partir do cadastro do usuário no painel.
    """
    usuario = _buscar_usuario_por_login(db, dados.usuario)
    if usuario is None or not usuario.senha_hash:
        raise HTTPException(status_code=401, detail="Usuário ou senha incorretos.")
    if not auth.verificar_senha(dados.senha, usuario.senha_hash):
        raise HTTPException(status_code=401, detail="Usuário ou senha incorretos.")

    conta = _conta_vinculada_do_usuario(usuario, db)

    token = auth.criar_token_sessao(usuario.id, auth.DURACAO_SESSAO_CMX_SEGUNDOS)
    return {
        "token": token,
        "expira_em_segundos": auth.DURACAO_SESSAO_CMX_SEGUNDOS,
        "usuario": {"nome_exibicao": usuario.nome_exibicao},
        "conta": _serializar_conta(conta),
    }


class CustoSkuEntrada(BaseModel):
    sku: str
    custo: float
    nome_produto: str | None = None

    @field_validator("sku")
    @classmethod
    def _sku_nao_vazio(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("SKU não pode ser vazio.")
        return v

    @field_validator("custo")
    @classmethod
    def _custo_positivo(cls, v: float) -> float:
        # Regra dura, de propósito: custo errado infla/derruba margem de
        # verdade na tela de vendas -- não existe "custo zero ou negativo"
        # válido aqui. Vazio/inválido é rejeitado pelo próprio Pydantic
        # antes disso (campo é float obrigatório).
        if v <= 0:
            raise ValueError("Custo precisa ser maior que zero.")
        return v


class CustosEntrada(BaseModel):
    itens: list[CustoSkuEntrada]


@router.get("/custos")
def listar_custos(usuario: Usuario = Depends(usuario_logado_cmx), db: Session = Depends(get_db)):
    """
    Devolve TODOS os custos cadastrados da conta do usuário logado, numa
    chamada só (nunca uma por SKU) -- é o que a extensão usa pra calcular
    margem na tela de Promoções, e o que a tela "Produtos > Custos" do
    painel vai usar pra listar/gerar planilha.
    """
    conta = _conta_vinculada_do_usuario(usuario, db)
    custos = db.query(CustoSku).filter(CustoSku.conta_id == conta.id).order_by(CustoSku.sku).all()
    return {
        "conta": _serializar_conta(conta),
        "itens": [
            {
                "sku": c.sku,
                "custo": c.custo,
                "nome_produto": c.nome_produto,
                "atualizado_em": c.atualizado_em.isoformat() if c.atualizado_em else None,
                "atualizado_por": c.atualizado_por,
            }
            for c in custos
        ],
    }


@router.post("/custos")
def atualizar_custos(
    dados: CustosEntrada,
    usuario: Usuario = Depends(usuario_logado_cmx),
    db: Session = Depends(get_db),
):
    """
    Grava custos em lote (1 item ou centenas, sempre a mesma chamada --
    nunca uma requisição por SKU, ver conversa sobre desempenho com
    100+ contas usando ao mesmo tempo). Sempre isolado pela conta do
    usuário logado: mesmo que o payload tentasse, não existe campo
    conta_id vindo de fora, então não tem como um seller gravar custo
    em outra conta por engano ou de propósito.

    Cada item devolve o custo antigo x novo e uma marcação "suspeito"
    (variação de 5x ou mais) -- quem chamou (extensão ou painel) decide
    o que fazer com isso; esta rota não bloqueia a gravação sozinha.
    """
    if not dados.itens:
        raise HTTPException(status_code=400, detail="Nenhum item enviado.")

    conta = _conta_vinculada_do_usuario(usuario, db)

    existentes = {
        c.sku: c
        for c in db.query(CustoSku).filter(CustoSku.conta_id == conta.id).all()
    }

    resultado = []
    for item in dados.itens:
        registro = existentes.get(item.sku)
        custo_anterior = registro.custo if registro else None

        suspeito = False
        if custo_anterior and custo_anterior > 0:
            razao = (
                item.custo / custo_anterior
                if item.custo >= custo_anterior
                else custo_anterior / item.custo
            )
            suspeito = razao >= RAZAO_CUSTO_SUSPEITO

        if registro is None:
            registro = CustoSku(conta_id=conta.id, sku=item.sku)
            db.add(registro)
            existentes[item.sku] = registro  # evita duplicar se o SKU repetir no mesmo lote

        registro.custo = item.custo
        if item.nome_produto:
            registro.nome_produto = item.nome_produto
        registro.atualizado_por = usuario.nome_exibicao

        alterado = custo_anterior != item.custo
        if alterado:
            registrar_log_custo(
                db,
                conta_id=conta.id,
                sku=item.sku,
                custo_anterior=custo_anterior,
                custo_novo=item.custo,
                nome_produto=registro.nome_produto,
                alterado_por=usuario.nome_exibicao,
                origem="extensao",
            )

        resultado.append(
            {
                "sku": item.sku,
                "custo_anterior": custo_anterior,
                "custo_novo": item.custo,
                "alterado": alterado,
                "suspeito": suspeito,
            }
        )

    db.commit()
    return {"total_processados": len(resultado), "itens": resultado}


# --- Variáveis (taxas) da conta, ex: "imposto" 2%, "club" 1% -------------
#
# Mesmo padrão de isolamento dos custos: tudo filtrado por conta_id do
# usuário logado, nunca por nome sozinho. Antes ficava só no
# chrome.storage.local de cada instalação da extensão -- agora vive no
# servidor, junto com os custos, pra não perder nada trocando de PC e
# pra dar pro painel também enxergar/editar (tela "Variáveis" futura).


_BASES_CALCULO_VALIDAS = {"venda_bruta", "repasse", "lucro"}


class VariavelEntrada(BaseModel):
    nome: str
    percentual: float
    # "venda_bruta" (default) | "repasse" | "lucro" -- sobre qual valor o
    # percentual é aplicado. Default garante compatibilidade com
    # instalações antigas da extensão que ainda não enviam esse campo.
    base_calculo: str = "venda_bruta"

    @field_validator("nome")
    @classmethod
    def _nome_nao_vazio(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("Nome da variável não pode ser vazio.")
        return v

    @field_validator("percentual")
    @classmethod
    def _percentual_valido(cls, v: float) -> float:
        # Zero é aceitável aqui (diferente do custo) -- uma taxa
        # cadastrada com 0% é só "desativada temporariamente", não é um
        # erro de digitação como custo zero seria.
        if v < 0:
            raise ValueError("Percentual não pode ser negativo.")
        return v

    @field_validator("base_calculo")
    @classmethod
    def _base_calculo_valida(cls, v: str) -> str:
        v = (v or "venda_bruta").strip()
        if v not in _BASES_CALCULO_VALIDAS:
            raise ValueError("Base de cálculo inválida (use venda_bruta, repasse ou lucro).")
        return v


class VariaveisEntrada(BaseModel):
    itens: list[VariavelEntrada]


def _serializar_variavel(v: VariavelConta) -> dict:
    return {
        "nome": v.nome,
        "percentual": v.percentual,
        "base_calculo": v.base_calculo or "venda_bruta",
        "atualizado_em": v.atualizado_em.isoformat() if v.atualizado_em else None,
        "atualizado_por": v.atualizado_por,
    }


@router.get("/variaveis")
def listar_variaveis(usuario: Usuario = Depends(usuario_logado_cmx), db: Session = Depends(get_db)):
    """Devolve todas as variáveis (taxas) cadastradas da conta do usuário logado."""
    conta = _conta_vinculada_do_usuario(usuario, db)
    variaveis = (
        db.query(VariavelConta)
        .filter(VariavelConta.conta_id == conta.id)
        .order_by(VariavelConta.nome)
        .all()
    )
    return {
        "conta": _serializar_conta(conta),
        "itens": [_serializar_variavel(v) for v in variaveis],
    }


@router.post("/variaveis")
def atualizar_variaveis(
    dados: VariaveisEntrada,
    usuario: Usuario = Depends(usuario_logado_cmx),
    db: Session = Depends(get_db),
):
    """
    Grava as variáveis em lote (substitui o valor de cada uma pelo nome,
    cria se não existir) -- sempre isolado pela conta do usuário logado,
    igual à gravação de custos.
    """
    if not dados.itens:
        raise HTTPException(status_code=400, detail="Nenhum item enviado.")

    conta = _conta_vinculada_do_usuario(usuario, db)
    existentes = {
        v.nome: v
        for v in db.query(VariavelConta).filter(VariavelConta.conta_id == conta.id).all()
    }

    resultado = []
    for item in dados.itens:
        registro = existentes.get(item.nome)
        if registro is None:
            registro = VariavelConta(conta_id=conta.id, nome=item.nome)
            db.add(registro)
            existentes[item.nome] = registro
        registro.percentual = item.percentual
        registro.base_calculo = item.base_calculo
        registro.atualizado_por = usuario.nome_exibicao
        resultado.append({"nome": item.nome, "percentual": item.percentual, "base_calculo": item.base_calculo})

    db.commit()
    return {"total_processados": len(resultado), "itens": resultado}


@router.delete("/variaveis/{nome}")
def remover_variavel(
    nome: str,
    usuario: Usuario = Depends(usuario_logado_cmx),
    db: Session = Depends(get_db),
):
    """Remove uma variável (taxa) específica da conta do usuário logado."""
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


@router.get("/validar")
def validar_sessao_extensao(usuario: Usuario = Depends(usuario_logado_cmx), db: Session = Depends(get_db)):
    """
    Checagem periódica que a extensão faz (ao abrir o popup, e de tempos
    em tempos durante o uso) pra confirmar que o token ainda vale e pra
    já trazer os dados da conta atualizados. Qualquer motivo de recusa
    (usuário desativado, conta removida/inativada, token vencido) chega
    aqui como 401/403/404 -- a extensão trata qualquer resposta que não
    seja 200 como "sessão encerrada, mostrar tela de login de novo".
    """
    conta = _conta_vinculada_do_usuario(usuario, db)
    return {
        "valido": True,
        "usuario": {"nome_exibicao": usuario.nome_exibicao},
        "conta": _serializar_conta(conta),
    }


# --- OAuth do Mercado Livre por trás do servidor (não mais na extensão) --
#
# 07-08/10: CORREÇÃO DE ROTA. A promoção ("aderir") é uma função do app
# ClubMarketplaceX -- um app DIFERENTE do app mestre (pré/pós-venda:
# mensagens, perguntas, vendas). Cada conta vendedora tem seu PRÓPRIO
# ClubMarketplaceX cadastrado no Mercado Livre Devs (client_id/secret
# próprios de cada conta, não um único app global) -- por isso ficam
# guardados em conta.cmx_client_id/cmx_client_secret, nunca numa
# variável de ambiente compartilhada.
#
# Esta rota (/ml/token) ANTES devolvia o token do app MESTRE
# (ml_client.garantir_token_valido) pra extensão usar também pra
# promoções -- só que o Mercado Livre bloqueia isso com
# "PA_UNAUTHORIZED_RESULT_FROM_POLICIES" (PolicyAgent), porque um token
# emitido por um app não serve pro outro. Confirmado comparando com a
# extensão antiga (mesma conta, usando o ClubMarketplaceX direto,
# aderiu 5 de 5 promoções). Por isso agora usa _garantir_token_cmx_valido
# (token/refresh próprios do ClubMarketplaceX, colunas cmx_*), não mais
# ml_client.garantir_token_valido.
#
# O client_secret de cada conta nunca sai daqui -- a extensão só manda o
# "code" capturado do redirect (uma vez, pra autorizar) e depois só pede
# o access_token já pronto por /ml/token.


class TrocarCodigoMlEntrada(BaseModel):
    code: str
    code_verifier: str
    redirect_uri: str


class RenovarTokenMlEntrada(BaseModel):
    refresh_token: str


def _chamar_endpoint_token_ml(payload: dict, contexto: str) -> dict:
    # A validação de client_id/client_secret é feita por quem monta o
    # payload (cada conta tem a sua própria, em conta.cmx_client_id/
    # cmx_client_secret) -- não existe mais uma credencial global única
    # pra validar aqui.
    try:
        resposta = httpx.post(CMX_ML_TOKEN_URL, data=payload, timeout=15)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Falha de rede ao {contexto}: {exc}") from exc

    if resposta.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Mercado Livre recusou {contexto} (status {resposta.status_code}): {resposta.text}",
        )
    return resposta.json()


def _salvar_token_cmx_na_conta(conta: Conta, dados_token: dict, db: Session) -> None:
    """
    Grava o token do app ClubMarketplaceX na conta certa (nunca em outra
    -- ver checagem de ml_user_id em trocar_codigo_ml, antes de chamar
    esta função). Mesmo padrão de expiração usado no app mestre
    (app/auth_ml.py): guarda a margem de segurança já no cálculo, igual
    _garantir_token_cmx_valido espera encontrar.
    """
    expira_em_segundos = dados_token.get("expires_in", 21600)  # 6h, fallback do ML
    conta.cmx_access_token = dados_token.get("access_token")
    conta.cmx_refresh_token = dados_token.get("refresh_token")
    conta.cmx_token_expira_em = datetime.utcnow() + timedelta(seconds=expira_em_segundos)
    db.commit()
    db.refresh(conta)


def _garantir_token_cmx_valido(conta: Conta, db: Session) -> str:
    """
    Equivalente a ml_client.garantir_token_valido, só que pro app
    ClubMarketplaceX da PRÓPRIA conta (conta.cmx_client_id/secret) --
    são apps diferentes, com tokens que não se misturam. Renova sozinho
    via refresh_token quando está perto de vencer (mesma margem de 5 min
    usada no app mestre), e atualiza o banco.
    """
    if not conta.cmx_client_id or not conta.cmx_client_secret:
        raise HTTPException(
            status_code=409,
            detail=(
                f"A conta '{conta.apelido}' ainda não tem o app ClubMarketplaceX "
                f"configurado no servidor (client_id/client_secret) -- cadastre primeiro "
                f"as credenciais dessa conta (as mesmas do Mercado Livre Devs) antes de "
                f"autorizar/usar a adesão a promoções."
            ),
        )

    if not conta.cmx_access_token or not conta.cmx_refresh_token:
        raise HTTPException(
            status_code=409,
            detail=(
                f"A conta '{conta.apelido}' ainda não autorizou o app ClubMarketplaceX "
                f"-- é preciso logar na extensão e autorizar o Mercado Livre uma vez "
                f"(abre a aba de login) antes de conseguir aderir a promoções."
            ),
        )

    margem_seguranca = timedelta(minutes=5)
    if conta.cmx_token_expira_em and conta.cmx_token_expira_em > datetime.utcnow() + margem_seguranca:
        return conta.cmx_access_token  # ainda válido, nada a fazer

    payload = {
        "grant_type": "refresh_token",
        "client_id": conta.cmx_client_id,
        "client_secret": conta.cmx_client_secret,
        "refresh_token": conta.cmx_refresh_token,
    }
    dados_token = _chamar_endpoint_token_ml(payload, f"renovar o token do app ClubMarketplaceX da conta '{conta.apelido}'")
    _salvar_token_cmx_na_conta(conta, dados_token, db)
    return conta.cmx_access_token


@router.get("/ml/client-id")
def client_id_promocoes_da_conta_vinculada(
    usuario: Usuario = Depends(usuario_logado_cmx),
    db: Session = Depends(get_db),
):
    """
    Devolve só o client_id (nunca o secret) do app ClubMarketplaceX da
    conta vinculada ao login -- o client_id não é segredo (ele vai na
    própria URL de autorização, visível pra qualquer um), então é seguro
    mandar pra extensão montar a URL de login. Usado só na autorização
    única (ver /ml/trocar-codigo abaixo); depois disso a extensão nunca
    mais precisa dele, só do access_token (via /ml/token).
    """
    conta = _conta_vinculada_do_usuario(usuario, db)
    if not conta.cmx_client_id:
        raise HTTPException(
            status_code=409,
            detail=(
                f"A conta '{conta.apelido}' ainda não tem o app ClubMarketplaceX "
                f"configurado no servidor -- fale com o admin pra cadastrar o client_id/client_secret."
            ),
        )
    return {"client_id": conta.cmx_client_id, "apelido": conta.apelido}


@router.post("/ml/trocar-codigo")
def trocar_codigo_ml(
    dados: TrocarCodigoMlEntrada,
    usuario: Usuario = Depends(usuario_logado_cmx),
    db: Session = Depends(get_db),
):
    """
    Troca o "code" do OAuth do Mercado Livre por access_token/refresh_token
    do app ClubMarketplaceX (promoções) e SALVA na conta vinculada ao
    usuário logado -- autorização de uma vez por conta, feita pela
    extensão (abre a aba de login, captura o "code" do redirect e manda
    pra cá). O client_secret da conta nunca sai daqui.

    07/10: cada conta tem seu próprio app cadastrado no Mercado Livre
    Devs -- por isso a conta é resolvida ANTES de montar o payload, pra
    usar a credencial certa (conta.cmx_client_id/cmx_client_secret).
    """
    conta = _conta_vinculada_do_usuario(usuario, db)
    if not conta.cmx_client_id or not conta.cmx_client_secret:
        raise HTTPException(
            status_code=409,
            detail=(
                f"A conta '{conta.apelido}' ainda não tem o app ClubMarketplaceX "
                f"configurado no servidor (client_id/client_secret) -- cadastre primeiro "
                f"as credenciais dessa conta antes de autorizar o Mercado Livre."
            ),
        )

    payload = {
        "grant_type": "authorization_code",
        "client_id": conta.cmx_client_id,
        "client_secret": conta.cmx_client_secret,
        "code": dados.code,
        "redirect_uri": dados.redirect_uri,
        "code_verifier": dados.code_verifier,
    }
    dados_token = _chamar_endpoint_token_ml(payload, "trocar o código por token")

    ml_user_id_autorizado = str(dados_token.get("user_id", ""))
    if ml_user_id_autorizado and ml_user_id_autorizado != str(conta.ml_user_id):
        # Mesma trava de segurança do app mestre (app/auth_ml.py callback):
        # quem autorizou no Mercado Livre não é a mesma conta vinculada a
        # este usuário no painel -- provavelmente a pessoa estava logada
        # no Mercado Livre com a conta errada ao autorizar. Não salva nada.
        raise HTTPException(
            status_code=409,
            detail=(
                f"A conta do Mercado Livre que acabou de autorizar não é a '{conta.apelido}' "
                f"(é outra conta, ml_user_id={ml_user_id_autorizado}). Nada foi salvo -- "
                f"refaça o login no Mercado Livre com a conta vendedora certa antes de autorizar."
            ),
        )

    _salvar_token_cmx_na_conta(conta, dados_token, db)
    return dados_token


@router.post("/ml/renovar-token")
def renovar_token_ml(
    dados: RenovarTokenMlEntrada,
    usuario: Usuario = Depends(usuario_logado_cmx),
    db: Session = Depends(get_db),
):
    """
    Renova o access_token do Mercado Livre a partir do refresh_token, sem
    expor o secret na extensão. Rota manual, não usada pelo background.js
    atual (que usa /ml/token, com renovação automática via
    _garantir_token_cmx_valido) -- mantida só por compatibilidade, usando
    a credencial da própria conta (conta.cmx_client_id/secret).
    """
    conta = _conta_vinculada_do_usuario(usuario, db)
    if not conta.cmx_client_id or not conta.cmx_client_secret:
        raise HTTPException(
            status_code=409,
            detail=(
                f"A conta '{conta.apelido}' ainda não tem o app ClubMarketplaceX "
                f"configurado no servidor (client_id/client_secret)."
            ),
        )
    payload = {
        "grant_type": "refresh_token",
        "client_id": conta.cmx_client_id,
        "client_secret": conta.cmx_client_secret,
        "refresh_token": dados.refresh_token,
    }
    return _chamar_endpoint_token_ml(payload, "renovar o token")


@router.get("/ml/token")
def token_ml_da_conta_vinculada(
    usuario: Usuario = Depends(usuario_logado_cmx),
    db: Session = Depends(get_db),
):
    """
    Devolve o access_token do app ClubMarketplaceX (promoções) da conta
    vinculada ao usuário logado na extensão -- e só dessa conta, nunca de
    outra (mesma trava de /custos e /variaveis: quem decide a conta é o
    login, não o que a extensão pede). Renova sozinho via
    _garantir_token_cmx_valido quando está perto de vencer.

    Cada conta vendedora precisa autorizar o app ClubMarketplaceX pelo
    menos uma vez (fluxo /ml/trocar-codigo) antes desta rota funcionar
    pra ela -- sem isso, vira 409 abaixo.
    """
    conta = _conta_vinculada_do_usuario(usuario, db)
    access_token = _garantir_token_cmx_valido(conta, db)

    expira_em_segundos = 0
    if conta.cmx_token_expira_em:
        expira_em_segundos = max(0, int((conta.cmx_token_expira_em - datetime.utcnow()).total_seconds()))

    return {
        "access_token": access_token,
        "ml_user_id": str(conta.ml_user_id),
        "apelido": conta.apelido,
        "expira_em_segundos": expira_em_segundos,
    }


# --- Configuração do app ClubMarketplaceX de cada conta (admin) ---------
#
# 07/10: cada conta vendedora tem seu próprio app cadastrado no Mercado
# Livre Devs (client_id/client_secret próprios) -- diferente do app
# mestre, que é um só compartilhado por todas as contas. Essa rota é
# como o admin cadastra, no servidor, a credencial de cada conta (uma
# vez por conta), pra depois a extensão poder pedir o token dela sem
# nunca ver o client_secret. Autenticação igual ao painel (cookie de
# sessão), não o login da extensão -- isso aqui só o admin faz.


class CredenciaisCmxEntrada(BaseModel):
    client_id: str
    client_secret: str

    @field_validator("client_id", "client_secret")
    @classmethod
    def _nao_vazio(cls, valor: str) -> str:
        valor = valor.strip()
        if not valor:
            raise ValueError("não pode ser vazio")
        return valor


@router.post("/admin/app-promocoes/{apelido}")
def definir_credenciais_cmx(
    apelido: str,
    dados: CredenciaisCmxEntrada,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Cadastra/atualiza o client_id+client_secret do app ClubMarketplaceX
    (promoções) de uma conta específica. Só admin. O client_secret fica
    só aqui no banco do servidor -- nunca é devolvido pra extensão nem
    pro navegador (só o access_token de curta duração sai por /ml/token).
    """
    usuario = auth.usuario_atual(request, db)
    if not auth.papel_permite(usuario, ("admin",)):
        raise HTTPException(status_code=403, detail="Só admin pode configurar o app ClubMarketplaceX das contas.")

    conta = next((c for c in db.query(Conta).all() if chave_conta(c.apelido) == chave_conta(apelido)), None)
    if conta is None:
        raise HTTPException(status_code=404, detail=f"Conta '{apelido}' não encontrada.")

    conta.cmx_client_id = dados.client_id
    conta.cmx_client_secret = dados.client_secret
    db.commit()
    db.refresh(conta)

    return {
        "apelido": conta.apelido,
        "cmx_client_id": conta.cmx_client_id,
        "salvo": True,
    }


# --- Histórico de adesões (cópia no servidor) ----------------------------
#
# A extensão continua gravando a cópia LOCAL do histórico (chrome.storage,
# inalterada) -- isto aqui é uma SEGUNDA cópia, enviada em segundo plano
# (sem travar a tela de Promoções), pra alimentar a página "Produtos >
# Histórico de adesões" do painel: sobrevive a desinstalar a extensão,
# junta os registros de vários logins/PCs na mesma conta, e é pesquisável
# no servidor. Ver app/models.py:EventoAdesaoCmx pro desenho completo
# (idempotência via id_envio, índice composto conta_id+data_adesao).

CMX_HISTORICO_LOTE_MAXIMO = 500  # trava de segurança no tamanho de 1 requisição -- a extensão já manda em lotes de até 200
CMX_HISTORICO_DIAS_PADRAO = 10  # janela padrão da consulta quando a extensão/painel não pedem um período específico
CMX_HISTORICO_DIAS_MAXIMO = 90  # mesmo pedindo um período maior, nunca varre mais que isso (ver listar_historico_adesoes)
CMX_HISTORICO_PAGINA_TAMANHO = 50


class EventoAdesaoCmxEntrada(BaseModel):
    id_envio: str
    item_id: str
    sku: str | None = None
    nome: str | None = None
    tipo_promocao: str | None = None
    preco_final: float | None = None
    voce_recebe: float | None = None
    custo: float | None = None
    lucro_liquido: float | None = None
    sucesso: bool
    motivo: str | None = None
    data_adesao: datetime
    maquina: str | None = None

    @field_validator("id_envio", "item_id")
    @classmethod
    def _nao_vazio(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("Campo obrigatório vazio.")
        return v


class HistoricoAdesoesEntrada(BaseModel):
    itens: list[EventoAdesaoCmxEntrada]


@router.post("/historico-adesoes")
def gravar_historico_adesoes(
    dados: HistoricoAdesoesEntrada,
    usuario: Usuario = Depends(usuario_logado_cmx),
    db: Session = Depends(get_db),
):
    """
    Grava em lote os eventos de adesão que a extensão já registrou
    localmente -- SEMPRE em lote (a extensão nunca manda 1 por chamada,
    ver fila de retry no content.js/background.js), pra não virar N
    requisições por lote de adesão com 100+ contas usando ao mesmo tempo.

    IDEMPOTENTE por `id_envio`: a extensão reenvia o mesmo lote (com os
    mesmos id_envio) quando uma tentativa anterior falhou por rede/timeout
    -- aqui, qualquer id_envio que já exista pra esta conta é simplesmente
    ignorado (não duplica, não sobrescreve), em vez de dar erro. Isolado
    pela conta vinculada ao login, igual a toda outra rota deste arquivo
    -- nunca grava em outra conta, mesmo que o payload tentasse.
    """
    if not dados.itens:
        raise HTTPException(status_code=400, detail="Nenhum item enviado.")
    if len(dados.itens) > CMX_HISTORICO_LOTE_MAXIMO:
        raise HTTPException(
            status_code=400,
            detail=f"Lote grande demais ({len(dados.itens)} itens) -- envie no máximo {CMX_HISTORICO_LOTE_MAXIMO} por chamada.",
        )

    conta = _conta_vinculada_do_usuario(usuario, db)

    ids_recebidos = [item.id_envio for item in dados.itens]
    ja_existentes = {
        row[0]
        for row in db.query(EventoAdesaoCmx.id_envio)
        .filter(EventoAdesaoCmx.conta_id == conta.id, EventoAdesaoCmx.id_envio.in_(ids_recebidos))
        .all()
    }

    novos = 0
    ja_tinha = 0
    vistos_neste_lote: set[str] = set()  # o mesmo lote pode repetir um id_envio (bug do cliente) -- não duplica também
    for item in dados.itens:
        if item.id_envio in ja_existentes or item.id_envio in vistos_neste_lote:
            ja_tinha += 1
            continue
        vistos_neste_lote.add(item.id_envio)
        db.add(
            EventoAdesaoCmx(
                id_envio=item.id_envio,
                conta_id=conta.id,
                usuario=usuario.nome_exibicao,
                maquina=item.maquina,
                item_id=item.item_id,
                sku=item.sku,
                nome_anuncio=item.nome,
                tipo_promocao=item.tipo_promocao,
                preco_final=item.preco_final,
                voce_recebe=item.voce_recebe,
                custo=item.custo,
                lucro_liquido=item.lucro_liquido,
                sucesso=item.sucesso,
                motivo=item.motivo,
                data_adesao=item.data_adesao,
            )
        )
        novos += 1

    try:
        db.commit()
    except IntegrityError:
        # Rede de segurança: duas requisições com o mesmo id_envio chegando
        # em paralelo (ex: aba duplicada) poderiam colidir na UNIQUE do
        # banco entre a checagem acima e o commit. Não é erro de verdade
        # pro cliente -- só significa que outra tentativa já gravou
        # primeiro; a extensão trata qualquer "sucesso" como "pode tirar
        # da fila".
        db.rollback()

    return {"recebidos": len(dados.itens), "gravados": novos, "ja_existentes": ja_tinha}


def _serializar_evento_adesao(e: EventoAdesaoCmx) -> dict:
    return {
        "id_envio": e.id_envio,
        "usuario": e.usuario,
        "maquina": e.maquina,
        "item_id": e.item_id,
        "sku": e.sku,
        "nome": e.nome_anuncio,
        "tipo_promocao": e.tipo_promocao,
        "preco_final": e.preco_final,
        "voce_recebe": e.voce_recebe,
        "custo": e.custo,
        "lucro_liquido": e.lucro_liquido,
        "sucesso": e.sucesso,
        "motivo": e.motivo,
        "data_adesao": e.data_adesao.isoformat() if e.data_adesao else None,
    }


def consultar_historico_adesoes(
    conta: Conta,
    db: Session,
    desde: datetime | None,
    ate: datetime | None,
    busca: str,
    pagina: int,
) -> dict:
    """
    Lógica da consulta paginada de histórico, compartilhada pelas duas
    rotas que a expõem (extensão, Bearer, abaixo; e painel, cookie, em
    app/routers/historico_adesoes_painel.py) -- mesma conta, mesma regra,
    só muda de onde vem o usuário logado.

    Por desempenho com 100+ contas, a janela de data é OBRIGATÓRIA (nunca
    varre a tabela inteira): sem `desde`, assume os últimos
    CMX_HISTORICO_DIAS_PADRAO dias; mesmo pedindo um período maior, o
    início nunca passa de CMX_HISTORICO_DIAS_MAXIMO dias atrás -- quem
    quiser mais do que isso precisa pedir em partes. Sempre paginado
    (CMX_HISTORICO_PAGINA_TAMANHO por página) e sempre com o índice
    composto (conta_id, data_adesao) cobrindo o filtro principal.
    """
    agora = datetime.utcnow()
    limite_mais_antigo = agora - timedelta(days=CMX_HISTORICO_DIAS_MAXIMO)
    inicio = desde or (agora - timedelta(days=CMX_HISTORICO_DIAS_PADRAO))
    if inicio < limite_mais_antigo:
        inicio = limite_mais_antigo
    fim = ate or agora

    consulta = db.query(EventoAdesaoCmx).filter(
        EventoAdesaoCmx.conta_id == conta.id,
        EventoAdesaoCmx.data_adesao >= inicio,
        EventoAdesaoCmx.data_adesao <= fim,
    )

    termo = (busca or "").strip()
    if termo:
        termo_like = f"%{termo}%"
        consulta = consulta.filter(
            (EventoAdesaoCmx.nome_anuncio.ilike(termo_like))
            | (EventoAdesaoCmx.sku.ilike(termo_like))
            | (EventoAdesaoCmx.item_id.ilike(termo_like))
            | (EventoAdesaoCmx.usuario.ilike(termo_like))
        )

    pagina = max(1, pagina)
    total = consulta.count()
    itens = (
        consulta.order_by(EventoAdesaoCmx.data_adesao.desc())
        .offset((pagina - 1) * CMX_HISTORICO_PAGINA_TAMANHO)
        .limit(CMX_HISTORICO_PAGINA_TAMANHO)
        .all()
    )

    return {
        "conta": _serializar_conta(conta),
        "desde": inicio.isoformat(),
        "ate": fim.isoformat(),
        "pagina": pagina,
        "tamanho_pagina": CMX_HISTORICO_PAGINA_TAMANHO,
        "total": total,
        "total_paginas": (total + CMX_HISTORICO_PAGINA_TAMANHO - 1) // CMX_HISTORICO_PAGINA_TAMANHO if total else 0,
        "itens": [_serializar_evento_adesao(e) for e in itens],
    }


@router.get("/historico-adesoes")
def listar_historico_adesoes(
    desde: datetime | None = None,
    ate: datetime | None = None,
    busca: str = "",
    pagina: int = 1,
    usuario: Usuario = Depends(usuario_logado_cmx),
    db: Session = Depends(get_db),
):
    """Mesma consulta de consultar_historico_adesoes, pelo login da extensão (Bearer token)."""
    conta = _conta_vinculada_do_usuario(usuario, db)
    return consultar_historico_adesoes(conta, db, desde, ate, busca, pagina)
