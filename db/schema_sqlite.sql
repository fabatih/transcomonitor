-- ═══════════════════════════════════════════════════════════════════════════
-- transcomonitor — Schéma SQLite (MVP)
-- ═══════════════════════════════════════════════════════════════════════════
-- Plateforme ATIH de maintenance du transcodage CIM-10 FR/PMSI ↔ CIM-11.
--
-- Architecture conçue pour être portée 1:1 vers PostgreSQL en V1 :
--   - INTEGER PK AUTOINCREMENT → BIGSERIAL
--   - TEXT JSON-as-string      → JSONB
--   - CHECK CONSTRAINTS        → identiques
--   - WITHOUT ROWID            → non utilisé (compat plus large)
--
-- Sections :
--   1. Users / Authentication
--   2. Référentiels CIM-10 (FR/PMSI)
--   3. Référentiels CIM-11 (Foundation + Linearizations MMS)
--   4. Types de relations & sources de décision
--   5. Versions de nomenclatures
--   6. Mappings (cœur métier — forward + reverse)
--   7. Proposals (historique append-only des mappings)
--   8. Justifications structurées
--   9. Mapping ↔ Foundation links (vue dérivée pour requêtes inverses)
--  10. Assignment lists & assignments
--  11. Frozen versions & snapshots
--  12. Import batches & diffs
--  13. Audit events (append-only)
--  14. Rules engine (V2 scaffolding)
--  15. App config (key-value)
-- ═══════════════════════════════════════════════════════════════════════════

PRAGMA foreign_keys = ON;

-- ─────────────────────────────────────────────────────────────────────────
-- 1. Users / Authentication
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT    NOT NULL UNIQUE,
    password_hash   TEXT    NOT NULL,
    role            TEXT    NOT NULL DEFAULT 'mainteneur'
                    CHECK (role IN ('admin', 'mainteneur', 'valideur')),
    active          INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    email           TEXT,
    full_name       TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    last_login      TEXT
);

CREATE INDEX IF NOT EXISTS idx_users_role ON users (role) WHERE active = 1;

-- ─────────────────────────────────────────────────────────────────────────
-- 2. Référentiels CIM-10 (FR/PMSI)
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS cim10_codes (
    code                    TEXT    NOT NULL PRIMARY KEY,        -- ex: 'A011' (sans point)
    libelle_fr              TEXT    NOT NULL,
    chapitre                TEXT,                                 -- ex: '01' à '22' + 'XX'
    est_classant            INTEGER NOT NULL DEFAULT 0 CHECK (est_classant IN (0, 1)),
    est_cma                 INTEGER NOT NULL DEFAULT 0 CHECK (est_cma IN (0, 1)),
    niveau_cma              INTEGER CHECK (niveau_cma IS NULL OR niveau_cma IN (2, 3, 4)),
    est_expe                INTEGER NOT NULL DEFAULT 0 CHECK (est_expe IN (0, 1)),
    type_code               TEXT    CHECK (type_code IS NULL OR type_code IN (
                                'INTERNATIONAL_BASE',
                                'INTERNATIONAL_EXTENSION',
                                'FR_ONLY',
                                'WHO_POST_2019'
                            )),
    parent_international    TEXT,                                 -- code parent 3-4 car. (extensions)
    type_extension          TEXT,                                 -- ID modifieur ClaML
    is_terminal             INTEGER NOT NULL DEFAULT 1 CHECK (is_terminal IN (0, 1)),
    is_category             INTEGER NOT NULL DEFAULT 0 CHECK (is_category IN (0, 1)),
    cim10_version           TEXT    NOT NULL DEFAULT '2026'      -- année ATIH
);

CREATE INDEX IF NOT EXISTS idx_cim10_chapitre ON cim10_codes (chapitre);
CREATE INDEX IF NOT EXISTS idx_cim10_type_code ON cim10_codes (type_code);
CREATE INDEX IF NOT EXISTS idx_cim10_classant ON cim10_codes (est_classant) WHERE est_classant = 1;

-- ─────────────────────────────────────────────────────────────────────────
-- 3. Référentiels CIM-11 — Foundation (stable inter-releases)
--    et Linearizations MMS (release-dépendantes)
-- ─────────────────────────────────────────────────────────────────────────

