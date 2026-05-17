import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VENDOR_DIR = Path(__file__).parent / ".vendor"
if VENDOR_DIR.exists():
    sys.path.insert(0, str(VENDOR_DIR))

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

ALLOWED_RULE_TYPES = {
    "ALIQUOTA",
    "PAUTA",
    "ISENCAO",
    "NAO_TRIBUTADO",
    "REDUCAO_BASE",
    "DIFERIMENTO",
    "SUBSTITUICAO_TRIBUTARIA",
}

ALLOWED_SOURCE_TYPES = {
    "DIARIO_OFICIAL",
    "SEFAZ",
    "LEI",
    "DECRETO",
    "PORTARIA",
    "RESOLUCAO",
    "OUTRO",
}

ALLOWED_TAXES = {"ICMS", "DIFAL", "PIS", "COFINS", "IPI", "ICMS-ST"}


@dataclass(frozen=True)
class LLMConfig:
    provider: str = os.getenv("LLM_PROVIDER", "openai").lower()
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    openai_api_key: str | None = os.getenv("OPENAI_API_KEY")
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    gemini_api_key: str | None = os.getenv("GEMINI_API_KEY")
    gemini_base_url: str = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta")
    timeout: int = int(os.getenv("LLM_TIMEOUT", os.getenv("OPENAI_TIMEOUT", "90")))
    max_retries: int = int(os.getenv("LLM_MAX_RETRIES", "3"))
    retry_base_seconds: float = float(os.getenv("LLM_RETRY_BASE_SECONDS", "2"))


def build_prompt(source: dict[str, Any], targets: dict[str, Any], text: str) -> str:
    ncms = ", ".join(item["ncm"] for item in targets["ncms"])
    tributos = ", ".join(targets["tributos"])
    return f"""
Voce e um analista tributario brasileiro. Extraia SOMENTE regras fiscais
explicitamente sustentadas pelo texto da fonte.

Escopo do MVP:
- Estado: SP quando a regra for estadual.
- NCMs: {ncms}.
- Tributos: {tributos}.

Fonte:
- Tipo sugerido: {source.get("tipo")}
- Titulo: {source.get("titulo")}
- Orgao: {source.get("orgao")}
- URL: {source.get("url")}

Regras:
1. Nao invente aliquota, pauta, vigencia ou produto.
2. Se o texto nao permitir afirmar uma regra para um dos NCMs, retorne rules vazio.
3. Use NCM sem pontos, com 8 digitos.
4. Para ICMS-ST, use tributo "ICMS-ST".
5. Para DIFAL, use tributo "DIFAL".
6. Para PIS/COFINS, se a fonte separar os tributos, retorne uma regra por tributo.
7. texto_relevante deve conter apenas o trecho que justifica a regra.
8. Retorne JSON puro, sem markdown.

Formato obrigatorio:
{{
  "source": {{
    "tipo": "SEFAZ|DIARIO_OFICIAL|LEI|DECRETO|PORTARIA|RESOLUCAO|OUTRO",
    "titulo": "string",
    "url": "string",
    "orgao": "string|null",
    "data_publicacao": "YYYY-MM-DD|null",
    "texto_relevante": "string"
  }},
  "rules": [
    {{
      "tributo": "ICMS|DIFAL|PIS|COFINS|IPI|ICMS-ST",
      "jurisdicao": {{
        "tipo": "FEDERAL|ESTADUAL|MUNICIPAL",
        "uf": "SP|null",
        "municipio": null,
        "codigo_ibge": null
      }},
      "produto": {{
        "ncm": "string",
        "descricao": "string",
        "categoria": "string|null"
      }},
      "tipo_regra": "ALIQUOTA|PAUTA|ISENCAO|NAO_TRIBUTADO|REDUCAO_BASE|DIFERIMENTO|SUBSTITUICAO_TRIBUTARIA",
      "aliquota_percentual": 0.0,
      "valor_fixo": null,
      "unidade_valor": null,
      "vigencia_inicio": "YYYY-MM-DD",
      "vigencia_fim": "YYYY-MM-DD|null",
      "ativo": true,
      "resumo_regra": "string",
      "observacoes": "string|null",
      "confianca": "ALTA|MEDIA|BAIXA"
    }}
  ]
}}

Texto da fonte:
\"\"\"
{text[:45000]}
\"\"\"
""".strip()


