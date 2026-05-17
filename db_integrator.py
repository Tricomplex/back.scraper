import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

VENDOR_DIR = Path(__file__).parent / ".vendor"
if VENDOR_DIR.exists():
    sys.path.insert(0, str(VENDOR_DIR))

import mysql.connector
from dotenv import load_dotenv

from llm_interpreter import validate_interpretation


load_dotenv()


DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "tricomplex"),
    "charset": "utf8mb4",
}


def connect():
    return mysql.connector.connect(**DB_CONFIG)


def apply_interpretation(data: dict[str, Any], targets: dict[str, Any], dry_run: bool = True) -> dict[str, Any]:
    validate_interpretation(data, targets)
    if dry_run:
        return plan_interpretation(data, targets)

    conn = connect()
    try:
        conn.start_transaction()
        source_id = _get_or_create_source(conn, data["source"])
        inserted = 0
        skipped = 0
        rule_ids: list[int] = []

        for rule in data["rules"]:
            tributo_id = _get_or_create_tax(conn, rule["tributo"])
            jurisdicao_id = _get_or_create_jurisdiction(conn, rule["jurisdicao"])
            produto_id = _get_or_create_product(conn, rule["produto"])
            existing_id = _find_existing_rule(conn, rule, tributo_id, jurisdicao_id, produto_id, source_id)
            if existing_id:
                skipped += 1
                rule_ids.append(existing_id)
                continue

            rule_ids.append(_insert_rule(conn, rule, tributo_id, jurisdicao_id, produto_id, source_id))
            inserted += 1

        if dry_run:
            conn.rollback()
        else:
            conn.commit()

        return {
            "dry_run": dry_run,
            "mutates_db": True,
            "source_id": source_id,
            "inserted_rules": inserted,
            "planned_insert_rules": inserted,
            "skipped_existing_rules": skipped,
            "rule_ids": rule_ids,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def apply_interpretation_batch(data: dict[str, Any], targets: dict[str, Any], dry_run: bool = True) -> dict[str, Any]:
    interpretations = data.get("interpretations", [])
    if not isinstance(interpretations, list):
        raise ValueError("JSON precisa conter interpretations como lista.")

    results = []
    total_inserted = 0
    total_planned = 0
    total_skipped = 0
    for interpretation in interpretations:
        result = apply_interpretation(interpretation, targets, dry_run=dry_run)
        results.append(result)
        total_inserted += result["inserted_rules"]
        total_planned += result.get("planned_insert_rules", result["inserted_rules"])
        total_skipped += result["skipped_existing_rules"]

    return {
        "dry_run": dry_run,
        "mutates_db": not dry_run,
        "interpretations": len(interpretations),
        "inserted_rules": total_inserted,
        "planned_insert_rules": total_planned,
        "skipped_existing_rules": total_skipped,
        "results": results,
    }


def plan_interpretation(data: dict[str, Any], targets: dict[str, Any]) -> dict[str, Any]:
    validate_interpretation(data, targets)
    conn = connect()
    try:
        source_id = _find_source(conn, data["source"])
        planned = 0
        skipped = 0
        missing_dependencies = 0
        rule_ids: list[int] = []
        planned_rules: list[dict[str, Any]] = []

        for rule in data["rules"]:
            tributo_id = _find_tax(conn, rule["tributo"])
            jurisdicao_id = _find_jurisdiction(conn, rule["jurisdicao"])
            produto_id = _find_product(conn, rule["produto"])

            if source_id and tributo_id and jurisdicao_id and produto_id:
                existing_id = _find_existing_rule(conn, rule, tributo_id, jurisdicao_id, produto_id, source_id)
                if existing_id:
                    skipped += 1
                    rule_ids.append(existing_id)
                    continue
            else:
                missing_dependencies += 1

            planned += 1
            planned_rules.append(
                {
                    "tributo": rule["tributo"],
                    "ncm": rule["produto"]["ncm"],
                    "produto": rule["produto"].get("descricao"),
                    "tipo_regra": rule["tipo_regra"],
                    "aliquota_percentual": rule.get("aliquota_percentual"),
                    "valor_fixo": rule.get("valor_fixo"),
                    "vigencia_inicio": rule["vigencia_inicio"],
                }
            )

        return {
            "dry_run": True,
            "mutates_db": False,
            "source_id": source_id,
            "inserted_rules": 0,
            "planned_insert_rules": planned,
            "skipped_existing_rules": skipped,
            "missing_dependency_rules": missing_dependencies,
            "rule_ids": rule_ids,
            "planned_rules": planned_rules,
        }
    finally:
        conn.close()


def _fetch_one(conn, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    cur = conn.cursor(dictionary=True)
    cur.execute(sql, params)
    row = cur.fetchone()
    cur.close()
    return row


def _execute(conn, sql: str, params: tuple[Any, ...]) -> int:
    cur = conn.cursor()
    cur.execute(sql, params)
    last_id = cur.lastrowid
    cur.close()
    return last_id


def _get_or_create_tax(conn, name: str) -> int:
    existing_id = _find_tax(conn, name)
    if existing_id:
        return existing_id
    return _execute(conn, "INSERT INTO tributos (nome, ativo) VALUES (%s, 1)", (name,))


def _find_tax(conn, name: str) -> int | None:
    row = _fetch_one(conn, "SELECT id FROM tributos WHERE nome = %s", (name,))
    return int(row["id"]) if row else None


def _get_or_create_jurisdiction(conn, data: dict[str, Any]) -> int:
    existing_id = _find_jurisdiction(conn, data)
    if existing_id:
        return existing_id
    return _execute(
        conn,
        """
        INSERT INTO jurisdicoes (tipo, uf, municipio, codigo_ibge, ativo)
        VALUES (%s, %s, %s, %s, 1)
        """,
        (data.get("tipo"), data.get("uf"), data.get("municipio"), data.get("codigo_ibge")),
    )


def _find_jurisdiction(conn, data: dict[str, Any]) -> int | None:
    row = _fetch_one(
        conn,
        """
        SELECT id FROM jurisdicoes
        WHERE tipo = %s AND uf <=> %s AND municipio <=> %s AND codigo_ibge <=> %s
        """,
        (data.get("tipo"), data.get("uf"), data.get("municipio"), data.get("codigo_ibge")),
    )
    return int(row["id"]) if row else None


def _get_or_create_product(conn, data: dict[str, Any]) -> int:
    existing_id = _find_product(conn, data)
    if existing_id:
        return existing_id
    return _execute(
        conn,
        """
        INSERT INTO produtos_fiscais (ncm, descricao, categoria, ativo)
        VALUES (%s, %s, %s, 1)
        """,
        (data["ncm"], data["descricao"], data.get("categoria")),
    )


def _find_product(conn, data: dict[str, Any]) -> int | None:
    row = _fetch_one(
        conn,
        "SELECT id FROM produtos_fiscais WHERE ncm = %s AND descricao = %s",
        (data["ncm"], data["descricao"]),
    )
    return int(row["id"]) if row else None


def _find_source(conn, data: dict[str, Any]) -> int | None:
    row = None
    if data.get("url"):
        row = _fetch_one(conn, "SELECT id FROM fontes_legais WHERE url = %s", (data["url"],))
    if not row:
        row = _fetch_one(
            conn,
            """
            SELECT id FROM fontes_legais
            WHERE titulo = %s AND orgao <=> %s AND data_publicacao <=> %s
            """,
            (data["titulo"], data.get("orgao"), data.get("data_publicacao")),
        )
    if row:
        return int(row["id"])
    return None


def _get_or_create_source(conn, data: dict[str, Any]) -> int:
    existing_id = _find_source(conn, data)
    if existing_id:
        return existing_id
    return _execute(
        conn,
        """
        INSERT INTO fontes_legais
          (tipo, titulo, url, orgao, data_publicacao, texto_relevante, ativo)
        VALUES (%s, %s, %s, %s, %s, %s, 1)
        """,
        (
            data["tipo"],
            data["titulo"],
            data.get("url"),
            data.get("orgao"),
            data.get("data_publicacao"),
            data.get("texto_relevante"),
        ),
    )


def _find_existing_rule(conn, rule: dict[str, Any], tributo_id: int, jurisdicao_id: int, produto_id: int, source_id: int) -> int | None:
    row = _fetch_one(
        conn,
        """
        SELECT id FROM regras_tributarias
        WHERE tributo_id = %s
          AND jurisdicao_id = %s
          AND produto_fiscal_id = %s
          AND fonte_legal_id = %s
          AND tipo_regra = %s
          AND aliquota_percentual <=> %s
          AND valor_fixo <=> %s
          AND unidade_valor <=> %s
          AND vigencia_inicio = %s
          AND vigencia_fim <=> %s
        LIMIT 1
        """,
        (
            tributo_id,
            jurisdicao_id,
            produto_id,
            source_id,
            rule["tipo_regra"],
            _decimal_or_none(rule.get("aliquota_percentual")),
            _decimal_or_none(rule.get("valor_fixo")),
            rule.get("unidade_valor"),
            rule["vigencia_inicio"],
            rule.get("vigencia_fim"),
        ),
    )
    return int(row["id"]) if row else None


def _insert_rule(conn, rule: dict[str, Any], tributo_id: int, jurisdicao_id: int, produto_id: int, source_id: int) -> int:
    return _execute(
        conn,
        """
        INSERT INTO regras_tributarias (
          tributo_id, jurisdicao_id, produto_fiscal_id, fonte_legal_id,
          tipo_regra, aliquota_percentual, valor_fixo, unidade_valor,
          vigencia_inicio, vigencia_fim, ativo, resumo_regra, observacoes,
          substitui_regra_id
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)
        """,
        (
            tributo_id,
            jurisdicao_id,
            produto_id,
            source_id,
            rule["tipo_regra"],
            _decimal_or_none(rule.get("aliquota_percentual")),
            _decimal_or_none(rule.get("valor_fixo")),
            rule.get("unidade_valor"),
            rule["vigencia_inicio"],
            rule.get("vigencia_fim"),
            1 if rule.get("ativo", True) else 0,
            rule.get("resumo_regra"),
            rule.get("observacoes"),
        ),
    )


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))
