"""Advisory contract signals for review-facing change projections."""

from __future__ import annotations

import fnmatch
import re
import tomllib
from http import HTTPStatus
from pathlib import Path
from typing import Any

from .review_standards import project_matched_standards
from .test_gap_config import path_matches_any

AMBIGUOUS_SPEC_RE = re.compile(r"\b(ambiguous|maybe|unclear|tbd|should probably)\b", re.I)
HTTP_STATUS_RE = re.compile(
    r"HTTPStatus\.([A-Z_]+)|\bstatus\s*=\s*(\d{3})|abort\((?:status_code=)?(\d{3})"
)
DOC_RESPONSES_RE = re.compile(r"responses\s*=\s*\{([^}]*)\}", re.S)
SQL_SELECT_RE = re.compile(r"\bselect\b(?P<body>.*?)(?:\bfrom\b|;|\bend\b)", re.I | re.S)
SQL_ALIAS_RE = re.compile(r"(?:\[[^\]]+\]|\w+)(?:\s+as)?\s+(?:\[([^\]]+)\]|(\w+))\s*(?:,|$)", re.I)
TYPED_DICT_RE = re.compile(r"class\s+\w+\s*\([^)]*TypedDict[^)]*\):(?P<body>.*?)(?=\n\S|$)", re.S)
ANNOTATION_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*:", re.M)
TEMP_TABLE_RE = re.compile(r"#actualResults\s*\((?P<body>.*?)\)", re.I | re.S)
TEMP_COL_RE = re.compile(r"^\s*\[?([A-Za-z_]\w*)\]?\s+[A-Za-z]", re.M)
SPROC_NAME_RE = re.compile(r"\b(?:procedure|proc|exec(?:ute)?)\s+(?:dbo\.)?([A-Za-z_]\w*)", re.I)


