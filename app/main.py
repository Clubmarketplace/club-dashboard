from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app.database import Base, engine, SessionLocal
from app.models import Usuario, SolicitacaoCancelamento
from app import auth, config
from app.routers import devolucoes, relatorio_conta, auth_ml, webhook_ml, pre_venda, manuais, pos_venda, cancelamentos, eventos_webhook, solicitacoes_cancelamento

# Cria as tabelas no banco se ainda não existirem (em produção, o ideal
# é usar uma ferramenta de migração como Alembic, mas isso é suficiente
# pra essa fase inicial).
Base.metadata.create_all(bind=engine)


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

# --- Middleware de autenticação ---
# Tudo exige login por padrão. As exceções abaixo são as páginas/APIs
# que PRECISAM ficar abertas: as telas de TV (ficam ligadas o dia
# inteiro, sem ninguém logado), o formulário público de cancelamento
# (link compartilhado com todas as contas, sem senha), o webhook do
# Mercado Livre (quem chama é o Mercado Livre, não uma pessoa), e o
# callback de OAuth.
_PUBLICO_EXATO = {
    ("GET", "/login"), ("POST", "/login"),
    ("GET", "/logout"),
    ("GET", "/primeiro-acesso"), ("POST", "/primeiro-acesso"),
    ("GET", "/solicitar-cancelamento"),
    ("POST", "/api/solicitacoes-cancelamento"),
    ("GET", "/painel-tv/geral"), ("GET", "/painel-tv/fila"),
    ("GET", "/api/pre-venda/fila"), ("GET", "/api/pre-venda/painel-geral"),
    ("POST", "/webhook/mercado-livre"),
    ("GET", "/auth/ml/callback"),
    ("GET", "/api/saude"),
}
_PUBLICO_PREFIXOS = ("/static/",)


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
        return RedirectResponse("/login", status_code=303)

    # Seller só pode ver as próprias telas (Meus Cancelamentos e o
    # formulário de solicitar, que já é público de qualquer forma) --
    # qualquer outra rota interna é bloqueada, mesmo sabendo a URL.
    _ROTAS_PERMITIDAS_PARA_SELLER = {"/meus-cancelamentos", "/logout"}
    if caminho not in _ROTAS_PERMITIDAS_PARA_SELLER:
        with SessionLocal() as db:
            usuario_logado = db.query(Usuario).filter(Usuario.id == usuario_id).first()
        if usuario_logado and usuario_logado.papel == "seller":
            if caminho.startswith("/api/"):
                return JSONResponse({"detail": "Acesso restrito"}, status_code=403)
            return RedirectResponse("/meus-cancelamentos", status_code=303)

    return await call_next(request)

app.include_router(devolucoes.router)
app.include_router(relatorio_conta.router)
app.include_router(auth_ml.router)
app.include_router(webhook_ml.router)
app.include_router(pre_venda.router)
app.include_router(manuais.router)
app.include_router(pos_venda.router)
app.include_router(cancelamentos.router)
app.include_router(eventos_webhook.router)
app.include_router(solicitacoes_cancelamento.router)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.get("/", response_class=HTMLResponse)
def pagina_inicial(request: Request):
    return templates.TemplateResponse(request=request, name="dashboard.html")


@app.get("/contas", response_class=HTMLResponse)
def pagina_contas(request: Request):
    return templates.TemplateResponse(request=request, name="contas.html")


@app.get("/pre-venda", response_class=HTMLResponse)
def pagina_pre_venda(request: Request):
    return templates.TemplateResponse(request=request, name="pre-venda.html")


@app.get("/manuais", response_class=HTMLResponse)
def pagina_manuais(request: Request):
    return templates.TemplateResponse(request=request, name="manuais.html")


@app.get("/pos-venda", response_class=HTMLResponse)
def pagina_pos_venda(request: Request):
    return templates.TemplateResponse(request=request, name="pos-venda.html")


@app.get("/cancelamentos", response_class=HTMLResponse)
def pagina_cancelamentos(request: Request):
    return templates.TemplateResponse(request=request, name="cancelamentos.html")


