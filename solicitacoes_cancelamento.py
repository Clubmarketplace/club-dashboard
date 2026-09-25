"""
Endpoints das SOLICITAÇÕES MANUAIS de cancelamento -- diferente da fila
de IA em routers/cancelamentos.py (que detecta pedido de cancelamento
dentro de mensagens de clientes). Aqui alguém registra manualmente
"preciso cancelar essa venda" (CEP errado, sem estoque, etiqueta não
gerada...).

Quem pode registrar (a ORIGEM é decidida pelo servidor, pela sessão --
nunca pelo que vem do formulário):
  - "seller":    seller logado; a conta é SEMPRE a vinculada a ele.
  - "logistica": operador de galpão logado; escolhe a conta e o galpão.
  - "publico":   link sem login (continua funcionando como antes).

A data é sempre gerada pelo servidor (datetime.utcnow), nunca vem do
formulário. Um envio pode ter VÁRIAS vendas da mesma conta/plataforma;
cada uma vira uma linha própria (tudo ou nada).

Nome de conta: tudo que compara/agrupa/filtra usa a conta_chave (ver
app/contas_util.py), pra "Friaça", "friaca" e "FRIAÇA" serem a mesma.
"""
import re
import io
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from pydantic import BaseModel, field_validator
from sqlalchemy import and_, case, or_, update
from sqlalchemy.orm import Session

from app import auth
from app.cancelamento_apoio import preencher_produtos_em_segundo_plano, registrar_evento
from app.contas_util import chave_conta, limpar_espacos, nome_exibicao_novo, sufixo_de_plataforma
from app.database import get_db
from app.models import Conta, SolicitacaoCancelamento, SolicitacaoEvento, Usuario

router = APIRouter(prefix="/api/solicitacoes-cancelamento", tags=["solicitacoes-cancelamento"])

# --- Opções (fonte ÚNICA: formulário, filtros e telas leem daqui) ---
# Pra incluir/renomear: é só mexer nestes dicionários. A chave é o que
# fica gravado no banco -- NUNCA mudar uma chave existente, só o nome.
PLATAFORMAS = {
    "mercado_livre": "Mercado Livre",
    "shopee": "Shopee",
    "magalu": "Magalu",
    "tiktok_shop": "TikTok Shop",
}
GALPOES = {
    1: "Galpão 1",
    2: "Galpão 2",
    3: "Galpão 3",
}
PLATAFORMAS_VALIDAS = set(PLATAFORMAS)
GALPOES_VALIDOS = set(GALPOES)
ORIGENS_VALIDAS = {"seller", "logistica", "publico"}

# Horário de Brasília pros filtros de período. O Brasil não tem horário
# de verão desde 2019, então UTC-3 fixo é exato e não depende de tzdata.
FUSO_BR = timezone(timedelta(hours=-3))

# Papéis que podem CONFIRMAR (quem pede não confirma: controle cruzado).
PAPEIS_QUE_CONFIRMAM = ("admin", "supervisor", "atendente")

# "Em atendimento" NÃO expira: o pedido fica com quem assumiu até confirmar
# ou liberar. Se outra pessoa precisar, ela assume no lugar (e o histórico
# guarda quanto tempo ficou com cada um). Decidido com a equipe.
TAMANHO_MAX_PROTOCOLO = 80

RESULTADOS_IMPACTO_VALIDOS = {"sem_impacto", "com_impacto", "aguardando_confirmacao"}
LABEL_RESULTADO_IMPACTO = {
    "sem_impacto": "Sem impacto",
    "com_impacto": "Com impacto",
    "aguardando_confirmacao": "Aguardando confirmação do ML",
}


# ---------------------------------------------------------------------------
# Modelos de entrada
# ---------------------------------------------------------------------------
class ItemCancelamento(BaseModel):
    numero_venda: str
    motivo: str
    # Opcional, pras plataformas ainda não integradas. No Mercado Livre o
    # SKU é lido do próprio pedido (cancelamento_apoio.py) e este campo é ignorado.
    sku: Optional[str] = None

    @field_validator("sku")
    @classmethod
    def limpar_sku(cls, valor: Optional[str]) -> Optional[str]:
        valor = limpar_espacos(valor or "")
        return valor[:60] or None

    @field_validator("numero_venda", "motivo")
    @classmethod
    def validar_nao_vazio(cls, valor: str) -> str:
        valor = valor.strip()
        if not valor:
            raise ValueError("Campo obrigatório não pode ficar em branco.")
        return valor


class NovaSolicitacaoLote(BaseModel):
    plataforma: str
    conta: str
    itens: list[ItemCancelamento]
    galpao: Optional[int] = None  # obrigatório só pra logística
    # Quando o formulário já avisou "essa venda já foi solicitada" e a
    # pessoa decidiu enviar mesmo assim.
    confirmar_duplicadas: bool = False

    @field_validator("plataforma")
    @classmethod
    def validar_plataforma(cls, valor: str) -> str:
        if valor not in PLATAFORMAS_VALIDAS:
            raise ValueError(f"Plataforma inválida. Use uma de: {', '.join(PLATAFORMAS_VALIDAS)}")
        return valor

    @field_validator("conta")
    @classmethod
    def validar_conta(cls, valor: str) -> str:
        valor = limpar_espacos(valor)
        if not valor:
            raise ValueError("Campo obrigatório não pode ficar em branco.")
        return valor

    @field_validator("itens")
    @classmethod
    def validar_ao_menos_um_item(cls, valor: list) -> list:
        if not valor:
            raise ValueError("Informe pelo menos uma venda.")
        return valor


# ---------------------------------------------------------------------------
# Auxiliares
# ---------------------------------------------------------------------------
def _normalizar_numero_venda(numero: str, plataforma: str, estrito: bool) -> str:
    """
    Tira espaços/pontos/traços. No modo estrito (logística): ML só aceita
    números; Shopee, Magalu e TikTok Shop aceitam letras e números, sempre
    em maiúsculo (formato dos pedidos não confirmado pra todas). Fora do
    modo estrito (seller/link público) só limpa espaços -- mantém o
    comportamento que já existia.
    """
    if not estrito:
        return re.sub(r"\s+", "", numero)
    limpo = re.sub(r"[\s.\-/]", "", numero).upper()
    if plataforma == "mercado_livre" and not limpo.isdigit():
        raise HTTPException(status_code=400, detail=f"Nº da venda do Mercado Livre deve ter só números: \"{numero}\".")
    if plataforma != "mercado_livre" and not limpo.isalnum():
        nome = PLATAFORMAS.get(plataforma, plataforma)
        raise HTTPException(status_code=400, detail=f"Nº da venda da {nome} deve ter só letras e números: \"{numero}\".")
    return limpo


def _mapa_contas_conhecidas(db: Session) -> dict[str, str]:
    """
    {chave: nome oficial} de todas as contas que o sistema conhece. Ordem
    de preferência do nome: contas conectadas do ML > conta vinculada dos
    sellers > nome usado em solicitações (o mais antigo, que costuma ser
    o original). Cobre também contas da Shopee (ainda não integradas).
    """
    mapa: dict[str, str] = {}

    def registrar(nome: Optional[str]) -> None:
        if nome and limpar_espacos(nome):
            mapa.setdefault(chave_conta(nome), limpar_espacos(nome))

    contas = db.query(Conta).all()
    ativas = [c for c in contas if c.inativa_em is None]
    # Conectadas primeiro: com nomes repetidos ("Velasco"/"VELASCO") vale o
    # nome da conta que está em uso.
    for c in sorted(ativas, key=lambda c: not c.access_token):
        registrar(c.apelido)
    for (vinculada,) in db.query(Usuario.conta_vinculada).filter(Usuario.papel == "seller").all():
        registrar(vinculada)
    for (nome,) in db.query(SolicitacaoCancelamento.conta).order_by(SolicitacaoCancelamento.criado_em.asc()).all():
        registrar(nome)

    # Contas que saíram do Club somem das listas/filtros (o histórico continua
    # aparecendo com o nome gravado na própria solicitação). Só sai se não
    # houver outra conta ATIVA com a mesma chave.
    chaves_ativas = {chave_conta(c.apelido) for c in ativas}
    for c in contas:
        if c.inativa_em is not None and chave_conta(c.apelido) not in chaves_ativas:
            mapa.pop(chave_conta(c.apelido), None)
    return mapa


