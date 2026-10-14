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
  POST /api/cadastro/produtos/lote       -> inclusão MANUAL de VÁRIOS produtos de uma nota (colar da planilha).
  POST /api/cadastro/produtos/{id}/assumir -> "assumi este cadastro" (ou solta); guarda quem e quando.
  POST /api/cadastro/produtos/{id}/reabrir -> reabre um produto TRAVADO (já cadastrado), com
        motivo obrigatório. Só admin/supervisor (ver "TRAVA" abaixo).
  GET  /api/cadastro/empresas            -> nomes de empresa já usados (sugestão no formulário).
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
from app.models import ProdutoCadastro, Usuario

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
    """Tela e API de Cadastro: admin, supervisor e o perfil "cadastro" (os demais perfis já são barrados no middleware)."""
    usuario = auth.usuario_atual(request, db)
    if not auth.papel_permite(usuario, ("admin", "supervisor", "cadastro")):
        raise HTTPException(status_code=403, detail="Acesso restrito.")
    return usuario


def _supervisao_logado(request: Request, db: Session = Depends(get_db)) -> Usuario:
    """Produtividade (quanto cada um cadastrou): só admin e supervisor -- o perfil "cadastro" não vê o número dos colegas."""
    usuario = auth.usuario_atual(request, db)
    if not auth.papel_permite(usuario, ("admin", "supervisor")):
        raise HTTPException(status_code=403, detail="Acesso restrito.")
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


def _recalcular_status(produto: ProdutoCadastro, usuario_nome: str) -> None:
    """
    O produto fica "cadastrado" (OK) quando os 3 marketplaces estão resolvidos
    (feito OU não se aplica). Quem resolveu o ÚLTIMO é gravado como quem
    fechou o produto; ao reabrir algum, volta a "pendente" e limpa isso.
    """
    resolvidos = all(_estado_mp(produto, k) in ("feito", "nao_se_aplica") for k in MARKETPLACES)
    if resolvidos:
        if produto.status != "cadastrado":
            produto.status = "cadastrado"
            produto.cadastrado_por = usuario_nome
            produto.cadastrado_em = datetime.utcnow()
    else:
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
        "marketplaces": _dados_marketplaces(p),
        "refazer": bool(p.refazer),
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

    consulta = db.query(ProdutoCadastro)
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
    """Nomes de empresa já usados (uma por chave), pra sugerir no formulário de inclusão manual e evitar 'JC Machado' x 'JC MACHADO'."""
    por_chave = {}
    for chave, nome in db.query(ProdutoCadastro.empresa_chave, ProdutoCadastro.empresa_nome).order_by(ProdutoCadastro.id.desc()).all():
        por_chave.setdefault(chave, nome)
    return {"empresas": sorted(por_chave.values(), key=lambda n: chave_conta(n))}


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
                gtin=None, quantidade: float = 0, custo_unitario: float = 0, refazer: bool = False):
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


class LoteManualIn(BaseModel):
    empresa: str
    fornecedor: Optional[str] = ""
    numero_nota: Optional[str] = ""
    refazer: bool = False
    itens: List[ItemLoteIn]


LIMITE_LOTE_MANUAL = 300


@router.post("/api/cadastro/produtos/lote")
def incluir_lote_manual(
    corpo: LoteManualIn,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(_cadastro_logado),
):
    """
    Inclusão manual de VÁRIOS produtos de uma nota de uma vez (copiado e colado da
    planilha). Cada produto é conferido sozinho: os bons entram, os que não podem
    (SKU já pendente, campo faltando...) voltam em `ignorados` com o motivo --
    nada some em silêncio. Empresa, fornecedor, nota e a marca "refazer" valem pra todos.
    """
    if not (corpo.empresa or "").strip():
        raise HTTPException(status_code=422, detail="Informe a empresa.")
    if not corpo.itens:
        raise HTTPException(status_code=422, detail="Nenhum produto para incluir.")
    if len(corpo.itens) > LIMITE_LOTE_MANUAL:
        raise HTTPException(status_code=413, detail=f"Máximo de {LIMITE_LOTE_MANUAL} produtos por vez.")
    criados, ignorados, avisos = [], [], []
    try:
        for n, it in enumerate(corpo.itens, start=1):
            try:
                registro, aviso = _incluir_um(
                    db, usuario, empresa=corpo.empresa, produto=it.produto, sku=it.sku, fornecedor=corpo.fornecedor,
                    numero_nota=corpo.numero_nota, gtin=it.gtin, quantidade=it.quantidade, custo_unitario=it.custo_unitario,
                    refazer=corpo.refazer,
                )
            except HTTPException as e:
                ignorados.append({"linha": n, "sku": (it.sku or "").strip(), "motivo": e.detail})
                continue
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
    """
    produto = db.query(ProdutoCadastro).filter(ProdutoCadastro.id == produto_id).first()
    if produto is None:
        raise HTTPException(status_code=404, detail="Produto não encontrado.")
    if corpo.assumir:
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
    guardando quem fez e quando. Quando os 3 estão resolvidos o produto vira
    "cadastrado" (OK) sozinho. Cada chamada mexe só no marketplace pedido -- duas
    pessoas cadastrando marketplaces diferentes do mesmo produto não se atropelam.
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
    """
    produto = db.query(ProdutoCadastro).filter(ProdutoCadastro.id == produto_id).first()
    if produto is None:
        raise HTTPException(status_code=404, detail="Produto não encontrado.")
    if produto.status == "cadastrado":
        raise HTTPException(
            status_code=423,
            detail="Este produto já foi cadastrado e confirmado -- está travado. Peça a um supervisor para reabrir, informando o motivo.",
        )
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
