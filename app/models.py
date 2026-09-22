"""
Modelos do banco de dados.

IMPORTANTE SOBRE O BANCO: em produção, isso roda em PostgreSQL — foi a
decisão tomada considerando o volume esperado (~100 contas escrevendo
ao mesmo tempo: perguntas, devoluções, notificações). SQLite trava o
arquivo inteiro a cada escrita, o que vira gargalo real nesse cenário.

Pra rodar localmente sem precisar de um servidor Postgres instalado
(útil pra testar essa base agora, com dados de exemplo), o
DATABASE_URL pode apontar pra um arquivo SQLite — o SQLAlchemy é
agnóstico de banco, então o mesmo código funciona nos dois. Mas ao
subir de verdade pras contas reais, o .env precisa apontar pra um
Postgres (ver README.md).
"""
from datetime import datetime
from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    DateTime,
    ForeignKey,
    Text,
    Boolean,
)
from sqlalchemy.orm import relationship
from app.database import Base


class EmpresaPlanejada(Base):
    """
    Lista de referência com o nome de todas as empresas que o Club
    Marketplace gerencia (mesma lista da Central Financeira) -- usada
    só pra acompanhar o progresso do rollout: cruza com a tabela
    `contas` (por nome, sem diferenciar maiúsculas/minúsculas) pra
    mostrar quais já autorizaram e quais ainda faltam. Não participa
    de nenhuma lógica de negócio, é só uma lista de conferência.
    """

    __tablename__ = "empresas_planejadas"

    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String, unique=True, index=True, nullable=False)
    criado_em = Column(DateTime, default=datetime.utcnow)


class Conta(Base):
    """Uma conta de seller do Mercado Livre conectada ao sistema."""

    __tablename__ = "contas"

    id = Column(Integer, primary_key=True, index=True)
    ml_user_id = Column(String, unique=True, index=True, nullable=False)  # ID do usuário no Mercado Livre
    apelido = Column(String, nullable=False)  # nome/apelido da conta (ex: "Velasco")
    access_token = Column(Text, nullable=True)  # preenchido depois da autorização OAuth real
    refresh_token = Column(Text, nullable=True)
    token_expira_em = Column(DateTime, nullable=True)
    conectada_em = Column(DateTime, default=datetime.utcnow)
    ativa = Column(Boolean, default=True)

    devolucoes = relationship("Devolucao", back_populates="conta")
    acoes = relationship("AcaoRegistrada", back_populates="conta")


class Devolucao(Base):
    """Uma devolução (claim do tipo 'return') de uma conta."""

    __tablename__ = "devolucoes"

    id = Column(Integer, primary_key=True, index=True)
    conta_id = Column(Integer, ForeignKey("contas.id"), nullable=False)
    claim_id = Column(String, index=True, nullable=False)  # ID da reclamação no Mercado Livre
    order_id = Column(String, nullable=True)
    sku = Column(String, nullable=True)
    nome_produto = Column(String, nullable=True)
    motivo = Column(String, nullable=True)  # motivo declarado da devolução
    status = Column(String, nullable=False)  # opened, shipped, delivered, closed, cancelled, etc.
    product_condition = Column(String, nullable=True)  # saleable / unsaleable / discard (só quando passou por depósito)
    valor = Column(Float, nullable=True)  # valor do pedido devolvido
    custo_frete_retorno = Column(Float, nullable=True)
    data_criacao = Column(DateTime, nullable=True)  # quando a devolução foi aberta
    data_entrega = Column(DateTime, nullable=True)  # quando chegou de volta ao vendedor
    atualizado_em = Column(DateTime, default=datetime.utcnow)

    conta = relationship("Conta", back_populates="devolucoes")