@app.get("/cancelamentos-painel", response_class=HTMLResponse)
def pagina_painel_cancelamentos(request: Request):
    """Tela interna de acompanhamento dos pedidos de cancelamento, agrupada por conta."""
    return templates.TemplateResponse(request=request, name="painel-cancelamentos.html")


@app.get("/eventos-webhook", response_class=HTMLResponse)
def pagina_eventos_webhook(request: Request):
    return templates.TemplateResponse(request=request, name="eventos-webhook.html")


@app.get("/painel-tv/geral", response_class=HTMLResponse)
def pagina_painel_tv_geral(request: Request):
    """Tela de TV 1 — visão geral agregada, pra bater o olho de longe."""
    return templates.TemplateResponse(request=request, name="painel-tv-geral.html")


@app.get("/painel-tv/fila", response_class=HTMLResponse)
def pagina_painel_tv_fila(request: Request):
    """Tela de TV 2 — fila de ação, só o que precisa de humano, cross-conta."""
    return templates.TemplateResponse(request=request, name="painel-tv-fila.html")


@app.get("/solicitar-cancelamento", response_class=HTMLResponse)
def pagina_solicitar_cancelamento(request: Request):
    """
    Página pública (sem login) pra qualquer conta registrar manualmente
    um pedido de cancelamento. Se quem está acessando for um seller
    logado, a conta dele já vem pré-preenchida (sem precisar digitar).
    """
    with SessionLocal() as db:
        usuario = auth.usuario_atual(request, db)
        conta_pre_preenchida = usuario.conta_vinculada if (usuario and usuario.papel == "seller") else None
    return templates.TemplateResponse(
        request=request,
        name="solicitar-cancelamento.html",
        context={"conta_pre_preenchida": conta_pre_preenchida, "seller_logado": bool(conta_pre_preenchida)},
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
            .filter(SolicitacaoCancelamento.conta == usuario.conta_vinculada)
            .order_by(SolicitacaoCancelamento.criado_em.desc())
            .all()
        )
        return templates.TemplateResponse(
            request=request,
            name="meus-cancelamentos.html",
            context={"usuario": usuario, "solicitacoes": solicitacoes},
        )


@app.get("/usuarios/novo", response_class=HTMLResponse)
def pagina_criar_usuario(request: Request):
    with SessionLocal() as db:
        usuario_logado = auth.usuario_atual(request, db)
        if not auth.papel_permite(usuario_logado, ("admin", "supervisor")):
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="criar-usuario.html",
            context={"papel_logado": usuario_logado.papel, "erro": None, "sucesso": None, "valores": None},
        )


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

        # Supervisor só pode criar seller, mesmo que alguém tente forçar
        # outro valor mexendo no HTML -- a checagem de verdade é aqui,
        # no servidor, nunca só no <select> da tela.
        papeis_que_esse_criador_pode_atribuir = ("admin", "supervisor", "seller") if usuario_logado.papel == "admin" else ("seller",)
        contexto_base = {"papel_logado": usuario_logado.papel, "sucesso": None}
        valores_preenchidos = {"usuario": usuario, "nome_exibicao": nome_exibicao, "papel": papel, "conta_vinculada": conta_vinculada}

        if papel not in papeis_que_esse_criador_pode_atribuir:
            return templates.TemplateResponse(
                request=request, name="criar-usuario.html", status_code=403,
                context={**contexto_base, "erro": "Você não tem permissão pra criar esse papel.", "valores": valores_preenchidos},
            )

        usuario_normalizado = usuario.strip().lower()
        if not usuario_normalizado:
            return templates.TemplateResponse(
                request=request, name="criar-usuario.html", status_code=400,
                context={**contexto_base, "erro": "Informe o nome de usuário.", "valores": valores_preenchidos},
            )

        if db.query(Usuario).filter(Usuario.usuario == usuario_normalizado).first() is not None:
            return templates.TemplateResponse(
                request=request, name="criar-usuario.html", status_code=400,
                context={**contexto_base, "erro": f"Já existe um usuário com o login '{usuario_normalizado}'.", "valores": valores_preenchidos},
            )

        conta_vinculada_limpa = conta_vinculada.strip() or None
        if papel == "seller" and not conta_vinculada_limpa:
            return templates.TemplateResponse(
                request=request, name="criar-usuario.html", status_code=400,
                context={**contexto_base, "erro": "Informe a conta vinculada -- obrigatório pra usuários seller.", "valores": valores_preenchidos},
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
            name="criar-usuario.html",
            context={
                "papel_logado": usuario_logado.papel,
                "erro": None,
                "valores": None,
                "sucesso": {"usuario": novo_usuario.usuario, "nome_exibicao": novo_usuario.nome_exibicao, "papel": novo_usuario.papel, "codigo": codigo},
            },
        )


