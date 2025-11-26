## Visão Geral do projeto

**Banco Ágil** é uma aplicação de atendimento digital construída com Streamlit que simula o front de atendimento de um banco digital. O sistema é composto por agentes especializados que realizam tarefas específicas:

* **Agente de Triagem** — autentica o cliente e direciona para serviços (crédito, entrevista de crédito, câmbio).
* **Agente de Crédito** — consulta limite e processa pedidos de aumento de limite.
* **Agente de Entrevista de Crédito** — conduz uma entrevista financeira estruturada para recalcular o score e atualizar a base.
* **Agente de Câmbio** — consulta cotações via API externa.

O protótipo é modular e pensado para ser seguro (manipulação local de PII), resiliente a falhas de API e claro no diálogo com o usuário.

---

## Arquitetura do sistema

### Visão geral

O sistema segue uma arquitetura modular baseada em componentes (services) responsáveis por domínios específicos. A UI (Streamlit) comunica-se com o **TriageAgent**, que orquestra os demais agentes e mantém o estado da sessão.

```
Streamlit (app.py)
    ↕
TriageAgent (services/triage_agent.py)
    ↙         ↓         ↘
CreditAgent  ExchangeAgent  InterviewAgent
    ↘           ↘             ↘
   CSVs (src/csvs/*.csv)   ClientRepository (data/client_repository.py)
```

### Agentes e responsabilidades

* **TriageAgent**

  * Conduz todo o fluxo conversacional: saudação → coleta CPF → coleta data de nascimento → autenticação → roteamento para ação.
  * Mantém estado da conversa (`state`, `last_action`, flags) no servidor (via `st.session_state`).
  * Garante regras gerais (máximo de tentativas, tratamento de encerrar conversa, proteção PII).

* **CreditAgent**

  * `consulta_limite(cpf)` — busca limite e score no `clientes.csv`.
  * `solicitar_aumento(cpf, novo_limite)` — cria registro na `solicitacoes_aumento_limite.csv` e decide aprovar/rejeitar com base na tabela `score_limite.csv`.

* **InterviewAgent**

  * Conduz entrevista estruturada (renda, emprego, despesas, dependentes, dívidas).
  * Calcula novo score com fórmula ponderada e atualiza `clientes.csv` (escrita atômica).

* **ExchangeAgent**

  * Consulta cotações via API (configurada por `EXCHANGE_API_KEY`).
  * Trata erros da API e retorna mensagens amigáveis ao usuário.

* **ClientRepository**

  * Abstração para leitura/atualização de `clientes.csv`.

### Fluxos de dados

* Dados de clientes e limites são mantidos em CSVs locais (`src/csvs/`).
* Quando o usuário solicita aumento de limite, o pedido é gravado em `solicitacoes_aumento_limite.csv`.
* A entrevista atualiza a coluna `score` do `clientes.csv` de forma atômica (arquivo temporário → rename).

---

## Funcionalidades implementadas

* Autenticação por **CPF** e **data de nascimento** (DD/MM/AAAA) com até 3 tentativas.
* Fluxo conversacional guiado via Streamlit (`st.chat_*`).
* Consulta de limite de crédito.
* Solicitação de aumento de limite com decisão baseada no score do cliente (e registro em CSV).
* Entrevista de crédito estruturada, cálculo de novo score e atualização da base.
* Consulta de cotação de moedas via provedor externo, com tratamento robusto de erro (mensagem amigável: *"Serviço indisponível. Volte mais tarde."*).
* Estado de conversa persistido durante a sessão (usuário autenticado não é solicitado novamente).
* Validações e parsing de entradas (ex.: valores como `8 mil`, `8k`, `1.500,50`).
* Arquitetura modular, comentários técnicos neutros e mensagens ao usuário sem exposição de PII.

---

## Desafios enfrentados e como foram resolvidos

1. **Perda de estado / reautenticação indesejada**

   * *Problema:* ao alternar ações (ex.: crédito → câmbio), o fluxo às vezes pedia CPF novamente.
   * *Solução:* quando sessão está autenticada (`self.authenticated`) o `TriageAgent` força retorno ao `post_auth` em vez de reiniciar a autenticação; `last_action` mantém contexto para continuar operações.

2. **Falhas/erros na API de câmbio**

   * *Problema:* provedores retornam erros quando chave faltante/expirada.
   * *Solução:* ExchangeAgent valida presença da chave, trata `HTTP`/JSON errors e retorna texto amigável ao usuário. Logs guardam detalhes.


---

## Escolhas técnicas e justificativas

* **Streamlit** — rapidez para prototipação de interfaces de chat e sessão (ideal para demonstradores).
* **CSV (local)** — escolha simples para protótipo; facilita inspeção e versionamento. Em produção, migrar para banco (SQLite/Postgres).
* **Modularização por agentes** — separa responsabilidades, facilita testes e extensão (p.ex. adicionar agente de investimentos).
* **Uso limitado de LLM** — LLMs são usados (quando aplicável) para formulários de diálogo e mensagens.
* **apilayer / Exchange API** — provedor de cotações (padrão no código). Código é facilmente adaptável para outro provedor.
* **Logging e tratamento de erros** — cada agente captura exceções e responde ao usuário com mensagens amigáveis.

---

## Tutorial de execução e testes

### 1. Pré-requisitos

* Python 3.9+
* pip / venv
* (opcional) contas/keys:

  * `EXCHANGE_API_KEY` (para cotação) — opcional: sem ela a cotação indicará indisponibilidade.
  * `OPENAI_API_KEY` (se desejar usar recursos LLM) — opcional.

