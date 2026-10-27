from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app.database import Base, engine, SessionLocal, garantir_estrutura_atualizada
from datetime import datetime, timedelta, timezone
from sqlalchemy import func, or_
from app.models import Usuario, SolicitacaoCancelamento, Devolucao, Pergunta, Conta, ProdutoCadastro
from app import auth, config
from app.contas_util import chave_conta
from sqlalchemy.orm import defer
from app.fotos_armazenamento import listar_fotos, obter_bytes, solicitacao_da_foto
from app.routers.solicitacoes_cancelamento import n_fotos_sql, PROBLEMAS_GALPAO, ESTADOS_CHEGADA, CHECKLIST_REVERSO, GALPOES, MAX_FOTOS_REVERSO, MOTIVOS_NAO_VOLTOU, PLATAFORMAS, RESULTADOS_REVERSO, itens_do_checklist
from app.routers import devolucoes, relatorio_conta, auth_ml, webhook_ml, pre_venda, manuais, pos_venda, cancelamentos, eventos_webhook, solicitacoes_cancelamento, diagnostico_sku, reputacao as reputacao_rotas, respostas_padrao as respostas_padrao_rotas, calibrar_ia as calibrar_ia_rotas, cmx as cmx_rotas, custos_painel as custos_painel_rotas, produtos as produtos_rotas, vendas as vendas_rotas, taxas as taxas_rotas, historico_adesoes_painel as historico_adesoes_painel_rotas, admin_painel as admin_painel_rotas, dashboard_painel as dashboard_painel_rotas, cadastro_produtos as cadastro_produtos_rotas, cadastro_seller as cadastro_seller_rotas, painel_galpao as painel_galpao_rotas, reversos_gestor as reversos_gestor_rotas

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


# --- Tarefa em segundo plano: reforço silencioso de vendas (ver
# app/reforco_vendas.py). Passa pelas contas ativas, uma a uma, com
# pausa entre cada, sincronizando os últimos 2 dias direto no Mercado
# Livre -- cobre o caso de webhook perdido (ex: depois de reautorizar
# uma conta) sem depender de ninguém abrir o painel do admin ou do
# seller. Desliga com REFORCO_VENDAS_ATIVO=0.
@app.on_event("startup")
async def _iniciar_reforco_vendas() -> None:
    import asyncio
    import logging
    import os

    if os.getenv("REFORCO_VENDAS_ATIVO", "1").strip() in ("0", "false", "nao", "não"):
        logging.getLogger(__name__).info("Reforço silencioso de vendas DESLIGADO pela variável de ambiente.")
        return
    from app.reforco_vendas import loop_reforco_vendas
    app.state.tarefa_reforco_vendas = asyncio.create_task(loop_reforco_vendas())
    logging.getLogger(__name__).info("Reforço silencioso de vendas iniciado (ciclo contínuo, últimos 2 dias por conta).")

# --- Tarefa em segundo plano: fotos (ver app/fotos_armazenamento.py). A cada ~90 s move
# as fotos do banco para o Cloudflare R2 (se as variáveis R2_* existirem), converte as
# fotos antigas e aplica o prazo de guarda (FOTOS_DIAS_GUARDA; 0 = nunca apaga).
# Desliga com FOTOS_TAREFA_ATIVA=0.
@app.on_event("startup")
async def _iniciar_tarefa_fotos() -> None:
    import asyncio
    import logging
    import os

    if os.getenv("FOTOS_TAREFA_ATIVA", "1").strip() in ("0", "false", "nao", "não"):
        logging.getLogger(__name__).info("Tarefa de fotos DESLIGADA pela variável de ambiente.")
        return
    from app.fotos_armazenamento import loop_fotos, r2_configurado
    app.state.tarefa_fotos = asyncio.create_task(loop_fotos())
    logging.getLogger(__name__).info("Tarefa de fotos iniciada (R2 %s).", "LIGADO" if r2_configurado() else "não configurado: fotos ficam no banco")

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
    ("POST", "/api/operador/produtos-novos"),  # Operador Logístico (Apps Script) manda os produtos novos; token conferido dentro da rota
    ("GET", "/auth/ml/callback"),
    ("GET", "/api/saude"),
    ("GET", "/privacidade-extensao"),
    ("GET", "/favicon.ico"),
}
# /api/cmx/ (extensão ClubMarketplaceX) também fica de fora daqui: ela não
# usa o cookie de sessão do painel, manda o token no cabeçalho
# "Authorization: Bearer ..." -- a verificação desse token acontece dentro
# de cada rota (ver app/routers/cmx.py:usuario_logado_cmx), não aqui no
# middleware. Sem essa exceção, toda chamada da extensão cairia como
# "Não autenticado" antes mesmo de chegar na rota.
_PUBLICO_PREFIXOS = ("/static/", "/api/cmx/")


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
            "/api/pre-venda/fila", "/api/pre-venda/painel-geral", "/api/pre-venda/painel-resumo", "/api/pre-venda/painel-detalhe",
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
    # "/relatorio-cancelamentos" NÃO entra na lista de páginas aqui, de
    # propósito -- é relatório gerencial (só gerente_setor/admin/supervisor/
    # atendente), igual à Produtividade do Cadastro logo abaixo. Se entrasse
    # aqui, viraria candidata a "página inicial" de QUALQUER operador/
    # gerente_setor com esse setor (15/10 -- foi exatamente isso que causou
    # o loop de redirecionamento). Acesso de verdade é liberado à parte, em
    # _PAGINAS_SO_GERENCIA (logo abaixo de _papel_pode_acessar).
    "solicitacoes_cancelamento": {
        "paginas": {"/solicitacoes-painel", "/solicitar-cancelamento", "/api/solicitacoes-cancelamento"},
        "prefixos_api": ("/api/solicitacoes-cancelamento/",),
    },
    # Time de Cadastro (14/10): a tela de produtos novos e a API dela. A
    # tela de produtividade (/cadastro/produtividade) NÃO entra aqui de
    # propósito, pelo mesmo motivo acima -- acesso liberado à parte em
    # _PAGINAS_SO_GERENCIA.
    "cadastro": {"paginas": {"/cadastro"}, "prefixos_api": ("/api/cadastro/",)},
    # Galpão: registrar (o formulário e o envio já são públicos) e
    # acompanhar. Só endereços EXATOS -- de propósito, o /confirmar
    # fica de fora (quem pede não confirma).
    "logistica": {
        "paginas": {
            "/solicitacoes-galpao", "/painel-galpao",
            "/api/painel-galpao/resumo", "/api/painel-galpao/relatorio",
            "/solicitar-cancelamento", "/reverso", "/api/solicitacoes-cancelamento",
            "/api/solicitacoes-cancelamento/busca",
            "/api/solicitacoes-cancelamento/contas",
            "/api/solicitacoes-cancelamento/opcoes",
            "/api/solicitacoes-cancelamento/fotos",   # endereços das fotos de um pedido (sob demanda)
            "/api/solicitacoes-cancelamento/foto",    # os bytes de uma foto/miniatura
        },
        # (26/10) O galpão só REGISTRA o reverso e acompanha; quem conclui
        # ("voltou para o estoque") é o operador, em /api/solicitacoes-cancelamento/{id}/reverso/concluir.
        "prefixos_api": (),
    },
}

