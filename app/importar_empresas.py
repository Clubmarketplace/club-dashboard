"""
Lê dados/empresas.txt (um nome de empresa por linha, exatamente como
usado na Central Financeira) e cadastra/atualiza a lista de
EmpresaPlanejada -- só pra acompanhar o progresso de autorização na
tela /contas (quais já autorizaram, quais ainda faltam).

Rode toda vez que atualizar a lista:
    venv\\Scripts\\python.exe -m app.importar_empresas
"""
import os

from app.database import SessionLocal, engine, Base
from app.models import EmpresaPlanejada

Base.metadata.create_all(bind=engine)

CAMINHO_LISTA = os.path.join("dados", "empresas.txt")


def rodar():
    if not os.path.isfile(CAMINHO_LISTA):
        print(f"Não encontrei {CAMINHO_LISTA}. Cria esse arquivo com um nome de empresa por linha.")
        return

    with open(CAMINHO_LISTA, "r", encoding="utf-8") as f:
        nomes = [linha.strip() for linha in f if linha.strip()]

    if not nomes:
        print(f"{CAMINHO_LISTA} está vazio -- nada a importar.")
        return

    db = SessionLocal()
    novos = 0
    for nome in nomes:
        if not db.query(EmpresaPlanejada).filter(EmpresaPlanejada.nome == nome).first():
            db.add(EmpresaPlanejada(nome=nome))
            novos += 1
    db.commit()
    total = db.query(EmpresaPlanejada).count()
    db.close()

    print(f"Pronto. {novos} empresa(s) nova(s) adicionada(s). Total na lista: {total}.")


if __name__ == "__main__":
    rodar()