def _serializar(s: SolicitacaoCancelamento, mapa_contas: Optional[dict] = None) -> dict:
    chave = s.conta_chave or chave_conta(s.conta)
    return {
        "id": s.id,
        "plataforma": s.plataforma,
        "conta": s.conta,
        "conta_chave": chave,
        # Nome oficial da conta (registros antigos podem ter sido digitados
        # como "friaca"; na tela aparece "Friaça").
        "conta_nome": (mapa_contas or {}).get(chave, s.conta),
        "numero_venda": s.numero_venda,
        "motivo": s.motivo,
        "criado_em": s.criado_em.isoformat() if s.criado_em else None,
        "confirmado_por": s.confirmado_por,
        "confirmado_em": s.confirmado_em.isoformat() if s.confirmado_em else None,
        "origem": s.origem,
        "solicitado_por": s.solicitado_por,
        "galpao": s.galpao,
        "galpao_nome": GALPOES.get(s.galpao, f"Galpão {s.galpao}") if s.galpao else None,
        "plataforma_nome": PLATAFORMAS.get(s.plataforma, s.plataforma),
        "resultado_impacto": s.resultado_impacto,
        "protocolo": s.protocolo,
        "sku": s.sku,
        "produto_titulo": s.produto_titulo,
        "assumido_primeiro_por": s.assumido_primeiro_por,
        "assumido_primeiro_em": s.assumido_primeiro_em.isoformat() if s.assumido_primeiro_em else None,
        **_dados_atendimento(s),
    }




def _dados_atendimento(s: SolicitacaoCancelamento) -> dict:
    """Quem está com o pedido agora -- só enquanto pendente e dentro do prazo."""
    ativo = (
        not s.confirmado_por
        and s.em_atendimento_por_id is not None
        and s.em_atendimento_desde is not None
    )
    return {
        "em_atendimento_por": s.em_atendimento_por if ativo else None,
        "em_atendimento_por_id": s.em_atendimento_por_id if ativo else None,
        "em_atendimento_desde": s.em_atendimento_desde.isoformat() if ativo else None,
    }


def _inicio_do_dia_br_em_utc(data_br: datetime) -> datetime:
    """Meia-noite de Brasília daquele dia, convertida pra UTC 'cru' (como no banco)."""
    meia_noite = data_br.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=FUSO_BR)
    return meia_noite.astimezone(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Registrar
# ---------------------------------------------------------------------------
@router.post("")
def criar_solicitacoes(corpo: NovaSolicitacaoLote, request: Request, tarefas: BackgroundTasks, db: Session = Depends(get_db)):
    """
    Registra um ou mais pedidos de uma vez, todos da mesma conta e
    plataforma (tudo ou nada). Rota pública (o link sem login continua
    valendo), mas se houver alguém logado a origem/nome vêm da sessão.
    """
    usuario = auth.usuario_atual(request, db)
    papel = usuario.papel if usuario else None
    mapa_contas = _mapa_contas_conhecidas(db)

    # --- Origem, conta e galpão decididos pelo servidor ---
    galpao = None
    if papel == "seller" and usuario.conta_vinculada:
        origem = "seller"
        conta_digitada = usuario.conta_vinculada  # seller nunca pede por outra conta
    elif papel == "logistica":
        origem = "logistica"
        conta_digitada = corpo.conta
        if corpo.galpao not in GALPOES_VALIDOS:
            raise HTTPException(status_code=400, detail="Escolha o galpão.")
        galpao = corpo.galpao
        sufixo = sufixo_de_plataforma(conta_digitada)
        if sufixo:
            raise HTTPException(
                status_code=400,
                detail=f"Use só o nome da loja, sem \"{sufixo.upper()}\" -- a plataforma já é escolhida nos botões.",
            )
    else:
        origem = "publico"
        conta_digitada = corpo.conta

    chave = chave_conta(conta_digitada)
    # Conta conhecida -> sempre o nome oficial; conta nova -> nome padronizado.
    nome_conta = mapa_contas.get(chave) or nome_exibicao_novo(conta_digitada)

    estrito = origem == "logistica"
    numeros = [_normalizar_numero_venda(i.numero_venda, corpo.plataforma, estrito) for i in corpo.itens]

    # --- Aviso de duplicidade (mesma venda + mesma plataforma) ---
    if not corpo.confirmar_duplicadas:
        existentes = (
            db.query(SolicitacaoCancelamento)
            .filter(SolicitacaoCancelamento.plataforma == corpo.plataforma)
            .filter(SolicitacaoCancelamento.numero_venda.in_(numeros))
            .order_by(SolicitacaoCancelamento.criado_em.asc())
            .all()
        )
        if existentes:
            raise HTTPException(
                status_code=409,
                detail={
                    "codigo": "duplicada",
                    "mensagem": "Já existe solicitação para essa(s) venda(s).",
                    "duplicadas": [
                        {
                            "numero_venda": s.numero_venda,
                            "conta": s.conta,
                            "criado_em": s.criado_em.isoformat() if s.criado_em else None,
                            "solicitado_por": s.solicitado_por,
                            "galpao": s.galpao,
                            "confirmado_por": s.confirmado_por,
                        }
                        for s in existentes
                    ],
                },
            )

    try:
        criadas = []
        for item, numero in zip(corpo.itens, numeros):
            solicitacao = SolicitacaoCancelamento(
                plataforma=corpo.plataforma,
                conta=nome_conta,
                conta_chave=chave,
                numero_venda=numero,
                motivo=item.motivo,
                origem=origem,
                solicitado_por=usuario.nome_exibicao if usuario else None,
                galpao=galpao,
                sku=(item.sku if corpo.plataforma != "mercado_livre" else None),
            )
            db.add(solicitacao)
            criadas.append(solicitacao)
        db.flush()  # gera os ids, pra registrar o histórico na mesma transação
        rotulo_origem = {"seller": "Seller", "logistica": GALPOES.get(galpao, "Galpão"), "publico": "Link público"}.get(origem, origem)
        for s in criadas:
            registrar_evento(db, s.id, "registrou", usuario=usuario, nome=None if usuario else "Link público",
                             detalhe=f"{rotulo_origem} · motivo: {s.motivo}")
        db.commit()
        for s in criadas:
            db.refresh(s)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="Não foi possível registrar a solicitação. Tente de novo.") from exc

    # Mercado Livre: lê o SKU/produto no pedido DEPOIS de responder, pra o
    # formulário não ficar esperando o ML.
    ids_ml = [s.id for s in criadas if s.plataforma == "mercado_livre"]
    if ids_ml:
        tarefas.add_task(preencher_produtos_em_segundo_plano, ids_ml)

    return {
        "status": "registrado",
        "quantidade": len(criadas),
        "conta": nome_conta,
        "origem": origem,
        "galpao": galpao,
        "galpao_nome": GALPOES.get(galpao) if galpao else None,
        "solicitado_por": usuario.nome_exibicao if usuario else None,
        "itens": [{"id": s.id, "numero_venda": s.numero_venda, "criado_em": s.criado_em.isoformat()} for s in criadas],
    }


