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
    LargeBinary,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import relationship, deferred
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
    # Incluídas pela tela Contas (as antigas vieram do dados/empresas.txt):
    plataformas = Column(String, nullable=True)   # "mercado_livre,shopee,..." (só informativo por enquanto)
    observacao = Column(String, nullable=True)
    criado_por = Column(String, nullable=True)
    removida_em = Column(DateTime, nullable=True)  # saiu do Club: some das listas (o histórico fica)


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
    # Faixa de margem (%) usada pela tela "Vendas" pra classificar cada
    # venda como boa/ruim (ver app/routers/vendas.py). Nulo = ainda não
    # configurado pelo seller (a tela então só mostra o número, sem
    # classificar). Era só local (chrome.storage.local da extensão,
    # perdia ao trocar de PC); agora mora aqui pra valer pro painel todo.
    margem_minima = Column(Float, nullable=True)
    margem_maxima = Column(Float, nullable=True)
    # 07/10: token do app "ClubMarketplaceX" (promoções, usado pela
    # extensão) -- SEPARADO de access_token/refresh_token acima, que são
    # do app "mestre" (pré/pós-venda). Ver app/cmx.py.
    cmx_access_token = Column(Text, nullable=True)
    cmx_refresh_token = Column(Text, nullable=True)
    cmx_token_expira_em = Column(DateTime, nullable=True)
    # App ClubMarketplaceX PRÓPRIO dessa conta (cada conta tem o seu,
    # cadastrado separadamente no Mercado Livre Devs) -- usado em vez de
    # uma credencial global única. Precisa ser preenchido (ver rota
    # admin /api/cmx/admin/app-promocoes/{apelido}) antes da conta
    # conseguir autorizar/usar a adesão via API.
    cmx_client_id = Column(String, nullable=True)
    cmx_client_secret = Column(Text, nullable=True)

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
    respondida_por = Column(String, nullable=True)  # nome do atendente (só quando a equipe respondeu pela nossa tela)

    # Revisão das respostas da nossa IA marcadas "revisar" (ficha do anúncio,
    # manual ou internet): "certa" | "corrigida" | "errada". A resposta que o
    # cliente recebeu não muda (o ML não deixa editar); a revisão ensina o banco.
    revisao = Column(String, nullable=True)
    revisado_por = Column(String, nullable=True)
    revisado_em = Column(DateTime, nullable=True)
    resposta_corrigida = Column(Text, nullable=True)
    # De onde a pesquisa na internet tirou a resposta (ex.: "2 · manual em PDF").
    fonte_detalhe = Column(String, nullable=True)

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

    # Tipo da solicitação: "cancelamento" ou "reputacao" (pedido pra
    # contestar/retirar um impacto de reputação no marketplace).
    # Nulo = registro antigo, tratado como "cancelamento".
    tipo = Column(String, nullable=True, index=True)

    # --- 16/10: exclusão (admin) ------------------------------------------
    # Excluir NÃO apaga a linha -- só marca excluido_em preenchido e guarda
    # quem e por quê (motivo obrigatório na tela). Some de todas as listas
    # (painel, Minhas solicitações, relatórios), mas fica no banco pra
    # auditoria -- mesma lógica já usada em ProdutoCadastro.excluido_em.
    excluido_por = Column(String, nullable=True)
    excluido_em = Column(DateTime, nullable=True)
    motivo_exclusao = Column(Text, nullable=True)

    # --- 16/10: evidência (fotos e/ou link) -- pedida principalmente pra
    # Reputação, mas vale pros dois tipos (mesmo painel). Cada solicitação
    # (uma por venda) pode ter VÁRIAS fotos -- gravadas direto no banco como
    # uma lista JSON de data URLs base64 (cada uma já redimensionada/
    # comprimida no navegador antes de enviar). NÃO vai pro disco do
    # container, que é efêmero no Railway (perderia tudo a cada deploy);
    # ver app/manuais_storage.py pro mesmo problema resolvido com S3 pros
    # manuais. Foto(s) e link são opcionais e independentes; nulo = não
    # anexado. fotos_base64 nulo ou "[]" = nenhuma foto.
    # deferred=True: as fotos (base64, dezenas de KB cada) só são lidas do banco
    # quando alguém acessa este campo de propósito (ver GET /{id}/fotos). Sem isso
    # QUALQUER consulta que traga solicitações também trazia todas as fotos --
    # em 26/10 isso travou o sistema (memória/banco) depois da entrega das fotos.
    fotos_base64 = deferred(Column(Text, nullable=True))
    link_evidencia = Column(String, nullable=True)

    # --- 26/10: REVERSO (tipo "reverso") -- produto que voltou (devolução,
    # recusa na entrega, chegou quebrado). Reaproveita o mesmo fluxo das
    # outras solicitações (assumir -> concluir, mesmo painel/histórico) e
    # só acrescenta o que é próprio do reverso. Nulos nos outros tipos.
    # Aspecto do produto ao chegar: bom | avariado | faltando_peca | diferente.
    estado_chegada = Column(String, nullable=True)
    # v30: sugestão do galpão: "bom" | "quebrado" (+ problema). Informativo; a equipe decide.
    avaliacao_galpao = Column(String, nullable=True)
    # v31: por que o produto voltou (devolucao/recusa/chegou_quebrado/envio_errado/outro) + detalhe do envio errado
    motivo_reverso = Column(String, nullable=True, index=True)
    detalhe_envio = Column(String, nullable=True)
    # v31: "Em análise" (não conclui): quando/quem marcou e a observação interna (seller não vê)
    em_analise_em = Column(DateTime, nullable=True)
    em_analise_por = Column(String, nullable=True)
    em_analise_obs = Column(Text, nullable=True)
    problema_galpao = Column(String, nullable=True)
    # Como o reverso terminou (ver RESULTADOS_REVERSO no router): liberado_venda,
    # negociacao_ml, negociacao_ressarcido, negociacao_sem_ressarcimento, descartado.
    resultado_reverso = Column(String, nullable=True, index=True)
    # Liberação PELO GALPÃO (checklist completo): quem, quando e o checklist
    # marcado (JSON {"caixa":true,...}). Nulo = não foi liberado pelo galpão.
    liberado_por = Column(String, nullable=True)
    liberado_em = Column(DateTime, nullable=True)
    checklist_json = Column(Text, nullable=True)
    # Observação livre de quem concluiu (ex.: "ML ressarciu R$ 120 em 3 dias").
    observacao_resultado = Column(Text, nullable=True)


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


