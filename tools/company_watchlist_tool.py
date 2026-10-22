#!/usr/bin/env python3
"""Company watchlist management tool for Andrei's daily company news brief.

This tool intentionally handles durable state only: watchlist CRUD, validation,
and dedup-log operations. It also emits a structured run spec for generate /
dry_run so an agent can perform web_search/web_extract and use this tool to
check/record dedup entries. It does not perform web research itself.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import fcntl

from tools.registry import registry

_LEGACY_ROOT = Path(os.environ.get("HERMES_COMPANY_WATCHLIST_ROOT", Path.home() / "openclaw"))
DEFAULT_WATCHLIST_PATH = Path(os.environ.get(
    "HERMES_COMPANY_WATCHLIST_PATH",
    _LEGACY_ROOT / "watchlists/companies_news_watchlist.json",
))
DEFAULT_DEDUP_LOG_PATH = Path(os.environ.get(
    "HERMES_COMPANY_DEDUP_LOG_PATH",
    _LEGACY_ROOT / "memory/company-watchlist-log.md",
))
DEDUP_MODULE_DIR = Path(os.environ.get(
    "HERMES_COMPANY_DEDUP_MODULE_DIR",
    _LEGACY_ROOT / "memory",
))

ARTICLE_TYPES = {
    "earnings",
    "product_launch",
    "product_release",
    "partnership",
    "acquisition",
    "funding",
    "exec_change",
    "regulatory",
    "exec_compensation",
    "software_update",
    "marketplace",
    "other",
}


def _json(data: Dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _error(message: str, **extra: Any) -> str:
    payload = {"success": False, "error": message}
    payload.update(extra)
    return _json(payload)


def _watchlist_path(path: Optional[str] = None) -> Path:
    return Path(path).expanduser() if path else DEFAULT_WATCHLIST_PATH


def _dedup_log_path(path: Optional[str] = None) -> Path:
    return Path(path).expanduser() if path else DEFAULT_DEDUP_LOG_PATH


def _load_watchlist(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Watchlist not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Watchlist root must be a JSON object")
    if "companies" not in data or not isinstance(data["companies"], list):
        raise ValueError("Watchlist must contain a 'companies' list")
    return data


def _save_watchlist(path: Path, data: Dict[str, Any]) -> None:
    data["updatedAt"] = date.today().isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")


def _normalize_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _company_matches(company: Dict[str, Any], needle: str) -> bool:
    n = needle.strip().lower()
    names = [company.get("name", ""), *company.get("tickers", []), *company.get("aliases", [])]
    return any(str(x).strip().lower() == n for x in names)


def _find_company(data: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    for company in data.get("companies", []):
        if _company_matches(company, name):
            return company
    return None


def _import_dedup():
    if str(DEDUP_MODULE_DIR) not in sys.path:
        sys.path.insert(0, str(DEDUP_MODULE_DIR))
    from watchlist_dedup import WatchlistDedup  # type: ignore

    return WatchlistDedup


def _dedup(log_path: Path):
    WatchlistDedup = _import_dedup()
    return WatchlistDedup(str(log_path))


@contextmanager
def _mutation_lock(path: Path):
    """Serialize read-check-write mutations across concurrent tool callers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(str(path) + ".lock")
    with lock_path.open("a", encoding="utf-8") as handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def _validate_watchlist(data: Dict[str, Any]) -> List[str]:
    problems: List[str] = []
    seen_names = set()
    seen_aliases: Dict[str, str] = {}
    for idx, company in enumerate(data.get("companies", []), start=1):
        if not isinstance(company, dict):
            problems.append(f"companies[{idx}] is not an object")
            continue
        name = str(company.get("name", "")).strip()
        if not name:
            problems.append(f"companies[{idx}] missing name")
        elif name.lower() in seen_names:
            problems.append(f"duplicate company name: {name}")
        else:
            seen_names.add(name.lower())
        for key in ("tickers", "aliases"):
            if key not in company:
                problems.append(f"{name or 'companies[' + str(idx) + ']'} missing {key}")
            elif not isinstance(company[key], list):
                problems.append(f"{name or idx} {key} must be a list")
        for alias in [name, *_normalize_list(company.get("tickers")), *_normalize_list(company.get("aliases"))]:
            a = alias.lower()
            if not a:
                continue
            owner = seen_aliases.get(a)
            if owner and owner != name:
                problems.append(f"duplicate ticker/alias across watchlist: {alias} ({owner} and {name})")
            else:
                seen_aliases[a] = name
    return problems


def _dedup_entries(log_path: Path) -> List[Dict[str, Any]]:
    d = _dedup(log_path)
    return list(getattr(d, "entries", []))