# ---------------------------------------------------------------------------
# Consultar
# ---------------------------------------------------------------------------
@router.get("")
def listar_solicitacoes(db: Session = Depends(get_db), limite: int = 500):
    """Lista simples dos pedidos mais recentes (mantida por compatibilidade)."""
    solicitacoes = (
        db.query(SolicitacaoCancelamento)
        .order_by(SolicitacaoCancelamento.criado_em.desc())
        .limit(min(max(limite, 1), 2000))
        .all()
    )
    return [_serializar(s) for s in solicitacoes]


@router.get("/opcoes")
def listar_opcoes():
    """Plataformas e galpões disponíveis (pro formulário e pros filtros)."""
    return {
        "plataformas": [{"valor": k, "nome": v} for k, v in PLATAFORMAS.items()],
        "galpoes": [{"valor": k, "nome": v} for k, v in GALPOES.items()],
    }


@router.get("/contas")
def listar_contas(db: Session = Depends(get_db)):
    """Lista de contas conhecidas pro campo "Conta" (com busca) e pros filtros."""
    mapa = _mapa_contas_conhecidas(db)
    return sorted(({"chave": k, "nome": v} for k, v in mapa.items()), key=lambda c: c["chave"])


@router.get("/busca")
def buscar_solicitacoes(
    request: Request,
    db: Session = Depends(get_db),
    status: str = "pendente",          # pendente | confirmado | todos
    galpao: Optional[int] = None,
    conta: Optional[str] = None,       # nome ou chave -- comparado pela chave
    plataforma: Optional[str] = None,
    origem: Optional[str] = None,      # seller | logistica | publico
    dias: Optional[int] = 30,          # atalho de período (ignorado se de/ate)
    de: Optional[str] = None,          # AAAA-MM-DD (horário de Brasília)
    ate: Optional[str] = None,
    busca: Optional[str] = None,       # nº da venda ou conta -- ignora o período
    atendimento: Optional[str] = None, # livres | em_atendimento | meus (só pedidos pendentes)
    ordem: str = "recentes",           # recentes (mais novos primeiro) | espera (mais antigos primeiro -- a fila)
    pagina: int = 1,
    por_pagina: int = 100,
):
    """
    Busca com filtros, feita no servidor e paginada. Devolve também os
    contadores (total/pendentes/confirmados) com os mesmos filtros,
    exceto o de status, pros botões mostrarem "Aguardando (3)" etc.
    Logística só enxerga solicitações feitas pelos galpões.
    """
    usuario = auth.usuario_atual(request, db)
    if usuario is None:
        raise HTTPException(status_code=401, detail="Sessão expirada -- faça login de novo.")
    if usuario.papel == "logistica":
        origem = "logistica"

    consulta = db.query(SolicitacaoCancelamento)

    if galpao in GALPOES_VALIDOS:
        consulta = consulta.filter(SolicitacaoCancelamento.galpao == galpao)
    if conta:
        consulta = consulta.filter(SolicitacaoCancelamento.conta_chave == chave_conta(conta))
    if plataforma in PLATAFORMAS_VALIDAS:
        consulta = consulta.filter(SolicitacaoCancelamento.plataforma == plataforma)
    if origem == "publico":
        # Registros antigos (origem nula) entram junto com o link público.
        consulta = consulta.filter(or_(SolicitacaoCancelamento.origem == "publico", SolicitacaoCancelamento.origem.is_(None)))
    elif origem in ORIGENS_VALIDAS:
        consulta = consulta.filter(SolicitacaoCancelamento.origem == origem)

    termo = limpar_espacos(busca or "")
    if termo:
        # Busca geral: em TODO o histórico (ignora o período).
        termo_venda = re.sub(r"\s+", "", termo)
        consulta = consulta.filter(
            or_(
                SolicitacaoCancelamento.numero_venda.ilike(f"%{termo_venda}%"),
                SolicitacaoCancelamento.conta_chave.contains(chave_conta(termo)),
            )
        )
    else:
        agora_br = datetime.now(FUSO_BR)
        try:
            if de or ate:
                if de:
                    inicio = _inicio_do_dia_br_em_utc(datetime.strptime(de, "%Y-%m-%d"))
                    consulta = consulta.filter(SolicitacaoCancelamento.criado_em >= inicio)
                if ate:
                    fim = _inicio_do_dia_br_em_utc(datetime.strptime(ate, "%Y-%m-%d") + timedelta(days=1))
                    consulta = consulta.filter(SolicitacaoCancelamento.criado_em < fim)
            elif dias and dias > 0:
                inicio = _inicio_do_dia_br_em_utc(agora_br - timedelta(days=dias - 1))
                consulta = consulta.filter(SolicitacaoCancelamento.criado_em >= inicio)
        except ValueError:
            raise HTTPException(status_code=400, detail="Data inválida -- use o formato AAAA-MM-DD.")

    # Contadores de status com todos os filtros, menos status e atendimento
    # (cada aba mostra o seu número, independente da aba aberta).
    total = consulta.count()
    confirmados = consulta.filter(SolicitacaoCancelamento.confirmado_por.isnot(None)).count()

    # Contadores do "em atendimento" (entre os pendentes, com os filtros acima).
    ativo = and_(SolicitacaoCancelamento.em_atendimento_por_id.isnot(None), SolicitacaoCancelamento.em_atendimento_desde.isnot(None))
    pendentes_q = consulta.filter(SolicitacaoCancelamento.confirmado_por.is_(None))
    contadores_atendimento = {
        "livres": pendentes_q.filter(~ativo).count(),
        "em_atendimento": pendentes_q.filter(ativo).count(),
        "meus": pendentes_q.filter(ativo, SolicitacaoCancelamento.em_atendimento_por_id == usuario.id).count(),
    }
    if atendimento == "livres":
        consulta = consulta.filter(SolicitacaoCancelamento.confirmado_por.is_(None), ~ativo)
    elif atendimento == "em_atendimento":
        consulta = consulta.filter(SolicitacaoCancelamento.confirmado_por.is_(None), ativo)
    elif atendimento == "meus":
        consulta = consulta.filter(SolicitacaoCancelamento.confirmado_por.is_(None), ativo, SolicitacaoCancelamento.em_atendimento_por_id == usuario.id)

    if status == "pendente":
        consulta = consulta.filter(SolicitacaoCancelamento.confirmado_por.is_(None))
    elif status == "confirmado":
        consulta = consulta.filter(SolicitacaoCancelamento.confirmado_por.isnot(None))

    por_pagina = min(max(por_pagina, 1), 500)
    pagina = max(pagina, 1)
    total_filtrado = consulta.count()
    if ordem == "espera":
        # Fila: PENDENTES primeiro (quem espera há mais tempo no topo); depois os
        # CONFIRMADOS (confirmação mais recente primeiro). Dentro de cada grupo a
        # 2ª chave é toda preenchida (pendentes) ou toda vazia (confirmados), então
        # a ordem vale igual no Postgres e no SQLite.
        pendente = SolicitacaoCancelamento.confirmado_por.is_(None)
        ordenacao = (
            case((pendente, 0), else_=1),
            case((pendente, SolicitacaoCancelamento.criado_em), else_=None).asc(),
            SolicitacaoCancelamento.confirmado_em.desc(),
        )
    else:
        ordenacao = (SolicitacaoCancelamento.criado_em.desc(),)
    itens = (
        consulta.order_by(*ordenacao)
        .offset((pagina - 1) * por_pagina)
        .limit(por_pagina)
        .all()
    )

    mapa_contas = _mapa_contas_conhecidas(db)
    return {
        "itens": [_serializar(s, mapa_contas) for s in itens],
        "contadores": {"total": total, "confirmados": confirmados, "pendentes": total - confirmados},
        "total_filtrado": total_filtrado,
        "pagina": pagina,
        "por_pagina": por_pagina,
        "tem_mais": pagina * por_pagina < total_filtrado,
        "buscando_todo_historico": bool(termo),
        "atendimento": contadores_atendimento,
        "eu": {"id": usuario.id, "nome": usuario.nome_exibicao},
    }