def build_review_advisories(
    *,
    repo_root: Path | None,
    changed_files: list[str],
    changed_functions: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Build additive advisory lists for compact review context."""
    if repo_root is None:
        return {}
    config = _load_config(repo_root)
    advisories = {
        "synthetic_edges": _synthetic_edges(repo_root, changed_files, config),
        "policy_gaps": _policy_gaps(repo_root, changed_functions, config),
        "contract_shape_mismatches": _shape_mismatches(repo_root, changed_files, config),
        "http_contract_mismatches": _http_mismatches(repo_root, changed_files, config),
        "matched_standards": project_matched_standards(repo_root, changed_files),
        "advisory_reconciliation": _advisory_reconciliation(repo_root, changed_files),
    }
    return {key: value for key, value in advisories.items() if value}


def _load_config(repo_root: Path) -> dict[str, Any]:
    path = repo_root / "pyproject.toml"
    if not path.exists():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return data.get("tool", {}).get("code-review-graph", {})


def _synthetic_edges(
    repo_root: Path,
    changed_files: list[str],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    rules = _rules(config.get("synthetic_edges"))
    rows: list[dict[str, Any]] = []
    for rule in rules:
        source_globs = _as_list(rule.get("source_path_globs") or rule.get("source_paths"))
        target_globs = _as_list(rule.get("target_path_globs") or rule.get("target_paths"))
        source_re = str(rule.get("source_name_regex") or rule.get("source_regex") or "")
        target_re = str(rule.get("target_content_regex") or rule.get("target_regex") or "")
        if not source_globs or not target_globs or not source_re:
            continue
        for source in _matching_changed(changed_files, source_globs):
            source_text = _read(repo_root / source)
            match = re.search(source_re, source_text + "\n" + source, re.I | re.S)
            if not match:
                continue
            name = _first_group(match)
            needle = _expand(target_re, name)
            for target in _iter_files(repo_root, target_globs):
                if target == source:
                    continue
                target_text = _read(repo_root / target)
                if needle and not re.search(needle, target_text + "\n" + target, re.I | re.S):
                    continue
                rows.append({
                    "kind": str(rule.get("kind") or "SYNTHETIC_CONTRACT"),
                    "source_file": source,
                    "target_file": target,
                    "contract": name,
                    "reason": str(rule.get("reason") or "configured synthetic edge"),
                })
    return rows


def _policy_gaps(
    repo_root: Path,
    changed_functions: list[dict[str, Any]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    policy = config.get("review_policies", {}).get("service_integration", {})
    service_globs = _as_list(policy.get("service_path_globs") or policy.get("service_paths"))
    if not service_globs:
        return []
    db_markers = _as_list(policy.get("db_test_markers") or ["pytest.mark.db"])
    test_globs = _as_list(policy.get("test_path_globs") or ["tests/**/*.py"])
    dependency_patterns = _as_list(policy.get("real_dependency_patterns"))
    rows: list[dict[str, Any]] = []
    db_tests = [(path, _read(repo_root / path)) for path in _iter_files(repo_root, test_globs)]
    for func in changed_functions:
        file_path = str(func.get("file_path") or func.get("file") or "")
        if not _matches(file_path, service_globs):
            continue
        name = str(func.get("name") or "")
        source = _read(repo_root / file_path)
        dependency = _first_matching_dependency(source, dependency_patterns)
        if dependency is None:
            continue
        has_db_test = any(
            name in text and any(marker in text for marker in db_markers)
            for _, text in db_tests
        )
        if not has_db_test:
            rows.append({
                "kind": "SERVICE_INTEGRATION_GAP",
                "name": name,
                "file": file_path,
                "dependency": dependency,
                "reason": (
                    f"changed service function calls {dependency} but no configured "
                    f"DB happy-path integration test exercises {name}"
                ),
            })
    return rows


def _shape_mismatches(
    repo_root: Path,
    changed_files: list[str],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    shape = config.get("contract_shapes", {})
    sql_globs = _as_list(shape.get("sql_path_globs") or shape.get("sql_paths"))
    wrapper_globs = _as_list(shape.get("wrapper_path_globs") or shape.get("wrapper_paths"))
    test_globs = _as_list(shape.get("test_path_globs") or shape.get("test_paths"))
    if not sql_globs or not wrapper_globs:
        return []
    rows: list[dict[str, Any]] = []
    for sql_path in _matching_changed(changed_files, sql_globs):
        sql_text = _read(repo_root / sql_path)
        contract = _contract_name(sql_text, sql_path)
        sql_cols = _sql_columns(sql_text)
        wrapper_path, wrapper_cols = _find_columns(
            repo_root, wrapper_globs, contract, _typed_dict_columns,
        )
        test_path, test_cols = _find_columns(repo_root, test_globs, contract, _temp_table_columns)
        mismatch = _first_diff(sql_cols, wrapper_cols or test_cols)
        if mismatch is None:
            continue
        rows.append({
            "contract": contract,
            "sql_file": sql_path,
            "wrapper_file": wrapper_path,
            "test_file": test_path,
            "sql_columns": sql_cols,
            "wrapper_columns": wrapper_cols,
            "test_columns": test_cols,
            "first_differing_index": mismatch,
        })
    return rows


def _http_mismatches(
    repo_root: Path,
    changed_files: list[str],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    http = config.get("http_contracts", {})
    route_globs = _as_list(http.get("route_path_globs") or http.get("route_paths"))
    test_globs = _as_list(http.get("test_path_globs") or http.get("test_paths"))
    if not route_globs:
        return []
    rows: list[dict[str, Any]] = []
    tests = [_read(repo_root / path) for path in _iter_files(repo_root, test_globs)]
    tested = {code for text in tests for code in re.findall(r"\bstatus_code\s*==\s*(\d{3})", text)}
    for route_path in _matching_changed(changed_files, route_globs):
        text = _read(repo_root / route_path)
        returned = sorted(_http_statuses(text))
        documented = sorted(_documented_statuses(text))
        missing_docs = [code for code in returned if code not in documented]
        missing_tests = [code for code in returned if code not in tested]
        if missing_docs or missing_tests:
            rows.append({
                "route_file": route_path,
                "returned_statuses": returned,
                "documented_responses": documented,
                "tested_statuses": sorted(tested),
                "missing_documented_responses": missing_docs,
                "missing_status_tests": missing_tests,
            })
    return rows


def _advisory_reconciliation(repo_root: Path, changed_files: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rel in changed_files:
        if not fnmatch.fnmatch(rel, "*.md") and not fnmatch.fnmatch(rel, "*.txt"):
            continue
        text = _read(repo_root / rel)
        match = AMBIGUOUS_SPEC_RE.search(text)
        if match:
            rows.append({
                "file": rel,
                "kind": "AMBIGUOUS_TEXT_RECONCILIATION",
                "evidence": match.group(0),
                "advisory": "Ambiguous text needs source corroboration before becoming a finding.",
            })
    return rows


def _rules(section: Any) -> list[dict[str, Any]]:
    if isinstance(section, list):
        return [row for row in section if isinstance(row, dict)]
    if isinstance(section, dict):
        rules = section.get("rules", [])
        return [row for row in rules if isinstance(row, dict)]
    return []


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _matching_changed(changed_files: list[str], globs: list[str]) -> list[str]:
    return [path for path in changed_files if _matches(path, globs)]


def _matches(path: str, globs: list[str]) -> bool:
    if path_matches_any(path, tuple(globs)):
        return True
    normalized = path.replace("\\", "/")
    return any(
        "**/" in pattern and fnmatch.fnmatch(normalized, pattern.replace("**/", ""))
        for pattern in globs
    )


def _iter_files(repo_root: Path, globs: list[str]) -> list[str]:
    paths: set[str] = set()
    for pattern in globs:
        for path in repo_root.glob(pattern):
            if path.is_file():
                paths.add(path.relative_to(repo_root).as_posix())
    return sorted(paths)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _first_group(match: re.Match[str]) -> str:
    if match.groupdict():
        return next(iter(match.groupdict().values()))
    return next((group for group in match.groups() if group), match.group(0))


def _expand(pattern: str, name: str) -> str:
    return pattern.replace("{source_name}", re.escape(name)).replace("{contract}", re.escape(name))


def _first_matching_dependency(source: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, source)
        if match:
            return _first_group(match)
    return None


def _contract_name(text: str, path: str) -> str:
    match = SPROC_NAME_RE.search(text)
    if match:
        return match.group(1)
    return Path(path).stem


def _sql_columns(text: str) -> list[str]:
    match = SQL_SELECT_RE.search(text)
    if not match:
        return []
    body = re.sub(r"--.*", "", match.group("body"))
    columns: list[str] = []
    for part in body.split(","):
        alias = SQL_ALIAS_RE.search(part.strip() + ",")
        token = alias.group(1) or alias.group(2) if alias else part.strip().split(".")[-1]
        columns.append(token.strip(" []\n\t"))
    return [col for col in columns if col]


def _typed_dict_columns(text: str) -> list[str]:
    match = TYPED_DICT_RE.search(text)
    if not match:
        return []
    return ANNOTATION_RE.findall(match.group("body"))


def _temp_table_columns(text: str) -> list[str]:
    match = TEMP_TABLE_RE.search(text)
    if not match:
        return []
    return TEMP_COL_RE.findall(match.group("body"))


def _find_columns(
    repo_root: Path,
    globs: list[str],
    contract: str,
    extractor,
) -> tuple[str | None, list[str]]:
    if not globs:
        return None, []
    contract_l = contract.lower()
    for rel in _iter_files(repo_root, globs):
        text = _read(repo_root / rel)
        if contract_l not in (text + "\n" + rel).lower():
            continue
        columns = extractor(text)
        if columns:
            return rel, columns
    return None, []


def _first_diff(left: list[str], right: list[str]) -> int | None:
    for index, (lval, rval) in enumerate(zip(left, right)):
        if lval.lower() != rval.lower():
            return index
    if len(left) != len(right):
        return min(len(left), len(right))
    return None


def _http_statuses(text: str) -> set[str]:
    codes: set[str] = set()
    for enum, explicit, abort_code in HTTP_STATUS_RE.findall(text):
        if enum:
            codes.add(str(HTTPStatus[enum].value))
        elif explicit or abort_code:
            codes.add(explicit or abort_code)
    return codes


def _documented_statuses(text: str) -> set[str]:
    statuses: set[str] = set()
    for match in DOC_RESPONSES_RE.finditer(text):
        statuses.update(re.findall(r"\b(\d{3})\b", match.group(1)))
    return statuses
