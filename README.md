# back.scraper

Motor MVP de atualizacao da base tributaria da Tricomplex.

Escopo atual:

- UF: SP
- NCMs: `22011000` agua, `22021000` refrigerante, `20099000` suco
- Tributos: ICMS, DIFAL, PIS, COFINS, IPI, ICMS-ST

## Arquivos principais

- `fontes_mvp.json`: escopo fiscal e fontes oficiais iniciais.
- `scraping.py`: scraper/crawler HTML generico.
- `limpador_dados.py`: extrai contexto ao redor de aliquotas e termos fiscais.
- `llm_interpreter.py`: envia texto para LLM e valida o JSON fiscal retornado.
- `xlsx_extractor.py`: baixa fontes XLSX oficiais, como TIPI, e extrai linhas dos NCMs do MVP.
- `candidate_builder.py`: transforma fontes deterministicas em candidatos estruturados para revisao/banco.
- `db_integrator.py`: cria/recupera registros e insere regras no MySQL.
- `pipeline.py`: orquestra scraping, limpeza, LLM e banco.

## Setup

```bash
pip install -r requirements.txt
```

Variaveis opcionais:

```bash
LLM_PROVIDER=openai # openai ou gemini
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-4.1-mini
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-2.0-flash

DB_HOST=localhost
DB_PORT=3306
DB_USER=root
DB_PASSWORD=
DB_NAME=tricomplex
```

## Rodar somente scraping + limpeza

```bash
python pipeline.py --no-llm
```

Os artefatos ficam em `artifacts/<timestamp>/`:

- `*_scraped.json`: pagina bruta estruturada.
- `*_contexts.json`: trechos relevantes perto de aliquotas/termos.
- `*_relevant_contexts.json`: trechos filtrados por NCM, apelidos, tributos e palavras-chave.
- `*_structured_candidates.json`: candidatos estruturados, com `ready_for_db` ou `needs_review`.
- `structured_candidates_all.json`: todos os candidatos estruturados da rodada em um unico arquivo.
- `summary.json`: resumo da rodada.

## Rodar com LLM sem gravar no banco

```bash
python pipeline.py
```

Esse modo interpreta `structured_candidates_all.json` em um lote final de interpretacoes para banco, mantendo o contrato interno `{source, rules}` por fonte. A LLM deve retornar JSON puro validado por `llm_interpreter.py`. Use `LLM_PROVIDER=openai` com `OPENAI_API_KEY` ou `LLM_PROVIDER=gemini` com `GEMINI_API_KEY`.

Para reaproveitar uma rodada ja gerada:

```bash
python pipeline.py --candidates-file artifacts/20260517_175409/structured_candidates_all.json
```

O resultado fica em `db_interpretations_from_candidates.json`. A LLM nao gera SQL; candidatos inseguros entram em `review_items`.

Se o provider LLM estiver sem quota, da para rodar o filtro conservador sem LLM:

```bash
python pipeline.py --candidates-file artifacts/20260517_175409/structured_candidates_all.json --no-llm --apply-db
```

Esse modo so promove candidatos `ready_for_db` com `confidence=ALTA`, evidencia e `vigencia_inicio`; o resto segue para revisao.

## Rodar com LLM e testar inserts no banco

```bash
python pipeline.py --apply-db
```

Por padrao, esse comando abre transacao e faz rollback no final. Ele serve para validar conexao, IDs e inserts sem alterar a base.

## Gravar de verdade no banco

```bash
python pipeline.py --apply-db --commit
```

O integrador nao executa SQL gerado pela LLM. Ele valida o JSON, resolve/cadastra `tributos`, `jurisdicoes`, `produtos_fiscais`, `fontes_legais` e so entao insere linhas em `regras_tributarias`.
