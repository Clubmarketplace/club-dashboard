"""
Endpoints da página pública de SOLICITAÇÃO MANUAL de cancelamento —
diferente da fila de IA em routers/cancelamentos.py (que detecta pedido
de cancelamento dentro de mensagens de clientes). Aqui é a própria
conta/equipe que registra manualmente "preciso cancelar essa venda"
(CEP errado, sem estoque, etiqueta não gerada, etc.), sem login — o
link é compartilhado com todas as contas.

A data é sempre gerada pelo servidor (datetime.utcnow), nunca vem do
formulário, pra não dar margem a preencher data errada.

Um envio pode conter VÁRIOS itens (vendas) da mesma conta/plataforma de
uma vez só — cada item vira uma linha própria no banco, todas com a
mesma conta/plataforma, mas motivo individual (podem ser motivos
diferentes por venda).
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import SolicitacaoCancelamento

router = APIRouter(prefix="/api/solicitacoes-cancelamento", tags=["solicitacoes-cancelamento"])

PLATAFORMAS_VALIDAS = {"mercado_livre", "shopee"}


class ItemCancelamento(BaseModel):
    numero_venda: str
    motivo: str

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

    @field_validator("plataforma")
    @classmethod
    def validar_plataforma(cls, valor: str) -> str:
        if valor not in PLATAFORMAS_VALIDAS:
            raise ValueError(f"Plataforma inválida. Use uma de: {', '.join(PLATAFORMAS_VALIDAS)}")
        return valor

    @field_validator("conta")
    @classmethod
    def validar_conta(cls, valor: str) -> str:
        valor = valor.strip()
        if not valor:
            raise ValueError("Campo obrigatório não pode ficar em branco.")
        return valor

    @field_validator("itens")
    @classmethod
    def validar_ao_menos_um_item(cls, valor: list) -> list:
        if not valor:
            raise ValueError("Informe pelo menos uma venda.")
        return valor


@router.post("")
def criar_solicitacoes(corpo: NovaSolicitacaoLote, db: Session = Depends(get_db)):
    """
    Registra um ou mais pedidos de cancelamento de uma vez, todos da
    mesma conta/plataforma. Cada item vira uma linha própria no banco
    — se algo falhar no meio, nada é salvo (tudo ou nada).
    """
    try:
        criadas = []
        for item in corpo.itens:
            solicitacao = SolicitacaoCancelamento(
                plataforma=corpo.plataforma,
                conta=corpo.conta,
                numero_venda=item.numero_venda,
                motivo=item.motivo,
            )
            db.add(solicitacao)
            criadas.append(solicitacao)
        db.commit()
        for s in criadas:
            db.refresh(s)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="Não foi possível registrar a solicitação. Tente de novo.") from exc

    return {
        "status": "registrado",
        "quantidade": len(criadas),
        "itens": [{"id": s.id, "numero_venda": s.numero_venda, "criado_em": s.criado_em.isoformat()} for s in criadas],
    }


@router.get("")
def listar_solicitacoes(db: Session = Depends(get_db), limite: int = 500):
    """Lista os pedidos mais recentes, usado pela tela de acompanhamento (agrupada por conta)."""
    solicitacoes = (
        db.query(SolicitacaoCancelamento)
        .order_by(SolicitacaoCancelamento.criado_em.desc())
        .limit(limite)
        .all()
    )
    return [
        {
            "id": s.id,
            "plataforma": s.plataforma,
            "conta": s.conta,
            "numero_venda": s.numero_venda,
            "motivo": s.motivo,
            "criado_em": s.criado_em.isoformat() if s.criado_em else None,
            "confirmado_por": s.confirmado_por,
            "confirmado_em": s.confirmado_em.isoformat() if s.confirmado_em else None,
        }
        for s in solicitacoes
    ]


class ConfirmacaoCancelamento(BaseModel):
    nome: str

    @field_validator("nome")
    @classmethod
    def validar_nome(cls, valor: str) -> str:
        valor = valor.strip()
        if not valor:
            raise ValueError("Informe o nome de quem confirmou.")
        return valor


@router.post("/{solicitacao_id}/confirmar")
def confirmar_solicitacao(solicitacao_id: int, corpo: ConfirmacaoCancelamento, db: Session = Depends(get_db)):
    """Marca um pedido como já cancelado de verdade na plataforma, registrando quem e quando."""
    solicitacao = db.query(SolicitacaoCancelamento).filter(SolicitacaoCancelamento.id == solicitacao_id).first()
    if solicitacao is None:
        raise HTTPException(status_code=404, detail="Solicitação não encontrada")
    if solicitacao.confirmado_por:
        raise HTTPException(status_code=400, detail="Essa solicitação já foi confirmada antes.")

    solicitacao.confirmado_por = corpo.nome
    solicitacao.confirmado_em = datetime.utcnow()
    db.commit()
    db.refresh(solicitacao)

    return {
        "status": "confirmado",
        "id": solicitacao.id,
        "confirmado_por": solicitacao.confirmado_por,
        "confirmado_em": solicitacao.confirmado_em.isoformat(),
    }
