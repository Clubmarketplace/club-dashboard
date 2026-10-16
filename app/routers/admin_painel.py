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
from app.models import (
    Conta,
    EstoqueSku,
    EventoAdesaoCmx,
    MensagemPosVenda,
    Pergunta,
    ProdutoCadastro,
    SolicitacaoCancelamento,
    Usuario,
    Venda,
)
from app.routers.vendas import (
    _comparativo_mensal_6m,
    _evolucao_3_meses,
    _evolucao_dias_com_anterior,
    _somente_vendas_validas,
    _FUSO_BR,
)

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


def _ranking_atendimento(linhas: list, campo_autor: str) -> list[dict]:
    """Agrupa uma lista de linhas (perguntas/mensagens/solicitações já
    filtradas) pelo autor e devolve TODO MUNDO que atendeu, do maior pro
    menor número de atendimentos -- usado nos cards de setor ("Chamados",
    "Cancelamentos") pro botão "Ver ranking completo". Lista vazia se
    ninguém tiver atendido."""
    contagem: dict[str, int] = {}
    for linha in linhas:
        autor = getattr(linha, campo_autor, None)
        if not autor:
            continue
        contagem[autor] = contagem.get(autor, 0) + 1
    ranking = [{"nome": nome, "quantidade": qtd} for nome, qtd in contagem.items()]
    ranking.sort(key=lambda item: item["quantidade"], reverse=True)
    return ranking


def _quem_fez_mais(linhas: list, campo_autor: str) -> dict | None:
    """Primeiro do ranking (quem respondeu/confirmou mais) -- None se
    ninguém tiver atendido ainda."""
    ranking = _ranking_atendimento(linhas, campo_autor)
    return ranking[0] if ranking else None


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
    ranking = _ranking_atendimento(respondidas_perguntas, "respondida_por")
    top = ranking[0] if ranking else None

    return {
        "total": total,
        "respondidas": respondidas,
        "pendentes": total - respondidas,
        "top": top,
        "ranking": ranking,
    }


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
    ranking = _ranking_atendimento(confirmadas, "confirmado_por")
    top = ranking[0] if ranking else None
    return {
        "total": len(solicitacoes),
        "confirmadas": len(confirmadas),
        "pendentes": len(solicitacoes) - len(confirmadas),
        "top": top,
        "ranking": ranking,
    }


def _setor_cadastro(db: Session, inicio_utc: datetime) -> dict:
    """Produtos novos (ProdutoCadastro) que chegaram no período -- total,
    quantos já têm cadastro completo ("feitos" -- os 3 marketplaces
    resolvidos) e quem mais cadastrou. Mesmo padrão de
    _setor_cancelamentos, usando `cadastrado_por` como o "confirmado_por"
    de lá."""
    produtos = (
        db.query(ProdutoCadastro)
        .filter(ProdutoCadastro.recebido_em >= inicio_utc)
        .filter(ProdutoCadastro.status != "excluida")
        .all()
    )
    feitos = [p for p in produtos if p.status == "cadastrado"]
    ranking = _ranking_atendimento(feitos, "cadastrado_por")
    top = ranking[0] if ranking else None
    empresas = {p.empresa_chave for p in produtos if p.empresa_chave}
    return {
        "total": len(produtos),
        "feitos": len(feitos),
        "pendentes": len(produtos) - len(feitos),
        "empresas": len(empresas),
        "top": top,
        "ranking": ranking,
    }


def _cadastro_backlog_geral(db: Session) -> dict:
    """Fila pendente de cadastro AGORA, sem recorte de período -- é o
    "o que falta" de verdade (o que chegou há mais de hoje/mês também
    conta), junto com o custo total parado nesses produtos (ainda não
    estão à venda em lugar nenhum)."""
    pendentes = db.query(ProdutoCadastro).filter(ProdutoCadastro.status == "pendente").all()
    valor_parado = sum((p.custo_unitario or 0) * (p.quantidade or 0) for p in pendentes)
    return {"pendentes": len(pendentes), "valor_parado": round(valor_parado, 2)}