def interpret_with_llm(source: dict[str, Any], targets: dict[str, Any], text: str, config: LLMConfig | None = None) -> dict[str, Any]:
    config = config or LLMConfig()
    raw_text = _call_llm(build_prompt(source, targets, text), config)
    parsed = _loads_json_object(raw_text)
    validate_interpretation(parsed, targets)
    return parsed


def interpret_candidates_with_llm(candidates: list[dict[str, Any]], targets: dict[str, Any], config: LLMConfig | None = None) -> dict[str, Any]:
    config = config or LLMConfig()
    prepared = prepare_candidates_for_prompt(candidates)
    raw_text = _call_llm(build_candidates_prompt(prepared, targets), config)
    parsed = _loads_json_object(raw_text)
    parsed = repair_candidate_interpretations(parsed, prepared)
    validate_candidate_interpretations(parsed, targets, prepared)
    return parsed


def repair_candidate_interpretations(data: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {candidate["candidate_id"]: candidate for candidate in candidates}
    repaired_by_source: dict[str, dict[str, Any]] = {}
    review_items = _normalize_review_items(data.get("review_items", []), by_id)
    review_ids = {item["candidate_id"] for item in review_items if item.get("candidate_id")}
    accepted_ids: set[str] = set()

    for interpretation in data.get("interpretations", []) or []:
        if not isinstance(interpretation, dict):
            continue
        for rule in interpretation.get("rules", []) or []:
            if not isinstance(rule, dict):
                continue
            candidate_id = rule.get("candidate_id")
            candidate = by_id.get(candidate_id)
            if not candidate:
                continue

            reason = _candidate_review_reason(candidate, allow_mvp_promotion=True)
            if reason:
                if candidate_id not in review_ids:
                    review_items.append(
                        {
                            "candidate_id": candidate_id,
                            "source_id": candidate.get("source_id"),
                            "reason": reason,
                        }
                    )
                    review_ids.add(candidate_id)
                continue

            source_key = candidate.get("source_id") or candidate.get("source_url") or "unknown"
            repaired = repaired_by_source.setdefault(source_key, _empty_interpretation_from_candidate(candidate))
            repaired_rule = _rule_from_candidate(candidate)
            repaired_rule["resumo_regra"] = rule.get("resumo_regra") or repaired_rule["resumo_regra"]
            repaired_rule["observacoes"] = rule.get("observacoes", repaired_rule.get("observacoes"))
            repaired_rule["ativo"] = bool(rule.get("ativo", repaired_rule["ativo"]))
            repaired_rule["confianca"] = "ALTA"
            repaired["rules"].append(repaired_rule)
            repaired["_evidences"].append(candidate.get("evidence", ""))
            accepted_ids.add(candidate_id)

    for candidate in candidates:
        candidate_id = candidate["candidate_id"]
        if candidate_id in accepted_ids or candidate_id in review_ids:
            continue
        reason = _candidate_review_reason(candidate, allow_mvp_promotion=True)
        if reason:
            review_items.append(
                {
                    "candidate_id": candidate_id,
                    "source_id": candidate.get("source_id"),
                    "reason": reason,
                }
            )
            continue

        source_key = candidate.get("source_id") or candidate.get("source_url") or "unknown"
        repaired = repaired_by_source.setdefault(source_key, _empty_interpretation_from_candidate(candidate))
        repaired["rules"].append(_rule_from_candidate(candidate))
        repaired["_evidences"].append(candidate.get("evidence", ""))
        accepted_ids.add(candidate_id)

    interpretations = []
    for interpretation in repaired_by_source.values():
        evidences = [text for text in interpretation.pop("_evidences", []) if text]
        interpretation["source"]["texto_relevante"] = "\n".join(evidences)[:5000]
        interpretations.append(interpretation)

    return {"interpretations": interpretations, "review_items": review_items}


def _normalize_review_items(items: Any, by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    if not isinstance(items, list):
        return normalized
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        candidate_id = item.get("candidate_id")
        candidate = by_id.get(candidate_id)
        if not candidate or candidate_id in seen:
            continue
        normalized.append(
            {
                "candidate_id": candidate_id,
                "source_id": item.get("source_id") or candidate.get("source_id"),
                "reason": item.get("reason") or _candidate_review_reason(candidate) or "LLM indicou revisao.",
            }
        )
        seen.add(candidate_id)
    return normalized


def interpret_candidates_deterministically(candidates: list[dict[str, Any]], targets: dict[str, Any]) -> dict[str, Any]:
    prepared = prepare_candidates_for_prompt(candidates)
    interpretations_by_source: dict[str, dict[str, Any]] = {}
    review_items: list[dict[str, Any]] = []

    for candidate in prepared:
        reason = _candidate_review_reason(candidate)
        if reason:
            review_items.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "source_id": candidate.get("source_id"),
                    "reason": reason,
                }
            )
            continue

        source_key = candidate.get("source_id") or candidate.get("source_url") or "unknown"
        interpretation = interpretations_by_source.setdefault(
            source_key,
            _empty_interpretation_from_candidate(candidate),
        )
        interpretation["rules"].append(_rule_from_candidate(candidate))
        interpretation["_evidences"].append(candidate.get("evidence", ""))

    interpretations = []
    for interpretation in interpretations_by_source.values():
        evidences = [text for text in interpretation.pop("_evidences", []) if text]
        interpretation["source"]["texto_relevante"] = "\n".join(evidences)[:5000]
        interpretations.append(interpretation)

    result = {"interpretations": interpretations, "review_items": review_items}
    validate_candidate_interpretations(result, targets, prepared)
    return result


