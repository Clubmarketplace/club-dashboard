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
import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import auth
from app.config import CMX_ML_CLIENT_ID, CMX_ML_CLIENT_SECRET
from app.contas_util import chave_conta
from app.database import get_db
from app.models import Conta, CustoSku, Usuario, VariavelConta

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
    if usuario.papel != "seller" or not usuario.conta_vinculada:
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

        resultado.append(
            {
                "sku": item.sku,
                "custo_anterior": custo_anterior,
                "custo_novo": item.custo,
                "alterado": custo_anterior != item.custo,
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


class VariavelEntrada(BaseModel):
    nome: str
    percentual: float

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


class VariaveisEntrada(BaseModel):
    itens: list[VariavelEntrada]


def _serializar_variavel(v: VariavelConta) -> dict:
    return {
        "nome": v.nome,
        "percentual": v.percentual,
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
        registro.atualizado_por = usuario.nome_exibicao
        resultado.append({"nome": item.nome, "percentual": item.percentual})

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
# Antes, a troca do "code" por token e a renovação do token aconteciam
# DIRETO na extensão (background.js), usando CMX_ML_CLIENT_SECRET escrito
# no próprio arquivo -- que vai dentro do pacote publicado na Chrome Web
# Store. Qualquer pessoa que instalasse a extensão conseguia abrir o
# "Inspecionar" em chrome://extensions e ler esse secret em texto puro.
# Se vazasse e fosse usado de forma abusiva, o Mercado Livre poderia
# suspender o client_id inteiro -- derrubando o login de TODOS os
# sellers de uma vez, não só de quem vazou.
#
# Agora essas duas etapas passam por aqui: o secret fica só numa
# variável de ambiente no servidor (CMX_ML_CLIENT_SECRET), nunca em
# código nenhum que saia daqui. A extensão continua fazendo a parte que
# só ela pode fazer (abrir a aba de login e capturar o "code" do
# redirect), mas manda esse "code" pra cá em vez de trocar direto com o
# Mercado Livre. Protegido pelo mesmo login da extensão (usuário
# precisa estar autenticado no Club Marketplace pra conectar o ML).


class TrocarCodigoMlEntrada(BaseModel):
    code: str
    code_verifier: str
    redirect_uri: str


class RenovarTokenMlEntrada(BaseModel):
    refresh_token: str


def _chamar_endpoint_token_ml(payload: dict, contexto: str) -> dict:
    if not CMX_ML_CLIENT_ID or not CMX_ML_CLIENT_SECRET:
        raise HTTPException(
            status_code=500,
            detail="CMX_ML_CLIENT_ID/CMX_ML_CLIENT_SECRET não configurados no servidor (variáveis de ambiente).",
        )
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


@router.post("/ml/trocar-codigo")
def trocar_codigo_ml(
    dados: TrocarCodigoMlEntrada,
    usuario: Usuario = Depends(usuario_logado_cmx),
):
    """
    Troca o "code" do OAuth do Mercado Livre por access_token/refresh_token.
    Chamada pela extensão logo depois de capturar o "code" do redirect --
    o CMX_ML_CLIENT_SECRET nunca sai daqui.
    """
    payload = {
        "grant_type": "authorization_code",
        "client_id": CMX_ML_CLIENT_ID,
        "client_secret": CMX_ML_CLIENT_SECRET,
        "code": dados.code,
        "redirect_uri": dados.redirect_uri,
        "code_verifier": dados.code_verifier,
    }
    return _chamar_endpoint_token_ml(payload, "trocar o código por token")


@router.post("/ml/renovar-token")
def renovar_token_ml(
    dados: RenovarTokenMlEntrada,
    usuario: Usuario = Depends(usuario_logado_cmx),
):
    """Renova o access_token do Mercado Livre a partir do refresh_token, sem expor o secret na extensão."""
    payload = {
        "grant_type": "refresh_token",
        "client_id": CMX_ML_CLIENT_ID,
        "client_secret": CMX_ML_CLIENT_SECRET,
        "refresh_token": dados.refresh_token,
    }
    return _chamar_endpoint_token_ml(payload, "renovar o token")