class AcaoRegistrada(Base):
    """
    Log de toda ação que o sistema tomou numa conta — base do relatório
    por conta (ex: "Velasco: 12 perguntas respondidas, 3 pedidos
    cancelados por falta de estoque"). Cada módulo (pré-venda,
    pós-venda, devoluções) grava aqui o que fez.
    """

    __tablename__ = "acoes_registradas"

    id = Column(Integer, primary_key=True, index=True)
    conta_id = Column(Integer, ForeignKey("contas.id"), nullable=False)
    tipo = Column(String, nullable=False)  # ex: "pergunta_respondida", "pedido_cancelado", "promocao_aderida"
    sku = Column(String, nullable=True)
    detalhe = Column(Text, nullable=True)  # descrição livre da ação (ex: motivo do cancelamento)
    valor_envolvido = Column(Float, nullable=True)  # ex: valor da venda impactada
    criado_em = Column(DateTime, default=datetime.utcnow)

    conta = relationship("Conta", back_populates="acoes")


class Pergunta(Base):
    """
    Uma pergunta de pré-venda recebida via webhook do Mercado Livre
    (tópico 'questions'). Registra o que aconteceu com ela: se foi
    respondida automaticamente, virou candidata, ou caiu pra fila
    humana — e por qual camada da lógica de decisão.
    """

    __tablename__ = "perguntas"

    id = Column(Integer, primary_key=True, index=True)
    conta_id = Column(Integer, ForeignKey("contas.id"), nullable=False)
    ml_question_id = Column(String, unique=True, index=True, nullable=False)
    item_id = Column(String, index=True, nullable=True)  # MLB... do anúncio
    sku = Column(String, index=True, nullable=True)  # SELLER_SKU do item, quando existe
    texto = Column(Text, nullable=False)

    # pendente -> ainda não processada | respondida -> alguém (camada
    # automática ou humano) já enviou a resposta | fila_humana ->
    # nenhuma camada automática resolveu, precisa de humano |
    # respondida_externamente -> já tinha resposta no Mercado Livre
    # antes da gente processar (ex: assistente nativo do ML foi mais
    # rápido) -- não é erro nosso, só não tem nada mais a fazer aqui
    status = Column(String, nullable=False, default="pendente")
    camada_resolvida = Column(String, nullable=True)  # "resposta_validada" | "manual_sku_ia" | "politica_geral" | "manual" | None
    precisa_auditoria = Column(Boolean, default=False)  # True quando a camada 2 (IA) respondeu -- revisar depois

    resposta_enviada = Column(Text, nullable=True)
    recebida_em = Column(DateTime, default=datetime.utcnow)
    respondida_em = Column(DateTime, nullable=True)

    conta = relationship("Conta")


class RespostaValidadaSku(Base):
    """
    Resposta já validada por humano pra um SKU específico — camada 1
    da lógica de decisão. Não é por conta: um SKU cadastrado aqui vale
    pra qualquer conta que venda esse mesmo produto (chave universal).
    """

    __tablename__ = "respostas_validadas_sku"

    id = Column(Integer, primary_key=True, index=True)
    sku = Column(String, index=True, nullable=False)
    pergunta_exemplo = Column(Text, nullable=True)  # a pergunta original que gerou essa resposta
    resposta = Column(Text, nullable=False)
    criado_em = Column(DateTime, default=datetime.utcnow)


class ManualSku(Base):
    """
    Manual técnico/ficha técnica de um SKU — camada 2 da lógica de
    decisão (ainda não conectada a um modelo de IA de verdade; ver
    nota no router de webhook). Cadastrado uma vez, vale pra todas as
    contas que vendem esse SKU.
    """

    __tablename__ = "manuais_sku"

    id = Column(Integer, primary_key=True, index=True)
    sku = Column(String, index=True, nullable=False)
    titulo = Column(String, nullable=True)
    conteudo = Column(Text, nullable=False)
    criado_em = Column(DateTime, default=datetime.utcnow)


