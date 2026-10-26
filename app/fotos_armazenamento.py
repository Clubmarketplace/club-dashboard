"""
Armazenamento das fotos das solicitações (cancelamento, reputação, reverso).

POR QUE EXISTE (26/10): as fotos eram guardadas como texto base64 dentro da
própria solicitação e carregadas junto em toda lista -- isso travou o sistema.
Agora:

  * cada foto é uma linha em `solicitacao_fotos` (binário, ~25% menor que base64);
  * a MINIATURA (~5 KB) fica sempre no banco: é o que o painel mostra;
  * a FOTO GRANDE nasce no banco e, com o Cloudflare R2 configurado, é movida em
    segundo plano para o R2 (padrão de mercado: arquivo fora do banco);
  * R2 fora do ar / não configurado => a foto continua no banco e o sistema tenta
    de novo no próximo ciclo. Registrar um reverso NUNCA depende do R2.

Variáveis de ambiente (Railway) -- todas as 4 primeiras precisam existir para ligar o R2:
  R2_ACCOUNT_ID         id da conta Cloudflare (monta o endereço do R2)
  R2_ACCESS_KEY_ID      chave de acesso (token S3 do R2)
  R2_SECRET_ACCESS_KEY  segredo da chave
  R2_BUCKET             nome do bucket (privado)
  R2_ENDPOINT           (opcional) endereço completo, se não quiser usar o da conta
  FOTOS_DIAS_GUARDA     (opcional) apaga as fotos de solicitações CONCLUÍDAS há mais de
                        N dias. 0 ou vazio = nunca apaga (padrão).
  FOTOS_TAREFA_ATIVA    (opcional) 0 desliga a tarefa de segundo plano.
  FOTOS_INTERVALO_SEG   (opcional) intervalo da tarefa (padrão 90 s).

boto3 e Pillow são importados só quando usados: se algum faltar, o sistema continua
de pé (sem R2 / sem miniatura) em vez de cair na partida.
"""
from __future__ import annotations

import base64
import binascii
import io
import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, defer, undefer

from app.database import SessionLocal
from app.models import SolicitacaoCancelamento, SolicitacaoFoto

logger = logging.getLogger(__name__)

MINIATURA_LADO = 240          # px (maior lado)
MINIATURA_QUALIDADE = 65      # JPEG
TETO_TOTAL_CHARS = 4_000_000  # soma máxima das fotos (base64) de UM registro
LOTE_MOVER = 20               # fotos movidas para o R2 por ciclo
LOTE_LEGADOS = 3              # solicitações antigas convertidas por ciclo
LOTE_PRAZO = 50               # fotos apagadas por ciclo (prazo de guarda)

_RE_DATA_URL = re.compile(r"^data:(image/[A-Za-z0-9.+-]+);base64,(.*)$", re.S)


# ---------------------------------------------------------------------------
# Configuração do R2
# ---------------------------------------------------------------------------
def _env(nome: str) -> str:
    return (os.getenv(nome) or "").strip()


def r2_configurado() -> bool:
    return bool(_env("R2_ACCESS_KEY_ID") and _env("R2_SECRET_ACCESS_KEY") and _env("R2_BUCKET")
                and (_env("R2_ENDPOINT") or _env("R2_ACCOUNT_ID")))


_cliente = None


def _cliente_r2():
    """Cliente S3 (compatível com o R2), criado uma vez. Timeouts curtos: o R2 lento
    nunca pode segurar o sistema."""
    global _cliente
    if _cliente is None:
        import boto3
        from botocore.config import Config
        endpoint = _env("R2_ENDPOINT") or f"https://{_env('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com"
        _cliente = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=_env("R2_ACCESS_KEY_ID"),
            aws_secret_access_key=_env("R2_SECRET_ACCESS_KEY"),
            region_name="auto",
            config=Config(signature_version="s3v4", connect_timeout=5, read_timeout=20,
                          retries={"max_attempts": 2, "mode": "standard"}),
        )
    return _cliente


def _resetar_cliente() -> None:
    global _cliente
    _cliente = None


