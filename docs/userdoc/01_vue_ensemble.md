# Vue d'ensemble

## Objectif

**transcomonitor** est la plateforme ATIH de maintenance des tables de
transcodage **CIM‑10 FR/PMSI ↔ CIM‑11**. Elle permet à une équipe restreinte
de mainteneurs, correcteurs et valideurs ATIH de :

- Visualiser les **42 897 mappings forward** (CIM‑10 → CIM‑11) et les
  **18 505 mappings reverse** (CIM‑11 → CIM‑10) produits par la pipeline
  initiale (`../transcodage/`).
- **Corriger code par code** chaque mapping avec une justification structurée
  et une typologie de problématique paramétrable.
- **Valider** le travail via un workflow à 3 rôles (admin / mainteneur /
  valideur) avec séparation des pouvoirs et auto‑validation tracée.
- **Versionner** des snapshots gelés immuables pour publication aval (PMSI,
  groupage, registres, indicateurs).
- **Importer** de nouvelles versions de pipeline avec un mécanisme de
  précédence vis‑à‑vis des validations expertes existantes.
- **Naviguer / sélectionner** dans l'arborescence CIM‑11 OMS via le
  *Embedded Browser* (EB) avec choix de release.
- **Auditer** toutes les actions (append‑only).

## Architecture en bref

```
shinyapps.io container (1 instance forcée)
├── /tmp/transcomonitor.sqlite       (DB locale, restaurée depuis S3 au boot)
└── app.py (Shiny + WHO proxy ASGI)

      ↕ (async upload S3 debounced 10s)

AWS S3 (eu-west-3, bucket transcomonitor, versioning activé)
├── db/transcomonitor.sqlite        (état canonique)
├── snapshots/<version_label>/...   (frozen_versions)
└── cache/cim11_*.json              (referentiels CIM-11)
```

## Workflow général

1. **Démarrage** : auto‑seed depuis `data/seed/transcodage_pipeline_complete.xlsx`
   (61 402 mappings ingérés en ~25 s).
2. **Travail quotidien** :
   - Mainteneur consulte les tables Forward/Reverse, sélectionne une ligne →
     onglet Édition se charge.
   - Mainteneur édite la cible, ajoute une justification structurée
     (motif + problématique + commentaire + références).
   - Statut passe à `en_revue`.
   - Valideur consulte la file, valide ou conteste.
3. **Imports** : à chaque nouvelle release CIM‑11 OMS, import massif avec
   politique de précédence (`auto_unless_valid` par défaut).
4. **Gel** : admin gèle périodiquement une version → snapshot immuable
   exporté.

## Rôles

| Rôle | Capacités principales |
|---|---|
| **Admin** | Tout (users, listes, params, gel, secrets, backup, exports complets) |
| **Mainteneur** | Édite mappings, propose modifications, crée listes d'affectation, exports filtrés |
| **Valideur** | Valide / conteste / rejette des mappings en revue |

Voir [`06_admin_users.md`](06_admin_users.md) pour le détail de la matrice
de capabilities.