@app.get("/solicitacoes-painel", response_class=HTMLResponse)
def pagina_solicitacoes_painel(request: Request):
    """Tela interna de acompanhamento das solicitações manuais de cancelamento, agrupada por conta."""
    return templates.TemplateResponse(request=request, name="painel-cancelamentos.html")


@app.get("/api/saude")
def saude():
    """Endpoint simples pra confirmar que o backend está de pé."""
    return {"status": "ok"}


def _redirecionar_por_papel(papel: str) -> RedirectResponse:
    destino = "/meus-cancelamentos" if papel == "seller" else "/"
    return RedirectResponse(destino, status_code=303)


@app.get("/login", response_class=HTMLResponse)
def pagina_login(request: Request):
    # Se já está logado, não faz sentido mostrar o login de novo.
    with SessionLocal() as db:
        usuario = auth.usuario_atual(request, db)
        if usuario:
            return _redirecionar_por_papel(usuario.papel)
    return templates.TemplateResponse(request=request, name="login.html", context={"erro": None})


@app.post("/login")
def fazer_login(request: Request, usuario: str = Form(...), senha: str = Form(...)):
    db = SessionLocal()
    try:
        conta = db.query(Usuario).filter(Usuario.usuario == usuario.strip(), Usuario.ativo.is_(True)).first()

        if conta is None or not conta.senha_hash:
            erro = "Usuário ou senha incorretos." if conta is None else "Essa conta ainda não concluiu o primeiro acesso — use o link 'Criar sua senha'."
            return templates.TemplateResponse(request=request, name="login.html", context={"erro": erro}, status_code=401)

        if not auth.verificar_senha(senha, conta.senha_hash):
            return templates.TemplateResponse(request=request, name="login.html", context={"erro": "Usuário ou senha incorretos."}, status_code=401)

        resposta = _redirecionar_por_papel(conta.papel)
        token = auth.criar_token_sessao(conta.id)
        resposta.set_cookie(auth.COOKIE_SESSAO, token, httponly=True, samesite="lax", max_age=auth.DURACAO_SESSAO_SEGUNDOS)
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
        conta = db.query(Usuario).filter(Usuario.usuario == usuario.strip(), Usuario.ativo.is_(True)).first()
        codigo_confere = conta is not None and conta.codigo_primeiro_acesso and conta.codigo_primeiro_acesso.upper() == codigo.strip().upper()

        if not codigo_confere:
            contexto_erro["erro"] = "Usuário ou código incorretos."
            return templates.TemplateResponse(request=request, name="primeiro-acesso.html", context=contexto_erro, status_code=401)

        conta.senha_hash = auth.gerar_hash_senha(nova_senha)
        conta.codigo_primeiro_acesso = None
        conta.precisa_trocar_senha = False
        db.commit()

        resposta = _redirecionar_por_papel(conta.papel)
        token = auth.criar_token_sessao(conta.id)
        resposta.set_cookie(auth.COOKIE_SESSAO, token, httponly=True, samesite="lax", max_age=auth.DURACAO_SESSAO_SEGUNDOS)
        return resposta
    finally:
        db.close()
