"""
Atendimento das contas com reputação em atenção (tela "Reputação das contas").

Mesmo padrão das Solicitações sellers:
  - a conta entra na FILA sozinha quando o termômetro a coloca em
    "Passou do limite" ou "A verificar";
  - alguém ASSUME (fica "Com Fulano · há X"); outra pessoa pode assumir no
    lugar (o histórico guarda quanto tempo ficou com cada um);
  - quem está com a conta ANOTA o que fez e CONCLUI (o que foi feito é
    obrigatório; protocolo opcional).

Regras automáticas (sincronizar):
  - conta em atenção sem atendimento aberto -> abre ("entrou na fila");
  - atendimento concluído há mais de DIAS_REABRIR dias e a conta continua em
    atenção -> reabre; se piorou de "A verificar" para "Passou do limite"
    depois de concluída -> reabre na hora;
  - conta voltou a ficar em dia e NINGUÉM estava com ela -> encerra sozinho.
    (Se alguém está com ela, continua com essa pessoa até concluir/liberar.)

Tudo que muda de mãos passa por UPDATE condicional no banco, pra dois
operadores nunca "ganharem" a mesma conta ao mesmo tempo.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models import AtendimentoReputacao, AtendimentoReputacaoEvento

logger = logging.getLogger("reputacao_atendimento")

# Decidido com a equipe (29/09): "parada" = mais de 4 h com a mesma pessoa;
# conta concluída que continua acima do limite volta pra fila em 7 dias.
HORAS_PARADA = 4
DIAS_REABRIR = 7

SITUACOES_NA_FILA = ("critico", "atencao")
TAMANHO_MAX_TEXTO = 1000
TAMANHO_MAX_PROTOCOLO = 80

# Várias telas abertas atualizam ao mesmo tempo: a trava garante que duas
# sincronizações simultâneas nunca abram DOIS atendimentos pra mesma conta.
_trava_sincronizar = threading.Lock()

ROTULO_EVENTO = {
    "entrou_na_fila": "Entrou na fila",
    "reabriu": "Voltou para a fila",
    "assumiu": "Assumiu",
    "assumiu_no_lugar": "Assumiu no lugar de outra pessoa",
    "liberou": "Liberou",
    "anotou": "Anotação",
    "concluiu": "Concluiu",
    "saiu_da_fila": "Saiu da fila",
}


# ---------------------------------------------------------------------------
# Auxiliares
# ---------------------------------------------------------------------------
def registrar(db: Session, atendimento_id: int, tipo: str, usuario=None, detalhe: str | None = None) -> None:
    """Acrescenta um evento ao histórico (sem commit: entra na transação de quem chamou)."""
    db.add(AtendimentoReputacaoEvento(
        atendimento_id=atendimento_id,
        tipo=tipo,
        usuario_id=getattr(usuario, "id", None),
        usuario_nome=getattr(usuario, "nome_exibicao", None) or ("Sistema" if usuario is None else None),
        detalhe=(detalhe or None) and detalhe[:TAMANHO_MAX_TEXTO],
        quando=datetime.utcnow(),
    ))


def atual_por_conta(db: Session) -> dict[int, AtendimentoReputacao]:
    """{conta_id: atendimento mais recente}. Uma consulta só, pra tela inteira."""
    mais_recente: dict[int, AtendimentoReputacao] = {}
    for a in db.query(AtendimentoReputacao).order_by(AtendimentoReputacao.id.asc()).all():
        mais_recente[a.conta_id] = a  # ordem crescente: o último que sobra é o mais novo
    return mais_recente


def motivo_texto(resumo: dict, faixa: float) -> str:
    """Mesmo texto do cartão: "Reclamações 2,4% de 1,0% · Canceladas ..."."""
    piores = sorted(
        (m for m in resumo.get("metricas", []) if m.get("uso") is not None and m["uso"] >= faixa),
        key=lambda m: m["uso"], reverse=True,
    )
    # Mesmo formato do cartão (reputacao.js -> pct): 2,4% · 1,0% · 0,59% · 0%
    fmt = lambda v: "0%" if not v else (f"{v:.2f}"[:-1] if f"{v:.2f}".endswith("0") else f"{v:.2f}").replace(".", ",") + "%"
    partes = [f"{m['nome']} {fmt(m['taxa'])} de {fmt(m['limite'])}" for m in piores]
    if not partes and resumo.get("nivel") is not None and resumo["nivel"] <= 4:
        partes.append("Termômetro abaixo do verde")
    return " · ".join(partes)


# ---------------------------------------------------------------------------
# Fila automática
# ---------------------------------------------------------------------------
def sincronizar(db: Session, resumos: dict[int, dict], faixa: float) -> int:
    """
    Abre/reabre/encerra atendimentos conforme a situação atual de cada conta.
    `resumos` = {conta_id: reputacao.classificar(...)} das contas ATIVAS.
    Devolve quantas mudanças fez (e já faz commit). Nunca levanta exceção.
    """
    with _trava_sincronizar:
        return _sincronizar(db, resumos, faixa)


def _sincronizar(db: Session, resumos: dict[int, dict], faixa: float) -> int:
    try:
        agora = datetime.utcnow()
        atuais = atual_por_conta(db)
        mudancas = 0
        for conta_id, resumo in resumos.items():
            situacao = resumo.get("situacao")
            if situacao == "sem_dados":
                continue  # sem leitura: não mexe em nada
            na_fila = situacao in SITUACOES_NA_FILA
            atual = atuais.get(conta_id)

            if na_fila:
                motivo_reabrir = None
                if atual is None or atual.status == "encerrado":
                    motivo_reabrir = ("entrou_na_fila", None)
                elif atual.status == "concluido" and atual.concluido_em:
                    if atual.concluido_em < agora - timedelta(days=DIAS_REABRIR):
                        motivo_reabrir = ("reabriu", f"Continua acima do limite {DIAS_REABRIR} dias depois de concluída")
                    elif atual.situacao_conclusao == "atencao" and situacao == "critico":
                        motivo_reabrir = ("reabriu", "Piorou depois de concluída: passou do limite")
                if motivo_reabrir:
                    motivo = motivo_texto(resumo, faixa)
                    novo = AtendimentoReputacao(
                        conta_id=conta_id, status="aberto", aberto_em=agora,
                        situacao_abertura=situacao, motivo_abertura=motivo or None,
                        metricas_abertura=json.dumps({m["chave"]: m["taxa"] for m in resumo.get("metricas", [])}),
                    )
                    db.add(novo)
                    db.flush()
                    tipo, detalhe = motivo_reabrir
                    registrar(db, novo.id, tipo, detalhe=" · ".join(x for x in (detalhe, motivo) if x) or None)
                    mudancas += 1
            elif atual is not None and atual.status == "aberto" and atual.em_atendimento_por_id is None:
                # Voltou a ficar em dia sem ninguém ter assumido: sai da fila sozinha.
                atual.status = "encerrado"
                atual.encerrado_em = agora
                registrar(db, atual.id, "saiu_da_fila", detalhe="A conta voltou a ficar em dia")
                mudancas += 1
        if mudancas:
            db.commit()
        return mudancas
    except Exception:  # a tela nunca pode quebrar por causa da fila
        db.rollback()
        logger.exception("Falha ao sincronizar os atendimentos de reputação")
        return 0


# ---------------------------------------------------------------------------
# Ações (assumir / liberar / anotar / concluir)
# ---------------------------------------------------------------------------
class ErroAtendimento(Exception):
    """Erro de regra, com o status HTTP que a rota deve devolver."""

    def __init__(self, status: int, mensagem, dados: dict | None = None):
        super().__init__(mensagem)
        self.status = status
        self.mensagem = mensagem
        self.dados = dados or {}


def aberto_da_conta(db: Session, conta_id: int) -> AtendimentoReputacao:
    atendimento = (
        db.query(AtendimentoReputacao)
        .filter(AtendimentoReputacao.conta_id == conta_id, AtendimentoReputacao.status == "aberto")
        .order_by(AtendimentoReputacao.id.desc())
        .first()
    )
    if atendimento is None:
        raise ErroAtendimento(404, "Essa conta não está na fila de atendimento agora.")
    return atendimento


def assumir(db: Session, conta_id: int, usuario, forcar: bool) -> AtendimentoReputacao:
    atendimento = aberto_da_conta(db, conta_id)
    antes_id, antes_nome = atendimento.em_atendimento_por_id, atendimento.em_atendimento_por
    agora = datetime.utcnow()
    condicao = [AtendimentoReputacao.id == atendimento.id, AtendimentoReputacao.status == "aberto"]
    if not forcar:
        # Só pega se estiver livre (ou já for meu). A decisão é do banco, num UPDATE só.
        condicao.append((AtendimentoReputacao.em_atendimento_por_id.is_(None)) |
                        (AtendimentoReputacao.em_atendimento_por_id == usuario.id))
    resultado = db.execute(
        update(AtendimentoReputacao).where(*condicao)
        .values(em_atendimento_por=usuario.nome_exibicao, em_atendimento_por_id=usuario.id, em_atendimento_desde=agora)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    db.refresh(atendimento)
    if resultado.rowcount == 0:
        if atendimento.status != "aberto":
            raise ErroAtendimento(400, "Esse atendimento já foi concluído.")
        raise ErroAtendimento(409, f"{atendimento.em_atendimento_por or 'Outra pessoa'} já está com essa conta.",
                              {"codigo": "ocupado", "em_atendimento_por": atendimento.em_atendimento_por})
    if antes_id != usuario.id:
        if antes_id is not None:
            registrar(db, atendimento.id, "assumiu_no_lugar", usuario, detalhe=f"no lugar de {antes_nome}")
        else:
            registrar(db, atendimento.id, "assumiu", usuario)
    if not atendimento.assumido_primeiro_em:
        atendimento.assumido_primeiro_em = agora
    db.commit()
    return atendimento


def liberar(db: Session, conta_id: int, usuario) -> None:
    atendimento = aberto_da_conta(db, conta_id)
    dono = atendimento.em_atendimento_por_id
    if dono not in (None, usuario.id) and usuario.papel not in ("admin", "supervisor"):
        raise ErroAtendimento(403, f"Essa conta está com {atendimento.em_atendimento_por}; só essa pessoa (ou um supervisor) pode liberar.")
    if dono is not None:
        registrar(db, atendimento.id, "liberou", usuario,
                  detalhe=None if dono == usuario.id else f"estava com {atendimento.em_atendimento_por}")
    atendimento.em_atendimento_por = None
    atendimento.em_atendimento_por_id = None
    atendimento.em_atendimento_desde = None
    db.commit()


def anotar(db: Session, conta_id: int, usuario, texto: str) -> None:
    texto = (texto or "").strip()
    if not texto:
        raise ErroAtendimento(400, "Escreva a anotação.")
    atendimento = aberto_da_conta(db, conta_id)
    registrar(db, atendimento.id, "anotou", usuario, detalhe=texto)
    db.commit()


def concluir(db: Session, conta_id: int, usuario, texto: str, protocolo: str | None, situacao_atual: str | None) -> None:
    texto = (texto or "").strip()
    protocolo = (protocolo or "").strip()[:TAMANHO_MAX_PROTOCOLO] or None
    if not texto:
        raise ErroAtendimento(400, "Conte o que foi feito para concluir.")
    atendimento = aberto_da_conta(db, conta_id)
    antes_id, antes_nome = atendimento.em_atendimento_por_id, atendimento.em_atendimento_por
    if antes_id != usuario.id:
        # Concluiu sem ter assumido (ou no lugar de alguém): o histórico mostra os dois passos.
        registrar(db, atendimento.id, "assumiu_no_lugar" if antes_id else "assumiu", usuario,
                  detalhe=f"no lugar de {antes_nome}" if antes_id else "ao concluir")
        if not atendimento.assumido_primeiro_em:
            atendimento.assumido_primeiro_em = datetime.utcnow()
    agora = datetime.utcnow()
    atendimento.status = "concluido"
    atendimento.concluido_por = usuario.nome_exibicao
    atendimento.concluido_por_id = usuario.id
    atendimento.concluido_em = agora
    atendimento.conclusao = texto[:TAMANHO_MAX_TEXTO]
    atendimento.protocolo = protocolo
    atendimento.situacao_conclusao = situacao_atual
    atendimento.em_atendimento_por = None
    atendimento.em_atendimento_por_id = None
    atendimento.em_atendimento_desde = None
    registrar(db, atendimento.id, "concluiu", usuario, detalhe=texto + (f" · Protocolo {protocolo}" if protocolo else ""))
    db.commit()


# ---------------------------------------------------------------------------
# Leitura (cartões, detalhe e quadro da supervisão)
# ---------------------------------------------------------------------------
def _iso(dt):
    return dt.isoformat() + "Z" if dt else None


def resumo_do_cartao(a: AtendimentoReputacao | None) -> dict | None:
    """O que o cartão precisa pra desenhar a etiqueta de atendimento."""
    if a is None or a.status == "encerrado":
        return None
    agora = datetime.utcnow()
    parada = bool(a.em_atendimento_desde and a.em_atendimento_desde < agora - timedelta(hours=HORAS_PARADA))
    return {
        "id": a.id,
        "status": a.status,
        "aberto_em": _iso(a.aberto_em),
        "em_atendimento_por": a.em_atendimento_por,
        "em_atendimento_por_id": a.em_atendimento_por_id,
        "em_atendimento_desde": _iso(a.em_atendimento_desde),
        "parada": parada,
        "livre_ha_muito": bool(a.status == "aberto" and a.em_atendimento_por_id is None
                               and a.aberto_em < agora - timedelta(hours=HORAS_PARADA)),
        "concluido_por": a.concluido_por,
        "concluido_em": _iso(a.concluido_em),
        "conclusao": a.conclusao,
        "protocolo": a.protocolo,
    }


def _duracao(segundos: float) -> str:
    minutos = int(max(0, segundos) // 60)
    if minutos < 60:
        return f"{minutos} min"
    horas, resto = divmod(minutos, 60)
    if horas < 24:
        return f"{horas}h{resto:02d}"
    dias, horas = divmod(horas, 24)
    return f"{dias}d {horas}h"


def tempos(a: AtendimentoReputacao, eventos: list) -> dict:
    """Tempo na fila, com cada pessoa e total (mesma lógica das Solicitações)."""
    fim = a.concluido_em or a.encerrado_em or datetime.utcnow()
    trechos: dict[str, float] = {}
    ordem: list[str] = []
    atual, desde, primeiro = None, None, None
    for e in sorted(eventos, key=lambda x: (x.quando, x.id)):
        if e.tipo in ("assumiu", "assumiu_no_lugar", "liberou", "concluiu") and atual and desde:
            trechos[atual] = trechos.get(atual, 0) + (e.quando - desde).total_seconds()
            atual, desde = None, None
        if e.tipo in ("assumiu", "assumiu_no_lugar"):
            atual, desde = (e.usuario_nome or "—"), e.quando
            primeiro = primeiro or e.quando
            if atual not in ordem:
                ordem.append(atual)
    if atual and desde:
        trechos[atual] = trechos.get(atual, 0) + (fim - desde).total_seconds()
    return {
        "fila": _duracao(((primeiro or fim) - a.aberto_em).total_seconds()),
        "por_operador": [{"nome": n, "texto": _duracao(trechos.get(n, 0)), "atual": n == a.em_atendimento_por}
                         for n in ordem],
        "total": _duracao((fim - a.aberto_em).total_seconds()),
    }


def detalhe(db: Session, conta_id: int) -> dict | None:
    """Atendimento mais recente da conta + histórico + tempos (pro painel lateral)."""
    a = (db.query(AtendimentoReputacao).filter(AtendimentoReputacao.conta_id == conta_id)
         .order_by(AtendimentoReputacao.id.desc()).first())
    if a is None:
        return None
    eventos = (db.query(AtendimentoReputacaoEvento)
               .filter(AtendimentoReputacaoEvento.atendimento_id == a.id)
               .order_by(AtendimentoReputacaoEvento.quando.asc(), AtendimentoReputacaoEvento.id.asc()).all())
    try:
        metricas_abertura = json.loads(a.metricas_abertura or "{}")
    except ValueError:
        metricas_abertura = {}
    return {
        **(resumo_do_cartao(a) or {"id": a.id, "status": a.status}),
        "motivo_abertura": a.motivo_abertura,
        "metricas_abertura": metricas_abertura,
        "tempos": tempos(a, eventos),
        "historico": [{
            "tipo": e.tipo, "rotulo": ROTULO_EVENTO.get(e.tipo, e.tipo), "quem": e.usuario_nome,
            "detalhe": e.detalhe, "quando": _iso(e.quando),
        } for e in eventos],
    }
