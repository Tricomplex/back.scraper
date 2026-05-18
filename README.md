# back.scraper

Motor MVP de atualizacao da base tributaria do Tricomplex.

Este modulo coleta ou processa fontes oficiais, transforma trechos legais em candidatos estruturados, valida esses candidatos e popula o MySQL usado pelo `back/extractor`.

O scraper nao atende usuario final. Ele e um processo de manutencao da base tributaria.

## Papel na arquitetura

```text
fontes oficiais
  -> back/scraper
       -> scraping.py / xlsx_extractor.py
       -> limpador_dados.py
       -> candidate_builder.py
       -> llm_interpreter.py opcional
       -> db_integrator.py
  -> MySQL tributario
  -> back/extractor consulta regras
```

Para o pitch, este modulo pode ser rodado localmente de vez em quando, apontando para o MySQL em nuvem. Nao e obrigatorio deployar o scraper como servico web.

## Escopo do MVP

- UF: SP.
- NCMs:
  - `22011000`: agua
  - `22021000`: refrigerante
  - `20099000`: suco / mistura de sucos
  - `22030000`: cerveja
  - `84433233`: impressora
  - `84439923`: cartucho de impressora
  - `84439933`: toner de impressora
  - `48025610`: papel
  - `19059090`: alimento / panificacao / confeitaria
- Tributos:
  - ICMS
  - PIS
  - COFINS
  - IPI
  - DIFAL e ICMS-ST em revisao quando nao houver regra segura.

## Fontes iniciais

- RICMS/SP, artigos 52 a 56-C: aliquotas gerais de ICMS.
- TIPI XLSX da Receita Federal: IPI por NCM.
- Lei 10.637/2002: PIS.
- Lei 10.833/2003: COFINS.
- Portaria CAT 68/2019: substituicao tributaria, quando aplicavel.

## Arquivos principais

- `fontes_mvp.json`: escopo fiscal e fontes oficiais iniciais.
- `urls.txt`: URLs principais para scraping HTML.
- `scraping.py`: crawler/scraper HTML generico.
- `limpador_dados.py`: extrai contexto ao redor de aliquotas e termos fiscais.
- `xlsx_extractor.py`: processa fontes XLSX oficiais, como TIPI.
- `candidate_builder.py`: transforma fontes deterministicas em candidatos estruturados.
- `llm_interpreter.py`: chama LLM e valida JSON fiscal retornado.
- `db_integrator.py`: resolve IDs e grava regras no MySQL.
- `pipeline.py`: orquestra o fluxo completo.
- `.env.example`: exemplo de configuracao.

## Variaveis de ambiente

```text
LLM_PROVIDER=openai

OPENAI_API_KEY=
OPENAI_MODEL=gpt-4.1-mini
OPENAI_BASE_URL=https://api.openai.com/v1

GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.0-flash
GEMINI_BASE_URL=https://generativelanguage.googleapis.com/v1beta

DB_HOST=localhost
DB_PORT=3306
DB_USER=root
DB_PASSWORD=
DB_NAME=tricomplex
```

O provider de LLM e opcional dependendo do modo de execucao. O banco so e necessario quando usar `--apply-db`.

## Instalar

```bash
pip install -r requirements.txt
```

Crie `.env`:

```bash
copy .env.example .env
```

## Modos de execucao

### Scraping e limpeza sem LLM

```bash
python pipeline.py --no-llm
```

Gera artefatos em:

```text
artifacts/<timestamp>/
```

Artefatos comuns:

- `*_scraped.json`: pagina bruta estruturada.
- `*_contexts.json`: trechos perto de aliquotas/termos fiscais.
- `*_relevant_contexts.json`: trechos filtrados por NCM, apelidos, tributos e palavras-chave.
- `*_structured_candidates.json`: candidatos estruturados.
- `structured_candidates_all.json`: candidatos de toda a rodada.
- `summary.json`: resumo da execucao.

### Rodar com LLM sem gravar no banco

```bash
python pipeline.py
```

Esse modo interpreta candidatos em JSON fiscal estruturado. A LLM deve retornar JSON puro no contrato interno `{source, rules}`. O codigo valida antes de qualquer escrita.

### Reaproveitar candidatos ja gerados

```bash
python pipeline.py --candidates-file artifacts/20260517_175409/structured_candidates_all.json
```

Util para testar interpretacao sem refazer scraping.

### Validar banco em dry-run

```bash
python pipeline.py --apply-db
```

Por padrao, esse modo nao grava definitivamente. Ele valida conexao, dependencias e planejamento de inserts.

### Gravar no banco

```bash
python pipeline.py --apply-db --commit
```

Use apenas quando os candidatos estiverem revisados e coerentes.

## Contrato da LLM

A LLM nao gera SQL. Ela retorna JSON estruturado:

```json
{
  "source": {
    "tipo": "SEFAZ",
    "titulo": "Fonte oficial",
    "url": "https://...",
    "orgao": "SEFAZ SP",
    "data_publicacao": "2025-01-01",
    "texto_relevante": "Trecho que sustenta a regra"
  },
  "rules": [
    {
      "tributo": "ICMS",
      "jurisdicao": {
        "tipo": "ESTADUAL",
        "uf": "SP"
      },
      "produto": {
        "ncm": "22021000",
        "descricao": "Refrigerantes",
        "categoria": "Bebidas"
      },
      "tipo_regra": "ALIQUOTA",
      "aliquota_percentual": 18.0,
      "vigencia_inicio": "2025-01-01",
      "vigencia_fim": null,
      "ativo": true,
      "resumo_regra": "Aliquota aplicavel conforme fonte oficial.",
      "observacoes": null,
      "confianca": "ALTA"
    }
  ]
}
```

Validacoes importantes:

- NCM dentro do escopo do MVP.
- Tributo permitido.
- UF SP para regra estadual.
- Tipo de regra valido.
- Aliquota obrigatoria para `ALIQUOTA`.
- Valor fixo obrigatorio para `PAUTA`.
- Vigencia obrigatoria.
- Fonte com titulo e URL.

## Integracao com o banco

O integrador grava nas tabelas:

- `tributos`
- `jurisdicoes`
- `produtos_fiscais`
- `fontes_legais`
- `regras_tributarias`

Principios:

- nao sobrescrever regra historica;
- criar nova linha para mudanca legal;
- sempre manter fonte legal rastreavel;
- deixar regras ambiguas em revisao;
- nao promover regra sem evidencia suficiente.

## Relacao com o extractor

O extractor espera encontrar no MySQL regras vigentes por:

- NCM;
- tributo;
- jurisdicao;
- vigencia;
- `ativo = 1`.

Portanto, depois de rodar o scraper com `--commit`, valide a analise no extractor:

```bash
cd ../extractor
python matcher.py nota_exemplo.xml
python matcher.py nota_exemplo2.xml
```

## Limitacoes atuais

- Golden dataset ainda precisa ser consolidado.
- DIFAL e ICMS-ST ainda precisam de regras mais especificas antes de automatizar.
- Fontes legais podem ter ambiguidade textual e exigir revisao humana.
- O scraper e processo de manutencao, nao endpoint publico.
