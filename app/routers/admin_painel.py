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
from app.models import Conta, MensagemPosVenda, Pergunta, SolicitacaoCancelamento, Usuario, Venda
from app.routers.vendas import _somente_vendas_validas, _FUSO_BR

_STATUS_PERGUNTA_RESPONDIDA = ("respondida", "respondida_externamente")

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


def _quem_fez_mais(linhas: list, campo_autor: str) -> dict | None:
    """Agrupa uma lista de linhas (perguntas/mensagens/solicitações já
    filtradas) pelo autor e devolve quem tem mais -- usado nos cards de
    setor ("Chamados", "Cancelamentos"). None se não tiver nenhuma."""
    contagem: dict[str, int] = {}
    for linha in linhas:
        autor = getattr(linha, campo_autor, None)
        if not autor:
            continue
        contagem[autor] = contagem.get(autor, 0) + 1
    if not contagem:
        return None
    nome, qtd = max(contagem.items(), key=lambda item: item[1])
    return {"nome": nome, "quantidade": qtd}


def _setor_chamados(db: Session, inicio_utc: datetime) -> dict:
    """Pré-venda (Pergunta) + pós-venda (MensagemPosVenda) juntos, igual
    o card "Chamados" do dashboard -- total recebido, respondido,
    pendente, e quem respondeu mais (por atendente)."""
    perguntas = db.query(Pergunta).filter(Pergunta.recebida_em >= inicio_utc).all()
    mensagens = db.query(MensagemPosVenda).filter(MensagemPosVenda.recebida_em >= inicio_utc).all()

    respondidas_perguntas = [p for p in perguntas if p.status in _STATUS_PERGUNTA_RESPONDIDA]
    respondidas_mensagens = [m for m in mensagens if m.status == "respondida"]
    total = len(perguntas) + len(mensagens)
    respondidas = len(respondidas_perguntas) + len(respondidas_mensagens)

    # Só a pergunta tem "respondida_por" preenchido hoje (atendente que
    # responde pela tela); mensagem de pós-venda ainda não grava autor.
    top = _quem_fez_mais(respondidas_perguntas, "respondida_por")

    return {"total": total, "respondidas": respondidas, "pendentes": total - respondidas, "top": top}


def _setor_cancelamentos(db: Session, inicio_utc: datetime) -> dict:
    """Solicitações de cancelamento (SolicitacaoCancelamento) de TODAS
    as contas -- total, confirmadas, pendentes, e quem confirmou mais."""
    solicitacoes = (
        db.query(SolicitacaoCancelamento)
        .filter(SolicitacaoCancelamento.criado_em >= inicio_utc)
        .filter((SolicitacaoCancelamento.tipo.is_(None)) | (SolicitacaoCancelamento.tipo == "cancelamento"))
        .all()
    )
    confirmadas = [s for s in solicitacoes if s.confirmado_por]
    top = _quem_fez_mais(confirmadas, "confirmado_por")
    return {
        "total": len(solicitacoes),
        "confirmadas": len(confirmadas),
        "pendentes": len(solicitacoes) - len(confirmadas),
        "top": top,
    }


@router.get("/resumo-contas")
def resumo_contas(usuario: Usuario = Depends(_admin_logado), db: Session = Depends(get_db)):
    """
    Visão geral pro admin: valor vendido de cada conta (hoje/mês) +
    total da rede, evolução dos últimos 14 dias somando todas as
    contas, e os setores Chamados/Cancelamentos (total x resolvido x
    pendente + quem fez mais). Resumo de propósito (sem margem/lucro
    por conta) -- o detalhe fica no painel de custo/venda de cada uma.
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
    # Ranking: quem mais vendeu no mês primeiro.
    resultado.sort(key=lambda c: c["vendido_mes"], reverse=True)

    # Evolução: venda bruta por dia (TODAS as contas somadas), últimos 14 dias.
    evolucao = []
    for i in range(13, -1, -1):
        dia_br = (datetime.now(_FUSO_BR) - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        inicio = dia_br.astimezone(timezone.utc).replace(tzinfo=None)
        fim = (dia_br + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)
        total_dia = (
            _somente_vendas_validas(db.query(Venda).filter(Venda.data_venda >= inicio, Venda.data_venda < fim))
            .with_entities(Venda.venda_bruta)
            .all()
        )
        evolucao.append({"data": dia_br.date().isoformat(), "venda_bruta": round(sum(v[0] for v in total_dia), 2)})

    return {
        "contas": resultado,
        "total_geral": {
            "vendido_hoje": round(total_hoje, 2),
            "vendido_mes": round(total_mes, 2),
            "quantidade_contas": len(resultado),
        },
        "evolucao_14_dias": evolucao,
        "setor_chamados": {"hoje": _setor_chamados(db, inicio_dia_utc), "mes": _setor_chamados(db, inicio_mes_utc)},
        "setor_cancelamentos": {
            "hoje": _setor_cancelamentos(db, inicio_dia_utc),
            "mes": _setor_cancelamentos(db, inicio_mes_utc),
        },
    }
