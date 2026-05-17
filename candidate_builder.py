import re
from typing import Any


NCM_CELL_RE = re.compile(r"\b\d{4}(?:\.\d{2}(?:\.\d{1,2})?)?\b")
CEST_RE = re.compile(r"\b\d{2}\.\d{3}\.\d{2}\b")
PERCENT_RE = re.compile(r"(\d+(?:[,.]\d+)?)\s*%")


def build_structured_candidates(source: dict[str, Any], scraped: dict[str, Any], targets: dict[str, Any]) -> list[dict[str, Any]]:
    source_id = source["id"]
    if source_id == "tipi-xlsx-receita":
        return build_tipi_candidates(source, scraped, targets)
    if source_id == "sp-portaria-cat-68-2019":
        return build_portaria_cat_candidates(source, scraped, targets)
    if source_id == "sp-ricms-2023":
        return build_ricms_aliquota_candidates(source, scraped, targets)
    if source_id == "sp-ricms-difal-art253-258":
        return build_difal_candidates(source, scraped, targets)
    if source_id == "pis-cofins-lei-10637-2002":
        return build_pis_candidates(source, scraped, targets)
    if source_id == "cofins-lei-10833-2003":
        return build_cofins_candidates(source, scraped, targets)
    return []


def build_tipi_candidates(source: dict[str, Any], scraped: dict[str, Any], targets: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = []
    target_ncms = {target["ncm"] for target in targets["ncms"]}
    for row in scraped.get("rows", []):
        values = [str(value).strip() for value in row.get("values", []) if str(value).strip()]
        if not values:
            continue
        ncm = digits_only(values[0])
        if ncm not in target_ncms:
            continue
        rate = values[-1]
        description = " | ".join(values[1:-1]) if len(values) > 2 else values[1] if len(values) > 1 else ""
        is_nt = rate.upper() == "NT"
        candidates.append(
            base_candidate(
                source,
                tributo="IPI",
                ncm=ncm,
                descricao=description.lstrip("- "),
                tipo_regra="NAO_TRIBUTADO" if is_nt else "ALIQUOTA",
                aliquota_percentual=0 if is_nt else decimal_number(rate),
                valor_fixo=None,
                unidade_valor=None,
                status="ready_for_db",
                confidence="ALTA",
                evidence=f"TIPI XLSX {row.get('sheet')} linha {row.get('row')}: {' | '.join(values)}",
                notes=None if not is_nt else "TIPI indica NT para esta subcategoria especifica.",
            )
        )
    return candidates


def build_portaria_cat_candidates(source: dict[str, Any], scraped: dict[str, Any], targets: dict[str, Any]) -> list[dict[str, Any]]:
    texts = clean_texts(scraped.get("texts", []))
    target_ncms = [(target["ncm"], target["descricao"]) for target in targets["ncms"]]
    candidates = []
    seen = set()
    for index, text in enumerate(texts):
        if not CEST_RE.search(text):
            continue
        cest = CEST_RE.search(text).group(0)
        item = find_previous_item(texts[max(0, index - 4): index + 1])
        ncm_text = first_ncm_like_text(texts[index + 1: index + 4])
        if not ncm_text:
            continue
        normalized = normalize_ncm_text(ncm_text)
        for ncm, target_description in target_ncms:
            match_kind = None
            if ncm in normalized:
                match_kind = "NCM_EXATO"
            elif ncm[:4] in normalized and ncm == "20099000":
                match_kind = "NCM_PREFIXO_4_DIGITOS"
            if not match_kind:
                continue

            description = find_description_after(texts, index + 1)
            window = texts[max(0, index - 4): min(len(texts), index + 6)]
            evidence = " ".join(window)
            status = "needs_review"
            confidence = "MEDIA" if match_kind == "NCM_EXATO" else "BAIXA"
            if is_revoked_context(evidence):
                confidence = "BAIXA"
            key = (ncm, cest, description, item)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                base_candidate(
                    source,
                    tributo="ICMS-ST",
                    ncm=ncm,
                    descricao=description or target_description,
                    tipo_regra="SUBSTITUICAO_TRIBUTARIA",
                    aliquota_percentual=None,
                    valor_fixo=None,
                    unidade_valor=None,
                    status=status,
                    confidence=confidence,
                    evidence=evidence,
                    notes=(
                        f"Portaria CAT 68/2019 indica enquadramento em ST ({match_kind}); "
                        "nao traz sozinha a aliquota/MVA final. "
                        f"CEST proximo: {cest or 'nao identificado'}; item proximo: {item or 'nao identificado'}."
                    ),
                    extra={"cest": cest, "item": item, "match_kind": match_kind, "revoked_context": is_revoked_context(evidence)},
                )
            )
    return candidates


