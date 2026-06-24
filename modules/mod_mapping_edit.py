"""
modules/mod_mapping_edit.py — Mapping edit form.

Lets a maintainer/validator update a single mapping :
  - Change target (3 modes : mms_simple / mms_cluster / foundation_only / non_mappable)
  - Change relation_type
  - Add structured justification (motif + commentaire + references)
  - Transition status (with role-based permissions)

The 3-mode foundation toggle is implemented here directly (vs being split
across mod-mapping-edit and mod-mapping-edit-foundation as initially planned)
because the logic is tightly coupled.

Workflow guarantees :
  - Old value snapshotted into mapping_proposals (append-only)
  - Audit event written for every change
  - Self-validation (§14 #7) flagged with is_self_validation=1 + audit note
  - Role-based transition enforcement via services.authz
"""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

from shiny import module, reactive, render, ui

from db.database import get_connection
from services import authz
from services.audit import audit_user_action
from services.foundation import (
    decompose_cluster_string, is_cluster_string,
    resolve_cluster_components, resolve_mms_to_foundation,
    sync_mapping_foundation_links, validate_foundation_uri, validate_mms_code,
)


# ─────────────────────────────────────────────────────────────────────────
# Data fetching for a single mapping
# ─────────────────────────────────────────────────────────────────────────

