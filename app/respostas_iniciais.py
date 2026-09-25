"""
Respostas padrão iniciais, cadastradas UMA vez -- só quando a tabela
respostas_padrao é criada pela primeira vez (se alguém apagar depois, não
voltam). Entram DESLIGADAS: nada é enviado a comprador antes de alguém da
equipe revisar o texto e ligar na tela "Respostas padrão".
"""
import logging

from app.models import RespostaPadrao

logger = logging.getLogger(__name__)

INICIAIS = [
    {
        "tema": "Nota fiscal",
        "exemplos": "Possui nota?\nEmite nota fiscal?\nVem com NF?\nTem nota pro CNPJ?",
        "resposta": "Olá! Sim, possui nota fiscal. Você pode acessá-la diretamente na plataforma do Mercado Livre, na página da sua compra. Ficamos à disposição!",
    },
    {
        "tema": "Prazo de entrega",
        "exemplos": "Quando chega?\nQual o prazo de entrega?\nChega até sexta?\nEm quantos dias chega?\nEntrega rápido?",
        "resposta": (
            "Olá! O prazo exato de entrega é calculado pelo Mercado Livre de acordo com o seu CEP: "
            "ele aparece no anúncio, logo abaixo do preço, em \"Chegará…\". Após a compra, você acompanha "
            "a data e o rastreio em \"Minhas compras\". O envio é feito pelo Mercado Envios. Ficamos à disposição!"
        ),
    },
    {
        "tema": "Garantia",
        "exemplos": "Tem garantia?\nQual a garantia?\nQuanto tempo de garantia?",
        "resposta": "Olá! Sim, o produto tem garantia de [PRAZO] contra defeitos de fabricação. Ficamos à disposição!",
    },
]


def semear(db) -> None:
    for item in INICIAIS:
        db.add(RespostaPadrao(**item, alcance="geral", ativa=False, criado_por="sistema (revisar)"))
    db.commit()
    logger.info("Respostas padrão iniciais cadastradas (desligadas, para revisão).")
