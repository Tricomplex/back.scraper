import json
import re
from typing import List, Dict, Any
from pathlib import Path


TERMS_TO_BUSCAR = [
    "ICMS-ST",
    "PIS",
    "COFINS",
    "IPI",
    "FCP",
    "DIFAL",
    "ICMS",
    "IBS",
    "CBS",
    "RICMS",
]


def _extrair_palavras_e_spans(texto: str):
    palavras = []
    spans = []
    for match in re.finditer(r'\S+', texto):
        palavras.append(match.group(0))
        spans.append(match.span())
    return palavras, spans


def _achar_indice_palavra(spans: List[tuple], match_start: int, match_end: int) -> int:
    for index, (start, end) in enumerate(spans):
        if start <= match_start < end or start < match_end <= end or (match_start <= start and match_end >= end):
            return index
    if match_start < spans[0][0]:
        return 0
    return len(spans) - 1


def _extrair_contexto_por_posicao(palavras: List[str], indice: int, palavras_antes: int, palavras_depois: int) -> Dict[str, str]:
    inicio = max(0, indice - palavras_antes)
    fim = min(len(palavras), indice + palavras_depois + 1)
    contexto_antes = ' '.join(palavras[inicio:indice])
    contexto_depois = ' '.join(palavras[indice + 1:fim])
    contexto_completo = ' '.join(palavras[inicio:fim])
    return {
        'contexto_antes': contexto_antes.strip(),
        'contexto_depois': contexto_depois.strip(),
        'contexto_completo': contexto_completo.strip(),
        'posicao_palavra': indice,
    }


def extrair_contexto_aliquotas(texto: str, palavras_antes=30, palavras_depois=30) -> List[Dict[str, Any]]:
    """
    Extrai contexto ao redor de porcentagens encontradas no texto.
    """
    resultados = []
    palavras, spans = _extrair_palavras_e_spans(texto)

    matches = []
    for match in re.finditer(r'(\d+(?:[.,]\d+)?)\s*%', texto):
        indice = _achar_indice_palavra(spans, match.start(), match.end())
        matches.append({
            'tipo': 'aliquota',
            'indice': indice,
            'aliquota': match.group(1).replace(',', '.'),
            'texto_match': match.group(0),
            'span': match.span()
        })

    matches_ordenados = sorted(matches, key=lambda x: x['indice'])
    ultimo_fim = -1

    for match in matches_ordenados:
        indice = match['indice']

        contexto_antes_real = palavras_antes
        if ultimo_fim >= 0 and indice - ultimo_fim < palavras_antes:
            contexto_antes_real = max(1, indice - ultimo_fim)

        contexto = _extrair_contexto_por_posicao(palavras, indice, contexto_antes_real, palavras_depois)
        ultimo_fim = indice + palavras_depois

        resultados.append({
            'tipo': match['tipo'],
            'aliquota': match['aliquota'],
            'termo': None,
            'texto_match': match['texto_match'],
            **contexto,
        })

    return resultados


def extrair_contexto_termos(texto: str, termos: List[str], palavras_antes=30, palavras_depois=30) -> List[Dict[str, Any]]:
    """
    Extrai contexto ao redor de termos fiscais no texto, evitando sobreposições.
    """
    resultados = []
    palavras, spans = _extrair_palavras_e_spans(texto)

    matches = []
    for termo in termos:
        regex = re.compile(rf'(?<![A-Za-z0-9_]){re.escape(termo)}(?![A-Za-z0-9_])', re.IGNORECASE)
        for match in regex.finditer(texto):
            indice = _achar_indice_palavra(spans, match.start(), match.end())
            matches.append({
                'tipo': 'termo',
                'indice': indice,
                'termo': termo,
                'texto_match': match.group(0),
                'span': match.span()
            })

    matches_ordenados = sorted(matches, key=lambda x: x['indice'])
    ultimo_fim = -1

    for match in matches_ordenados:
        indice = match['indice']

        contexto_antes_real = palavras_antes
        if ultimo_fim >= 0 and indice - ultimo_fim < palavras_antes:
            contexto_antes_real = max(1, indice - ultimo_fim)

        contexto = _extrair_contexto_por_posicao(palavras, indice, contexto_antes_real, palavras_depois)
        ultimo_fim = indice + palavras_depois

        resultados.append({
            'tipo': match['tipo'],
            'aliquota': None,
            'termo': match['termo'],
            'texto_match': match['texto_match'],
            **contexto,
        })

    return resultados


def processar_json_scraping(caminho_json: str, palavras_antes=30, palavras_depois=30) -> List[Dict[str, Any]]:
    """
    Processa arquivo JSON do scraping e extrai contextos de alíquotas e termos fiscais.
    """
    with open(caminho_json, 'r', encoding='utf-8') as f:
        dados = json.load(f)

    resultados_encontrados = []

    if isinstance(dados, list):
        items = dados
    else:
        items = [dados]

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue

        textos_para_processar = []

        if 'texts' in item and isinstance(item['texts'], list):
            textos_para_processar.extend(item['texts'])

        if 'paragraphs' in item and isinstance(item['paragraphs'], list):
            textos_para_processar.extend(item['paragraphs'])

        if 'body_text' in item:
            textos_para_processar.append(item['body_text'])

        if 'body' in item:
            textos_para_processar.append(item['body'])

        if 'text' in item:
            textos_para_processar.append(item['text'])

        if 'title' in item:
            textos_para_processar.append(item['title'])

        if 'headings' in item:
            headings = item['headings']
            if isinstance(headings, dict):
                for nivel, titulos in headings.items():
                    if isinstance(titulos, list):
                        textos_para_processar.extend(titulos)
            elif isinstance(headings, list):
                textos_para_processar.extend(headings)

        for texto in textos_para_processar:
            if not texto:
                continue

            extracoes = []
            extracoes.extend(extrair_contexto_aliquotas(texto, palavras_antes=palavras_antes, palavras_depois=palavras_depois))
            extracoes.extend(extrair_contexto_termos(texto, TERMS_TO_BUSCAR, palavras_antes=palavras_antes, palavras_depois=palavras_depois))

            for item_extracao in extracoes:
                item_extracao['url'] = item.get('url', 'N/A')
                item_extracao['titulo_pagina'] = item.get('title', 'N/A')
                item_extracao['indice_item'] = idx
                resultados_encontrados.append(item_extracao)

    return resultados_encontrados