class PoliticaGeral(Base):
    """
    Pergunta institucional/comercial genérica, válida igual pra todas
    as contas (desconto por quantidade, nota fiscal, prazo de troca,
    etc.) — camada 3 da lógica de decisão.
    """

    __tablename__ = "politicas_gerais"

    id = Column(Integer, primary_key=True, index=True)
    tema = Column(String, nullable=False)  # ex: "desconto por quantidade"
    palavras_chave = Column(String, nullable=False)  # ex: "desconto,quantidade,atacado" (separadas por vírgula)
    resposta = Column(Text, nullable=False)
    criado_em = Column(DateTime, default=datetime.utcnow)


class MensagemPosVenda(Base):
    """
    Mensagem recebida depois da venda (tópico 'messages' do Mercado
    Livre) ou reclamação/devolução (tópico 'claims') -- tratadas juntas
    na mesma fila de pós-venda, diferenciadas pelo campo 'tipo'.

    Diferente da pré-venda, aqui NUNCA respondemos automático de
    verdade por enquanto -- toda mensagem cai em fila_humana, com uma
    sugestão de resposta da IA (resposta_sugerida) pra agilizar quem
    for responder. É mais seguro dado o risco maior (reclamação,
    devolução, dinheiro envolvido).
    """

    __tablename__ = "mensagens_pos_venda"

    id = Column(Integer, primary_key=True, index=True)
    conta_id = Column(Integer, ForeignKey("contas.id"), nullable=False)
    ml_recurso_id = Column(String, unique=True, index=True, nullable=False)  # message_id ou claim_id
    tipo = Column(String, nullable=False)  # "mensagem" (topic messages) ou "reclamacao" (topic claims)
    pack_id = Column(String, nullable=True)  # só mensagens -- precisa pra responder
    order_id = Column(String, nullable=True)
    sku = Column(String, index=True, nullable=True)
    texto = Column(Text, nullable=False)

    # pendente -> ainda não processada | fila_humana -> esperando
    # atendente responder | respondida -> atendente já respondeu |
    # eh_pedido_cancelamento -> foi identificada como pedido de
    # cancelamento e virou um PedidoCancelamento à parte (não fica
    # solta aqui também, pra não duplicar fila)
    status = Column(String, nullable=False, default="pendente")

    resposta_sugerida = Column(Text, nullable=True)  # rascunho da IA -- nunca enviado sozinho
    resposta_enviada = Column(Text, nullable=True)
    recebida_em = Column(DateTime, default=datetime.utcnow)
    respondida_em = Column(DateTime, nullable=True)

    conta = relationship("Conta")


class PedidoCancelamento(Base):
    """
    Pedido de cancelamento identificado dentro de uma mensagem ou
    reclamação (por palavra-chave, ver pos_venda_logica.py). Fica
    numa fila própria, separada da fila geral de pós-venda, pra dar
    visibilidade e controle -- por enquanto SEMPRE tratado por humano
    (a IA só sugere o texto de resposta, nunca cancela nem envia
    sozinha). Esse histórico serve de base pra, no futuro, a IA
    aprender o padrão de resposta e passar a tratar sozinha.
    """

    __tablename__ = "pedidos_cancelamento"

    id = Column(Integer, primary_key=True, index=True)
    conta_id = Column(Integer, ForeignKey("contas.id"), nullable=False)
    mensagem_pos_venda_id = Column(Integer, ForeignKey("mensagens_pos_venda.id"), nullable=True)
    order_id = Column(String, nullable=True)
    sku = Column(String, index=True, nullable=True)
    texto_pedido = Column(Text, nullable=False)
    resposta_sugerida = Column(Text, nullable=True)  # rascunho da IA -- nunca enviado nem executado sozinho

    # pendente -> aguardando tratamento humano | tratado -> humano já
    # decidiu/agiu (aprovar, negar, orientar) e registrou o desfecho
    status = Column(String, nullable=False, default="pendente")
    observacao_humano = Column(Text, nullable=True)  # o que o atendente decidiu/fez, pra virar histórico depois

    criado_em = Column(DateTime, default=datetime.utcnow)
    tratado_em = Column(DateTime, nullable=True)

    conta = relationship("Conta")