# Perfis com acesso RESTRITO: só entram no que está listado aqui (lista
# do que PODE -- tela nova nasce bloqueada pra eles até alguém liberar).
# Admin e supervisor não aparecem aqui porque têm acesso amplo.
_AREAS_POR_PAPEL = {
    "atendente": ("pre_venda", "pos_venda", "paineis_tv", "solicitacoes_cancelamento", "reputacao", "respostas_padrao"),
    "logistica": ("logistica",),
    # Cadastro (14/10): só a tela "Cadastro" -- cada pessoa do time com o seu login.
    "cadastro": ("cadastro",),
    # TV: só enxerga os painéis (não responde nada, não abre outras telas).
    "tv": ("paineis_tv",),
}

# Setores "soltos" (15/10): em vez de inventar um papel novo pra cada
# combinação de telas, "operador" e "gerente_setor" marcam pessoa por pessoa
# (Usuario.setores, ver editar/criar usuário) quais desses setores têm --
# a chave de cada um é o mesmo nome de área em _AREAS. Pra abrir um setor
# novo no futuro, basta acrescentar uma linha aqui (e a área em _AREAS, se
# ainda não existir) -- nada mais no sistema precisa mudar.
SETORES_DISPONIVEIS = {
    "cadastro": "Cadastro",
    "solicitacoes_cancelamento": "Cancelamento",
}

# Todo papel de acesso restrito (só abre o que está liberado explicitamente):
# os "clássicos" de cima (lista fixa) + os dois novos (lista vem de
# usuario.setores). Admin e supervisor não entram aqui -- acesso amplo.
_PAPEIS_RESTRITOS = set(_AREAS_POR_PAPEL) | {"operador", "gerente_setor"}


def _areas_do_usuario(usuario) -> tuple:
    """Quais áreas (ver _AREAS) esse usuário pode abrir -- só chamado pra
    quem tem papel restrito (ver _PAPEIS_RESTRITOS)."""
    if usuario is None:
        return ()
    if usuario.papel in _AREAS_POR_PAPEL:
        return _AREAS_POR_PAPEL[usuario.papel]
    if usuario.papel in ("operador", "gerente_setor"):
        return tuple(s for s in auth.setores_do_usuario(usuario) if s in SETORES_DISPONIVEIS)
    return ()


# Papéis que entram no PAINEL DO SELLER (telas /vendas, /custos, /taxas...).
#  - "seller": o dono da conta.
#  - "suporte": perfil interno pra testar/acompanhar o painel usando a
#    conta de um seller (ex: Washington entrando como a Velasco), sem
#    ter que logar com o acesso do próprio seller. Vê exatamente o que o
#    seller vê (inclusive telas em teste, como Vendas), nada de admin.
#    Também usa conta_vinculada; só admin cria.
# Toda regra "é seller?" do painel deve usar esta tupla, não comparar
# com "seller" direto -- senão o suporte cai fora sem querer.
PAPEIS_PAINEL_SELLER = ("seller", "suporte")

_PAGINA_INICIAL_POR_PAPEL = {
    # Seller volta pra "Meus cancelamentos" enquanto a tela Vendas está em
    # ajuste (ela era a inicial; quando liberar de novo, troque pra "/vendas").
    "seller": "/meus-cancelamentos",
    "suporte": "/vendas",
    "atendente": "/pre-venda",
    "logistica": "/painel-galpao",
    "tv": "/painel-tv/fila",
    "cadastro": "/cadastro",
}

# Quais perfis cada papel pode criar/editar/resetar/desativar. Usado em
# TODAS as rotas de operadores -- a checagem de verdade é sempre aqui,
# no servidor, nunca só no <select> da tela.
PAPEIS_GERENCIAVEIS_POR = {
    "admin": ("admin", "supervisor", "gerente_setor", "operador", "atendente", "logistica", "cadastro", "seller", "suporte", "tv"),
    # Supervisor continua sem poder criar/editar admin, supervisor ou suporte --
    # mesma fronteira de sempre (permissão dele é sempre menor que a do admin),
    # só com os dois papéis novos de setor somados à lista que ele já gerenciava.
    "supervisor": ("gerente_setor", "operador", "atendente", "logistica", "cadastro", "seller", "tv"),
}


def _eh_painel_seller(usuario) -> bool:
    """True se o usuário logado navega pelo painel do seller (seller ou suporte) com conta vinculada."""
    return bool(usuario and usuario.papel in PAPEIS_PAINEL_SELLER and usuario.conta_vinculada)


def _conta_logada_de(usuario):
    """Apelido da conta que as telas do painel do seller devem mostrar (None pros outros perfis)."""
    return usuario.conta_vinculada if _eh_painel_seller(usuario) else None


# Páginas "de gestão" (relatório/produtividade) que ficam de propósito
# FORA de _AREAS[...]["paginas"] (ver comentário lá em cima) -- cada uma
# mapeia pro setor que ela pertence. Quem abre: admin/supervisor (acesso
# livre, nem passa por aqui), "gerente_setor" com o setor marcado, e quem
# estiver em _ATENDENTE_LIBERADO_EM (acesso clássico, de antes de existir
# setor, preservado como sempre foi).
_PAGINAS_SO_GERENCIA = {
    "/cadastro/produtividade": "cadastro",
    "/relatorio-cancelamentos": "solicitacoes_cancelamento",
    "/produtividade-cancelamento": "solicitacoes_cancelamento",
}
_ATENDENTE_LIBERADO_EM = {"/relatorio-cancelamentos"}  # atendente já via isso antes dos setores existirem


