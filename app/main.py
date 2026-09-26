from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app.database import Base, engine, SessionLocal, garantir_estrutura_atualizada
from datetime import datetime, timedelta, timezone
from sqlalchemy import func
from app.models import Usuario, SolicitacaoCancelamento, Devolucao, Pergunta
from app import auth, config
from app.contas_util import chave_conta
from app.routers.solicitacoes_cancelamento import GALPOES, PLATAFORMAS
from app.routers import devolucoes, relatorio_conta, auth_ml, webhook_ml, pre_venda, manuais, pos_venda, cancelamentos, eventos_webhook, solicitacoes_cancelamento, diagnostico_sku, reputacao as reputacao_rotas, respostas_padrao as respostas_padrao_rotas

# Cria as tabelas no banco se ainda não existirem (em produção, o ideal
# é usar uma ferramenta de migração como Alembic, mas isso é suficiente
# pra essa fase inicial).
from sqlalchemy import inspect as _inspecionar
_tabela_respostas_padrao_existia = _inspecionar(engine).has_table("respostas_padrao")
Base.metadata.create_all(bind=engine)
# Colunas novas em tabelas que já existiam (o create_all não faz isso).
garantir_estrutura_atualizada()
# Primeira subida com a tela "Respostas padrão": cadastra as respostas
# iniciais (desligadas, pra revisão). Só nessa vez -- se apagarem, não voltam.
if not _tabela_respostas_padrao_existia:
    try:
        from app.respostas_iniciais import semear as _semear_respostas
        with SessionLocal() as _db:
            _semear_respostas(_db)
    except Exception:  # nunca impede o sistema de subir por causa disso
        import logging as _logging
        _logging.getLogger(__name__).exception("Não consegui cadastrar as respostas padrão iniciais")


def bootstrap_admin_inicial():
    """
    Cria o primeiro admin a partir de ADMIN_USUARIO_INICIAL/
    ADMIN_SENHA_INICIAL (variáveis de ambiente), mas SÓ se a tabela de
    usuários ainda estiver vazia -- depois que existir qualquer
    usuário, essas variáveis não fazem mais nada. Sem isso, ninguém
    conseguiria logar pela primeira vez (criar usuário exige estar
    logado como admin -- problema do "ovo e da galinha").
    """
    if not (config.ADMIN_USUARIO_INICIAL and config.ADMIN_SENHA_INICIAL):
        return
    db = SessionLocal()
    try:
        if db.query(Usuario).first() is not None:
            return
        admin = Usuario(
            usuario=config.ADMIN_USUARIO_INICIAL,
            nome_exibicao=config.ADMIN_USUARIO_INICIAL,
            papel="admin",
            senha_hash=auth.gerar_hash_senha(config.ADMIN_SENHA_INICIAL),
            precisa_trocar_senha=False,
        )
        db.add(admin)
        db.commit()
    finally:
        db.close()


bootstrap_admin_inicial()

app = FastAPI(title="Club Marketplace — Painel")


# --- Tarefa em segundo plano: verificação automática das solicitações de
# cancelamento (ver app/verificacao_cancelamento.py). A cada 12 min confere
# no Mercado Livre se as pendentes já foram canceladas. Pode ser desligada
# sem mexer no código com a variável VERIFICACAO_CANCELAMENTO_ATIVA=0.
@app.on_event("startup")
async def _iniciar_verificacao_cancelamentos() -> None:
    import asyncio
    import logging
    import os

    if os.getenv("VERIFICACAO_CANCELAMENTO_ATIVA", "1").strip() in ("0", "false", "nao", "não"):
        logging.getLogger(__name__).info("Verificação automática de cancelamentos DESLIGADA pela variável de ambiente.")
        return
    from app.verificacao_cancelamento import loop_verificacao_cancelamentos
    # Guarda a referência da tarefa (senão o Python pode descartá-la).
    app.state.tarefa_verificacao_cancelamentos = asyncio.create_task(loop_verificacao_cancelamentos())
    logging.getLogger(__name__).info("Verificação automática de cancelamentos iniciada (a cada 12 min).")


# --- Tarefa em segundo plano: vigia da fila de pré-venda (ver
# app/vigia_perguntas.py). A cada 60 s confere no ML se as perguntas que
# estão esperando já foram respondidas por fora (IA do ML / seller no app)
# ou apagadas. Intervalo: VIGIA_PERGUNTAS_INTERVALO_SEG. Desliga com
# VIGIA_PERGUNTAS_ATIVA=0.
@app.on_event("startup")
async def _iniciar_vigia_perguntas() -> None:
    import asyncio
    import logging
    import os

    if os.getenv("VIGIA_PERGUNTAS_ATIVA", "1").strip() in ("0", "false", "nao", "não"):
        logging.getLogger(__name__).info("Vigia da fila de pré-venda DESLIGADO pela variável de ambiente.")
        return
    from app.vigia_perguntas import loop_vigia_perguntas
    app.state.tarefa_vigia_perguntas = asyncio.create_task(loop_vigia_perguntas())
    logging.getLogger(__name__).info("Vigia da fila de pré-venda iniciado.")


