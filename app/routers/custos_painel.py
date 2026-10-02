"""
Rotas de custos por SKU acessadas PELO PAINEL (cookie de sessão do
seller), não pela extensão. Mesma tabela (CustoSku) e mesma regra de
isolamento por conta que app/routers/cmx.py -- só muda de onde vem o
usuário logado (cookie aqui, Bearer token lá), então reaproveitamos a
função `_conta_vinculada_do_usuario` de lá em vez de duplicar a lógica.

A extensão continua só LENDO (GET /api/cmx/custos) -- é o painel que
agora ganha a forma de ESCREVER pensada pra gente (não seller com a
extensão instalada): buscar por SKU, editar um item, ou importar uma
planilha inteira de uma vez.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app import auth
from app.database import get_db
from app.models import CustoSku, Usuario
from app.routers.cmx import _conta_vinculada_do_usuario, _serializar_conta

router = APIRouter(prefix="/api/painel/custos", tags=["painel-custos"])


def _seller_logado(request: Request, db: Session = Depends(get_db)) -> Usuario:
    """
    Dependency equivalente ao usuario_logado_cmx, só que lendo o cookie
    de sessão do painel em vez do cabeçalho Authorization -- o
    middleware exigir_login (main.py) já barrou quem não está logado
    antes de chegar aqui; isso só confirma que é mesmo um seller.
    """
    usuario = auth.usuario_atual(request, db)
    if not usuario or usuario.papel != "seller":
        raise HTTPException(status_code=403, detail="Acesso restrito a sellers.")
    return usuario


class CustoSkuEntrada(BaseModel):
    sku: str
    custo: float
    nome_produto: str | None = None

    @field_validator("sku")
    @classmethod
    def _sku_nao_vazio(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("SKU não pode ser vazio.")
        return v

    @field_validator("custo")
    @classmethod
    def _custo_positivo(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("Custo precisa ser maior que zero.")
        return v


class CustosEntrada(BaseModel):
    itens: list[CustoSkuEntrada]


def _serializar_custo(c: CustoSku) -> dict:
    return {
        "sku": c.sku,
        "custo": c.custo,
        "nome_produto": c.nome_produto,
        "atualizado_em": c.atualizado_em.isoformat() if c.atualizado_em else None,
        "atualizado_por": c.atualizado_por,
    }


@router.get("")
def listar_ou_buscar_custos(
    sku: str = "",
    usuario: Usuario = Depends(_seller_logado),
    db: Session = Depends(get_db),
):
    """
    Sem `sku`: devolve todos os custos da conta (usado pra listar a
    tela). Com `sku`: filtra por trecho do SKU (contém, sem diferenciar
    maiúsculas/minúsculas) -- é a busca "encontrar pra editar" da tela.
    """
    conta = _conta_vinculada_do_usuario(usuario, db)
    consulta = db.query(CustoSku).filter(CustoSku.conta_id == conta.id)
    termo = sku.strip()
    if termo:
        consulta = consulta.filter(CustoSku.sku.ilike(f"%{termo}%"))
    custos = consulta.order_by(CustoSku.sku).all()
    return {
        "conta": _serializar_conta(conta),
        "itens": [_serializar_custo(c) for c in custos],
    }


@router.post("")
def gravar_custos(
    dados: CustosEntrada,
    usuario: Usuario = Depends(_seller_logado),
    db: Session = Depends(get_db),
):
    """
    Grava um custo (edição/criação individual pela tela) ou vários de
    uma vez (importação de planilha) -- mesmo formato de entrada
    {"itens": [...]} nos dois casos, só muda quantos itens a tela manda
    de uma vez. Sempre isolado pela conta do usuário logado, igual à
    rota equivalente da extensão (app/routers/cmx.py:atualizar_custos).
    """
    if not dados.itens:
        raise HTTPException(status_code=400, detail="Nenhum item enviado.")

    conta = _conta_vinculada_do_usuario(usuario, db)
    existentes = {
        c.sku: c
        for c in db.query(CustoSku).filter(CustoSku.conta_id == conta.id).all()
    }

    resultado = []
    for item in dados.itens:
        registro = existentes.get(item.sku)
        custo_anterior = registro.custo if registro else None

        if registro is None:
            registro = CustoSku(conta_id=conta.id, sku=item.sku)
            db.add(registro)
            existentes[item.sku] = registro  # evita duplicar se o SKU repetir no mesmo lote

        registro.custo = item.custo
        if item.nome_produto:
            registro.nome_produto = item.nome_produto
        registro.atualizado_por = usuario.nome_exibicao

        resultado.append(
            {
                "sku": item.sku,
                "custo_anterior": custo_anterior,
                "custo_novo": item.custo,
                "alterado": custo_anterior != item.custo,
            }
        )

    db.commit()
    return {"total_processados": len(resultado), "itens": resultado}


@router.delete("/{sku}")
def remover_custo(
    sku: str,
    usuario: Usuario = Depends(_seller_logado),
    db: Session = Depends(get_db),
):
    """Remove um SKU cadastrado por engano -- da conta do usuário logado, nunca de outra."""
    conta = _conta_vinculada_do_usuario(usuario, db)
    registro = (
        db.query(CustoSku)
        .filter(CustoSku.conta_id == conta.id, CustoSku.sku == sku)
        .first()
    )
    if registro is None:
        raise HTTPException(status_code=404, detail="SKU não encontrado nessa conta.")
    db.delete(registro)
    db.commit()
    return {"removido": sku}
