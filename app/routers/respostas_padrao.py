"""
API da tela "Respostas padrão" (grupo Pré-venda do menu).

  GET    /api/respostas-padrao              -> lista todas
  POST   /api/respostas-padrao              -> cria
  PUT    /api/respostas-padrao/{id}         -> altera
  POST   /api/respostas-padrao/{id}/ativa   -> liga/desliga  {"ativa": true|false}
  DELETE /api/respostas-padrao/{id}         -> apaga
  POST   /api/respostas-padrao/testar       -> "a IA usaria essa resposta pra esta pergunta?"
  GET    /api/respostas-padrao/produtos     -> busca produto (SKU/anúncio/título) pro alcance "Só um produto"

Ver, cadastrar, editar, testar e ligar/desligar: admin, supervisor e
atendente (são os atendentes que tocam o dia a dia). Apagar: só admin e
supervisor, pra ninguém perder uma resposta boa sem querer. A IA usa as respostas ATIVAS na ordem descrita em
app/pre_venda_logica.py (depois das respostas já dadas pro mesmo SKU).
"""
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app import auth
from app.database import get_db
from app.ia_pre_venda import escolher_resposta_padrao, _obter_cliente
from app.models import Pergunta, RespostaPadrao

router = APIRouter(prefix="/api/respostas-padrao", tags=["respostas-padrão"])

LIMITE_RESPOSTA = 2000   # limite de caracteres de uma resposta no Mercado Livre
LIMITE_TEMA = 80
LIMITE_EXEMPLOS = 30


PAPEIS_EDITAM = ("admin", "supervisor", "atendente")
PAPEIS_APAGAM = ("admin", "supervisor")


def _exigir(request: Request, db: Session, papeis: tuple = PAPEIS_EDITAM):
    usuario = auth.usuario_atual(request, db)
    if not auth.papel_permite(usuario, papeis):
        raise HTTPException(status_code=403, detail="Sem permissão para esta ação nas respostas padrão.")
    return usuario


def _limpar_exemplos(texto) -> str:
    linhas = [l.strip() for l in str(texto or "").splitlines() if l.strip()]
    return "\n".join(linhas[:LIMITE_EXEMPLOS])


def _validar(dados: dict) -> dict:
    tema = str(dados.get("tema") or "").strip()
    resposta = str(dados.get("resposta") or "").strip()
    alcance = dados.get("alcance") if dados.get("alcance") in ("geral", "produto") else "geral"
    produto_chave = str(dados.get("produto_chave") or "").strip() or None
    if not tema:
        raise HTTPException(status_code=400, detail="Informe o tema.")
    if len(tema) > LIMITE_TEMA:
        raise HTTPException(status_code=400, detail=f"Tema muito longo (máximo {LIMITE_TEMA} caracteres).")
    if not resposta:
        raise HTTPException(status_code=400, detail="Informe a resposta.")
    if len(resposta) > LIMITE_RESPOSTA:
        raise HTTPException(status_code=400, detail=f"Resposta muito longa (o Mercado Livre aceita até {LIMITE_RESPOSTA} caracteres).")
    if alcance == "produto" and not produto_chave:
        raise HTTPException(status_code=400, detail='Escolha o produto (SKU ou anúncio) para o alcance "Só um produto".')
    return {
        "tema": tema,
        "exemplos": _limpar_exemplos(dados.get("exemplos")),
        "resposta": resposta,
        "alcance": alcance,
        "produto_chave": produto_chave if alcance == "produto" else None,
        "produto_nome": (str(dados.get("produto_nome") or "").strip()[:200] or None) if alcance == "produto" else None,
    }


def _serializar(r: RespostaPadrao) -> dict:
    return {
        "id": r.id, "tema": r.tema, "exemplos": r.exemplos or "", "resposta": r.resposta,
        "alcance": r.alcance, "produto_chave": r.produto_chave, "produto_nome": r.produto_nome,
        "ativa": bool(r.ativa), "usada": r.usada or 0,
        "atualizado_em": (r.atualizado_em.isoformat() + "Z") if r.atualizado_em else None,
    }


def _achar(db: Session, id_: int) -> RespostaPadrao:
    r = db.query(RespostaPadrao).filter(RespostaPadrao.id == id_).first()
    if r is None:
        raise HTTPException(status_code=404, detail="Resposta padrão não encontrada.")
    return r