# --- Tarefa em segundo plano: leitura da reputação (termômetro) de todas as
# contas no Mercado Livre (ver app/reputacao.py). Padrão: a cada 90 min
# (REPUTACAO_INTERVALO_MIN). Desliga com REPUTACAO_AUTOMATICA_ATIVA=0.
@app.on_event("startup")
async def _iniciar_leitura_reputacao() -> None:
    import asyncio
    import logging
    import os

    if os.getenv("REPUTACAO_AUTOMATICA_ATIVA", "1").strip() in ("0", "false", "nao", "não"):
        logging.getLogger(__name__).info("Leitura automática de reputação DESLIGADA pela variável de ambiente.")
        return
    from app.reputacao import loop_reputacao
    app.state.tarefa_reputacao = asyncio.create_task(loop_reputacao())
    logging.getLogger(__name__).info("Leitura automática de reputação iniciada.")

# --- Middleware de autenticação ---
# Tudo exige login por padrão, INCLUSIVE os painéis de TV (a TV entra com
# um usuário do perfil "tv": só visualiza e fica logada 30 dias). As
# exceções abaixo são só o que PRECISA ficar aberto: login/primeiro acesso,
# o webhook do Mercado Livre (quem chama é o ML, não uma pessoa), o
# callback de OAuth e a verificação de saúde. O formulário de solicitação
# de cancelamento também exige login (seller, logística, atendente...).
_PUBLICO_EXATO = {
    ("GET", "/login"), ("POST", "/login"),
    ("GET", "/logout"),
    ("GET", "/primeiro-acesso"), ("POST", "/primeiro-acesso"),
    ("POST", "/webhook/mercado-livre"),
    ("GET", "/auth/ml/callback"),
    ("GET", "/api/saude"),
}
_PUBLICO_PREFIXOS = ("/static/",)


# --- Permissões por perfil ---
# Áreas do sistema que podem ser liberadas pra perfis restritos. Cada
# área lista as páginas e os prefixos de API que ela usa. Pra criar no
# futuro um "Atendente Pré" e um "Atendente Pós", basta dar a cada um
# só a área correspondente em _AREAS_POR_PAPEL -- sem mexer no resto.
_AREAS = {
    "pre_venda": {"paginas": {"/pre-venda"}, "prefixos_api": ("/api/pre-venda/",)},
    "pos_venda": {"paginas": {"/pos-venda"}, "prefixos_api": ("/api/pos-venda/",)},
    # Painéis de TV + os dados que eles leem (só leitura: endereços EXATOS).
    "paineis_tv": {
        "paginas": {
            "/painel-tv/geral", "/painel-tv/fila",
            "/api/pre-venda/fila", "/api/pre-venda/painel-geral", "/api/pre-venda/painel-resumo",
        },
        "prefixos_api": (),
    },
    # Termômetro de reputação de todas as contas (só visualizar; o
    # "Atualizar agora" é conferido na própria rota: admin/supervisor).
    "reputacao": {"paginas": {"/reputacao"}, "prefixos_api": ("/api/reputacao/",)},
    # Respostas padrão da nossa IA (os atendentes cadastram e mantêm; apagar
    # é conferido na própria rota: só admin/supervisor).
    "respostas_padrao": {"paginas": {"/respostas-padrao", "/api/respostas-padrao"}, "prefixos_api": ("/api/respostas-padrao/",)},
    # Acompanhar e confirmar as solicitações manuais de cancelamento.
    # A lista é "/api/solicitacoes-cancelamento" (exato) e o confirmar é
    # "/api/solicitacoes-cancelamento/{id}/confirmar" (prefixo).
    "solicitacoes_cancelamento": {
        "paginas": {"/solicitacoes-painel", "/relatorio-cancelamentos", "/solicitar-cancelamento", "/api/solicitacoes-cancelamento"},
        "prefixos_api": ("/api/solicitacoes-cancelamento/",),
    },
    # Galpão: registrar (o formulário e o envio já são públicos) e
    # acompanhar. Só endereços EXATOS -- de propósito, o /confirmar
    # fica de fora (quem pede não confirma).
    "logistica": {
        "paginas": {
            "/solicitacoes-galpao",
            "/solicitar-cancelamento", "/api/solicitacoes-cancelamento",
            "/api/solicitacoes-cancelamento/busca",
            "/api/solicitacoes-cancelamento/contas",
            "/api/solicitacoes-cancelamento/opcoes",
        },
        "prefixos_api": (),
    },
}

# Perfis com acesso RESTRITO: só entram no que está listado aqui (lista
# do que PODE -- tela nova nasce bloqueada pra eles até alguém liberar).
# Admin e supervisor não aparecem aqui porque têm acesso amplo.
_AREAS_POR_PAPEL = {
    "atendente": ("pre_venda", "pos_venda", "paineis_tv", "solicitacoes_cancelamento", "reputacao", "respostas_padrao"),
    "logistica": ("logistica",),
    # TV: só enxerga os painéis (não responde nada, não abre outras telas).
    "tv": ("paineis_tv",),
}
_PAGINA_INICIAL_POR_PAPEL = {
    "seller": "/meus-cancelamentos",
    "atendente": "/pre-venda",
    "logistica": "/solicitar-cancelamento",
    "tv": "/painel-tv/fila",
}