def dias_de_guarda() -> int:
    try:
        return max(int(_env("FOTOS_DIAS_GUARDA") or 0), 0)
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# Entrada: data URL -> bytes + miniatura
# ---------------------------------------------------------------------------
def decodificar_data_url(url: str) -> tuple[str, bytes]:
    """'data:image/jpeg;base64,....' -> ('image/jpeg', bytes). ValueError se inválida."""
    m = _RE_DATA_URL.match((url or "").strip())
    if not m:
        raise ValueError("Foto inválida.")
    corpo = re.sub(r"\s+", "", m.group(2))
    corpo += "=" * (-len(corpo) % 4)   # tolera base64 sem o preenchimento final
    try:
        return m.group(1).lower(), base64.b64decode(corpo, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Foto inválida.") from exc


def gerar_miniatura(dados: bytes) -> Optional[bytes]:
    """Miniatura JPEG ~5 KB. None se o Pillow não estiver instalado ou a imagem não abrir
    (nesse caso o painel usa a foto grande -- só fica mais pesado, nunca quebra)."""
    try:
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(dados)) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((MINIATURA_LADO, MINIATURA_LADO))
            if im.mode != "RGB":
                im = im.convert("RGB")
            saida = io.BytesIO()
            im.save(saida, "JPEG", quality=MINIATURA_QUALIDADE, optimize=True)
            return saida.getvalue()
    except Exception as exc:  # noqa: BLE001 -- qualquer falha aqui só tira a miniatura
        logger.warning("Fotos: não consegui gerar miniatura (%s)", exc)
        return None


def salvar_fotos(db: Session, solicitacao_id: int, data_urls: list[str], ordem_inicial: int = 0) -> int:
    """Grava as fotos (data URLs) de uma solicitação, no BANCO. Não faz commit: quem chama
    decide (assim entra na mesma transação do registro). Devolve quantas gravou.
    O envio para o R2 acontece depois, em segundo plano (mover_pendentes)."""
    gravadas = 0
    for i, url in enumerate(data_urls or []):
        mime, dados = decodificar_data_url(url)
        db.add(SolicitacaoFoto(
            solicitacao_id=solicitacao_id, ordem=ordem_inicial + i, mime=mime, tamanho=len(dados),
            destino="db", dados=dados, miniatura=gerar_miniatura(dados),
        ))
        gravadas += 1
    return gravadas


# ---------------------------------------------------------------------------
# Leitura
# ---------------------------------------------------------------------------
def _fotos_legado(s: SolicitacaoCancelamento) -> list[str]:
    """Fotos antigas (coluna fotos_base64, JSON de data URLs)."""
    if not s.fotos_base64:
        return []
    try:
        lista = json.loads(s.fotos_base64)
        return [f for f in lista if isinstance(f, str)] if isinstance(lista, list) else []
    except (ValueError, TypeError):
        return []


def listar_fotos(db: Session, s: SolicitacaoCancelamento, url_base: str) -> dict:
    """
    Endereços das fotos de UMA solicitação: {"fotos": [...], "minis": [...]}.
    Fotos novas viram endereços do próprio sistema ({url_base}?id=N e &mini=1);
    fotos antigas (legado) vão como data URL, igual antes. Não lê os binários.
    """
    fotos, minis = [], []
    for (foto_id,) in (db.query(SolicitacaoFoto.id)
                       .filter(SolicitacaoFoto.solicitacao_id == s.id)
                       .order_by(SolicitacaoFoto.ordem.asc(), SolicitacaoFoto.id.asc()).all()):
        fotos.append(f"{url_base}?id={foto_id}")
        minis.append(f"{url_base}?id={foto_id}&mini=1")
    for data_url in _fotos_legado(s):
        fotos.append(data_url)
        minis.append(data_url)
    return {"id": s.id, "fotos": fotos, "minis": minis}


def contar_fotos(db: Session, solicitacao_id: int) -> int:
    """Quantidade total (novas + legado) de uma solicitação, sem ler as fotos."""
    from app.routers.solicitacoes_cancelamento import n_fotos_sql  # import local: evita ciclo
    return int(db.query(n_fotos_sql()).filter(SolicitacaoCancelamento.id == solicitacao_id).scalar() or 0)


