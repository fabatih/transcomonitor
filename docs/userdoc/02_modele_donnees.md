# Modèle de données

Schéma SQLite à 22 tables. Source : `db/schema_sqlite.sql`. Migration runtime
appliquée par `db/database._migrate()`.

## Tables principales

### `mappings` (cœur)

Une ligne par mapping CIM‑10↔CIM‑11. Colonnes clés :

| Colonne | Sémantique |
|---|---|
| `id` | identifiant interne |
| `direction` | `forward` (CIM‑10 → CIM‑11) ou `reverse` (CIM‑11 → CIM‑10) |
| `source_code` | code CIM‑10 (forward) ou code MMS (reverse) |
| `source_kind` | `cim10_code`, `mms_code` ou `foundation_uri` |
| `source_version_id` | FK vers `nomenclature_versions` |
| `target_kind` | voir [`03_types_valeurs.md`](03_types_valeurs.md) |
| `target_mms_code` | code MMS forward (peut être un cluster `BA00&XN8P1`) |
| `target_cim10_code` | code CIM‑10 reverse |
| `target_label` | **dénormalisé** — libellé cible figé à l'ingest (fallback si JOIN cim11_linearizations échoue) |
| `target_foundation_uris` | JSON array d'URIs fondation (stables inter‑releases) |
| `target_components` | JSON detail des composants d'un cluster |
| `target_release_id` | FK release CIM‑11 cible |
| `relation_type` | voir `relation_types` |
| `fiabilite` | échelle 6 niveaux (TRES_HAUTE → NON_RESOLU) |
| `source_decision` | étape pipeline qui a produit la décision initiale |
| `status` | `propose`, `en_revue`, `valide`, `conteste`, `rejete`, `gele` |
| `current_version_id` | FK `frozen_versions` (tag version) |
| `pipeline_traceability` | JSON intégral étape1→étape5 de la pipeline source |
| `impacts_aval` | JSON (PMSI classant/CMA/expe, indicateurs, registres) |
| `actions_necessaires` | JSON (`demande_OMS_pending`, `decision_nationale_pending`, …) |
| `is_self_validation` | flag binaire si le valideur valide sa propre proposition (§14 #7) |
| `revision` | numéro de version optimiste (incrémenté à chaque update) |

### `cim10_codes` (référentiel)

42 897 codes CIM‑10 FR/PMSI avec :
- `code` (PK), `libelle_fr`, `chapitre` (01..22)
- flags PMSI : `est_classant`, `est_cma`, `niveau_cma` (2/3/4), `est_expe`
- `type_code` : `INTERNATIONAL_BASE` | `INTERNATIONAL_EXTENSION` | `FR_ONLY` | `WHO_POST_2019`

### `cim11_foundation` (entités fondation, stables)

Entités de la fondation CIM‑11 (URIs stables inter‑releases) avec libellés FR/EN,
parents/enfants, kind (entity / chapter / residual / extension_*).

### `cim11_linearizations` (codes MMS, par release)

Codes MMS d'une release donnée avec leurs URIs fondation associées. Peuplé
par `scripts/bootstrap_cim11_refs.py`.

### `mapping_proposals` (historique append-only)

Une ligne par modification d'un mapping (snapshot de l'ancienne valeur).
**Append-only** : pas d'UPDATE/DELETE (triggers).

### `justifications`

Une ligne par justification structurée associée à un mapping :
- `motif` (enum : confirmation_OMS, decision_ANS, consigne_PMSI, arbitrage_expert, postcoord, autre)
- `commentaire` (texte libre)
- `references_` (JSON array `[{type, value, url}]`)
- **`problematique`** (FK applicative `problematique_types.code`, plan §16.7)
- `attached_to_action` (create / edit / validate / contest / reject / …)

### `problematique_types` (typologie paramétrable)

Catalogue géré par les admins via **Admin → Paramètres → Typologie des
problématiques**. Voir [`07_admin_parametres.md`](07_admin_parametres.md).

### `audit_events` (append-only)

Trace de chaque action métier. **Append-only** strict. Voir
[`04_workflows.md`](04_workflows.md).

### `frozen_versions` + `version_mappings_snapshot`

Versions gelées (immuables) avec snapshot complet des mappings au moment du gel.

### `assignment_lists` + `assignments`

Listes de travail (dynamiques ou statiques) affectables aux utilisateurs.
Voir [`10_admin_listes.md`](10_admin_listes.md).

### `users` + `app_config`

Utilisateurs (bcrypt) + paramètres applicatifs key-value avec chiffrement
Fernet des secrets.

## Diagramme simplifié

```
mappings  ──< mapping_proposals (append-only)
   │     ──< justifications ──> problematique_types
   │     ──< mapping_foundation_links ──> cim11_foundation
   │     ──> cim10_codes (source/target)
   │     ──> cim11_linearizations (target)
   │     ──> nomenclature_versions

frozen_versions ──< version_mappings_snapshot

assignment_lists ──< assignments ──> users

audit_events (append-only, références par object_type/id)
```
