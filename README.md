# Buscador — Monitor de Preços & Mini-SAD (Processos Decisórios e SAD)

Sistema completo de coleta automatizada de preços e **Mini-SAD (Sistema de Apoio à Decisão)** composto por:
- **Bot no Discord** (`discord.py`) com comandos slash e job agendado (global ou individual, em minutos, horas, dias ou semanas).
- **Extrator Universal Multi-Camadas** (`scraper.py`) compatível com múltiplas lojas online (incluindo API do Mercado Livre, `JSON-LD`, `__NEXT_DATA__`, importação de até **40 dias de histórico real** do Buscapé/Zoom, Meta tags, scripts embutidos e fallback JS via Jina Reader / Scrape.do).
- **Banco de Dados SQLite** (`data/buscador.db`) armazenando até 5 produtos simultâneos e toda a série histórica de preços.
- **Motor Mini-SAD** (`sad.py`) com indicadores estatísticos, classificação multicritério de oportunidade, recomendação justificada, ranking comparativo e simulação **What-If** de preço-alvo.
- **Dashboard Web Interativo** (`web/` — HTML, CSS, JavaScript + Chart.js) servido localmente pelo bot (`http://localhost:8080`) e pronto para deploy na **Vercel**.

---

## 1. Estrutura do Projeto

```text
Buscador/
├── main.py                # Bot Discord (comandos slash + agendador automático)
├── sad.py                 # Motor do Mini-SAD (estatísticas, score multicritério, regras e What-If)
├── scraper.py             # Coletor universal multi-camadas + histórico real Buscapé/Zoom
├── database.py            # Camada SQLite (produtos, histórico, agendamentos e filtro por data)
├── api_server.py          # API REST (aiohttp) com CORS + servidor estático do dashboard
├── config.py              # Carregamento de variáveis de ambiente (.env)
├── gerar_zip_entrega.py   # Script auxiliar para gerar o ZIP final da entrega (sem expor .env)
├── requirements.txt       # Dependências Python
├── discloud.config        # Configuração para deploy na Discloud
├── .env.example           # Modelo de variáveis de ambiente
├── dados_coletados.csv    # Exportação tabular dos dados coletados (gerado pelo /exportar ou script)
├── data/
│   └── buscador.db        # Banco SQLite com os dados coletados
└── web/
    ├── index.html         # Dashboard interativo + painel Mini-SAD
    ├── styles.css         # Estilização responsiva (tema escuro)
    ├── app.js             # Lógica do dashboard, filtros de data, gráficos e What-If
    ├── config.js          # Configuração da URL da API (para deploy na Vercel)
    └── vercel.json        # Configuração de publicação estática na Vercel
```

---

## 2. Como funciona o Mini-SAD (`sad.py`)

A partir da série histórica coletada em diferentes dias (filtrável a partir de qualquer data até hoje ou desde a data de adição), o Mini-SAD calcula:

1. **Indicadores Estatísticos:** Preço Atual, Mínimo, Máximo, Média, Desvio Padrão, Amplitude, Volatilidade (%), Variação no Período (%), Posição na Faixa (0% = mínima, 100% = máxima) e **Tendência** (via inclinação de regressão linear simples).
2. **Score Multicritério de Oportunidade (0 a 100 pontos):**
   - **45%** — Desconto relativo à média histórica do período.
   - **35%** — Proximidade do menor preço registrado.
   - **20%** — Direção da tendência recente.
3. **Regras Explícitas de Recomendação:**
   - 🟢 **COMPRAR AGORA:** preço atual $\le$ média $- 2\%$, ou nos $25\%$ inferiores da faixa, ou score $\ge 65$.
   - 🟡 **AGUARDAR QUEDA:** preço atual $\ge$ média $+ 2{,}5\%$, ou nos $30\%$ superiores da faixa, ou tendência de queda ainda acima da mínima.
   - 🔵 **MONITORAR:** preço próximo da média histórica (zona neutra).
4. **Análise What-If (Sensibilidade):** o usuário informa um preço-alvo desejado e o sistema calcula a distância em valor e %, verificando se a meta já foi atingida ou se é realista frente à mínima histórica.
5. **Ranking Comparativo:** ordena todos os produtos monitorados pela melhor oportunidade de compra.

---

## 3. Como Executar o Projeto Localmente

### Pré-requisitos
- **Python 3.10+** instalado
- Um aplicativo/bot criado no [Discord Developer Portal](https://discord.com/developers/applications)

### Passo a passo
1. Crie e ative um ambiente virtual e instale as dependências:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\pip.exe install -r requirements.txt
   ```
2. Copie `.env.example` para `.env` e preencha o `DISCORD_TOKEN` do seu bot:
   ```env
   DISCORD_TOKEN=SEU_TOKEN_DO_DISCORD
   CHECK_INTERVAL_HOURS=6
   MAX_LINKS=5
   WEB_URL=http://localhost:8080
   API_PORT=8080
   ```
3. Inicie o bot e a API do Dashboard:
   ```powershell
   .\.venv\Scripts\python.exe main.py
   ```
4. Acesse o **Dashboard Web + Mini-SAD** no navegador em:
   - **`http://localhost:8080`**

*(Observação: mesmo sem configurar um token do Discord, os dados históricos já coletados podem ser inspecionados em `dados_coletados.csv` e `data/buscador.db`.)*

---

## 4. Comandos no Discord

| Comando | Descrição |
| :--- | :--- |
| `/tutorial` | Guia completo dos comandos do bot. |
| `/adicionar <link> [apelido]` | Adiciona um produto (máx. 5 links). Links do Buscapé/Zoom importam até 40 dias reais automaticamente. |
| `/listar` | Lista todos os links salvos, último preço e frequência de varredura. |
| `/agendar <escopo> <valor> <unidade> [produto_id]` | Configura varredura **Global** ou **Individual** em **minutos, horas, dias ou semanas** (mín. 5 min). |
| `/verificar [id]` | Força a coleta imediata de 1 produto ou de todos. |
| `/historico <id> [data_inicio] [todo_historico]` | Mostra histórico **a partir de uma data até hoje** ou desde a **data de adição**, com resumo do Mini-SAD. |
| `/sad [id] [data_inicio] [preco_alvo]` | Executa o **Mini-SAD** (recomendação, score, tendência, What-If e ranking comparativo). |
| `/exportar [baixar_csv]` | Envia o link clicável do Painel Web (e opcionalmente anexa o arquivo CSV). |
| `/remover <id>` | Remove um produto e libera uma vaga. |

---

## 5. Deploy (Vercel + Discloud)

- **Vercel (Frontend):** publique a pasta `web/` na Vercel e aponte `API_BASE_URL` em `web/config.js` (ou no botão ⚙️ do topo do site) para a URL pública da API do bot; atualize `WEB_URL` no `.env` com o link da Vercel.
- **Discloud (Bot):** compacte os arquivos Python, `requirements.txt`, `discloud.config`, `.env` e `web/` (sem `.venv/`, `__pycache__/` nem `data/`). Para expor a porta `8080` publicamente na Discloud, utilize `TYPE=site`, `RAM=512` e `ID=seu-subdominio` no `discloud.config` (plano Platinum+).