def _candidate_review_reason(candidate: dict[str, Any], allow_mvp_promotion: bool = False) -> str | None:
    if candidate.get("status") != "ready_for_db" and not (
        allow_mvp_promotion and _is_mvp_promotable_candidate(candidate)
    ):
        return f"status={candidate.get('status')} exige revisao."
    if candidate.get("confidence") != "ALTA" and not (
        allow_mvp_promotion and _is_mvp_promotable_candidate(candidate)
    ):
        return f"confidence={candidate.get('confidence')} exige revisao."
    if not candidate.get("vigencia_inicio"):
        return "vigencia_inicio nao informada pela fonte/candidato."
    if (candidate.get("extra") or {}).get("revoked_context"):
        return "evidencia indica contexto de revogacao."
    if not candidate.get("evidence"):
        return "candidato sem evidencia textual."
    if candidate.get("tipo_regra") == "ALIQUOTA" and candidate.get("aliquota_percentual") is None:
        return "ALIQUOTA sem aliquota_percentual."
    if candidate.get("tipo_regra") == "PAUTA" and candidate.get("valor_fixo") is None:
        return "PAUTA sem valor_fixo."
    return None


def _is_mvp_promotable_candidate(candidate: dict[str, Any]) -> bool:
    extra = candidate.get("extra") or {}
    if not extra.get("mvp_promotable"):
        return False
    if extra.get("promotion_kind") != "general_rate_rule":
        return False
    if candidate.get("source_id") not in {
        "sp-ricms-2023",
        "pis-cofins-lei-10637-2002",
        "cofins-lei-10833-2003",
    }:
        return False
    if candidate.get("tributo") not in {"ICMS", "PIS", "COFINS"}:
        return False
    if candidate.get("tipo_regra") != "ALIQUOTA":
        return False
    if candidate.get("confidence") != "MEDIA":
        return False
    if (candidate.get("extra") or {}).get("revoked_context"):
        return False
    return True