def _setor_subida_notas(db: Session, inicio_utc: datetime) -> dict:
    """Notas fiscais que o Operador Logístico lançou no período (chegada
    = ProdutoCadastro.data_nota) -- quantas notas distintas, quantos
    produtos vieram nelas e o valor total (custo_unitário x quantidade),
    com as 3 maiores notas do período. "Notas com erro/pendência" não
    tem rastreamento próprio ainda, por isso não entra aqui -- não quis
    inventar número."""
    produtos = (
        db.query(ProdutoCadastro)
        .filter(ProdutoCadastro.data_nota.isnot(None))
        .filter(ProdutoCadastro.data_nota >= inicio_utc)
        .all()
    )
    notas: dict[str, dict] = {}
    for p in produtos:
        nota = notas.setdefault(
            p.nota_id,
            {"nota_id": p.nota_id, "numero_nota": p.numero_nota, "empresa": p.empresa_nome, "valor": 0.0, "produtos": 0},
        )
        nota["valor"] += (p.custo_unitario or 0) * (p.quantidade or 0)
        nota["produtos"] += 1
    lista_notas = sorted(notas.values(), key=lambda n: n["valor"], reverse=True)
    for n in lista_notas:
        n["valor"] = round(n["valor"], 2)
    return {
        "notas": len(notas),
        "produtos": len(produtos),
        "valor_total": round(sum(n["valor"] for n in lista_notas), 2),
        "top_notas": lista_notas[:3],
    }


def _contas_conectadas_extra(db: Session, contas: list) -> dict:
    """Informações extras do card "Contas conectadas": quantas contas
    ativas estão sem nenhuma venda há 7+ dias (sinal de alerta -- conta
    parada)."""
    limite_7d = datetime.utcnow() - timedelta(days=7)
    sem_venda_recente = 0
    for conta in contas:
        ultima = (
            db.query(Venda.data_venda)
            .filter(Venda.conta_id == conta.id)
            .order_by(Venda.data_venda.desc())
            .first()
        )
        if not ultima or ultima[0] < limite_7d:
            sem_venda_recente += 1

    return {"sem_venda_7d": sem_venda_recente}


def _financeiro_por_setor(db: Session, inicio_dia_utc: datetime, inicio_mes_utc: datetime) -> dict:
    """Só os números que dá pra calcular com dado real hoje -- nada
    estimado ou inventado num relatório que vai pro financeiro/diretor.

    "Valor cancelado" (setor Cancelamento) de propósito NÃO entra aqui:
    SolicitacaoCancelamento grava o número do pedido, não o valor da
    venda, e cruzar esse número com `Venda` não é confiável o bastante
    pra virar um número financeiro (pedido com vários itens, formato de
    número diferente por plataforma). Precisa de um campo de valor
    próprio nessa tabela pra isso funcionar direito."""
    backlog = _cadastro_backlog_geral(db)

    produtos_notas_mes = (
        db.query(ProdutoCadastro)
        .filter(ProdutoCadastro.data_nota.isnot(None))
        .filter(ProdutoCadastro.data_nota >= inicio_mes_utc)
        .all()
    )
    valor_notas_mes = sum((p.custo_unitario or 0) * (p.quantidade or 0) for p in produtos_notas_mes)

    def _lucro_adesoes(inicio_utc: datetime) -> float:
        linhas = (
            db.query(EventoAdesaoCmx.lucro_liquido)
            .filter(EventoAdesaoCmx.sucesso.is_(True), EventoAdesaoCmx.data_adesao >= inicio_utc)
            .all()
        )
        return round(sum(l[0] or 0 for l in linhas), 2)

    return {
        "cadastro": {"custo_parado_pendente": backlog["valor_parado"]},
        "subida_notas": {"valor_notas_mes": round(valor_notas_mes, 2)},
        "promocoes": {"lucro_hoje": _lucro_adesoes(inicio_dia_utc), "lucro_mes": _lucro_adesoes(inicio_mes_utc)},
        "cancelamento_disponivel": False,
    }