class SolicitacaoFoto(Base):
    """
    Uma FOTO de uma solicitação (26/10). Antes as fotos ficavam como texto base64
    dentro da própria solicitação (coluna fotos_base64) e travavam o sistema ao
    carregar listas. Agora cada foto é uma linha aqui:
      - miniatura (~5 KB, JPEG): sempre no banco -- o painel mostra só ela;
      - foto grande: começa no banco (destino="db", coluna dados, binário) e, com o
        Cloudflare R2 configurado, é movida em segundo plano para o R2
        (destino="r2", chave_r2) e some da coluna dados. Se o R2 falhar, a foto
        continua no banco e o sistema tenta de novo -- nunca se perde.
    Sem FOREIGN KEY de propósito: apagar/limpar nunca é barrado por esta tabela.
    Fotos antigas (coluna fotos_base64) continuam sendo lidas e são convertidas aos poucos.
    """

    __tablename__ = "solicitacao_fotos"

    id = Column(Integer, primary_key=True, index=True)
    solicitacao_id = Column(Integer, nullable=False, index=True)
    ordem = Column(Integer, nullable=False, default=0)
    mime = Column(String, nullable=False, default="image/jpeg")
    tamanho = Column(Integer, nullable=False, default=0)          # bytes da foto grande
    destino = Column(String, nullable=False, default="db", index=True)  # db | r2
    chave_r2 = Column(String, nullable=True)
    # Binários "adiados": só são lidos quando alguém pede a foto (nunca em listas).
    dados = deferred(Column(LargeBinary, nullable=True))
    miniatura = deferred(Column(LargeBinary, nullable=True))
    criado_em = Column(DateTime, default=datetime.utcnow, nullable=False)


