"""
Registro de histórico de alterações de custo por SKU -- pra responder
"quando e quem mudou esse custo" caso um valor errado apareça depois.

Um arquivo só, chamado tanto pelo painel (app/routers/custos_painel.py)
quanto pela extensão (app/routers/cmx.py), pra não duplicar a regra de
"só grava se realmente mudou algo" nos dois lugares.
"""
from app.models import LogCustoSku


def registrar_log_custo(
    db,
    conta_id: int,
    sku: str,
    custo_anterior: float | None,
    custo_novo: float | None,
    nome_produto: str | None,
    alterado_por: str | None,
    origem: str,
) -> None:
    """
    Grava uma linha de histórico. Não decide por conta própria se deve
    registrar -- quem chama já filtrou antes (normalmente: só quando
    custo_anterior != custo_novo, ou numa remoção). Não dá commit aqui:
    fica na mesma transação de quem chamou, pra nunca salvar o log sem
    salvar a alteração em si (ou vice-versa).
    """
    db.add(
        LogCustoSku(
            conta_id=conta_id,
            sku=sku,
            custo_anterior=custo_anterior,
            custo_novo=custo_novo,
            nome_produto=nome_produto,
            alterado_por=alterado_por,
            origem=origem,
        )
    )
