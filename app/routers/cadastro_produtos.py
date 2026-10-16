"""
Produtos NOVOS das notas fiscais (Operador Logístico) esperando cadastro.

A INFORMAÇÃO
------------
É a mesma que hoje vai na mensagem de Zap "PRODUTO(S) NOVO(S) PARA CADASTRO"
do Operador: empresa, fornecedor, nota e, por produto, SKU, nome, EAN,
quantidade e custo unitário. Em vez de (só) ir pro Zap, agora ela também cai
numa tela exclusiva do painel -- "Cadastro" -- agrupada por EMPRESA.

COMO FUNCIONA
-------------
O Operador (Apps Script) manda os produtos novos de cada nota no momento em
que a nota é registrada -- só notas lançadas depois da integração entrar no
ar, nada de notas antigas. Mesmo padrão do Mercado Livre -> webhook: quem tem
a informação empurra, o backend só recebe. Nada aqui lê a planilha do Operador.

  POST /api/operador/produtos-novos      -> RECEBE (máquina -> máquina).
        Protegido por uma senha compartilhada no cabeçalho "X-Operador-Token"
        (variável OPERADOR_API_TOKEN no Railway). Sem login de pessoa -- por
        isso essa rota é liberada em _PUBLICO_EXATO no main.py, e a conferência
        do token é feita AQUI.
  GET  /api/cadastro/produtos            -> lista pra tela "Cadastro".
  POST /api/cadastro/produtos            -> inclusão MANUAL de um produto (14/10),
        com a marca "refazer" (cadastro que já foi feito antes e está sendo refeito).
  POST /api/cadastro/produtos/{id}/marketplace
                                         -> marca Mercado Livre / TikTok / Shopee
        (feito, não se aplica ou volta a pendente), guardando quem e quando.
        O produto só fica "cadastrado" (OK) quando os 3 estão resolvidos.
  POST /api/cadastro/produtos/{id}/status-> atalho: marca/desmarca os 3 de uma vez.
  POST /api/cadastro/produtos/lote       -> inclusão MANUAL de VÁRIOS produtos de uma nota (colar da
        planilha). Exige "kit" e "plataformas" (pelo menos 1 canal) -- valem pra todos os
        produtos do envio; EAN e custo > 0 são obrigatórios por produto (15/10).
  POST /api/cadastro/produtos/{id}/assumir -> "assumi este cadastro" (ou solta); guarda quem e quando.
  POST /api/cadastro/produtos/{id}/reabrir -> reabre um produto TRAVADO (já cadastrado), com
        motivo obrigatório. Só admin/supervisor (ver "TRAVA" abaixo).
  POST /api/cadastro/produtos/{id}/excluir -> exclui uma solicitação PENDENTE (motivo
        obrigatório, só admin/supervisor). Não apaga -- marca "excluida" e some das listas.
  GET  /api/cadastro/empresas            -> nomes de empresa já usados (sugestão no formulário antigo).
  GET  /api/cadastro/contas              -> contas ativas (tabela "contas", a mesma de "Contas
        conectadas") -- fonte do seletor "Conta" da solicitação manual (15/10). Em vez de digitar
        o nome da empresa livremente, a pessoa escolhe de uma lista fixa, pra nunca ter duas
        grafias da mesma conta ("JC Machado" x "JC MACHADO").
  GET  /api/cadastro/produtividade       -> quanto cada pessoa cadastrou (só admin/supervisor).
        (as de /api/cadastro: admin, supervisor e o perfil "cadastro")

TRAVA (14/10)
-------------
Produto "cadastrado" (os 3 marketplaces resolvidos) fica TRAVADO: as rotas de
marcação (/marketplace e /status) recusam qualquer mudança nele (423), pra
ninguém -- nem por engano -- desfazer um cadastro já confirmado. A única
forma de mexer de novo é a rota /reabrir (só admin/supervisor), que exige
motivo e grava quem reabriu, quando e por quê.

REENVIO SEGURO
--------------
A chave é (nota_id, sku). Mandar o mesmo produto de novo ATUALIZA os dados em
vez de duplicar, e nunca desfaz um "cadastrado". Se a mesma nota for
reprocessada depois com outro ID, o produto que ainda está PENDENTE pra mesma
empresa + SKU é atualizado em vez de aparecer duas vezes.

O custo do produto por empresa (2ª rodada) NÃO está aqui de propósito -- vem
depois, separado.
"""
import hmac
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app import auth, config
from app.contas_util import chave_conta
from app.database import get_db
from app.models import Conta, ProdutoCadastro, Usuario

logger = logging.getLogger(__name__)

router = APIRouter(tags=["cadastro-produtos"])

LIMITE_PRODUTOS_POR_ENVIO = 500
LIMITE_LISTA = 5000