class AtendimentoReputacao(Base):
    """
    Atendimento de uma conta que entrou na zona de atenção da reputação
    (tela "Reputação das contas"). Mesmo padrão das solicitações: alguém
    ASSUME, pode passar pra outra pessoa, anota o que fez e CONCLUI.

    Uma conta pode ter vários atendimentos ao longo do tempo (um por vez
    aberto). Quem abre/fecha automaticamente é app/reputacao_atendimento.py
    (sincronizar), a partir da classificação do termômetro.
    """

    __tablename__ = "atendimentos_reputacao"

    id = Column(Integer, primary_key=True, index=True)
    conta_id = Column(Integer, ForeignKey("contas.id"), nullable=False, index=True)
    # "aberto" (na fila / com alguém) | "concluido" (tratado por alguém) |
    # "encerrado" (saiu sozinho: a conta voltou a ficar em dia sem ninguém ter assumido)
    status = Column(String, nullable=False, default="aberto", index=True)
    aberto_em = Column(DateTime, default=datetime.utcnow, nullable=False)
    situacao_abertura = Column(String, nullable=True)   # "critico" | "atencao"
    motivo_abertura = Column(Text, nullable=True)        # ex.: "Reclamações 2,4% de 1,0%"
    metricas_abertura = Column(Text, nullable=True)      # JSON {chave: taxa em %} na hora em que entrou na fila

    # Quem está com a conta agora (vazio = livre).
    em_atendimento_por = Column(String, nullable=True)
    em_atendimento_por_id = Column(Integer, nullable=True, index=True)
    em_atendimento_desde = Column(DateTime, nullable=True)
    assumido_primeiro_em = Column(DateTime, nullable=True)

    concluido_por = Column(String, nullable=True)
    concluido_por_id = Column(Integer, nullable=True)
    concluido_em = Column(DateTime, nullable=True, index=True)
    conclusao = Column(Text, nullable=True)              # o que foi feito (obrigatório ao concluir)
    protocolo = Column(String, nullable=True)
    situacao_conclusao = Column(String, nullable=True)   # situação da conta no momento da conclusão
    encerrado_em = Column(DateTime, nullable=True)


class AtendimentoReputacaoEvento(Base):
    """
    Histórico de cada atendimento de reputação (só acrescenta, nunca altera):
    entrou_na_fila | reabriu | assumiu | assumiu_no_lugar | liberou | anotou |
    concluiu | saiu_da_fila. Base do "tempo com cada pessoa" e do histórico da tela.
    """

    __tablename__ = "atendimento_reputacao_eventos"

    id = Column(Integer, primary_key=True, index=True)
    atendimento_id = Column(Integer, ForeignKey("atendimentos_reputacao.id"), nullable=False, index=True)
    tipo = Column(String, nullable=False)
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
    papel = Column(String, nullable=False)  # "admin" | "supervisor" | "gerente_setor" | "operador" | "atendente" | "logistica" | "seller" | "suporte" | "tv"
    senha_hash = Column(String, nullable=True)  # nulo até o primeiro acesso ser concluído
    codigo_primeiro_acesso = Column(String, nullable=True)
    precisa_trocar_senha = Column(Boolean, default=True)
    ativo = Column(Boolean, default=True)

    # Bloqueio temporário por senha errada repetida (ver app/auth.py,
    # registrar_tentativa_falha/limpar_bloqueio_login, usado pelo login
    # do painel em main.py e da extensão em app/routers/cmx.py).
    # tentativas_login_falhas zera ao acertar a senha; ao bater o limite,
    # bloqueado_ate recebe "agora + N minutos" e o login fica recusado
    # até esse horário passar -- sem precisar de nenhum admin desbloquear
    # na mão (evita virar porta pra alguém travar de propósito o acesso
    # de outra pessoa só sabendo o usuário dela).
    tentativas_login_falhas = Column(Integer, default=0)
    bloqueado_ate = Column(DateTime, nullable=True)
    criado_em = Column(DateTime, default=datetime.utcnow)
    criado_por_usuario_id = Column(Integer, ForeignKey("usuarios.id"), nullable=True)

    # Só usado quando papel é "seller" ou "suporte" -- qual conta (ex:
    # "Velasco") esse usuário representa. É o que filtra o que ele vê em
    # /meus-cancelamentos, no painel (Vendas/Custos/Taxas) e pré-preenche
    # a conta no formulário público. "suporte" é o perfil interno que
    # testa/acompanha o painel usando a conta de um seller.
    conta_vinculada = Column(String, nullable=True)

    # Só usado quando papel é "operador" ou "gerente_setor" (15/10) -- quais
    # setores essa pessoa tem, texto simples separado por vírgula (ex.:
    # "cadastro,solicitacoes_cancelamento"; chaves em app/main.py:SETORES_DISPONIVEIS).
    # Substitui ter que inventar um papel novo pra cada combinação de telas:
    # pra abrir um setor novo no futuro, só se acrescenta mais uma opção aqui.
    # Nulo/vazio pra todos os outros papéis -- não muda nada de quem já existe.
    setores = Column(String, nullable=True)


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



class ConfigIA(Base):
    """
    Ajustes da nossa IA feitos na tela "Administração › Calibrar IA".
    Uma linha por ajuste (chave -> valor em JSON). Ajuste sem linha aqui usa
    o valor padrão de app/config_ia.py.
    """

    __tablename__ = "config_ia"

    chave = Column(String, primary_key=True)
    valor = Column(Text, nullable=False)  # JSON
    atualizado_por = Column(String, nullable=True)
    atualizado_em = Column(DateTime, default=datetime.utcnow)


