"""
Reforço silencioso de vendas: roda sozinho, em segundo plano, o tempo
todo que o servidor estiver ligado -- NÃO depende de ninguém abrir o
painel do admin ou do seller.

Por quê: a via principal de uma venda chegar no nosso banco é o webhook
do Mercado Livre (tempo real, sem custo de requisição nenhuma). Mas às
vezes esse webhook falha pra uma conta específica -- o caso real que
motivou isso (12/10): uma conta foi reautorizada no Mercado Livre e a
inscrição do webhook não "pegou" de novo, então as vendas dela paravam
de chegar automaticamente, e só apareciam de novo se alguém entrasse
como aquela conta e clicasse "Atualizar agora" na tela de Vendas.

Como funciona, pra caber até várias centenas de contas sem pesar:
  - Passa pelas contas ativas e conectadas UMA de cada vez, nunca em
    paralelo -- com uma pausa curta entre cada (gentileza com a API do
    ML, mesmo padrão já usado em app/reputacao.py).
  - Cada conta: só os ÚLTIMOS 2 DIAS (janela curta, rápida -- não é
    recarregar o histórico todo).
  - Terminou a última conta da volta, espera um tempinho e começa de
    novo da primeira -- ciclo contínuo.
  - Erro numa conta (token revogado, ML fora do ar) nunca trava as
    outras nem para o loop -- fica só registrado no log.

Desliga (ex: debug local) com REFORCO_VENDAS_ATIVO=0 na env.
Ajustes finos, se um dia precisar: REFORCO_VENDAS_ESPERA_VOLTA_MIN
(espera entre uma volta completa e a próxima, padrão 40 min -- acima do
que a maioria dos sistemas usa pra esse tipo de reconciliação
periódica, que costuma ficar entre 30 e 60 min; o webhook continua
sendo a via em tempo real, isso aqui é só a rede de segurança).
"""
import asyncio
import logging
import os
import threading
import time
from datetime import datetime

from app import ml_client
from app.database import SessionLocal
from app.ml_client import MLApiError, MLAuthError
from app.models import Conta

logger = logging.getLogger(__name__)

PAUSA_ENTRE_CONTAS_SEG = 3.0
DIAS_JANELA_REFORCO = 2

_trava = threading.Lock()
_progresso = {"em_andamento": False, "feitas": 0, "total": 0, "inicio": None, "fim": None}


def progresso() -> dict:
    return dict(_progresso)


def _sincronizar_uma_conta(conta: Conta, db) -> bool:
    """Sincroniza os últimos DIAS_JANELA_REFORCO dias de uma conta. Nunca levanta exceção."""
    try:
        ml_client.sincronizar_vendas_recentes(conta, db, dias=DIAS_JANELA_REFORCO)
        return True
    except (MLAuthError, MLApiError) as exc:
        logger.warning("Reforço de vendas: conta %s não sincronizada (%s)", conta.apelido, exc)
        return False
    except Exception:
        logger.exception("Reforço de vendas: erro inesperado na conta %s", conta.apelido)
        return False


def atualizar_todas() -> dict:
    """
    Sincroniza os últimos 2 dias de todas as contas ativas e conectadas,
    uma a uma, com pausa entre cada. Se já houver uma volta em
    andamento, não começa outra (devolve o progresso atual).
    """
    if not _trava.acquire(blocking=False):
        return progresso()
    try:
        with SessionLocal() as db:
            contas = (
                db.query(Conta)
                .filter(Conta.ativa.is_(True), Conta.inativa_em.is_(None), Conta.access_token.isnot(None))
                .order_by(Conta.apelido)
                .all()
            )
            _progresso.update(em_andamento=True, feitas=0, total=len(contas),
                               inicio=datetime.utcnow().isoformat(), fim=None)
            sincronizadas = 0
            for conta in contas:
                if _sincronizar_uma_conta(conta, db):
                    sincronizadas += 1
                _progresso["feitas"] += 1
                time.sleep(PAUSA_ENTRE_CONTAS_SEG)
        logger.info("Reforço de vendas: %s de %s contas sincronizadas", sincronizadas, len(contas))
    finally:
        _progresso.update(em_andamento=False, fim=datetime.utcnow().isoformat())
        _trava.release()
    return progresso()


async def loop_reforco_vendas() -> None:
    """Roda atualizar_todas() em ciclo contínuo, sem travar o servidor."""
    try:
        espera_entre_voltas_min = max(1, int(os.getenv("REFORCO_VENDAS_ESPERA_VOLTA_MIN", "40")))
    except ValueError:
        espera_entre_voltas_min = 40
    await asyncio.sleep(60)  # deixa o sistema subir por completo antes da 1ª volta
    while True:
        try:
            await asyncio.to_thread(atualizar_todas)
        except Exception:
            logger.exception("Erro inesperado no loop de reforço de vendas")
        await asyncio.sleep(espera_entre_voltas_min * 60)
