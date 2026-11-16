#!/usr/bin/env python3
"""Deterministic validation and publication for the DSV Dealflow Kanban sync.

The agent remains responsible for semantic decisions and supplies the proposed
Kanban/audit-log edits. This tool owns the repeatable safety gates: parsing,
structural validation, cross-checking the audit log, exact-path Git commits,
fast-forward publication, and remote read-back receipts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ruamel.yaml.error import YAMLError

from tools.registry import registry
from utils import fast_safe_load

REPO_PATH = Path("/home/andrei/Research_Bench")
STATE_ROOT = Path("/home/andrei/.hermes/state/dealflow_kanban_sync")
EXPECTED_ORIGIN = "ssh://git@git.ceesaxp.org:2222/andrei/Research_Bench.git"
KANBAN_REL = Path("DealFlow/DSV_Deals_Kanban.md")
LOG_DIR_REL = Path("DealFlow/sync_logs")
RECEIPT_DIR = STATE_ROOT / "receipts"
REQUIRED_VALIDATION_KEYS = (
    "yaml_frontmatter",
    "unique_company_placement",
    "valid_stage_labels",
    "markdown_structure",
    "git_diff_check",
    "changed_company_rows_read_back",
    "addition_rows_read_back",
    "checkpoint_equals_verified_max_updated_at",
)


class ValidationError(ValueError):
    """A deterministic validation or publication precondition failed."""


def _json(data: Dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _error(message: str, **extra: Any) -> str:
    payload: Dict[str, Any] = {"success": False, "error": message}
    payload.update(extra)
    return _json(payload)


def _run(
    args: Sequence[str],
    *,
    cwd: Path,
    check: bool = True,
    timeout: int = 120,
    env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(args),
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
        env=env,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ValidationError(f"Command failed ({' '.join(args)}): {detail}")
    return result


def _git(
    args: Sequence[str],
    *,
    cwd: Path,
    check: bool = True,
    timeout: int = 120,
    extra_env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    """Run Git with hooks and unsafe external protocols disabled."""
    env = os.environ.copy()
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    if extra_env:
        env.update(extra_env)
    return _run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "protocol.ext.allow=never",
            "-c",
            "user.name=Hermes",
            "-c",
            "user.email=hermes@ceesaxp.org",
            *args,
        ],
        cwd=cwd,
        check=check,
        timeout=timeout,
        env=env,
    )


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _resolve_worktree(raw: str) -> Path:
    if not raw:
        raise ValidationError("worktree_path is required")
    path = Path(raw).expanduser().resolve()
    if not _within(path, STATE_ROOT):
        raise ValidationError(f"Worktree must be under {STATE_ROOT}: {path}")
    if not path.is_dir():
        raise ValidationError(f"Worktree does not exist: {path}")
    if not (path / KANBAN_REL).is_file():
        raise ValidationError(f"Kanban file not found: {path / KANBAN_REL}")
    inside = _git(["rev-parse", "--is-inside-work-tree"], cwd=path).stdout.strip()
    if inside != "true":
        raise ValidationError(f"Not a Git worktree: {path}")
    top_level = Path(_git(["rev-parse", "--show-toplevel"], cwd=path).stdout.strip()).resolve()
    if top_level != path:
        raise ValidationError(f"worktree_path must be the Git worktree root: {path}")
    common_raw = _git(["rev-parse", "--git-common-dir"], cwd=path).stdout.strip()
    common_dir = (path / common_raw).resolve() if not Path(common_raw).is_absolute() else Path(common_raw).resolve()
    expected_common = (REPO_PATH / ".git").resolve()
    if common_dir != expected_common:
        raise ValidationError(
            f"Worktree is not attached to the canonical Research_Bench repository: {common_dir}"
        )
    origin = _git(["remote", "get-url", "origin"], cwd=path).stdout.strip()
    if origin != EXPECTED_ORIGIN:
        raise ValidationError(f"Unexpected origin URL: {origin}")
    return path


def _resolve_audit_log(worktree: Path, raw: Optional[str]) -> Path:
    if raw:
        candidate = Path(raw).expanduser()
        path = candidate.resolve() if candidate.is_absolute() else (worktree / candidate).resolve()
    else:
        logs = sorted((worktree / LOG_DIR_REL).glob("*_nightly_sync.log"), key=lambda p: p.stat().st_mtime)
        if len(logs) != 1:
            raise ValidationError("audit_log_path is required when there is not exactly one nightly sync log")
        path = logs[0].resolve()
    if not _within(path, worktree / LOG_DIR_REL):
        raise ValidationError(f"Audit log must be under {worktree / LOG_DIR_REL}: {path}")
    if not path.is_file():
        raise ValidationError(f"Audit log not found: {path}")
    if not re.fullmatch(r"\d{8}_nightly_sync\.log", path.name):
        raise ValidationError(f"Audit log must match YYYYMMDD_nightly_sync.log: {path.name}")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rfc3339_utc(value: Any, *, field: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            raise ValidationError(f"{field} is missing")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{field} is not RFC-3339: {raw}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValidationError(f"{field} must use UTC: {value}")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _split_frontmatter(text: str) -> Tuple[Dict[str, Any], str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValidationError("Kanban must start with YAML frontmatter")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration as exc:
        raise ValidationError("Kanban frontmatter has no closing delimiter") from exc
    raw = "\n".join(lines[1:end])
    try:
        data = fast_safe_load(raw)
    except YAMLError as exc:
        raise ValidationError(f"Invalid Kanban YAML frontmatter: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError("Kanban frontmatter must be a mapping")
    return data, "\n".join(lines[end + 1 :]), raw


def _extract_allowed_stages(body: str) -> List[str]:
    match = re.search(r"(?ms)^## Columns\s*$\n(.*?)(?=^##\s)", body)
    if not match:
        raise ValidationError("Missing '## Columns' section")
    stages = []
    for line in match.group(1).splitlines():
        item = re.match(r"^\s*-\s+\*\*(.+?)\*\*\s*$", line)
        if item:
            stages.append(item.group(1).strip())
    if not stages:
        raise ValidationError("No canonical stages found in '## Columns'")
    if len(stages) != len(set(stages)):
        raise ValidationError("Duplicate canonical stage labels in '## Columns'")
    return stages


def _cells(line: str) -> List[str]:
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return []
    return [cell.strip() for cell in stripped[1:-1].split("|")]


def _separator(cells: Sequence[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells)


def _normalize_company(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip()).casefold()


def _parse_company_tables(body: str, worktree: Path, allowed_stages: Sequence[str]) -> Dict[str, Any]:
    lines = body.splitlines()
    rows: List[Dict[str, Any]] = []
    structure_errors: List[str] = []
    path_errors: List[str] = []
    missing_paths: List[str] = []
    stage_errors: List[str] = []
    i = 0
    while i < len(lines):
        header = _cells(lines[i])
        if not header or "Company" not in header or "Stage" not in header:
            i += 1
            continue
        if i + 1 >= len(lines) or not _separator(_cells(lines[i + 1])):
            structure_errors.append(f"Table at body line {i + 1} lacks a valid separator row")
            i += 1
            continue
        expected = len(header)
        hmap = {name: idx for idx, name in enumerate(header)}
        i += 2
        while i < len(lines) and lines[i].lstrip().startswith("|"):
            values = _cells(lines[i])
            if _separator(values):
                structure_errors.append(f"Unexpected extra separator at body line {i + 1}")
                i += 1
                continue
            if len(values) != expected:
                structure_errors.append(
                    f"Table row at body line {i + 1} has {len(values)} cells; expected {expected}"
                )
                i += 1
                continue
            company = values[hmap["Company"]].strip()
            stage = values[hmap["Stage"]].strip()
            if not company:
                structure_errors.append(f"Empty company at body line {i + 1}")
            if stage not in allowed_stages:
                stage_errors.append(f"{company or '<empty>'}: invalid stage '{stage}'")
            path_value = None
            if "Research_Bench path" in hmap:
                path_value = values[hmap["Research_Bench path"]].strip().strip("`")
                if not re.fullmatch(r"Companies/.+/Overview\.md", path_value):
                    path_errors.append(f"{company}: malformed Research_Bench path '{path_value}'")
                elif not (worktree / path_value).is_file():
                    missing_paths.append(f"{company}: missing Research_Bench path '{path_value}'")
            rows.append(
                {
                    "company": company,
                    "stage": stage,
                    "path": path_value,
                    "path_exists": bool(path_value and (worktree / path_value).is_file()),
                    "line": i + 1,
                    "header": header,
                    "values": values,
                }
            )
            i += 1
    if not rows:
        structure_errors.append("No company-stage table rows found")
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(_normalize_company(row["company"]), []).append(row)
    duplicates = {
        entries[0]["company"]: [entry["line"] for entry in entries]
        for entries in grouped.values()
        if len(entries) > 1
    }
    return {
        "rows": rows,
        "row_by_company": {key: values[0] for key, values in grouped.items()},
        "duplicates": duplicates,
        "structure_errors": structure_errors,
        "stage_errors": stage_errors,
        "path_errors": path_errors,
        "missing_paths": missing_paths,
    }


def _load_audit(path: Path) -> Dict[str, Any]:
    try:
        data = fast_safe_load(path.read_text(encoding="utf-8"))
    except YAMLError as exc:
        raise ValidationError(f"Invalid audit-log YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError("Audit log must be a YAML mapping")
    return data


def _named_stage(item: Any, *, arrow: bool) -> Tuple[str, str]:
    raw = str(item or "").strip().strip('"')
    if arrow:
        match = re.match(r"^(.+?):\s+.+?\s+->\s+(.+)$", raw)
    else:
        match = re.match(r"^(.+?):\s+(.+)$", raw)
    if not match:
        kind = "stage change" if arrow else "addition"
        raise ValidationError(f"Malformed {kind} entry: {raw}")
    return match.group(1).strip(), match.group(2).strip()


def _named_company(item: Any) -> str:
    raw = str(item or "").strip().strip('"')
    company = raw.split(":", 1)[0].strip()
    if not company:
        raise ValidationError(f"Malformed company entry: {raw}")
    return company


def _validate_audit(
    audit: Dict[str, Any],
    *,
    checkpoint: str,
    parsed: Dict[str, Any],
    audit_rel: str,
    changed_paths: Sequence[str],
) -> Dict[str, Any]:
    errors: List[str] = []
    checks: Dict[str, Any] = {}
    window = audit.get("window") or {}
    try:
        verified_max = _rfc3339_utc(window.get("verified_max_updated_at"), field="window.verified_max_updated_at")
        checkpoint_written = _rfc3339_utc(window.get("checkpoint_written"), field="window.checkpoint_written")
        checks["checkpoint_equals_verified_max_updated_at"] = (
            checkpoint == verified_max == checkpoint_written
        )
        if not checks["checkpoint_equals_verified_max_updated_at"]:
            errors.append(
                "Checkpoint mismatch: Kanban last_sync, verified_max_updated_at, and checkpoint_written must match"
            )
    except ValidationError as exc:
        checks["checkpoint_equals_verified_max_updated_at"] = False
        errors.append(str(exc))

    result = audit.get("kanban_result") or {}
    rows = parsed["rows"]
    by_company = parsed["row_by_company"]
    company_count = len(rows)
    checks["company_count"] = company_count
    if result.get("company_count_after_sync") != company_count:
        errors.append(
            f"Audit company_count_after_sync={result.get('company_count_after_sync')} but parsed {company_count}"
        )

    stage_changes = result.get("stage_changes") or []
    metadata_changes = result.get("metadata_only_changes") or []
    additions = result.get("additions") or []
    disappearances = result.get("disappearances") or []
    expected_counts = {
        "stage_change_count": len(stage_changes),
        "metadata_only_change_count": len(metadata_changes),
        "changed_existing_company_count": len(stage_changes) + len(metadata_changes),
        "additions_count": len(additions),
        "disappearance_count": len(disappearances),
    }
    for field, expected in expected_counts.items():
        if result.get(field) != expected:
            errors.append(f"Audit {field}={result.get(field)} but list length implies {expected}")

    changed_rows_read_back = True
    for item in stage_changes:
        try:
            company, new_stage = _named_stage(item, arrow=True)
        except ValidationError as exc:
            errors.append(str(exc))
            changed_rows_read_back = False
            continue
        row = by_company.get(_normalize_company(company))
        if not row or row["stage"] != new_stage:
            errors.append(f"Stage-change read-back failed for {company}: expected {new_stage}")
            changed_rows_read_back = False
        elif not row.get("path_exists"):
            errors.append(f"Stage-change read-back failed for {company}: Research_Bench path is missing")
            changed_rows_read_back = False
    for item in metadata_changes:
        try:
            company = _named_company(item)
        except ValidationError as exc:
            errors.append(str(exc))
            changed_rows_read_back = False
            continue
        row = by_company.get(_normalize_company(company))
        if not row:
            errors.append(f"Metadata-change read-back failed for missing company: {company}")
            changed_rows_read_back = False
        elif not row.get("path_exists"):
            errors.append(f"Metadata-change read-back failed for {company}: Research_Bench path is missing")
            changed_rows_read_back = False
    for item in disappearances:
        try:
            company = _named_company(item)
        except ValidationError as exc:
            errors.append(str(exc))
            changed_rows_read_back = False
            continue
        if _normalize_company(company) in by_company:
            errors.append(f"Disappearance read-back failed; company still present: {company}")
            changed_rows_read_back = False
    checks["changed_company_rows_read_back"] = changed_rows_read_back

    additions_read_back = True
    for item in additions:
        try:
            company, stage = _named_stage(item, arrow=False)
        except ValidationError as exc:
            errors.append(str(exc))
            additions_read_back = False
            continue
        row = by_company.get(_normalize_company(company))
        if not row or row["stage"] != stage:
            errors.append(f"Addition read-back failed for {company}: expected {stage}")
            additions_read_back = False
        elif not row.get("path_exists"):
            errors.append(f"Addition read-back failed for {company}: Research_Bench path is missing")
            additions_read_back = False
    checks["addition_rows_read_back"] = additions_read_back

    files_changed = audit.get("files_changed") or []
    expected_files = set(changed_paths)
    if set(map(str, files_changed)) != expected_files:
        errors.append(f"Audit files_changed must be exactly {sorted(expected_files)}")

    if str(KANBAN_REL) not in expected_files and any(expected_counts.values()):
        errors.append("Audit-only runs must report zero stage, metadata, addition, and disappearance counts")

    pagination = audit.get("pagination") or {}
    collected = pagination.get("collected_count")
    deduplicated = pagination.get("deduplicated_count")
    boundary = pagination.get("inclusive_boundary_duplicate_count")
    if not all(isinstance(value, int) and value >= 0 for value in (collected, deduplicated, boundary)):
        errors.append("Pagination counts must be non-negative integers")
    elif isinstance(collected, int) and isinstance(deduplicated, int) and isinstance(boundary, int):
        if collected != deduplicated + boundary:
            errors.append("Pagination collected_count must equal deduplicated_count + boundary duplicates")
    if pagination.get("complete") is not True:
        errors.append("Pagination must be marked complete")

    return {"errors": errors, "checks": checks, "counts": expected_counts}


def _git_changed_paths(worktree: Path) -> List[str]:
    result = _git(["status", "--porcelain", "--untracked-files=all"], cwd=worktree)
    paths: List[str] = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        raw = line[3:]
        if " -> " in raw:
            raw = raw.split(" -> ", 1)[1]
        paths.append(raw.strip())
    return sorted(paths)


def _git_diff_check_exact(worktree: Path, paths: Sequence[str]) -> Tuple[bool, str]:
    """Run diff --check against an isolated temporary index, including untracked files."""
    handle = tempfile.NamedTemporaryFile(prefix="dealflow-kanban-index-", delete=False)
    index_path = Path(handle.name)
    handle.close()
    index_path.unlink(missing_ok=True)
    env = {"GIT_INDEX_FILE": str(index_path)}
    try:
        _git(["read-tree", "HEAD"], cwd=worktree, extra_env=env)
        _git(["add", "--", *paths], cwd=worktree, extra_env=env)
        result = _git(
            ["diff", "--cached", "--check", "--", *paths],
            cwd=worktree,
            check=False,
            extra_env=env,
        )
        detail = (result.stdout + result.stderr).strip()
        return result.returncode == 0, detail
    finally:
        index_path.unlink(missing_ok=True)


def _validate(worktree: Path, audit_log: Path) -> Dict[str, Any]:
    kanban_path = worktree / KANBAN_REL
    text = kanban_path.read_text(encoding="utf-8")
    frontmatter, body, _ = _split_frontmatter(text)
    errors: List[str] = []
    checks: Dict[str, Any] = {}

    if frontmatter.get("title") != "DSV Deals Kanban":
        errors.append("Frontmatter title must be 'DSV Deals Kanban'")
    if frontmatter.get("type") != "deals-kanban":
        errors.append("Frontmatter type must be 'deals-kanban'")
    updated = frontmatter.get("updated")
    if not isinstance(updated, (date, str)):
        errors.append("Frontmatter updated must be an ISO date")
    else:
        try:
            date.fromisoformat(str(updated))
        except ValueError:
            errors.append(f"Frontmatter updated is not an ISO date: {updated}")
    checkpoint = _rfc3339_utc(frontmatter.get("last_sync_with_dealflow"), field="last_sync_with_dealflow")
    checks["yaml_frontmatter"] = not errors

    allowed_stages = _extract_allowed_stages(body)
    parsed = _parse_company_tables(body, worktree, allowed_stages)
    errors.extend(parsed["structure_errors"])
    errors.extend(parsed["stage_errors"])
    errors.extend(parsed["path_errors"])
    if parsed["duplicates"]:
        errors.append(f"Duplicate company placement: {parsed['duplicates']}")
    checks["markdown_structure"] = not parsed["structure_errors"] and not parsed["path_errors"]
    checks["valid_stage_labels"] = not parsed["stage_errors"]
    checks["unique_company_placement"] = not parsed["duplicates"]

    audit_rel = str(audit_log.relative_to(worktree))
    changed_paths = _git_changed_paths(worktree)
    allowed_changed_path_sets = (
        [audit_rel],
        sorted([str(KANBAN_REL), audit_rel]),
    )
    if changed_paths not in allowed_changed_path_sets:
        errors.append(
            f"Changed paths must be audit-only or Kanban plus audit log; got {changed_paths}"
        )
    audit = _load_audit(audit_log)
    run_id = str(audit.get("run_id") or "")
    if not re.fullmatch(r"\d{8}T\d{6}Z", run_id):
        errors.append(f"Audit run_id must match YYYYMMDDTHHMMSSZ: {run_id}")
    elif audit_log.name[:8] != run_id[:8]:
        errors.append(f"Audit filename date does not match run_id: {audit_log.name} vs {run_id}")
    audit_result = _validate_audit(
        audit,
        checkpoint=checkpoint,
        parsed=parsed,
        audit_rel=audit_rel,
        changed_paths=changed_paths,
    )
    errors.extend(audit_result["errors"])
    checks.update(audit_result["checks"])

    diff_ok, diff_detail = _git_diff_check_exact(worktree, changed_paths)
    checks["git_diff_check"] = diff_ok
    if not diff_ok:
        errors.append(f"git diff --cached --check failed: {diff_detail}")


    for key in REQUIRED_VALIDATION_KEYS:
        if checks.get(key) is not True:
            errors.append(f"Required validation check did not pass: {key}")

    artifacts = {
        str(KANBAN_REL): _sha256(kanban_path),
        audit_rel: _sha256(audit_log),
    }
    receipt_material = json.dumps(
        {"worktree": str(worktree), "artifacts": artifacts, "checkpoint": checkpoint, "run_id": run_id},
        sort_keys=True,
    ).encode("utf-8")
    receipt = hashlib.sha256(receipt_material).hexdigest()
    return {
        "success": not errors,
        "action": "validate",
        "worktree": str(worktree),
        "audit_log": audit_rel,
        "validation_receipt": receipt,
        "checkpoint": checkpoint,
        "allowed_stages": allowed_stages,
        "company_count": len(parsed["rows"]),
        "counts": audit_result["counts"],
        "checks": checks,
        "changed_paths": changed_paths,
        "artifact_sha256": artifacts,
        "errors": errors,
    }


def _replace_scalar(text: str, key: str, value: str, *, indent: int = 0) -> str:
    prefix = " " * indent
    pattern = re.compile(rf"(?m)^{re.escape(prefix + key)}:\s*.*$")
    replacement = f"{prefix}{key}: {value}"
    if not pattern.search(text):
        raise ValidationError(f"Audit log missing field: {prefix}{key}")
    return pattern.sub(replacement, text, count=1)


def _finalize_validation_fields(audit_log: Path) -> None:
    text = audit_log.read_text(encoding="utf-8")
    text = _replace_scalar(text, "status", "validated_for_publication")
    for key in REQUIRED_VALIDATION_KEYS:
        text = _replace_scalar(text, key, "passed", indent=2)
    audit_log.write_text(text, encoding="utf-8")


def _receipt_path(audit: Dict[str, Any]) -> Path:
    run_id = str(audit.get("run_id") or "")
    if not re.fullmatch(r"\d{8}T\d{6}Z", run_id):
        raise ValidationError(f"Audit run_id must match YYYYMMDDTHHMMSSZ: {run_id}")
    path = (RECEIPT_DIR / f"{run_id}.json").resolve()
    if not _within(path, RECEIPT_DIR):
        raise ValidationError(f"Receipt path escapes receipt directory: {path}")
    return path


def _write_receipt_atomic(path: Path, payload: Dict[str, Any]) -> None:
    if not _within(path, RECEIPT_DIR):
        raise ValidationError(f"Receipt path escapes receipt directory: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temp_path.replace(path)


def _prepare_publication_fields(audit_log: Path, *, branch: str, receipt_path: Path) -> None:
    """Record where final, non-self-referential publication evidence will live."""
    text = audit_log.read_text(encoding="utf-8")
    text = _replace_scalar(text, "status", "publication_evidence_in_tool_receipt")
    replacements = {
        "automation_branch": branch,
        "automation_branch_push": "see_tool_receipt",
        "content_commit": "see_tool_receipt",
        "merge_commit": "atomic_fast_forward_to_master",
        "master_push": "see_tool_receipt",
        "remote_master_commit_at_final_verification": "see_tool_receipt",
        "kanban_blob_at_final_verification": "see_tool_receipt",
        "audit_log_blob_at_final_verification": "see_tool_receipt",
        "remote_blob_verified": "see_tool_receipt",
        "audit_log_verified": "see_tool_receipt",
        "final_audit_commit": "see_tool_receipt",
    }
    for key, value in replacements.items():
        text = _replace_scalar(text, key, value, indent=2)
    if "  tool_receipt_path:" not in text:
        anchor = "  final_audit_commit: see_tool_receipt"
        text = text.replace(anchor, anchor + f"\n  tool_receipt_path: {receipt_path}", 1)
    audit_log.write_text(text, encoding="utf-8")


def _ensure_remote_base(worktree: Path) -> str:
    _git(["fetch", "origin", "master"], cwd=worktree, timeout=180)
    head = _git(["rev-parse", "HEAD"], cwd=worktree).stdout.strip()
    remote = _git(["rev-parse", "origin/master"], cwd=worktree).stdout.strip()
    if head != remote:
        raise ValidationError(
            f"Worktree base is stale: HEAD={head}, origin/master={remote}. Rebase the proposed edits and validate again."
        )
    return remote


def _commit_exact(worktree: Path, paths: Iterable[str], message: str) -> str:
    path_list = list(paths)
    _git(["add", "--", *path_list], cwd=worktree)
    staged = _git(["diff", "--cached", "--name-only"], cwd=worktree).stdout.splitlines()
    if sorted(staged) != sorted(path_list):
        raise ValidationError(f"Staged paths must be exactly {sorted(path_list)}; got {sorted(staged)}")
    diff_check = _git(["diff", "--cached", "--check", "--", *path_list], cwd=worktree, check=False)
    if diff_check.returncode != 0:
        raise ValidationError(f"Staged diff check failed: {(diff_check.stdout + diff_check.stderr).strip()}")
    _git(["commit", "-m", message, "--", *path_list], cwd=worktree)
    return _git(["rev-parse", "HEAD"], cwd=worktree).stdout.strip()


def _remote_master(worktree: Path) -> str:
    remote = _git(["ls-remote", "origin", "refs/heads/master"], cwd=worktree, timeout=180).stdout.strip()
    return remote.split()[0] if remote else ""


def _push_atomic_and_verify(worktree: Path, branch: str, commit: str) -> str:
    _git(
        [
            "push",
            "--atomic",
            "origin",
            f"HEAD:refs/heads/{branch}",
            "HEAD:refs/heads/master",
        ],
        cwd=worktree,
        timeout=180,
    )
    remote_sha = _remote_master(worktree)
    if remote_sha != commit:
        raise ValidationError(f"Remote master read-back mismatch: expected {commit}, got {remote_sha or '<empty>'}")
    return remote_sha


def _finalize_prepared_receipt(worktree: Path, receipt_path: Path, receipt: Dict[str, Any]) -> Dict[str, Any]:
    commit = str(receipt.get("expected_commit") or "")
    branch = str(receipt.get("branch") or "")
    head = _git(["rev-parse", "HEAD"], cwd=worktree).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or head != commit:
        raise ValidationError("Prepared publication receipt does not match worktree HEAD")
    remote = _remote_master(worktree)
    if remote == str(receipt.get("base_commit") or ""):
        remote = _push_atomic_and_verify(worktree, branch, commit)
    elif remote != commit:
        raise ValidationError(
            f"Cannot recover prepared publication: remote master is {remote}, expected base or {commit}"
        )
    receipt.update(
        {
            "success": True,
            "state": "published",
            "action": "publish",
            "final_audit_commit": commit,
            "remote_master": remote,
            "read_back_verified": True,
            "published_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "receipt_path": str(receipt_path),
        }
    )
    _write_receipt_atomic(receipt_path, receipt)
    return receipt


def _publish(
    worktree: Path,
    audit_log: Path,
    *,
    validation_receipt: str,
    commit_message: str,
) -> Dict[str, Any]:
    audit = _load_audit(audit_log)
    receipt_path = _receipt_path(audit)
    if receipt_path.exists():
        existing = json.loads(receipt_path.read_text(encoding="utf-8"))
        if existing.get("state") in {"prepared", "published"}:
            return _finalize_prepared_receipt(worktree, receipt_path, existing)

    before = _validate(worktree, audit_log)
    if not before["success"]:
        raise ValidationError("Validation failed; publication refused")
    if validation_receipt != before["validation_receipt"]:
        raise ValidationError("Validation receipt does not match the current artifact bytes")
    if not commit_message.strip():
        raise ValidationError("commit_message is required")
    _ensure_remote_base(worktree)

    branch = _git(["branch", "--show-current"], cwd=worktree).stdout.strip()
    if not re.fullmatch(r"automation/dealflow-[A-Za-z0-9._-]+", branch):
        raise ValidationError(f"Refusing to publish from non-automation branch: {branch}")

    audit_rel = str(audit_log.relative_to(worktree))
    _finalize_validation_fields(audit_log)
    _prepare_publication_fields(audit_log, branch=branch, receipt_path=receipt_path)
    after_finalize = _validate(worktree, audit_log)
    if not after_finalize["success"]:
        raise ValidationError(f"Finalized artifacts failed validation: {after_finalize['errors']}")

    commit = _commit_exact(
        worktree,
        before["changed_paths"],
        commit_message.strip(),
    )
    kanban_blob = _git(["rev-parse", f"{commit}:{KANBAN_REL}"], cwd=worktree).stdout.strip()
    audit_blob = _git(["rev-parse", f"{commit}:{audit_rel}"], cwd=worktree).stdout.strip()
    receipt = {
        "success": False,
        "state": "prepared",
        "run_id": audit.get("run_id"),
        "worktree": str(worktree),
        "branch": branch,
        "checkpoint": before["checkpoint"],
        "base_commit": _git(["rev-parse", f"{commit}^"], cwd=worktree).stdout.strip(),
        "expected_commit": commit,
        "content_commit": commit,
        "kanban_blob": kanban_blob,
        "audit_log_blob": audit_blob,
        "audit_log": audit_rel,
        "company_count": before["company_count"],
        "counts": before["counts"],
        "validation_receipt": validation_receipt,
        "prepared_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    _write_receipt_atomic(receipt_path, receipt)
    return _finalize_prepared_receipt(worktree, receipt_path, receipt)


def _company_inventory(worktree: Path) -> Dict[str, Any]:
    """Discover actual paths once; absence is data, never a guessed file read.

    Return every Overview (not a search tool's truncated first 50 matches).
    IDs are literal source values; names/folders are candidates, not alias proof.
    No writes, external calls, or automatic onboarding are performed.
    """
    companies = []
    for path in sorted((worktree / "Companies").glob("*/Overview.md")):
        if not _within(path, worktree):
            raise ValidationError(f"Company Overview escapes worktree: {path}")
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        metadata: Dict[str, Any] = {}
        if text.startswith("---\n"):
            try:
                metadata, _, _ = _split_frontmatter(text)
            except ValidationError as exc:
                raise ValidationError(f"{path.relative_to(worktree)}: {exc}") from exc
        ids = metadata["dealflow"] if "dealflow" in metadata else {}
        if not isinstance(ids, dict):
            raise ValidationError(f"Invalid dealflow mapping: {path.relative_to(worktree)}")
        companies.append({
            "path": str(path.relative_to(worktree)),
            "folder": path.parent.name,
            "name": metadata.get("company") or metadata.get("title") or path.parent.name,
            "company_id": ids.get("company_id"),
            "deal_id": ids.get("deal_id"),
        })
    return {"success": True, "action": "company_inventory", "complete": True,
            "company_count": len(companies), "companies": companies,
            "missing_company_policy": "Do not guess or read absent paths. Unmapped deals block publication and checkpoint advancement; report onboarding required. Never create Overview files in this two-artifact sync."}


def dealflow_kanban(
    action: str,
    worktree_path: Optional[str] = None,
    audit_log_path: Optional[str] = None,
    validation_receipt: Optional[str] = None,
    commit_message: Optional[str] = None,
) -> str:
    """Dispatch deterministic Dealflow-to-Kanban operations."""
    action = (action or "").strip().lower()
    try:
        if action == "run_spec":
            return _json(
                {
                    "success": True,
                    "action": "run_spec",
                    "workflow": [
                        "After preparing the fetched worktree, call company_inventory once before any company file reads. Use only returned literal paths; missing companies require onboarding, not guessed read_file probes or checkpoint advancement.",
                        "Use Dealflow MCP read tools for whoami, vocabularies, pagination, and get_deal evidence.",
                        "The agent may resolve semantic aliases and prepare only the Kanban and dated audit-log edits in a state-root worktree.",
                        "Call dealflow_kanban action=validate with the exact worktree and audit-log paths.",
                        "Do not use inline interpreters or hand-written validators. Fix reported artifact errors using native file tools, then validate again.",
                        "Call dealflow_kanban action=publish with the returned validation_receipt and a scoped commit message.",
                        "Treat the publish receipt as the only evidence of commit, push, and remote read-back success.",
                    ],
                    "allowed_worktree_root": str(STATE_ROOT),
                    "changed_files": [str(KANBAN_REL), str(LOG_DIR_REL / "YYYYMMDD_nightly_sync.log")],
                }
            )

        if action not in {"company_inventory", "validate", "publish"}:
            return _error("Unknown action", allowed_actions=["run_spec", "company_inventory", "validate", "publish"])
        worktree = _resolve_worktree(worktree_path or "")
        if action == "company_inventory":
            return _json(_company_inventory(worktree))
        audit_log = _resolve_audit_log(worktree, audit_log_path)
        if action == "validate":
            return _json(_validate(worktree, audit_log))
        return _json(
            _publish(
                worktree,
                audit_log,
                validation_receipt=validation_receipt or "",
                commit_message=commit_message or "",
            )
        )
    except Exception as exc:
        return _error(str(exc), action=action)


def check_dealflow_kanban_requirements() -> bool:
    return REPO_PATH.is_dir() and STATE_ROOT.is_dir()


DEALFLOW_KANBAN_SCHEMA = {
    "name": "dealflow_kanban",
    "description": (
        "Deterministically validate and publish the DSV Dealflow-to-Research_Bench Kanban sync. "
        "Use run_spec before each run; validate parses YAML/Markdown, checks stages, uniqueness, paths, "
        "audit counts/checkpoint/read-back, changed paths, and git diff; publish requires the exact validation "
        "receipt, commits only the Kanban and audit log, fast-forwards remote master, and returns a verified receipt."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["run_spec", "company_inventory", "validate", "publish"],
                "description": "Deterministic operation to run.",
            },
            "worktree_path": {
                "type": "string",
                "description": "Sync worktree under /home/andrei/.hermes/state/dealflow_kanban_sync/.",
            },
            "audit_log_path": {
                "type": "string",
                "description": "Absolute path or worktree-relative dated audit log.",
            },
            "validation_receipt": {
                "type": "string",
                "description": "Exact receipt returned by validate; required by publish.",
            },
            "commit_message": {
                "type": "string",
                "description": "Scoped Git commit message; required by publish.",
            },
        },
        "required": ["action"],
    },
}


registry.register(
    name="dealflow_kanban",
    toolset="dealflow_kanban",
    schema=DEALFLOW_KANBAN_SCHEMA,
    handler=lambda args, **kw: dealflow_kanban(
        action=args.get("action", ""),
        worktree_path=args.get("worktree_path"),
        audit_log_path=args.get("audit_log_path"),
        validation_receipt=args.get("validation_receipt"),
        commit_message=args.get("commit_message"),
    ),
    check_fn=check_dealflow_kanban_requirements,
    emoji="🔄",
)