def obter_bytes(db: Session, foto_id: int, miniatura: bool = False) -> Optional[tuple[str, bytes, int]]:
    """
    Conteúdo de UMA foto: (mime, bytes, solicitacao_id) ou None se não existir.
    Miniatura: vem do banco (cai na foto grande se não houver). Foto grande: do banco
    ou, se já foi movida, do R2. Levanta RuntimeError se estiver no R2 e o R2 falhar.
    """
    f = (db.query(SolicitacaoFoto)
         .options(undefer(SolicitacaoFoto.dados), undefer(SolicitacaoFoto.miniatura))
         .filter(SolicitacaoFoto.id == foto_id).first())
    if f is None:
        return None
    if miniatura and f.miniatura:
        return "image/jpeg", bytes(f.miniatura), f.solicitacao_id
    if f.dados:
        return f.mime, bytes(f.dados), f.solicitacao_id
    if f.destino == "r2" and f.chave_r2:
        try:
            corpo = _cliente_r2().get_object(Bucket=_env("R2_BUCKET"), Key=f.chave_r2)["Body"].read()
        except Exception as exc:  # noqa: BLE001
            logger.error("Fotos: falha ao ler do R2 (%s): %s", f.chave_r2, exc)
            raise RuntimeError("Não consegui buscar a foto agora.") from exc
        return f.mime, corpo, f.solicitacao_id
    return None


def solicitacao_da_foto(db: Session, foto_id: int) -> Optional[int]:
    row = db.query(SolicitacaoFoto.solicitacao_id).filter(SolicitacaoFoto.id == foto_id).first()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Tarefas de segundo plano
# ---------------------------------------------------------------------------
def mover_pendentes(limite: int = LOTE_MOVER) -> int:
    """Move fotos grandes do banco para o R2 (uma a uma). Cada foto só sai do banco
    DEPOIS do upload dar certo; qualquer erro para o lote e tenta de novo no próximo ciclo."""
    if not r2_configurado():
        return 0
    movidas = 0
    with SessionLocal() as db:
        ids = [i for (i,) in (db.query(SolicitacaoFoto.id)
                              .filter(SolicitacaoFoto.destino == "db", SolicitacaoFoto.dados.isnot(None))
                              .order_by(SolicitacaoFoto.id.asc()).limit(limite).all())]
        for foto_id in ids:
            f = (db.query(SolicitacaoFoto).options(undefer(SolicitacaoFoto.dados))
                 .filter(SolicitacaoFoto.id == foto_id).first())
            if f is None or not f.dados:
                continue
            chave = f"fotos/{f.solicitacao_id}/{f.id}.{'png' if f.mime == 'image/png' else 'jpg' if f.mime == 'image/jpeg' else 'img'}"
            try:
                _cliente_r2().put_object(Bucket=_env("R2_BUCKET"), Key=chave, Body=bytes(f.dados), ContentType=f.mime)
                f.destino, f.chave_r2, f.dados = "r2", chave, None
                db.commit()
                movidas += 1
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                logger.warning("Fotos: R2 indisponível (%s); fica no banco e tento de novo depois.", exc)
                break
    return movidas


_legados_com_erro: set[int] = set()


def converter_legados(limite: int = LOTE_LEGADOS) -> int:
    """Converte solicitações ANTIGAS (fotos em texto na própria linha) para a tabela de
    fotos, uma por vez. Só apaga o texto antigo depois de gravar as fotos novas (mesma
    transação). Só roda com o R2 ligado (o objetivo é tirar peso do banco)."""
    if not r2_configurado():
        return 0
    col = SolicitacaoCancelamento.fotos_base64
    convertidas = 0
    with SessionLocal() as db:
        consulta = db.query(SolicitacaoCancelamento.id).filter(col.isnot(None), col != "", col != "[]")
        if _legados_com_erro:
            consulta = consulta.filter(~SolicitacaoCancelamento.id.in_(_legados_com_erro))
        for (sid,) in consulta.order_by(SolicitacaoCancelamento.id.asc()).limit(limite).all():
            s = db.query(SolicitacaoCancelamento).filter(SolicitacaoCancelamento.id == sid).first()
            try:
                urls = _fotos_legado(s)
                existentes = db.query(func.count(SolicitacaoFoto.id)).filter(SolicitacaoFoto.solicitacao_id == sid).scalar() or 0
                salvar_fotos(db, sid, urls, ordem_inicial=int(existentes))
                s.fotos_base64 = None
                db.commit()
                convertidas += 1
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                _legados_com_erro.add(sid)
                logger.warning("Fotos: não converti as fotos antigas da solicitação %s (%s); mantidas como estão.", sid, exc)
    return convertidas