def _empty_interpretation_from_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": {
            "tipo": candidate.get("source_tipo") or "OUTRO",
            "titulo": candidate.get("source_title"),
            "url": candidate.get("source_url"),
            "orgao": candidate.get("source_orgao"),
            "data_publicacao": candidate.get("source_data_publicacao"),
            "texto_relevante": "",
        },
        "rules": [],
        "_evidences": [],
    }


def _rule_from_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": candidate["candidate_id"],
        "tributo": candidate["tributo"],
        "jurisdicao": candidate["jurisdicao"],
        "produto": candidate["produto"],
        "tipo_regra": candidate["tipo_regra"],
        "aliquota_percentual": candidate.get("aliquota_percentual"),
        "valor_fixo": candidate.get("valor_fixo"),
        "unidade_valor": candidate.get("unidade_valor"),
        "vigencia_inicio": candidate["vigencia_inicio"],
        "vigencia_fim": candidate.get("vigencia_fim"),
        "ativo": True,
        "resumo_regra": _summary_from_candidate(candidate),
        "observacoes": candidate.get("notes"),
        "confianca": "ALTA",
    }


def _summary_from_candidate(candidate: dict[str, Any]) -> str:
    aliquota = candidate.get("aliquota_percentual")
    if candidate.get("tipo_regra") == "NAO_TRIBUTADO":
        return f"{candidate['tributo']} nao tributado conforme fonte {candidate.get('source_title')}."
    if aliquota is not None:
        return f"{candidate['tributo']} de {aliquota}% conforme fonte {candidate.get('source_title')}."
    return f"{candidate['tributo']} conforme fonte {candidate.get('source_title')}."


def _call_llm(prompt: str, config: LLMConfig) -> str:
    provider = config.provider.lower()
    if provider == "openai":
        return _call_openai(prompt, config)
    if provider == "gemini":
        return _call_gemini(prompt, config)
    raise RuntimeError(f"LLM_PROVIDER invalido: {config.provider}. Use openai ou gemini.")


def _call_openai(prompt: str, config: LLMConfig) -> str:
    import requests

    if not config.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY nao configurada. Rode com --no-llm ou configure LLM_PROVIDER=gemini.")

    url = f"{config.openai_base_url.rstrip('/')}/responses"
    response = _post_with_retries(
        url,
        headers={
            "Authorization": f"Bearer {config.openai_api_key}",
            "Content-Type": "application/json",
        },
        json_payload={
            "model": config.openai_model,
            "input": prompt,
            "temperature": 0,
        },
        timeout=config.timeout,
        config=config,
        provider="OpenAI",
    )
    payload = response.json()
    return payload.get("output_text") or _extract_response_text(payload)


def _call_gemini(prompt: str, config: LLMConfig) -> str:
    import requests

    if not config.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY nao configurada. Defina LLM_PROVIDER=gemini e GEMINI_API_KEY.")

    url = (
        f"{config.gemini_base_url.rstrip('/')}/models/"
        f"{config.gemini_model}:generateContent?key={config.gemini_api_key}"
    )
    response = _post_with_retries(
        url,
        headers={"Content-Type": "application/json"},
        json_payload={
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
            },
        },
        timeout=config.timeout,
        config=config,
        provider="Gemini",
    )
    return _extract_gemini_text(response.json())


def _post_with_retries(
    url: str,
    headers: dict[str, str],
    json_payload: dict[str, Any],
    timeout: int,
    config: LLMConfig,
    provider: str,
):
    import requests

    last_response = None
    for attempt in range(config.max_retries + 1):
        response = requests.post(url, headers=headers, json=json_payload, timeout=timeout)
        if response.status_code not in {429, 500, 502, 503, 504}:
            if response.status_code >= 400:
                raise RuntimeError(_format_llm_http_error(provider, response))
            return response

        last_response = response
        if attempt >= config.max_retries:
            break
        time.sleep(_retry_delay_seconds(response, attempt, config.retry_base_seconds))

    raise RuntimeError(_format_llm_http_error(provider, last_response))