def fetch_mapping(con: sqlite3.Connection, mapping_id: int) -> Optional[dict]:
    """Fetch a single mapping with its labels (source + target, per plan §16.1)
    and version metadata. COALESCE with denormalized target_label as fallback."""
    row = con.execute(
        """SELECT m.*,
                  nv_src.version_label AS source_release,
                  nv_tgt.version_label AS target_release,
                  c_src.libelle_fr     AS source_label_cim10,
                  l_src.label_fr       AS source_label_cim11,
                  COALESCE(c_tgt.libelle_fr, m.target_label) AS target_label_cim10_resolved,
                  COALESCE(l_tgt.label_fr, m.target_label) AS target_label_cim11_resolved
           FROM mappings m
           LEFT JOIN nomenclature_versions nv_src ON nv_src.id = m.source_version_id
           LEFT JOIN nomenclature_versions nv_tgt ON nv_tgt.id = m.target_release_id
           LEFT JOIN cim10_codes c_src ON c_src.code = m.source_code
                                       AND m.source_kind = 'cim10_code'
           LEFT JOIN cim11_linearizations l_src ON l_src.code = m.source_code
                                                AND l_src.release = nv_src.version_label
                                                AND m.source_kind = 'mms_code'
           LEFT JOIN cim10_codes c_tgt ON c_tgt.code = m.target_cim10_code
           LEFT JOIN cim11_linearizations l_tgt ON l_tgt.code = m.target_mms_code
                                                AND l_tgt.release = nv_tgt.version_label
           WHERE m.id = ?""",
        (mapping_id,),
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    # Synthesize convenience fields :
    # source_label : from cim10 (if source_kind=cim10_code) or cim11 (mms_code)
    d["source_label"] = d.get("source_label_cim10") or d.get("source_label_cim11")
    # target_label : depends on direction (forward → MMS ; reverse → CIM-10)
    if d.get("direction") == "forward":
        d["target_label"] = d.get("target_label_cim11_resolved")
    else:
        d["target_label"] = d.get("target_label_cim10_resolved")
    return d


def fetch_proposals(con: sqlite3.Connection, mapping_id: int) -> list[dict]:
    """Return the change history (latest first)."""
    rows = con.execute(
        """SELECT p.*, u.username AS proposer_username
           FROM mapping_proposals p
           LEFT JOIN users u ON u.id = p.proposed_by
           WHERE p.mapping_id = ?
           ORDER BY p.superseded_at DESC, p.id DESC""",
        (mapping_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_justifications(con: sqlite3.Connection, mapping_id: int) -> list[dict]:
    rows = con.execute(
        """SELECT j.*, u.username AS author_username
           FROM justifications j
           LEFT JOIN users u ON u.id = j.created_by
           WHERE j.mapping_id = ?
           ORDER BY j.created_at DESC, j.id DESC""",
        (mapping_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────
# Atomic edit function (transactional, audited)
# ─────────────────────────────────────────────────────────────────────────

def edit_mapping(
    con: sqlite3.Connection,
    *,
    user: dict,
    mapping_id: int,
    target_kind: str,
    target_mms_code: Optional[str],
    target_cim10_code: Optional[str],
    target_foundation_uris: Optional[list[str]],
    target_components: Optional[list[dict]],
    relation_type: Optional[str],
    new_status: Optional[str],
    justification_motif: str,
    justification_commentaire: str,
    justification_references: Optional[list[dict]] = None,
    justification_problematique: Optional[str] = None,
    auto_resolve_foundation_from_mms: bool = True,
    target_release: Optional[str] = None,
) -> dict:
    """Apply an atomic edit to a mapping :
      1. Validate role + transition
      2. Snapshot old values into mapping_proposals (append-only)
      3. Update mappings (new values + revision++)
      4. Resync mapping_foundation_links
      5. Insert justification (mandatory)
      6. Audit event with diff

    Returns a dict with {ok, mapping_id, is_self_validation, new_status}.
    """
    authz.require_authenticated(user)

    # Validate kinds
    if target_kind not in {"mms_simple", "mms_cluster", "foundation_only",
                            "cim10_code", "non_mappable"}:
        raise ValueError(f"invalid target_kind: {target_kind!r}")
    if justification_motif not in {"confirmation_OMS", "decision_ANS", "consigne_PMSI",
                                     "arbitrage_expert", "postcoord", "autre"}:
        raise ValueError(f"invalid justification_motif: {justification_motif!r}")

    # Load current mapping
    current = fetch_mapping(con, mapping_id)
    if current is None:
        raise KeyError(f"mapping not found: id={mapping_id}")

    # Role check : depending on what is being changed
    # - If target/relation changes : require 'edit_mapping' capability
    # - If only status changes : require the appropriate transition capability
    target_changed = (
        target_kind != current["target_kind"]
        or (target_mms_code or None) != (current["target_mms_code"] or None)
        or (target_cim10_code or None) != (current.get("target_cim10_code") or None)
        or (relation_type and relation_type != current["relation_type"])
    )
    if target_changed:
        authz.require_capability(user, "edit_mapping")

    # If status is changing, check transition rules
    final_status = new_status or current["status"]
    is_self = False
    if new_status and new_status != current["status"]:
        authz.require_transition(user, current["status"], new_status)
        if new_status == "valide":
            authz.require_capability(user, "validate_mapping")
            # Self-validation flag (§14 #7) : the user transitioning to 'valide'
            # is the same as the one who proposed the latest change.
            last_proposal_proposed_by = con.execute(
                """SELECT proposed_by FROM mapping_proposals
                   WHERE mapping_id = ? ORDER BY id DESC LIMIT 1""",
                (mapping_id,),
            ).fetchone()
            if last_proposal_proposed_by is not None:
                is_self = authz.is_self_validation(user, last_proposal_proposed_by[0])

    # Validate target consistency
    if target_kind == "mms_simple":
        if not target_mms_code or not validate_mms_code(target_mms_code):
            raise ValueError(f"target_mms_code required for mms_simple, got {target_mms_code!r}")
        if is_cluster_string(target_mms_code):
            raise ValueError(f"target_kind=mms_simple but code is a cluster: {target_mms_code!r}")
    elif target_kind == "mms_cluster":
        if not target_mms_code or not is_cluster_string(target_mms_code):
            raise ValueError(f"target_kind=mms_cluster requires a cluster string, got {target_mms_code!r}")
    elif target_kind == "foundation_only":
        if not target_foundation_uris:
            raise ValueError("target_kind=foundation_only requires target_foundation_uris")
        for uri in target_foundation_uris:
            if not validate_foundation_uri(uri):
                raise ValueError(f"invalid foundation URI: {uri!r}")
    elif target_kind == "cim10_code":
        if not target_cim10_code:
            raise ValueError("target_kind=cim10_code requires target_cim10_code")
    # non_mappable : all targets null

    # Auto-resolve foundation URIs from MMS if requested
    if (auto_resolve_foundation_from_mms
            and target_kind in ("mms_simple", "mms_cluster")
            and target_mms_code
            and not target_foundation_uris):
        release = target_release or current.get("target_release")
        if release:
            try:
                target_foundation_uris = resolve_mms_to_foundation(con, target_mms_code, release)
            except KeyError:
                target_foundation_uris = None  # cache miss — leave NULL

    if (auto_resolve_foundation_from_mms
            and target_kind == "mms_cluster"
            and target_mms_code
            and not target_components):
        release = target_release or current.get("target_release")
        if release:
            try:
                comps = resolve_cluster_components(con, target_mms_code, release)
                target_components = [c.to_dict() for c in comps]
            except KeyError:
                target_components = None

    # Snapshot old values
    con.execute(
        """INSERT INTO mapping_proposals (
                mapping_id, target_kind_old, target_mms_code_old, target_cim10_code_old,
                target_foundation_uris_old, target_components_old,
                relation_type_old, fiabilite_old, source_decision_old, status_old,
                proposed_by, proposed_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (mapping_id,
         current["target_kind"], current["target_mms_code"], current.get("target_cim10_code"),
         current["target_foundation_uris"], current["target_components"],
         current["relation_type"], current["fiabilite"], current["source_decision"],
         current["status"],
         user["id"], "ui_edit"),
    )

    # Resolve target release id if a release was provided
    target_release_id = current["target_release_id"]
    if target_release:
        row = con.execute(
            "SELECT id FROM nomenclature_versions WHERE nomenclature='cim11_mms' AND version_label=?",
            (target_release,),
        ).fetchone()
        if row:
            target_release_id = row[0] if not isinstance(row, sqlite3.Row) else row["id"]
        else:
            cur = con.execute(
                "INSERT INTO nomenclature_versions (nomenclature, version_label) VALUES ('cim11_mms', ?)",
                (target_release,),
            )
            target_release_id = cur.lastrowid

    # Update mapping
    last_validated_by = user["id"] if final_status == "valide" else current["last_validated_by"]
    last_validated_at_clause = (
        ", last_validated_at = datetime('now')" if final_status == "valide" else ""
    )

    con.execute(
        f"""UPDATE mappings SET
                target_kind = ?, target_mms_code = ?, target_cim10_code = ?,
                target_foundation_uris = ?, target_components = ?,
                target_release_id = ?,
                relation_type = ?, status = ?,
                last_validated_by = ?,
                is_self_validation = ?,
                updated_at = datetime('now'), updated_by = ?,
                revision = revision + 1
                {last_validated_at_clause}
            WHERE id = ?""",
        (target_kind,
         target_mms_code if target_kind in ("mms_simple", "mms_cluster") else None,
         target_cim10_code if target_kind == "cim10_code" else None,
         json.dumps(target_foundation_uris, ensure_ascii=False) if target_foundation_uris else None,
         json.dumps(target_components, ensure_ascii=False) if target_components else None,
         target_release_id,
         relation_type or current["relation_type"],
         final_status,
         last_validated_by,
         int(is_self) if final_status == "valide" else 0,
         user["id"],
         mapping_id),
    )

    # Insert justification
    con.execute(
        """INSERT INTO justifications
               (mapping_id, motif, commentaire, references_, attached_to_action, created_by,
                problematique)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (mapping_id, justification_motif, justification_commentaire,
         json.dumps(justification_references, ensure_ascii=False) if justification_references else None,
         "validate" if final_status == "valide" else "edit",
         user["id"],
         justification_problematique),
    )

    # Resync mapping_foundation_links
    sync_mapping_foundation_links(con, mapping_id)

    # Audit
    audit_user_action(
        con, user=user,
        action="validate_mapping" if final_status == "valide" else "edit_mapping",
        object_type="mapping", object_id=mapping_id,
        old_value={
            "target_kind": current["target_kind"],
            "target_mms_code": current["target_mms_code"],
            "target_cim10_code": current.get("target_cim10_code"),
            "relation_type": current["relation_type"],
            "status": current["status"],
        },
        new_value={
            "target_kind": target_kind,
            "target_mms_code": target_mms_code,
            "target_cim10_code": target_cim10_code,
            "relation_type": relation_type or current["relation_type"],
            "status": final_status,
        },
        note=("self-validation" if is_self else None),
    )
    con.commit()

    return {
        "ok": True,
        "mapping_id": mapping_id,
        "is_self_validation": is_self,
        "new_status": final_status,
    }


# ─────────────────────────────────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────────────────────────────────

JUSTIFICATION_MOTIFS = {
    "confirmation_OMS": "Confirmation OMS",
    "decision_ANS":     "Décision ANS",
    "consigne_PMSI":    "Consigne PMSI",
    "arbitrage_expert": "Arbitrage expert",
    "postcoord":        "Post-coordination",
    "autre":            "Autre",
}

RELATION_TYPES = {
    "equivalent":          "Équivalent",
    "plus_large":          "Plus large (broader)",
    "plus_precis":         "Plus précis (narrower)",
    "multiple":            "Mapping multiple (1:n)",
    "composite":           "Cluster post-coordonné",
    "residuel":            "Résiduel (NEC/NOS)",
    "non_mappable":        "Non mappable",
    "necessite_postcoord": "Nécessite post-coordination",
    "ambigu":              "Ambigu",
}

TARGET_KINDS_FORWARD = {
    "mms_simple":      "MMS simple (1 code)",
    "mms_cluster":     "Cluster post-coordonné (stem & specifiers)",
    "foundation_only": "Fondation directe (URIs sans MMS)",
    "non_mappable":    "Non mappable",
}
TARGET_KINDS_REVERSE = {
    "cim10_code":      "Code CIM-10 cible",
    "non_mappable":    "Non mappable",
}


@module.ui
def mapping_edit_ui() -> ui.Tag:
    return ui.div(
        ui.output_ui("edit_form_content"),
    )


@module.server
def mapping_edit_server(
    input, output, session,
    mapping_id: reactive.Value,       # reactive.value(int | None)
    current_user: reactive.Value,     # reactive.value(dict | None)
    on_saved=None,                    # callback : on_saved(mapping_id)
):
    # Renamed from `edit_message` to avoid name collision with the
    # @output @render.ui below (the decorator wraps the function and the
    # reactive.value cannot share the name — caused `TypeError: __call__()
    # missing 1 required positional argument: '_fn'` per plan §16.11).
    _edit_msg_state = reactive.value("")

    @reactive.calc
    def loaded_mapping() -> Optional[dict]:
        mid = mapping_id()
        if mid is None:
            return None
        con = get_connection()
        try:
            return fetch_mapping(con, mid)
        finally:
            con.close()

    @reactive.calc
    def loaded_history() -> tuple[list[dict], list[dict]]:
        mid = mapping_id()
        if mid is None:
            return [], []
        con = get_connection()
        try:
            return fetch_proposals(con, mid), fetch_justifications(con, mid)
        finally:
            con.close()

    @output
    @render.ui
    def edit_form_content():
        m = loaded_mapping()
        user = current_user()
        if m is None:
            return ui.div("Aucun mapping sélectionné.", class_="alert alert-info")
        if user is None:
            return ui.div("Veuillez vous connecter pour éditer.",
                          class_="alert alert-warning")

        is_reverse = m["direction"] == "reverse"
        kind_choices = TARGET_KINDS_REVERSE if is_reverse else TARGET_KINDS_FORWARD

        # Load active problematique types for the selector (plan §16.7)
        from services.problematiques import list_problematiques
        con = get_connection()
        try:
            problematiques = list_problematiques(con, only_active=True)
        finally:
            con.close()
        problematique_choices = {p["code"]: p["libelle"] for p in problematiques}
        if not problematique_choices:
            problematique_choices = {"aucune": "Aucune"}

        # Current foundation URIs (display)
        try:
            current_uris = json.loads(m["target_foundation_uris"]) if m["target_foundation_uris"] else []
        except Exception:
            current_uris = []

        # Read user role for showing/hiding actions
        can_validate = authz.has_capability(user, "validate_mapping")

        return ui.div(
            ui.h4(
                f"Édition mapping #{m['id']} ({m['direction']})",
                ui.tags.small(f" — {m['source_code']}", class_="text-muted ms-2"),
            ),
            # Source → target context with labels (per plan §16.1)
            ui.div(
                ui.div(
                    ui.tags.strong(m['source_code']),
                    ui.tags.small(
                        f" — {m.get('source_label') or '(libellé manquant)'}",
                        class_="text-muted ms-2",
                    ),
                    class_="mb-1",
                ),
                ui.div(
                    ui.tags.i(class_="bi bi-arrow-down me-2 text-muted"),
                    ui.tags.strong(
                        m.get("target_mms_code") or m.get("target_cim10_code") or "(non mappable)"
                    ),
                    ui.tags.small(
                        f" — {m.get('target_label') or '—'}",
                        class_="text-muted ms-2",
                    ),
                    class_="mb-2",
                ),
                class_="p-2 mb-3 border rounded bg-light-subtle",
            ),
            ui.div(
                ui.tags.strong("Statut courant : "),
                ui.HTML(f'<span class="badge bg-info">{m["status"]}</span>'),
                ui.tags.strong(" — Fiabilité : ", class_="ms-3"),
                ui.HTML(f'<span class="badge bg-secondary">{m["fiabilite"] or "—"}</span>'),
                ui.tags.strong(" — Source : ", class_="ms-3"),
                ui.tags.code(m["source_decision"] or "—"),
                class_="mb-3",
            ),
            ui.layout_columns(
                # Left : edit form (with full-width inputs per plan §16.10).
                # Per plan §16.8 : show ONLY the fields relevant to the
                # mapping's direction. To change the source, the user must
                # navigate back to the Forward/Reverse table.
                ui.div(
                    ui.h6("Cible"),
                    # Direction-context hint
                    ui.div(
                        ui.tags.i(class_="bi bi-info-circle me-1"),
                        ui.tags.span(
                            "Pour modifier le code source, retourner dans "
                            "l'onglet Forward/Reverse et sélectionner le bon code source.",
                            class_="small text-muted",
                        ),
                        class_="alert alert-light py-1 px-2 mb-2",
                    ),
                    ui.input_select(
                        "edit_target_kind", "Type de cible",
                        choices=kind_choices,
                        selected=m["target_kind"],
                        width="100%",
                    ),
                    # Forward fields (hidden in reverse but kept in DOM for input tracking)
                    ui.div(
                        # MMS code : click-to-edit widget (plan §16.9 + §16.12).
                        # Display the current code+label as a clickable button
                        # that opens the EB browser pre-filled. The underlying
                        # text input is shown only in "manual edit" mode.
                        ui.tags.label("Code MMS cible", class_="form-label fw-bold"),
                        ui.input_action_button(
                            "edit_open_mms_picker",
                            ui.div(
                                ui.tags.i(class_="bi bi-search me-2"),
                                ui.tags.strong(m["target_mms_code"] or "(cliquer pour choisir)"),
                                ui.tags.small(
                                    f" — {m.get('target_label') or '—'}",
                                    class_="text-muted ms-2",
                                ),
                            ),
                            class_="btn btn-outline-primary text-start w-100 mb-2",
                            style="white-space: normal;",
                        ),
                        ui.input_checkbox(
                            "edit_mms_manual", "Édition manuelle (texte libre)",
                            value=False,
                        ),
                        # Single text input ; hidden when manual edit is OFF.
                        ui.panel_conditional(
                            "input['edit_panel-edit_mms_manual']",
                            ui.input_text(
                                "edit_target_mms", "",
                                value=m["target_mms_code"] or "",
                                placeholder="ex: BA00 ou BA00&XN8P1",
                                width="100%",
                            ),
                        ),
                        ui.input_text_area(
                            "edit_foundation_uris", "URIs fondation (mode expert)",
                            value="\n".join(current_uris),
                            placeholder="http://id.who.int/icd/entity/...",
                            rows=3,
                            width="100%",
                        ),
                        ui.input_text(
                            "edit_release", "Release MMS cible",
                            value=m.get("target_release") or "2024-01",
                            width="100%",
                        ),
                        style="display: none;" if is_reverse else "",
                    ),
                    # Reverse field (hidden in forward but kept in DOM)
                    ui.div(
                        ui.input_text(
                            "edit_target_cim10", "Code CIM-10 cible",
                            value=m.get("target_cim10_code") or "",
                            placeholder="ex: A011",
                            width="100%",
                        ),
                        style="display: none;" if not is_reverse else "",
                    ),
                    ui.input_select(
                        "edit_relation_type", "Type de relation",
                        choices=RELATION_TYPES,
                        selected=m["relation_type"] or "equivalent",
                        width="100%",
                    ),
                    ui.hr(),
                    ui.h6("Justification (obligatoire)"),
                    ui.input_select(
                        "edit_motif", "Motif",
                        choices=JUSTIFICATION_MOTIFS,
                        selected="arbitrage_expert",
                        width="100%",
                    ),
                    # Problematique typology selector (plan §16.7)
                    ui.input_select(
                        "edit_problematique", "Problématique éventuelle",
                        choices=problematique_choices,
                        selected="aucune",
                        width="100%",
                    ),
                    ui.input_text_area(
                        "edit_commentaire", "Commentaire",
                        rows=4,
                        placeholder="Justification structurée du changement…",
                        width="100%",
                    ),
                    ui.input_text(
                        "edit_references", "Références (URLs ou IDs, séparées par virgules)",
                        placeholder="https://…, DOI:10…",
                        width="100%",
                    ),
                    ui.hr(),
                    ui.h6("Action de workflow"),
                    ui.input_select(
                        "edit_new_status", "Nouveau statut",
                        choices={
                            "": "(aucun changement)",
                            "en_revue": "Envoyer en revue",
                            "valide":   "Valider" if can_validate else "Valider (réservé valideurs)",
                            "conteste": "Contester",
                            "rejete":   "Rejeter",
                        },
                        selected="",
                        width="100%",
                    ),
                    ui.div(
                        ui.input_action_button("edit_save", "Enregistrer",
                                                class_="btn btn-primary"),
                        class_="d-flex gap-2 mt-3",
                    ),
                    ui.output_ui("edit_message"),
                    class_="card card-body bg-light",
                ),
                # Right : pipeline trace + history
                ui.div(
                    ui.h6("Traçabilité pipeline"),
                    ui.output_ui("trace_panel"),
                    ui.hr(),
                    ui.h6("Historique des propositions"),
                    ui.output_ui("proposals_panel"),
                    ui.hr(),
                    ui.h6("Justifications existantes"),
                    ui.output_ui("justifications_panel"),
                ),
                col_widths=(6, 6),
            ),
        )

    @output
    @render.ui
    def trace_panel():
        m = loaded_mapping()
        if m is None or not m.get("pipeline_traceability"):
            return ui.div("Aucune trace pipeline.", class_="text-muted small")
        try:
            trace = json.loads(m["pipeline_traceability"])
        except Exception:
            return ui.div("(trace illisible)", class_="text-muted small")
        items = []
        for k in sorted(trace.keys()):
            v = trace[k]
            if v in (None, "", []):
                continue
            v_str = str(v)
            if len(v_str) > 150:
                v_str = v_str[:147] + "…"
            items.append(ui.tags.li(
                ui.tags.code(k, class_="small"), ": ",
                ui.tags.span(v_str, class_="small"),
            ))
        return ui.tags.ul(*items, class_="list-unstyled small mb-0",
                          style="max-height: 250px; overflow-y: auto;")

    @output
    @render.ui
    def proposals_panel():
        proposals, _ = loaded_history()
        if not proposals:
            return ui.div("Aucune modification.", class_="text-muted small")
        rows = []
        for p in proposals[:10]:
            rows.append(ui.tags.li(
                ui.tags.span(p["superseded_at"], class_="small text-muted"),
                " — ",
                ui.tags.strong(p.get("proposer_username") or "(import/système)"),
                ui.tags.span(f" : ancien target = {p.get('target_mms_code_old') or p.get('target_cim10_code_old') or '—'}",
                             class_="small"),
                class_="mb-1",
            ))
        return ui.tags.ul(*rows, class_="list-unstyled small mb-0",
                          style="max-height: 200px; overflow-y: auto;")

    @output
    @render.ui
    def justifications_panel():
        _, justifs = loaded_history()
        if not justifs:
            return ui.div("Aucune justification.", class_="text-muted small")
        rows = []
        for j in justifs[:10]:
            rows.append(ui.tags.li(
                ui.tags.span(j["created_at"], class_="small text-muted"),
                " — ",
                ui.HTML(f'<span class="badge bg-info">{j["motif"]}</span>'),
                " ",
                ui.tags.strong(j.get("author_username") or "?"),
                ui.tags.div((j["commentaire"] or "")[:200], class_="small ms-3"),
                class_="mb-2",
            ))
        return ui.tags.ul(*rows, class_="list-unstyled small mb-0",
                          style="max-height: 250px; overflow-y: auto;")

    @output
    @render.ui
    def edit_message():
        msg = _edit_msg_state()
        if not msg:
            return ui.div()
        ok = msg.startswith("✓")
        return ui.div(
            msg,
            class_=f"alert alert-{'success' if ok else 'danger'} mt-2 py-2 small",
        )

    @reactive.effect
    @reactive.event(input.edit_save)
    def _save():
        user = current_user()
        m = loaded_mapping()
        if user is None or m is None:
            _edit_msg_state.set("✗ Utilisateur ou mapping non chargé")
            return

        try:
            # Build edit payload from inputs
            target_kind = input.edit_target_kind()
            target_mms = (input.edit_target_mms() or "").strip() or None
            target_cim10 = (input.edit_target_cim10() or "").strip() or None
            uris_text = (input.edit_foundation_uris() or "").strip()
            target_foundation_uris = [u.strip() for u in uris_text.splitlines() if u.strip()] if uris_text else None

            refs_text = (input.edit_references() or "").strip()
            refs = None
            if refs_text:
                refs = [{"type": "manual", "value": r.strip()}
                        for r in refs_text.split(",") if r.strip()]

            new_status = input.edit_new_status() or None

            con = get_connection()
            try:
                result = edit_mapping(
                    con, user=user, mapping_id=m["id"],
                    target_kind=target_kind,
                    target_mms_code=target_mms,
                    target_cim10_code=target_cim10,
                    target_foundation_uris=target_foundation_uris,
                    target_components=None,  # auto-resolved for clusters
                    relation_type=input.edit_relation_type(),
                    new_status=new_status,
                    justification_motif=input.edit_motif(),
                    justification_commentaire=input.edit_commentaire() or "",
                    justification_references=refs,
                    justification_problematique=(input.edit_problematique() or "aucune"),
                    target_release=input.edit_release() or None,
                )
            finally:
                con.close()

            extra = " (AUTO-VALIDATION tracée)" if result["is_self_validation"] else ""
            success_msg = f"✓ Enregistré — nouveau statut : {result['new_status']}{extra}"
            _edit_msg_state.set(success_msg)
            # Persistent toast for visibility across re-renders (§16.6 pattern)
            try:
                ui.notification_show(success_msg, type="message", duration=5)
            except Exception:
                pass
            if on_saved is not None:
                on_saved(m["id"])

        except authz.AuthzError as e:
            _edit_msg_state.set(f"✗ Accès refusé : {e}")
        except ValueError as e:
            _edit_msg_state.set(f"✗ Erreur de validation : {e}")
        except Exception as e:
            _edit_msg_state.set(f"✗ Erreur : {type(e).__name__}: {e}")

    # Click on the MMS picker widget → open EB modal pre-filled (plan §16.9 + §16.12)
    @reactive.effect
    @reactive.event(input.edit_open_mms_picker)
    async def _open_eb_with_current_code():
        m = loaded_mapping()
        if m is None:
            return
        # For clusters, we pre-fill on the first stem
        code = m.get("target_mms_code") or ""
        if "&" in code:
            code = code.split("&")[0]
        if "/" in code:
            code = code.split("/")[0]
        try:
            await session.send_custom_message("eb_open_with_code", {"code": code})
        except Exception as e:
            print(f"[mod_mapping_edit] EB open failed : {e}")
