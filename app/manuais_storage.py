"""
Abstrai DE ONDE os manuais técnicos são lidos -- hoje uma pasta local
no computador, futuramente um bucket S3 na AWS. Pra migrar, troca só
MANUAIS_STORAGE (e MANUAIS_S3_BUCKET) no .env -- nada mais no sistema
precisa mudar, porque tanto app/importar_manuais.py quanto
app/ia_pre_venda.py só conhecem as funções deste módulo, nunca leem
arquivo/S3 diretamente.

Convenção: um arquivo por SKU, nomeado exatamente como o SKU mais a
extensão -- ex: "LP320-220V.pdf" ou "LP320-220V.txt". O nome do
arquivo (sem extensão) é o SKU que o sistema casa com o anúncio.

Formatos aceitos: .txt e .md (lidos direto) e .pdf (texto extraído).
Um PDF puramente escaneado (imagem, sem texto real) não tem como ser
lido por aqui -- precisaria de OCR, que não está incluído.
"""
import os

from app.config import MANUAIS_STORAGE, MANUAIS_LOCAL_DIR, MANUAIS_S3_BUCKET

EXTENSOES_SUPORTADAS = (".txt", ".md", ".pdf")


class ErroManuais(Exception):
    """Erro ao listar ou ler manuais do armazenamento configurado."""


def listar_arquivos() -> list[dict]:
    """Devolve [{"sku": str, "nome_arquivo": str}, ...] pra cada manual encontrado."""
    if MANUAIS_STORAGE == "s3":
        return _listar_arquivos_s3()
    return _listar_arquivos_local()


def ler_texto(nome_arquivo: str) -> str:
    """Lê o arquivo e devolve o texto -- extrai de PDF quando necessário."""
    if MANUAIS_STORAGE == "s3":
        conteudo_bytes = _ler_bytes_s3(nome_arquivo)
    else:
        conteudo_bytes = _ler_bytes_local(nome_arquivo)

    if nome_arquivo.lower().endswith(".pdf"):
        return _extrair_texto_pdf(conteudo_bytes)
    return conteudo_bytes.decode("utf-8", errors="replace")


def salvar_arquivo(nome_arquivo: str, conteudo_bytes: bytes) -> None:
    """
    Salva um arquivo recebido por upload (tela de Manuais) no
    armazenamento configurado. Em modo local (Railway sem S3
    configurado ainda), isso grava dentro do container -- funciona
    pra uso imediato, mas SE PERDE no próximo deploy, porque o
    Railway reconstrói o container do zero a partir do código no
    GitHub. Pra persistir de verdade, migre MANUAIS_STORAGE pra "s3".
    """
    if MANUAIS_STORAGE == "s3":
        _salvar_bytes_s3(nome_arquivo, conteudo_bytes)
    else:
        _salvar_bytes_local(nome_arquivo, conteudo_bytes)


# -------- Local (pasta no computador) --------

def _listar_arquivos_local() -> list[dict]:
    if not os.path.isdir(MANUAIS_LOCAL_DIR):
        return []
    resultado = []
    for nome in sorted(os.listdir(MANUAIS_LOCAL_DIR)):
        caminho = os.path.join(MANUAIS_LOCAL_DIR, nome)
        if os.path.isfile(caminho) and nome.lower().endswith(EXTENSOES_SUPORTADAS):
            sku = os.path.splitext(nome)[0]
            resultado.append({"sku": sku, "nome_arquivo": nome})
    return resultado


def _ler_bytes_local(nome_arquivo: str) -> bytes:
    caminho = os.path.join(MANUAIS_LOCAL_DIR, nome_arquivo)
    if not os.path.isfile(caminho):
        raise ErroManuais(f"Arquivo não encontrado: {caminho}")
    with open(caminho, "rb") as f:
        return f.read()


def _salvar_bytes_local(nome_arquivo: str, conteudo_bytes: bytes) -> None:
    os.makedirs(MANUAIS_LOCAL_DIR, exist_ok=True)
    caminho = os.path.join(MANUAIS_LOCAL_DIR, nome_arquivo)
    with open(caminho, "wb") as f:
        f.write(conteudo_bytes)


# -------- S3 (Amazon, pra quando migrar) --------

def _obter_cliente_s3():
    import boto3  # import local -- só precisa estar instalado quando MANUAIS_STORAGE=s3
    return boto3.client("s3")


def _listar_arquivos_s3() -> list[dict]:
    if not MANUAIS_S3_BUCKET:
        raise ErroManuais("MANUAIS_S3_BUCKET não configurado no .env.")
    cliente = _obter_cliente_s3()
    resultado = []
    paginador = cliente.get_paginator("list_objects_v2")
    for pagina in paginador.paginate(Bucket=MANUAIS_S3_BUCKET):
        for obj in pagina.get("Contents", []):
            nome = obj["Key"]
            if nome.lower().endswith(EXTENSOES_SUPORTADAS):
                sku = os.path.splitext(os.path.basename(nome))[0]
                resultado.append({"sku": sku, "nome_arquivo": nome})
    return resultado


def _ler_bytes_s3(nome_arquivo: str) -> bytes:
    cliente = _obter_cliente_s3()
    resposta = cliente.get_object(Bucket=MANUAIS_S3_BUCKET, Key=nome_arquivo)
    return resposta["Body"].read()


def _salvar_bytes_s3(nome_arquivo: str, conteudo_bytes: bytes) -> None:
    if not MANUAIS_S3_BUCKET:
        raise ErroManuais("MANUAIS_S3_BUCKET não configurado no .env.")
    cliente = _obter_cliente_s3()
    cliente.put_object(Bucket=MANUAIS_S3_BUCKET, Key=nome_arquivo, Body=conteudo_bytes)


# -------- Extração de texto de PDF --------

def _extrair_texto_pdf(conteudo_bytes: bytes) -> str:
    import io
    from pypdf import PdfReader  # import local -- só precisa quando de fato ler um PDF

    leitor = PdfReader(io.BytesIO(conteudo_bytes))
    paginas = [pagina.extract_text() or "" for pagina in leitor.pages]
    return "\n\n".join(paginas).strip()