def _retry_delay_seconds(response: Any, attempt: int, base_seconds: float) -> float:
    retry_after = response.headers.get("retry-after") if response is not None else None
    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            pass
    return base_seconds * (2**attempt)


def _format_llm_http_error(provider: str, response: Any) -> str:
    status = getattr(response, "status_code", "desconhecido")
    message = ""
    try:
        payload = response.json()
        message = (payload.get("error") or {}).get("message") or json.dumps(payload, ensure_ascii=False)
    except Exception:
        message = getattr(response, "text", "") or ""

    hint = ""
    if status == 429:
        hint = (
            " Limite/quota do provider atingido. Aguarde a janela de quota, reduza o lote, "
            "troque GEMINI_MODEL/OPENAI_MODEL, ou use outro provider."
        )

    return f"{provider} retornou HTTP {status}: {_redact_secrets(message)}{hint}"


def _redact_secrets(value: str) -> str:
    value = re.sub(r"key=[A-Za-z0-9_\-]+", "key=[REDACTED]", value)
    value = re.sub(r"AIza[0-9A-Za-z_\-]{20,}", "AIza[REDACTED]", value)
    return value


def _extract_gemini_text(payload: dict[str, Any]) -> str:
    chunks: list[str] = []
    for candidate in payload.get("candidates", []):
        content = candidate.get("content") or {}
        for part in content.get("parts", []):
            if "text" in part:
                chunks.append(part["text"])
    return "\n".join(chunks).strip()


def prepare_candidates_for_prompt(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates, 1):
        item = dict(candidate)
        item["candidate_id"] = candidate.get("candidate_id") or f"cand-{index:04d}"
        prepared.append(item)
    return prepared


def build_candidates_prompt(candidates: list[dict[str, Any]], targets: dict[str, Any]) -> str:
    ncms = ", ".join(item["ncm"] for item in targets["ncms"])
    tributos = ", ".join(targets["tributos"])
    payload = json.dumps(candidates, ensure_ascii=False, indent=2)
    return f"""
Voce e um analista tributario brasileiro. Sua tarefa e transformar candidatos
estruturados em interpretacoes finais para gravacao em banco, usando SOMENTE
os campos dos candidatos e suas evidencias.

Escopo do MVP:
- Estado: SP quando a regra for estadual.
- NCMs: {ncms}.
- Tributos: {tributos}.

Regras criticas:
1. Nao gere SQL.
2. Nao invente aliquota, vigencia, excecao, produto, fonte legal ou trecho.
3. Inclua em rules todos os candidatos com status "ready_for_db" e confidence "ALTA".
4. Para este MVP, voce tambem pode promover candidatos "needs_review" SOMENTE quando:
   extra.mvp_promotable=true, extra.promotion_kind="general_rate_rule", tributo ICMS/PIS/COFINS,
   tipo_regra ALIQUOTA, aliquota_percentual preenchida, vigencia_inicio preenchida,
   evidencia sem revogacao e confidence "MEDIA".
   Quando estas condicoes forem atendidas, inclua o candidato em rules.
5. Candidatos "needs_review" fora da regra de promocao acima, com confidence BAIXA,
   evidencia de revogacao, valor nulo quando a regra exige valor, ou vigencia incerta
   devem ir para review_items.
6. Cada rule deve preservar candidate_id, tributo, jurisdicao, produto, tipo_regra,
   aliquota_percentual, valor_fixo e unidade_valor do candidato.
7. source deve preservar source_tipo/source_title/source_url/source_orgao dos candidatos.
8. texto_relevante da fonte deve ser composto por evidencias literais dos candidatos aceitos.
9. vigencia_inicio e vigencia_fim devem representar validade real. Se a evidencia nao sustentar
   vigencia_inicio, nao inclua o candidato em rules.
10. ativo deve ser true apenas se a regra estiver vigente agora.
11. Retorne JSON puro, sem markdown.

Formato obrigatorio:
{{
  "interpretations": [
    {{
      "source": {{
        "tipo": "SEFAZ|DIARIO_OFICIAL|LEI|DECRETO|PORTARIA|RESOLUCAO|OUTRO",
        "titulo": "string",
        "url": "string",
        "orgao": "string|null",
        "data_publicacao": "YYYY-MM-DD|null",
        "texto_relevante": "string"
      }},
      "rules": [
        {{
          "candidate_id": "string",
          "tributo": "ICMS|DIFAL|PIS|COFINS|IPI|ICMS-ST",
          "jurisdicao": {{
            "tipo": "FEDERAL|ESTADUAL|MUNICIPAL",
            "uf": "SP|null",
            "municipio": null,
            "codigo_ibge": null
          }},
          "produto": {{
            "ncm": "string",
            "descricao": "string",
            "categoria": "string|null"
          }},
          "tipo_regra": "ALIQUOTA|PAUTA|ISENCAO|NAO_TRIBUTADO|REDUCAO_BASE|DIFERIMENTO|SUBSTITUICAO_TRIBUTARIA",
          "aliquota_percentual": 0.0,
          "valor_fixo": null,
          "unidade_valor": null,
          "vigencia_inicio": "YYYY-MM-DD",
          "vigencia_fim": "YYYY-MM-DD|null",
          "ativo": true,
          "resumo_regra": "string",
          "observacoes": "string|null",
          "confianca": "ALTA|MEDIA|BAIXA"
        }}
      ]
    }}
  ],
  "review_items": [
    {{
      "candidate_id": "string",
      "source_id": "string",
      "reason": "string"
    }}
  ]
}}

Candidatos:
{payload[:120000]}
""".strip()