def aplicar_prazo(dias: int, limite: int = LOTE_PRAZO) -> int:
    """Prazo de guarda: apaga as fotos (R2 + banco) de solicitações CONCLUÍDAS há mais de
    `dias` dias. A solicitação e o histórico continuam; só as fotos saem. 0 = nunca."""
    if dias <= 0:
        return 0
    corte = datetime.utcnow() - timedelta(days=dias)
    apagadas = 0
    with SessionLocal() as db:
        antigas = (db.query(SolicitacaoCancelamento.id)
                   .filter(SolicitacaoCancelamento.confirmado_por.isnot(None),
                           SolicitacaoCancelamento.confirmado_em.isnot(None),
                           SolicitacaoCancelamento.confirmado_em < corte))
        fotos = (db.query(SolicitacaoFoto).filter(SolicitacaoFoto.solicitacao_id.in_(antigas.subquery().select()))
                 .order_by(SolicitacaoFoto.id.asc()).limit(limite).all())
        for f in fotos:
            if f.destino == "r2" and f.chave_r2:
                try:
                    _cliente_r2().delete_object(Bucket=_env("R2_BUCKET"), Key=f.chave_r2)
                except Exception as exc:  # noqa: BLE001 -- sem R2 não dá pra apagar o arquivo: mantém a linha
                    logger.warning("Fotos: não consegui apagar %s no R2 (%s); tento de novo depois.", f.chave_r2, exc)
                    continue
            db.delete(f)
            apagadas += 1
        # texto antigo (legado) das concluídas há muito tempo
        col = SolicitacaoCancelamento.fotos_base64
        velhas = (db.query(SolicitacaoCancelamento)
                  .filter(SolicitacaoCancelamento.confirmado_em < corte, col.isnot(None), col != "", col != "[]")
                  .order_by(SolicitacaoCancelamento.id.asc()).limit(limite).all())
        for s in velhas:
            s.fotos_base64 = None
            apagadas += 1
        db.commit()
    return apagadas


def ciclo() -> dict:
    """Um ciclo da tarefa de segundo plano (síncrono; main.py roda em thread)."""
    resultado = {"movidas": 0, "convertidas": 0, "apagadas": 0}
    for nome, fn in (("movidas", mover_pendentes), ("convertidas", converter_legados),
                     ("apagadas", lambda: aplicar_prazo(dias_de_guarda()))):
        try:
            resultado[nome] = fn()
        except Exception:  # noqa: BLE001 -- um passo com erro não derruba os outros
            logger.exception("Fotos: erro no passo '%s' do ciclo", nome)
    if any(resultado.values()):
        logger.info("Fotos: ciclo %s", resultado)
    return resultado


async def loop_fotos() -> None:
    """Tarefa em segundo plano (ver main.py): move fotos pro R2, converte as antigas e
    aplica o prazo de guarda. Intervalo FOTOS_INTERVALO_SEG (padrão 90 s)."""
    import asyncio
    try:
        intervalo = max(int(_env("FOTOS_INTERVALO_SEG") or 90), 15)
    except ValueError:
        intervalo = 90
    await asyncio.sleep(30)  # deixa o sistema terminar de subir
    while True:
        try:
            await asyncio.to_thread(ciclo)
        except Exception:  # noqa: BLE001
            logger.exception("Fotos: erro no ciclo")
        await asyncio.sleep(intervalo)