class CustoSku(Base):
    """
    Custo de cada SKU, por conta -- usado pela extensão ClubMarketplaceX
    (calcula margem e "injeta" o custo na tela de Promoções do Mercado
    Livre) e pela futura tela "Produtos > Custos" do painel.

    Isolamento por conta é o ponto central aqui: um mesmo SKU pode
    existir em contas diferentes com custos diferentes, e uma conta
    NUNCA pode ler ou alterar o custo de outra -- toda consulta feita
    pelos endpoints em app/routers/cmx.py filtra por conta_id, nunca
    por sku sozinho. A restrição UNIQUE (conta_id, sku) garante que não
    existam duas linhas pro mesmo produto na mesma conta.
    """

    __tablename__ = "custos_sku"
    __table_args__ = (UniqueConstraint("conta_id", "sku", name="uq_custo_sku_conta_sku"),)

    id = Column(Integer, primary_key=True, index=True)
    conta_id = Column(Integer, ForeignKey("contas.id"), nullable=False, index=True)
    sku = Column(String, nullable=False, index=True)
    custo = Column(Float, nullable=False)
    nome_produto = Column(String, nullable=True)  # só informativo (vem da varredura ou da planilha)
    atualizado_em = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # Quem fez a última alteração: nome de exibição do usuário logado, ou
    # "planilha" quando veio de uma importação em lote sem usuário identificado
    # linha a linha (a importação em si sempre exige login).
    atualizado_por = Column(String, nullable=True)


class LogCustoSku(Base):
    """
    Histórico de toda alteração de custo por SKU -- criação, edição e
    remoção -- pra responder "quando e quem mudou esse custo" se algo
    der errado (custo errado que inflou/derrubou a margem, por exemplo).

    NUNCA é editado nem apagado por ninguém (nem pelo próprio sistema) --
    só recebe linhas novas, uma por alteração. `custo_anterior` nulo
    significa criação (SKU novo); `custo_novo` nulo significa remoção.

    Gravado em 2 pontos (ver app/custos_log.py:registrar_log_custo):
    app/routers/custos_painel.py (edição manual ou importação de
    planilha pelo painel) e app/routers/cmx.py (gravação vinda da
    extensão) -- `origem` diz qual dos dois foi.
    """

    __tablename__ = "log_custo_sku"

    id = Column(Integer, primary_key=True, index=True)
    conta_id = Column(Integer, ForeignKey("contas.id"), nullable=False, index=True)
    sku = Column(String, nullable=False, index=True)
    custo_anterior = Column(Float, nullable=True)
    custo_novo = Column(Float, nullable=True)
    nome_produto = Column(String, nullable=True)
    alterado_por = Column(String, nullable=True)
    origem = Column(String, nullable=False)  # "painel" | "extensao"
    criado_em = Column(DateTime, default=datetime.utcnow, index=True)


