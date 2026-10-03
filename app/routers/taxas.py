"""
Tela "Taxas" do painel do seller: cadastro das variáveis percentuais
(imposto, CLUB etc) que entram no cálculo da margem -- tanto no Lucro
Líquido da tela "Vendas" quanto na extensão ClubMarketplaceX (que
busca essas mesmas taxas no servidor via app/routers/cmx.py, em vez de
guardar localmente no chrome.storage.local de cada PC).

Mesma tabela (VariavelConta) e mesmo padrão de autenticação/isolamento
por conta do resto do painel (custos_painel.py / produtos.py / vendas.py):
cookie de sessão do seller, `_conta_vinculada_do_usuario` garante que
uma conta nunca vê/edita taxa de outra.

Por decisão do cliente, por enquanto só existe taxa percentual -- valor
fixo (ex: "contador R$500/mês") ficou fora de escopo (ver VariavelConta
em app/models.py).
"""
from pydantic import BaseModel, field_validator
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import VariavelConta
from app.routers.custos_painel import _seller_logado
from app.routers.cmx import _conta_vinculada_do_usuario

router = APIRouter(prefix="/api/painel/taxas", tags=["painel-taxas"])

_BASES_VALIDAS = {"venda_bruta", "repasse", "lucro"}


class TaxaEntrada(BaseModel):
    nome: str
    percentual: float
    base_calculo: str = "venda_bruta"

    @field_validator("nome")
    @classmethod
    def _nome_nao_vazio(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("Nome não pode ser vazio.")
        return v

    @field_validator("percentual")
    @classmethod
    def _percentual_valido(cls, v: float) -> float:
        if v < 0:
            raise ValueError("Percentual não pode ser negativo.")
        return v

    @field_validator("base_calculo")
    @classmethod
    def _base_valida(cls, v: str) -> str:
        v = (v or "venda_bruta").strip()
        if v not in _BASES_VALIDAS:
            raise ValueError("Base de cálculo inválida (use venda_bruta, repasse ou lucro).")
        return v


def _serializar(v: VariavelConta) -> dict:
    return {
        "nome": v.nome,
        "percentual": v.percentual,
        "base_calculo": v.base_calculo or "venda_bruta",
        "atualizado_em": v.atualizado_em.isoformat() if v.atualizado_em else None,
        "atualizado_por": v.atualizado_por,
    }


@router.get("")
def listar_taxas(usuario=Depends(_seller_logado), db: Session = Depends(get_db)):
    conta = _conta_vinculada_do_usuario(usuario, db)
    taxas = db.query(VariavelConta).filter(VariavelConta.conta_id == conta.id).order_by(VariavelConta.nome).all()
    return {"itens": [_serializar(v) for v in taxas]}


@router.post("")
def gravar_taxa(dados: TaxaEntrada, usuario=Depends(_seller_logado), db: Session = Depends(get_db)):
    conta = _conta_vinculada_do_usuario(usuario, db)
    registro = (
        db.query(VariavelConta)
        .filter(VariavelConta.conta_id == conta.id, VariavelConta.nome == dados.nome)
        .first()
    )
    if registro is None:
        registro = VariavelConta(conta_id=conta.id, nome=dados.nome)
        db.add(registro)
    registro.percentual = dados.percentual
    registro.base_calculo = dados.base_calculo
    registro.atualizado_por = usuario.nome_exibicao
    db.commit()
    return _serializar(registro)


@router.delete("/{nome}")
def remover_taxa(nome: str, usuario=Depends(_seller_logado), db: Session = Depends(get_db)):
    conta = _conta_vinculada_do_usuario(usuario, db)
    registro = (
        db.query(VariavelConta)
        .filter(VariavelConta.conta_id == conta.id, VariavelConta.nome == nome)
        .first()
    )
    if registro is None:
        raise HTTPException(status_code=404, detail="Taxa não encontrada nessa conta.")
    db.delete(registro)
    db.commit()
    return {"removida": nome}