### 2. Instalação (Linux/macOS / Windows PowerShell)

```bash
# criar virtualenv
python -m venv .venv
# ativar
# Linux/macOS:
source .venv/bin/activate
# Windows (Powershell)
.venv\Scripts\Activate.ps1

# instalar dependências
pip install -r requirements.txt
```

### 3. Configurar chaves (opções)

* **.env** com `python-decouple`:

```
EXCHANGE_API_KEY="SUA_CHAVE_AQUI"
OPENAI_API_KEY="SUA_CHAVE_OPENAI"
```

* Ou variáveis de ambiente:

  * Linux/macOS:

    ```bash
    export EXCHANGE_API_KEY="SUA_CHAVE"
    export OPENAI_API_KEY="SUA_CHAVE_OPENAI"
    ```
  * Windows PowerShell:

    ```powershell
    setx EXCHANGE_API_KEY "SUA_CHAVE"
    setx OPENAI_API_KEY "SUA_CHAVE_OPENAI"
    ```
* Ou `~/.streamlit/secrets.toml`:

```toml
EXCHANGE_API_KEY = "SUA_CHAVE"
OPENAI_API_KEY = "SUA_CHAVE_OPENAI"
```

> **Atenção:** nunca compartilhe essas chaves em repositórios públicos.

### 4. CSVs iniciais

Crie `src/csvs/` com os arquivos a seguir (exemplos):

`src/csvs/clientes.csv`

```csv
cpf,nome,data_nascimento,limite,score
12345678901,Fernando Mesquita,07/07/1985,2000.00,650
12345678909,Ana Silva,02/01/2003,1500.00,420
```

`src/csvs/score_limite.csv`

```csv
min_score,max_allowed_limit
0,1000.0
300,5000.0
600,15000.0
```

`src/csvs/solicitacoes_aumento_limite.csv`

```csv
cpf_cliente,data_hora_solicitacao,limite_atual,novo_limite_solicitado,status_pedido
```

(Os agentes também criam esses arquivos se não existirem.)

### 5. Executar a aplicação

```bash
streamlit run src/app.py
```

### 6. Cenários de teste rápidos (manuais)

* **Autenticação válida**

  * Envie: `12345678901` → `07/07/1985`
  * Esperado: mensagem de autenticação com menu (1/2/3).

* **Consultar limite**

  * Escolha `1` → `consultar limite`
  * Esperado: exibe limite atual (ex.: `R$ 2000.00`) e pergunta se deseja mais algo.

* **Solicitar aumento (com parsing de número)**

  * Escolha `1` → `solicitar aumento` → envie `8 mil`
  * Esperado: registro em `solicitacoes_aumento_limite.csv` e resposta aprovada/rejeitada conforme score.

* **Entrevista de crédito**

  * Escolha `2` → responder renda/emprego/despesas/dependentes/dívidas conforme solicitado
  * Esperado: cálculo de novo score e atualização em `clientes.csv`, redirecionamento para análise de crédito.

* **Consultar cotação**

  * Escolha `3` → envie `USD para BRL` ou `EUR`
  * Esperado: `Cotação atual: 1 USD = 5.39 BRL` (ou mensagem de indisponibilidade se a API falhar).

---

## Estrutura organizada do código

```
src/
├─ app.py                      # front Streamlit
├─ config.py                   # caminhos e nomes de variáveis
├─ services/
│  ├─ ai_dialogue.py           # mensagens e fallback para diálogo
│  ├─ triage_agent.py          # orquestrador / estado de sessão
│  ├─ credit_agent.py          # lógica de crédito
│  ├─ interview_agent.py       # entrevista e cálculo de score
│  └─ exchange_agent.py        # consulta de cotação (API)
├─ data/
│  └─ client_repository.py     # leitura/atualização de clientes.csv
├─ utils/
│  └─ validators.py            # parsers de CPF/data/validações simples
└─ csvs/
   ├─ clientes.csv
   ├─ score_limite.csv
   └─ solicitacoes_aumento_limite.csv
```

**Responsabilidades**

* `app.py` — UI e sessão (`st.session_state`) — não contém regras de negócio.
* `triage_agent.py` — faz toda a orquestração e mantém o estado da conversa.
* `credit_agent.py` — implementa regras de negócio de crédito.
* `interview_agent.py` — conduz entrevista e atualiza base.
* `exchange_agent.py` — abstrai chamadas de cotações e tratamento de erros.
* `client_repository.py` — centraliza acesso aos dados do cliente.

---

## Boas práticas e próximos passos

* **Migrar CSV → Banco (SQLite/Postgres)** para concorrência, consultas e consistência.
* **Adicionar testes automatizados** (pytest) para os agentes (autenticação, cálculo de score, regra de aprovação/rejeição).
* **Adicionar logs estruturados** (rotacionados) para auditoria.
* **Implementar fila/worker** para processar solicitações de aumento.
* **Segurança**: armazenar chaves em secrets manager, restringir acesso aos arquivos CSV.

---

## Observações finais

* O projeto é um protótipo funcional, modular e pronto para ser estendido ou migrado para produção.
* Comentários e mensagens foram escritos em linguagem técnica neutra — sem indicação de autoria.
* Se desejar, posso:

  * gerar `setup.py` para criar os CSVs automaticamente,
  * adicionar botões rápidos na interface para reduzir ambiguidade,
  * criar testes unitários de exemplo (`tests/`),
  * preparar um `dockerfile` para empacotamento.

---

Se quiser o arquivo em outro formato ou que eu gere também um `setup.py` para inicializar os CSVs, me avise.