# ---------------------------------------------------------------------------
# Confirmar
# ---------------------------------------------------------------------------
class ConfirmarSolicitacaoBody(BaseModel):
    resultado_impacto: str
    # Obrigatório, a não ser que sem_protocolo=True (cancelado direto no
    # painel do ML, sem atendimento -- aí não existe protocolo).
    protocolo: Optional[str] = None
    sem_protocolo: bool = False

    @field_validator("protocolo")
    @classmethod
    def limpar_protocolo(cls, valor: Optional[str]) -> Optional[str]:
        if valor is None:
            return None
        valor = limpar_espacos(valor)
        if len(valor) > TAMANHO_MAX_PROTOCOLO:
            raise ValueError(f"Protocolo muito longo (máx. {TAMANHO_MAX_PROTOCOLO} caracteres).")
        return valor

    @field_validator("resultado_impacto")
    @classmethod
    def validar_resultado(cls, valor: str) -> str:
        if valor not in RESULTADOS_IMPACTO_VALIDOS:
            raise ValueError(f"resultado_impacto inválido. Use um de: {', '.join(RESULTADOS_IMPACTO_VALIDOS)}")
        return valor


@router.post("/{solicitacao_id}/confirmar")
def confirmar_solicitacao(solicitacao_id: int, corpo: ConfirmarSolicitacaoBody, request: Request, db: Session = Depends(get_db)):
    """
    Marca um pedido como já cancelado de verdade na plataforma. Quem
    confirmou é sempre a pessoa LOGADA no momento do clique (pego da
    sessão, nunca digitado). Seller e logística não confirmam -- quem
    pede não é quem confirma.

    corpo.resultado_impacto é OBRIGATÓRIO e sempre preenchido à mão:
    o Mercado Livre não devolve essa informação por nenhuma API
    pública -- só confirma se pesou ou não na reputação através do
    atendimento deles (chat/WhatsApp, veja os botões de atalho na tela).
    """
    usuario_logado = auth.usuario_atual(request, db)
    if usuario_logado is None:
        raise HTTPException(status_code=401, detail="Sessão expirada -- faça login de novo.")
    if usuario_logado.papel not in PAPEIS_QUE_CONFIRMAM:
        raise HTTPException(status_code=403, detail="Seu perfil não pode confirmar cancelamentos.")

    solicitacao = db.query(SolicitacaoCancelamento).filter(SolicitacaoCancelamento.id == solicitacao_id).first()
    if solicitacao is None:
        raise HTTPException(status_code=404, detail="Solicitação não encontrada")
    if solicitacao.confirmado_por:
        raise HTTPException(status_code=400, detail="Essa solicitação já foi confirmada antes.")
    if not corpo.sem_protocolo and not corpo.protocolo:
        raise HTTPException(status_code=400, detail="Informe o número do protocolo (ou marque que foi cancelado sem protocolo).")

    # Confirmou direto, sem ter assumido: o histórico registra os dois passos.
    atual = _dados_atendimento(solicitacao)
    if atual["em_atendimento_por_id"] != usuario_logado.id:
        registrar_evento(db, solicitacao.id, "assumiu", usuario=usuario_logado,
                         detalhe=(f"no lugar de {atual['em_atendimento_por']}" if atual["em_atendimento_por"] else "ao confirmar"))
    if not solicitacao.assumido_primeiro_por:
        solicitacao.assumido_primeiro_por = usuario_logado.nome_exibicao
        solicitacao.assumido_primeiro_em = datetime.utcnow()

    solicitacao.confirmado_por = usuario_logado.nome_exibicao
    solicitacao.confirmado_em = datetime.utcnow()
    solicitacao.resultado_impacto = corpo.resultado_impacto
    # "" = confirmado como "sem protocolo"; texto = protocolo informado.
    solicitacao.protocolo = "" if corpo.sem_protocolo else corpo.protocolo
    # Confirmado: sai do "em atendimento".
    solicitacao.em_atendimento_por = None
    solicitacao.em_atendimento_por_id = None
    solicitacao.em_atendimento_desde = None
    registrar_evento(
        db, solicitacao.id, "confirmou", usuario=usuario_logado,
        detalhe=f"{'Sem protocolo' if corpo.sem_protocolo else 'Protocolo ' + corpo.protocolo} · {LABEL_RESULTADO_IMPACTO.get(corpo.resultado_impacto, corpo.resultado_impacto)}",
    )
    db.commit()
    db.refresh(solicitacao)

    return {
        "status": "confirmado",
        "id": solicitacao.id,
        "confirmado_por": solicitacao.confirmado_por,
        "confirmado_em": solicitacao.confirmado_em.isoformat(),
        "resultado_impacto": solicitacao.resultado_impacto,
        "protocolo": solicitacao.protocolo,
    }


# ---------------------------------------------------------------------------
# Em atendimento (assumir / liberar)
# ---------------------------------------------------------------------------
class AssumirBody(BaseModel):
    # True = assumir mesmo que outra pessoa esteja com o pedido (a tela
    # só manda isso depois de perguntar "Assumir no lugar de Fulano?").
    forcar: bool = False


def _operador_que_confirma(request: Request, db: Session):
    usuario = auth.usuario_atual(request, db)
    if usuario is None:
        raise HTTPException(status_code=401, detail="Sessão expirada -- faça login de novo.")
    if usuario.papel not in PAPEIS_QUE_CONFIRMAM:
        raise HTTPException(status_code=403, detail="Seu perfil não trata cancelamentos.")
    return usuario


