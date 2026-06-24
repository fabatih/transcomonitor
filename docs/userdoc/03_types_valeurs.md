# Types et valeurs

Référence exhaustive des enums et catalogues utilisés dans l'app.

## `relation_types` (9 valeurs)

Type sémantique de la relation entre un code source et son code cible.

| Code | Libellé | Définition |
|---|---|---|
| `equivalent` | Équivalent | Concepts strictement équivalents |
| `plus_large` | Plus large (broader) | Cible plus large que la source |
| `plus_precis` | Plus précis (narrower) | Cible plus précise que la source |
| `multiple` | Mapping multiple (1:n) | Plusieurs cibles nécessaires |
| `composite` | Cluster post-coordonné | Cluster CIM-11 (stem + specifiers) |
| `residuel` | Résiduel (NEC/NOS) | Cible résiduelle |
| `non_mappable` | Non mappable | Aucun mapping pertinent |
| `necessite_postcoord` | Nécessite post-coordination | Mapping incomplet sans post-coord |
| `ambigu` | Ambigu | Plusieurs interprétations possibles |

## `target_kind` (5 valeurs)

Nature de la cible. Diffère selon la direction (forward / reverse).

| `target_kind` | Direction | Détail |
|---|---|---|
| `mms_simple` | forward | 1 code MMS terminal (ex. `BA00`) |
| `mms_cluster` | forward | Cluster post-coordonné (`BA00&XN8P1` ou multi-stem `1G40/1B5Z`) |
| `foundation_only` | forward | 1+ URIs fondation, sans code MMS associé |
| `cim10_code` | reverse | Code CIM-10 cible |
| `non_mappable` | les deux | Pas de cible (aucun mapping pertinent) |

## `fiabilite` (échelle 6 niveaux)

Mesure de confiance dans le mapping, ordonnée du plus fiable au moins fiable.

| Niveau | Sémantique |
|---|---|
| `TRES_HAUTE` | Concordance OMS+ANS+pipeline-LLM, validation experte ou règle 1:1 stricte |
| `HAUTE` | Concordance forte (OMS ou ANS) + arbitrage cohérent |
| `MOYENNE` | Mapping plausible mais non confirmé par double source |
| `BASSE` | Mapping faible — incertitudes documentées |
| `HERITAGE` | Mapping hérité d'un code parent (extension implicite) |
| `CONTESTEE` | Arbitrage bidirectionnel a contesté ce mapping |
| `NON_RESOLU` | Aucune décision satisfaisante — à traiter manuellement |

## `status` (workflow)

États d'un mapping dans le cycle de vie :

| Statut | Sémantique | Transitions autorisées |
|---|---|---|
| `propose` | État initial (auto-seed ou nouvelle proposition) | → `en_revue`, `valide`, `rejete` |
| `en_revue` | Envoyé pour validation | → `valide`, `rejete`, `conteste` |
| `valide` | Mapping validé expert | → `en_revue`, `conteste` |
| `conteste` | Contesté (souvent par arbitrage post-import) | → `en_revue`, `rejete`, `valide` |
| `rejete` | Rejeté définitivement | → `en_revue` (réouverture) |
| `gele` | Inclus dans une version gelée — **immuable** | (aucune) |

Voir [`04_workflows.md`](04_workflows.md) pour les règles RBAC par rôle.

## `source_decision` (origine pipeline)

Étape de la pipeline source qui a produit la décision initiale. Préservé tel quel
à l'ingestion :

- **Étape 1** (base algorithmique R) : `OMS+ANS`, `OMS`, `ANS`, `HERITAGE`, `MANUEL`, `AUCUNE`
- **Étape 2** (LLM forward) : `LLM_AMELIORE`, `LLM_CONFIRME`, `LLM_POST_COORD`, `LLM_INCERTAIN`, `LLM_INVALIDE`
- **Étape 3** (extensions ClaML) : `DETERMINISTE_EXTENSION`
- **Étape 4** (arbitrage bidirectionnel) : `BIDIR_CONFIRME`, `BIDIR_CONTESTE`, `BIDIR_NI_LUN_NI_LAUTRE`, `BIDIR_STRUCTURAL`
- **Étape 5** (re-review) : `REREV_B_AMELIORE`, `REREV_B_POST_COORD`, `REREV_B_CONFIRME`, `REREV_B_INCERTAIN`
- **Étape 6** (consolidation) : `LLM_CORRIGE_INVALIDE`
- **Reverse** : `ALGO_OMS`, `LLM_REVERSE_AMELIORE`, `LLM_REVERSE_CONFIRME`, `LLM_REVERSE_INCERTAIN`, `LLM_REVERSE_SANS_EQUIV`, `BIDIR_FORWARD_RETENU`, `BIDIR_REVERSE_CONFIRME`, `BIDIR_ALTERNATIVE`, `CLASSANT_PULL`
- **Décisions humaines** (post-ingestion) : `HUMAIN_ATIH`, `REGLE_AUTO`, `IMPORT_AUTO`

## `motif_justification` (lors d'une édition)

Catégorie de la justification renseignée par l'utilisateur :

| Motif | Sémantique |
|---|---|
| `confirmation_OMS` | Confirmation par les tables OMS |
| `decision_ANS` | Décision officielle ANS (sémantique des nomenclatures) |
| `consigne_PMSI` | Consigne PMSI / ATIH |
| `arbitrage_expert` | Arbitrage expert métier |
| `postcoord` | Post-coordination (cluster CIM-11) |
| `autre` | Autre raison (préciser dans le commentaire) |

## `problematique_types` (paramétrable, plan §16.7)

Typologie des problématiques de transcodage. Gérée par les admins via
**Admin → Paramètres**. Valeurs pré-seedées :

| Code | Libellé | Couleur |
|---|---|---|
| `aucune` | Aucune | success |
| `ambiguite_oms` | Ambiguïté OMS | warning |
| `decision_fr_manquante` | Décision FR manquante | danger |
| `postcoord_incomplete` | Post-coordination incomplète | warning |
| `divergence_classant_pmsi` | Divergence classant PMSI | danger |
| `necessite_demande_oms` | Nécessite une demande OMS | info |
| `autre` | Autre | secondary |

## `type_code` (caractérisation CIM-10)

Origine d'un code CIM-10 :

| Valeur | Sémantique |
|---|---|
| `INTERNATIONAL_BASE` | Présent explicitement dans ClaML OMS 2019 |
| `INTERNATIONAL_EXTENSION` | Reproductible via ModifiedBy/ModifierClass ClaML |
| `FR_ONLY` | Spécifique France (FR/PMSI uniquement) |
| `WHO_POST_2019` | OMS post-2019 (ex. U07.1 COVID) |