def _papel_pode_acessar(usuario_logado, caminho: str) -> bool:
    """Diz se um perfil restrito (ex: atendente, operador, gerente_setor) pode abrir esse caminho."""
    if caminho == "/logout":
        return True
    if caminho in _PAGINAS_SO_GERENCIA:
        if usuario_logado.papel == "atendente" and caminho in _ATENDENTE_LIBERADO_EM:
            return True
        setor = _PAGINAS_SO_GERENCIA[caminho]
        return usuario_logado.papel == "gerente_setor" and setor in auth.setores_do_usuario(usuario_logado)
    for nome_area in _areas_do_usuario(usuario_logado):
        area = _AREAS[nome_area]
        if caminho in area["paginas"] or any(caminho.startswith(p) for p in area["prefixos_api"]):
            return True
    return False


def _pode_gerenciar(usuario_logado, papel_alvo: str) -> bool:
    """True se quem está logado pode mexer num operador com esse papel."""
    return bool(usuario_logado) and papel_alvo in PAPEIS_GERENCIAVEIS_POR.get(usuario_logado.papel, ())


def _pagina_inicial(usuario) -> str:
    """Pra onde mandar alguém ao entrar (ou quando tenta abrir algo que não
    pode). Papéis clássicos têm destino fixo (_PAGINA_INICIAL_POR_PAPEL);
    "operador" e "gerente_setor" variam pessoa por pessoa, então usa a
    primeira página do primeiro setor que a pessoa tiver marcado -- sem
    isso ela cairia numa home que o próprio perfil restrito não pode abrir
    e entraria num loop de redirecionamento."""
    if usuario is None:
        return "/logout"
    destino = _PAGINA_INICIAL_POR_PAPEL.get(usuario.papel)
    if destino:
        return destino
    for nome_area in _areas_do_usuario(usuario):
        for pagina in sorted(_AREAS[nome_area]["paginas"]):
            if not pagina.startswith("/api/"):
                return pagina
    # Papel restrito sem nenhum setor marcado ainda: evita cair numa home
    # bloqueada -- fica no login até alguém liberar um setor pra ele.
    return "/logout" if usuario.papel in _PAPEIS_RESTRITOS else "/"


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
        "/dashboard",
        "/meus-cancelamentos", "/meus-cancelamentos/fotos", "/meus-cancelamentos/foto", "/logout",
        "/solicitar-cancelamento", "/api/solicitacoes-cancelamento", "/api/solicitacoes-cancelamento/contas",
        "/reverso",  # avisar um reverso (26/10) -- a conta é sempre a do próprio seller
        # "Cadastro", dentro de "Minhas solicitações" -- pedir cadastro de produto novo (15/10).
        "/cadastro-seller",
        "/extensao",
        "/custos", "/api/painel/custos",
        "/produtos", "/produtos/lista", "/api/painel/produtos/lista",
        "/taxas",
        "/historico-adesoes", "/api/painel/historico-adesoes",
        # Tela "Vendas" (Resumo + Consulta): liberada pra seller e suporte
        # (07/10) -- antes só o "suporte" entrava, pra testar com a conta
        # do seller primeiro.
        "/vendas", "/vendas/consulta",
    }

    with SessionLocal() as db:
        usuario_logado = db.query(Usuario).filter(Usuario.id == usuario_id).first()
    papel = usuario_logado.papel if usuario_logado else None

    _seller_liberado = (
        caminho in _ROTAS_PERMITIDAS_PARA_SELLER
        or caminho.startswith("/api/painel/custos/")
        or caminho.startswith("/api/painel/taxas")
        or caminho.startswith("/api/painel/vendas")
        or caminho.startswith("/api/painel/dashboard")
        or caminho.startswith("/api/painel/cadastro")
    )

    # Seller e suporte passam pelas mesmas portas (a diferença é só quem é a pessoa).
    if papel in PAPEIS_PAINEL_SELLER and not _seller_liberado:
        if caminho.startswith("/api/"):
            return JSONResponse({"detail": "Acesso restrito"}, status_code=403)
        return RedirectResponse(_pagina_inicial(usuario_logado), status_code=303)

    if papel in _PAPEIS_RESTRITOS and not _papel_pode_acessar(usuario_logado, caminho):
        if caminho.startswith("/api/"):
            return JSONResponse({"detail": "Acesso restrito"}, status_code=403)
        return RedirectResponse(_pagina_inicial(usuario_logado), status_code=303)

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
app.include_router(calibrar_ia_rotas.router)
app.include_router(cmx_rotas.router)  # login/validação da extensão ClubMarketplaceX
app.include_router(custos_painel_rotas.router)  # tela "Custos" do painel (seller, cookie)
app.include_router(produtos_rotas.router)  # tela "Produtos > Lista" do painel (seller, cookie)
app.include_router(vendas_rotas.router)  # tela "Vendas" do painel (seller, cookie)
app.include_router(taxas_rotas.router)  # tela "Taxas" do painel (seller, cookie)
app.include_router(historico_adesoes_painel_rotas.router)  # tela "Histórico de adesões" do painel (seller, cookie)
app.include_router(admin_painel_rotas.router)  # resumo de vendas de todas as contas -- só admin (12/10)
app.include_router(dashboard_painel_rotas.router)  # tela "Dashboard" do painel do seller (12/10)
app.include_router(cadastro_produtos_rotas.router)  # produtos novos das notas do Operador Logístico, pra tela "Cadastro" (13/10)
app.include_router(painel_galpao_rotas.router)  # números e relatório do galpão (26/10)
app.include_router(reversos_gestor_rotas.router)  # tela "Reversos" do gestor -- só admin/supervisor (27/10)
app.include_router(cadastro_seller_rotas.router)  # "Solicitar cadastro" dentro de "Minhas solicitações" do seller (15/10)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