def salvar_resultados(resultados: List[Dict], caminho_saida: str):
    """Salva resultados em arquivo JSON."""
    with open(caminho_saida, 'w', encoding='utf-8') as f:
        json.dump(resultados, f, ensure_ascii=False, indent=2)
    
    print(f" Resultados salvos em: {caminho_saida}")
    print(f" Total de itens extraídos: {len(resultados)}")


def main():
    # Definir caminhos
    caminho_base = Path(__file__).parent
    
    arquivos_entrada = [
        'resultados.json',
        'resultado_com_filtro.json',
        'resultado.json'
    ]
    
    processado = False
    for arquivo in arquivos_entrada:
        caminho_entrada = caminho_base / arquivo
        if caminho_entrada.exists():
            print(f"\n Processando: {arquivo}")
            print(f"   Extraindo contexto de 30 palavras antes e depois de porcentagens e termos fiscais...")
            
            try:
                resultados = processar_json_scraping(
                    str(caminho_entrada),
                    palavras_antes=30,
                    palavras_depois=30
                )
                
                nome_saida = f"{arquivo.replace('.json', '')}_limpo.json"
                caminho_saida = caminho_base / nome_saida
                salvar_resultados(resultados, str(caminho_saida))
                
                if resultados:
                    print(f"\n Exemplo do primeiro resultado:")
                    exemplo = resultados[0]
                    if exemplo['tipo'] == 'aliquota':
                        print(f"   Alíquota: {exemplo['aliquota']}%")
                    else:
                        print(f"   Termo fiscal: {exemplo['termo']}")
                    print(f"   Texto encontrado: {exemplo['texto_match']}")
                    print(f"   Página: {exemplo['titulo_pagina'][:60]}...")
                    print(f"   Contexto: ...{exemplo['contexto_antes'][-100:]} [{exemplo['texto_match']}] {exemplo['contexto_depois'][:100]}...")
                else:
                    print(f"  Nenhum item encontrado neste arquivo.")
                
                processado = True
                break
            
            except Exception as e:
                print(f"Erro ao processar {arquivo}: {e}")
                continue
    
    if not processado:
        print(" Nenhum arquivo de resultado encontrado!")
        print("\n Sugestões:")
        print("   1. Verifique se o scraping realmente capturou dados com termos fiscais ou porcentagens")
        print("   2. Você pode testar o script com dados de exemplo usando: python limpador_dados.py --teste")


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == '--teste':
        print("\n Modo TESTE com dados de exemplo\n")
        print("=" * 70)
        
        dados_teste = {
            "url": "https://exemplo.gov.br/aliquotas",
            "title": "Alíquotas de ICMS e Impostos",
            "paragraphs": [
                "O ICMS sobre combustíveis varia de estado para estado. Em São Paulo, a alíquota de 18% se aplica à gasolina comum.",
                "Para o diesel, a alíquota de 15% é cobrada sobre todas as operações de venda no estado.",
                "Produtos importados podem ter uma alíquota especial de 12% dependendo da classificação fiscal.",
                "A contribuição de PIS/PASEP é de 7,6% para produtos em geral e 11,3% para bebidas."
            ]
        }
        
        texto_teste = ' '.join(dados_teste['paragraphs'])
        resultados = []
        resultados.extend(extrair_contexto_aliquotas(texto_teste, palavras_antes=30, palavras_depois=30))
        resultados.extend(extrair_contexto_termos(texto_teste, TERMS_TO_BUSCAR, palavras_antes=30, palavras_depois=30))
        
        for item_extracao in resultados:
            item_extracao['url'] = dados_teste['url']
            item_extracao['titulo_pagina'] = dados_teste['title']
        
        caminho_teste = Path(__file__).parent / "teste_dados_limpo.json"
        salvar_resultados(resultados, str(caminho_teste))
        
        print(f"\n Resultados do teste:")
        for i, item_extracao in enumerate(resultados, 1):
            if item_extracao['tipo'] == 'aliquota':
                label = f"Alíquota encontrada: {item_extracao['aliquota']}%"
            else:
                label = f"Termo encontrado: {item_extracao['termo']}"
            print(f"\n   [{i}] {label}")
            print(f"       Texto: {item_extracao['texto_match']}")
            print(f"       Contexto: ...{item_extracao['contexto_antes'][-80:]}... {item_extracao['texto_match']} {item_extracao['contexto_depois'][:80]}...")
        
        print(f"\n{'=' * 70}")
        print(f"✓ Arquivo de teste salvo em: teste_dados_limpo.json")
    
    else:
        main()