@router.get("/resumo-contas")
def resumo_contas(usuario: Usuario = Depends(_admin_logado), db: Session = Depends(get_db)):
    """
    Visão geral pro admin: valor vendido de cada conta (hoje/mês) +
    total da rede, evolução (15 dias / 30 dias / 3 meses) somando todas
    as contas, e os setores Chamados/Cancelamentos (total x resolvido x
    pendente + ranking completo de quem mais atendeu). Resumo de
    propósito (sem margem/lucro por conta) -- o detalhe fica no painel
    de custo/venda de cada uma.

    Os números vêm só do que já está no banco -- a sincronização com o
    Mercado Livre acontece à parte, num ciclo contínuo em segundo plano
    (ver app/reforco_vendas.py), não disparada por essa rota. Isso evita
    empilhar sincronizações se o admin atualizar a página várias vezes
    seguidas.
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

    # Anúncios cadastrados -- soma de TODAS as contas, mesmo critério do
    # dashboard do seller (app/routers/dashboard_painel.py): ativos (com
    # estoque), sem estoque, pausados.
    conta_ids = [c.id for c in contas]
    todos_anuncios = (
        db.query(EstoqueSku).filter(EstoqueSku.conta_id.in_(conta_ids)).all() if conta_ids else []
    )
    anuncios_ativos_total = sum(1 for a in todos_anuncios if a.ativo)
    anuncios_sem_estoque_total = sum(1 for a in todos_anuncios if a.ativo and a.quantidade <= 0)
    anuncios_pausados_total = sum(1 for a in todos_anuncios if not a.ativo)

    # Promoções aderidas AUTOMATICAMENTE pelo Precificador -- soma de
    # TODAS as contas, mesma contagem real de eventos (EventoAdesaoCmx)
    # usada no dashboard do seller, não um retrato de "quantos têm custo
    # cadastrado agora".
    def _promocoes_aderidas(inicio_utc: datetime) -> int:
        return (
            db.query(EventoAdesaoCmx)
            .filter(EventoAdesaoCmx.sucesso.is_(True), EventoAdesaoCmx.data_adesao >= inicio_utc)
            .count()
        )

    # Evolução: venda bruta por dia (TODAS as contas somadas) -- 15 e 30
    # dias (cada um com o período anterior pra comparação) + 3 meses,
    # pro filtro da tela trocar sem precisar buscar de novo.
    evolucao_15_dias = _evolucao_dias_com_anterior(db, 15, None)
    evolucao_30_dias = _evolucao_dias_com_anterior(db, 30, None)

    return {
        "contas": resultado,
        "total_geral": {
            "vendido_hoje": round(total_hoje, 2),
            "vendido_mes": round(total_mes, 2),
            "quantidade_contas": len(resultado),
            "anuncios_ativos": anuncios_ativos_total,
            "anuncios_sem_estoque": anuncios_sem_estoque_total,
            "anuncios_pausados": anuncios_pausados_total,
        },
        "promocoes_aderidas": {
            "hoje": _promocoes_aderidas(inicio_dia_utc),
            "mes": _promocoes_aderidas(inicio_mes_utc),
        },
        "comparativo_mensal_6m": _comparativo_mensal_6m(db, None),
        "evolucao_15_dias": evolucao_15_dias,
        "evolucao_30_dias": evolucao_30_dias,
        "evolucao_3_meses": _evolucao_3_meses(db, None),
        "setor_chamados": {"hoje": _setor_chamados(db, inicio_dia_utc), "mes": _setor_chamados(db, inicio_mes_utc)},
        "setor_cancelamentos": {
            "hoje": _setor_cancelamentos(db, inicio_dia_utc),
            "mes": _setor_cancelamentos(db, inicio_mes_utc),
        },
        "setor_cadastro": {
            "hoje": _setor_cadastro(db, inicio_dia_utc),
            "mes": _setor_cadastro(db, inicio_mes_utc),
            "backlog": _cadastro_backlog_geral(db),
        },
        "setor_subida_notas": {
            "hoje": _setor_subida_notas(db, inicio_dia_utc),
            "mes": _setor_subida_notas(db, inicio_mes_utc),
        },
        "contas_conectadas_extra": _contas_conectadas_extra(db, contas),
        "financeiro_por_setor": _financeiro_por_setor(db, inicio_dia_utc, inicio_mes_utc),
    }