# O navegador pede "/favicon.ico" direto na raiz por padrão, sem olhar pro
# HTML -- por isso é uma rota própria (não dá só pra confiar num <link> no
# <head> de cada template). Fica liberado em _PUBLICO_EXATO acima, senão
# cai no redirect de login antes mesmo de mostrar o ícone na tela de login.
@app.get("/favicon.ico")
def favicon():
    return FileResponse("app/static/favicon.ico")

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
templates.env.globals["RESULTADOS_REVERSO"] = RESULTADOS_REVERSO
templates.env.globals["MOTIVOS_NAO_VOLTOU"] = MOTIVOS_NAO_VOLTOU
templates.env.globals["CHECKLIST_REVERSO"] = CHECKLIST_REVERSO
templates.env.globals["PROBLEMAS_GALPAO"] = PROBLEMAS_GALPAO
templates.env.globals["MAX_FOTOS_REVERSO"] = MAX_FOTOS_REVERSO
templates.env.globals["ESTADOS_CHEGADA"] = ESTADOS_CHEGADA


def _fotos_lista(texto):
    """Lista de fotos (data URLs) gravada em JSON na solicitação; vazio se não tiver/inválido."""
    import json as _json
    try:
        dados = _json.loads(texto or "[]")
        return [f for f in dados if isinstance(f, str)] if isinstance(dados, list) else []
    except (ValueError, TypeError):
        return []