# Quais perfis cada papel pode criar/editar/resetar/desativar. Usado em
# TODAS as rotas de operadores -- a checagem de verdade é sempre aqui,
# no servidor, nunca só no <select> da tela.
PAPEIS_GERENCIAVEIS_POR = {
    "admin": ("admin", "supervisor", "atendente", "logistica", "seller", "tv"),
    "supervisor": ("atendente", "logistica", "seller", "tv"),
}


def _papel_pode_acessar(papel: str, caminho: str) -> bool:
    """Diz se um perfil restrito (ex: atendente) pode abrir esse caminho."""
    if caminho == "/logout":
        return True
    for nome_area in _AREAS_POR_PAPEL.get(papel, ()):
        area = _AREAS[nome_area]
        if caminho in area["paginas"] or any(caminho.startswith(p) for p in area["prefixos_api"]):
            return True
    return False


def _pode_gerenciar(usuario_logado, papel_alvo: str) -> bool:
    """True se quem está logado pode mexer num operador com esse papel."""
    return bool(usuario_logado) and papel_alvo in PAPEIS_GERENCIAVEIS_POR.get(usuario_logado.papel, ())


@app.middleware("http")
async def exigir_login(request: Request, call_next):
    caminho = request.url.path
    metodo = request.method

    eh_publico = (metodo, caminho) in _PUBLICO_EXATO or any(caminho.startswith(p) for p in _PUBLICO_PREFIXOS)
    if eh_publico:
        return await call_next(request)

    token = request.cookies.get(auth.COOKIE_SESSAO)
    usuario_id = auth.ler_usuario_id_da_sessao(token) if token else None

    if not usuario_id:
        if caminho.startswith("/api/"):
            return JSONResponse({"detail": "Não autenticado"}, status_code=401)
        # Volta pra onde a pessoa queria ir depois de entrar (ex.: seller abrindo o link do formulário).
        from urllib.parse import quote
        destino = caminho + (("?" + request.url.query) if request.url.query else "")
        return RedirectResponse("/login?proximo=" + quote(destino, safe=""), status_code=303)

    # Perfis restritos. Cada perfil é conferido SEPARADAMENTE (um atalho
    # comum deixava outros perfis passarem pelas rotas liberadas ao seller).
    #  - Seller: as telas dele + o formulário de solicitar cancelamento (a
    #    conta dele vem preenchida e o servidor não deixa pedir por outra).
    #  - Atendente, logística, TV...: só o que está nas áreas do perfil.
    # Qualquer outra rota é bloqueada, mesmo sabendo a URL.
    _ROTAS_PERMITIDAS_PARA_SELLER = {
        "/meus-cancelamentos", "/logout",
        "/solicitar-cancelamento", "/api/solicitacoes-cancelamento", "/api/solicitacoes-cancelamento/contas",
    }
    with SessionLocal() as db:
        usuario_logado = db.query(Usuario).filter(Usuario.id == usuario_id).first()
    papel = usuario_logado.papel if usuario_logado else None

    if papel == "seller" and caminho not in _ROTAS_PERMITIDAS_PARA_SELLER:
        if caminho.startswith("/api/"):
            return JSONResponse({"detail": "Acesso restrito"}, status_code=403)
        return RedirectResponse("/meus-cancelamentos", status_code=303)

    if papel in _AREAS_POR_PAPEL and not _papel_pode_acessar(papel, caminho):
        if caminho.startswith("/api/"):
            return JSONResponse({"detail": "Acesso restrito"}, status_code=403)
        return RedirectResponse(_PAGINA_INICIAL_POR_PAPEL.get(papel, "/logout"), status_code=303)

    return await call_next(request)

app.include_router(devolucoes.router)
app.include_router(diagnostico_sku.router)  # ferramentas de admin do SKU
app.include_router(relatorio_conta.router)
app.include_router(auth_ml.router)
app.include_router(webhook_ml.router)
app.include_router(pre_venda.router)
app.include_router(manuais.router)
app.include_router(pos_venda.router)
app.include_router(cancelamentos.router)
app.include_router(eventos_webhook.router)
app.include_router(solicitacoes_cancelamento.router)
app.include_router(reputacao_rotas.router)
app.include_router(respostas_padrao_rotas.router)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# O banco guarda datas em UTC "cru" (datetime.utcnow). Nas telas montadas
# no servidor, sempre exibir no horário de Brasília (UTC-3 fixo -- sem
# horário de verão desde 2019). Uso: {{ s.criado_em | hora_br }}
_FUSO_BR = timezone(timedelta(hours=-3))


def _hora_br(data, formato: str = "%d/%m/%Y %H:%M") -> str:
    if not data:
        return "-"
    if data.tzinfo is None:
        data = data.replace(tzinfo=timezone.utc)
    return data.astimezone(_FUSO_BR).strftime(formato)


templates.env.filters["hora_br"] = _hora_br
# Nomes de plataforma/galpão disponíveis em todas as telas montadas no servidor.
templates.env.globals["PLATAFORMAS"] = PLATAFORMAS
templates.env.globals["GALPOES"] = GALPOES


