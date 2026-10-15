"""
"Solicitar cadastro" no painel do SELLER (15/10): a MESMA lógica da tela da
equipe (app/routers/cadastro_produtos.py -- rota /api/cadastro/produtos/lote),
só que:
  - a conta vem da sessão do seller (usuario.conta_vinculada), nunca do corpo
    da requisição -- o seller não tem como pedir cadastro em nome de outra
    conta, nem por engano nem forçando a chamada direto na API;
  - sem "refazer" (controle interno da equipe; não existe pro seller);
  - sem escolher a conta (ela é sempre a mesma, travada).

Cai na MESMA tabela (ProdutoCadastro) e na MESMA lista de pendentes que a
equipe de Cadastro já usa -- pro time ver exatamente igual a uma solicitação
feita por eles, só com criado_por = o nome do seller.

Reaproveita _incluir_um, MARKETPLACES e _produto_json de cadastro_produtos.py
pra não duplicar a validação (EAN/SKU/nome obrigatórios, custo >= 0, SKU
repetido etc.) -- ficar com duas implementações da mesma regra é o tipo de
coisa que diverge sozinha com o tempo.
"""
import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Usuario
from app.routers.cadastro_produtos import MARKETPLACES, _incluir_um, _produto_json
from app.routers.custos_painel import _seller_logado

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/painel/cadastro", tags=["painel-cadastro-seller"])

LIMITE_LOTE_SELLER = 300


class ItemLoteSellerIn(BaseModel):
    sku: str
    produto: str
    gtin: Optional[str] = ""
    quantidade: float = 0
    custo_unitario: float = 0
    # Fornecedor/nota por produto -- mesma ideia da tela da equipe (um envio
    # pode juntar produtos de mais de uma nota).
    fornecedor: Optional[str] = ""
    numero_nota: Optional[str] = ""


class LoteSellerIn(BaseModel):
    # SEM campo "empresa" de propósito -- vem de usuario.conta_vinculada.
    kit: bool
    plataformas: List[str]
    itens: List[ItemLoteSellerIn]


@router.post("/produtos/lote")
def incluir_lote_seller(
    corpo: LoteSellerIn,
    db: Session = Depends(get_db),
    usuario: Usuario = Depends(_seller_logado),
):
    """
    Mesma regra de validação da tela da equipe (ver incluir_lote_manual em
    cadastro_produtos.py): EAN e custo > 0 obrigatórios por produto; kit e
    pelo menos 1 canal obrigatórios pra toda a solicitação. A diferença é só
    a conta -- aqui é sempre usuario.conta_vinculada, travada no servidor.
    """
    if not usuario.conta_vinculada:
        raise HTTPException(status_code=422, detail="Sua conta não está vinculada -- fale com o suporte.")
    if not corpo.itens:
        raise HTTPException(status_code=422, detail="Nenhum produto para incluir.")
    if len(corpo.itens) > LIMITE_LOTE_SELLER:
        raise HTTPException(status_code=413, detail=f"Máximo de {LIMITE_LOTE_SELLER} produtos por vez.")
    plataformas = {(p or "").strip().lower() for p in corpo.plataformas}
    if not plataformas:
        raise HTTPException(status_code=422, detail="Escolha pelo menos um canal para cadastro.")
    if not plataformas.issubset(MARKETPLACES):
        raise HTTPException(status_code=422, detail="Canal inválido -- use ml, tiktok ou shopee.")

    criados, ignorados = [], []
    try:
        for n, it in enumerate(corpo.itens, start=1):
            try:
                if not (it.gtin or "").strip():
                    raise HTTPException(status_code=422, detail="EAN é obrigatório.")
                if not (it.custo_unitario or 0) > 0:
                    raise HTTPException(status_code=422, detail="Custo precisa ser maior que zero.")
                registro, _aviso = _incluir_um(
                    db, usuario, empresa=usuario.conta_vinculada, produto=it.produto, sku=it.sku,
                    fornecedor=it.fornecedor, numero_nota=it.numero_nota,
                    gtin=it.gtin, quantidade=it.quantidade, custo_unitario=it.custo_unitario,
                    refazer=False,
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
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Falha ao gravar o lote de cadastro do seller (conta=%s)", usuario.conta_vinculada)
        raise HTTPException(status_code=500, detail="Erro ao gravar os produtos no servidor.")

    logger.info(
        "Cadastro (seller): %d produto(s) enviado(s) por %s (conta %s)",
        len(criados), usuario.nome_exibicao, usuario.conta_vinculada,
    )
    return {"ok": True, "criados": [_produto_json(r) for r in criados], "ignorados": ignorados}