def build_ricms_aliquota_candidates(source: dict[str, Any], scraped: dict[str, Any], targets: dict[str, Any]) -> list[dict[str, Any]]:
    texts = clean_texts(scraped.get("texts", []))
    candidates = []
    for text in texts:
        if "operações ou prestações internas" in text and "18%" in text:
            for target in targets["ncms"]:
                candidates.append(
                    base_candidate(
                        source,
                        tributo="ICMS",
                        ncm=target["ncm"],
                        descricao=target["descricao"],
                        tipo_regra="ALIQUOTA",
                        aliquota_percentual=18,
                        valor_fixo=None,
                        unidade_valor=None,
                        status="needs_review",
                        confidence="MEDIA",
                        evidence=text,
                        notes=(
                            "Regra geral interna do RICMS/SP. Aplicar ao NCM apenas se nao houver excecao "
                            "especifica, reducao, isencao, ST ou regime especial."
                        ),
                    )
                )
            break
    return candidates


def build_difal_candidates(source: dict[str, Any], scraped: dict[str, Any], targets: dict[str, Any]) -> list[dict[str, Any]]:
    texts = clean_texts(scraped.get("texts", []))
    evidence = first_text_containing(texts, ["DIFAL", "consumidor final não contribuinte"]) or first_text_containing(texts, ["diferença entre as alíquotas"])
    if not evidence:
        return []
    return [
        base_candidate(
            source,
            tributo="DIFAL",
            ncm=target["ncm"],
            descricao=target["descricao"],
            tipo_regra="DIFERIMENTO",
            aliquota_percentual=None,
            valor_fixo=None,
            unidade_valor=None,
            status="needs_review",
            confidence="BAIXA",
            evidence=evidence,
            notes=(
                "Fonte disciplina recolhimento/procedimento de DIFAL para consumidor final nao contribuinte. "
                "Nao define aliquota por NCM; o calculo depende de aliquota interna, aliquota interestadual e operacao."
            ),
        )
        for target in targets["ncms"]
    ]


def build_pis_candidates(source: dict[str, Any], scraped: dict[str, Any], targets: dict[str, Any]) -> list[dict[str, Any]]:
    texts = clean_texts(scraped.get("texts", []))
    candidates = []
    general = first_text_containing(texts, ["PIS/Pasep", "1,65%"])
    if general:
        for target in targets["ncms"]:
            candidates.append(
                base_candidate(
                    source,
                    tributo="PIS",
                    ncm=target["ncm"],
                    descricao=target["descricao"],
                    tipo_regra="ALIQUOTA",
                    aliquota_percentual=1.65,
                    valor_fixo=None,
                    unidade_valor=None,
                    status="needs_review",
                    confidence="MEDIA",
                    evidence=general,
                    notes="Aliquota geral de PIS nao cumulativo; validar regime do contribuinte e excecoes de bebidas.",
                )
            )
    candidates.extend(build_beverage_special_candidates(source, texts, targets, "PIS"))
    return candidates


def build_cofins_candidates(source: dict[str, Any], scraped: dict[str, Any], targets: dict[str, Any]) -> list[dict[str, Any]]:
    texts = clean_texts(scraped.get("texts", []))
    candidates = []
    general = first_text_containing(texts, ["COFINS", "7,6%"])
    if general:
        for target in targets["ncms"]:
            candidates.append(
                base_candidate(
                    source,
                    tributo="COFINS",
                    ncm=target["ncm"],
                    descricao=target["descricao"],
                    tipo_regra="ALIQUOTA",
                    aliquota_percentual=7.6,
                    valor_fixo=None,
                    unidade_valor=None,
                    status="needs_review",
                    confidence="MEDIA",
                    evidence=general,
                    notes="Aliquota geral de COFINS nao cumulativa; validar regime do contribuinte e excecoes de bebidas.",
                )
            )
    candidates.extend(build_beverage_special_candidates(source, texts, targets, "COFINS"))
    return candidates