@app.get("/", response_class=HTMLResponse)
def pagina_dashboard(request: Request):
    """
    Dashboard — tela inicial, resumo geral. Só leitura, agrega números
    de várias áreas (devoluções, pré-venda, cancelamentos) pra dar uma
    visão rápida de "como está tudo" antes de entrar em cada tela
    específica.
    """
    with SessionLocal() as db:
        agora = datetime.utcnow()
        usuario_logado = auth.usuario_atual(request, db)

        # Devoluções (últimos 30 dias)
        limite_30d = agora - timedelta(days=30)
        devolucoes_30d = db.query(Devolucao).filter(Devolucao.data_criacao >= limite_30d).all()
        devolucoes_total = len(devolucoes_30d)
        devolucoes_impacto = sum((d.valor or 0) + (d.custo_frete_retorno or 0) for d in devolucoes_30d)

        # Pré-venda de hoje
        inicio_do_dia = agora.replace(hour=0, minute=0, second=0, microsecond=0)
        perguntas_hoje = db.query(Pergunta).filter(Pergunta.recebida_em >= inicio_do_dia).all()
        pre_venda_total_hoje = len(perguntas_hoje)
        camadas_automaticas = {"resposta_validada", "resposta_padrao", "manual_sku_ia", "politica_geral", "busca_site_fabricante"}
        pre_venda_ia = sum(1 for p in perguntas_hoje if p.camada_resolvida in camadas_automaticas)
        pre_venda_pct_ia = round(pre_venda_ia / pre_venda_total_hoje * 100) if pre_venda_total_hoje else 0

        # Pendentes agora (qualquer dia, não só hoje)
        pendentes = db.query(Pergunta).filter(Pergunta.status == "fila_humana").all()
        pendentes_total = len(pendentes)
        pendentes_criticas = sum(
            1 for p in pendentes
            if p.recebida_em and (agora - p.recebida_em).total_seconds() / 60 > 30
        )

        # Cancelamentos manuais desta semana (segunda a agora)
        inicio_semana = (agora - timedelta(days=agora.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        cancelamentos_semana = db.query(SolicitacaoCancelamento).filter(SolicitacaoCancelamento.criado_em >= inicio_semana).all()
        cancelamentos_total = len(cancelamentos_semana)
        cancelamentos_tratados = sum(1 for s in cancelamentos_semana if s.confirmado_por)

        contexto = {
            "devolucoes_total": devolucoes_total,
            "devolucoes_impacto": devolucoes_impacto,
            "pre_venda_total_hoje": pre_venda_total_hoje,
            "pre_venda_pct_ia": pre_venda_pct_ia,
            "pendentes_total": pendentes_total,
            "pendentes_criticas": pendentes_criticas,
            "cancelamentos_total": cancelamentos_total,
            "cancelamentos_tratados": cancelamentos_tratados,
            "usuario_logado": usuario_logado,
        }

    return templates.TemplateResponse(request=request, name="dashboard.html", context=contexto)


def _usuario_logado(request: Request):
    """Busca o usuário logado (ou None) pra exibir nome no rodapé do menu."""
    with SessionLocal() as db:
        return auth.usuario_atual(request, db)


@app.get("/devolucoes", response_class=HTMLResponse)
def pagina_devolucoes(request: Request):
    return templates.TemplateResponse(request=request, name="devolucoes.html", context={"usuario_logado": _usuario_logado(request)})


@app.get("/contas", response_class=HTMLResponse)
def pagina_contas(request: Request):
    return templates.TemplateResponse(request=request, name="contas.html", context={"usuario_logado": _usuario_logado(request)})


@app.get("/respostas-padrao", response_class=HTMLResponse)
def pagina_respostas_padrao(request: Request):
    return templates.TemplateResponse(request=request, name="respostas-padrao.html", context={"usuario_logado": _usuario_logado(request)})


@app.get("/reputacao", response_class=HTMLResponse)
def pagina_reputacao(request: Request):
    return templates.TemplateResponse(request=request, name="reputacao.html", context={"usuario_logado": _usuario_logado(request)})


@app.get("/pre-venda", response_class=HTMLResponse)
def pagina_pre_venda(request: Request):
    return templates.TemplateResponse(request=request, name="pre-venda.html", context={"usuario_logado": _usuario_logado(request)})


@app.get("/manuais", response_class=HTMLResponse)
def pagina_manuais(request: Request):
    return templates.TemplateResponse(request=request, name="manuais.html", context={"usuario_logado": _usuario_logado(request)})


@app.get("/pos-venda", response_class=HTMLResponse)
def pagina_pos_venda(request: Request):
    return templates.TemplateResponse(request=request, name="pos-venda.html", context={"usuario_logado": _usuario_logado(request)})


@app.get("/cancelamentos", response_class=HTMLResponse)
def pagina_cancelamentos(request: Request):
    return templates.TemplateResponse(request=request, name="cancelamentos.html", context={"usuario_logado": _usuario_logado(request)})


@app.get("/cancelamentos-painel", response_class=HTMLResponse)
def pagina_painel_cancelamentos(request: Request):
    """Tela interna de acompanhamento dos pedidos de cancelamento, agrupada por conta."""
    return templates.TemplateResponse(request=request, name="painel-cancelamentos.html", context={"usuario_logado": _usuario_logado(request)})


@app.get("/eventos-webhook", response_class=HTMLResponse)
def pagina_eventos_webhook(request: Request):
    return templates.TemplateResponse(request=request, name="eventos-webhook.html", context={"usuario_logado": _usuario_logado(request)})


@app.get("/painel-tv/geral", response_class=HTMLResponse)
def pagina_painel_tv_geral(request: Request):
    """Tela de TV 1 — visão geral agregada, pra bater o olho de longe (exige login)."""
    return templates.TemplateResponse(request=request, name="painel-tv-geral.html", context={"usuario_logado": _usuario_logado(request)})


@app.get("/painel-tv/fila", response_class=HTMLResponse)
def pagina_painel_tv_fila(request: Request):
    """
    Tela de TV 2 — fila de ação, só o que precisa de humano, cross-conta.
    Exige login. Admin, supervisor e atendente respondem clicando na
    pergunta; o perfil "tv" só visualiza.
    """
    usuario = _usuario_logado(request)
    pode_responder = bool(usuario and usuario.papel in ("admin", "supervisor", "atendente"))
    return templates.TemplateResponse(
        request=request, name="painel-tv-fila.html",
        context={"usuario_logado": usuario, "pode_responder": pode_responder},
    )


@app.get("/solicitar-cancelamento", response_class=HTMLResponse)
def pagina_solicitar_cancelamento(request: Request):
    """
    Formulário pra registrar manualmente um pedido de cancelamento (exige
    login). Se quem está acessando for um seller, a conta dele já vem
    pré-preenchida (e o servidor não deixa pedir por outra conta).
    """
    with SessionLocal() as db:
        usuario = auth.usuario_atual(request, db)
        conta_pre_preenchida = usuario.conta_vinculada if (usuario and usuario.papel == "seller") else None
        logistica_logado = bool(usuario and usuario.papel == "logistica")
        nome_usuario = usuario.nome_exibicao if usuario else None
    return templates.TemplateResponse(
        request=request,
        name="solicitar-cancelamento.html",
        context={
            "conta_pre_preenchida": conta_pre_preenchida,
            "seller_logado": bool(conta_pre_preenchida),
            "logistica_logado": logistica_logado,
            "nome_usuario": nome_usuario,
        },
    )


@app.get("/solicitacoes-galpao", response_class=HTMLResponse)
def pagina_solicitacoes_galpao(request: Request):
    """Acompanhamento das solicitações feitas pelos galpões (perfil Logística)."""
    with SessionLocal() as db:
        usuario = auth.usuario_atual(request, db)
        if not auth.papel_permite(usuario, ("logistica", "admin", "supervisor")):
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(
            request=request, name="solicitacoes-galpao.html", context={"usuario": usuario},
        )


@app.get("/meus-cancelamentos", response_class=HTMLResponse)
def pagina_meus_cancelamentos(request: Request):
    """Tela do seller: só as solicitações da conta vinculada a ele."""
    with SessionLocal() as db:
        usuario = auth.usuario_atual(request, db)
        if not usuario or usuario.papel != "seller" or not usuario.conta_vinculada:
            return RedirectResponse("/", status_code=303)

        solicitacoes = (
            db.query(SolicitacaoCancelamento)
            .filter(SolicitacaoCancelamento.conta_chave == chave_conta(usuario.conta_vinculada))
            .order_by(SolicitacaoCancelamento.confirmado_por.is_(None).desc(), SolicitacaoCancelamento.criado_em.desc())
            .all()
        )
        return templates.TemplateResponse(
            request=request,
            name="meus-cancelamentos.html",
            context={"usuario": usuario, "solicitacoes": solicitacoes},
        )


def _listar_usuarios_visiveis(db, usuario_logado):
    """Cada um vê só quem pode gerenciar (admin: todos; supervisor: atendentes e sellers) -- mesma regra de criar/editar/resetar/desativar."""
    query = db.query(Usuario)
    if usuario_logado.papel != "admin":
        query = query.filter(Usuario.papel.in_(PAPEIS_GERENCIAVEIS_POR.get(usuario_logado.papel, ())))
    return query.order_by(Usuario.papel, Usuario.nome_exibicao).all()


@app.get("/usuarios", response_class=HTMLResponse)
def pagina_listar_usuarios(request: Request):
    """
    Lista os usuários -- admin vê todos, supervisor vê atendentes e sellers
    (mesma regra de quem cada um pode criar, aplicada aqui também pra
    quem cada um pode desativar/reativar). O formulário de "Adicionar
    operador" já vem embutido nessa mesma tela.
    """
    with SessionLocal() as db:
        usuario_logado = auth.usuario_atual(request, db)
        if not auth.papel_permite(usuario_logado, ("admin", "supervisor")):
            return RedirectResponse("/", status_code=303)

        usuarios = _listar_usuarios_visiveis(db, usuario_logado)

        return templates.TemplateResponse(
            request=request,
            name="usuarios.html",
            context={"papel_logado": usuario_logado.papel, "usuarios": usuarios, "usuario_logado": usuario_logado},
        )


@app.post("/usuarios/{usuario_id}/alternar-status")
def alternar_status_usuario(usuario_id: int, request: Request):
    """
    Ativa/desativa um usuário (nunca apaga) -- desativado não consegue
    mais logar, mas todo o histórico dele (cancelamentos confirmados,
    por exemplo) continua intacto.
    """
    with SessionLocal() as db:
        usuario_logado = auth.usuario_atual(request, db)
        if not auth.papel_permite(usuario_logado, ("admin", "supervisor")):
            return RedirectResponse("/", status_code=303)

        alvo = db.query(Usuario).filter(Usuario.id == usuario_id).first()
        if alvo is None:
            return RedirectResponse("/usuarios", status_code=303)

        # Supervisor só mexe em atendente e seller; ninguém mexe na própria conta por aqui.
        if alvo.id == usuario_logado.id:
            return RedirectResponse("/usuarios", status_code=303)
        if not _pode_gerenciar(usuario_logado, alvo.papel):
            return RedirectResponse("/usuarios", status_code=303)

        alvo.ativo = not alvo.ativo
        db.commit()

        return RedirectResponse("/usuarios", status_code=303)


@app.post("/usuarios/{usuario_id}/resetar-senha", response_class=HTMLResponse)
def resetar_senha_usuario(usuario_id: int, request: Request):
    """
    Reseta a senha de alguém gerando um NOVO código de primeiro acesso
    -- reaproveita o mesmo fluxo de /primeiro-acesso. A senha antiga
    para de funcionar assim que isso roda (senha_hash vira None).
    """
    with SessionLocal() as db:
        usuario_logado = auth.usuario_atual(request, db)
        if not auth.papel_permite(usuario_logado, ("admin", "supervisor")):
            return RedirectResponse("/", status_code=303)

        alvo = db.query(Usuario).filter(Usuario.id == usuario_id).first()
        if alvo is None:
            return RedirectResponse("/usuarios", status_code=303)

        # Mesma regra de escopo do desativar: supervisor só mexe em atendente e seller.
        if not _pode_gerenciar(usuario_logado, alvo.papel):
            return RedirectResponse("/usuarios", status_code=303)

        codigo = auth.gerar_codigo_primeiro_acesso()
        alvo.senha_hash = None
        alvo.codigo_primeiro_acesso = codigo
        alvo.precisa_trocar_senha = True
        db.commit()

        usuarios = _listar_usuarios_visiveis(db, usuario_logado)

        return templates.TemplateResponse(
            request=request,
            name="usuarios.html",
            context={
                "papel_logado": usuario_logado.papel,
                "usuarios": usuarios,
                "usuario_logado": usuario_logado,
                "reset_sucesso": {"usuario": alvo.usuario, "nome_exibicao": alvo.nome_exibicao, "codigo": codigo},
            },
        )


@app.get("/usuarios/{usuario_id}/editar", response_class=HTMLResponse)
def pagina_editar_usuario(usuario_id: int, request: Request):
    with SessionLocal() as db:
        usuario_logado = auth.usuario_atual(request, db)
        if not auth.papel_permite(usuario_logado, ("admin", "supervisor")):
            return RedirectResponse("/", status_code=303)

        alvo = db.query(Usuario).filter(Usuario.id == usuario_id).first()
        if alvo is None:
            return RedirectResponse("/usuarios", status_code=303)
        if not _pode_gerenciar(usuario_logado, alvo.papel):
            return RedirectResponse("/usuarios", status_code=303)

        return templates.TemplateResponse(
            request=request,
            name="editar-usuario.html",
            context={"papel_logado": usuario_logado.papel, "usuario_logado": usuario_logado, "alvo": alvo, "erro": None},
        )


@app.post("/usuarios/{usuario_id}/editar", response_class=HTMLResponse)
def editar_usuario(
    usuario_id: int,
    request: Request,
    nome_exibicao: str = Form(...),
    papel: str = Form(...),
    conta_vinculada: str = Form(""),
):
    with SessionLocal() as db:
        usuario_logado = auth.usuario_atual(request, db)
        if not auth.papel_permite(usuario_logado, ("admin", "supervisor")):
            return RedirectResponse("/", status_code=303)

        alvo = db.query(Usuario).filter(Usuario.id == usuario_id).first()
        if alvo is None:
            return RedirectResponse("/usuarios", status_code=303)
        if not _pode_gerenciar(usuario_logado, alvo.papel):
            return RedirectResponse("/usuarios", status_code=303)

        # Mesma regra de criar: supervisor só atribui "atendente" ou "seller", mesmo
        # que tentem forçar outro valor mexendo no HTML.
        papeis_permitidos = PAPEIS_GERENCIAVEIS_POR.get(usuario_logado.papel, ())
        contexto_erro = {"papel_logado": usuario_logado.papel, "usuario_logado": usuario_logado, "alvo": alvo}

        if papel not in papeis_permitidos:
            return templates.TemplateResponse(
                request=request, name="editar-usuario.html", status_code=403,
                context={**contexto_erro, "erro": "Você não tem permissão pra atribuir esse papel."},
            )

        nome_limpo = nome_exibicao.strip()
        if not nome_limpo:
            return templates.TemplateResponse(
                request=request, name="editar-usuario.html", status_code=400,
                context={**contexto_erro, "erro": "Informe o nome de exibição."},
            )

        conta_limpa = conta_vinculada.strip() or None
        if papel == "seller" and not conta_limpa:
            return templates.TemplateResponse(
                request=request, name="editar-usuario.html", status_code=400,
                context={**contexto_erro, "erro": "Informe a conta vinculada -- obrigatório pra usuários seller."},
            )

        alvo.nome_exibicao = nome_limpo
        alvo.papel = papel
        alvo.conta_vinculada = conta_limpa if papel == "seller" else None
        db.commit()

        return RedirectResponse("/usuarios", status_code=303)


@app.get("/usuarios/novo", response_class=HTMLResponse)
def pagina_criar_usuario_redirect(request: Request):
    """O formulário de criar agora vive dentro de /usuarios -- esse endereço antigo só redireciona pra lá."""
    return RedirectResponse("/usuarios", status_code=303)


@app.post("/usuarios/novo", response_class=HTMLResponse)
def criar_usuario(
    request: Request,
    usuario: str = Form(...),
    nome_exibicao: str = Form(...),
    papel: str = Form(...),
    conta_vinculada: str = Form(""),
):
    with SessionLocal() as db:
        usuario_logado = auth.usuario_atual(request, db)
        if not auth.papel_permite(usuario_logado, ("admin", "supervisor")):
            return RedirectResponse("/", status_code=303)

        # Supervisor só pode criar atendente ou seller, mesmo que alguém tente forçar
        # outro valor mexendo no HTML -- a checagem de verdade é aqui,
        # no servidor, nunca só no <select> da tela.
        papeis_que_esse_criador_pode_atribuir = PAPEIS_GERENCIAVEIS_POR.get(usuario_logado.papel, ())
        contexto_base = {"papel_logado": usuario_logado.papel, "usuario_logado": usuario_logado, "usuarios": _listar_usuarios_visiveis(db, usuario_logado)}

        if papel not in papeis_que_esse_criador_pode_atribuir:
            return templates.TemplateResponse(
                request=request, name="usuarios.html", status_code=403,
                context={**contexto_base, "erro_criar": "Você não tem permissão pra criar esse papel."},
            )

        # O login é guardado como foi digitado ("Josef"), mas na hora de
        # entrar maiúsculas/minúsculas não importam (_buscar_usuario_por_login).
        usuario_normalizado = usuario.strip()
        if not usuario_normalizado:
            return templates.TemplateResponse(
                request=request, name="usuarios.html", status_code=400,
                context={**contexto_base, "erro_criar": "Informe o nome de usuário."},
            )
        if any(c.isspace() for c in usuario_normalizado):
            return templates.TemplateResponse(
                request=request, name="usuarios.html", status_code=400,
                context={**contexto_base, "erro_criar": "O login não pode ter espaços."},
            )

        # "Josef" e "josef" seriam o mesmo login -- não deixa duplicar.
        if db.query(Usuario).filter(func.lower(Usuario.usuario) == usuario_normalizado.lower()).first() is not None:
            return templates.TemplateResponse(
                request=request, name="usuarios.html", status_code=400,
                context={**contexto_base, "erro_criar": f"Já existe um usuário com o login '{usuario_normalizado}'."},
            )

        conta_vinculada_limpa = conta_vinculada.strip() or None
        if papel == "seller" and not conta_vinculada_limpa:
            return templates.TemplateResponse(
                request=request, name="usuarios.html", status_code=400,
                context={**contexto_base, "erro_criar": "Informe a conta vinculada -- obrigatório pra usuários seller."},
            )

        codigo = auth.gerar_codigo_primeiro_acesso()
        novo_usuario = Usuario(
            usuario=usuario_normalizado,
            nome_exibicao=nome_exibicao.strip(),
            papel=papel,
            codigo_primeiro_acesso=codigo,
            precisa_trocar_senha=True,
            conta_vinculada=conta_vinculada_limpa if papel == "seller" else None,
            criado_por_usuario_id=usuario_logado.id,
        )
        db.add(novo_usuario)
        db.commit()

        return templates.TemplateResponse(
            request=request,
            name="usuarios.html",
            context={
                "papel_logado": usuario_logado.papel,
                "usuario_logado": usuario_logado,
                "usuarios": _listar_usuarios_visiveis(db, usuario_logado),
                "criar_sucesso": {"usuario": novo_usuario.usuario, "nome_exibicao": novo_usuario.nome_exibicao, "papel": novo_usuario.papel, "codigo": codigo},
            },
        )


@app.get("/solicitacoes-painel", response_class=HTMLResponse)
def pagina_solicitacoes_painel(request: Request):
    """Tela interna de acompanhamento das solicitações manuais de cancelamento, agrupada por conta."""
    return templates.TemplateResponse(request=request, name="painel-cancelamentos.html", context={"usuario_logado": _usuario_logado(request)})


@app.get("/relatorio-cancelamentos", response_class=HTMLResponse)
def pagina_relatorio_cancelamentos(request: Request):
    """Relatório de cancelamentos por conta e período (imprimir / PDF / Excel). Admin, supervisor e atendente."""
    usuario = _usuario_logado(request)
    if not usuario or usuario.papel not in ("admin", "supervisor", "atendente"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request=request, name="relatorio-cancelamentos.html", context={"usuario_logado": usuario})


@app.get("/api/saude")
def saude():
    """Endpoint simples pra confirmar que o backend está de pé."""
    return {"status": "ok"}


def _redirecionar_por_papel(papel: str) -> RedirectResponse:
    destino = _PAGINA_INICIAL_POR_PAPEL.get(papel, "/")
    return RedirectResponse(destino, status_code=303)


@app.get("/login", response_class=HTMLResponse)
def pagina_login(request: Request):
    # Se já está logado, não faz sentido mostrar o login de novo.
    with SessionLocal() as db:
        usuario = auth.usuario_atual(request, db)
        if usuario:
            return _redirecionar_por_papel(usuario.papel)
    return templates.TemplateResponse(request=request, name="login.html", context={"erro": None, "proximo": _proximo_seguro(request.query_params.get("proximo", ""))})


def _buscar_usuario_por_login(db, login: str):
    """
    Acha o usuário pelo login IGNORANDO maiúsculas/minúsculas ("Josef",
    "josef" e "JOSEF" são a mesma pessoa). Só usuários ativos. Se por
    acaso existirem dois que diferem só na caixa (cadastros antigos),
    prefere o que bate exatamente com o digitado.
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


def _proximo_seguro(proximo: str) -> str:
    """Só aceita caminho interno ("/algo"): nunca manda pra outro site depois do login."""
    proximo = (proximo or "").strip()
    if proximo.startswith("/") and not proximo.startswith("//") and "\\" not in proximo and proximo != "/login":
        return proximo
    return ""


@app.post("/login")
def fazer_login(request: Request, usuario: str = Form(...), senha: str = Form(...), proximo: str = Form("")):
    proximo = _proximo_seguro(proximo)
    db = SessionLocal()
    try:
        conta = _buscar_usuario_por_login(db, usuario)

        if conta is None or not conta.senha_hash:
            erro = "Usuário ou senha incorretos." if conta is None else "Essa conta ainda não concluiu o primeiro acesso — use o link 'Criar sua senha'."
            return templates.TemplateResponse(request=request, name="login.html", context={"erro": erro, "proximo": proximo}, status_code=401)

        if not auth.verificar_senha(senha, conta.senha_hash):
            return templates.TemplateResponse(request=request, name="login.html", context={"erro": "Usuário ou senha incorretos.", "proximo": proximo}, status_code=401)

        # Voltou pra tela que queria abrir; se o perfil não puder, o próprio
        # controle de acesso manda pra página inicial dele.
        resposta = RedirectResponse(proximo, status_code=303) if proximo else _redirecionar_por_papel(conta.papel)
        duracao = auth.duracao_sessao(conta.papel)
        token = auth.criar_token_sessao(conta.id, duracao)
        resposta.set_cookie(auth.COOKIE_SESSAO, token, httponly=True, samesite="lax", max_age=duracao)
        return resposta
    finally:
        db.close()


@app.get("/logout")
def fazer_logout():
    resposta = RedirectResponse("/login", status_code=303)
    resposta.delete_cookie(auth.COOKIE_SESSAO)
    return resposta


@app.get("/primeiro-acesso", response_class=HTMLResponse)
def pagina_primeiro_acesso(request: Request):
    return templates.TemplateResponse(request=request, name="primeiro-acesso.html", context={"erro": None, "usuario_preenchido": None})


@app.post("/primeiro-acesso")
def concluir_primeiro_acesso(
    request: Request,
    usuario: str = Form(...),
    codigo: str = Form(...),
    nova_senha: str = Form(...),
    confirmar_senha: str = Form(...),
):
    contexto_erro = {"usuario_preenchido": usuario}

    if nova_senha != confirmar_senha:
        contexto_erro["erro"] = "As duas senhas não são iguais."
        return templates.TemplateResponse(request=request, name="primeiro-acesso.html", context=contexto_erro, status_code=400)
    if len(nova_senha) < 8:
        contexto_erro["erro"] = "A senha precisa ter pelo menos 8 caracteres."
        return templates.TemplateResponse(request=request, name="primeiro-acesso.html", context=contexto_erro, status_code=400)

    db = SessionLocal()
    try:
        conta = _buscar_usuario_por_login(db, usuario)
        codigo_confere = conta is not None and conta.codigo_primeiro_acesso and conta.codigo_primeiro_acesso.upper() == codigo.strip().upper()

        if not codigo_confere:
            contexto_erro["erro"] = "Usuário ou código incorretos."
            return templates.TemplateResponse(request=request, name="primeiro-acesso.html", context=contexto_erro, status_code=401)

        conta.senha_hash = auth.gerar_hash_senha(nova_senha)
        conta.codigo_primeiro_acesso = None
        conta.precisa_trocar_senha = False
        db.commit()

        resposta = _redirecionar_por_papel(conta.papel)
        duracao = auth.duracao_sessao(conta.papel)
        token = auth.criar_token_sessao(conta.id, duracao)
        resposta.set_cookie(auth.COOKIE_SESSAO, token, httponly=True, samesite="lax", max_age=duracao)
        return resposta
    finally:
        db.close()