def company_watchlist(
    action: str,
    name: Optional[str] = None,
    tickers: Optional[List[str]] = None,
    aliases: Optional[List[str]] = None,
    notes: Optional[str] = None,
    article_type: Optional[str] = None,
    headline: Optional[str] = None,
    url: Optional[str] = None,
    days: int = 7,
    limit: int = 20,
    watchlist_path: Optional[str] = None,
    dedup_log_path: Optional[str] = None,
) -> str:
    """Dispatch company watchlist/dedup actions."""
    action = (action or "").strip().lower()
    wl_path = _watchlist_path(watchlist_path)
    log_path = _dedup_log_path(dedup_log_path)

    try:
        if action in {"watchlist", "list"}:
            data = _load_watchlist(wl_path)
            companies = data.get("companies", [])
            return _json({
                "success": True,
                "path": str(wl_path),
                "updatedAt": data.get("updatedAt"),
                "count": len(companies),
                "companies": companies,
            })

        if action == "add_company":
            if not name or not name.strip():
                return _error("add_company requires name")
            with _mutation_lock(wl_path):
                data = _load_watchlist(wl_path)
                company = {
                    "name": name.strip(),
                    "tickers": _normalize_list(tickers),
                    "aliases": _normalize_list(aliases) or [name.strip()],
                    "notes": (notes or "").strip(),
                }
                identifiers = [company["name"], *company["tickers"], *company["aliases"]]
                collision = next((value for value in identifiers if _find_company(data, value)), None)
                if collision:
                    return _error(
                        f"Company name, ticker, or alias already exists: {collision}",
                        conflicting_identifier=collision,
                    )
                data.setdefault("companies", []).append(company)
                problems = _validate_watchlist(data)
                if problems:
                    return _error("Proposed company would make the watchlist invalid", problems=problems)
                data["companies"] = sorted(data["companies"], key=lambda c: c.get("name", "").lower())
                _save_watchlist(wl_path, data)
            return _json({"success": True, "action": "add_company", "company": company, "path": str(wl_path)})

        if action == "remove_company":
            if not name or not name.strip():
                return _error("remove_company requires name")
            with _mutation_lock(wl_path):
                data = _load_watchlist(wl_path)
                before = len(data.get("companies", []))
                removed = [c for c in data.get("companies", []) if _company_matches(c, name)]
                data["companies"] = [c for c in data.get("companies", []) if not _company_matches(c, name)]
                if len(data["companies"]) == before:
                    return _error(f"No company matched: {name}")
                _save_watchlist(wl_path, data)
            return _json({"success": True, "action": "remove_company", "removed": removed, "path": str(wl_path)})

        if action == "validate":
            data = _load_watchlist(wl_path)
            problems = _validate_watchlist(data)
            dedup_ok = True
            dedup_error = None
            try:
                d = _dedup(log_path)
                _ = getattr(d, "entries", [])  # validates parse without mutating the log
            except Exception as e:  # pragma: no cover - defensive
                dedup_ok = False
                dedup_error = str(e)
            return _json({
                "success": not problems and dedup_ok,
                "watchlist_path": str(wl_path),
                "dedup_log_path": str(log_path),
                "watchlist_count": len(data.get("companies", [])),
                "problems": problems,
                "dedup_ok": dedup_ok,
                "dedup_error": dedup_error,
            })

        if action in {"dedup_stats", "stats"}:
            d = _dedup(log_path)
            stats = d.get_stats() if hasattr(d, "get_stats") else {"total_entries": len(getattr(d, "entries", []))}
            return _json({"success": True, "path": str(log_path), "stats": stats})

        if action == "dedup_recent":
            entries = _dedup_entries(log_path)
            cutoff = datetime.utcnow().date() - timedelta(days=max(0, int(days)))
            recent = []
            for e in entries:
                raw = e.get("included_in_brief") or e.get("date") or ""
                try:
                    entry_date = datetime.strptime(raw, "%Y-%m-%d").date()
                except ValueError:
                    continue
                if entry_date >= cutoff:
                    recent.append(e)
            recent.sort(key=lambda e: (e.get("included_in_brief", ""), e.get("company", "")), reverse=True)
            return _json({"success": True, "path": str(log_path), "days": days, "count": len(recent), "entries": recent[:limit]})

        if action == "check_duplicate":
            if not name or not article_type or not headline:
                return _error("check_duplicate requires name, article_type, and headline")
            if article_type not in ARTICLE_TYPES:
                return _error(f"Invalid article_type: {article_type}", allowed_article_types=sorted(ARTICLE_TYPES))
            d = _dedup(log_path)
            include = d.should_include(company=name, article_type=article_type, headline=headline)
            return _json({"success": True, "should_include": include, "duplicate": not include})

        if action == "record_included":
            if not name or not article_type or not headline or not url:
                return _error("record_included requires name, article_type, headline, and url")
            if article_type not in ARTICLE_TYPES:
                return _error(f"Invalid article_type: {article_type}", allowed_article_types=sorted(ARTICLE_TYPES))
            with _mutation_lock(log_path):
                d = _dedup(log_path)
                if not d.should_include(company=name, article_type=article_type, headline=headline):
                    return _error(
                        "Article is already present inside the deduplication window",
                        duplicate=True,
                        recorded=False,
                    )
                d.add_entry(name, article_type, headline, url)
                d.save()
            return _json({"success": True, "action": "record_included", "company": name, "article_type": article_type, "headline": headline, "url": url})

        if action == "purge_dedup":
            with _mutation_lock(log_path):
                d = _dedup(log_path)
                before = len(getattr(d, "entries", []))
                d.save()
                after = len(getattr(d, "entries", []))
            return _json({"success": True, "path": str(log_path), "entries_before": before, "entries_after": after})

        if action in {"generate", "dry_run"}:
            data = _load_watchlist(wl_path)
            dry = action == "dry_run"
            return _json({
                "success": True,
                "action": action,
                "dry_run": dry,
                "watchlist_path": str(wl_path),
                "dedup_log_path": str(log_path),
                "companies": data.get("companies", []),
                "instructions": [
                    "Use web_search for each company using canonical name, tickers, and 2-3 aliases.",
                    "Use web_extract/web_fetch on promising results; do not include search snippets alone.",
                    "Only include high-signal events in the last 24h: earnings/filings, major launches/releases, M&A/funding, major partnerships/contracts, legal/regulatory actions, exec changes, material security incidents, meaningful marketplace/platform policy changes.",
                    "Before including a candidate, call company_watchlist action=check_duplicate with company, article_type, and headline; skip duplicates.",
                    "If the item is included and this is not dry_run, call company_watchlist action=record_included with company, article_type, headline, and url.",
                    "If no items are included and this is not dry_run, call company_watchlist action=purge_dedup so old entries can purge.",
                    "Final brief should be 5-10 bullets total; each bullet: Company — one sentence what happened + why it matters; source: URL",
                    "If nothing high-signal is found, say exactly: No high-signal updates in the last 24h.",
                ],
                "allowed_article_types": sorted(ARTICLE_TYPES),
            })

        return _error(f"Unknown action: {action}", allowed_actions=[
            "generate", "dry_run", "watchlist", "add_company", "remove_company", "validate",
            "dedup_stats", "dedup_recent", "check_duplicate", "record_included", "purge_dedup",
        ])
    except Exception as e:
        return _error(str(e))


