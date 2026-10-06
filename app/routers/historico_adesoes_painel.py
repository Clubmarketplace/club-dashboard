"""
Rota do histórico de adesões acessada PELO PAINEL (cookie de sessão do
seller), não pela extensão. Mesma tabela (EventoAdesaoCmx) e mesma regra
de isolamento por conta que app/routers/cmx.py -- só muda de onde vem o
usuário logado (cookie aqui, Bearer token lá), então reaproveitamos
`_conta_vinculada_do_usuario` e `consultar_historico_adesoes` de lá em
vez de duplicar a lógica de consulta.

Esta rota é só LEITURA -- quem grava o histórico é sempre a extensão
(POST /api/cmx/historico-adesoes), nunca o painel.
"""
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.routers.cmx import _conta_vinculada_do_usuario, consultar_historico_adesoes
from app.routers.custos_painel import _seller_logado
from app.models import Usuario

router = APIRouter(prefix="/api/painel/historico-adesoes", tags=["painel-historico-adesoes"])


@router.get("")
def listar_historico_adesoes_painel(
    desde: datetime | None = None,
    ate: datetime | None = None,
    busca: str = "",
    pagina: int = 1,
    usuario: Usuario = Depends(_seller_logado),
    db: Session = Depends(get_db),
):
    """Mesma consulta de app/routers/cmx.py:consultar_historico_adesoes, pelo cookie de sessão do painel."""
    conta = _conta_vinculada_do_usuario(usuario, db)
    return consultar_historico_adesoes(conta, db, desde, ate, busca, pagina)