@router.post("/{solicitacao_id}/assumir")
def assumir_atendimento(solicitacao_id: int, request: Request, corpo: Optional[AssumirBody] = None, db: Session = Depends(get_db)):
    """
    Marca que o operador logado começou a tratar o pedido. A checagem e a
    gravação acontecem num único UPDATE condicional no banco: se duas
    pessoas clicarem no mesmo instante, só uma consegue (a outra recebe
    409 com o nome de quem ficou com o pedido).
    """
    usuario = _operador_que_confirma(request, db)
    forcar = bool(corpo and corpo.forcar)
    agora = datetime.utcnow()

    # Como estava ANTES (só pra escrever o histórico; a decisão é o UPDATE abaixo).
    antes = db.query(SolicitacaoCancelamento).filter(SolicitacaoCancelamento.id == solicitacao_id).first()
    antes_por = antes.em_atendimento_por if antes else None
    antes_por_id = antes.em_atendimento_por_id if antes else None
    antes_ativo = bool(antes and _dados_atendimento(antes)["em_atendimento_por_id"])

    condicao = [
        SolicitacaoCancelamento.id == solicitacao_id,
        SolicitacaoCancelamento.confirmado_por.is_(None),
    ]
    if not forcar:
        condicao.append(or_(
            SolicitacaoCancelamento.em_atendimento_por_id.is_(None),
            SolicitacaoCancelamento.em_atendimento_por_id == usuario.id,
        ))
    resultado = db.execute(
        update(SolicitacaoCancelamento)
        .where(*condicao)
        .values(em_atendimento_por=usuario.nome_exibicao, em_atendimento_por_id=usuario.id, em_atendimento_desde=agora)
        .execution_options(synchronize_session=False)
    )
    db.commit()

    solicitacao = db.query(SolicitacaoCancelamento).filter(SolicitacaoCancelamento.id == solicitacao_id).first()
    if solicitacao is None:
        raise HTTPException(status_code=404, detail="Solicitação não encontrada")
    if resultado.rowcount == 0:
        if solicitacao.confirmado_por:
            raise HTTPException(status_code=400, detail=f"Esse pedido já foi confirmado por {solicitacao.confirmado_por}.")
        atual = _dados_atendimento(solicitacao)
        raise HTTPException(status_code=409, detail={
            "codigo": "ocupado",
            "mensagem": f"{atual['em_atendimento_por'] or 'Outra pessoa'} já está com esse pedido.",
            **atual,
        })

    # Histórico (só quando mudou de mãos -- assumir de novo o que já é meu não conta).
    if antes_por_id != usuario.id or not antes_ativo:
        if antes_ativo and antes_por_id != usuario.id:
            registrar_evento(db, solicitacao.id, "assumiu_no_lugar", usuario=usuario, detalhe=f"no lugar de {antes_por}")
        else:
            registrar_evento(db, solicitacao.id, "assumiu", usuario=usuario)
    if not solicitacao.assumido_primeiro_por:
        solicitacao.assumido_primeiro_por = usuario.nome_exibicao
        solicitacao.assumido_primeiro_em = agora
    db.commit()
    return {"status": "assumido", **_dados_atendimento(solicitacao)}


@router.post("/{solicitacao_id}/liberar")
def liberar_atendimento(solicitacao_id: int, request: Request, db: Session = Depends(get_db)):
    """Devolve o pedido pra fila. Só quem está com ele (ou admin/supervisor)."""
    usuario = _operador_que_confirma(request, db)
    solicitacao = db.query(SolicitacaoCancelamento).filter(SolicitacaoCancelamento.id == solicitacao_id).first()
    if solicitacao is None:
        raise HTTPException(status_code=404, detail="Solicitação não encontrada")
    atual = _dados_atendimento(solicitacao)
    if atual["em_atendimento_por_id"] not in (None, usuario.id) and usuario.papel not in ("admin", "supervisor"):
        raise HTTPException(status_code=403, detail=f"Esse pedido está com {atual['em_atendimento_por']}; só essa pessoa (ou um supervisor) pode liberar.")
    if atual["em_atendimento_por_id"] is not None:
        registrar_evento(db, solicitacao.id, "liberou", usuario=usuario,
                         detalhe=None if atual["em_atendimento_por_id"] == usuario.id else f"estava com {atual['em_atendimento_por']}")
    solicitacao.em_atendimento_por = None
    solicitacao.em_atendimento_por_id = None
    solicitacao.em_atendimento_desde = None
    db.commit()
    return {"status": "liberado"}


ROTULO_EVENTO = {
    "registrou": "Registrou a solicitação",
    "assumiu": "Assumiu o atendimento",
    "assumiu_no_lugar": "Assumiu no lugar de outra pessoa",
    "liberou": "Liberou o atendimento",
    "confirmou": "Confirmou o cancelamento",
    "confirmou_automatico": "Cancelamento confirmado automaticamente",
}


def _tempo_por_operador(solicitacao: SolicitacaoCancelamento, eventos: list) -> dict:
    """
    Reconstrói, pelo histórico, quanto tempo o pedido ficou com cada operador:
    assumiu / assumiu no lugar abre um trecho; liberou, a troca de operador
    ou a confirmação fecham. Soma os trechos do mesmo operador.
    Devolve {"fila": texto, "por_operador": [{"nome", "texto"}], "total": texto}.
    """
    fim_geral = solicitacao.confirmado_em or datetime.utcnow()
    trechos: dict[str, float] = {}
    ordem: list[str] = []
    atual, desde = None, None
    primeiro_assumiu = None
    for e in sorted(eventos, key=lambda x: (x.quando, x.id)):
        if e.tipo in ("assumiu", "assumiu_no_lugar", "liberou", "confirmou", "confirmou_automatico") and atual and desde:
            trechos[atual] = trechos.get(atual, 0) + max(0, (e.quando - desde).total_seconds())
            atual, desde = None, None
        if e.tipo in ("assumiu", "assumiu_no_lugar"):
            atual, desde = (e.usuario_nome or "—"), e.quando
            primeiro_assumiu = primeiro_assumiu or e.quando
            if atual not in ordem:
                ordem.append(atual)
    if atual and desde:  # ainda está com alguém
        trechos[atual] = trechos.get(atual, 0) + max(0, (fim_geral - desde).total_seconds())
    base = datetime(2000, 1, 1)
    texto = lambda seg: _duracao_texto(base, base + timedelta(seconds=seg)) or "0 min"
    return {
        "fila": _duracao_texto(solicitacao.criado_em, primeiro_assumiu or solicitacao.assumido_primeiro_em) or "—",
        "por_operador": [{"nome": nome, "texto": texto(trechos.get(nome, 0))} for nome in ordem],
        "total": _duracao_texto(solicitacao.criado_em, fim_geral) or "0 min",
    }


@router.get("/{solicitacao_id}/tempos")
def tempos_solicitacao(solicitacao_id: int, request: Request, db: Session = Depends(get_db)):
    """Tempo na fila, tempo com cada operador e tempo total do pedido."""
    _operador_que_confirma(request, db)
    solicitacao = db.query(SolicitacaoCancelamento).filter(SolicitacaoCancelamento.id == solicitacao_id).first()
    if solicitacao is None:
        raise HTTPException(status_code=404, detail="Solicitação não encontrada")
    eventos = db.query(SolicitacaoEvento).filter(SolicitacaoEvento.solicitacao_id == solicitacao_id).all()
    return _tempo_por_operador(solicitacao, eventos)


@router.get("/{solicitacao_id}/historico")
def historico_solicitacao(solicitacao_id: int, request: Request, db: Session = Depends(get_db)):
    """Linha do tempo da solicitação (quem fez o quê e quando), da mais antiga pra mais nova."""
    _operador_que_confirma(request, db)
    eventos = (
        db.query(SolicitacaoEvento)
        .filter(SolicitacaoEvento.solicitacao_id == solicitacao_id)
        .order_by(SolicitacaoEvento.quando.asc(), SolicitacaoEvento.id.asc())
        .all()
    )
    return [
        {
            "tipo": e.tipo,
            "rotulo": ROTULO_EVENTO.get(e.tipo, e.tipo),
            "quem": e.usuario_nome,
            "detalhe": e.detalhe,
            "quando": e.quando.isoformat() if e.quando else None,
        }
        for e in eventos
    ]