# ---------------------------------------------------------------------------
# Segurança
# ---------------------------------------------------------------------------
def _conferir_token(request: Request) -> None:
    """
    Dependency da rota de recebimento. Sem OPERADOR_API_TOKEN configurado no
    servidor a rota fica DESLIGADA (503) -- nunca aberta. Comparação em tempo
    constante, pra não vazar o token por diferença de tempo de resposta.
    """
    esperado = config.OPERADOR_API_TOKEN
    if not esperado:
        raise HTTPException(
            status_code=503,
            detail="Integração com o Operador Logístico desligada (falta OPERADOR_API_TOKEN no servidor).",
        )
    enviado = request.headers.get("x-operador-token", "")
    if not hmac.compare_digest(enviado.encode("utf-8"), esperado.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Token inválido.")


def _cadastro_logado(request: Request, db: Session = Depends(get_db)) -> Usuario:
    """Tela e API de Cadastro: admin, supervisor, o perfil "cadastro" (os demais
    perfis já são barrados no middleware), e "operador"/"gerente_setor" que
    tiverem o setor "cadastro" marcado (ver app/main.py:SETORES_DISPONIVEIS)."""
    usuario = auth.usuario_atual(request, db)
    permitido = auth.papel_permite(usuario, ("admin", "supervisor", "cadastro")) or (
        usuario is not None and usuario.papel in ("operador", "gerente_setor") and "cadastro" in auth.setores_do_usuario(usuario)
    )
    if not permitido:
        raise HTTPException(status_code=403, detail="Acesso restrito.")
    return usuario


def _supervisao_logado(request: Request, db: Session = Depends(get_db)) -> Usuario:
    """Produtividade (quanto cada um cadastrou): admin, supervisor, e
    "gerente_setor" com o setor "cadastro" -- o perfil "cadastro"/"operador"
    comum não vê o número dos colegas."""
    usuario = auth.usuario_atual(request, db)
    if not auth.pode_ver_produtividade(usuario, "cadastro"):
        raise HTTPException(status_code=403, detail="Acesso restrito.")
    return usuario


def _admin_supervisor_logado(request: Request, db: Session = Depends(get_db)) -> Usuario:
    """Excluir solicitação (15/10): SÓ admin e supervisor -- diferente de
    _supervisao_logado acima, aqui "gerente_setor" não entra."""
    usuario = auth.usuario_atual(request, db)
    if not auth.papel_permite(usuario, ("admin", "supervisor")):
        raise HTTPException(status_code=403, detail="Acesso restrito a admin e supervisor.")
    return usuario


# ---------------------------------------------------------------------------
# Formato do que o Operador envia (texto sempre como texto, número como número)
# ---------------------------------------------------------------------------
class ProdutoNovoIn(BaseModel):
    nota_id: str
    empresa: str
    fornecedor: Optional[str] = ""
    numero_nota: Optional[str] = ""
    data_hora: Optional[str] = ""  # ISO 8601 em UTC, ex: 2026-10-13T14:30:00Z
    sku: str
    produto: Optional[str] = ""
    gtin: Optional[str] = ""  # EAN
    quantidade: float = 0
    custo_unitario: float = 0


class LoteProdutosNovosIn(BaseModel):
    produtos: List[ProdutoNovoIn]


# ---------------------------------------------------------------------------
# Gravação
# ---------------------------------------------------------------------------
def _limpo(texto: Optional[str]) -> Optional[str]:
    """Texto sem espaço sobrando; vazio vira None (não grava string vazia no banco)."""
    valor = (texto or "").strip()
    return valor or None


def _ler_data_utc(texto: Optional[str]) -> Optional[datetime]:
    """ISO 8601 -> datetime UTC "cru" (sem fuso), que é como o banco guarda datas."""
    if not texto:
        return None
    try:
        data = datetime.fromisoformat(texto.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if data.tzinfo is not None:
        data = data.astimezone(timezone.utc).replace(tzinfo=None)
    return data


def _gravar_produto(db: Session, entrada: ProdutoNovoIn) -> str:
    """Cria ou atualiza um produto. Devolve 'criado' ou 'atualizado'."""
    empresa_nome = entrada.empresa.strip()
    chave_empresa = chave_conta(empresa_nome)
    sku = entrada.sku.strip()

    registro = (
        db.query(ProdutoCadastro)
        .filter(ProdutoCadastro.nota_id == entrada.nota_id, ProdutoCadastro.sku == sku)
        .first()
    )
    if registro is None:
        # Mesma empresa + SKU ainda pendente, vindo de outro ID de nota (nota reprocessada):
        # atualiza esse em vez de criar um segundo igual.
        registro = (
            db.query(ProdutoCadastro)
            .filter(
                ProdutoCadastro.empresa_chave == chave_empresa,
                ProdutoCadastro.sku == sku,
                ProdutoCadastro.status == "pendente",
            )
            .order_by(ProdutoCadastro.id.desc())
            .first()
        )
        if registro is not None:
            registro.nota_id = entrada.nota_id

    criado = registro is None
    if criado:
        registro = ProdutoCadastro(nota_id=entrada.nota_id, sku=sku, status="pendente")
        db.add(registro)

    registro.empresa_nome = empresa_nome
    registro.empresa_chave = chave_empresa
    registro.fornecedor = _limpo(entrada.fornecedor)
    registro.numero_nota = _limpo(entrada.numero_nota)
    registro.data_nota = _ler_data_utc(entrada.data_hora) or registro.data_nota
    registro.produto = _limpo(entrada.produto)
    registro.gtin = _limpo(entrada.gtin)
    registro.quantidade = entrada.quantidade or 0
    registro.custo_unitario = entrada.custo_unitario or 0
    return "criado" if criado else "atualizado"


@router.post("/api/operador/produtos-novos", dependencies=[Depends(_conferir_token)])
def receber_produtos_novos(lote: LoteProdutosNovosIn, db: Session = Depends(get_db)):
    """
    Recebe os produtos novos de uma ou mais notas. Tudo-ou-nada: se qualquer
    produto do lote falhar, nada é gravado e o Operador reenvia (é seguro).
    Lote vazio é aceito -- é o "teste de conexão" do Operador (confere URL e
    token sem gravar nada).
    """
    if len(lote.produtos) > LIMITE_PRODUTOS_POR_ENVIO:
        raise HTTPException(status_code=413, detail=f"Máximo de {LIMITE_PRODUTOS_POR_ENVIO} produtos por envio.")
    for p in lote.produtos:
        if not (p.nota_id or "").strip() or not (p.empresa or "").strip() or not (p.sku or "").strip():
            raise HTTPException(status_code=422, detail="Todo produto precisa de nota_id, empresa e sku.")

    criados = atualizados = 0
    try:
        for p in lote.produtos:
            if _gravar_produto(db, p) == "criado":
                criados += 1
            else:
                atualizados += 1
            db.flush()  # grava já, pra um produto repetido DENTRO do mesmo lote ser achado
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Falha ao gravar produtos novos do Operador Logístico")
        raise HTTPException(status_code=500, detail="Erro ao gravar os produtos no servidor.")

    if lote.produtos:
        logger.info("Cadastro: %d produto(s) novo(s) criado(s), %d atualizado(s)", criados, atualizados)
    return {"ok": True, "recebidos": len(lote.produtos), "criados": criados, "atualizados": atualizados}


# ---------------------------------------------------------------------------
# Marketplaces (14/10): cada produto é cadastrado em até 3 lugares
# ---------------------------------------------------------------------------
MARKETPLACES = {
    "ml": "Mercado Livre",
    "tiktok": "TikTok",
    "shopee": "Shopee",
}
ESTADOS_MARKETPLACE = ("feito", "nao_se_aplica", "pendente")


def _estado_mp(produto: ProdutoCadastro, chave: str) -> Optional[str]:
    return getattr(produto, f"{chave}_status")


def _todos_resolvidos(produto: ProdutoCadastro) -> bool:
    return all(_estado_mp(produto, k) in ("feito", "nao_se_aplica") for k in MARKETPLACES)


def _precisa_definir_plataformas(produto: ProdutoCadastro) -> bool:
    """
    16/10 (correção): produto automático (nota fiscal) sem NENHUM dos 3
    marketplaces tocado ainda precisa que supervisor/gerente escolha quais
    essa solicitação precisa (ver /plataformas) ANTES de qualquer operador
    poder assumir ou marcar um canal -- senão o operador clicando direto nos
    botões de canal é quem fica "escolhendo" sozinho, o que não pode.
    Produto manual/seller já chega com isso resolvido na criação (plataformas
    escolhidas na hora, o resto já marcado "não se aplica"), então nunca cai aqui.
    """
    return produto.origem == "automatico" and all(_estado_mp(produto, k) is None for k in MARKETPLACES)


def _recalcular_status(produto: ProdutoCadastro, usuario_nome: str) -> None:
    """
    16/10: NÃO trava mais sozinho quando os 3 marketplaces ficam resolvidos --
    isso agora só acontece no Confirmar explícito (ver /confirmar), que é quem
    carimba cadastrado_por/cadastrado_em. `usuario_nome` fica sem uso aqui
    (mantido só pra não mudar a assinatura e as chamadas já existentes).

    O que esta função continua fazendo: se algum canal voltar a ficar em
    aberto (ex: reabertura em app/routers/cadastro_produtos.py:/reabrir, ou
    alguém desmarcar um canal já resolvido), garante que o produto NUNCA fique
    com status "cadastrado" sem os 3 resolvidos -- volta pra "pendente" e
    limpa o carimbo.
    """
    if not _todos_resolvidos(produto) and produto.status == "cadastrado":
        produto.status = "pendente"
        produto.cadastrado_por = None
        produto.cadastrado_em = None


def _dados_marketplaces(p: ProdutoCadastro) -> dict:
    return {
        k: {
            "estado": _estado_mp(p, k),
            "por": getattr(p, f"{k}_por"),
            "em": _iso_utc(getattr(p, f"{k}_em")),
        }
        for k in MARKETPLACES
    }


def _iso_utc(data: Optional[datetime]) -> Optional[str]:
    """Datas do banco são UTC sem fuso; manda com 'Z' pro navegador converter pro horário de Brasília."""
    return (data.isoformat() + "Z") if data else None


def _produto_json(p: ProdutoCadastro) -> dict:
    return {
        "id": p.id,
        "empresa": p.empresa_nome,
        "fornecedor": p.fornecedor,
        "numero_nota": p.numero_nota,
        "data_nota": _iso_utc(p.data_nota or p.recebido_em),
        "sku": p.sku,
        "produto": p.produto,
        "gtin": p.gtin,
        "quantidade": p.quantidade,
        "custo_unitario": p.custo_unitario,
        "status": p.status,
        "cadastrado_por": p.cadastrado_por,
        "cadastrado_em": _iso_utc(p.cadastrado_em),
        "origem": p.origem or "automatico",
        "criado_por": p.criado_por,
        "link_fotos": p.link_fotos,
        "marketplaces": _dados_marketplaces(p),
        "refazer": bool(p.refazer),
        "kit": bool(p.kit),
        "assumido_por": p.assumido_por,
        "assumido_em": _iso_utc(p.assumido_em),
        "travado": p.status == "cadastrado",
        "reaberto_por": p.reaberto_por,
        "reaberto_em": _iso_utc(p.reaberto_em),
        "motivo_reabertura": p.motivo_reabertura,
    }


# ---------------------------------------------------------------------------
# Tela "Cadastro"
# ---------------------------------------------------------------------------
@router.get("/api/cadastro/produtos")
def listar_produtos_cadastro(
    status: str = "pendente",
    busca: Optional[str] = None,
    limite: int = 3000,
    db: Session = Depends(get_db),
    _usuario: Usuario = Depends(_cadastro_logado),
):
    """
    Produtos esperando cadastro (ou já cadastrados), agrupáveis por empresa na
    tela. `status`: pendente | cadastrado | todos. `contagem` vem sempre completa
    (não é afetada pelo filtro nem pela busca) pra tela mostrar os totais.
    """
    status = (status or "").strip().lower()
    if status not in ("pendente", "cadastrado", "todos"):
        raise HTTPException(status_code=422, detail="status precisa ser pendente, cadastrado ou todos.")
    limite = max(1, min(limite, LIMITE_LISTA))

    contagem = {"pendente": 0, "cadastrado": 0}
    for st, total in db.query(ProdutoCadastro.status, func.count(ProdutoCadastro.id)).group_by(ProdutoCadastro.status).all():
        if st in contagem:
            contagem[st] = total

    # Excluída (15/10) nunca aparece em nenhuma aba -- nem em "Todos" -- mas continua
    # gravada no banco (ver rota /excluir).
    consulta = db.query(ProdutoCadastro).filter(ProdutoCadastro.status != "excluida")
    if status != "todos":
        consulta = consulta.filter(ProdutoCadastro.status == status)
    if busca and busca.strip():
        trecho = f"%{busca.strip().lower()}%"
        consulta = consulta.filter(
            or_(
                func.lower(ProdutoCadastro.sku).like(trecho),
                func.lower(func.coalesce(ProdutoCadastro.produto, "")).like(trecho),
                func.lower(func.coalesce(ProdutoCadastro.gtin, "")).like(trecho),
                func.lower(ProdutoCadastro.empresa_nome).like(trecho),
                func.lower(func.coalesce(ProdutoCadastro.numero_nota, "")).like(trecho),
                func.lower(func.coalesce(ProdutoCadastro.fornecedor, "")).like(trecho),
            )
        )

    linhas = (
        consulta.order_by(
            ProdutoCadastro.empresa_chave,
            func.coalesce(ProdutoCadastro.data_nota, ProdutoCadastro.recebido_em).desc(),
            ProdutoCadastro.id.desc(),
        )
        .limit(limite + 1)
        .all()
    )
    truncado = len(linhas) > limite
    linhas = linhas[:limite]

    return {"itens": [_produto_json(p) for p in linhas], "contagem": contagem, "truncado": truncado}


@router.get("/api/cadastro/empresas")
def listar_empresas_cadastro(db: Session = Depends(get_db), _usuario: Usuario = Depends(_cadastro_logado)):
    """Nomes de empresa já usados (uma por chave) -- mantido pra quem ainda chamar; a tela
    de solicitação manual usa /api/cadastro/contas (lista fixa), não esta."""
    por_chave = {}
    for chave, nome in db.query(ProdutoCadastro.empresa_chave, ProdutoCadastro.empresa_nome).order_by(ProdutoCadastro.id.desc()).all():
        por_chave.setdefault(chave, nome)
    return {"empresas": sorted(por_chave.values(), key=lambda n: chave_conta(n))}


@router.get("/api/cadastro/contas")
def listar_contas_cadastro(db: Session = Depends(get_db), _usuario: Usuario = Depends(_cadastro_logado)):
    """
    Contas ativas (mesma tabela "contas" de "Contas conectadas"), pro seletor de
    Conta da solicitação manual (15/10). Diferente de /empresas: aqui é uma lista
    FIXA, de uma fonte só -- não dá pra ter "Velasco" e "velasco" como duas
    entradas, porque não dá mais pra digitar.
    """
    nomes = (
        db.query(Conta.apelido)
        .filter(Conta.ativa.is_(True), Conta.inativa_em.is_(None))
        .order_by(Conta.apelido)
        .all()
    )
    return {"contas": [n[0] for n in nomes]}


class ProdutoManualIn(BaseModel):
    empresa: str
    produto: str
    sku: str
    fornecedor: Optional[str] = ""
    numero_nota: Optional[str] = ""
    gtin: Optional[str] = ""
    quantidade: float = 0
    custo_unitario: float = 0
    # Marca "refazer": o produto já foi cadastrado antes e está sendo refeito.
    refazer: bool = False


def _incluir_um(db: Session, usuario: Usuario, *, empresa: str, produto: str, sku: str, fornecedor=None, numero_nota=None,
                gtin=None, quantidade: float = 0, custo_unitario: float = 0, refazer: bool = False, link_fotos=None):
    """
    Cria UM produto manual (sem commit -- quem chama confirma). Levanta HTTPException
    com o motivo quando não pode (campo faltando, negativo, SKU já pendente nessa
    empresa). Devolve (registro, aviso_ou_None).
    """
    empresa = (empresa or "").strip()
    sku = (sku or "").strip()
    nome = (produto or "").strip()
    if not empresa or not sku or not nome:
        raise HTTPException(status_code=422, detail="Empresa, SKU e nome do produto são obrigatórios.")
    if quantidade < 0 or custo_unitario < 0:
        raise HTTPException(status_code=422, detail="Quantidade e custo não podem ser negativos.")

    chave_empresa = chave_conta(empresa)
    # Se a empresa já existe com outra grafia, usa a grafia que já está no sistema (um grupo só na tela).
    existente = db.query(ProdutoCadastro.empresa_nome).filter(ProdutoCadastro.empresa_chave == chave_empresa).first()
    if existente:
        empresa = existente[0]

    repetido = (
        db.query(ProdutoCadastro)
        .filter(ProdutoCadastro.empresa_chave == chave_empresa, ProdutoCadastro.sku == sku)
        .order_by(ProdutoCadastro.id.desc())
        .all()
    )
    if any(r.status == "pendente" for r in repetido):
        raise HTTPException(status_code=409, detail="Esse SKU já está na lista de pendentes dessa empresa.")

    registro = ProdutoCadastro(
        nota_id="MANUAL-" + uuid.uuid4().hex[:10],
        empresa_nome=empresa,
        empresa_chave=chave_empresa,
        fornecedor=_limpo(fornecedor),
        numero_nota=_limpo(numero_nota),
        data_nota=datetime.utcnow(),
        sku=sku,
        produto=nome,
        gtin=_limpo(gtin),
        quantidade=quantidade or 0,
        custo_unitario=custo_unitario or 0,
        status="pendente",
        origem="manual",
        criado_por=usuario.nome_exibicao,
        refazer=bool(refazer),
        link_fotos=_limpo(link_fotos),
    )
    db.add(registro)
    db.flush()  # já aparece nas próximas conferências (SKU repetido no mesmo lote)
    aviso = None
    if repetido and not refazer:
        aviso = "Esse SKU já tinha sido cadastrado antes nessa empresa; entrou de novo como pendente."
    return registro, aviso


@router.post("/api/cadastro/produtos")
def incluir_produto_manual(
    corpo: ProdutoManualIn,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(_cadastro_logado),
):
    """
    Inclusão MANUAL de um produto: entra na mesma lista, pendente, marcado como
    "manual" e com quem incluiu. Se já existe um PENDENTE da mesma empresa com o
    mesmo SKU, recusa (409) pra não duplicar. `refazer` marca um cadastro que
    já foi feito antes e está sendo refeito.
    """
    registro, aviso = _incluir_um(
        db, usuario, empresa=corpo.empresa, produto=corpo.produto, sku=corpo.sku, fornecedor=corpo.fornecedor,
        numero_nota=corpo.numero_nota, gtin=corpo.gtin, quantidade=corpo.quantidade, custo_unitario=corpo.custo_unitario,
        refazer=corpo.refazer,
    )
    db.commit()
    db.refresh(registro)
    resposta = {"ok": True, "item": _produto_json(registro)}
    if aviso:
        resposta["aviso"] = aviso
    return resposta


class ItemLoteIn(BaseModel):
    sku: str
    produto: str
    gtin: Optional[str] = ""
    quantidade: float = 0
    custo_unitario: float = 0
    # 15/10: fornecedor/nota POR PRODUTO -- a coluna já existe em ProdutoCadastro
    # (um valor por linha), só não estava exposta aqui. Quando vier preenchido,
    # vale só pra esse item e sobrescreve o do lote; permite uma solicitação
    # juntar produtos de mais de uma nota/fornecedor de uma vez (colando uma
    # planilha com uma coluna "Fornecedor" e outra "Nota").
    fornecedor: Optional[str] = ""
    numero_nota: Optional[str] = ""
    # 16/10: link do Drive (ou onde estiverem) com as fotos do produto -- opcional,
    # por item (cada produto pode ter uma pasta/link diferente).
    link_fotos: Optional[str] = ""


class LoteManualIn(BaseModel):
    empresa: str
    # Fornecedor/nota no nível do lote: valem como PADRÃO pra item que não
    # trouxer o seu próprio (ver ItemLoteIn.fornecedor/numero_nota acima).
    fornecedor: Optional[str] = ""
    numero_nota: Optional[str] = ""
    refazer: bool = False
    # 15/10: "é kit" e os canais marcados valem pra TODOS os produtos desta
    # solicitação -- é uma marcação só, feita uma vez na tela, não por produto.
    # Os dois são obrigatórios na tela (botão Finalizar fica travado sem eles);
    # aqui só confere que não chegou nada vazio/fora do esperado.
    kit: bool
    plataformas: List[str]
    itens: List[ItemLoteIn]


LIMITE_LOTE_MANUAL = 300


@router.post("/api/cadastro/produtos/lote")
def incluir_lote_manual(
    corpo: LoteManualIn,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(_cadastro_logado),
):
    """
    Inclusão manual de VÁRIOS produtos de uma vez (tabela/planilha da tela de
    solicitação). Cada produto é conferido sozinho: os bons entram, os que não
    podem (SKU já pendente, campo faltando, EAN vazio, custo zerado...) voltam
    em `ignorados` com o motivo -- nada some em silêncio. Empresa, "refazer",
    "kit" e os canais marcados (`plataformas`) valem pra todos os produtos da
    solicitação; fornecedor e nota valem por produto (ou o do lote, se o item
    não trouxer o seu -- ver ItemLoteIn), então um envio só pode juntar
    produtos de mais de uma nota/fornecedor. Canal NÃO marcado em
    `plataformas` já entra como "não se aplica" pra cada produto criado --
    não fica pendente esperando um cadastro que nem era pra acontecer ali.
    """
    if not (corpo.empresa or "").strip():
        raise HTTPException(status_code=422, detail="Informe a empresa.")
    if not corpo.itens:
        raise HTTPException(status_code=422, detail="Nenhum produto para incluir.")
    if len(corpo.itens) > LIMITE_LOTE_MANUAL:
        raise HTTPException(status_code=413, detail=f"Máximo de {LIMITE_LOTE_MANUAL} produtos por vez.")
    plataformas = {(p or "").strip().lower() for p in corpo.plataformas}
    if not plataformas:
        raise HTTPException(status_code=422, detail="Escolha pelo menos um canal para cadastro.")
    if not plataformas.issubset(MARKETPLACES):
        raise HTTPException(status_code=422, detail="Canal inválido -- use ml, tiktok ou shopee.")

    criados, ignorados, avisos = [], [], []
    try:
        for n, it in enumerate(corpo.itens, start=1):
            try:
                # EAN e custo > 0 são obrigatórios nesta tela (15/10) -- SKU/nome
                # já são conferidos dentro de _incluir_um.
                if not (it.gtin or "").strip():
                    raise HTTPException(status_code=422, detail="EAN é obrigatório.")
                if not (it.custo_unitario or 0) > 0:
                    raise HTTPException(status_code=422, detail="Custo precisa ser maior que zero.")
                registro, aviso = _incluir_um(
                    db, usuario, empresa=corpo.empresa, produto=it.produto, sku=it.sku,
                    fornecedor=(it.fornecedor or corpo.fornecedor),
                    numero_nota=(it.numero_nota or corpo.numero_nota),
                    gtin=it.gtin, quantidade=it.quantidade, custo_unitario=it.custo_unitario,
                    refazer=corpo.refazer, link_fotos=it.link_fotos,
                )
            except HTTPException as e:
                ignorados.append({"linha": n, "sku": (it.sku or "").strip(), "motivo": e.detail})
                continue
            registro.kit = corpo.kit
            for chave in MARKETPLACES:
                if chave not in plataformas:
                    setattr(registro, f"{chave}_status", "nao_se_aplica")
                    setattr(registro, f"{chave}_por", usuario.nome_exibicao)
                    setattr(registro, f"{chave}_em", datetime.utcnow())
            criados.append(registro)
            if aviso:
                avisos.append({"linha": n, "sku": registro.sku, "aviso": aviso})
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Falha ao gravar o lote manual do Cadastro")
        raise HTTPException(status_code=500, detail="Erro ao gravar os produtos no servidor.")
    return {"ok": True, "criados": [_produto_json(r) for r in criados], "ignorados": ignorados, "avisos": avisos}


def _assumir_se_ninguem(produto: ProdutoCadastro, usuario: Usuario, estado: str) -> None:
    """
    Quem marca um canal em um produto que ninguém assumiu passa a ser quem
    assumiu (o relógio de "quanto tempo levou" começa aí). Só conta marcar de
    verdade -- voltar a "pendente" não assume nada.
    """
    if estado != "pendente" and not produto.assumido_por:
        produto.assumido_por = usuario.nome_exibicao
        produto.assumido_em = datetime.utcnow()


def _marcar_plataformas_pedidas(produto: ProdutoCadastro, usuario: Usuario, plataformas: List[str]) -> None:
    """
    16/10: marca quais marketplaces uma solicitação AUTOMÁTICA (nota fiscal)
    precisa -- usado tanto no momento de assumir quanto na marcação antecipada
    do supervisor/gerente (ver /plataformas). Só deve ser chamada quando os 3
    canais ainda estão em branco (quem chama confere isso antes); o que não
    foi escolhido aqui já marca sozinho como "não se aplica", igual já
    acontece na inclusão manual/seller (ver _incluir_um e incluir_lote_*).
    """
    escolhidas = {(p or "").strip().lower() for p in plataformas}
    if not escolhidas:
        raise HTTPException(status_code=422, detail="Escolha pelo menos um canal para cadastro.")
    if not escolhidas.issubset(MARKETPLACES):
        raise HTTPException(status_code=422, detail="Canal inválido -- use ml, tiktok ou shopee.")
    agora = datetime.utcnow()
    for chave in MARKETPLACES:
        if chave not in escolhidas:
            setattr(produto, f"{chave}_status", "nao_se_aplica")
            setattr(produto, f"{chave}_por", usuario.nome_exibicao)
            setattr(produto, f"{chave}_em", agora)


class PlataformasIn(BaseModel):
    plataformas: List[str]


@router.post("/api/cadastro/produtos/{produto_id}/plataformas")
def definir_plataformas_produto(
    produto_id: int,
    corpo: PlataformasIn,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(_supervisao_logado),
):
    """
    16/10: supervisor/gerente (ou admin) marca ANTES de alguém assumir quais
    marketplaces uma solicitação automática (nota fiscal) precisa -- pra quando
    o operador for assumir já estar tudo definido. Só funciona uma vez, enquanto
    os 3 canais ainda estão em branco -- se já tiver algo marcado (por quem for),
    recusa; ninguém troca a escolha depois de feita.
    """
    produto = db.query(ProdutoCadastro).filter(ProdutoCadastro.id == produto_id).first()
    if produto is None:
        raise HTTPException(status_code=404, detail="Produto não encontrado.")
    if produto.status == "cadastrado":
        raise HTTPException(status_code=423, detail="Este produto já foi confirmado -- está travado.")
    if not all(_estado_mp(produto, k) is None for k in MARKETPLACES):
        raise HTTPException(status_code=409, detail="Esse produto já tem marketplace marcado -- não dá mais pra escolher de novo.")
    _marcar_plataformas_pedidas(produto, usuario, corpo.plataformas)
    db.commit()
    logger.info("Plataformas definidas por %s (id=%s): %s", usuario.nome_exibicao, produto_id, corpo.plataformas)
    return {"ok": True, "item": _produto_json(produto)}


class AssumirIn(BaseModel):
    assumir: bool = True  # False = soltar (volta a "ninguém assumiu")
    forcar: bool = False  # True = assumir mesmo que outra pessoa já tenha assumido


@router.post("/api/cadastro/produtos/{produto_id}/assumir")
def assumir_produto(
    produto_id: int,
    corpo: AssumirIn,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(_cadastro_logado),
):
    """
    "Assumi este cadastro": guarda quem e quando (o tempo de cadastro conta daqui).
    Se outra pessoa já tinha assumido, recusa (409) a menos que `forcar` -- a tela
    pergunta antes ("Ana já assumiu. Assumir mesmo assim?"). Soltar só vale pra
    quem assumiu (ou supervisor/admin).

    16/10: a escolha de QUAIS marketplaces essa solicitação precisa não é feita
    aqui -- é sempre antes, pelo supervisor/gerente (ver /plataformas). Um
    produto automático (nota fiscal) sem isso definido continua sendo assumido
    normalmente, só que com os 3 marketplaces em aberto (comportamento de sempre).
    """
    produto = db.query(ProdutoCadastro).filter(ProdutoCadastro.id == produto_id).first()
    if produto is None:
        raise HTTPException(status_code=404, detail="Produto não encontrado.")
    if corpo.assumir:
        if _precisa_definir_plataformas(produto):
            raise HTTPException(status_code=409, detail="Aguarde um supervisor/gerente definir quais marketplaces essa solicitação precisa antes de assumir.")
        if produto.assumido_por and produto.assumido_por != usuario.nome_exibicao and not corpo.forcar:
            raise HTTPException(status_code=409, detail=f"{produto.assumido_por} já assumiu este cadastro.")
        if produto.assumido_por != usuario.nome_exibicao:
            produto.assumido_por = usuario.nome_exibicao
            produto.assumido_em = datetime.utcnow()
    else:
        if produto.assumido_por and produto.assumido_por != usuario.nome_exibicao and usuario.papel not in ("admin", "supervisor"):
            raise HTTPException(status_code=403, detail="Só quem assumiu (ou o supervisor) pode soltar.")
        produto.assumido_por = None
        produto.assumido_em = None
    db.commit()
    return {"ok": True, "item": _produto_json(produto)}


class ReabrirIn(BaseModel):
    motivo: str


@router.post("/api/cadastro/produtos/{produto_id}/reabrir")
def reabrir_produto(
    produto_id: int,
    corpo: ReabrirIn,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(_supervisao_logado),  # só admin/supervisor -- ver "TRAVA" no topo do arquivo
):
    """
    Reabre um produto TRAVADO (já cadastrado/confirmado), exigindo o motivo.
    Limpa as 3 marcações de marketplace e quem assumiu -- volta pra fila como
    se fosse um cadastro novo, mas com o rastro de quem reabriu, quando e por
    quê (reaberto_por/reaberto_em/motivo_reabertura) e marcado como "refazer"
    pra quem for cadastrar saber que já foi feito antes.
    """
    motivo = (corpo.motivo or "").strip()
    if len(motivo) < 5:
        raise HTTPException(status_code=422, detail="Informe o motivo da reabertura (mínimo 5 caracteres).")

    produto = db.query(ProdutoCadastro).filter(ProdutoCadastro.id == produto_id).first()
    if produto is None:
        raise HTTPException(status_code=404, detail="Produto não encontrado.")
    if produto.status != "cadastrado":
        raise HTTPException(status_code=409, detail="Este produto não está travado (ainda não foi cadastrado).")

    for k in MARKETPLACES:
        setattr(produto, f"{k}_status", None)
        setattr(produto, f"{k}_por", None)
        setattr(produto, f"{k}_em", None)
    produto.assumido_por = None
    produto.assumido_em = None
    produto.refazer = True
    produto.reaberto_por = usuario.nome_exibicao
    produto.reaberto_em = datetime.utcnow()
    produto.motivo_reabertura = motivo
    _recalcular_status(produto, usuario.nome_exibicao)  # os 3 ficaram vazios -> volta pra "pendente" sozinho

    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Falha ao reabrir produto do Cadastro (id=%s)", produto_id)
        raise HTTPException(status_code=500, detail="Erro ao reabrir no servidor.")

    logger.info("Produto de cadastro reaberto: id=%s por=%s motivo=%s", produto_id, usuario.nome_exibicao, motivo)
    return {"ok": True, "item": _produto_json(produto)}


class ExcluirIn(BaseModel):
    motivo: str


@router.post("/api/cadastro/produtos/{produto_id}/excluir")
def excluir_produto(
    produto_id: int,
    corpo: ExcluirIn,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(_admin_supervisor_logado),  # só admin/supervisor -- ver _admin_supervisor_logado
):
    """
    Exclui uma solicitação PENDENTE (15/10). Não apaga a linha -- marca
    status="excluida" e grava quem excluiu, quando e o motivo (obrigatório,
    mínimo 5 caracteres). Some de qualquer aba da tela (Pendentes/Cadastrados/
    Todos), mas continua no banco pra consultar depois se precisar. Só vale
    pra pendente: produto já cadastrado (travado) ou já excluído não entra
    por aqui.
    """
    motivo = (corpo.motivo or "").strip()
    if len(motivo) < 5:
        raise HTTPException(status_code=422, detail="Informe o motivo da exclusão (mínimo 5 caracteres).")

    produto = db.query(ProdutoCadastro).filter(ProdutoCadastro.id == produto_id).first()
    if produto is None:
        raise HTTPException(status_code=404, detail="Produto não encontrado.")
    if produto.status != "pendente":
        raise HTTPException(status_code=409, detail="Só é possível excluir uma solicitação pendente.")

    produto.status = "excluida"
    produto.excluido_por = usuario.nome_exibicao
    produto.excluido_em = datetime.utcnow()
    produto.motivo_exclusao = motivo

    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Falha ao excluir produto do Cadastro (id=%s)", produto_id)
        raise HTTPException(status_code=500, detail="Erro ao excluir no servidor.")

    logger.info("Produto de cadastro excluído: id=%s por=%s motivo=%s", produto_id, usuario.nome_exibicao, motivo)
    return {"ok": True, "id": produto.id}


class MarketplaceIn(BaseModel):
    marketplace: str  # "ml" | "tiktok" | "shopee"
    estado: str  # "feito" | "nao_se_aplica" | "pendente"


@router.post("/api/cadastro/produtos/{produto_id}/marketplace")
def marcar_marketplace(
    produto_id: int,
    corpo: MarketplaceIn,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(_cadastro_logado),
):
    """
    Marca um marketplace do produto (feito / não se aplica / volta a pendente),
    guardando quem fez e quando. Cada chamada mexe só no marketplace pedido --
    duas pessoas cadastrando marketplaces diferentes do mesmo produto não se
    atropelam.

    16/10: marcar o ÚLTIMO marketplace pendente NÃO fecha mais o produto
    sozinho -- só deixa todos "resolvidos" (feito ou não se aplica), esperando
    o Confirmar explícito (ver /confirmar) travar de vez.
    """
    chave = (corpo.marketplace or "").strip().lower()
    estado = (corpo.estado or "").strip().lower()
    if chave not in MARKETPLACES:
        raise HTTPException(status_code=422, detail="marketplace precisa ser ml, tiktok ou shopee.")
    if estado not in ESTADOS_MARKETPLACE:
        raise HTTPException(status_code=422, detail="estado precisa ser feito, nao_se_aplica ou pendente.")

    produto = db.query(ProdutoCadastro).filter(ProdutoCadastro.id == produto_id).first()
    if produto is None:
        raise HTTPException(status_code=404, detail="Produto não encontrado.")
    if produto.status == "cadastrado":
        raise HTTPException(
            status_code=423,
            detail="Este produto já foi cadastrado e confirmado -- está travado. Peça a um supervisor para reabrir, informando o motivo.",
        )
    if estado != "pendente" and _precisa_definir_plataformas(produto):
        raise HTTPException(status_code=409, detail="Aguarde um supervisor/gerente definir quais marketplaces essa solicitação precisa antes de marcar.")

    if estado == "pendente":
        setattr(produto, f"{chave}_status", None)
        setattr(produto, f"{chave}_por", None)
        setattr(produto, f"{chave}_em", None)
    else:
        setattr(produto, f"{chave}_status", estado)
        setattr(produto, f"{chave}_por", usuario.nome_exibicao)
        setattr(produto, f"{chave}_em", datetime.utcnow())
    _assumir_se_ninguem(produto, usuario, estado)
    _recalcular_status(produto, usuario.nome_exibicao)
    db.commit()
    return {"ok": True, "item": _produto_json(produto)}


@router.post("/api/cadastro/produtos/{produto_id}/confirmar")
def confirmar_produto(
    produto_id: int,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(_cadastro_logado),
):
    """
    16/10: trava de vez o cadastro -- a ÚNICA forma do produto virar
    "cadastrado" (marcar o último marketplace sozinho não fecha mais, ver
    marcar_marketplace/_recalcular_status acima). Só libera quando TODOS os
    marketplaces estiverem resolvidos (feito ou não se aplica); a tela nem
    deve deixar clicar antes disso, mas o servidor confere de novo aqui (409
    se ainda faltar algum) -- nunca confia só na tela.

    Carimba quem confirmou e quando nos MESMOS campos que já existiam
    (cadastrado_por/cadastrado_em) -- só muda que agora são preenchidos por
    uma ação explícita da pessoa, não mais sozinho. Dá pra calcular "tempo
    levado" como cadastrado_em - assumido_em, sem precisar de nenhum campo
    novo. Reabrir (só admin/supervisor, motivo obrigatório) é a única forma
    de desfazer isso depois -- ver /reabrir.
    """
    produto = db.query(ProdutoCadastro).filter(ProdutoCadastro.id == produto_id).first()
    if produto is None:
        raise HTTPException(status_code=404, detail="Produto não encontrado.")
    if produto.status == "cadastrado":
        raise HTTPException(status_code=423, detail="Este produto já foi confirmado -- está travado.")
    if not _todos_resolvidos(produto):
        raise HTTPException(status_code=409, detail="Marque todos os marketplaces pedidos antes de confirmar.")

    produto.status = "cadastrado"
    produto.cadastrado_por = usuario.nome_exibicao
    produto.cadastrado_em = datetime.utcnow()
    _assumir_se_ninguem(produto, usuario, "feito")  # garantia: se ninguém tinha assumido, confirmar também assume

    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Falha ao confirmar produto do Cadastro (id=%s)", produto_id)
        raise HTTPException(status_code=500, detail="Erro ao confirmar no servidor.")

    logger.info("Produto de cadastro confirmado: id=%s por=%s", produto_id, usuario.nome_exibicao)
    return {"ok": True, "item": _produto_json(produto)}


class StatusCadastroIn(BaseModel):
    cadastrado: bool


@router.post("/api/cadastro/produtos/{produto_id}/status")
def alterar_status_cadastro(
    produto_id: int,
    corpo: StatusCadastroIn,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(_cadastro_logado),
):
    """
    Atalho dos 3 de uma vez. Marcar: o que ainda faltava vira "feito" por quem
    clicou (o que já estava marcado por outra pessoa ou como "não se aplica"
    fica como está). Desmarcar: limpa os 3 e volta a pendente.

    16/10: marcar aqui também NÃO fecha mais o produto sozinho -- deixa tudo
    resolvido, esperando o Confirmar explícito (ver /confirmar) travar de vez,
    igual ao marcar_marketplace.
    """
    produto = db.query(ProdutoCadastro).filter(ProdutoCadastro.id == produto_id).first()
    if produto is None:
        raise HTTPException(status_code=404, detail="Produto não encontrado.")
    if produto.status == "cadastrado":
        raise HTTPException(
            status_code=423,
            detail="Este produto já foi cadastrado e confirmado -- está travado. Peça a um supervisor para reabrir, informando o motivo.",
        )
    if corpo.cadastrado and _precisa_definir_plataformas(produto):
        raise HTTPException(status_code=409, detail="Aguarde um supervisor/gerente definir quais marketplaces essa solicitação precisa antes de marcar.")
    agora = datetime.utcnow()
    for k in MARKETPLACES:
        if corpo.cadastrado:
            if _estado_mp(produto, k) is None:
                setattr(produto, f"{k}_status", "feito")
                setattr(produto, f"{k}_por", usuario.nome_exibicao)
                setattr(produto, f"{k}_em", agora)
        else:
            setattr(produto, f"{k}_status", None)
            setattr(produto, f"{k}_por", None)
            setattr(produto, f"{k}_em", None)
    if corpo.cadastrado:
        _assumir_se_ninguem(produto, usuario, "feito")
    _recalcular_status(produto, usuario.nome_exibicao)
    db.commit()
    return {"ok": True, "item": _produto_json(produto), **{
        "id": produto.id,
        "status": produto.status,
        "cadastrado_por": produto.cadastrado_por,
        "cadastrado_em": _iso_utc(produto.cadastrado_em),
    }}


# ---------------------------------------------------------------------------
# Produtividade (só admin e supervisor)
# ---------------------------------------------------------------------------
def _fuso_brasilia():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/Sao_Paulo")
    except Exception:  # sem base de fusos instalada: Brasil não tem horário de verão desde 2019
        return timezone(timedelta(hours=-3))


def _limites_utc(de: date, ate: date):
    """Dias (inclusive) no horário de Brasília -> [início, fim) em UTC "cru", como o banco guarda."""
    fuso = _fuso_brasilia()
    inicio = datetime(de.year, de.month, de.day, tzinfo=fuso).astimezone(timezone.utc).replace(tzinfo=None)
    dia_seguinte = ate + timedelta(days=1)
    fim = datetime(dia_seguinte.year, dia_seguinte.month, dia_seguinte.day, tzinfo=fuso).astimezone(timezone.utc).replace(tzinfo=None)
    return inicio, fim


def _hoje_brasilia() -> date:
    return datetime.now(_fuso_brasilia()).date()


def _no_periodo(data: Optional[datetime], inicio: datetime, fim: datetime) -> bool:
    return data is not None and inicio <= data < fim


def _agregar_produtividade(linhas, inicio: datetime, fim: datetime) -> list:
    """
    Conta, por pessoa, o que ela fez dentro de [inicio, fim) (UTC). Função
    pura (recebe as linhas já lidas) pra ser testável sem banco:
      - ml/tiktok/shopee: marcações "feito" naquele marketplace;
      - total: soma dos 3;
      - produtos_fechados: produtos que ela completou (foi a última a resolver);
      - incluidos_manual: produtos que ela incluiu à mão.
    "Não se aplica" NÃO conta como produção.
    """
    pessoas = {}

    def _pessoa(nome):
        nome = nome or "—"
        return pessoas.setdefault(
            nome, {"nome": nome, "ml": 0, "tiktok": 0, "shopee": 0, "total": 0, "produtos_fechados": 0, "incluidos_manual": 0,
             "tempo_soma_seg": 0, "tempo_n": 0}
        )

    for p in linhas:
        for k in MARKETPLACES:
            if _estado_mp(p, k) == "feito" and _no_periodo(getattr(p, f"{k}_em"), inicio, fim):
                pessoa = _pessoa(getattr(p, f"{k}_por"))
                pessoa[k] += 1
                pessoa["total"] += 1
        if p.status == "cadastrado" and _no_periodo(p.cadastrado_em, inicio, fim):
            pessoa = _pessoa(p.cadastrado_por)
            pessoa["produtos_fechados"] += 1
            # tempo que levou: de quando alguém assumiu até o produto ficar OK
            if p.assumido_em and p.cadastrado_em and p.cadastrado_em >= p.assumido_em:
                pessoa["tempo_soma_seg"] += int((p.cadastrado_em - p.assumido_em).total_seconds())
                pessoa["tempo_n"] += 1
        if p.origem == "manual" and _no_periodo(p.recebido_em, inicio, fim):
            _pessoa(p.criado_por)["incluidos_manual"] += 1

    for pessoa in pessoas.values():
        pessoa["tempo_medio_seg"] = (pessoa["tempo_soma_seg"] // pessoa["tempo_n"]) if pessoa["tempo_n"] else None
    return sorted(pessoas.values(), key=lambda x: (-x["total"], -x["produtos_fechados"], x["nome"].lower()))


def _agregar_fila(pendentes) -> dict:
    """Fila de agora: dos produtos pendentes, em quantos ainda falta cada marketplace."""
    fila = {k: 0 for k in MARKETPLACES}
    for p in pendentes:
        for k in MARKETPLACES:
            if _estado_mp(p, k) is None:
                fila[k] += 1
    fila["produtos_pendentes"] = len(pendentes)
    return fila


@router.get("/api/cadastro/produtividade")
def produtividade_cadastro(
    de: Optional[str] = None,
    ate: Optional[str] = None,
    db: Session = Depends(get_db),
    _usuario: Usuario = Depends(_supervisao_logado),
):
    """
    Quanto cada pessoa cadastrou no período (datas no horário de Brasília,
    padrão: hoje). Por pessoa: cadastros em cada marketplace, o total, quantos
    produtos ela FECHOU e quantos incluiu à mão. Também devolve a fila de agora
    (quanto ainda falta em cada marketplace), que não depende do período.
    """
    try:
        data_de = date.fromisoformat(de) if de else _hoje_brasilia()
        data_ate = date.fromisoformat(ate) if ate else data_de
    except ValueError:
        raise HTTPException(status_code=422, detail="Datas no formato AAAA-MM-DD.")
    if data_ate < data_de:
        raise HTTPException(status_code=422, detail="A data final não pode ser antes da inicial.")
    if (data_ate - data_de).days > 366:
        raise HTTPException(status_code=422, detail="Escolha no máximo 1 ano de intervalo.")
    inicio, fim = _limites_utc(data_de, data_ate)

    # Só lê as linhas que podem contar no período (o resto não muda nenhum número).
    condicoes = [
        and_(ProdutoCadastro.cadastrado_em >= inicio, ProdutoCadastro.cadastrado_em < fim),
        and_(ProdutoCadastro.origem == "manual", ProdutoCadastro.recebido_em >= inicio, ProdutoCadastro.recebido_em < fim),
    ]
    for k in MARKETPLACES:
        em = getattr(ProdutoCadastro, f"{k}_em")
        condicoes.append(and_(em >= inicio, em < fim))
    linhas = db.query(ProdutoCadastro).filter(or_(*condicoes)).all()
    pessoas = _agregar_produtividade(linhas, inicio, fim)

    totais = {"ml": 0, "tiktok": 0, "shopee": 0, "total": 0, "produtos_fechados": 0, "incluidos_manual": 0}
    for pessoa in pessoas:
        for campo in totais:
            totais[campo] += pessoa[campo]
    soma_t = sum(x["tempo_soma_seg"] for x in pessoas)
    n_t = sum(x["tempo_n"] for x in pessoas)
    totais["tempo_medio_seg"] = (soma_t // n_t) if n_t else None

    pendentes = db.query(ProdutoCadastro).filter(ProdutoCadastro.status == "pendente").all()
    return {
        "de": data_de.isoformat(),
        "ate": data_ate.isoformat(),
        "pessoas": pessoas,
        "totais": totais,
        "fila": _agregar_fila(pendentes),
        "marketplaces": MARKETPLACES,
    }