-- Entités de la fondation CIM-11 (URIs stables, poly-hiérarchie possible)
CREATE TABLE IF NOT EXISTS cim11_foundation (
    uri                  TEXT    NOT NULL PRIMARY KEY,            -- http://id.who.int/icd/entity/{id}
    entity_id            TEXT    NOT NULL UNIQUE,                 -- {id} numérique extrait
    label_fr             TEXT,
    label_en             TEXT,
    definition_fr        TEXT,
    parent_uris          TEXT,                                    -- JSON array — poly-hiérarchie
    child_uris           TEXT,                                    -- JSON array (lazy)
    kind                 TEXT    CHECK (kind IN (
                            'entity', 'chapter', 'residual',
                            'extension_axis', 'extension_value', 'specifier'
                        )),
    is_residual          INTEGER NOT NULL DEFAULT 0 CHECK (is_residual IN (0, 1)),
    release_first_seen   TEXT,                                    -- ex: '2024-01'
    release_last_seen    TEXT,
    cached_at            TEXT    NOT NULL DEFAULT (datetime('now')),
    cache_source         TEXT    DEFAULT 'who_api'
);

CREATE INDEX IF NOT EXISTS idx_cim11_found_label_fr ON cim11_foundation (label_fr);
CREATE INDEX IF NOT EXISTS idx_cim11_found_kind    ON cim11_foundation (kind);

-- Codes MMS de linéarisation (release-dépendants)
-- Une ligne par (release, code). foundation_uris liste les URI(s) fondation référencées
-- (généralement 1, plusieurs si code résiduel NEC/NOS).
CREATE TABLE IF NOT EXISTS cim11_linearizations (
    release              TEXT    NOT NULL,                        -- ex: '2026-01'
    code                 TEXT    NOT NULL,                        -- ex: 'BA00' ou '1A00' (stem) — NB: les clusters &/ ne sont PAS stockés ici
    uri                  TEXT    NOT NULL,                        -- http://id.who.int/icd/release/11/{release}/mms/{code}
    label_fr             TEXT,
    label_en             TEXT,
    parent_code          TEXT,
    chapitre             TEXT,
    foundation_uris      TEXT    NOT NULL,                        -- JSON array (au moins 1 URI)
    is_stem              INTEGER NOT NULL DEFAULT 1 CHECK (is_stem IN (0, 1)),
    is_extension         INTEGER NOT NULL DEFAULT 0 CHECK (is_extension IN (0, 1)),
    is_terminal          INTEGER NOT NULL DEFAULT 1 CHECK (is_terminal IN (0, 1)),
    is_category          INTEGER NOT NULL DEFAULT 0 CHECK (is_category IN (0, 1)),
    cached_at            TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (release, code)
);

CREATE INDEX IF NOT EXISTS idx_cim11_lin_parent  ON cim11_linearizations (release, parent_code);
CREATE INDEX IF NOT EXISTS idx_cim11_lin_uri     ON cim11_linearizations (uri);
CREATE INDEX IF NOT EXISTS idx_cim11_lin_chap    ON cim11_linearizations (release, chapitre);

-- ─────────────────────────────────────────────────────────────────────────
-- 4. Types de relations & sources de décision (catalogues)
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS relation_types (
    code         TEXT NOT NULL PRIMARY KEY,
    libelle      TEXT NOT NULL,
    definition   TEXT,
    sort_order   INTEGER DEFAULT 0
);

INSERT OR IGNORE INTO relation_types (code, libelle, definition, sort_order) VALUES
    ('equivalent',          'Équivalent',                          'Concepts considérés strictement équivalents',                       10),
    ('plus_large',          'Plus large (broader)',                'Le code cible est conceptuellement plus large que le code source',  20),
    ('plus_precis',         'Plus précis (narrower)',              'Le code cible est conceptuellement plus précis que le code source', 30),
    ('multiple',            'Mapping multiple (1:n)',              'Plusieurs codes cibles nécessaires pour exprimer le code source',   40),
    ('composite',           'Cluster post-coordonné',              'Mapping nécessitant un cluster CIM-11 (stem + specifiers)',         50),
    ('residuel',            'Résiduel (NEC/NOS)',                  'Mapping vers code résiduel (non explicitement classé ailleurs)',    60),
    ('non_mappable',        'Non mappable',                        'Aucun mapping pertinent dans la nomenclature cible',                70),
    ('necessite_postcoord', 'Nécessite post-coordination',         'Mapping incomplet sans post-coordination CIM-11',                   80),
    ('ambigu',              'Ambigu',                              'Plusieurs interprétations possibles, à arbitrer',                   90);

CREATE TABLE IF NOT EXISTS source_decisions (
    code         TEXT NOT NULL PRIMARY KEY,
    libelle      TEXT NOT NULL,
    description  TEXT,
    etape        INTEGER         -- étape pipeline (1..6) ou NULL si humain
);

