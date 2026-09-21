"""
Cadastra uma política geral de EXEMPLO, só pra testar a camada 3 da
pré-venda. Rode com:
    python -m app.seed_conhecimento

Pra manuais técnicos por SKU (camada 2), não edite mais este arquivo
-- em vez disso, coloque o arquivo (PDF ou .txt) na pasta configurada
em MANUAIS_LOCAL_DIR (padrão: "manuais", dentro da pasta do projeto),
nomeado exatamente como o SKU -- ex: "LP320-220V.pdf" -- e rode:
    python -m app.importar_manuais
"""
from app.database import SessionLocal, engine, Base
from app.models import PoliticaGeral

Base.metadata.create_all(bind=engine)

POLITICA_DE_TESTE = (
    "Emitimos nota fiscal em todas as vendas automaticamente -- ela "
    "é enviada pelo próprio Mercado Livre junto com os dados de envio, "
    "não precisa pedir separado."
)


def rodar():
    db = SessionLocal()

    if not db.query(PoliticaGeral).filter(PoliticaGeral.tema == "nota fiscal").first():
        db.add(PoliticaGeral(tema="nota fiscal", palavras_chave="nota fiscal,nota,nf,cupom fiscal", resposta=POLITICA_DE_TESTE))
        db.commit()
        print("Política geral de teste cadastrada (nota fiscal).")
    else:
        print("Política geral de teste já existia -- nada a fazer.")

    db.close()


if __name__ == "__main__":
    rodar()
