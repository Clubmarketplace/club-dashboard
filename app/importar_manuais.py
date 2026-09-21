"""
Varre a pasta (ou bucket S3, dependendo do .env) de manuais e
cadastra/atualiza um ManualSku no banco pra cada arquivo encontrado --
um arquivo por SKU (o nome do arquivo, sem extensão, é o SKU).

Rode toda vez que adicionar ou atualizar um manual na pasta:
    venv\\Scripts\\python.exe -m app.importar_manuais
"""
from app.database import SessionLocal, engine, Base
from app.models import ManualSku
from app import manuais_storage

Base.metadata.create_all(bind=engine)


def rodar():
    db = SessionLocal()
    arquivos = manuais_storage.listar_arquivos()

    if not arquivos:
        print("Nenhum manual encontrado. Confira a pasta (ou bucket) configurado no .env.")
        db.close()
        return

    novos, atualizados, ignorados = 0, 0, 0
    for item in arquivos:
        sku = item["sku"]
        nome_arquivo = item["nome_arquivo"]
        try:
            texto = manuais_storage.ler_texto(nome_arquivo)
        except manuais_storage.ErroManuais as exc:
            print(f"[ERRO] {nome_arquivo}: {exc}")
            ignorados += 1
            continue

        if not texto.strip():
            print(f"[AVISO] {nome_arquivo} não tem texto para extrair (PDF escaneado como imagem?) -- pulando.")
            ignorados += 1
            continue

        manual = db.query(ManualSku).filter(ManualSku.sku == sku).first()
        if manual:
            manual.conteudo = texto
            manual.titulo = nome_arquivo
            atualizados += 1
            print(f"Atualizado: SKU '{sku}' <- {nome_arquivo}")
        else:
            db.add(ManualSku(sku=sku, titulo=nome_arquivo, conteudo=texto))
            novos += 1
            print(f"Novo: SKU '{sku}' <- {nome_arquivo}")

    db.commit()
    db.close()
    print(f"\nPronto. {novos} manual(is) novo(s), {atualizados} atualizado(s), {ignorados} ignorado(s).")


if __name__ == "__main__":
    rodar()
