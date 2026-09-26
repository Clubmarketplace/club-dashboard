"""
Rotas da tela de Manuais: upload direto pelo painel (sem precisar de
acesso ao servidor), lista de tudo já cadastrado, e busca por SKU pra
saber se um produto já tem manual ou não.
"""
import re

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ManualSku
from app import manuais_storage

router = APIRouter(prefix="/api/manuais", tags=["manuais"])

EXTENSOES_ACEITAS = (".pdf", ".txt", ".md")

# "SKU 7560025" / "SKU: 7560025" / "sku:7560025" -> "7560025".
# Só tira a palavra quando vem seguida de espaço ou dois-pontos: um SKU de
# verdade como "SKU-100" continua intacto.
_PREFIXO_SKU = re.compile(r"^\s*sku(?:\s*:\s*|\s+)", re.IGNORECASE)


def normalizar_sku(texto: str | None) -> str:
    """SKU como o anúncio tem: sem espaços nas pontas e sem a palavra "SKU" na frente."""
    return _PREFIXO_SKU.sub("", texto or "").strip()


@router.get("/lista")
def listar_manuais(busca: str | None = None, db: Session = Depends(get_db)):
    """
    Lista os manuais cadastrados, mais recentes primeiro. Com 'busca',
    filtra por SKU (contém, sem diferenciar maiúsculas/minúsculas) --
    é o que alimenta tanto a tela de Manuais quanto a checagem de
    "esse SKU já tem manual?" na tela de SKUs.
    """
    query = db.query(ManualSku)
    busca = normalizar_sku(busca)
    if busca:
        query = query.filter(ManualSku.sku.ilike(f"%{busca}%"))
    manuais = query.order_by(ManualSku.criado_em.desc()).all()
    return [
        {
            "sku": m.sku,
            "titulo": m.titulo,
            "criado_em": m.criado_em.isoformat() if m.criado_em else None,
            "tamanho_caracteres": len(m.conteudo or ""),
        }
        for m in manuais
    ]


@router.get("/existe/{sku}")
def verificar_manual(sku: str, db: Session = Depends(get_db)):
    """Diz se um SKU específico já tem manual cadastrado -- usado pela tela de SKUs."""
    sku = normalizar_sku(sku)
    manual = db.query(ManualSku).filter(ManualSku.sku == sku).first()
    return {"sku": sku, "tem_manual": manual is not None}


@router.post("/upload")
async def upload_manual(
    sku: str = Form(...),
    arquivo: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """
    Recebe um arquivo (PDF/txt/md) pelo painel, salva no armazenamento
    configurado (pasta local ou S3, ver manuais_storage.py) e
    cadastra/atualiza o ManualSku no banco -- não precisa mais rodar
    o importador manualmente pra manuais enviados por aqui.
    """
    sku = normalizar_sku(sku)  # "SKU 7560025" vira "7560025", senão a IA não acha o manual
    if not sku:
        raise HTTPException(400, "Informe o SKU.")

    nome_original = arquivo.filename or ""
    extensao = "." + nome_original.rsplit(".", 1)[-1].lower() if "." in nome_original else ""
    if extensao not in EXTENSOES_ACEITAS:
        raise HTTPException(400, f"Formato não aceito ({extensao or 'sem extensão'}). Use PDF, .txt ou .md.")

    conteudo_bytes = await arquivo.read()
    nome_arquivo = f"{sku}{extensao}"

    try:
        manuais_storage.salvar_arquivo(nome_arquivo, conteudo_bytes)
        texto = manuais_storage.ler_texto(nome_arquivo)
    except manuais_storage.ErroManuais as exc:
        raise HTTPException(500, str(exc)) from exc

    if not texto.strip():
        raise HTTPException(
            422,
            "Não consegui extrair texto desse arquivo -- se for PDF, confira se não é uma "
            "imagem escaneada (precisa ter texto selecionável).",
        )

    manual = db.query(ManualSku).filter(ManualSku.sku == sku).first()
    if manual:
        manual.titulo = nome_original
        manual.conteudo = texto
    else:
        manual = ManualSku(sku=sku, titulo=nome_original, conteudo=texto)
        db.add(manual)
    db.commit()

    return {"sku": sku, "titulo": nome_original, "tamanho_caracteres": len(texto)}
