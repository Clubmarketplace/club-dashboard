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
  POST /api/cadastro/produtos/{id}/status-> marca/desmarca "cadastrado".
        (as duas últimas: só admin e supervisor)

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
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, or_
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
    """Tela e API de Cadastro: só admin e supervisor (o acesso dos demais perfis já é barrado no middleware)."""
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
# Tela "Cadastro" (admin e supervisor)
# ---------------------------------------------------------------------------
def _iso_utc(data: Optional[datetime]) -> Optional[str]:
    """Datas do banco são UTC sem fuso; manda com 'Z' pro navegador converter pro horário de Brasília."""
    return (data.isoformat() + "Z") if data else None


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

    return {
        "itens": [
            {
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
            }
            for p in linhas
        ],
        "contagem": contagem,
        "truncado": truncado,
    }


class StatusCadastroIn(BaseModel):
    cadastrado: bool


@router.post("/api/cadastro/produtos/{produto_id}/status")
def alterar_status_cadastro(
    produto_id: int,
    corpo: StatusCadastroIn,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(_cadastro_logado),
):
    """Marca (ou desmarca) um produto como cadastrado, guardando quem fez e quando."""
    produto = db.query(ProdutoCadastro).filter(ProdutoCadastro.id == produto_id).first()
    if produto is None:
        raise HTTPException(status_code=404, detail="Produto não encontrado.")
    if corpo.cadastrado:
        produto.status = "cadastrado"
        produto.cadastrado_por = usuario.nome_exibicao
        produto.cadastrado_em = datetime.utcnow()
    else:
        produto.status = "pendente"
        produto.cadastrado_por = None
        produto.cadastrado_em = None
    db.commit()
    return {
        "ok": True,
        "id": produto.id,
        "status": produto.status,
        "cadastrado_por": produto.cadastrado_por,
        "cadastrado_em": _iso_utc(produto.cadastrado_em),
    }
