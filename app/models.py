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
    # "Saiu do Club": preenchido quando o admin inativa a conta (vazio = conta
    # ativa). É separado de "ativa" porque "ativa" já significa só "conectada"
    # (o Desconectar zera). Conta inativa some das listas/filtros, mas o
    # histórico dela é preservado; reativar limpa este campo.
    inativa_em = Column(DateTime, nullable=True)
    motivo_inativacao = Column(String, nullable=True)

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
    # Todos os SKUs do anúncio separados por vírgula (anúncio com variações
    # pode ter um por variação). "" = anúncio lido e sem SKU; nulo = ainda
    # não lido (ou a leitura falhou) -- a ferramenta de preenchimento tenta de novo.
    skus_anuncio = Column(String, nullable=True)
    titulo_anuncio = Column(String, nullable=True)  # nome do produto no anúncio, quando o ML deixa ler
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

    # --- Origem da solicitação (colunas adicionadas depois; em bancos
    # antigos são criadas por database.garantir_estrutura_atualizada) ---
    # "seller" (logado), "logistica" (galpão) ou "publico" (link sem
    # login). Nulo = registro antigo, de antes dessa informação existir.
    origem = Column(String, nullable=True)
    solicitado_por = Column(String, nullable=True)  # nome de quem estava logado
    galpao = Column(Integer, nullable=True)  # 1 ou 2 -- só pra origem "logistica"
    # Nome da conta normalizado (minúsculo, sem acento, sem espaço sobrando)
    # -- é por ele que tudo compara/agrupa, pra "Friaça" e "friaca" nunca
    # virarem duas contas. O campo "conta" guarda o nome de exibição.
    conta_chave = Column(String, nullable=True, index=True)

    # Impacto do cancelamento na reputação: "sem_impacto" | "com_impacto" |
    # "aguardando_confirmacao". Preenchido na confirmação manual ou pela
    # verificação automática (app/verificacao_cancelamento.py), que lê o
    # cancel_detail do próprio Mercado Livre. Nulo = ainda não informado.
    resultado_impacto = Column(String, nullable=True)

    # Protocolo do cancelamento informado na confirmação manual.
    # Nulo = não informado (registros antigos ou confirmação automática);
    # "" (vazio) = confirmado explicitamente como "sem protocolo"
    # (cancelado direto no painel do Mercado Livre).
    protocolo = Column(String, nullable=True)

    # "Em atendimento": quem assumiu o tratamento desse pedido, pra outro
    # operador não pegar o mesmo. Vale por MINUTOS_EXPIRA_ATENDIMENTO
    # (routers/solicitacoes_cancelamento.py); depois disso volta a ficar livre.
    em_atendimento_por = Column(String, nullable=True)        # nome de exibição
    em_atendimento_por_id = Column(Integer, nullable=True)    # id do usuário
    em_atendimento_desde = Column(DateTime, nullable=True)

    # Quem assumiu o pedido pela PRIMEIRA vez e quando (fica gravado mesmo
    # depois de confirmar) -- base dos tempos do relatório.
    assumido_primeiro_por = Column(String, nullable=True)
    assumido_primeiro_em = Column(DateTime, nullable=True)

    # Produto vendido. Mercado Livre: lido do próprio pedido (order_items).
    # Outras plataformas: digitado (opcional) no formulário.
    # sku nulo = ainda não lido; "" = lido e o pedido não tem SKU.
    sku = Column(String, nullable=True, index=True)
    produto_titulo = Column(String, nullable=True)


class SolicitacaoEvento(Base):
    """
    Histórico de cada solicitação de cancelamento: quem registrou, assumiu,
    liberou, assumiu no lugar de outro e confirmou -- e quando. Só se
    ACRESCENTA (nunca altera nem apaga), pra servir de trilha pros
    relatórios por operador e pro "ver histórico" da tela.
    """

    __tablename__ = "solicitacao_eventos"

    id = Column(Integer, primary_key=True, index=True)
    solicitacao_id = Column(Integer, ForeignKey("solicitacoes_cancelamento.id"), nullable=False, index=True)
    # registrou | assumiu | assumiu_no_lugar | liberou | confirmou | confirmou_automatico
    tipo = Column(String, nullable=False, index=True)
    usuario_id = Column(Integer, nullable=True)
    usuario_nome = Column(String, nullable=True)
    detalhe = Column(Text, nullable=True)
    quando = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


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
    papel = Column(String, nullable=False)  # "admin" | "supervisor" | "atendente" | "logistica" | "seller"
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


class ReputacaoConta(Base):
    """
    Última leitura da reputação de cada conta no Mercado Livre
    (GET /users/{id} -> seller_reputation). Uma linha por conta, sobrescrita
    a cada leitura -- é uma "foto" do termômetro, não histórico.
    As taxas ficam como o ML manda (fração: 0.0088 = 0,88%).
    """

    __tablename__ = "reputacao_contas"

    id = Column(Integer, primary_key=True, index=True)
    conta_id = Column(Integer, ForeignKey("contas.id"), nullable=False, unique=True, index=True)
    lido_em = Column(DateTime, nullable=True)       # última leitura BEM-SUCEDIDA
    tentativa_em = Column(DateTime, nullable=True)  # última tentativa (com ou sem sucesso)
    erro = Column(String, nullable=True)            # motivo da última falha (vazio = ok)

    level_id = Column(String, nullable=True)             # ex.: "5_green"
    power_seller_status = Column(String, nullable=True)  # silver / gold / platinum / vazio
    vendas = Column(Integer, nullable=True)
    periodo = Column(String, nullable=True)              # ex.: "60 days"

    reclamacoes_taxa = Column(Float, nullable=True)
    reclamacoes_qtd = Column(Integer, nullable=True)
    mediacoes_taxa = Column(Float, nullable=True)
    mediacoes_qtd = Column(Integer, nullable=True)
    canceladas_taxa = Column(Float, nullable=True)
    canceladas_qtd = Column(Integer, nullable=True)
    atrasos_taxa = Column(Float, nullable=True)
    atrasos_qtd = Column(Integer, nullable=True)

    bruto = Column(Text, nullable=True)  # seller_reputation completo (JSON), pra conferência


class RespostaPadrao(Base):
    """
    Resposta padrão cadastrada pela equipe (tela "Respostas padrão"):
    tema + exemplos de pergunta + resposta. A nossa IA entende o SENTIDO
    (não precisa bater a palavra exata) e usa quando a IA do ML não
    respondeu. Substitui, com vantagem, a antiga PoliticaGeral por
    palavra-chave (que continua funcionando como último recurso).

    alcance: "geral"   -> vale pra todas as contas
             "produto" -> vale só pro produto de produto_chave (SKU, ou
                          o código MLB do anúncio quando não tem SKU)
    """

    __tablename__ = "respostas_padrao"

    id = Column(Integer, primary_key=True, index=True)
    tema = Column(String, nullable=False)
    exemplos = Column(Text, nullable=False, default="")  # um exemplo de pergunta por linha
    resposta = Column(Text, nullable=False)
    alcance = Column(String, nullable=False, default="geral")
    produto_chave = Column(String, nullable=True, index=True)
    produto_nome = Column(String, nullable=True)  # só pra exibir na tela
    ativa = Column(Boolean, nullable=False, default=True)
    usada = Column(Integer, nullable=False, default=0)  # quantas vezes a IA usou
    criado_por = Column(String, nullable=True)
    criado_em = Column(DateTime, default=datetime.utcnow)
    atualizado_em = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