INSERT OR IGNORE INTO source_decisions (code, libelle, description, etape) VALUES
    -- Pipeline forward
    ('OMS+ANS',                'OMS + ANS concordants',           'Sources OMS et ANS concordantes',                      1),
    ('OMS',                    'OMS seul',                        'Source OMS seule',                                     1),
    ('ANS',                    'ANS seul',                        'Source ANS seule',                                     1),
    ('HERITAGE',               'Hérité du parent',                'Mapping hérité du code parent',                        1),
    ('MANUEL',                 'Mapping manuel',                  'Mapping manuel spécifique FR',                          1),
    ('AUCUNE',                 'Aucune source',                   'Aucune source identifiée',                              1),
    ('LLM_AMELIORE',           'LLM amélioré',                    'LLM a proposé un meilleur code CIM-11',                2),
    ('LLM_CONFIRME',           'LLM confirmé',                    'LLM confirme le code de la pipeline R',                2),
    ('LLM_POST_COORD',         'LLM post-coordination',           'LLM propose une post-coordination',                    2),
    ('LLM_INCERTAIN',          'LLM incertain',                   'LLM incertain — code R conservé',                       2),
    ('LLM_INVALIDE',           'LLM code invalide',               'Code proposé invalide',                                 2),
    ('DETERMINISTE_EXTENSION', 'Extension ClaML déterministe',    'Cluster CIM-11 post-coordonné déterministe',           3),
    ('BIDIR_CONFIRME',         'Arbitrage confirme forward',      'Arbitrage bidirectionnel confirme le forward',         4),
    ('BIDIR_CONTESTE',         'Arbitrage conteste forward',      'Arbitrage indique forward incorrect',                  4),
    ('BIDIR_NI_LUN_NI_LAUTRE', 'Arbitrage : ni un ni l''autre',   'Ni forward ni reverse satisfaisants',                  4),
    ('BIDIR_STRUCTURAL',       'Discordance structurelle',        'Discordance structurelle (pas d''erreur)',              4),
    ('REREV_B_AMELIORE',       'Re-review amélioré',              'Re-review a trouvé un meilleur code',                  5),
    ('REREV_B_POST_COORD',     'Re-review post-coordination',     'Re-review propose une post-coordination',              5),
    ('REREV_B_CONFIRME',       'Re-review confirme',              'Re-review confirme le forward malgré contestation',    5),
    ('REREV_B_INCERTAIN',      'Re-review incertain',             'Re-review non concluante',                              5),
    ('LLM_CORRIGE_INVALIDE',   'Code invalide corrigé',           'Code INVALIDE corrigé par recherche sémantique',       6),
    -- Pipeline reverse
    ('ALGO_OMS',               'Mapping OMS d''origine',          'Mapping OMS reverse non révisé',                       1),
    ('LLM_REVERSE_AMELIORE',   'LLM reverse amélioré',            'LLM a trouvé un meilleur CIM-10',                       2),
    ('LLM_REVERSE_CONFIRME',   'LLM reverse confirmé',            'LLM confirme le CIM-10 OMS',                            2),
    ('LLM_REVERSE_INCERTAIN',  'LLM reverse incertain',           'LLM incertain — CIM-10 OMS conservé',                   2),
    ('LLM_REVERSE_SANS_EQUIV', 'LLM reverse sans équivalent',     'LLM estime qu''il n''y a pas d''équivalent CIM-10',     2),
    ('BIDIR_FORWARD_RETENU',   'Arbitrage : forward retenu',      'Arbitrage : le CIM-10 du forward est meilleur',        3),
    ('BIDIR_REVERSE_CONFIRME', 'Arbitrage confirme reverse',      'Arbitrage confirme le reverse',                         3),
    ('BIDIR_ALTERNATIVE',      'Arbitrage propose alternative',   'Arbitrage propose un CIM-10 alternatif',                3),
    ('CLASSANT_PULL',          'Tiré vers classant',              'Code tiré vers un classant PMSI',                       4),
    -- Décisions humaines (transcomonitor)
    ('HUMAIN_ATIH',            'Décision experte ATIH',           'Mapping corrigé/validé par expert ATIH',                NULL),
    ('REGLE_AUTO',             'Règle automatique appliquée',     'Mapping issu d''une règle déclarative (V2)',           NULL),
    ('IMPORT_AUTO',            'Import massif automatique',       'Mapping issu d''un import sans intervention humaine',  NULL);

