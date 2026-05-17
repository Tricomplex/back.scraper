import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urldefrag, urljoin, urlparse

VENDOR_DIR = Path(__file__).parent / ".vendor"
if VENDOR_DIR.exists():
    sys.path.insert(0, str(VENDOR_DIR))

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


DEFAULT_URL = "https://books.toscrape.com"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}

LEAF_TEXT_TAGS = {
    "p",
    "li",
    "td",
    "th",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "span",
    "blockquote",
    "label",
}
CONTAINER_TEXT_TAGS = {"div", "article", "section", "main", "aside"}
TEXT_TAGS = sorted(LEAF_TEXT_TAGS | CONTAINER_TEXT_TAGS)

IGNORE_TAGS = ["script", "style", "nav", "header", "footer", "noscript", "iframe"]

IMAGE_SOURCE_ATTRS = [
    "src",
    "data-src",
    "data-original",
    "data-lazy-src",
    "data-srcset",
    "srcset",
]

NON_HTML_EXTENSIONS = {
    ".7z",
    ".avi",
    ".css",
    ".csv",
    ".doc",
    ".docx",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".rar",
    ".svg",
    ".txt",
    ".webm",
    ".webp",
    ".xls",
    ".xlsx",
    ".xml",
    ".zip",
}

MAX_RETRIES = 3
RETRY_DELAY = 2
DELAY_BETWEEN_REQUESTS = 1
TIMEOUT = 15
MIN_TEXT_LENGTH = 2
MAX_DEPTH = 2
FOLLOW_EXTERNAL_LINKS = False
BLOCKED_PATTERNS = [
    "access denied",
    "request blocked",
    "forbidden",
    "captcha",
    "cloudflare",
    "unusual traffic",
    "verifique que voce nao e um robo",
    "verifique que você não é um robô",
    "acesso negado",
    "nao autorizado",
    "não autorizado",
    "bloqueado",
]


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    return session