class EstoqueSku(Base):
    """
    Espelho em banco da ÚLTIMA quantidade conhecida de cada SKU, por
    conta, lida do Mercado Livre (CORREÇÃO 02/10 -- antes a tela
    "Produtos > Lista" consultava o Mercado Livre AO VIVO toda vez que
    abria, o que era lento com muitos anúncios e dependia da API estar
    disponível na hora).

    Preenchida por `ml_client.sincronizar_estoque_sku`, chamada: (a) na
    primeira vez que a conta abre a tela (sem cache ainda, só essa vez é
    lento), e (b) em segundo plano toda vez que a tela é aberta depois
    disso -- a tela sempre LÊ daqui (rápido), nunca espera o Mercado
    Livre responder.

    Isolamento por conta igual ao CustoSku: UNIQUE (conta_id, sku), toda
    consulta filtra por conta_id, nunca por sku sozinho.

    `ativo=False` quando o SKU não apareceu mais na última varredura
    (anúncio pausado/removido) -- mantemos o registro (em vez de
    apagar) só pra não perder o histórico de custo/nome associado, mas
    ele some da tela como se tivesse zerado o estoque.
    """

    __tablename__ = "estoque_sku"
    __table_args__ = (UniqueConstraint("conta_id", "sku", name="uq_estoque_sku_conta_sku"),)

    id = Column(Integer, primary_key=True, index=True)
    conta_id = Column(Integer, ForeignKey("contas.id"), nullable=False, index=True)
    sku = Column(String, nullable=False, index=True)
    quantidade = Column(Integer, nullable=False, default=0)
    titulo = Column(String, nullable=True)
    ativo = Column(Boolean, nullable=False, default=True)
    atualizado_em = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class VariavelConta(Base):
    """
    "Taxas" (variáveis percentuais) de cada conta, usadas pra calcular a
    margem de lucro tanto no painel (página Vendas) quanto na extensão
    ClubMarketplaceX -- ex: "imposto" 2%, "club" 1%. Antes ficavam só no
    chrome.storage.local do computador de cada seller (perdia tudo se
    trocasse de PC, e cada instalação tinha o próprio valor sem ninguém
    no Club Marketplace conseguir ver ou ajustar). Agora seguem o mesmo
    modelo do CustoSku: isolado por conta_id, nunca por nome sozinho --
    toda consulta filtra pela conta do usuário logado (ver
    app/routers/cmx.py e app/routers/taxas.py).

    Por enquanto só suporta percentual. Valor fixo (ex: "contador
    R$500/mês") foi deixado de fora por decisão do cliente -- pode
    entrar depois como campo/tabela adicional, sem quebrar o que existe.

    base_calculo indica sobre QUAL valor o percentual é aplicado:
      - "venda_bruta": sobre o preço final de venda (comportamento
        antigo, único que existia antes deste campo -- por isso é o
        default, pra não mudar o cálculo de quem já tinha taxas
        cadastradas).
      - "repasse": sobre o valor que o Mercado Livre repassa ao seller
        (já líquido de tarifa ML + frete).
      - "lucro": sobre o lucro (repasse - custo - taxas de venda_bruta e
        repasse já deduzidas) -- é a última dedução antes do líquido.
    """

    __tablename__ = "variaveis_conta"
    __table_args__ = (UniqueConstraint("conta_id", "nome", name="uq_variavel_conta_nome"),)

    id = Column(Integer, primary_key=True, index=True)
    conta_id = Column(Integer, ForeignKey("contas.id"), nullable=False, index=True)
    nome = Column(String, nullable=False)
    percentual = Column(Float, nullable=False)
    base_calculo = Column(String, nullable=False, default="venda_bruta")
    atualizado_em = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    atualizado_por = Column(String, nullable=True)


class EventoAdesaoCmx(Base):
    """
    Cópia no SERVIDOR de cada adesão a promoção feita pela extensão
    ClubMarketplaceX -- a extensão continua gravando a cópia LOCAL dela
    (chrome.storage.local.cmxHistoricoAdesoes, inalterada) pra manter a
    tela "Ver histórico de adesões" rápida e funcionando sem depender de
    rede; esta tabela é uma SEGUNDA cópia, assíncrona, pra servir à
    página do painel ("Produtos > Histórico de adesões"), com 3
    vantagens que o armazenamento local não dá: (1) sobrevive a
    desinstalar/reinstalar a extensão ou trocar de PC; (2) junta os
    registros de VÁRIOS logins/computadores usando a mesma conta (cada
    linha grava `usuario`, o nome de quem fez); (3) pesquisável no
    servidor sem precisar abrir a extensão.

    IDEMPOTÊNCIA: `id_envio` é gerado pela extensão (crypto.randomUUID())
    no momento em que a adesão é registrada localmente, e reenviado
    IGUAL em toda tentativa de retry (fila local em
    chrome.storage.local.cmxFilaEnvioHistorico) -- a gravação em lote
    (app/routers/cmx.py) usa esse campo pra nunca duplicar a mesma
    adesão quando a extensão reenviar por falha de rede ou reinício do
    service worker.

    ESCALA (100+ contas): toda consulta da página do painel filtra por
    conta_id E por intervalo de data (default: últimos 10 dias), nunca a
    tabela inteira -- por isso o índice composto (conta_id, data_adesao)
    abaixo, que é exatamente o par usado nessa consulta. Sem ele, cada
    busca precisaria varrer a tabela inteira (todas as contas, todo o
    histórico) pra achar as linhas de uma conta num período -- rápido
    com poucas contas, lento conforme o total de linhas crescer.
    """

    __tablename__ = "eventos_adesao_cmx"
    __table_args__ = (
        UniqueConstraint("id_envio", name="uq_evento_adesao_cmx_id_envio"),
        Index("ix_evento_adesao_cmx_conta_data", "conta_id", "data_adesao"),
    )

    id = Column(Integer, primary_key=True, index=True)
    # Gerado pela extensão (UUID) -- é o que garante que reenviar o mesmo
    # registro (retry) nunca cria uma segunda linha.
    id_envio = Column(String, nullable=False, index=True)
    conta_id = Column(Integer, ForeignKey("contas.id"), nullable=False, index=True)
    # Quem fez a adesão -- nome de exibição do login da extensão (não o
    # dono da conta: numa conta com 2 PCs/usuários, cada um aparece
    # separado aqui, embora a conta/custos/taxas sejam as mesmas).
    usuario = Column(String, nullable=True)
    maquina = Column(String, nullable=True)  # apelido opcional do PC/pessoa (cmxApelidoMaquina), só informativo
    item_id = Column(String, nullable=False, index=True)
    sku = Column(String, nullable=True)
    nome_anuncio = Column(String, nullable=True)
    tipo_promocao = Column(String, nullable=True)
    preco_final = Column(Float, nullable=True)
    voce_recebe = Column(Float, nullable=True)
    custo = Column(Float, nullable=True)
    lucro_liquido = Column(Float, nullable=True)
    sucesso = Column(Boolean, nullable=False)
    motivo = Column(Text, nullable=True)  # só preenchido quando sucesso=False
    data_adesao = Column(DateTime, nullable=False, index=True)  # quando a adesão aconteceu (não quando chegou ao servidor)
    criado_em = Column(DateTime, default=datetime.utcnow)  # quando ESTA linha foi gravada no servidor (diagnóstico de atraso de fila)


