"""
scripts/bootstrap_cim11_refs.py — Populate CIM-11 reference caches offline.

Usage :
    # From a CSV file with a 'code_cim11' or 'code' column (e.g. seed pipeline)
    python -m scripts.bootstrap_cim11_refs --from-csv data/seed/codes_to_resolve.csv \\
        --release 2024-01 --concurrency 5

    # From explicit codes (smoke test)
    python -m scripts.bootstrap_cim11_refs --codes 1A07.Z,BA00,QD00 --release 2024-01

    # Refresh existing cache (re-resolve everything regardless of TTL)
    python -m scripts.bootstrap_cim11_refs --from-csv ... --force-refresh

    # Upload to S3 after completion
    python -m scripts.bootstrap_cim11_refs --from-csv ... --upload-s3

Strategy :
    1. For each MMS code in the input :
       a. Skip if already cached and not --force-refresh.
       b. Fetch /icd/release/11/{release}/mms/{code} → MMS entity.
       c. Extract foundationReference URI(s) and parent chain.
       d. Fetch each foundation URI → /icd/entity/{id} for label/parents.
       e. INSERT/REPLACE into cim11_linearizations + cim11_foundation.
    2. Optional upload of the SQLite DB to S3 for persistence.

Idempotent : re-running is safe (uses INSERT OR REPLACE).
Resumable : interruption leaves partial state ; next run picks up unfinished codes.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

# Path setup so the script works whether run via -m or directly
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from db.database import get_connection, init_db, set_db_path  # noqa: E402
from modules.mod_ect_browser import _get_who_token  # noqa: E402
from services.foundation import (  # noqa: E402
    FoundationURI, decompose_cluster_string, is_cluster_string,
)


# ─────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────

DEFAULT_RELEASE = "2024-01"  # latest stable release at time of writing
WHO_BASE = "https://id.who.int"


@dataclass
class Stats:
    total_input: int = 0
    cached_skipped: int = 0
    fetched_mms: int = 0
    fetched_foundation: int = 0
    errors: int = 0
    start_time: float = 0.0

    def elapsed(self) -> float:
        return time.time() - self.start_time

    def summary(self) -> str:
        return (
            f"Total input codes: {self.total_input}\n"
            f"  Already cached (skipped) : {self.cached_skipped}\n"
            f"  MMS codes fetched        : {self.fetched_mms}\n"
            f"  Foundation entities fetched : {self.fetched_foundation}\n"
            f"  Errors                   : {self.errors}\n"
            f"Elapsed : {self.elapsed():.1f}s ({self.fetched_mms / max(self.elapsed(), 1):.1f} fetches/s)"
        )


# ─────────────────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────────────────

def _has_lin(con: sqlite3.Connection, release: str, code: str) -> bool:
    return con.execute(
        "SELECT 1 FROM cim11_linearizations WHERE release = ? AND code = ?",
        (release, code),
    ).fetchone() is not None


def _has_foundation(con: sqlite3.Connection, uri: str) -> bool:
    return con.execute(
        "SELECT 1 FROM cim11_foundation WHERE uri = ?", (uri,)
    ).fetchone() is not None


def _upsert_linearization(
    con: sqlite3.Connection,
    *,
    release: str, code: str, uri: str,
    label_fr: Optional[str], label_en: Optional[str],
    parent_code: Optional[str], chapitre: Optional[str],
    foundation_uris: list[str],
    is_stem: bool, is_extension: bool,
    is_terminal: bool, is_category: bool,
) -> None:
    con.execute(
        """INSERT INTO cim11_linearizations
               (release, code, uri, label_fr, label_en, parent_code, chapitre,
                foundation_uris, is_stem, is_extension, is_terminal, is_category, cached_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(release, code) DO UPDATE SET
               uri = excluded.uri,
               label_fr = excluded.label_fr,
               label_en = excluded.label_en,
               parent_code = excluded.parent_code,
               chapitre = excluded.chapitre,
               foundation_uris = excluded.foundation_uris,
               is_stem = excluded.is_stem,
               is_extension = excluded.is_extension,
               is_terminal = excluded.is_terminal,
               is_category = excluded.is_category,
               cached_at = excluded.cached_at""",
        (release, code, uri, label_fr, label_en, parent_code, chapitre,
         json.dumps(foundation_uris, ensure_ascii=False),
         int(is_stem), int(is_extension), int(is_terminal), int(is_category)),
    )


def _upsert_foundation(
    con: sqlite3.Connection,
    *,
    uri: str, entity_id: str,
    label_fr: Optional[str], label_en: Optional[str],
    definition_fr: Optional[str],
    parent_uris: list[str], child_uris: Optional[list[str]],
    kind: Optional[str], is_residual: bool,
    release_seen: str,
) -> None:
    con.execute(
        """INSERT INTO cim11_foundation
               (uri, entity_id, label_fr, label_en, definition_fr,
                parent_uris, child_uris, kind, is_residual,
                release_first_seen, release_last_seen, cached_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(uri) DO UPDATE SET
               label_fr = COALESCE(excluded.label_fr, cim11_foundation.label_fr),
               label_en = COALESCE(excluded.label_en, cim11_foundation.label_en),
               definition_fr = COALESCE(excluded.definition_fr, cim11_foundation.definition_fr),
               parent_uris = excluded.parent_uris,
               child_uris = excluded.child_uris,
               kind = COALESCE(excluded.kind, cim11_foundation.kind),
               is_residual = excluded.is_residual,
               release_last_seen = excluded.release_last_seen,
               cached_at = excluded.cached_at""",
        (uri, entity_id, label_fr, label_en, definition_fr,
         json.dumps(parent_uris, ensure_ascii=False),
         json.dumps(child_uris, ensure_ascii=False) if child_uris is not None else None,
         kind, int(is_residual),
         release_seen, release_seen),
    )


# ─────────────────────────────────────────────────────────────────────────
# WHO API client (async)
# ─────────────────────────────────────────────────────────────────────────

async def _fetch_json(session, url: str, *, token: str, language: str = "fr") -> Optional[dict]:
    """GET a WHO API URL with the standard headers. Returns parsed JSON or None on 4xx/5xx.

    NOTE : WHO API responses sometimes contain `http://` URLs that must be
    rewritten to `https://` before we follow them, otherwise the bearer token
    is stripped on the redirect (security).
    """
    if url.startswith("http://id.who.int"):
        url = "https://" + url[len("http://"):]
    headers = {
        "Accept": "application/json",
        "Accept-Language": language,
        "API-Version": "v2",
        "Authorization": f"Bearer {token}",
    }
    try:
        async with session.get(url, headers=headers, timeout=30) as resp:
            if resp.status == 200:
                return await resp.json()
            elif resp.status == 404:
                return None  # gracefully skip
            else:
                text = await resp.text()
                print(f"  ⚠️  {resp.status} on {url}: {text[:100]}", file=sys.stderr)
                return None
    except Exception as e:
        print(f"  ⚠️  Network error on {url}: {e}", file=sys.stderr)
        return None


def _extract_title(data: dict) -> Optional[str]:
    t = data.get("title")
    if isinstance(t, dict):
        return t.get("@value")
    return None


def _extract_definition(data: dict) -> Optional[str]:
    d = data.get("definition")
    if isinstance(d, dict):
        return d.get("@value")
    return None


def _is_residual_mms(code: str) -> bool:
    """MMS residual codes end in .Z (unspecified) or .Y (other specified)."""
    return code.endswith(".Z") or code.endswith(".Y")


def _extract_chapitre(data: dict, mms_code: str) -> Optional[str]:
    """Best-effort extraction of the chapter from MMS data."""
    chapter = data.get("chapter")
    if chapter:
        return str(chapter)
    # Fallback : first char of code = chapter for chapters 01-09 (numbered) and A-Z
    if mms_code:
        first = mms_code[0]
        return first
    return None


# ─────────────────────────────────────────────────────────────────────────
# Single code resolution
# ─────────────────────────────────────────────────────────────────────────

async def resolve_one_code(
    session, con: sqlite3.Connection,
    release: str, code: str, token: str,
    stats: Stats, force_refresh: bool = False,
) -> bool:
    """Resolve a single MMS code + its foundation URIs into the cache.
    Returns True if anything was fetched ; False if cached or failed.

    WHO API flow (2-step) :
      Step 1 : GET /icd/release/11/{release}/mms/codeinfo/{code}
               → returns {stemId: <entity_url>}
      Step 2 : GET that stemId URL
               → returns full entity (code, title, parent, child, source=foundationReference, classKind, …)
    """
    code = code.strip()
    if not code:
        return False

    if not force_refresh and _has_lin(con, release, code):
        stats.cached_skipped += 1
        return False

    # ── Step 1 : codeinfo → stemId ─────────────────────────────────────
    codeinfo_url = f"{WHO_BASE}/icd/release/11/{release}/mms/codeinfo/{code}"
    info = await _fetch_json(session, codeinfo_url, token=token)
    if info is None or not info.get("stemId"):
        stats.errors += 1
        return False

    stem_url = info["stemId"]

    # ── Step 2 : full MMS entity (fr) ──────────────────────────────────
    mms_data = await _fetch_json(session, stem_url, token=token, language="fr")
    if mms_data is None:
        stats.errors += 1
        return False
    stats.fetched_mms += 1

    fnd_ref = mms_data.get("source") or mms_data.get("foundationReference") or ""
    foundation_uris: list[str] = []
    if fnd_ref:
        foundation_uris.append(fnd_ref)

    # MMS metadata
    label_fr = _extract_title(mms_data)
    parent_uri = None
    parents = mms_data.get("parent")
    if parents and isinstance(parents, list) and parents:
        parent_uri = parents[0]
    parent_code: Optional[str] = None
    if parent_uri:
        # Look up the parent's code by following the chain (best-effort, doesn't require new fetch
        # if we cached it ; otherwise leave None — can be backfilled later).
        row = con.execute(
            "SELECT code FROM cim11_linearizations WHERE uri = ?", (parent_uri,)
        ).fetchone()
        if row:
            parent_code = row[0]

    chapitre = _extract_chapitre(mms_data, code)
    class_kind = mms_data.get("classKind", "")  # 'chapter', 'block', 'category'
    is_residual = _is_residual_mms(code) or class_kind == "residual"
    is_terminal = not bool(mms_data.get("child"))
    is_extension = code.startswith("X") or "extension" in class_kind.lower()
    is_stem = not is_extension
    is_category = class_kind == "category"

    # English label : separate call (different language header)
    mms_en = await _fetch_json(session, stem_url, token=token, language="en")
    label_en = _extract_title(mms_en) if mms_en else None

    _upsert_linearization(
        con, release=release, code=code, uri=stem_url,
        label_fr=label_fr, label_en=label_en,
        parent_code=parent_code, chapitre=chapitre,
        foundation_uris=foundation_uris,
        is_stem=is_stem, is_extension=is_extension,
        is_terminal=is_terminal, is_category=is_category,
    )

    # ── Foundation entity fetch (separate URI) ─────────────────────────
    for f_uri in foundation_uris:
        if not force_refresh and _has_foundation(con, f_uri):
            continue
        try:
            entity_id = FoundationURI.parse(f_uri).entity_id
        except ValueError:
            continue
        f_data_fr = await _fetch_json(session, f_uri, token=token, language="fr")
        f_data_en = await _fetch_json(session, f_uri, token=token, language="en")
        if f_data_fr is None and f_data_en is None:
            continue
        stats.fetched_foundation += 1
        data = f_data_fr or f_data_en
        parent_uris = data.get("parent", []) if data else []
        child_uris = data.get("child") if data else None
        _upsert_foundation(
            con, uri=f_uri, entity_id=entity_id,
            label_fr=_extract_title(f_data_fr) if f_data_fr else None,
            label_en=_extract_title(f_data_en) if f_data_en else None,
            definition_fr=_extract_definition(f_data_fr) if f_data_fr else None,
            parent_uris=parent_uris, child_uris=child_uris,
            kind="extension_value" if is_extension else "entity",
            is_residual=is_residual,
            release_seen=release,
        )
    con.commit()
    return True


# ─────────────────────────────────────────────────────────────────────────
# Batch driver
# ─────────────────────────────────────────────────────────────────────────

async def run_batch(
    con: sqlite3.Connection,
    codes: list[str],
    release: str,
    *,
    concurrency: int = 5,
    force_refresh: bool = False,
    log_every: int = 50,
) -> Stats:
    import aiohttp

    # Expand cluster strings into individual stems/specifiers
    expanded: list[str] = []
    for c in codes:
        c = c.strip()
        if not c:
            continue
        if is_cluster_string(c):
            try:
                for comp in decompose_cluster_string(c):
                    if comp.mms_code not in expanded:
                        expanded.append(comp.mms_code)
            except ValueError:
                expanded.append(c)
        else:
            if c not in expanded:
                expanded.append(c)

    stats = Stats(total_input=len(expanded), start_time=time.time())
    token = _get_who_token()
    if not token:
        raise RuntimeError("Could not obtain WHO API token. Set WHO_CLIENT_ID + WHO_CLIENT_SECRET.")

    sem = asyncio.Semaphore(concurrency)
    timeout = aiohttp.ClientTimeout(total=60)
    connector = aiohttp.TCPConnector(limit=concurrency * 2, force_close=True)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        async def _bounded(code: str, idx: int) -> None:
            async with sem:
                await resolve_one_code(session, con, release, code, token, stats, force_refresh)
            if log_every and (idx + 1) % log_every == 0:
                elapsed = stats.elapsed()
                rate = stats.fetched_mms / max(elapsed, 1)
                pct = (idx + 1) / len(expanded) * 100
                print(f"  [{idx + 1}/{len(expanded)} ({pct:.1f}%)] "
                      f"fetched={stats.fetched_mms} cached={stats.cached_skipped} "
                      f"errors={stats.errors} rate={rate:.1f}/s")

        await asyncio.gather(*(_bounded(c, i) for i, c in enumerate(expanded)))

    return stats


# ─────────────────────────────────────────────────────────────────────────
# Input loaders
# ─────────────────────────────────────────────────────────────────────────

def load_codes_from_csv(path: str, column_candidates: tuple[str, ...] = (
    "code_cim11", "code_cim11_final", "code", "mms_code",
)) -> list[str]:
    """Read codes from a CSV. Detects the column name from a candidate list."""
    codes: list[str] = []
    with open(path, encoding="utf-8", newline="") as f:
        # Try common separators
        sample = f.read(2048)
        f.seek(0)
        sep = ";" if sample.count(";") > sample.count(",") else ","
        reader = csv.DictReader(f, delimiter=sep)
        if reader.fieldnames is None:
            raise ValueError(f"CSV {path} has no header")
        col = next((c for c in column_candidates if c in reader.fieldnames), None)
        if col is None:
            raise ValueError(
                f"CSV {path} has no column among {column_candidates}; "
                f"found: {reader.fieldnames}"
            )
        for row in reader:
            v = (row.get(col) or "").strip()
            if v:
                codes.append(v)
    return codes


# ─────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Bootstrap CIM-11 reference caches")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-csv", help="Path to CSV with a code column")
    src.add_argument("--codes", help="Comma-separated list of MMS codes")
    parser.add_argument("--release", default=DEFAULT_RELEASE,
                        help=f"CIM-11 release (default: {DEFAULT_RELEASE})")
    parser.add_argument("--concurrency", type=int, default=5,
                        help="Parallel HTTP requests (default: 5, respects WHO rate limits)")
    parser.add_argument("--force-refresh", action="store_true",
                        help="Re-resolve codes even if already cached")
    parser.add_argument("--db-path", help="Override SQLite DB path")
    parser.add_argument("--upload-s3", action="store_true",
                        help="Upload the resulting cache file to S3 after completion")
    parser.add_argument("--log-every", type=int, default=50,
                        help="Progress log frequency (codes processed)")
    args = parser.parse_args(argv)

    if args.db_path:
        set_db_path(args.db_path)

    print(f"=== bootstrap_cim11_refs (release={args.release}) ===")

    # Initialize DB if needed
    init_db()
    con = get_connection()

    # Load codes
    if args.from_csv:
        codes = load_codes_from_csv(args.from_csv)
        print(f"Loaded {len(codes)} codes from {args.from_csv}")
    else:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
        print(f"Got {len(codes)} codes from --codes")

    # Run
    stats = asyncio.run(run_batch(
        con, codes, args.release,
        concurrency=args.concurrency,
        force_refresh=args.force_refresh,
        log_every=args.log_every,
    ))
    print("\n=== Stats ===")
    print(stats.summary())

    con.close()

    # Upload to S3 if requested
    if args.upload_s3:
        from db.database import get_db_path
        from utils.s3_storage import s3_upload_db
        print("\n=== Uploading DB to S3 ===")
        ok = s3_upload_db(get_db_path())
        print("✓ S3 upload OK" if ok else "❌ S3 upload failed")

    return 0


if __name__ == "__main__":
    sys.exit(main())