def build_beverage_special_candidates(source: dict[str, Any], texts: list[str], targets: dict[str, Any], tributo: str) -> list[dict[str, Any]]:
    candidates = []
    for index, text in enumerate(texts):
        norm = normalize(text)
        if "22.01" not in text and "22.02" not in text and "2202" not in text:
            continue
        if not any(term in norm for term in ["agua", "refrigerante", "bebida"]):
            continue
        evidence = " ".join(texts[index: min(len(texts), index + 3)])
        for target in targets["ncms"]:
            if target["ncm"].startswith("2201") or target["ncm"].startswith("2202"):
                candidates.append(
                    base_candidate(
                        source,
                        tributo=tributo,
                        ncm=target["ncm"],
                        descricao=target["descricao"],
                        tipo_regra="ALIQUOTA",
                        aliquota_percentual=None,
                        valor_fixo=None,
                        unidade_valor=None,
                        status="needs_review",
                        confidence="BAIXA" if is_revoked_context(evidence) else "MEDIA",
                        evidence=evidence,
                        notes=(
                            "Trecho indica regime/excecao para bebidas das posicoes 22.01/22.02. "
                            "Pode conter redacao historica, revogacao ou regime especial; nao inserir sem revisao."
                        ),
                        extra={"revoked_context": is_revoked_context(evidence)},
                    )
                )
        break
    return candidates


def base_candidate(
    source: dict[str, Any],
    tributo: str,
    ncm: str,
    descricao: str,
    tipo_regra: str,
    aliquota_percentual: float | None,
    valor_fixo: float | None,
    unidade_valor: str | None,
    status: str,
    confidence: str,
    evidence: str,
    notes: str | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "source_id": source["id"],
        "source_tipo": source.get("tipo"),
        "source_title": source["titulo"],
        "source_url": source["url"],
        "source_orgao": source.get("orgao"),
        "source_data_publicacao": source.get("data_publicacao"),
        "vigencia_inicio": source.get("vigencia_inicio"),
        "vigencia_fim": source.get("vigencia_fim"),
        "tributo": tributo,
        "jurisdicao": {
            "tipo": "FEDERAL" if tributo in {"IPI", "PIS", "COFINS"} else "ESTADUAL",
            "uf": None if tributo in {"IPI", "PIS", "COFINS"} else "SP",
            "municipio": None,
            "codigo_ibge": None,
        },
        "produto": {
            "ncm": ncm,
            "descricao": descricao,
            "categoria": "Bebidas",
        },
        "tipo_regra": tipo_regra,
        "aliquota_percentual": aliquota_percentual,
        "valor_fixo": valor_fixo,
        "unidade_valor": unidade_valor,
        "status": status,
        "confidence": confidence,
        "evidence": re.sub(r"\s+", " ", evidence).strip(),
        "notes": notes,
        "extra": extra or {},
    }


def clean_texts(values: list[Any]) -> list[str]:
    return [re.sub(r"[\u200b\u200c\u200d\ufeff]+", "", str(value)).strip() for value in values if str(value).strip()]


def digits_only(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def normalize_ncm_text(value: str) -> str:
    return digits_only(value)


def decimal_number(value: str) -> float | None:
    cleaned = value.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def normalize(value: str) -> str:
    value = value.lower()
    return value.translate(str.maketrans("áàâãäéèêëíìîïóòôõöúùûüç", "aaaaaeeeeiiiiooooouuuuc"))


def find_previous_item(window: list[str]) -> str | None:
    for text in reversed(window):
        if re.match(r"^\d+(?:\.\d+)?\b", text):
            return text
    return None


def find_nearby_pattern(window: list[str], pattern: re.Pattern[str]) -> str | None:
    for text in window:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


def find_description_after(texts: list[str], index: int) -> str | None:
    for text in texts[index + 1: index + 5]:
        if not text:
            continue
        if CEST_RE.search(text) or NCM_CELL_RE.search(text):
            continue
        if re.match(r"^\d+(?:\.\d+)?\b", text):
            continue
        return text
    return None


def first_ncm_like_text(texts: list[str]) -> str | None:
    for text in texts:
        normalized = normalize_ncm_text(text)
        if len(normalized) >= 4:
            return text
    return None


def is_revoked_context(text: str) -> bool:
    norm = normalize(text)
    return "revogado" in norm or "revogada" in norm or "vigencia encerrada" in norm


def first_text_containing(texts: list[str], terms: list[str]) -> str | None:
    normalized_terms = [normalize(term) for term in terms]
    for text in texts:
        norm = normalize(text)
        if all(term in norm for term in normalized_terms):
            return text
    return None