class HistoricoConfigIA(Base):
    """Quem mudou qual ajuste da IA, de quê para quê e quando."""

    __tablename__ = "historico_config_ia"

    id = Column(Integer, primary_key=True, index=True)
    chave = Column(String, nullable=False)
    valor_antigo = Column(Text, nullable=True)
    valor_novo = Column(Text, nullable=True)
    alterado_por = Column(String, nullable=True)
    alterado_em = Column(DateTime, default=datetime.utcnow, index=True)


class EventoIA(Base):
    """
    Registro de cada chamada à API do Claude feita pela nossa IA de pré-venda:
    "falha" (sem crédito, limite, rede...) ou "sucesso". Alimenta o aviso
    "IA sem crédito/falhando" na tela Administração › Calibrar IA -- sem isso,
    uma falha na API só aparecia nos logs do servidor, e as perguntas iam
    pra fila humana silenciosamente, sem ninguém perceber o motivo.
    """

    __tablename__ = "eventos_ia"

    id = Column(Integer, primary_key=True, index=True)
    tipo = Column(String, nullable=False)  # "falha" | "sucesso"
    motivo = Column(String, nullable=True)  # resumo do erro (só quando tipo="falha")
    ocorrido_em = Column(DateTime, default=datetime.utcnow, index=True)
    # 18/10: termômetro de crédito -- consumo de cada chamada "sucesso",
    # pra estimar o custo localmente (ver app/uso_ia_custo.py). NULL em
    # eventos antigos e em "falha" (a chamada que falhou não gastou tokens).
    modelo = Column(String, nullable=True)
    tokens_entrada = Column(Integer, nullable=True)
    tokens_saida = Column(Integer, nullable=True)
    buscas_web = Column(Integer, nullable=True)