@router.post("/{solicitacao_id}/verificar-status")
def verificar_status_agora(solicitacao_id: int, request: Request, db: Session = Depends(get_db)):
    """
    Confere na hora (sem esperar o ciclo automático de até 12 min) se
    essa solicitação pendente já foi cancelada no Mercado Livre --
    útil pra testar uma solicitação específica. Ver
    app/verificacao_cancelamento.py pra lógica completa.
    """
    usuario_logado = auth.usuario_atual(request, db)
    if usuario_logado is None:
        raise HTTPException(status_code=401, detail="Sessão expirada -- faça login de novo.")

    solicitacao = db.query(SolicitacaoCancelamento).filter(SolicitacaoCancelamento.id == solicitacao_id).first()
    if solicitacao is None:
        raise HTTPException(status_code=404, detail="Solicitação não encontrada")
    if solicitacao.confirmado_por:
        raise HTTPException(status_code=400, detail="Essa solicitação já foi confirmada antes.")
    if solicitacao.plataforma != "mercado_livre":
        raise HTTPException(status_code=400, detail="Verificação automática só existe pra Mercado Livre.")

    from app.verificacao_cancelamento import verificar_uma_solicitacao
    resultado = verificar_uma_solicitacao(solicitacao, db)
    # Compatível com os dois formatos: o antigo (True/False) e o novo, que
    # também diz O MOTIVO ({"confirmou", "situacao", "detalhe"}).
    if isinstance(resultado, dict):
        confirmou = bool(resultado.get("confirmou"))
        situacao = resultado.get("situacao")
        mensagem = resultado.get("detalhe") or ("Cancelamento confirmado no Mercado Livre!" if confirmou else "Ainda não aparece como cancelada no Mercado Livre.")
    else:
        confirmou = bool(resultado)
        situacao = "confirmado" if confirmou else "ainda_pendente"
        mensagem = "Cancelamento confirmado no Mercado Livre!" if confirmou else "Ainda não aparece como cancelada no Mercado Livre."
    db.refresh(solicitacao)

    return {
        "confirmou": confirmou,
        "situacao": situacao,
        "mensagem": mensagem,
        "solicitacao": _serializar(solicitacao),
    }


# ---------------------------------------------------------------------------
# Relatório diário (Excel)
# ---------------------------------------------------------------------------
@router.get("/relatorio")
def relatorio_diario(data: Optional[str] = None, db: Session = Depends(get_db)):
    """
    Gera um Excel (.xlsx) de controle do dia: quantas solicitações
    foram registradas, quantas foram tratadas (confirmadas) e o
    resultado de reputação de cada uma tratada nesse dia -- pra
    acompanhamento diário de quanto foi respondido.

    `data` no formato AAAA-MM-DD (horário de Brasília); se omitido,
    usa o dia de hoje.
    """
    if data:
        try:
            dia = datetime.strptime(data, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="Data inválida -- use o formato AAAA-MM-DD.")
    else:
        dia = datetime.now(FUSO_BR).replace(tzinfo=None)

    inicio_dia = _inicio_do_dia_br_em_utc(dia)
    fim_dia = _inicio_do_dia_br_em_utc(dia + timedelta(days=1))

    tratadas_no_dia = (
        db.query(SolicitacaoCancelamento)
        .filter(SolicitacaoCancelamento.confirmado_em >= inicio_dia, SolicitacaoCancelamento.confirmado_em < fim_dia)
        .order_by(SolicitacaoCancelamento.confirmado_em.asc())
        .all()
    )
    registradas_no_dia = (
        db.query(SolicitacaoCancelamento)
        .filter(SolicitacaoCancelamento.criado_em >= inicio_dia, SolicitacaoCancelamento.criado_em < fim_dia)
        .count()
    )
    pendentes_total = db.query(SolicitacaoCancelamento).filter(SolicitacaoCancelamento.confirmado_por.is_(None)).count()

    contagem_impacto = {"sem_impacto": 0, "com_impacto": 0, "aguardando_confirmacao": 0, "nao_informado": 0}
    for s in tratadas_no_dia:
        chave = s.resultado_impacto if s.resultado_impacto in RESULTADOS_IMPACTO_VALIDOS else "nao_informado"
        contagem_impacto[chave] += 1

    wb = Workbook()

    resumo = wb.active
    resumo.title = "Resumo"
    resumo.append(["Relatório de cancelamentos -- Club Marketplace", dia.strftime("%d/%m/%Y")])
    resumo.append([])
    resumo.append(["Registradas no dia", registradas_no_dia])
    resumo.append(["Tratadas no dia", len(tratadas_no_dia)])
    resumo.append(["Pendentes agora (todas as datas)", pendentes_total])
    resumo.append([])
    resumo.append(["Resultado de reputação das tratadas no dia", ""])
    resumo.append(["Sem impacto", contagem_impacto["sem_impacto"]])
    resumo.append(["Com impacto", contagem_impacto["com_impacto"]])
    resumo.append(["Aguardando confirmação do ML", contagem_impacto["aguardando_confirmacao"]])
    resumo.append(["Não informado", contagem_impacto["nao_informado"]])
    resumo.column_dimensions["A"].width = 40
    resumo.column_dimensions["B"].width = 18

    mapa_contas = _mapa_contas_conhecidas(db)
    detalhe = wb.create_sheet("Detalhe do dia")
    detalhe.append([
        "Conta", "Plataforma", "Nº da venda", "Motivo", "Origem", "Confirmado por",
        "Confirmado em", "Resultado (reputação)", "Protocolo",
    ])
    for s in tratadas_no_dia:
        info = _serializar(s, mapa_contas)
        detalhe.append([
            info["conta_nome"],
            info["plataforma_nome"],
            info["numero_venda"],
            info["motivo"],
            {"seller": "Seller", "logistica": info["galpao_nome"] or "Logística", "publico": "Link público"}.get(info["origem"], "Seller / link público"),
            info["confirmado_por"],
            # Horário de Brasília (o banco guarda em UTC).
            datetime.fromisoformat(info["confirmado_em"]).replace(tzinfo=timezone.utc).astimezone(FUSO_BR).strftime("%d/%m/%Y %H:%M") if info["confirmado_em"] else "",
            LABEL_RESULTADO_IMPACTO.get(info["resultado_impacto"], "Não informado"),
            "Sem protocolo" if info["protocolo"] == "" else (info["protocolo"] or ""),
        ])
    larguras = [18, 14, 18, 30, 18, 18, 18, 26, 20]
    for indice, largura in enumerate(larguras, start=1):
        detalhe.column_dimensions[detalhe.cell(row=1, column=indice).column_letter].width = largura

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    nome_arquivo = f"cancelamentos_{dia.strftime('%Y-%m-%d')}.xlsx"
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nome_arquivo}"'},
    )


# ---------------------------------------------------------------------------
# Relatório por conta e período (tela "Relatório de cancelamentos")
# ---------------------------------------------------------------------------
PAPEIS_RELATORIO = ("admin", "supervisor", "atendente")
SEM_SKU = "—"


def _periodo_relatorio(periodo: str | None, de: str | None, ate: str | None, dias: int | None = None):
    """(início, fim, rótulo) em datas de Brasília. mes_atual | mes_anterior | datas | dias (últimos N dias)."""
    hoje = datetime.now(FUSO_BR).date()
    if periodo == "dias" and dias and dias > 0:
        inicio = hoje - timedelta(days=min(dias, 366) - 1)
        rotulo = "Hoje" if dias == 1 else f"Últimos {dias} dias"
        return inicio, hoje, f"{rotulo} ({inicio.strftime('%d/%m/%Y')} a {hoje.strftime('%d/%m/%Y')})"
    if periodo == "mes_anterior":
        fim = hoje.replace(day=1) - timedelta(days=1)
        inicio = fim.replace(day=1)
    elif periodo == "datas":
        try:
            inicio = datetime.strptime(de, "%Y-%m-%d").date() if de else hoje.replace(day=1)
            fim = datetime.strptime(ate, "%Y-%m-%d").date() if ate else hoje
        except ValueError:
            raise HTTPException(status_code=400, detail="Data inválida -- use o formato AAAA-MM-DD.")
        if inicio > fim:
            inicio, fim = fim, inicio
        if (fim - inicio).days > 366:
            raise HTTPException(status_code=400, detail="Período máximo de 1 ano.")
    else:
        inicio, fim = hoje.replace(day=1), hoje
    return inicio, fim, f"{inicio.strftime('%d/%m/%Y')} a {fim.strftime('%d/%m/%Y')}"


