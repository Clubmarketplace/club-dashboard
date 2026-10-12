"""
Tela/API exclusiva do papel "admin": resumo de valor vendido de TODAS
as contas, pra saber o movimento geral da rede, sem precisar entrar no
painel de custo/venda detalhado de cada uma.

Importante (correção de 12/10): isso NUNCA deve ficar acessível pro
supervisor. Diferente do resto do painel interno (chamados, cadastro,
cancelamentos), onde admin e supervisor têm o mesmo acesso amplo
(_AREAS_POR_PAPEL, em app/main.py, não restringe nenhum dos dois),
aqui o valor vendido de cada conta é informação que só o admin pode
ver -- por isso a dependency abaixo checa `papel == "admin"` direto,
em vez de reusar algo que valeria pros dois.

O painel de custo/venda DETALHADO (margem, lucro -- app/routers/vendas.py)
continua separado: admin só abre o de uma conta específica se ela
estiver vinculada ao usuário dele (mesma regra de UsuarioConta que vale
pra seller/suporte). Aqui é só o resumo simples (valor vendido), de
todas as contas de uma vez, sem depender de vínculo.
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app import auth
from app.database import get_db
from app.models import Conta, Usuario, Venda
from app.routers.vendas import _somente_vendas_validas, _FUSO_BR

router = APIRouter(prefix="/api/painel/admin", tags=["painel-admin"])


def _admin_logado(request: Request, db: Session = Depends(get_db)) -> Usuario:
    """
    Dependency equivalente ao _seller_logado (custos_painel.py), só que
    pra admin -- de propósito NÃO aceita supervisor, mesmo ele tendo
    acesso amplo ao resto do painel interno. Ver docstring do módulo.
    """
    usuario = auth.usuario_atual(request, db)
    if not usuario or usuario.papel != "admin":
        raise HTTPException(status_code=403, detail="Acesso restrito ao admin.")
    return usuario


def _inicio_do_dia_e_do_mes_em_utc() -> tuple[datetime, datetime]:
    """
    'Hoje' e 'este mês' no calendário de Brasília, convertidos pro UTC
    que `Venda.data_venda` usa -- mesmo cuidado de fuso do resumo de
    vendas do seller (app/routers/vendas.py:_periodo).
    """
    agora_br = datetime.now(_FUSO_BR)
    inicio_dia_br = agora_br.replace(hour=0, minute=0, second=0, microsecond=0)
    inicio_mes_br = inicio_dia_br.replace(day=1)
    return (
        inicio_dia_br.astimezone(timezone.utc).replace(tzinfo=None),
        inicio_mes_br.astimezone(timezone.utc).replace(tzinfo=None),
    )


@router.get("/resumo-contas")
def resumo_contas(usuario: Usuario = Depends(_admin_logado), db: Session = Depends(get_db)):
    """
    Pra cada conta ativa: valor vendido hoje e no mês. No final, a soma
    de todas -- o "movimento geral" que o admin pediu pra acompanhar.
    Resumo simples de propósito (só valor): o detalhe de margem/lucro
    de uma conta é visto no painel de custo/venda dela, não aqui.
    """
    inicio_dia_utc, inicio_mes_utc = _inicio_do_dia_e_do_mes_em_utc()

    contas = (
        db.query(Conta)
        .filter(Conta.ativa.is_(True), Conta.inativa_em.is_(None))
        .order_by(Conta.apelido)
        .all()
    )

    resultado = []
    total_hoje = 0.0
    total_mes = 0.0
    for conta in contas:
        vendido_hoje = (
            _somente_vendas_validas(
                db.query(Venda).filter(Venda.conta_id == conta.id, Venda.data_venda >= inicio_dia_utc)
            ).with_entities(Venda.venda_bruta).all()
        )
        vendido_mes = (
            _somente_vendas_validas(
                db.query(Venda).filter(Venda.conta_id == conta.id, Venda.data_venda >= inicio_mes_utc)
            ).with_entities(Venda.venda_bruta).all()
        )
        valor_hoje = sum(v[0] for v in vendido_hoje)
        valor_mes = sum(v[0] for v in vendido_mes)
        total_hoje += valor_hoje
        total_mes += valor_mes
        resultado.append(
            {
                "conta_id": conta.id,
                "apelido": conta.apelido,
                "vendido_hoje": round(valor_hoje, 2),
                "vendido_mes": round(valor_mes, 2),
                "pedidos_mes": len(vendido_mes),
            }
        )

    return {
        "contas": resultado,
        "total_geral": {
            "vendido_hoje": round(total_hoje, 2),
            "vendido_mes": round(total_mes, 2),
            "quantidade_contas": len(resultado),
        },
    }