templates.env.filters["fotos_lista"] = _fotos_lista
templates.env.filters["checklist_itens"] = itens_do_checklist
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
        camadas_automaticas = {"resposta_validada", "resposta_padrao", "dados_anuncio", "manual_sku_ia", "politica_geral", "busca_site_fabricante"}
        pre_venda_ia = sum(1 for p in perguntas_hoje if p.camada_resolvida in camadas_automaticas)
        pre_venda_pct_ia = round(pre_venda_ia / pre_venda_total_hoje * 100) if pre_venda_total_hoje else 0

        # Pendentes agora (qualquer dia, não só hoje)
        pendentes = db.query(Pergunta).filter(Pergunta.status == "fila_humana").all()
        pendentes_total = len(pendentes)
        pendentes_criticas = sum(
            1 for p in pendentes
            if p.recebida_em and (agora - p.recebida_em).total_seconds() / 60 > 30
        )

        # Cancelamentos manuais desta semana (segunda a agora). Só o tipo
        # "cancelamento" (nulo = registro antigo); reputação não entra aqui.
        inicio_semana = (agora - timedelta(days=agora.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        cancelamentos_semana = (
            db.query(SolicitacaoCancelamento)
            .filter(SolicitacaoCancelamento.criado_em >= inicio_semana)
            .filter(or_(SolicitacaoCancelamento.tipo.is_(None), SolicitacaoCancelamento.tipo == "cancelamento"))
            .all()
        )
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
            # Admin vê a visão geral de vendas (valor de cada conta + total
            # da rede) nessa mesma tela, no lugar dos cards operacionais --
            # supervisor e os demais continuam com os cards de sempre
            # (eles não podem ver valor de venda, ver app/routers/admin_painel.py).
            "eh_admin": bool(usuario_logado and usuario_logado.papel == "admin"),
            # a20/10: supervisor ganha o card de "Promoções aderidas pelo
            # sistema de precificação" nos cards operacionais (abaixo) --
            # só esse card, não o resto da visão geral de vendas do admin.
            "eh_supervisor": bool(usuario_logado and usuario_logado.papel == "supervisor"),
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


@app.get("/calibrar-ia", response_class=HTMLResponse)
def pagina_calibrar_ia(request: Request):
    """Administração › Calibrar IA -- só admin e supervisor (a API confere de novo)."""
    usuario = _usuario_logado(request)
    if not auth.papel_permite(usuario, ("admin", "supervisor")):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request=request, name="calibrar-ia.html", context={"usuario_logado": usuario})


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
    # Gráficos clicáveis: admin/supervisor/atendente respondem as pendentes
    # direto da lista; o perfil "tv" só visualiza.
    usuario = _usuario_logado(request)
    pode_responder = bool(usuario and usuario.papel in ("admin", "supervisor", "atendente"))
    return templates.TemplateResponse(
        request=request, name="painel-tv-geral.html",
        context={"usuario_logado": usuario, "pode_responder": pode_responder},
    )


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
        conta_pre_preenchida = _conta_logada_de(usuario)
        logistica_logado = bool(usuario and usuario.papel == "logistica")
        # Equipe: abre dentro do sistema (menu à esquerda) e lança por qualquer conta.
        equipe_logado = bool(usuario and usuario.papel in ("admin", "supervisor", "atendente"))
        nome_usuario = usuario.nome_exibicao if usuario else None
    return templates.TemplateResponse(
        request=request,
        name="solicitar-cancelamento.html",
        context={
            "conta_pre_preenchida": conta_pre_preenchida,
            "seller_logado": bool(conta_pre_preenchida),
            "logistica_logado": logistica_logado,
            "equipe_logado": equipe_logado,
            "usuario_logado": usuario,
            "papel_logado": usuario.papel if usuario else None,
            "nome_usuario": nome_usuario,
        },
    )


@app.get("/reverso", response_class=HTMLResponse)
def pagina_reverso(request: Request):
    """
    Reverso (26/10): produto que voltou (devolução, recusa na entrega, chegou
    quebrado). Uma tela só, dois usos:
      - SELLER: avisa o reverso da conta dele (conta fixa, o servidor não deixa
        pedir por outra) e manda as fotos;
      - GALPÃO: recebe o produto que voltou escolhendo a conta, registra o
        aspecto e as fotos, e depois -- testado e conferido -- libera pra venda
        com o checklist (lista "Recebidos aguardando conferência").
    """
    with SessionLocal() as db:
        usuario = auth.usuario_atual(request, db)
        if usuario is None:
            return RedirectResponse("/login?proximo=/reverso", status_code=303)
        conta_pre_preenchida = _conta_logada_de(usuario)
        logistica = usuario.papel == "logistica"
        if not logistica:   # 27/10: só o galpão registra reverso; o seller só acompanha
            return RedirectResponse("/", status_code=303)
        contexto = {
            "usuario": usuario,
            "usuario_logado": usuario,
            "papel_logado": usuario.papel,
            "nome_usuario": usuario.nome_exibicao,
            "modo_galpao": logistica,
            "conta_seller": conta_pre_preenchida if not logistica else None,
        }
    return templates.TemplateResponse(request=request, name="reverso.html", context=contexto)


@app.get("/extensao", response_class=HTMLResponse)
def pagina_extensao(request: Request):
    """
    Tela de instalação/apresentação do "Sistema de Precificação" (extensão
    do Chrome). Reaproveita a sessão por cookie que o seller já tem no
    painel -- sem login separado. O link do botão "Instalar" aponta pra
    Chrome Web Store (Chrome não deixa instalar extensão de outro lugar).
    """
    with SessionLocal() as db:
        usuario = auth.usuario_atual(request, db)
        conta_logada = _conta_logada_de(usuario)
        seller_logado = bool(conta_logada)
    return templates.TemplateResponse(
        request=request,
        name="extensao.html",
        context={
            "conta_logada": conta_logada,
            "seller_logado": seller_logado,
            "papel_logado": usuario.papel if usuario else None,
            # Link direto do item "Sistema de Precificação — ClubMarketplace"
            # na Chrome Web Store (publicado como "não listado"). O botão só
            # fica clicável depois que o usuário rola o Termo de Uso até o
            # fim e marca "Li e aceito" (ver extensao.html).
            "url_extensao": "https://chromewebstore.google.com/detail/feonnbeecdbchkdbkffghoemjgcklghm",
        },
    )


@app.get("/custos", response_class=HTMLResponse)
def pagina_custos(request: Request):
    """
    Tela "Produtos > Custos" do seller: gerar/baixar o modelo de
    planilha e importar uma planilha de custos inteira de uma vez --
    gravado direto na mesma tabela que a extensão lê (CustoSku), pelo
    cookie de sessão do painel (a API fica em app/routers/custos_painel.py).
    A busca/edição de um SKU individual mora agora em "Produtos > Lista"
    (/produtos/lista), ao lado do estoque ao vivo.
    """
    with SessionLocal() as db:
        usuario = auth.usuario_atual(request, db)
        conta_logada = _conta_logada_de(usuario)
        seller_logado = bool(conta_logada)
    return templates.TemplateResponse(
        request=request,
        name="custos.html",
        context={"conta_logada": conta_logada, "seller_logado": seller_logado, "papel_logado": usuario.papel if usuario else None},
    )


@app.get("/historico-adesoes", response_class=HTMLResponse)
def pagina_historico_adesoes(request: Request):
    """
    Tela "Produtos > Histórico de adesões": cópia no servidor de toda
    adesão a promoção feita pela extensão ClubMarketplaceX, juntando os
    registros de todos os logins/computadores da conta (cada linha mostra
    quem fez) -- sobrevive a desinstalar a extensão ou trocar de PC. A
    tela só monta o layout; os dados vêm de GET /api/cmx/historico-adesoes
    (app/routers/cmx.py), filtrados pelo mesmo cookie de sessão do painel
    (seller ou suporte navegando na conta dele).
    """
    with SessionLocal() as db:
        usuario = auth.usuario_atual(request, db)
        conta_logada = _conta_logada_de(usuario)
        seller_logado = bool(conta_logada)
    return templates.TemplateResponse(
        request=request,
        name="historico-adesoes.html",
        context={"conta_logada": conta_logada, "seller_logado": seller_logado, "papel_logado": usuario.papel if usuario else None},
    )


@app.get("/produtos/lista", response_class=HTMLResponse)
def pagina_produtos_lista(request: Request):
    """
    Tela "Produtos > Lista": pra cada SKU, mostra custo (nosso banco) +
    quantidade em estoque (lida ao vivo da API do Mercado Livre, nunca
    guardada aqui) + valor total (quantidade x custo), com busca por
    SKU e edição de custo individual. A API fica em
    app/routers/produtos.py; reaproveita a mesma API de custo
    (app/routers/custos_painel.py) pra criar/editar um SKU.
    """
    with SessionLocal() as db:
        usuario = auth.usuario_atual(request, db)
        conta_logada = _conta_logada_de(usuario)
        seller_logado = bool(conta_logada)
    return templates.TemplateResponse(
        request=request,
        name="produtos_lista.html",
        context={"conta_logada": conta_logada, "seller_logado": seller_logado, "papel_logado": usuario.papel if usuario else None},
    )


@app.get("/cadastro-seller", response_class=HTMLResponse)
def pagina_cadastro_seller(request: Request):
    """
    Tela "Cadastro" do seller, dentro de "Minhas solicitações" (15/10): o
    mesmo formulário de solicitar cadastro de produto novo que a equipe usa
    (/cadastro), só que sem escolher a Conta -- ela vem travada da sessão
    (conta_logada) e o servidor nunca aceita outra (ver
    app/routers/cadastro_seller.py). Cai na mesma tabela/fila que a equipe
    de Cadastro já usa.
    """
    with SessionLocal() as db:
        usuario = auth.usuario_atual(request, db)
        conta_logada = _conta_logada_de(usuario)
        seller_logado = bool(conta_logada)
    return templates.TemplateResponse(
        request=request,
        name="cadastro-seller.html",
        context={"conta_logada": conta_logada, "seller_logado": seller_logado, "papel_logado": usuario.papel if usuario else None},
    )


@app.get("/dashboard", response_class=HTMLResponse)
def pagina_dashboard_seller(request: Request):
    """
    Tela "Dashboard" do seller: visão rápida da conta (hoje/mês, status
    dos anúncios, evolução). A API fica em app/routers/dashboard_painel.py.

    NOME DO ARQUIVO (12/10): "dashboard_seller.html" -- de propósito
    diferente de "dashboard.html" (esse já existia e é a tela inicial
    "/" da equipe interna/admin, resumo de devoluções/pré-venda/
    cancelamentos). Usar o mesmo nome aqui por engano sobrescreveu
    aquele outro -- não repetir esse nome de novo.
    """
    with SessionLocal() as db:
        usuario = auth.usuario_atual(request, db)
        conta_logada = _conta_logada_de(usuario)
        seller_logado = bool(conta_logada)
    return templates.TemplateResponse(
        request=request,
        name="dashboard_seller.html",
        context={"conta_logada": conta_logada, "seller_logado": seller_logado, "papel_logado": usuario.papel if usuario else None},
    )


@app.get("/vendas", response_class=HTMLResponse)
def pagina_vendas(request: Request):
    """
    Tela "Vendas": venda a venda em tempo real, com margem boa/ruim e os
    cards de Venda Bruta/Repasse/CMV/Lucro, equivalente à Central
    Financeira só que dentro do nosso sistema. A API fica em
    app/routers/vendas.py.
    """
    with SessionLocal() as db:
        usuario = auth.usuario_atual(request, db)
        conta_logada = _conta_logada_de(usuario)
        seller_logado = bool(conta_logada)
    return templates.TemplateResponse(
        request=request,
        name="vendas.html",
        context={"conta_logada": conta_logada, "seller_logado": seller_logado, "papel_logado": usuario.papel if usuario else None},
    )


@app.get("/vendas/consulta", response_class=HTMLResponse)
def pagina_vendas_consulta(request: Request):
    """
    Submenu "Consulta de Vendas": a mesma venda a venda da tela "Vendas"
    (Resumo), só que em formato de lista/cartão -- um cartão por venda,
    com lucro/markup/margem já calculados e foco em atualização em tempo
    real (a tela fica se auto-atualizando sozinha). Mesma API de
    app/routers/vendas.py (GET /lista), nenhuma rota nova no back-end.
    """
    with SessionLocal() as db:
        usuario = auth.usuario_atual(request, db)
        conta_logada = _conta_logada_de(usuario)
        seller_logado = bool(conta_logada)
    return templates.TemplateResponse(
        request=request,
        name="vendas_consulta.html",
        context={"conta_logada": conta_logada, "seller_logado": seller_logado, "papel_logado": usuario.papel if usuario else None},
    )


@app.get("/taxas", response_class=HTMLResponse)
def pagina_taxas(request: Request):
    """
    Tela "Taxas": cadastro das variáveis percentuais (imposto, CLUB
    etc) usadas pra calcular a margem -- no painel (Vendas) e na
    extensão. A API fica em app/routers/taxas.py.
    """
    with SessionLocal() as db:
        usuario = auth.usuario_atual(request, db)
        conta_logada = _conta_logada_de(usuario)
        seller_logado = bool(conta_logada)
    return templates.TemplateResponse(
        request=request,
        name="taxas.html",
        context={"conta_logada": conta_logada, "seller_logado": seller_logado, "papel_logado": usuario.papel if usuario else None},
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


@app.get("/painel-galpao", response_class=HTMLResponse)
def pagina_painel_galpao(request: Request):
    """Dashboard do galpão (26/10): números de cancelamento e reverso, gráficos e relatório por período."""
    with SessionLocal() as db:
        usuario = auth.usuario_atual(request, db)
        if not auth.papel_permite(usuario, ("logistica", "admin", "supervisor")):
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(
            request=request, name="painel-galpao.html", context={"usuario": usuario, "modo_logistica": usuario.papel == "logistica"},
        )


@app.get("/reversos-gestao", response_class=HTMLResponse)
def pagina_reversos_gestao(request: Request):
    """Tela "Reversos" do gestor (27/10): visão completa de tudo sobre reverso. SÓ admin e supervisor
    (decisão do Washington); qualquer outro perfil volta pra tela inicial dele. A API tem a mesma trava."""
    with SessionLocal() as db:
        usuario = auth.usuario_atual(request, db)
        if not auth.papel_permite(usuario, ("admin", "supervisor")):
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(request=request, name="reversos-gestor.html", context={"usuario_logado": usuario})


@app.get("/meus-cancelamentos", response_class=HTMLResponse)
def pagina_meus_cancelamentos(request: Request):
    """Tela do seller: as solicitações (cancelamento, reputação e cadastro) da conta vinculada a ele."""
    with SessionLocal() as db:
        usuario = auth.usuario_atual(request, db)
        if not _eh_painel_seller(usuario):
            return RedirectResponse("/", status_code=303)

        # Fotos NÃO são carregadas aqui (a página trazia as fotos de TODAS as
        # solicitações do seller de uma vez e travava o sistema): só a quantidade;
        # as fotos vêm ao clicar em "Ver fotos" (GET /meus-cancelamentos/fotos).
        linhas = (
            db.query(SolicitacaoCancelamento, n_fotos_sql())
            .options(defer(SolicitacaoCancelamento.fotos_base64))
            .filter(SolicitacaoCancelamento.conta_chave == chave_conta(usuario.conta_vinculada))
            .order_by(SolicitacaoCancelamento.confirmado_por.is_(None).desc(), SolicitacaoCancelamento.criado_em.desc())
            .all()
        )
        solicitacoes = []
        for sol, n in linhas:
            sol.n_fotos_tmp = int(n or 0)   # atributo só de tela (não é coluna)
            solicitacoes.append(sol)
        # Cadastro (15/10): mesma conta, sem contar as excluídas -- igual às outras
        # listas da tela "Cadastro", nunca aparece o que foi excluído (ver _admin_supervisor_logado).
        cadastros = (
            db.query(ProdutoCadastro)
            .filter(ProdutoCadastro.empresa_chave == chave_conta(usuario.conta_vinculada))
            .filter(ProdutoCadastro.status != "excluida")
            .order_by((ProdutoCadastro.status == "cadastrado"), ProdutoCadastro.recebido_em.desc())
            .all()
        )
        return templates.TemplateResponse(
            request=request,
            name="meus-cancelamentos.html",
            context={"usuario": usuario, "solicitacoes": solicitacoes, "cadastros": cadastros, "papel_logado": usuario.papel},
        )


def _solicitacao_do_seller(db, request, solicitacao_id):
    """Solicitação pedida pelo seller logado, SÓ se for da conta vinculada a ele; senão None."""
    usuario = auth.usuario_atual(request, db)
    if not _eh_painel_seller(usuario):
        return False, None
    sol = (
        db.query(SolicitacaoCancelamento)
        .filter(SolicitacaoCancelamento.id == solicitacao_id)
        .filter(SolicitacaoCancelamento.conta_chave == chave_conta(usuario.conta_vinculada))  # só as dele
        .first()
    )
    return True, sol


@app.get("/meus-cancelamentos/fotos")
def fotos_meus_cancelamentos(request: Request, id: int):
    """Endereços das fotos de UMA solicitação do próprio seller (carregado ao clicar em "Ver fotos")."""
    with SessionLocal() as db:
        eh_seller, sol = _solicitacao_do_seller(db, request, id)
        if not eh_seller:
            return JSONResponse({"detail": "Sem permissão."}, status_code=403)
        if sol is None:
            return JSONResponse({"detail": "Não encontrada."}, status_code=404)
        return listar_fotos(db, sol, "/meus-cancelamentos/foto")


@app.get("/meus-cancelamentos/foto")
def foto_meus_cancelamentos(request: Request, id: int, mini: int = 0):
    """Bytes de UMA foto (ou miniatura) de uma solicitação do próprio seller."""
    with SessionLocal() as db:
        sid = solicitacao_da_foto(db, id)
        eh_seller, sol = _solicitacao_do_seller(db, request, sid if sid is not None else -1)
        if not eh_seller:
            return JSONResponse({"detail": "Sem permissão."}, status_code=403)
        if sol is None:
            return JSONResponse({"detail": "Não encontrada."}, status_code=404)
        try:
            achada = obter_bytes(db, id, miniatura=bool(mini))
        except RuntimeError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=502)
        if achada is None:
            return JSONResponse({"detail": "Não encontrada."}, status_code=404)
        mime, dados, _ = achada
        return Response(content=dados, media_type=mime, headers={"Cache-Control": "private, max-age=31536000, immutable"})


def _listar_usuarios_visiveis(db, usuario_logado):
    """Cada um vê só quem pode gerenciar (admin: todos; supervisor: atendentes e sellers) -- mesma regra de criar/editar/resetar/desativar.
    Ordem alfabética pura pelo nome de exibição (ignorando maiúsculas/minúsculas) --
    antes agrupava por papel primeiro (ver pedido do usuário, 07/10)."""
    query = db.query(Usuario)
    if usuario_logado.papel != "admin":
        query = query.filter(Usuario.papel.in_(PAPEIS_GERENCIAVEIS_POR.get(usuario_logado.papel, ())))
    return query.order_by(func.lower(Usuario.nome_exibicao)).all()


def _setores_validos(valores) -> str:
    """Junta só os setores que realmente existem em SETORES_DISPONIVEIS (ignora
    qualquer coisa fora disso, mesmo que alguém tente forçar pelo HTML),
    sem duplicar, numa string separada por vírgula pra guardar em Usuario.setores."""
    vistos = [v for v in valores if v in SETORES_DISPONIVEIS]
    return ",".join(dict.fromkeys(vistos))


def _contas_disponiveis(db):
    """Lista real de contas cadastradas (não inativadas), pro <select> de
    "Conta vinculada" -- antes era texto livre, o que deixava passar erro de
    digitação/espaço e gerava usuário apontando pra uma conta que não existe
    (ver pedido do usuário, 07/10). Ordem alfabética ignorando maiúsculas/
    minúsculas. Só lista, nunca apaga nada."""
    return db.query(Conta).filter(Conta.inativa_em.is_(None)).order_by(func.lower(Conta.apelido)).all()


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
            context={
                "papel_logado": usuario_logado.papel,
                "usuarios": usuarios,
                "usuario_logado": usuario_logado,
                "contas_disponiveis": _contas_disponiveis(db),
                "setores_disponiveis": SETORES_DISPONIVEIS,
            },
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
                "contas_disponiveis": _contas_disponiveis(db),
                "setores_disponiveis": SETORES_DISPONIVEIS,
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
            context={
                "papel_logado": usuario_logado.papel,
                "usuario_logado": usuario_logado,
                "alvo": alvo,
                "erro": None,
                "contas_disponiveis": _contas_disponiveis(db),
                "setores_disponiveis": SETORES_DISPONIVEIS,
                "setores_do_alvo": auth.setores_do_usuario(alvo),
            },
        )


