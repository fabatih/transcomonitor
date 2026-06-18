# Plateforme ATIH de maintenance du transcodage CIM

**Nom technique** : `transcomonitor`
**Statut** : 🚧 En développement (MVP)

Application Shiny for Python pour maintenir, valider et versionner les tables de transcodage CIM‑10 FR/PMSI ↔ CIM‑11 (OMS) pour l'ATIH.

## Contexte

Cet outil consomme le résultat de la pipeline de transcodage initiale (`../transcodage/`) — qui produit ~42 897 mappings forward CIM‑10 → CIM‑11 et ~18 505 mappings reverse CIM‑11 → CIM‑10 — et permet à une équipe restreinte de mainteneurs/correcteurs/valideurs ATIH de :

- Corriger code par code les mappings avec justification structurée
- Importer massivement de nouvelles versions de la pipeline avec gestion de la précédence vs validations expertes existantes
- Versionner et geler des snapshots immuables exportables (PMSI, indicateurs, registres, profil sémantique foundation)
- Gérer les mappings post‑coordonnés CIM‑11 et les URIs de fondation (interopérabilité sémantique)
- Naviguer/sélectionner dans l'arborescence CIM‑11 OMS via le browser embarqué (ECT/EB), avec choix de release et mode foundation
- Tracer toutes les actions dans un audit log append‑only

## Architecture

- **Stack** : Python ≥ 3.11, Shiny for Python ≥ 1.5, SQLAlchemy 2.x Core
- **Persistance MVP** : SQLite + S3 (bucket `transcomonitor` eu-west-3) avec `max-instances=1` sur shinyapps.io
- **Persistance V1** : PostgreSQL managé EU (provider à déterminer)
- **API WHO** : OAuth2 client_credentials avec proxy server‑side (pattern repris d'`icd11pycode`)
- **Réutilisation** : modules `mod_auth`, `mod_ect_browser`, `utils/crypto`, `utils/security`, `utils/s3_storage` issus d'`icd11pycode`

## Structure

```
transcomonitor/
├── app.py                  # entry point Shiny
├── config/                 # config.yml
├── db/                     # schema SQL, engine SQLAlchemy, models CRUD
├── modules/                # Shiny modules (UI + server)
├── services/               # ingest, diff, audit, exporter, …
├── utils/                  # config_manager, crypto, security
├── scripts/                # bootstrap_cim11_refs.py, …
├── tests/                  # pytest (services + smoke UI)
├── www/                    # custom.css, ect_bridge.js
└── data/seed/              # transcodage_pipeline_complete.xlsx (seed initial)
```

## Variables d'environnement

Voir [`.env.example`](.env.example).

## Plan détaillé

Le plan d'implémentation complet (15 sections, 25 todos) est versionné en parallèle dans `~/.copilot/session-state/.../plan.md`.

## Licence

MIT (à confirmer avec ATIH).

## Auteur

Fabrice Danjou (`@fabatih`) — ATIH.