@router.get("")
def listar(request: Request, db: Session = Depends(get_db)):
    usuario = _exigir(request, db)
    itens = db.query(RespostaPadrao).order_by(RespostaPadrao.ativa.desc(), RespostaPadrao.tema).all()
    return {
        "itens": [_serializar(r) for r in itens],
        "ia_disponivel": _obter_cliente() is not None,
        "pode_apagar": auth.papel_permite(usuario, PAPEIS_APAGAM),
    }


@router.post("")
def criar(request: Request, dados: dict = Body(...), db: Session = Depends(get_db)):
    usuario = _exigir(request, db)
    campos = _validar(dados)
    r = RespostaPadrao(**campos, ativa=bool(dados.get("ativa", True)),
                       criado_por=getattr(usuario, "nome_exibicao", None))
    db.add(r)
    db.commit()
    db.refresh(r)
    return _serializar(r)


@router.put("/{id_}")
def alterar(id_: int, request: Request, dados: dict = Body(...), db: Session = Depends(get_db)):
    _exigir(request, db)
    r = _achar(db, id_)
    for k, v in _validar(dados).items():
        setattr(r, k, v)
    if "ativa" in dados:
        r.ativa = bool(dados["ativa"])
    r.atualizado_em = datetime.utcnow()
    db.commit()
    return _serializar(r)


@router.post("/{id_}/ativa")
def ligar_desligar(id_: int, request: Request, dados: dict = Body(...), db: Session = Depends(get_db)):
    _exigir(request, db)
    r = _achar(db, id_)
    r.ativa = bool(dados.get("ativa"))
    r.atualizado_em = datetime.utcnow()
    db.commit()
    return _serializar(r)


@router.delete("/{id_}")
def apagar(id_: int, request: Request, db: Session = Depends(get_db)):
    _exigir(request, db, PAPEIS_APAGAM)
    db.delete(_achar(db, id_))
    db.commit()
    return {"status": "apagada"}


@router.post("/testar")
def testar(request: Request, dados: dict = Body(...), db: Session = Depends(get_db)):
    """Pergunta pra IA se ESTA resposta (a do formulário, salva ou não) serve pra pergunta digitada."""
    _exigir(request, db)
    pergunta = str(dados.get("pergunta") or "").strip()
    if not pergunta:
        raise HTTPException(status_code=400, detail="Digite uma pergunta para testar.")
    if _obter_cliente() is None:
        return {"ia_disponivel": False, "usaria": False}
    candidata = {
        "tema": str(dados.get("tema") or "").strip() or "(sem tema)",
        "exemplos": [e for e in _limpar_exemplos(dados.get("exemplos")).splitlines() if e],
        "resposta": str(dados.get("resposta") or "").strip() or "(sem resposta)",
    }
    return {"ia_disponivel": True, "usaria": escolher_resposta_padrao(pergunta, [candidata]) == 0}


@router.get("/produtos")
def buscar_produtos(request: Request, busca: str = "", db: Session = Depends(get_db)):
    """Produtos que já receberam perguntas, pra escolher o alcance "Só um produto" sem digitar SKU."""
    _exigir(request, db)
    termo = busca.strip()
    if len(termo) < 2:
        return []
    like = f"%{termo}%"
    linhas = (
        db.query(Pergunta.sku, Pergunta.item_id, func.max(Pergunta.titulo_anuncio), func.count(Pergunta.id))
        .filter(or_(Pergunta.sku.ilike(like), Pergunta.item_id.ilike(like), Pergunta.titulo_anuncio.ilike(like)))
        .group_by(Pergunta.sku, Pergunta.item_id)
        .order_by(func.count(Pergunta.id).desc())
        .limit(40)
        .all()
    )
    vistos, itens = set(), []
    for sku, item_id, titulo, qtd in linhas:
        chave = (sku or "").strip() or (item_id or "").strip()  # mesma regra do banco de respostas
        if not chave or chave in vistos:
            continue
        vistos.add(chave)
        itens.append({"chave": chave, "sku": sku, "item_id": item_id, "titulo": titulo, "perguntas": qtd})
        if len(itens) >= 15:
            break
    return itens