def _hora_br_texto(data_utc: Optional[datetime]) -> str:
    if not data_utc:
        return ""
    return data_utc.replace(tzinfo=timezone.utc).astimezone(FUSO_BR).strftime("%d/%m/%Y %H:%M")


def _texto_protocolo(s: SolicitacaoCancelamento) -> str:
    if s.protocolo == "":
        return "Sem protocolo"
    return s.protocolo or ""


@router.get("/relatorio-conta")
def relatorio_por_conta(
    request: Request,
    db: Session = Depends(get_db),
    conta: Optional[str] = None,       # vazio = todas as contas
    periodo: str = "mes_atual",        # mes_atual | mes_anterior | datas
    de: Optional[str] = None,
    ate: Optional[str] = None,
    plataforma: Optional[str] = None,
    status: str = "todos",             # todos | confirmados
    formato: str = "json",             # json | xlsx
    dias: Optional[int] = None,        # com periodo=dias: últimos N dias (filtro da tela)
    galpao: Optional[int] = None,
    origem: Optional[str] = None,      # seller | logistica | publico
):
    """
    Cancelamentos SOLICITADOS no período, de uma conta (ou todas): resumo,
    total por SKU (quantas vezes cada produto foi cancelado) e o detalhe
    de cada venda. formato=xlsx devolve a planilha pronta.
    """
    usuario = auth.usuario_atual(request, db)
    if usuario is None:
        raise HTTPException(status_code=401, detail="Sessão expirada -- faça login de novo.")
    if usuario.papel not in PAPEIS_RELATORIO:
        raise HTTPException(status_code=403, detail="Seu perfil não gera este relatório.")

    inicio, fim, rotulo = _periodo_relatorio(periodo, de, ate, dias)
    consulta = db.query(SolicitacaoCancelamento).filter(
        SolicitacaoCancelamento.criado_em >= _inicio_do_dia_br_em_utc(datetime.combine(inicio, datetime.min.time())),
        SolicitacaoCancelamento.criado_em < _inicio_do_dia_br_em_utc(datetime.combine(fim + timedelta(days=1), datetime.min.time())),
    )
    mapa_contas = _mapa_contas_conhecidas(db)
    conta_nome = None
    if conta:
        chave = chave_conta(conta)
        consulta = consulta.filter(SolicitacaoCancelamento.conta_chave == chave)
        conta_nome = mapa_contas.get(chave, conta)
    if plataforma in PLATAFORMAS_VALIDAS:
        consulta = consulta.filter(SolicitacaoCancelamento.plataforma == plataforma)
    if galpao in GALPOES_VALIDOS:
        consulta = consulta.filter(SolicitacaoCancelamento.galpao == galpao)
    if origem == "publico":
        consulta = consulta.filter(or_(SolicitacaoCancelamento.origem == "publico", SolicitacaoCancelamento.origem.is_(None)))
    elif origem in ORIGENS_VALIDAS:
        consulta = consulta.filter(SolicitacaoCancelamento.origem == origem)
    if status == "confirmados":
        consulta = consulta.filter(SolicitacaoCancelamento.confirmado_por.isnot(None))
    itens = consulta.order_by(SolicitacaoCancelamento.criado_em.asc()).all()

    # Total por SKU (pedido com vários produtos conta em cada SKU).
    por_sku: dict[str, dict] = {}
    for s in itens:
        skus = [x.strip() for x in (s.sku or "").split(",") if x.strip()] or [SEM_SKU]
        # Nomes na mesma ordem dos SKUs (ver cancelamento_apoio.extrair_produto_do_pedido)
        nomes = [x.strip() for x in (s.produto_titulo or "").split(" | ")] if len(skus) > 1 else [s.produto_titulo]
        for indice, sku in enumerate(skus):
            linha = por_sku.setdefault(sku, {"sku": sku, "produto": None, "quantidade": 0, "cancelados": 0})
            linha["quantidade"] += 1
            linha["cancelados"] += 1 if s.confirmado_por else 0
            nome = nomes[indice] if indice < len(nomes) else None
            if not linha["produto"] and nome and sku != SEM_SKU:
                linha["produto"] = nome
    ranking = sorted(por_sku.values(), key=lambda x: (x["sku"] == SEM_SKU, -x["quantidade"], x["sku"]))
    for linha in ranking:
        if linha["sku"] == SEM_SKU:
            linha["produto"] = "Sem SKU identificado"

    detalhe = [
        {
            "conta": mapa_contas.get(s.conta_chave or chave_conta(s.conta), s.conta),
            "plataforma": PLATAFORMAS.get(s.plataforma, s.plataforma),
            "solicitado_em": _hora_br_texto(s.criado_em),
            "cancelado_em": _hora_br_texto(s.confirmado_em),
            "numero_venda": s.numero_venda,
            "sku": s.sku or SEM_SKU,
            "produto": s.produto_titulo or "",
            "motivo": s.motivo,
            "protocolo": _texto_protocolo(s),
            "impacto": LABEL_RESULTADO_IMPACTO.get(s.resultado_impacto, "") if s.confirmado_por else "",
            "confirmado_por": s.confirmado_por or "",
            "origem": {"seller": "Seller", "logistica": GALPOES.get(s.galpao, "Galpão"), "publico": "Link público"}.get(s.origem, "Seller / link público"),
        }
        for s in itens
    ]
    resumo = {
        "solicitacoes": len(itens),
        "cancelados": sum(1 for s in itens if s.confirmado_por),
        "pendentes": sum(1 for s in itens if not s.confirmado_por),
        "com_impacto": sum(1 for s in itens if s.confirmado_por and s.resultado_impacto == "com_impacto"),
    }
    meta = {
        "conta": conta_nome or "Todas as contas",
        "periodo": rotulo,
        "de": inicio.isoformat(),
        "ate": fim.isoformat(),
        "plataforma": PLATAFORMAS.get(plataforma, "Todas as plataformas") if plataforma else "Todas as plataformas",
        "status": "Só cancelados" if status == "confirmados" else "Cancelados e pendentes",
        "galpao": GALPOES.get(galpao) if galpao in GALPOES_VALIDOS else None,
        "origem": {"seller": "Sellers", "logistica": "Galpão", "publico": "Link público"}.get(origem),
        "gerado_em": datetime.now(FUSO_BR).strftime("%d/%m/%Y %H:%M"),
        "gerado_por": usuario.nome_exibicao,
    }

    if formato != "xlsx":
        return {"meta": meta, "resumo": resumo, "por_sku": ranking, "detalhe": detalhe}

    wb = Workbook()
    aba = wb.active
    aba.title = "Resumo"
    for linha in (
        ["Relatório de cancelamentos -- Club Marketplace"],
        ["Conta", meta["conta"]], ["Período", meta["periodo"]], ["Plataforma", meta["plataforma"]],
        ["Status", meta["status"]], ["Gerado em", f"{meta['gerado_em']} por {meta['gerado_por']}"], [],
        ["Solicitações", resumo["solicitacoes"]], ["Cancelados", resumo["cancelados"]],
        ["Pendentes", resumo["pendentes"]], ["Com impacto na reputação", resumo["com_impacto"]],
    ):
        aba.append(linha)
    aba.column_dimensions["A"].width = 30
    aba.column_dimensions["B"].width = 34

    aba_sku = wb.create_sheet("Por SKU")
    aba_sku.append(["SKU", "Produto", "Solicitações", "Cancelados"])
    for linha in ranking:
        aba_sku.append([linha["sku"], linha["produto"] or "", linha["quantidade"], linha["cancelados"]])
    for coluna, largura in zip("ABCD", (22, 50, 14, 12)):
        aba_sku.column_dimensions[coluna].width = largura

    aba_det = wb.create_sheet("Detalhe")
    colunas = [("Conta", "conta", 18), ("Plataforma", "plataforma", 14), ("Solicitado em", "solicitado_em", 17),
               ("Cancelado em", "cancelado_em", 17), ("Nº da venda", "numero_venda", 20), ("SKU", "sku", 18),
               ("Produto", "produto", 40), ("Motivo", "motivo", 30), ("Protocolo", "protocolo", 18),
               ("Impacto", "impacto", 16), ("Confirmado por", "confirmado_por", 18), ("Origem", "origem", 16)]
    aba_det.append([c[0] for c in colunas])
    for linha in detalhe:
        aba_det.append([linha[c[1]] for c in colunas])
    for indice, (_, _, largura) in enumerate(colunas, start=1):
        aba_det.column_dimensions[aba_det.cell(row=1, column=indice).column_letter].width = largura

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    nome = f"cancelamentos_{chave_conta(meta['conta']).replace(' ', '-')}_{inicio.isoformat()}_{fim.isoformat()}.xlsx"
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nome}"'},
    )


