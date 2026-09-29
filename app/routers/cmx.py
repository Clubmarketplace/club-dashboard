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
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import auth
from app.contas_util import chave_conta
from app.database import get_db
from app.models import Conta, CustoSku, Usuario

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
