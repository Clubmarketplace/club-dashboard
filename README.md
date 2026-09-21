# Club Marketplace — Painel (esqueleto inicial)

Backend em Python (FastAPI) + painel de devoluções, rodando com dados de
EXEMPLO por enquanto — ainda sem conexão real com a conta Velasco.

## Como rodar localmente (com dados de exemplo)

1. Crie um ambiente virtual e instale as dependências:

   ```
   python3 -m venv venv
   source venv/bin/activate       # no Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. Gere os dados de exemplo (cria um banco SQLite local, sem precisar
   instalar Postgres agora):

   ```
   python -m app.seed_data
   ```

3. Suba o servidor:

   ```
   uvicorn app.main:app --reload
   ```

4. Abra no navegador: http://localhost:8000

## O que já está pronto

- Modelo de banco de dados: contas, devoluções, e log de ações (base do
  relatório por conta).
- API de devoluções: resumo (KPIs), por motivo, tendência mensal, lista
  detalhada.
- API de relatório por conta: o que o sistema fez (perguntas
  respondidas, pedidos cancelados, promoções aderidas).
- Painel visual com KPIs, dois gráficos (tendência mensal e motivos) e
  tabela detalhada, com filtro por conta e por período.

## O que falta pra virar produção de verdade

1. **Domínio com HTTPS** — o Mercado Livre exige isso pro redirect de
   autorização (OAuth) e pro recebimento de notificações (webhook). Não
   aceita IP puro.
2. **Fluxo de autorização OAuth real** — hoje o sistema só lê contas já
   cadastradas no banco; falta implementar as rotas que geram o link de
   autorização, recebem o retorno do Mercado Livre, e trocam o código
   pelo access_token/refresh_token de verdade.
3. **Integração real com a API de devoluções do Mercado Livre** — hoje
   os dados vêm do `seed_data.py` (exemplo); falta o código que consulta
   `/post-purchase/v1/claims/search`, `/post-purchase/v2/claims/{id}/returns`
   e `/post-purchase/v1/claims/{id}/charges/return-cost` de verdade, e
   grava no banco.
4. **Processamento assíncrono** — pra não travar quando muitas contas
   mandarem notificação ao mesmo tempo, o ideal é usar uma fila de
   tarefas (ex: Celery) entre o recebimento do webhook e o
   processamento de fato.
5. **Troca de SQLite por Postgres de verdade** — antes de ligar contas
   reais, defina `DATABASE_URL` no `.env` apontando pra um servidor
   Postgres (ver `.env.example`).
6. **Hospedagem** — escolher onde subir (Railway/Render pra começar,
   ou AWS se for escalar direto pras ~100+ contas).

## Estrutura de pastas

```
club-dashboard/
├── app/
│   ├── main.py              → arquivo principal, junta tudo
│   ├── database.py          → conexão com o banco
│   ├── models.py            → tabelas (Conta, Devolucao, AcaoRegistrada)
│   ├── seed_data.py         → gera dados de exemplo
│   ├── routers/
│   │   ├── devolucoes.py    → API de devoluções
│   │   └── relatorio_conta.py → API de relatório por conta
│   ├── templates/
│   │   └── dashboard.html   → página do painel
│   └── static/
│       ├── dashboard.css
│       └── dashboard.js
├── requirements.txt
└── .env.example
```
