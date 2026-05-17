import argparse
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

VENDOR_DIR = Path(__file__).parent / ".vendor"
if VENDOR_DIR.exists():
    sys.path.insert(0, str(VENDOR_DIR))

from limpador_dados import processar_json_scraping
from llm_interpreter import validate_interpretation


BASE_DIR = Path(__file__).parent
DEFAULT_CONFIG = BASE_DIR / "fontes_mvp.json"
ARTIFACTS_DIR = BASE_DIR / "artifacts"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def load_config(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def save_json(data: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


def source_text(scraped: dict[str, Any], relevant_contexts: list[dict[str, Any]] | None = None, max_chars: int = 60000) -> str:
    parts: list[str] = []
    if relevant_contexts:
        parts.extend(context["text"] for context in relevant_contexts if context.get("text"))

    for heading_values in scraped.get("headings", {}).values():
        if isinstance(heading_values, list):
            parts.extend(heading_values)
    parts.extend(scraped.get("texts", []))

    for child in scraped.get("links_data", []):
        content = child.get("content") or {}
        if not isinstance(content, dict) or not content.get("success"):
            continue
        parts.append(f"Link relacionado: {child.get('link_href')}")
        parts.extend(content.get("texts", []))

    text = "\n".join(str(part) for part in parts if part)
    return text[:max_chars]


def run_pipeline(
    config_path: Path,
    use_llm: bool,
    apply_db: bool,
    dry_run: bool,
    max_depth: int,
    use_source_text_llm: bool = False,
) -> dict[str, Any]:
    config = load_config(config_path)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = ARTIFACTS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "run_id": run_id,
        "config": str(config_path),
        "sources": [],
        "interpretations": [],
        "db_results": [],
    }

    summary_path = run_dir / "summary.json"

    for source in config["sources"]:
        from scraping import scrape, scrape_with_keyword

        source_id = source["id"]
        logger.info("Scraping fonte %s: %s", source_id, source["url"])
        try:
            keyword = " ".join(source.get("keywords", []))
            if source.get("kind") == "xlsx":
                from xlsx_extractor import extract_target_rows

                scraped = extract_target_rows(source, config["targets"], run_dir)
            else:
                scraped = (
                    scrape_with_keyword(source["url"], keyword=keyword, max_depth=max_depth)
                    if keyword and max_depth > 0
                    else scrape(source["url"])
                )
            scraped_path = run_dir / f"{source_id}_scraped.json"
            save_json(scraped, scraped_path)

            contexts = processar_contextos(scraped_path, run_dir, source_id)
            relevant_contexts = collect_relevant_contexts(scraped, config["targets"], source)
            relevant_contexts_path = run_dir / f"{source_id}_relevant_contexts.json"
            save_json(relevant_contexts, relevant_contexts_path)
            from candidate_builder import build_structured_candidates

            structured_candidates = build_structured_candidates(source, scraped, config["targets"])
            structured_candidates_path = run_dir / f"{source_id}_structured_candidates.json"
            save_json(structured_candidates, structured_candidates_path)
            text = source_text(scraped, relevant_contexts)

            source_summary = {
                "id": source_id,
                "url": source["url"],
                "success": scraped.get("success", False),
                "status_code": scraped.get("status_code"),
                "error": scraped.get("error"),
                "blocked": scraped.get("blocked", False),
                "blocked_reason": scraped.get("blocked_reason"),
                "scraped_path": str(scraped_path),
                "contexts_path": str(contexts),
                "relevant_contexts_path": str(relevant_contexts_path),
                "relevant_contexts": len(relevant_contexts),
                "structured_candidates_path": str(structured_candidates_path),
                "structured_candidates": len(structured_candidates),
                "ready_for_db_candidates": sum(1 for item in structured_candidates if item.get("status") == "ready_for_db"),
                "text_chars": len(text),
            }
            summary["sources"].append(source_summary)
            save_json(summary, summary_path)

            if not use_llm or not use_source_text_llm:
                continue

            from llm_interpreter import interpret_with_llm

            logger.info("Interpretando fonte %s com LLM", source_id)
            interpretation = interpret_with_llm(source, config["targets"], text)
            validate_interpretation(interpretation, config["targets"])
            interpretation_path = run_dir / f"{source_id}_interpretation.json"
            save_json(interpretation, interpretation_path)
            summary["interpretations"].append(
                {
                    "source_id": source_id,
                    "path": str(interpretation_path),
                    "rules": len(interpretation.get("rules", [])),
                }
            )
            save_json(summary, summary_path)

            if apply_db:
                from db_integrator import apply_interpretation

                logger.info("Aplicando interpretacao no banco fonte %s (dry_run=%s)", source_id, dry_run)
                db_result = apply_interpretation(interpretation, config["targets"], dry_run=dry_run)
                db_result["source_id"] = source_id
                summary["db_results"].append(db_result)
                save_json(summary, summary_path)
        except Exception as exc:
            logger.exception("Falha ao processar fonte %s: %s", source_id, exc)
            summary["sources"].append(
                {
                    "id": source_id,
                    "url": source["url"],
                    "success": False,
                    "error": str(exc),
                }
            )
            save_json(summary, summary_path)

    combined_candidates = []
    for item in summary["sources"]:
        path = item.get("structured_candidates_path")
        if not path or not Path(path).exists():
            continue
        with open(path, "r", encoding="utf-8") as file:
            combined_candidates.extend(json.load(file))
    combined_candidates_path = run_dir / "structured_candidates_all.json"
    save_json(combined_candidates, combined_candidates_path)
    summary["structured_candidates_all_path"] = str(combined_candidates_path)
    summary["structured_candidates_all"] = len(combined_candidates)
    summary["ready_for_db_candidates_all"] = sum(1 for item in combined_candidates if item.get("status") == "ready_for_db")
    save_json(summary, summary_path)

    if use_llm:
        from llm_interpreter import interpret_candidates_with_llm

        combined_candidates = enrich_candidates_with_sources(combined_candidates, config["sources"])
        save_json(combined_candidates, combined_candidates_path)
        logger.info("Interpretando %s candidatos estruturados com LLM", len(combined_candidates))
        candidate_interpretations = interpret_candidates_with_llm(combined_candidates, config["targets"])
        candidate_interpretations_path = run_dir / "db_interpretations_from_candidates.json"
        save_json(candidate_interpretations, candidate_interpretations_path)
        summary["candidate_interpretations_path"] = str(candidate_interpretations_path)
        summary["candidate_interpretations"] = len(candidate_interpretations.get("interpretations", []))
        summary["candidate_rules"] = sum(
            len(item.get("rules", [])) for item in candidate_interpretations.get("interpretations", [])
        )
        summary["candidate_review_items"] = len(candidate_interpretations.get("review_items", []))
        save_json(summary, summary_path)

        if apply_db:
            from db_integrator import apply_interpretation_batch

            logger.info("Aplicando interpretacoes finais no banco (dry_run=%s)", dry_run)
            summary["candidate_db_result"] = apply_interpretation_batch(
                candidate_interpretations,
                config["targets"],
                dry_run=dry_run,
            )
            save_json(summary, summary_path)

    logger.info("Pipeline concluido. Summary: %s", summary_path)
    return summary


def run_candidates_file(
    config_path: Path,
    candidates_path: Path,
    apply_db: bool,
    dry_run: bool,
    candidate_mode: str = "llm",
) -> dict[str, Any]:
    config = load_config(config_path)
    with open(candidates_path, "r", encoding="utf-8") as file:
        candidates = json.load(file)
    if not isinstance(candidates, list):
        raise ValueError("Arquivo de candidatos precisa conter uma lista JSON.")
    candidates = enrich_candidates_with_sources(candidates, config["sources"])

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = ARTIFACTS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    summary_path = run_dir / "summary.json"

    if candidate_mode == "deterministic":
        from llm_interpreter import interpret_candidates_deterministically

        logger.info("Interpretando arquivo de candidatos %s em modo deterministico", candidates_path)
        interpretation_batch = interpret_candidates_deterministically(candidates, config["targets"])
    elif candidate_mode == "llm":
        from llm_interpreter import interpret_candidates_with_llm

        logger.info("Interpretando arquivo de candidatos %s com LLM", candidates_path)
        interpretation_batch = interpret_candidates_with_llm(candidates, config["targets"])
    else:
        raise ValueError("candidate_mode precisa ser llm ou deterministic.")
    interpretation_path = run_dir / "db_interpretations_from_candidates.json"
    save_json(interpretation_batch, interpretation_path)

    summary = {
        "run_id": run_id,
        "config": str(config_path),
        "candidates_path": str(candidates_path),
        "candidate_mode": candidate_mode,
        "candidate_interpretations_path": str(interpretation_path),
        "candidate_interpretations": len(interpretation_batch.get("interpretations", [])),
        "candidate_rules": sum(len(item.get("rules", [])) for item in interpretation_batch.get("interpretations", [])),
        "candidate_review_items": len(interpretation_batch.get("review_items", [])),
        "candidate_db_result": None,
    }

    if apply_db:
        from db_integrator import apply_interpretation_batch

        logger.info("Aplicando interpretacoes finais no banco (dry_run=%s)", dry_run)
        summary["candidate_db_result"] = apply_interpretation_batch(
            interpretation_batch,
            config["targets"],
            dry_run=dry_run,
        )

    save_json(summary, summary_path)
    logger.info("Processamento de candidatos concluido. Summary: %s", summary_path)
    return summary


def enrich_candidates_with_sources(candidates: list[dict[str, Any]], sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {source["id"]: source for source in sources}
    enriched: list[dict[str, Any]] = []
    for candidate in candidates:
        source = by_id.get(candidate.get("source_id"), {})
        item = dict(candidate)
        item.setdefault("source_tipo", source.get("tipo"))
        item.setdefault("source_title", source.get("titulo") or item.get("source_title"))
        item.setdefault("source_url", source.get("url") or item.get("source_url"))
        item.setdefault("source_orgao", source.get("orgao"))
        item.setdefault("source_data_publicacao", source.get("data_publicacao"))
        item.setdefault("vigencia_inicio", source.get("vigencia_inicio"))
        item.setdefault("vigencia_fim", source.get("vigencia_fim"))
        enriched.append(item)
    return enriched


def processar_contextos(scraped_path: Path, run_dir: Path, source_id: str) -> Path:
    resultados = processar_json_scraping(str(scraped_path), palavras_antes=45, palavras_depois=45)
    output_path = run_dir / f"{source_id}_contexts.json"
    save_json(resultados, output_path)
    return output_path


def collect_relevant_contexts(scraped: dict[str, Any], targets: dict[str, Any], source: dict[str, Any]) -> list[dict[str, Any]]:
    patterns = build_relevance_patterns(targets, source)
    candidates = []
    for index, text in enumerate(scraped.get("texts", [])):
        candidates.append(("text", index, str(text)))
    for index, block in enumerate(scraped.get("text_blocks", [])):
        candidates.append((block.get("tag", "text_block"), index, str(block.get("text", ""))))
    for index, row in enumerate(scraped.get("rows", [])):
        values = [str(value) for value in row.get("values", [])]
        candidates.append(("table_row", index, " | ".join(values)))

    relevant: list[dict[str, Any]] = []
    seen: set[str] = set()
    for kind, index, text in candidates:
        normalized_text = normalize_for_match(text)
        matched = [label for label, pattern in patterns if pattern.search(normalized_text)]
        if not matched:
            continue

        cleaned = re.sub(r"\s+", " ", text).strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        relevant.append(
            {
                "source_id": source["id"],
                "kind": kind,
                "index": index,
                "matched": matched,
                "text": cleaned,
            }
        )
    return relevant


def build_relevance_patterns(targets: dict[str, Any], source: dict[str, Any]) -> list[tuple[str, re.Pattern[str]]]:
    terms: list[tuple[str, str]] = []
    for item in targets["ncms"]:
        ncm = item["ncm"]
        dotted = f"{ncm[:4]}.{ncm[4:6]}.{ncm[6:]}"
        terms.append((ncm, re.escape(ncm)))
        terms.append((dotted, re.escape(dotted)))
        for alias in item.get("apelidos", []):
            terms.append((alias, rf"\b{re.escape(normalize_for_match(alias))}\b"))

    for tributo in targets.get("tributos", []):
        terms.append((tributo, rf"\b{re.escape(normalize_for_match(tributo))}\b"))
    for keyword in source.get("keywords", []):
        if len(keyword) >= 4:
            terms.append((keyword, rf"\b{re.escape(normalize_for_match(keyword))}\b"))

    # Marcadores fiscais sem porcentagem.
    terms.extend(
        [
            ("NT", r"\bnt\b"),
        ]
    )

    deduped: dict[str, re.Pattern[str]] = {}
    for label, pattern in terms:
        deduped[label] = re.compile(pattern, re.IGNORECASE)
    return list(deduped.items())


def normalize_for_match(text: str) -> str:
    text = text.lower()
    replacements = str.maketrans(
        "áàâãäéèêëíìîïóòôõöúùûüç",
        "aaaaaeeeeiiiiooooouuuuc",
    )
    return text.translate(replacements)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pipeline MVP de scraping tributario da Tricomplex.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Arquivo JSON com fontes e escopo.")
    parser.add_argument("--no-llm", action="store_true", help="Roda scraping + limpeza, sem interpretar com LLM.")
    parser.add_argument("--llm-source-text", action="store_true", help="Tambem roda o modo antigo de LLM por texto bruto de cada fonte.")
    parser.add_argument("--candidates-file", help="Interpreta um structured_candidates_all.json existente, sem refazer scraping.")
    parser.add_argument(
        "--candidate-mode",
        choices=["llm", "deterministic"],
        default=None,
        help="Modo para --candidates-file. Padrao: llm, ou deterministic quando usado com --no-llm.",
    )
    parser.add_argument("--apply-db", action="store_true", help="Aplica as regras interpretadas no MySQL.")
    parser.add_argument("--commit", action="store_true", help="Confirma inserts no banco. Sem isso, --apply-db roda em dry-run.")
    parser.add_argument("--max-depth", type=int, default=1, help="Profundidade de crawl por links filtrados.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.candidates_file:
        candidate_mode = args.candidate_mode or ("deterministic" if args.no_llm else "llm")
        run_candidates_file(
            config_path=Path(args.config),
            candidates_path=Path(args.candidates_file),
            apply_db=args.apply_db,
            dry_run=not args.commit,
            candidate_mode=candidate_mode,
        )
        return 0

    run_pipeline(
        config_path=Path(args.config),
        use_llm=not args.no_llm,
        apply_db=args.apply_db,
        dry_run=not args.commit,
        max_depth=args.max_depth,
        use_source_text_llm=args.llm_source_text,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