# ---------------------------------------------------------------------------
# Controle do dia -- dados pra prévia na tela / impressão.
# (O Excel do controle do dia continua sendo o /relatorio, sem mudança.)
# ---------------------------------------------------------------------------
def _duracao_texto(inicio: Optional[datetime], fim: Optional[datetime]) -> str:
    """'12 min' / '1h05' / '2 dia(s) 3h' -- vazio se faltar uma das pontas."""
    if not inicio or not fim or fim < inicio:
        return ""
    minutos = int((fim - inicio).total_seconds() // 60)
    if minutos < 60:
        return f"{minutos} min"
    if minutos < 60 * 24:
        return f"{minutos // 60}h{minutos % 60:02d}"
    return f"{minutos // 1440} dia(s) {(minutos % 1440) // 60}h"


def _media_texto(pares) -> str:
    """Média de várias durações (lista de (início, fim)), no mesmo formato."""
    validos = [(fim - ini).total_seconds() for ini, fim in pares if ini and fim and fim >= ini]
    if not validos:
        return "—"
    base = datetime(2000, 1, 1)
    return _duracao_texto(base, base + timedelta(seconds=sum(validos) / len(validos)))


@router.get("/relatorio-dia-dados")
def relatorio_dia_dados(request: Request, data: Optional[str] = None, db: Session = Depends(get_db)):
    """
    Controle do dia pra mostrar/imprimir: o que foi REGISTRADO no dia e o
    que foi TRATADO (confirmado) no dia, com os tempos de cada pedido:
      - esperou: do pedido até alguém assumir
      - com operador: de quando assumiu até confirmar
      - total: do pedido até a confirmação
    """
    usuario = auth.usuario_atual(request, db)
    if usuario is None:
        raise HTTPException(status_code=401, detail="Sessão expirada -- faça login de novo.")
    if usuario.papel not in PAPEIS_RELATORIO:
        raise HTTPException(status_code=403, detail="Seu perfil não gera este relatório.")
    try:
        dia = datetime.strptime(data, "%Y-%m-%d").date() if data else datetime.now(FUSO_BR).date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Data inválida -- use o formato AAAA-MM-DD.")
    inicio = _inicio_do_dia_br_em_utc(datetime.combine(dia, datetime.min.time()))
    fim = _inicio_do_dia_br_em_utc(datetime.combine(dia + timedelta(days=1), datetime.min.time()))

    mapa_contas = _mapa_contas_conhecidas(db)
    nome_conta = lambda s: mapa_contas.get(s.conta_chave or chave_conta(s.conta), s.conta)
    hora = lambda d: d.replace(tzinfo=timezone.utc).astimezone(FUSO_BR).strftime("%H:%M") if d else ""
    quem_pediu = lambda s: {"seller": "Seller", "logistica": GALPOES.get(s.galpao, "Galpão"), "publico": "Link público"}.get(s.origem, "Seller / link público") + (f" · {s.solicitado_por}" if s.solicitado_por else "")

    registradas = (
        db.query(SolicitacaoCancelamento)
        .filter(SolicitacaoCancelamento.criado_em >= inicio, SolicitacaoCancelamento.criado_em < fim)
        .order_by(SolicitacaoCancelamento.criado_em.asc()).all()
    )
    tratadas = (
        db.query(SolicitacaoCancelamento)
        .filter(SolicitacaoCancelamento.confirmado_em >= inicio, SolicitacaoCancelamento.confirmado_em < fim)
        .order_by(SolicitacaoCancelamento.confirmado_em.asc()).all()
    )
    # Histórico das tratadas numa consulta só (pro "tempo por operador").
    eventos_por_id: dict[int, list] = {}
    if tratadas:
        for e in db.query(SolicitacaoEvento).filter(SolicitacaoEvento.solicitacao_id.in_([s.id for s in tratadas])).all():
            eventos_por_id.setdefault(e.solicitacao_id, []).append(e)

    def por_operador(s):
        t = _tempo_por_operador(s, eventos_por_id.get(s.id, []))
        return " · ".join(f"{x['nome']} {x['texto']}" for x in t["por_operador"]) or "—"

    return {
        "meta": {
            "dia": dia.strftime("%d/%m/%Y"),
            "gerado_em": datetime.now(FUSO_BR).strftime("%d/%m/%Y %H:%M"),
            "gerado_por": usuario.nome_exibicao,
        },
        "resumo": {
            "registradas": len(registradas),
            "tratadas": len(tratadas),
            "pendentes_do_dia": sum(1 for s in registradas if not s.confirmado_por),
            "sem_impacto": sum(1 for s in tratadas if s.resultado_impacto == "sem_impacto"),
            "com_impacto": sum(1 for s in tratadas if s.resultado_impacto == "com_impacto"),
            "aguardando_ml": sum(1 for s in tratadas if s.resultado_impacto == "aguardando_confirmacao"),
            "media_espera": _media_texto([(s.criado_em, s.assumido_primeiro_em) for s in tratadas]),
            "media_com_operador": _media_texto([(s.assumido_primeiro_em, s.confirmado_em) for s in tratadas]),
            "media_total": _media_texto([(s.criado_em, s.confirmado_em) for s in tratadas]),
        },
        "registradas": [
            {
                "hora": hora(s.criado_em), "conta": nome_conta(s), "plataforma": PLATAFORMAS.get(s.plataforma, s.plataforma),
                "numero_venda": s.numero_venda, "sku": s.sku or "—", "motivo": s.motivo, "quem_pediu": quem_pediu(s),
                "situacao": f"Cancelado · {s.confirmado_por}" if s.confirmado_por else (
                    f"Com {s.em_atendimento_por}" if _dados_atendimento(s)["em_atendimento_por"] else "Pendente"),
            }
            for s in registradas
        ],
        "tratadas": [
            {
                "hora": hora(s.confirmado_em), "conta": nome_conta(s), "numero_venda": s.numero_venda,
                "confirmado_por": s.confirmado_por, "protocolo": _texto_protocolo(s) or "—",
                "impacto": LABEL_RESULTADO_IMPACTO.get(s.resultado_impacto, "Não informado"),
                "esperou": _duracao_texto(s.criado_em, s.assumido_primeiro_em) or "—",
                "com_operador": _duracao_texto(s.assumido_primeiro_em, s.confirmado_em) or "—",
                "total": _duracao_texto(s.criado_em, s.confirmado_em) or "—",
                "por_operador": por_operador(s),
            }
            for s in tratadas
        ],
    }
