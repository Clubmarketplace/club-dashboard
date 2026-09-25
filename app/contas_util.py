"""
Regras únicas de nome de conta (loja), usadas em todo o sistema.

Cada conta tem duas formas:
  - CHAVE: pra comparar/agrupar/filtrar. Minúsculo, sem acento, sem
    espaço sobrando. "FRIAÇA", "Friaça" e " friaca " -> "friaca".
  - NOME DE EXIBIÇÃO: como aparece na tela ("Friaça").

Tudo que compara usa a chave; nunca o texto digitado cru. É a evolução
da correção antiga que só ignorava maiúscula/minúscula.
"""
import re
import unicodedata

# Sufixos que o pessoal costuma colar no nome por hábito ("Velasco ML").
# A plataforma é escolhida nos botões, então isso não pode virar conta nova.
_SUFIXOS_PLATAFORMA = (
    "mercado livre", "mercadolivre", "meli", "ml",
    "shopee",
    "magazine luiza", "magalu",
    "tiktok shop", "tiktokshop", "tiktok",
)


def _sem_acento(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))


def limpar_espacos(texto: str) -> str:
    return re.sub(r"\s+", " ", (texto or "").strip())


def chave_conta(nome: str) -> str:
    """Chave de comparação da conta (ver docstring do módulo)."""
    return _sem_acento(limpar_espacos(nome)).lower()


def sufixo_de_plataforma(nome: str) -> str | None:
    """Se o nome termina com ML/Shopee/etc., devolve o sufixo encontrado."""
    chave = chave_conta(nome)
    for sufixo in _SUFIXOS_PLATAFORMA:
        if chave.endswith(" " + sufixo):
            return sufixo
    return None


def nome_exibicao_novo(nome: str) -> str:
    """
    Nome de exibição pra uma conta que ainda não existe em lugar nenhum:
    se veio tudo minúsculo ou tudo maiúsculo, capitaliza cada palavra
    ("velasco" -> "Velasco"); se já veio com letras misturadas, respeita.
    """
    limpo = limpar_espacos(nome)
    if limpo.islower() or limpo.isupper():
        return " ".join(p[:1].upper() + p[1:].lower() for p in limpo.split(" "))
    return limpo


def achar_conta_por_nome(db, nome_ou_chave: str):
    """
    Acha a Conta (Contas conectadas) pelo nome, usando a chave normalizada.

    Pode haver mais de uma conta com a mesma chave (ex.: "Velasco" antiga de
    teste e "VELASCO" real). Regra de desempate:
      1) ignora contas inativas ("saiu do Club");
      2) prefere a que está conectada (tem token);
      3) entre iguais, a mais recente (id maior).
    Devolve None se não houver conta ativa com esse nome.
    """
    from app.models import Conta  # import local: evita ciclo na carga

    alvo = chave_conta(nome_ou_chave)
    candidatas = [
        c for c in db.query(Conta).filter(Conta.inativa_em.is_(None)).all()
        if chave_conta(c.apelido) == alvo
    ]
    if not candidatas:
        return None
    candidatas.sort(key=lambda c: (bool(c.access_token), c.id), reverse=True)
    return candidatas[0]
