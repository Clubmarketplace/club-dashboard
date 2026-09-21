"""
Teste rápido da lógica de pré-venda (camadas 1, 2 e 3) sem precisar de
uma pergunta real vinda do Mercado Livre -- só simula o SKU e o texto
da pergunta, e mostra a decisão que o sistema tomaria de verdade.

Rode com (troque o SKU e a pergunta pelos seus):
    venv\\Scripts\\python.exe -m app.testar_resposta LP320-220V "Esse produto e bivolt?"
"""
import sys

from app.database import SessionLocal
from app.pre_venda_logica import decidir_resposta


def rodar():
    if len(sys.argv) < 3:
        print('Uso: python -m app.testar_resposta SEU-SKU "texto da pergunta"')
        return

    sku = sys.argv[1]
    texto_pergunta = sys.argv[2]

    db = SessionLocal()
    decisao = decidir_resposta(db, sku, texto_pergunta)
    db.close()

    print(f"\nSKU: {sku}")
    print(f"Pergunta: {texto_pergunta}")
    print(f"Status: {decisao['status']}")
    print(f"Camada: {decisao['camada']}")
    print(f"Resposta: {decisao['resposta']}")


if __name__ == "__main__":
    rodar()
