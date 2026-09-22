"""
Configuração central do sistema, carregada a partir do .env.

Mantendo isso num módulo só, qualquer parte do sistema que precisar
dessas variáveis importa daqui — evita repetir os.getenv espalhado
pelo código e facilita trocar o .env (local -> ngrok -> produção)
sem mexer em nada além dele.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# Credenciais do app mestre, cadastradas no Mercado Livre Devs.
ML_CLIENT_ID = os.getenv("ML_CLIENT_ID", "")
ML_CLIENT_SECRET = os.getenv("ML_CLIENT_SECRET", "")

# Precisa bater EXATAMENTE com o Redirect URI cadastrado no Mercado
# Livre Devs (ex: https://SEU-NGROK.ngrok-free.dev/auth/callback).
ML_REDIRECT_URI = os.getenv("ML_REDIRECT_URI", "")

# URLs fixas da API do Mercado Livre (não mudam por conta).
ML_AUTH_BASE_URL = "https://auth.mercadolivre.com.br/authorization"
ML_TOKEN_URL = "https://api.mercadolibre.com/oauth/token"
ML_API_BASE_URL = "https://api.mercadolibre.com"

# Camada 2 da lógica de pré-venda (manual do SKU -> IA gera resposta).
# Sem essa chave configurada, o sistema simplesmente pula a camada 2 e
# cai pra fila humana -- não quebra nada, só não automatiza essa parte.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL_PRE_VENDA = os.getenv("CLAUDE_MODEL_PRE_VENDA", "claude-haiku-4-5-20251001")

# Onde os manuais técnicos (PDF ou texto) são lidos. "local" (padrão)
# lê de uma pasta no computador; "s3" lê de um bucket na AWS -- pra
# quando migrar, só troca essa variável e preenche MANUAIS_S3_BUCKET,
# nada mais no código muda (ver app/manuais_storage.py).
MANUAIS_STORAGE = os.getenv("MANUAIS_STORAGE", "local")
MANUAIS_LOCAL_DIR = os.getenv("MANUAIS_LOCAL_DIR", "manuais")
MANUAIS_S3_BUCKET = os.getenv("MANUAIS_S3_BUCKET", "")

# URL do Google Apps Script da Central Financeira -- usava ser a URL de
# notificação cadastrada direto no Mercado Livre Devs, mas como agora o
# mesmo app do Mercado Livre serve os dois projetos (Central Financeira
# + esse aqui), só pode existir UMA URL de notificação cadastrada lá.
# Então o Mercado Livre manda tudo pra ESSE backend agora, e esse backend
# repassa pra cá qualquer notificação que não seja de pergunta de
# pré-venda (ex: tópico "orders_v2"), pra Central Financeira continuar
# recebendo exatamente o que recebia antes -- ver app/routers/webhook_ml.py.
GOOGLE_APPS_SCRIPT_WEBHOOK_URL = os.getenv("GOOGLE_APPS_SCRIPT_WEBHOOK_URL", "")

# --- Login / sessão ---
# Chave usada pra assinar o cookie de sessão (impede que alguém forje
# um cookie válido sem conhecer essa chave). PRECISA ser definida no
# Railway (Variables) em produção -- se não for definida, o sistema
# gera uma aleatória a cada reinício, o que derruba todo mundo logado
# sempre que o serviço reiniciar. Gere uma vez com:
#   python3 -c "import secrets; print(secrets.token_hex(32))"
SESSION_SECRET_KEY = os.getenv("SESSION_SECRET_KEY", "")

# Usado só na primeira vez que o sistema sobe, pra criar o admin
# inicial (bootstrap) -- sem isso, ninguém consegue logar nunca, já
# que criar novos usuários exige estar logado como admin. Depois que
# esse admin existir no banco, essas duas variáveis não fazem mais
# nada (o sistema só cria se a tabela de usuários estiver vazia).
ADMIN_USUARIO_INICIAL = os.getenv("ADMIN_USUARIO_INICIAL", "")
ADMIN_SENHA_INICIAL = os.getenv("ADMIN_SENHA_INICIAL", "")