class Venda(Base):
    """
    Uma linha de venda já CONCRETIZADA no Mercado Livre -- um item de um
    pedido real (não confundir com `CustoSku`/promoção, que é antes da
    venda acontecer). Alimenta a tela "Vendas" do painel, equivalente à
    Central Financeira (Google Apps Script) só que dentro do nosso
    próprio sistema, já cruzando com o custo cadastrado (CustoSku) pra
    mostrar lucro e margem venda a venda, em tempo real.

    Preenchida principalmente pelo webhook 'orders_v2' (ver
    app/routers/webhook_ml.py) assim que o Mercado Livre avisa um
    pedido novo/atualizado, e também por uma sincronização manual de
    reforço (app/ml_client.py:sincronizar_vendas_recentes) pra pegar
    pedidos de antes dessa função existir ou caso algum webhook se
    perca. UNIQUE (conta_id, ml_order_id, item_id) pra um reprocessamento
    (reenvio do Mercado Livre, ou a sincronização manual) atualizar a
    mesma linha em vez de duplicar.

    `custo_total`/`lucro`/`margem_percentual` ficam nulos quando o SKU
    vendido ainda não tem custo cadastrado -- a tela mostra esses casos
    separadamente, igual "Produtos > Lista" já faz com "Sem custo
    cadastrado".
    """

    __tablename__ = "vendas"
    __table_args__ = (
        UniqueConstraint("conta_id", "ml_order_id", "item_id", name="uq_venda_conta_pedido_item"),
    )

    id = Column(Integer, primary_key=True, index=True)
    conta_id = Column(Integer, ForeignKey("contas.id"), nullable=False, index=True)
    ml_order_id = Column(String, nullable=False, index=True)
    item_id = Column(String, nullable=False)
    sku = Column(String, nullable=True, index=True)
    titulo = Column(String, nullable=True)
    quantidade = Column(Integer, nullable=False, default=1)
    preco_unitario = Column(Float, nullable=False, default=0.0)
    venda_bruta = Column(Float, nullable=False, default=0.0)  # preco_unitario * quantidade
    taxa_ml = Column(Float, nullable=False, default=0.0)  # comissão/sale_fee do Mercado Livre
    frete = Column(Float, nullable=False, default=0.0)  # custo de frete descontado do vendedor
    repasse = Column(Float, nullable=False, default=0.0)  # venda_bruta - taxa_ml - frete (o que o ML repassa)
    custo_total = Column(Float, nullable=True)  # CMV desta linha (custo cadastrado x quantidade); nulo = sem custo
    lucro = Column(Float, nullable=True)  # repasse - custo_total; nulo se custo_total é nulo
    margem_percentual = Column(Float, nullable=True)  # lucro / venda_bruta * 100; nulo se custo_total é nulo
    status_pedido = Column(String, nullable=True)  # "paid", "cancelled", etc. (vem direto do Mercado Livre)
    # Situação do pagamento do pedido ("approved", "refunded", "charged_back"...).
    # Um pedido devolvido continua "paid" no status do pedido -- o que muda é
    # o pagamento, que vira "refunded". Guardado aqui pra tela de Vendas
    # deixar devolução/estorno fora dos totais, igual o próprio ML faz.
    pagamento_status = Column(String, nullable=True)
    # Carrinho do Mercado Livre: uma compra com vários itens vira um pedido
    # por item, todos com o mesmo pack_id. O ML conta o carrinho como UMA
    # venda -- o resumo da tela de Vendas usa isso pra contar igual.
    pack_id = Column(String, nullable=True)
    data_venda = Column(DateTime, nullable=False, index=True)  # date_created do pedido, em UTC
    atualizado_em = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DespesaFixaConta(Base):
    """
    Despesa fixa mensal configurada pela conta pra entrar no cálculo do
    "Lucro Líquido" da tela Vendas -- ex: "Contabilidade" R$500/mês,
    "Bling" R$185/mês. Diferente de `VariavelConta` (que é percentual,
    cobrado em cima do valor de cada venda, tipo imposto/CLUB) -- aqui é
    sempre um valor fixo por mês, e a tela rateia proporcionalmente pelos
    dias do período filtrado (ex: 7 dias de um mês de 30 -> 7/30 do
    valor mensal) em vez de aplicar o mês inteiro num filtro de um dia só.

    Configurada só pelo painel (não tem equivalente na extensão) --
    isolada por conta_id, igual todo o resto.
    """

    __tablename__ = "despesas_fixas_conta"
    __table_args__ = (UniqueConstraint("conta_id", "nome", name="uq_despesa_fixa_conta_nome"),)

    id = Column(Integer, primary_key=True, index=True)
    conta_id = Column(Integer, ForeignKey("contas.id"), nullable=False, index=True)
    nome = Column(String, nullable=False)
    valor_mensal = Column(Float, nullable=False)
    atualizado_em = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    atualizado_por = Column(String, nullable=True)