def _extract_response_text(payload: dict[str, Any]) -> str:
    chunks: list[str] = []
    for output in payload.get("output", []):
        for content in output.get("content", []):
            if content.get("type") in {"output_text", "text"}:
                chunks.append(content.get("text", ""))
    return "\n".join(chunks).strip()


def _loads_json_object(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("LLM retornou JSON que nao e objeto.")
    return parsed


def validate_interpretation(data: dict[str, Any], targets: dict[str, Any]) -> None:
    if "source" not in data or "rules" not in data:
        raise ValueError("JSON precisa conter source e rules.")

    source = data["source"]
    if source.get("tipo") not in ALLOWED_SOURCE_TYPES:
        raise ValueError(f"Tipo de fonte invalido: {source.get('tipo')}")
    if not source.get("titulo") or not source.get("url"):
        raise ValueError("Fonte precisa de titulo e url.")

    target_ncms = {item["ncm"] for item in targets["ncms"]}
    for index, rule in enumerate(data["rules"], 1):
        tributo = rule.get("tributo")
        if tributo not in ALLOWED_TAXES:
            raise ValueError(f"Regra {index}: tributo invalido: {tributo}")

        produto = rule.get("produto") or {}
        ncm = only_digits(produto.get("ncm", ""))
        if ncm not in target_ncms:
            raise ValueError(f"Regra {index}: NCM fora do MVP: {produto.get('ncm')}")
        produto["ncm"] = ncm

        jurisdicao = rule.get("jurisdicao") or {}
        if jurisdicao.get("tipo") == "ESTADUAL" and jurisdicao.get("uf") != "SP":
            raise ValueError(f"Regra {index}: regra estadual fora de SP.")

        tipo_regra = rule.get("tipo_regra")
        if tipo_regra not in ALLOWED_RULE_TYPES:
            raise ValueError(f"Regra {index}: tipo_regra invalido: {tipo_regra}")

        if tipo_regra == "ALIQUOTA" and rule.get("aliquota_percentual") is None:
            raise ValueError(f"Regra {index}: ALIQUOTA precisa de aliquota_percentual.")
        if tipo_regra == "PAUTA" and rule.get("valor_fixo") is None:
            raise ValueError(f"Regra {index}: PAUTA precisa de valor_fixo.")
        if not rule.get("vigencia_inicio"):
            raise ValueError(f"Regra {index}: vigencia_inicio obrigatoria.")


def validate_candidate_interpretations(data: dict[str, Any], targets: dict[str, Any], candidates: list[dict[str, Any]]) -> None:
    interpretations = data.get("interpretations")
    if not isinstance(interpretations, list):
        raise ValueError("JSON precisa conter interpretations como lista.")

    by_id = {candidate["candidate_id"]: candidate for candidate in candidates}
    used_ids: set[str] = set()
    for interpretation_index, interpretation in enumerate(interpretations, 1):
        validate_interpretation(interpretation, targets)
        source_url = interpretation["source"].get("url")
        for rule_index, rule in enumerate(interpretation.get("rules", []), 1):
            candidate_id = rule.get("candidate_id")
            if candidate_id not in by_id:
                raise ValueError(f"Interpretacao {interpretation_index}, regra {rule_index}: candidate_id desconhecido.")
            if candidate_id in used_ids:
                raise ValueError(f"Interpretacao {interpretation_index}, regra {rule_index}: candidate_id duplicado.")
            used_ids.add(candidate_id)

            candidate = by_id[candidate_id]
            _validate_rule_matches_candidate(rule, candidate, source_url, interpretation_index, rule_index)

    review_items = data.get("review_items", [])
    if not isinstance(review_items, list):
        raise ValueError("review_items precisa ser lista quando informado.")
    for index, item in enumerate(review_items, 1):
        candidate_id = item.get("candidate_id")
        if candidate_id not in by_id:
            raise ValueError(f"review_items[{index}]: candidate_id desconhecido.")
        if not item.get("reason"):
            raise ValueError(f"review_items[{index}]: reason obrigatorio.")


def _validate_rule_matches_candidate(
    rule: dict[str, Any],
    candidate: dict[str, Any],
    source_url: str | None,
    interpretation_index: int,
    rule_index: int,
) -> None:
    prefix = f"Interpretacao {interpretation_index}, regra {rule_index}"
    if _candidate_review_reason(candidate, allow_mvp_promotion=True):
        raise ValueError(f"{prefix}: candidato nao elegivel para regra de banco.")
    if source_url and candidate.get("source_url") and source_url != candidate.get("source_url"):
        raise ValueError(f"{prefix}: fonte da regra nao corresponde ao candidato.")

    comparable_fields = [
        "tributo",
        "tipo_regra",
        "aliquota_percentual",
        "valor_fixo",
        "unidade_valor",
    ]
    for field in comparable_fields:
        if _normalized_value(rule.get(field)) != _normalized_value(candidate.get(field)):
            raise ValueError(f"{prefix}: campo {field} diverge do candidato.")

    rule_product = rule.get("produto") or {}
    candidate_product = candidate.get("produto") or {}
    if only_digits(rule_product.get("ncm", "")) != only_digits(candidate_product.get("ncm", "")):
        raise ValueError(f"{prefix}: NCM diverge do candidato.")

    rule_jurisdiction = rule.get("jurisdicao") or {}
    candidate_jurisdiction = candidate.get("jurisdicao") or {}
    for field in ["tipo", "uf", "municipio", "codigo_ibge"]:
        if rule_jurisdiction.get(field) != candidate_jurisdiction.get(field):
            raise ValueError(f"{prefix}: jurisdicao.{field} diverge do candidato.")

    if rule.get("confianca") != "ALTA":
        raise ValueError(f"{prefix}: confianca precisa ser ALTA.")
    if not candidate.get("evidence"):
        raise ValueError(f"{prefix}: candidato sem evidencia.")


def _normalized_value(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, int):
        return float(value)
    return value


def only_digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")