@app.post("/usuarios/{usuario_id}/editar", response_class=HTMLResponse)
def editar_usuario(
    usuario_id: int,
    request: Request,
    nome_exibicao: str = Form(...),
    papel: str = Form(...),
    conta_vinculada: str = Form(""),
    setores: list[str] = Form([]),
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
        contexto_erro = {
            "papel_logado": usuario_logado.papel,
            "usuario_logado": usuario_logado,
            "alvo": alvo,
            "contas_disponiveis": _contas_disponiveis(db),
            "setores_disponiveis": SETORES_DISPONIVEIS,
            "setores_do_alvo": auth.setores_do_usuario(alvo),
        }

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
        if papel in PAPEIS_PAINEL_SELLER and not conta_limpa:
            return templates.TemplateResponse(
                request=request, name="editar-usuario.html", status_code=400,
                context={**contexto_erro, "erro": "Informe a conta vinculada -- obrigatório pra usuários seller e suporte."},
            )

        alvo.nome_exibicao = nome_limpo
        alvo.papel = papel
        alvo.conta_vinculada = conta_limpa if papel in PAPEIS_PAINEL_SELLER else None
        # Setores só fazem sentido pra "operador"/"gerente_setor" -- se o papel
        # mudar pra outro, limpa (evita ficar um valor velho sem efeito nenhum).
        alvo.setores = _setores_validos(setores) if papel in ("operador", "gerente_setor") else None
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
    setores: list[str] = Form([]),
):
    with SessionLocal() as db:
        usuario_logado = auth.usuario_atual(request, db)
        if not auth.papel_permite(usuario_logado, ("admin", "supervisor")):
            return RedirectResponse("/", status_code=303)

        # Supervisor só pode criar atendente ou seller, mesmo que alguém tente forçar
        # outro valor mexendo no HTML -- a checagem de verdade é aqui,
        # no servidor, nunca só no <select> da tela.
        papeis_que_esse_criador_pode_atribuir = PAPEIS_GERENCIAVEIS_POR.get(usuario_logado.papel, ())
        contexto_base = {
            "papel_logado": usuario_logado.papel,
            "usuario_logado": usuario_logado,
            "usuarios": _listar_usuarios_visiveis(db, usuario_logado),
            "contas_disponiveis": _contas_disponiveis(db),
            "setores_disponiveis": SETORES_DISPONIVEIS,
        }

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
        if papel in PAPEIS_PAINEL_SELLER and not conta_vinculada_limpa:
            return templates.TemplateResponse(
                request=request, name="usuarios.html", status_code=400,
                context={**contexto_base, "erro_criar": "Informe a conta vinculada -- obrigatório pra usuários seller e suporte."},
            )

        codigo = auth.gerar_codigo_primeiro_acesso()
        novo_usuario = Usuario(
            usuario=usuario_normalizado,
            nome_exibicao=nome_exibicao.strip(),
            papel=papel,
            codigo_primeiro_acesso=codigo,
            precisa_trocar_senha=True,
            conta_vinculada=conta_vinculada_limpa if papel in PAPEIS_PAINEL_SELLER else None,
            setores=_setores_validos(setores) if papel in ("operador", "gerente_setor") else None,
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
                "contas_disponiveis": _contas_disponiveis(db),
                "setores_disponiveis": SETORES_DISPONIVEIS,
                "criar_sucesso": {"usuario": novo_usuario.usuario, "nome_exibicao": novo_usuario.nome_exibicao, "papel": novo_usuario.papel, "codigo": codigo},
            },
        )