class EventoWebhook(Base):
    """
    Registro de TODA notificação que chega no webhook, sem exceção --
    inclusive as que dão erro, são ignoradas, ou não batem em conta
    nenhuma. É um log de auditoria/diagnóstico: existe justamente pra
    dar pra ver, direto no dashboard (sem precisar abrir o Railway),
    o que o Mercado Livre está mandando e o que o sistema fez com cada
    notificação -- essencial enquanto ajustamos os tópicos novos
    (messages, claims) e ainda não sabemos o formato exato do payload.
    """
    __tablename__ = "eventos_webhook"

    id = Column(Integer, primary_key=True, index=True)
    topico = Column(String, index=True, nullable=True)
    payload_bruto = Column(Text, nullable=True)  # o JSON cru da notificação, como veio
    resultado = Column(Text, nullable=True)  # o que o sistema respondeu/decidiu (status, erro, etc.)
    recebido_em = Column(DateTime, default=datetime.utcnow, index=True)


class SolicitacaoCancelamento(Base):
    """
    Pedido de cancelamento registrado MANUALMENTE por uma conta, através
    da página pública /solicitar-cancelamento (sem login — link único,
    compartilhado com todas as ~100 contas). Diferente de
    PedidoCancelamento (que é detectado pela IA dentro de mensagens de
    pós-venda): aqui é a própria conta que pede o cancelamento por
    motivo operacional (CEP errado, sem estoque, etiqueta não gerada,
    etc.), não o cliente. A data é sempre preenchida pelo servidor,
    nunca vem do formulário.
    """

    __tablename__ = "solicitacoes_cancelamento"

    id = Column(Integer, primary_key=True, index=True)
    plataforma = Column(String, nullable=False)  # "mercado_livre" | "shopee"
    conta = Column(String, nullable=False)
    numero_venda = Column(String, nullable=False)
    motivo = Column(Text, nullable=False)
    criado_em = Column(DateTime, default=datetime.utcnow)

    # Preenchidos só quando alguém confirma que já cancelou de verdade
    # na plataforma. Enquanto nulo, o pedido aparece como "pendente".
    confirmado_por = Column(String, nullable=True)
    confirmado_em = Column(DateTime, nullable=True)


class Usuario(Base):
    """
    Conta de acesso ao painel interno. Três papéis:
      - "admin": acesso total, cria/reseta qualquer usuário (inclusive
        outros admins e supervisores).
      - "supervisor": acesso ao painel interno, cria/reseta só "seller".
      - "seller": login mais restrito -- hoje usado pra acessar a tela
        de solicitar cancelamento da própria conta.

    Senha nunca é guardada em texto puro -- só o hash (ver app/auth.py).
    No primeiro acesso, a pessoa usa um código temporário
    (codigo_primeiro_acesso) em vez da senha; ao trocar pela senha
    pessoal, esse código é apagado e precisa_trocar_senha vira False.
    """

    __tablename__ = "usuarios"

    id = Column(Integer, primary_key=True, index=True)
    usuario = Column(String, unique=True, index=True, nullable=False)
    nome_exibicao = Column(String, nullable=False)
    papel = Column(String, nullable=False)  # "admin" | "supervisor" | "seller"
    senha_hash = Column(String, nullable=True)  # nulo até o primeiro acesso ser concluído
    codigo_primeiro_acesso = Column(String, nullable=True)
    precisa_trocar_senha = Column(Boolean, default=True)
    ativo = Column(Boolean, default=True)
    criado_em = Column(DateTime, default=datetime.utcnow)
    criado_por_usuario_id = Column(Integer, ForeignKey("usuarios.id"), nullable=True)

    # Só usado quando papel == "seller" -- qual conta (ex: "Velasco")
    # esse usuário representa. É o que filtra o que ele vê em
    # /meus-cancelamentos e pré-preenche a conta no formulário público.
    conta_vinculada = Column(String, nullable=True)