-- ─────────────────────────────────────────────────────────────────────────
-- 5. Versions de nomenclatures (multi-axes : CIM-10 FR, CIM-11 MMS, CIM-11 foundation)
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS nomenclature_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    nomenclature    TEXT    NOT NULL CHECK (nomenclature IN (
                       'cim10_fr', 'cim11_mms', 'cim11_foundation'
                    )),
    version_label   TEXT    NOT NULL,           -- ex: '2026', '2026-01', 'foundation_2026-05'
    effective_date  TEXT,                       -- date d'effet officielle
    imported_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    notes           TEXT,
    UNIQUE (nomenclature, version_label)
);

-- ─────────────────────────────────────────────────────────────────────────
-- 6. Mappings — cœur métier (forward + reverse unifiés)
-- ─────────────────────────────────────────────────────────────────────────
-- Modèle dual MMS / foundation conformément au plan §5 :
--   - target_kind contrôle la nature de la cible (mms_simple/cluster/foundation_only/...)
--   - target_mms_code : code MMS final (ou cluster string 'BA00&XN8P1') si applicable
--   - target_foundation_uris : JSON array — TOUJOURS peuplé quand target_kind ∈ {mms_simple, mms_cluster, foundation_only}
--   - target_components : JSON array — détail des composants d'un cluster (1 entrée par composant
--                          avec mms_code + lin_uri + foundation_uri + role + axis)
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS mappings (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    direction                TEXT    NOT NULL CHECK (direction IN ('forward', 'reverse')),

    -- Source
    source_code              TEXT    NOT NULL,         -- code CIM-10 si forward, code MMS si reverse
    source_kind              TEXT    NOT NULL CHECK (source_kind IN ('cim10_code', 'mms_code', 'foundation_uri')),
    source_version_id        INTEGER REFERENCES nomenclature_versions (id),

    -- Cible — modèle dual MMS / foundation
    target_kind              TEXT    NOT NULL CHECK (target_kind IN (
                                'mms_simple',      -- 1 code MMS terminal
                                'mms_cluster',     -- cluster post-coordonné
                                'foundation_only', -- 1..N URIs fondation, pas de code MMS
                                'cim10_code',      -- direction=reverse : code CIM-10 cible
                                'non_mappable'     -- pas de cible
                             )),
    target_mms_code          TEXT,                                  -- forward : 'BA00' ou 'BA00&XN8P1' ; reverse : NULL
    target_cim10_code        TEXT,                                  -- reverse uniquement : code CIM-10 cible
    target_label             TEXT,                                  -- dénormalisation §16.1 : libellé de la cible figé à l'ingest/edit
                                                                    -- (forward : libellé CIM-11 ; reverse : libellé CIM-10)
                                                                    -- Fallback prioritaire si la JOIN cim11_linearizations / cim10_codes échoue.
    target_foundation_uris   TEXT,                                  -- JSON array de foundation URIs
    target_components        TEXT,                                  -- JSON array (détail cluster)
    target_release_id        INTEGER REFERENCES nomenclature_versions (id),

    -- Sémantique
    relation_type            TEXT    REFERENCES relation_types (code),
    fiabilite                TEXT    CHECK (fiabilite IS NULL OR fiabilite IN (
                                'TRES_HAUTE', 'HAUTE', 'MOYENNE', 'BASSE',
                                'HERITAGE', 'CONTESTEE', 'NON_RESOLU'
                             )),
    source_decision          TEXT    REFERENCES source_decisions (code),
    pre_validation_rule_id   INTEGER REFERENCES rules (id),

    -- Workflow
    status                   TEXT    NOT NULL DEFAULT 'propose' CHECK (status IN (
                                'propose', 'en_revue', 'valide', 'gele',
                                'conteste', 'rejete'
                             )),
    current_version_id       INTEGER REFERENCES frozen_versions (id),

    -- Données structurées préservant la pipeline
    pipeline_traceability    TEXT,        -- JSON : etape1_* à etape5_* intégral
    impacts_aval             TEXT,        -- JSON : {pmsi_classant, pmsi_cma, indicateurs, registres, ...}
    actions_necessaires      TEXT,        -- JSON : [{type, detail, due}, ...]

    -- Validation
    last_validated_by        INTEGER REFERENCES users (id),
    last_validated_at        TEXT,
    is_self_validation       INTEGER NOT NULL DEFAULT 0 CHECK (is_self_validation IN (0, 1)),
                             -- §14 #7 : trace renforcée quand valideur valide sa propre proposition

    -- Timestamps
    created_at               TEXT    NOT NULL DEFAULT (datetime('now')),
    created_by               INTEGER REFERENCES users (id),
    updated_at               TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_by               INTEGER REFERENCES users (id),

    -- Concurrence : version applicative (incrémentée à chaque update — optimistic locking)
    revision                 INTEGER NOT NULL DEFAULT 1
);