def check_company_watchlist_requirements() -> bool:
    return DEFAULT_WATCHLIST_PATH.exists() and (DEDUP_MODULE_DIR / "watchlist_dedup.py").exists()


COMPANY_WATCHLIST_SCHEMA = {
    "name": "company_watchlist",
    "description": (
        "Manage Andrei's companies news watchlist and dedup log for the daily watchlist brief. "
        "Actions: generate/dry_run return a structured run spec for web research; watchlist lists tracked companies; "
        "add_company/remove_company edit the watchlist; validate checks files; dedup_stats/dedup_recent inspect history; "
        "check_duplicate and record_included enforce the 7-day dedup window."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "generate", "dry_run", "watchlist", "add_company", "remove_company", "validate",
                    "dedup_stats", "dedup_recent", "check_duplicate", "record_included", "purge_dedup",
                ],
                "description": "Command to run.",
            },
            "name": {"type": "string", "description": "Company name or exact ticker/alias for add/remove/dedup actions."},
            "tickers": {"type": "array", "items": {"type": "string"}, "description": "Tickers for add_company, e.g. ['NASDAQ:AAPL']."},
            "aliases": {"type": "array", "items": {"type": "string"}, "description": "Aliases/product names for add_company."},
            "notes": {"type": "string", "description": "Optional notes for add_company."},
            "article_type": {"type": "string", "enum": sorted(ARTICLE_TYPES), "description": "Article type for dedup check/record."},
            "headline": {"type": "string", "description": "Article headline for dedup check/record."},
            "url": {"type": "string", "description": "Source URL for record_included."},
            "days": {"type": "integer", "description": "Lookback window for dedup_recent.", "default": 7},
            "limit": {"type": "integer", "description": "Max entries returned by dedup_recent.", "default": 20},
            "watchlist_path": {"type": "string", "description": "Optional override watchlist JSON path."},
            "dedup_log_path": {"type": "string", "description": "Optional override dedup log path."},
        },
        "required": ["action"],
    },
}


registry.register(
    name="company_watchlist",
    toolset="company_watchlist",
    schema=COMPANY_WATCHLIST_SCHEMA,
    handler=lambda args, **kw: company_watchlist(
        action=args.get("action", ""),
        name=args.get("name"),
        tickers=args.get("tickers"),
        aliases=args.get("aliases"),
        notes=args.get("notes"),
        article_type=args.get("article_type"),
        headline=args.get("headline"),
        url=args.get("url"),
        days=args.get("days", 7),
        limit=args.get("limit", 20),
        watchlist_path=args.get("watchlist_path"),
        dedup_log_path=args.get("dedup_log_path"),
    ),
    check_fn=check_company_watchlist_requirements,
    emoji="📰",
)
