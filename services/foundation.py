"""
services/foundation.py — CIM-11 foundation URI helpers

Cœur de la dualité linéarisation MMS / foundation décrite au plan §5.

Responsabilités :
  - parse_uri / build_uri pour les URIs WHO (foundation et linéarisation MMS)
  - decompose_cluster_string : décompose 'BA00&XN8P1' ou '1G40/1B5Z' en composants
  - resolve_mms_to_foundation : à partir d'un code MMS (ou cluster), renvoie la liste
    des URIs fondation associées, en lisant le cache local cim11_linearizations.
  - sync_mapping_foundation_links : maintient mapping_foundation_links cohérent
    avec mappings.target_foundation_uris et target_components.
  - validate_foundation_uri : sanity check (URI bien formée, foundation/linéarisation)

Ces helpers sont DB-agnostiques (sqlite3 ici, portable SQLAlchemy plus tard).
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence
from urllib.parse import urlparse

# ─────────────────────────────────────────────────────────────────────────
# URI patterns
# ─────────────────────────────────────────────────────────────────────────

WHO_HOST = "id.who.int"

# Foundation : http://id.who.int/icd/entity/{entity_id}
_RE_FOUNDATION_URI = re.compile(
    r"^https?://id\.who\.int/icd/entity/(?P<entity_id>\d+)$"
)
# Linearization MMS : http://id.who.int/icd/release/11/{release}/mms/{code}
# {release} = '2026-01' | '2025-01' | …
# {code}    = 'BA00' | '1A07' | 'XN8P1' (stem ou extension — pas de cluster ici)
_RE_LIN_MMS_URI = re.compile(
    r"^https?://id\.who\.int/icd/release/11/(?P<release>[0-9]{4}-[0-9]{2})/mms/(?P<code>[A-Z0-9]+)$"
)

# Cluster CIM-11 : 'BA00' (simple), 'BA00&XN8P1' (stem+spec), '1G40/1B5Z' (multi-stem)
# Validation : alphanum + caractères '.', '/', '&' (et chaînes alphanum séparées)
_RE_CLUSTER_TOKEN = re.compile(r"^[A-Z0-9.]+$")


# ─────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FoundationURI:
    """Parsed foundation URI (stable, release-independent)."""
    uri: str
    entity_id: str

    @classmethod
    def parse(cls, uri: str) -> "FoundationURI":
        m = _RE_FOUNDATION_URI.match(uri.strip())
        if not m:
            raise ValueError(f"Not a valid foundation URI: {uri!r}")
        return cls(uri=uri.strip(), entity_id=m.group("entity_id"))

    @classmethod
    def from_entity_id(cls, entity_id: str) -> "FoundationURI":
        return cls(uri=f"http://id.who.int/icd/entity/{entity_id}", entity_id=str(entity_id))


@dataclass(frozen=True)
class LinearizationURI:
    """Parsed MMS linearization URI (release-dependent)."""
    uri: str
    release: str
    code: str  # stem or extension code, NOT a cluster

    @classmethod
    def parse(cls, uri: str) -> "LinearizationURI":
        m = _RE_LIN_MMS_URI.match(uri.strip())
        if not m:
            raise ValueError(f"Not a valid MMS linearization URI: {uri!r}")
        return cls(uri=uri.strip(), release=m.group("release"), code=m.group("code"))

    @classmethod
    def build(cls, release: str, code: str) -> "LinearizationURI":
        if not _RE_CLUSTER_TOKEN.match(code):
            raise ValueError(f"Invalid MMS code token: {code!r}")
        if not re.match(r"^[0-9]{4}-[0-9]{2}$", release):
            raise ValueError(f"Invalid release format: {release!r} (expected YYYY-MM)")
        return cls(uri=f"http://id.who.int/icd/release/11/{release}/mms/{code}",
                   release=release, code=code)


@dataclass
class ClusterComponent:
    """One component of a post-coordinated cluster mapping."""
    position: int
    role: str                       # 'stem' | 'specifier'
    mms_code: str                   # 'BA00' or 'XN8P1'
    lin_uri: Optional[str] = None   # populated when resolved
    foundation_uri: Optional[str] = None
    axis: Optional[str] = None      # 'hasManifestation', 'hasSeverity', …

    def to_dict(self) -> dict:
        d = {"position": self.position, "role": self.role, "mms_code": self.mms_code}
        if self.lin_uri:        d["lin_uri"] = self.lin_uri
        if self.foundation_uri: d["foundation_uri"] = self.foundation_uri
        if self.axis:           d["axis"] = self.axis
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ClusterComponent":
        return cls(
            position=d.get("position", 0),
            role=d.get("role", "stem"),
            mms_code=d["mms_code"],
            lin_uri=d.get("lin_uri"),
            foundation_uri=d.get("foundation_uri"),
            axis=d.get("axis"),
        )


# ─────────────────────────────────────────────────────────────────────────
# Cluster decomposition
# ─────────────────────────────────────────────────────────────────────────

def decompose_cluster_string(cluster: str) -> list[ClusterComponent]:
    """Decompose a CIM-11 cluster string into ordered components.

    Conventions OMS (cf. transcodage/docs/dictionnaire_donnees.md) :
      - 'BA00'             → 1 stem (mms_simple style — single component)
      - 'BA00&XN8P1'       → 1 stem + 1+ specifiers (post-coordination)
      - '1G40/1B5Z'        → 2+ stems combined (multi-stem)
      - '1A00&XN8P1/1B5Z'  → multi-stem with specifiers attached to first stem

    Heuristic: '/' separates stems (multi-stem), '&' separates a stem from its
    specifiers. We split first on '/', then within each part on '&'. The first
    token of each '/'-segment is the stem; subsequent tokens are specifiers.
    """
    cluster = cluster.strip()
    if not cluster:
        raise ValueError("empty cluster string")

    components: list[ClusterComponent] = []
    pos = 0

    for segment in cluster.split("/"):
        tokens = segment.split("&")
        if not tokens or not tokens[0]:
            raise ValueError(f"invalid cluster segment: {segment!r}")
        # First token = stem
        stem_token = tokens[0].strip()
        if not _RE_CLUSTER_TOKEN.match(stem_token):
            raise ValueError(f"invalid stem token: {stem_token!r}")
        components.append(ClusterComponent(position=pos, role="stem", mms_code=stem_token))
        pos += 1
        for spec_token in tokens[1:]:
            spec_token = spec_token.strip()
            if not _RE_CLUSTER_TOKEN.match(spec_token):
                raise ValueError(f"invalid specifier token: {spec_token!r}")
            components.append(ClusterComponent(position=pos, role="specifier", mms_code=spec_token))
            pos += 1

    return components


def assemble_cluster_string(components: Sequence[ClusterComponent]) -> str:
    """Inverse of decompose_cluster_string: rebuild a cluster code string.

    Rebuilds using '&' between a stem and its specifiers, and '/' between
    successive stems. Components MUST be position-ordered.
    """
    if not components:
        raise ValueError("no components")
    parts: list[str] = []
    current_seg: list[str] = []
    for c in components:
        if c.role == "stem":
            if current_seg:
                parts.append("&".join(current_seg))
            current_seg = [c.mms_code]
        elif c.role == "specifier":
            if not current_seg:
                raise ValueError("specifier before any stem")
            current_seg.append(c.mms_code)
        else:
            raise ValueError(f"unknown component role: {c.role!r}")
    if current_seg:
        parts.append("&".join(current_seg))
    return "/".join(parts)


def is_cluster_string(s: str) -> bool:
    """True if the string contains '&' or '/' (post-coordinated or multi-stem)."""
    return "&" in s or "/" in s


# ─────────────────────────────────────────────────────────────────────────
# MMS → foundation resolution (uses local cache cim11_linearizations)
# ─────────────────────────────────────────────────────────────────────────

def resolve_mms_to_foundation(
    con: sqlite3.Connection,
    mms_code: str,
    release: str,
) -> list[str]:
    """Return the list of foundation URIs referenced by a MMS code (single
    or cluster) in a given release.

    Reads cim11_linearizations.foundation_uris (JSON array). If the code is
    a cluster, decompose it and aggregate URIs of all components. Order is
    preserved (stem first, then specifiers, multi-stems in order).

    Raises KeyError if any component is not found in the local cache.
    """
    components = decompose_cluster_string(mms_code) if is_cluster_string(mms_code) else [
        ClusterComponent(position=0, role="stem", mms_code=mms_code.strip())
    ]
    uris: list[str] = []
    for c in components:
        row = con.execute(
            "SELECT foundation_uris FROM cim11_linearizations WHERE release = ? AND code = ?",
            (release, c.mms_code),
        ).fetchone()
        if row is None:
            raise KeyError(f"MMS code not cached: release={release!r} code={c.mms_code!r}")
        cached = json.loads(row[0]) if row[0] else []
        for u in cached:
            if u not in uris:
                uris.append(u)
    return uris


def resolve_cluster_components(
    con: sqlite3.Connection,
    mms_code: str,
    release: str,
) -> list[ClusterComponent]:
    """Decompose a cluster code and enrich each component with lin_uri +
    foundation_uri from the local cache.

    Returns a list of ClusterComponent ready to be JSON-serialized into
    mappings.target_components.

    Raises KeyError if any component is missing from cim11_linearizations.
    """
    components = decompose_cluster_string(mms_code) if is_cluster_string(mms_code) else [
        ClusterComponent(position=0, role="stem", mms_code=mms_code.strip())
    ]
    enriched: list[ClusterComponent] = []
    for c in components:
        row = con.execute(
            "SELECT uri, foundation_uris FROM cim11_linearizations WHERE release = ? AND code = ?",
            (release, c.mms_code),
        ).fetchone()
        if row is None:
            raise KeyError(f"MMS code not cached: release={release!r} code={c.mms_code!r}")
        lin_uri = row[0]
        foundation_uris = json.loads(row[1]) if row[1] else []
        # Primary foundation URI = first one (stems are 1:1 typically)
        primary_fnd = foundation_uris[0] if foundation_uris else None
        enriched.append(ClusterComponent(
            position=c.position, role=c.role, mms_code=c.mms_code,
            lin_uri=lin_uri, foundation_uri=primary_fnd, axis=c.axis,
        ))
    return enriched


# ─────────────────────────────────────────────────────────────────────────
# Foundation links table maintenance
# ─────────────────────────────────────────────────────────────────────────

def sync_mapping_foundation_links(con: sqlite3.Connection, mapping_id: int) -> int:
    """Re-derive mapping_foundation_links rows for one mapping from its
    target_foundation_uris + target_components JSON fields.

    Idempotent : deletes existing rows for this mapping_id then re-inserts.
    Returns the number of links written.

    Handles BOTH directions :
      - forward : foundation URIs come from target side (target_foundation_uris,
        target_components). Roles : primary / component_stem / component_specifier.
      - reverse : foundation URIs come from source side. We resolve source_code
        (which is a MMS code) against cim11_linearizations using source_version_id.
        If source_kind='foundation_uri', source_code IS already a URI.
        Role : primary.

    Roles assigned :
      - 'primary'             : mms_simple / foundation_only / all reverse direction sources
      - 'component_stem'      : each stem in target_components (forward cluster)
      - 'component_specifier' : each specifier in target_components (forward cluster)
      - 'residual'            : reserved for future NEC/NOS fallback
    """
    row = con.execute(
        """SELECT direction, source_code, source_kind, source_version_id,
                  target_kind, target_foundation_uris, target_components
           FROM mappings WHERE id = ?""",
        (mapping_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"mapping not found: id={mapping_id}")

    # Tuple-safe extraction (works for Row and tuple rows)
    direction          = row[0]
    source_code        = row[1]
    source_kind        = row[2]
    source_version_id  = row[3]
    target_kind        = row[4]
    target_foundation_uris_raw = row[5]
    target_components_raw      = row[6]

    foundation_uris = json.loads(target_foundation_uris_raw) if target_foundation_uris_raw else []
    components_raw  = json.loads(target_components_raw) if target_components_raw else []

    entries: list[tuple[str, str, int]] = []

    # ── Forward direction : foundation URIs from target side ─────────────
    if direction == "forward":
        if target_kind == "mms_cluster" and components_raw:
            for c_dict in components_raw:
                c = ClusterComponent.from_dict(c_dict)
                if c.foundation_uri:
                    role = "component_stem" if c.role == "stem" else "component_specifier"
                    entries.append((c.foundation_uri, role, c.position))
        elif target_kind in ("mms_simple", "foundation_only"):
            for i, uri in enumerate(foundation_uris):
                entries.append((uri, "primary", i))

    # ── Reverse direction : foundation URIs from source side ─────────────
    elif direction == "reverse":
        if source_kind == "foundation_uri":
            # source_code IS the foundation URI
            if validate_foundation_uri(source_code):
                entries.append((source_code, "primary", 0))
        elif source_kind == "mms_code" and source_version_id is not None:
            # Resolve source MMS code → foundation URIs via cache
            release_row = con.execute(
                "SELECT version_label FROM nomenclature_versions WHERE id = ?",
                (source_version_id,),
            ).fetchone()
            if release_row is not None:
                release = release_row[0]
                try:
                    uris = resolve_mms_to_foundation(con, source_code, release)
                    for i, uri in enumerate(uris):
                        entries.append((uri, "primary", i))
                except KeyError:
                    # Source MMS not cached — skip silently (link can be re-synced later)
                    pass

    # Idempotent replace
    con.execute("DELETE FROM mapping_foundation_links WHERE mapping_id = ?", (mapping_id,))
    con.executemany(
        """INSERT INTO mapping_foundation_links (mapping_id, foundation_uri, role, position)
           VALUES (?, ?, ?, ?)""",
        [(mapping_id, u, r, p) for (u, r, p) in entries],
    )
    con.commit()
    return len(entries)


def get_mappings_referencing_foundation(
    con: sqlite3.Connection,
    foundation_uri: str,
    direction: Optional[str] = None,
) -> list[sqlite3.Row]:
    """Inverse lookup : return all mappings referencing a given foundation URI.

    Useful pour : "Quels mappings CIM-10 référencent cette entité fondation ?"
    Indispensable pour la maintenance (impact d'une évolution de fondation).
    """
    sql = """
        SELECT m.id, m.direction, m.source_code, m.target_kind, m.target_mms_code,
               m.relation_type, m.fiabilite, m.status,
               mfl.role AS link_role, mfl.position AS link_position
        FROM mapping_foundation_links mfl
        JOIN mappings m ON m.id = mfl.mapping_id
        WHERE mfl.foundation_uri = ?
    """
    params: list = [foundation_uri]
    if direction:
        sql += " AND m.direction = ?"
        params.append(direction)
    sql += " ORDER BY m.direction, m.source_code, mfl.position"
    return con.execute(sql, params).fetchall()


# ─────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────

def validate_foundation_uri(uri: str) -> bool:
    """Strict syntactic validation. Does NOT check existence in WHO API."""
    try:
        FoundationURI.parse(uri)
        return True
    except ValueError:
        return False


def validate_lin_uri(uri: str) -> bool:
    try:
        LinearizationURI.parse(uri)
        return True
    except ValueError:
        return False


def validate_mms_code(code: str) -> bool:
    """Validate a MMS code or cluster string (syntactic only)."""
    try:
        decompose_cluster_string(code)
        return True
    except ValueError:
        return False