def normalize_space(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_url(base_url: str, value: Optional[str]) -> Optional[str]:
    if not value:
        return None

    value = value.strip()
    if not value:
        return None

    absolute = urljoin(base_url, value)
    without_fragment, _fragment = urldefrag(absolute)
    parsed = urlparse(without_fragment)

    if parsed.scheme not in {"http", "https"}:
        return None

    return without_fragment


def ensure_url(value: str) -> str:
    value = value.strip()
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    return normalize_url(value, value) or value


def domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def is_internal_link(base_url: str, href: str) -> bool:
    return domain_of(base_url) == domain_of(href)


def looks_like_html_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return not any(path.endswith(ext) for ext in NON_HTML_EXTENSIONS)


def is_html_response(content_type: str) -> bool:
    if not content_type:
        return True

    content_type = content_type.lower()
    html_markers = ["text/html", "application/xhtml+xml", "text/xml", "application/xml"]
    return any(marker in content_type for marker in html_markers)


def add_meta_value(meta: Dict[str, Any], key: str, value: str) -> None:
    if key not in meta:
        meta[key] = value
        return

    if isinstance(meta[key], list):
        if value not in meta[key]:
            meta[key].append(value)
        return

    if meta[key] != value:
        meta[key] = [meta[key], value]


def parse_srcset(base_url: str, srcset: Optional[str]) -> List[Dict[str, str]]:
    if not srcset:
        return []

    candidates = []
    for raw_part in srcset.split(","):
        part = normalize_space(raw_part)
        if not part:
            continue

        pieces = part.split()
        src = normalize_url(base_url, pieces[0])
        if not src:
            continue

        candidates.append(
            {
                "src": src,
                "descriptor": " ".join(pieces[1:]),
            }
        )

    return candidates


def extract_json_ld(soup: BeautifulSoup) -> List[Any]:
    structured_data = []

    for script in soup.find_all("script"):
        script_type = normalize_space(script.get("type", "")).lower()
        if "ld+json" not in script_type:
            continue

        raw = normalize_space(script.string or script.get_text())
        if not raw:
            continue

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            structured_data.append(
                {
                    "raw": raw,
                    "parse_error": str(exc),
                }
            )
            continue

        if isinstance(parsed, list):
            structured_data.extend(parsed)
        else:
            structured_data.append(parsed)

    return structured_data


def extract_meta(soup: BeautifulSoup) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    meta: Dict[str, Any] = {}
    meta_tags = []

    for tag in soup.find_all("meta"):
        key = tag.get("name") or tag.get("property") or tag.get("http-equiv")
        content = tag.get("content")
        charset = tag.get("charset")

        if charset:
            meta_tags.append({"charset": charset})
            add_meta_value(meta, "charset", charset)
            continue

        if not key or not content:
            continue

        key = normalize_space(key)
        content = normalize_space(content)
        if not key or not content:
            continue

        meta_tags.append({"name": key, "content": content})
        add_meta_value(meta, key, content)

    return meta, meta_tags


def extract_canonical_url(soup: BeautifulSoup, base_url: str) -> Optional[str]:
    for tag in soup.find_all("link", href=True):
        rel = tag.get("rel") or []
        if isinstance(rel, str):
            rel_values = rel.lower().split()
        else:
            rel_values = [str(value).lower() for value in rel]

        if "canonical" in rel_values:
            return normalize_url(base_url, tag.get("href"))

    return None


def extract_headings(soup: BeautifulSoup) -> Dict[str, List[str]]:
    headings: Dict[str, List[str]] = {}

    for level in ["h1", "h2", "h3", "h4", "h5", "h6"]:
        headings[level] = [
            normalize_space(heading.get_text(" ", strip=True))
            for heading in soup.find_all(level)
            if normalize_space(heading.get_text(" ", strip=True))
        ]

    return headings


def extract_links(soup: BeautifulSoup, base_url: str) -> List[Dict[str, Any]]:
    links = []
    seen: Set[Tuple[str, str, str]] = set()

    for tag in soup.find_all("a", href=True):
        href = normalize_url(base_url, tag.get("href"))
        if not href:
            continue

        text = normalize_space(tag.get_text(" ", strip=True))
        title = normalize_space(tag.get("title", ""))
        aria_label = normalize_space(tag.get("aria-label", ""))
        label = text or aria_label or title

        key = (href, label, title)
        if key in seen:
            continue
        seen.add(key)

        rel = tag.get("rel") or []
        if isinstance(rel, str):
            rel_values = rel.split()
        else:
            rel_values = [str(value) for value in rel]

        links.append(
            {
                "text": text,
                "label": label,
                "title": title,
                "href": href,
                "rel": rel_values,
                "is_internal": is_internal_link(base_url, href),
            }
        )

    return links


def extract_images(soup: BeautifulSoup, base_url: str) -> List[Dict[str, Any]]:
    images = []
    seen: Set[Tuple[str, str]] = set()

    for img in soup.find_all("img"):
        src_candidates: List[Dict[str, str]] = []

        for attr in IMAGE_SOURCE_ATTRS:
            raw_value = img.get(attr)
            if not raw_value:
                continue

            if attr.endswith("srcset"):
                for candidate in parse_srcset(base_url, raw_value):
                    src_candidates.append(
                        {
                            "attr": attr,
                            "src": candidate["src"],
                            "descriptor": candidate["descriptor"],
                        }
                    )
            else:
                src = normalize_url(base_url, raw_value)
                if src:
                    src_candidates.append({"attr": attr, "src": src, "descriptor": ""})

        picture_sources = []
        parent = img.parent
        if isinstance(parent, Tag) and parent.name == "picture":
            for source in parent.find_all("source"):
                media = normalize_space(source.get("media", ""))
                source_type = normalize_space(source.get("type", ""))
                for candidate in parse_srcset(base_url, source.get("srcset")):
                    picture_sources.append(
                        {
                            "src": candidate["src"],
                            "descriptor": candidate["descriptor"],
                            "media": media,
                            "type": source_type,
                        }
                    )

        primary_src = src_candidates[0]["src"] if src_candidates else ""
        alt = normalize_space(img.get("alt", ""))
        key = (primary_src, alt)
        if key in seen:
            continue
        seen.add(key)

        images.append(
            {
                "src": primary_src,
                "alt": alt,
                "title": normalize_space(img.get("title", "")),
                "loading": normalize_space(img.get("loading", "")),
                "src_candidates": src_candidates,
                "picture_sources": picture_sources,
            }
        )

    return images


def extract_forms(soup: BeautifulSoup, base_url: str) -> List[Dict[str, Any]]:
    forms = []

    for form in soup.find_all("form"):
        fields = []
        for field in form.find_all(["input", "textarea", "select", "button"]):
            field_data: Dict[str, Any] = {
                "tag": field.name,
                "type": normalize_space(field.get("type", "")),
                "name": normalize_space(field.get("name", "")),
                "id": normalize_space(field.get("id", "")),
                "label": normalize_space(field.get("aria-label", "")),
                "placeholder": normalize_space(field.get("placeholder", "")),
                "value": normalize_space(field.get("value", "")),
                "text": normalize_space(field.get_text(" ", strip=True)),
                "required": field.has_attr("required"),
            }

            if field.name == "select":
                field_data["options"] = [
                    normalize_space(option.get_text(" ", strip=True))
                    for option in field.find_all("option")
                    if normalize_space(option.get_text(" ", strip=True))
                ]

            fields.append(field_data)

        forms.append(
            {
                "method": normalize_space(form.get("method", "get")).lower() or "get",
                "action": normalize_url(base_url, form.get("action")) or base_url,
                "id": normalize_space(form.get("id", "")),
                "name": normalize_space(form.get("name", "")),
                "fields": fields,
            }
        )

    return forms


def should_skip_text_tag(tag: Tag) -> bool:
    if tag.name in CONTAINER_TEXT_TAGS and tag.find(list(LEAF_TEXT_TAGS)):
        return True

    if tag.name == "span" and tag.find_parent(["p", "li", "td", "th", "label", "blockquote"]):
        return True

    return False


def extract_text_blocks(soup: BeautifulSoup) -> Tuple[List[str], List[Dict[str, str]]]:
    seen: Set[str] = set()
    texts = []
    text_blocks = []

    for tag in soup.find_all(TEXT_TAGS):
        if not isinstance(tag, Tag) or should_skip_text_tag(tag):
            continue

        text = normalize_space(tag.get_text(" ", strip=True))
        if len(text) < MIN_TEXT_LENGTH:
            continue

        dedupe_key = text.lower()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        texts.append(text)
        text_blocks.append({"tag": tag.name, "text": text})

    return texts, text_blocks


def detect_blocked_response(status_code: int, title: str, texts: List[str], body_text: str) -> Optional[str]:
    if status_code in {401, 403, 429, 503}:
        return f"HTTP {status_code} indica bloqueio, autenticacao, rate limit ou indisponibilidade"

    haystack = normalize_space(" ".join([title, " ".join(texts[:20]), body_text[:3000]])).lower()
    for pattern in BLOCKED_PATTERNS:
        if pattern in haystack:
            return f"Pagina parece bloqueada por conter marcador: {pattern}"

    if status_code == 200 and len(texts) < 3:
        return "Pouco texto extraido para uma pagina HTML; possivel bloqueio ou pagina vazia"

    return None


def scrape(
    url: str,
    retry_count: int = 0,
    depth: int = 0,
    session: Optional[requests.Session] = None,
) -> Dict[str, Any]:
    session = session or build_session()
    url = ensure_url(url)

    logger.info("Acessando: %s", url)

    try:
        response = session.get(url, timeout=TIMEOUT)
        response.raise_for_status()
    except requests.exceptions.Timeout:
        logger.error("Timeout ao acessar %s", url)
        if retry_count < MAX_RETRIES:
            logger.info(
                "Tentando novamente em %ss (tentativa %s/%s)",
                RETRY_DELAY,
                retry_count + 1,
                MAX_RETRIES,
            )
            time.sleep(RETRY_DELAY)
            return scrape(url, retry_count + 1, depth, session)
        return {"url": url, "error": "Timeout apos multiplas tentativas", "success": False}
    except requests.exceptions.RequestException as exc:
        logger.error("Erro ao acessar %s: %s", url, exc)
        if retry_count < MAX_RETRIES and should_retry(exc):
            logger.info(
                "Tentando novamente em %ss (tentativa %s/%s)",
                RETRY_DELAY,
                retry_count + 1,
                MAX_RETRIES,
            )
            time.sleep(RETRY_DELAY)
            return scrape(url, retry_count + 1, depth, session)
        return {"url": url, "error": str(exc), "success": False}
    except Exception as exc:
        logger.error("Erro inesperado ao acessar %s: %s", url, exc)
        return {"url": url, "error": str(exc), "success": False}

    final_url = response.url
    content_type = response.headers.get("content-type", "")

    if not is_html_response(content_type):
        return {
            "url": url,
            "final_url": final_url,
            "status_code": response.status_code,
            "content_type": content_type,
            "error": "Resposta nao parece ser HTML/XML",
            "success": False,
        }

    if response.apparent_encoding:
        response.encoding = response.apparent_encoding

    try:
        soup = BeautifulSoup(response.text, "html.parser")
    except Exception as exc:
        logger.error("Erro ao fazer parse do HTML de %s: %s", final_url, exc)
        return {"url": url, "error": "Parse error: {}".format(exc), "success": False}

    structured_data = extract_json_ld(soup)
    forms = extract_forms(soup, final_url)

    for tag in soup(IGNORE_TAGS):
        tag.decompose()

    title = normalize_space(soup.title.string if soup.title and soup.title.string else "")
    meta, meta_tags = extract_meta(soup)
    headings = extract_headings(soup)
    links = extract_links(soup, final_url)
    images = extract_images(soup, final_url)
    canonical_url = extract_canonical_url(soup, final_url)
    texts, text_blocks = extract_text_blocks(soup)
    blocked_reason = detect_blocked_response(response.status_code, title, texts, soup.get_text(" ", strip=True))

    data = {
        "url": url,
        "final_url": final_url,
        "canonical_url": canonical_url,
        "status_code": response.status_code,
        "content_type": content_type,
        "encoding": response.encoding,
        "depth": depth,
        "title": title,
        "meta": meta,
        "meta_tags": meta_tags,
        "headings": headings,
        "links": links,
        "images": images,
        "forms": forms,
        "structured_data": structured_data,
        "texts": texts,
        "text_blocks": text_blocks,
        "stats": {
            "total_links": len(links),
            "total_images": len(images),
            "total_forms": len(forms),
            "total_structured_data": len(structured_data),
            "total_texts": len(texts),
        },
        "blocked": blocked_reason is not None,
        "blocked_reason": blocked_reason,
        "success": blocked_reason is None,
    }

    if blocked_reason:
        data["error"] = blocked_reason
        logger.warning("Resposta de %s parece bloqueada: %s", final_url, blocked_reason)
        return data

    logger.info("Scraping de %s concluido", final_url)
    logger.info("Titulo: %s", title or "(sem titulo)")
    logger.info("Links: %s", len(links))
    logger.info("Imagens: %s", len(images))
    logger.info("Textos: %s", len(texts))

    return data


def should_retry(exc: requests.exceptions.RequestException) -> bool:
    retryable_errors = (
        requests.exceptions.ConnectionError,
        requests.exceptions.ChunkedEncodingError,
    )
    return isinstance(exc, retryable_errors)


def keyword_terms(keyword: str) -> List[str]:
    terms = [normalize_space(part).lower() for part in re.split(r"[,;]", keyword)]
    return [term for term in terms if term]


def link_matches_keyword(link: Dict[str, Any], terms: Iterable[str]) -> List[str]:
    haystack = " ".join(
        [
            str(link.get("text", "")),
            str(link.get("label", "")),
            str(link.get("title", "")),
            str(link.get("href", "")),
        ]
    ).lower()

    return [term for term in terms if term in haystack]


def filter_links_by_keyword(links: List[Dict[str, Any]], keyword: str) -> List[Dict[str, Any]]:
    terms = keyword_terms(keyword)
    filtered = []

    for link in links:
        matches = link_matches_keyword(link, terms)
        if not matches:
            continue

        link_data = dict(link)
        link_data["matched_keywords"] = matches
        filtered.append(link_data)

    return filtered


def scrape_with_keyword(
    url: str,
    keyword: str,
    depth: int = 0,
    max_depth: int = MAX_DEPTH,
    visited: Optional[Set[str]] = None,
    session: Optional[requests.Session] = None,
    follow_external: bool = FOLLOW_EXTERNAL_LINKS,
    delay: float = DELAY_BETWEEN_REQUESTS,
) -> Dict[str, Any]:
    if visited is None:
        visited = set()
    session = session or build_session()

    url = ensure_url(url)
    if depth > max_depth:
        return {
            "url": url,
            "success": True,
            "skipped": True,
            "skip_reason": "Limite de profundidade atingido",
        }

    if url in visited:
        return {
            "url": url,
            "success": True,
            "skipped": True,
            "skip_reason": "URL ja visitada",
        }

    visited.add(url)
    data = scrape(url, depth=depth, session=session)

    if not data.get("success", False):
        return data

    filtered_links = filter_links_by_keyword(data.get("links", []), keyword)
    data["filtered_links"] = filtered_links
    data["links_data"] = []
    data["crawl"] = {
        "keyword": keyword,
        "depth": depth,
        "max_depth": max_depth,
        "follow_external": follow_external,
        "matched_links": len(filtered_links),
        "visited_count": len(visited),
    }

    logger.info(
        "Filtrados %s link(s) contendo '%s' em %s",
        len(filtered_links),
        keyword,
        data.get("final_url", url),
    )

    if depth >= max_depth:
        return data

    for index, link in enumerate(filtered_links, 1):
        href = link.get("href")
        if not href:
            continue

        skipped = get_skip_reason(url, href, visited, follow_external)
        if skipped:
            data["links_data"].append(
                {
                    "link_text": link.get("label") or link.get("text"),
                    "link_href": href,
                    "skipped": True,
                    "skip_reason": skipped,
                }
            )
            continue

        logger.info(
            "[%s/%s] Acessando link filtrado: %s",
            index,
            len(filtered_links),
            link.get("label") or href,
        )

        child_data = scrape_with_keyword(
            href,
            keyword,
            depth=depth + 1,
            max_depth=max_depth,
            visited=visited,
            session=session,
            follow_external=follow_external,
            delay=delay,
        )

        data["links_data"].append(
            {
                "link_text": link.get("label") or link.get("text"),
                "link_href": href,
                "content": child_data,
            }
        )

        time.sleep(delay)

    return data


def get_skip_reason(
    current_url: str,
    href: str,
    visited: Set[str],
    follow_external: bool,
) -> Optional[str]:
    if href in visited:
        return "URL ja visitada"

    if not follow_external and not is_internal_link(current_url, href):
        return "Link externo ignorado"

    if not looks_like_html_url(href):
        return "Arquivo nao-HTML ignorado"

    return None


def scrape_multiple(
    urls: List[str],
    output_file: str = "resultado.json",
    keyword: Optional[str] = None,
    max_depth: int = MAX_DEPTH,
    follow_external: bool = FOLLOW_EXTERNAL_LINKS,
    delay: float = DELAY_BETWEEN_REQUESTS,
) -> None:
    results = []
    successful = 0
    failed = 0
    session = build_session()

    logger.info("Iniciando scraping de %s URL(s)", len(urls))

    for index, raw_url in enumerate(urls, 1):
        url = ensure_url(raw_url)
        logger.info("[%s/%s] Processando: %s", index, len(urls), url)

        if keyword:
            result = scrape_with_keyword(
                url,
                keyword,
                max_depth=max_depth,
                session=session,
                follow_external=follow_external,
                delay=delay,
            )
        else:
            result = scrape(url, session=session)

        results.append(result)

        if result.get("success", False):
            successful += 1
        else:
            failed += 1

        if index < len(urls):
            time.sleep(delay)

    save_json(results, output_file)

    logger.info("Resumo final:")
    logger.info("  Total de URLs: %s", len(urls))
    logger.info("  Sucessos: %s", successful)
    logger.info("  Falhas: %s", failed)


def save_json(data: Any, output_file: str) -> None:
    try:
        with open(output_file, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        logger.info("Resultado salvo em %s", output_file)
    except Exception as exc:
        logger.error("Erro ao salvar resultado: %s", exc)


def read_urls_file(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as file:
        return [
            line.strip()
            for line in file
            if line.strip() and not line.strip().startswith("#")
        ]


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scraper generico com modo opcional de crawl por palavra-chave."
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=DEFAULT_URL,
        help="URL ou arquivo .txt com uma URL por linha.",
    )
    parser.add_argument(
        "keyword",
        nargs="*",
        help="Palavra-chave para filtrar e seguir links. Use aspas para frases.",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Arquivo JSON de saida.",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=MAX_DEPTH,
        help="Profundidade maxima ao seguir links filtrados.",
    )
    parser.add_argument(
        "--external",
        action="store_true",
        help="Permite seguir links externos que tambem batam com a palavra-chave.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DELAY_BETWEEN_REQUESTS,
        help="Intervalo, em segundos, entre requisicoes.",
    )
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    keyword = normalize_space(" ".join(args.keyword))
    output_file = args.output or ("resultado_com_filtro.json" if keyword else "resultado.json")

    try:
        if args.target.endswith(".txt"):
            urls = read_urls_file(args.target)
            if not urls:
                logger.error("Arquivo %s vazio", args.target)
                return 1

            scrape_multiple(
                urls,
                output_file=output_file,
                keyword=keyword or None,
                max_depth=args.max_depth,
                follow_external=args.external,
                delay=args.delay,
            )
            return 0

        session = build_session()
        if keyword:
            logger.info("Scraping com filtro: '%s'", keyword)
            result = scrape_with_keyword(
                args.target,
                keyword,
                max_depth=args.max_depth,
                session=session,
                follow_external=args.external,
                delay=args.delay,
            )
        else:
            result = scrape(args.target, session=session)

        save_json(result, output_file)
        return 0
    except KeyboardInterrupt:
        logger.warning("Execucao interrompida pelo usuario")
        return 130
    except Exception as exc:
        logger.exception("Erro inesperado: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