-- Garantit 1 mapping actif par (direction, source_code, version courante).
-- COALESCE traite NULL comme une valeur normale, sinon SQLite/PG considèrent
-- NULL ≠ NULL et autoriseraient plusieurs "heads" non-figés pour le même
-- (direction, source_code) — ce qui casserait le workflow.
CREATE UNIQUE INDEX IF NOT EXISTS uq_mappings_active
    ON mappings (direction, source_code, COALESCE(current_version_id, 0));

CREATE INDEX IF NOT EXISTS idx_mappings_direction_source ON mappings (direction, source_code);
CREATE INDEX IF NOT EXISTS idx_mappings_direction_status ON mappings (direction, status);
CREATE INDEX IF NOT EXISTS idx_mappings_target_mms      ON mappings (target_mms_code) WHERE target_mms_code IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_mappings_target_cim10    ON mappings (target_cim10_code) WHERE target_cim10_code IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_mappings_status_pending  ON mappings (status) WHERE status IN ('propose', 'en_revue', 'conteste');
CREATE INDEX IF NOT EXISTS idx_mappings_validator       ON mappings (last_validated_by) WHERE last_validated_by IS NOT NULL;

-- ─────────────────────────────────────────────────────────────────────────
-- 7. Mapping proposals (historique append-only — pas d'UPDATE/DELETE)
--    Chaque modification d'un mapping snapshote l'ANCIENNE valeur ici.
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS mapping_proposals (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    mapping_id               INTEGER NOT NULL REFERENCES mappings (id) ON DELETE CASCADE,

    -- Snapshot de l'ancienne valeur (avant modification)
    target_kind_old          TEXT,
    target_mms_code_old      TEXT,
    target_cim10_code_old    TEXT,
    target_foundation_uris_old TEXT,    -- JSON
    target_components_old    TEXT,      -- JSON
    relation_type_old        TEXT,
    fiabilite_old            TEXT,
    source_decision_old      TEXT,
    status_old               TEXT,

    -- Métadonnées de la proposition
    proposed_by              INTEGER REFERENCES users (id),    -- NULL = système (import, règle)
    proposed_source          TEXT NOT NULL CHECK (proposed_source IN (
                                'ui_edit', 'import_batch', 'rule_engine', 'system'
                             )),
    proposed_at              TEXT    NOT NULL DEFAULT (datetime('now')),
    superseded_at            TEXT    NOT NULL DEFAULT (datetime('now')),
    justification_id         INTEGER REFERENCES justifications (id),

    -- Lien éventuel à un batch d'import qui a écrasé
    import_batch_id          INTEGER REFERENCES import_batches (id)
);

CREATE INDEX IF NOT EXISTS idx_proposals_mapping ON mapping_proposals (mapping_id, superseded_at DESC);
CREATE INDEX IF NOT EXISTS idx_proposals_user    ON mapping_proposals (proposed_by);

-- ─────────────────────────────────────────────────────────────────────────
-- 8. Justifications structurées
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS justifications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    mapping_id   INTEGER NOT NULL REFERENCES mappings (id) ON DELETE CASCADE,
    motif        TEXT    NOT NULL CHECK (motif IN (
                    'confirmation_OMS', 'decision_ANS', 'consigne_PMSI',
                    'arbitrage_expert', 'postcoord', 'autre'
                 )),
    commentaire  TEXT,                                              -- texte libre
    references_  TEXT,                                              -- JSON : [{type, value, url}, ...]
                                                                    -- 'references' est mot-clé SQL → suffixe _
    problematique TEXT,                                             -- §16.7 : code problematique_types
                                                                    -- (FK applicative, pas SQL — la table problematique_types
                                                                    -- est définie plus bas, on évite l'ordre de création)
    attached_to_action TEXT CHECK (attached_to_action IS NULL OR attached_to_action IN (
                    'create', 'edit', 'validate', 'contest', 'reject', 'freeze', 'import_decision'
                 )),
    created_by   INTEGER REFERENCES users (id),
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_justifications_mapping ON justifications (mapping_id, created_at DESC);

-- ─────────────────────────────────────────────────────────────────────────
-- 9. Mapping ↔ Foundation links (vue dérivée — facilite jointures inverses)
--    Permet : "Quels mappings CIM-10 référencent cette entité fondation ?"
--    Maintenue par services/foundation.py à chaque insert/update de mappings.
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS mapping_foundation_links (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mapping_id      INTEGER NOT NULL REFERENCES mappings (id) ON DELETE CASCADE,
    foundation_uri  TEXT    NOT NULL REFERENCES cim11_foundation (uri),
    role            TEXT    NOT NULL CHECK (role IN (
                       'primary', 'component_stem', 'component_specifier', 'residual'
                    )),
    position        INTEGER DEFAULT 0,
    UNIQUE (mapping_id, foundation_uri, role, position)
);

CREATE INDEX IF NOT EXISTS idx_mfl_foundation ON mapping_foundation_links (foundation_uri);
CREATE INDEX IF NOT EXISTS idx_mfl_mapping    ON mapping_foundation_links (mapping_id);

-- ─────────────────────────────────────────────────────────────────────────
-- 10. Assignment lists & assignments
--     §14 #17 : accessible à tous les mainteneurs (sans restriction de rôle pour foundation_only)
--     Listes dynamiques (query_definition JSON) ou statiques (static_codes JSON).
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS assignment_lists (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT    NOT NULL,
    description       TEXT,
    direction         TEXT    NOT NULL CHECK (direction IN ('forward', 'reverse', 'both')),
    query_definition  TEXT,                                 -- JSON : {chapitre, fiabilite, status, ...}
    static_codes      TEXT,                                 -- JSON array (optionnel — liste explicite)
    is_frozen         INTEGER NOT NULL DEFAULT 0 CHECK (is_frozen IN (0, 1)),
                                                            -- si 1 : la liste est gelée à un instant T (snapshot des codes)
    frozen_codes_snapshot TEXT,                             -- JSON array — peuplé quand is_frozen=1
    created_by        INTEGER REFERENCES users (id),
    created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_assignment_lists_creator ON assignment_lists (created_by);

CREATE TABLE IF NOT EXISTS assignments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    list_id         INTEGER NOT NULL REFERENCES assignment_lists (id) ON DELETE CASCADE,
    user_id         INTEGER NOT NULL REFERENCES users (id),
    expected_role   TEXT    NOT NULL CHECK (expected_role IN ('mainteneur', 'valideur')),
    assigned_by     INTEGER REFERENCES users (id),
    assigned_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    due_date        TEXT,
    status          TEXT    NOT NULL DEFAULT 'open' CHECK (status IN (
                       'open', 'in_progress', 'done', 'cancelled'
                    )),
    completed_at    TEXT,
    UNIQUE (list_id, user_id, expected_role)
);

CREATE INDEX IF NOT EXISTS idx_assignments_user_status ON assignments (user_id, status);

-- ─────────────────────────────────────────────────────────────────────────
-- 11. Frozen versions & snapshots
--     §14 #5 : re-seed annuel + ponctuel → versions gelées comme jalons.
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS frozen_versions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    label               TEXT    NOT NULL UNIQUE,           -- ex: 'v_pipeline_initial', 'v2026.06_ATIH'
    description         TEXT,
    parent_version_id   INTEGER REFERENCES frozen_versions (id),
    frozen_by           INTEGER REFERENCES users (id),
    frozen_at           TEXT    NOT NULL DEFAULT (datetime('now')),
    snapshot_s3_uri     TEXT,                              -- s3://transcomonitor/snapshots/{label}/...
    manifest_sha256     TEXT,
    stats_json          TEXT,                              -- JSON : {n_mappings, n_valides, n_per_chapter, ...}
    is_initial_seed     INTEGER NOT NULL DEFAULT 0 CHECK (is_initial_seed IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_frozen_versions_parent ON frozen_versions (parent_version_id);

-- Matérialisation du contenu de mappings au moment du gel (1 ligne / mapping / version)
-- Permet la comparaison directe entre versions sans rejouer l'audit.
CREATE TABLE IF NOT EXISTS version_mappings_snapshot (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id               INTEGER NOT NULL REFERENCES frozen_versions (id) ON DELETE CASCADE,
    mapping_id               INTEGER NOT NULL REFERENCES mappings (id),
    direction                TEXT    NOT NULL,
    source_code              TEXT    NOT NULL,
    target_kind              TEXT,
    target_mms_code          TEXT,
    target_cim10_code        TEXT,
    target_foundation_uris   TEXT,                          -- JSON
    target_components        TEXT,                          -- JSON
    relation_type            TEXT,
    fiabilite                TEXT,
    source_decision          TEXT,
    status_at_freeze         TEXT,
    validated_by             INTEGER REFERENCES users (id),
    validated_at             TEXT,
    UNIQUE (version_id, mapping_id)
);

CREATE INDEX IF NOT EXISTS idx_vms_version_source ON version_mappings_snapshot (version_id, direction, source_code);

-- ─────────────────────────────────────────────────────────────────────────
-- 12. Import batches & diffs
--     §14 #6 : politique de précédence configurable par l'admin à chaque import
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS import_batches (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    source_label             TEXT    NOT NULL,             -- ex: 'pipeline_v6_2026-06'
    source_file_uri          TEXT,                          -- chemin S3 ou local
    source_file_sha256       TEXT,
    imported_by              INTEGER REFERENCES users (id),
    imported_at              TEXT    NOT NULL DEFAULT (datetime('now')),
    applied_at               TEXT,                          -- NULL si encore en preview
    precedence_policy        TEXT    NOT NULL CHECK (precedence_policy IN (
                                'auto_unless_valid',
                                'never_override_valid',
                                'always_override',
                                'manual_per_row'
                             )),
    status                   TEXT    NOT NULL DEFAULT 'preview' CHECK (status IN (
                                'preview', 'applied', 'rolled_back', 'cancelled'
                             )),
    stats_json               TEXT,                          -- JSON : {n_inserts, n_updates, n_conflicts, ...}
    nomenclature_version_id  INTEGER REFERENCES nomenclature_versions (id)
);

CREATE INDEX IF NOT EXISTS idx_import_batches_status ON import_batches (status, imported_at DESC);

CREATE TABLE IF NOT EXISTS import_diffs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id        INTEGER NOT NULL REFERENCES import_batches (id) ON DELETE CASCADE,
    mapping_id      INTEGER REFERENCES mappings (id),       -- NULL si change_type='new'
    direction       TEXT,
    source_code     TEXT    NOT NULL,
    change_type     TEXT    NOT NULL CHECK (change_type IN (
                       'new', 'target_changed', 'relation_changed',
                       'kind_changed', 'foundation_changed', 'removed', 'no_change'
                    )),
    before_json     TEXT,                                    -- snapshot avant
    after_json      TEXT,                                    -- snapshot proposé
    resolution      TEXT    NOT NULL DEFAULT 'pending' CHECK (resolution IN (
                       'pending', 'applied', 'skipped', 'manual_pending', 'conflict_validated'
                    )),
    resolved_by     INTEGER REFERENCES users (id),
    resolved_at     TEXT,
    resolution_note TEXT
);

CREATE INDEX IF NOT EXISTS idx_import_diffs_batch       ON import_diffs (batch_id, resolution);
CREATE INDEX IF NOT EXISTS idx_import_diffs_mapping     ON import_diffs (mapping_id);
CREATE INDEX IF NOT EXISTS idx_import_diffs_source_code ON import_diffs (source_code);

-- ─────────────────────────────────────────────────────────────────────────
-- 13. Audit events (append-only — jamais UPDATE/DELETE)
--     §14 #9 : IP/UA configurable via app_config.audit_capture_request_meta
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS audit_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL DEFAULT (datetime('now')),
    actor_user_id   INTEGER REFERENCES users (id),         -- NULL = système
    actor_username  TEXT,                                    -- dénormalisé (préserve trace si user supprimé)
    action          TEXT    NOT NULL CHECK (action IN (
                       'login', 'logout', 'login_failed',
                       'create_mapping', 'edit_mapping', 'validate_mapping',
                       'contest_mapping', 'reject_mapping',
                       'freeze_version', 'compare_versions',
                       'import_preview', 'import_apply', 'import_resolve_diff',
                       'export', 'admin_user_create', 'admin_user_update',
                       'admin_user_deactivate', 'admin_config_change',
                       'admin_secret_set', 'admin_list_create', 'admin_list_update',
                       'admin_assign', 'rule_apply', 'cache_refresh',
                       'backup_snapshot', 'backup_restore'
                    )),
    object_type     TEXT    CHECK (object_type IS NULL OR object_type IN (
                       'mapping', 'user', 'frozen_version', 'import_batch',
                       'assignment_list', 'assignment', 'rule', 'config', 'system'
                    )),
    object_id       TEXT,                                    -- ID texte pour souplesse
    old_value_json  TEXT,
    new_value_json  TEXT,
    source          TEXT    NOT NULL DEFAULT 'ui' CHECK (source IN (
                       'ui', 'import', 'rule_engine', 'api', 'system'
                    )),
    request_ip      TEXT,                                    -- nullable, dépend de app_config
    request_ua      TEXT,                                    -- nullable, dépend de app_config
    note            TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_ts            ON audit_events (ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_actor         ON audit_events (actor_user_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_object        ON audit_events (object_type, object_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action_recent ON audit_events (action, ts DESC);

-- Triggers pour empêcher UPDATE/DELETE sur audit_events (append-only strict)
CREATE TRIGGER IF NOT EXISTS audit_events_no_update
    BEFORE UPDATE ON audit_events
    BEGIN
        SELECT RAISE(ABORT, 'audit_events is append-only: UPDATE forbidden');
    END;

CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
    BEFORE DELETE ON audit_events
    BEGIN
        SELECT RAISE(ABORT, 'audit_events is append-only: DELETE forbidden');
    END;

-- Triggers identiques pour mapping_proposals (append-only)
CREATE TRIGGER IF NOT EXISTS mapping_proposals_no_update
    BEFORE UPDATE ON mapping_proposals
    BEGIN
        SELECT RAISE(ABORT, 'mapping_proposals is append-only: UPDATE forbidden');
    END;

CREATE TRIGGER IF NOT EXISTS mapping_proposals_no_delete
    BEFORE DELETE ON mapping_proposals
    BEGIN
        -- Exception : ON DELETE CASCADE depuis mappings autorisé via PRAGMA
        SELECT RAISE(ABORT, 'mapping_proposals is append-only: explicit DELETE forbidden');
    END;

-- ─────────────────────────────────────────────────────────────────────────
-- 14. Rules engine (V2 scaffolding — schéma prêt, moteur en V2)
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,
    description     TEXT,
    definition_yaml TEXT    NOT NULL,                  -- pattern → action (YAML)
    active          INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)),
    version         INTEGER NOT NULL DEFAULT 1,
    created_by      INTEGER REFERENCES users (id),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS rule_applications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id      INTEGER NOT NULL REFERENCES rules (id),
    mapping_id   INTEGER NOT NULL REFERENCES mappings (id) ON DELETE CASCADE,
    applied_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    applied_by   INTEGER REFERENCES users (id),
    result       TEXT,                                 -- JSON : {changed_fields, before, after}
    dry_run      INTEGER NOT NULL DEFAULT 0 CHECK (dry_run IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_rule_apps_mapping ON rule_applications (mapping_id);
CREATE INDEX IF NOT EXISTS idx_rule_apps_rule    ON rule_applications (rule_id, applied_at DESC);

-- ─────────────────────────────────────────────────────────────────────────
-- 16. Problématiques de transcodage (typologie paramétrable)
--     Per plan §16.7 : liste de valeurs annotables par les utilisateurs lors
--     de la justification d'un mapping, gérée par l'administrateur.
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS problematique_types (
    code        TEXT    NOT NULL PRIMARY KEY,
    libelle     TEXT    NOT NULL,
    description TEXT,
    color       TEXT    DEFAULT 'secondary',                     -- Bootstrap badge color
    active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    sort_order  INTEGER DEFAULT 0,
    created_by  INTEGER REFERENCES users(id),
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Pre-seed with reasonable defaults — admins can edit/disable/add via the UI
INSERT OR IGNORE INTO problematique_types (code, libelle, description, color, sort_order) VALUES
    ('aucune',                       'Aucune',
     'Pas de problématique identifiée à signaler.',                              'success', 10),
    ('ambiguite_oms',                'Ambiguïté OMS',
     'Le mapping OMS source est ambigu (plusieurs candidats équivalents).',      'warning', 20),
    ('decision_fr_manquante',        'Décision FR manquante',
     'Le mapping nécessite une décision nationale (ATIH/ANS) non encore prise.', 'danger',  30),
    ('postcoord_incomplete',         'Post-coordination incomplète',
     'Le cluster MMS est incomplet — axes/specifiers manquants.',                'warning', 40),
    ('divergence_classant_pmsi',     'Divergence classant PMSI',
     'Le mapping change le statut classant du code (impact groupage GHM).',      'danger',  50),
    ('necessite_demande_oms',        'Nécessite une demande OMS',
     'Le concept n''existe pas en CIM-11 — demande de création OMS requise.',    'info',    60),
    ('autre',                        'Autre',
     'Autre problématique — préciser dans le commentaire.',                      'secondary', 70);

-- ─────────────────────────────────────────────────────────────────────────
-- 15. App config (key-value — paramètres et secrets chiffrés)
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS app_config (
    key         TEXT    NOT NULL PRIMARY KEY,
    value       TEXT,                                 -- chiffré Fernet pour les secrets
    is_secret   INTEGER NOT NULL DEFAULT 0 CHECK (is_secret IN (0, 1)),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_by  INTEGER REFERENCES users (id)
);

-- Seeds initiaux (paramètres résolus côté config.yml, ici on n'amorce que le toggle audit)
INSERT OR IGNORE INTO app_config (key, value, is_secret) VALUES
    ('audit_capture_request_meta', '0', 0),                -- §14 #9 toggle (défaut désactivé)
    ('default_precedence_policy', 'auto_unless_valid', 0); -- §14 #6