@app.get("/solicitacoes-painel", response_class=HTMLResponse)
def pagina_solicitacoes_painel(request: Request):
    """Tela interna de acompanhamento das solicitações manuais de cancelamento, agrupada por conta."""
    return templates.TemplateResponse(request=request, name="painel-cancelamentos.html", context={"usuario_logado": _usuario_logado(request)})


@app.get("/cadastro", response_class=HTMLResponse)
def pagina_cadastro(request: Request):
    """
    Tela "Cadastro": produtos novos das notas fiscais do Operador Logístico
    (e os incluídos à mão), agrupados por empresa, esperando o time de
    Cadastro. Cada pessoa marca em quais marketplaces (Mercado Livre, TikTok,
    Shopee) já cadastrou -- a tela guarda quem fez cada um. Admin, supervisor
    e o perfil "cadastro" (de sempre) + operador/gerente_setor com o setor
    "cadastro" marcado (15/10); a API (app/routers/cadastro_produtos.py)
    confere de novo.
    """
    usuario = _usuario_logado(request)
    pode_abrir = auth.papel_permite(usuario, ("admin", "supervisor", "cadastro")) or (
        usuario is not None and usuario.papel in ("operador", "gerente_setor") and "cadastro" in auth.setores_do_usuario(usuario)
    )
    if not pode_abrir:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request=request, name="cadastro.html",
        context={"usuario_logado": usuario, "pode_ver_produtividade": auth.pode_ver_produtividade(usuario, "cadastro")},
    )


