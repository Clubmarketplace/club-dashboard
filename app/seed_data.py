"""
Popula o banco com dados de EXEMPLO — só pra validar o painel visual
antes de conectar a conta Velasco de verdade via OAuth. Rode com:
    python -m app.seed_data
"""
import random
from datetime import datetime, timedelta
from app.database import SessionLocal, engine, Base
from app.models import Conta, Devolucao, AcaoRegistrada

Base.metadata.create_all(bind=engine)

MOTIVOS = [
    "Produto com defeito",
    "Não era o que esperava",
    "Chegou diferente do anunciado",
    "Mudei de ideia",
    "Produto danificado no transporte",
    "Tamanho/cor errado",
]

PRODUTOS = [
    ("SKU-VLC-001", "Ventilador de Teto Tron 3 Pás Preto"),
    ("SKU-VLC-002", "Cadeira de Montagem Simples Natural"),
    ("SKU-VLC-003", "Kit 2 Ventiladores de Teto Eco Rio"),
    ("SKU-VLC-004", "Secadora de Roupas de Parede Comfort"),
    ("SKU-VLC-005", "Cortador/Aparador de Grama Elétrico"),
]


def rodar():
    db = SessionLocal()

    conta = db.query(Conta).filter(Conta.apelido == "Velasco (exemplo)").first()
    if not conta:
        conta = Conta(ml_user_id="EXEMPLO-000000", apelido="Velasco (exemplo)", ativa=True)
        db.add(conta)
        db.commit()
        db.refresh(conta)

    # Limpa dados de exemplo anteriores dessa conta, pra rodar de novo sem duplicar
    db.query(Devolucao).filter(Devolucao.conta_id == conta.id).delete()
    db.query(AcaoRegistrada).filter(AcaoRegistrada.conta_id == conta.id).delete()
    db.commit()

    hoje = datetime.utcnow()

    # --- Devoluções de exemplo, espalhadas nos últimos 60 dias ---
    for i in range(48):
        dias_atras = random.randint(0, 60)
        data_criacao = hoje - timedelta(days=dias_atras)
        sku, nome = random.choice(PRODUTOS)
        status = random.choices(
            ["opened", "shipped", "delivered", "closed", "cancelled"],
            weights=[15, 10, 15, 50, 10],
        )[0]
        data_entrega = data_criacao + timedelta(days=random.randint(2, 10)) if status in ("delivered", "closed") else None
        product_condition = None
        if status == "closed":
            product_condition = random.choices(["saleable", "unsaleable", "discard"], weights=[55, 35, 10])[0]
        valor = round(random.uniform(80, 900), 2)
        custo_frete = round(valor * random.uniform(0.04, 0.09), 2)

        db.add(
            Devolucao(
                conta_id=conta.id,
                claim_id=f"EXEMPLO-CLAIM-{1000 + i}",
                order_id=f"EXEMPLO-ORDER-{2000 + i}",
                sku=sku,
                nome_produto=nome,
                motivo=random.choice(MOTIVOS),
                status=status,
                product_condition=product_condition,
                valor=valor,
                custo_frete_retorno=custo_frete,
                data_criacao=data_criacao,
                data_entrega=data_entrega,
            )
        )

    # --- Ações de exemplo (pra alimentar o relatório por conta) ---
    tipos_acao = [
        ("pergunta_respondida", None),
        ("pedido_cancelado", "Sem estoque no momento da venda"),
        ("promocao_aderida", None),
    ]
    for i in range(30):
        dias_atras = random.randint(0, 30)
        tipo, detalhe_fixo = random.choice(tipos_acao)
        sku, _ = random.choice(PRODUTOS)
        valor_envolvido = round(random.uniform(50, 500), 2) if tipo == "pedido_cancelado" else None
        db.add(
            AcaoRegistrada(
                conta_id=conta.id,
                tipo=tipo,
                sku=sku,
                detalhe=detalhe_fixo,
                valor_envolvido=valor_envolvido,
                criado_em=hoje - timedelta(days=dias_atras),
            )
        )

    db.commit()
    db.close()
    print("Dados de exemplo criados com sucesso.")


if __name__ == "__main__":
    rodar()