class ProdutoCadastro(Base):
    """
    Produto NOVO que apareceu numa nota fiscal processada pelo Operador
    Logístico (Club Full, Apps Script) e está esperando CADASTRO. É a mesma
    informação da mensagem de Zap "PRODUTO(S) NOVO(S) PARA CADASTRO": empresa,
    fornecedor, nota e, por produto, SKU, nome, EAN, quantidade e custo
    unitário (o "Custo un." do Zap -- custo final, com frete e IPI rateados
    quando a nota tem).

    Chega por POST /api/operador/produtos-novos (ver
    app/routers/cadastro_produtos.py), enviado sozinho pelo Operador no
    momento em que a nota é registrada. Só notas lançadas DEPOIS da
    integração entrar no ar; nada do passado é importado. A tela "Cadastro"
    do painel lista isso agrupado por empresa e deixa marcar o que já foi
    cadastrado.

    Chave: (nota_id, sku) -- reenviar o mesmo produto da mesma nota ATUALIZA
    em vez de duplicar, e nunca desfaz um "cadastrado". `nota_id` é o ID que
    o próprio Operador gerou pra nota.

    O custo por empresa de TODOS os produtos (2ª rodada) NÃO está aqui de
    propósito: vem depois, separado.
    """

    __tablename__ = "produtos_cadastro"
    __table_args__ = (UniqueConstraint("nota_id", "sku", name="uq_produto_cadastro_nota_sku"),)

    id = Column(Integer, primary_key=True, index=True)
    nota_id = Column(String, nullable=False, index=True)  # ID da nota no Operador Logístico
    empresa_nome = Column(String, nullable=False)  # como veio do Operador (ex: "JC MACHADO")
    empresa_chave = Column(String, nullable=False, index=True)  # chave_conta(empresa_nome): minúsculo, sem acento
    fornecedor = Column(String, nullable=True)
    numero_nota = Column(String, nullable=True)
    data_nota = Column(DateTime, nullable=True)  # quando o Operador registrou a nota (UTC)
    sku = Column(String, nullable=False, index=True)
    produto = Column(String, nullable=True)
    gtin = Column(String, nullable=True)  # EAN / código de barras
    quantidade = Column(Float, nullable=False, default=0)
    custo_unitario = Column(Float, nullable=False, default=0)
    status = Column(String, nullable=False, default="pendente", index=True)  # "pendente" | "cadastrado"
    cadastrado_por = Column(String, nullable=True)
    cadastrado_em = Column(DateTime, nullable=True)
    recebido_em = Column(DateTime, default=datetime.utcnow)
    atualizado_em = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # --- 14/10: quem cadastrou, em qual marketplace ---------------------------
    # "automatico" = chegou do Operador; "manual" = incluído na tela por alguém
    # do time (criado_por guarda quem).
    origem = Column(String, nullable=True, default="automatico")
    criado_por = Column(String, nullable=True)
    # Uma marcação por marketplace: nulo = ainda falta; "feito" = cadastrado lá;
    # "nao_se_aplica" = esse produto não vai pra esse marketplace (conta como
    # resolvido, pra o produto poder ficar OK só com 2). *_por/*_em = quem e quando.
    ml_status = Column(String, nullable=True)
    ml_por = Column(String, nullable=True)
    ml_em = Column(DateTime, nullable=True)
    tiktok_status = Column(String, nullable=True)
    tiktok_por = Column(String, nullable=True)
    tiktok_em = Column(DateTime, nullable=True)
    shopee_status = Column(String, nullable=True)
    shopee_por = Column(String, nullable=True)
    shopee_em = Column(DateTime, nullable=True)

    # --- 14/10: "refazer cadastro" -------------------------------------------
    # Um cadastro que precisa ser refeito entra como solicitação NOVA (inclusão
    # manual), apenas marcada como "refazer": serve pra todo mundo saber que o
    # produto já foi cadastrado antes e está sendo refeito. Sem ligação com o anterior.
    refazer = Column(Boolean, nullable=True, default=False)

    # --- 14/10: quem ASSUMIU o cadastro e desde quando -----------------------
    # Dá pra saber quem está cuidando de cada produto e quanto tempo levou
    # (assumido_em -> cadastrado_em). Preenche ao clicar "Assumir" ou sozinho
    # quando alguém marca o primeiro canal e ninguém tinha assumido.
    assumido_por = Column(String, nullable=True)
    assumido_em = Column(DateTime, nullable=True)

    # --- 14/10: trava + reabertura --------------------------------------------
    # Produto "cadastrado" (os 3 marketplaces resolvidos) fica TRAVADO: ninguém
    # altera mais pelas rotas normais (ver marcar_marketplace/alterar_status_cadastro
    # em cadastro_produtos.py). A única forma de voltar a mexer é um admin/
    # supervisor reabrir pela rota /reabrir, informando o motivo -- fica
    # registrado aqui quem reabriu, quando e por quê (auditoria).
    reaberto_por = Column(String, nullable=True)
    reaberto_em = Column(DateTime, nullable=True)
    motivo_reabertura = Column(Text, nullable=True)

    # --- 15/10: "é kit?" -------------------------------------------------------
    # Marcado só na tela de solicitação manual (uma marcação só por solicitação,
    # vale pra todos os produtos dela). Produto automático do Operador fica NULL
    # -- essa marcação não existe pra ele.
    kit = Column(Boolean, nullable=True)

    # --- 15/10: exclusão (admin/supervisor) -------------------------------------
    # Excluir NÃO apaga a linha -- só marca status="excluida" e guarda quem, quando
    # e por quê (motivo obrigatório). Some de todas as listas da tela, mas fica no
    # banco pra auditoria (mesma lógica da reabertura acima).
    excluido_por = Column(String, nullable=True)
    excluido_em = Column(DateTime, nullable=True)
    motivo_exclusao = Column(Text, nullable=True)

    # --- 16/10: link das fotos (Drive do cliente, etc.) -------------------------
    # Opcional, por produto -- preenchido na hora de solicitar o cadastro (tela da
    # equipe ou /cadastro-seller), pra quem for cadastrar já abrir a pasta certa.
    link_fotos = Column(String, nullable=True)