@app.get("/cadastro/produtividade", response_class=HTMLResponse)
def pagina_cadastro_produtividade(request: Request):
    """Quanto cada pessoa do time de Cadastro cadastrou, por marketplace e período.
    Admin, supervisor, e gerente_setor com o setor "cadastro" (15/10) -- mesma
    regra de app.auth.pode_ver_produtividade usada na API (antes a página
    ficou pra trás da API e só liberava admin/supervisor)."""
    usuario = _usuario_logado(request)
    if not auth.pode_ver_produtividade(usuario, "cadastro"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request=request, name="cadastro-produtividade.html", context={"usuario_logado": usuario})


@app.get("/relatorio-cancelamentos", response_class=HTMLResponse)
def pagina_relatorio_cancelamentos(request: Request):
    """Relatório de cancelamentos por conta e período (imprimir / PDF / Excel).
    Admin, supervisor e atendente (acesso de sempre) + gerente_setor com o
    setor "solicitacoes_cancelamento" (15/10)."""
    usuario = _usuario_logado(request)
    if not usuario or not (
        usuario.papel in ("admin", "supervisor", "atendente")
        or auth.pode_ver_produtividade(usuario, "solicitacoes_cancelamento")
    ):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request=request, name="relatorio-cancelamentos.html", context={"usuario_logado": usuario})


@app.get("/produtividade-cancelamento", response_class=HTMLResponse)
def pagina_produtividade_cancelamento(request: Request):
    """Quanto cada pessoa confirmou (cancelamento + reputação) no período.
    Admin, supervisor, e gerente_setor com o setor "solicitacoes_cancelamento"
    (16/10) -- mesma regra de app.auth.pode_ver_produtividade usada na API e
    na Produtividade do Cadastro."""
    usuario = _usuario_logado(request)
    if not auth.pode_ver_produtividade(usuario, "solicitacoes_cancelamento"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request=request, name="produtividade-cancelamento.html", context={"usuario_logado": usuario})


@app.get("/api/saude")
def saude():
    """Endpoint simples pra confirmar que o backend está de pé."""
    return {"status": "ok"}


@app.get("/privacidade-extensao", response_class=HTMLResponse)
def pagina_privacidade_extensao(request: Request):
    """
    Política de privacidade da extensão "Sistema de Precificação" -- página
    pública (sem login), exigida pelo formulário de publicação da Chrome
    Web Store. Hospedada aqui (em vez de um documento externo) pra ficar
    sob o mesmo domínio e não depender de outro serviço.
    """
    return templates.TemplateResponse(request=request, name="privacidade-extensao.html", context={})


def _redirecionar_por_papel(usuario) -> RedirectResponse:
    return RedirectResponse(_pagina_inicial(usuario), status_code=303)


@app.get("/login", response_class=HTMLResponse)
def pagina_login(request: Request):
    # Se já está logado, não faz sentido mostrar o login de novo.
    with SessionLocal() as db:
        usuario = auth.usuario_atual(request, db)
        if usuario:
            return _redirecionar_por_papel(usuario)
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

        minutos_restantes = auth.minutos_bloqueado(conta)
        if minutos_restantes is not None:
            erro = f"Muitas tentativas erradas. Tente novamente em cerca de {minutos_restantes} minuto(s)."
            return templates.TemplateResponse(request=request, name="login.html", context={"erro": erro, "proximo": proximo}, status_code=429)

        if not auth.verificar_senha(senha, conta.senha_hash):
            auth.registrar_tentativa_falha(conta, db)
            return templates.TemplateResponse(request=request, name="login.html", context={"erro": "Usuário ou senha incorretos.", "proximo": proximo}, status_code=401)

        auth.limpar_bloqueio_login(conta, db)

        # Voltou pra tela que queria abrir; se o perfil não puder, o próprio
        # controle de acesso manda pra página inicial dele.
        resposta = RedirectResponse(proximo, status_code=303) if proximo else _redirecionar_por_papel(conta)
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

        resposta = _redirecionar_por_papel(conta)
        duracao = auth.duracao_sessao(conta.papel)
        token = auth.criar_token_sessao(conta.id, duracao)
        resposta.set_cookie(auth.COOKIE_SESSAO, token, httponly=True, samesite="lax", max_age=duracao)
        return resposta
    finally:
        db.close()
