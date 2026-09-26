"""
Autenticação por sessão, sem dependências externas de terceiros.

Como funciona:
  1. Login com sucesso -> gera um token (usuario_id + validade),
     assinado com HMAC-SHA256 usando SESSION_SECRET_KEY. Guardado num
     cookie httponly (JavaScript não consegue ler) chamado
     "cmx_sessao".
  2. A cada request, o middleware em main.py confere a assinatura do
     cookie -- se bater e não tiver vencido, a pessoa está autenticada.
     Se a assinatura não bater (cookie forjado) ou tiver vencido, é
     tratado como deslogado.
  3. Senhas nunca são guardadas em texto puro: usamos PBKDF2-HMAC-SHA256
     (biblioteca padrão do Python, sem precisar instalar nada) com um
     salt aleatório por usuário.

Isso NÃO é OAuth nem JWT -- é um mecanismo simples e auditável,
proporcional ao tamanho do time que vai usar o painel. Se um dia o
time crescer muito ou precisar de login social, dá pra trocar por algo
mais robusto sem mexer no resto do sistema (a interface girar em torno
de `usuario_atual` continua igual).
"""
import hashlib
import hmac
import secrets
import time
from typing import Optional

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.config import SESSION_SECRET_KEY
from app.database import get_db
from app.models import Usuario

COOKIE_SESSAO = "cmx_sessao"
DURACAO_SESSAO_SEGUNDOS = 60 * 60 * 12  # 12 horas

# Se a variável de ambiente não estiver definida, gera uma chave
# aleatória em memória -- funciona, mas invalida todas as sessões a
# cada reinício do processo. Só é aceitável em desenvolvimento local;
# em produção (Railway), SESSION_SECRET_KEY é obrigatória de verdade.
_CHAVE_SECRETA = (SESSION_SECRET_KEY or secrets.token_hex(32)).encode()


# --- Hash de senha ---

def gerar_hash_senha(senha: str) -> str:
    """Gera 'salt$hash' -- formato próprio, guardado inteiro em senha_hash."""
    sal = secrets.token_hex(16)
    hash_bytes = hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), bytes.fromhex(sal), 200_000)
    return f"{sal}${hash_bytes.hex()}"


def verificar_senha(senha_digitada: str, hash_salvo: str) -> bool:
    """Compara em tempo constante (evita ataque de timing)."""
    try:
        sal, hash_hex_esperado = hash_salvo.split("$", 1)
        hash_bytes = hashlib.pbkdf2_hmac("sha256", senha_digitada.encode("utf-8"), bytes.fromhex(sal), 200_000)
        return hmac.compare_digest(hash_bytes.hex(), hash_hex_esperado)
    except (ValueError, AttributeError):
        return False


def gerar_codigo_primeiro_acesso() -> str:
    """Código curto, fácil de repassar por WhatsApp/mensagem (ex: 'K7M2-PQ9X')."""
    alfabeto = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # sem 0/O/1/I, pra evitar confusão visual
    partes = ["".join(secrets.choice(alfabeto) for _ in range(4)) for _ in range(2)]
    return "-".join(partes)


# --- Cookie de sessão assinado ---

def _assinar(valor: str) -> str:
    assinatura = hmac.new(_CHAVE_SECRETA, valor.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{valor}.{assinatura}"


def _valor_se_assinatura_valida(token: str) -> Optional[str]:
    try:
        valor, assinatura_recebida = token.rsplit(".", 1)
    except ValueError:
        return None
    assinatura_esperada = hmac.new(_CHAVE_SECRETA, valor.encode("utf-8"), hashlib.sha256).hexdigest()
    if hmac.compare_digest(assinatura_recebida, assinatura_esperada):
        return valor
    return None


# O perfil "tv" (telas de TV, só visualização) fica logado bem mais tempo:
# a TV fica ligada o dia inteiro e não tem ninguém pra digitar a senha.
DURACAO_SESSAO_TV_SEGUNDOS = 60 * 60 * 24 * 30  # 30 dias


def duracao_sessao(papel: Optional[str]) -> int:
    return DURACAO_SESSAO_TV_SEGUNDOS if papel == "tv" else DURACAO_SESSAO_SEGUNDOS


def criar_token_sessao(usuario_id: int, duracao_segundos: Optional[int] = None) -> str:
    expira_em = int(time.time()) + (duracao_segundos or DURACAO_SESSAO_SEGUNDOS)
    return _assinar(f"{usuario_id}:{expira_em}")


def ler_usuario_id_da_sessao(token: str) -> Optional[int]:
    """Devolve o usuario_id se o cookie for válido e ainda não tiver vencido, senão None."""
    valor = _valor_se_assinatura_valida(token)
    if not valor:
        return None
    try:
        usuario_id_str, expira_em_str = valor.split(":")
        if int(time.time()) > int(expira_em_str):
            return None
        return int(usuario_id_str)
    except (ValueError, AttributeError):
        return None


# --- Dependências do FastAPI ---

def usuario_atual(request: Request, db: Session = Depends(get_db)) -> Optional[Usuario]:
    """Devolve o Usuario logado, ou None. Não levanta erro -- quem chama decide o que fazer."""
    token = request.cookies.get(COOKIE_SESSAO)
    if not token:
        return None
    usuario_id = ler_usuario_id_da_sessao(token)
    if not usuario_id:
        return None
    return db.query(Usuario).filter(Usuario.id == usuario_id, Usuario.ativo.is_(True)).first()


def papel_permite(usuario: Optional[Usuario], papeis_permitidos: tuple) -> bool:
    """Confere se o usuário (já logado) tem um dos papéis permitidos pra essa ação."""
    return usuario is not None and usuario.papel in papeis_permitidos
