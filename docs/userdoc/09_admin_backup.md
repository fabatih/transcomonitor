# Administration — Backup & cache

**Onglet Administration → 💾 Backup & cache** (visible pour admins uniquement).

## État de la persistance

L'onglet affiche :
- **DB locale** : chemin + taille en MB.
- **S3 disponible** : badge OK/KO + détails de la connexion.
- **Bucket** + **Région** + **Versioning S3** (Enabled/Disabled).
- **Dernière sauvegarde S3** : timestamp ISO + taille.

## Actions

### Sauvegarder maintenant

Upload immédiat de la DB locale (`/tmp/transcomonitor.sqlite`) vers
`s3://transcomonitor/db/transcomonitor.sqlite`. Le versioning S3 conserve
l'historique des sauvegardes (utile pour rollback en cas de corruption).

Action auditée : `backup_snapshot`.

### Re-tester la connexion S3

Diagnostic rapide : `head_bucket` + `get_bucket_versioning`. Toast indique
le résultat.

### Rafraîchir le cache CIM-11 (admin)

Cette action ne lance **pas** le bootstrap directement (trop long pour un
clic UI — ~3h). Elle :
1. Loggue une intention dans l'audit (`cache_refresh`).
2. Affiche un toast rappelant la commande à lancer localement :
   ```bash
   python -m scripts.bootstrap_cim11_refs \
       --from-csv data/seed/transcodage_pipeline_complete.xlsx \
       --release 2024-01 --upload-s3
   ```
3. Une fois le script terminé, l'admin re-clique sur **Re-tester S3** pour
   confirmer l'upload.

## Sauvegarde automatique

En plus du clic manuel, la DB est sauvegardée automatiquement :
- À chaque modification (edit_mapping, validate, freeze, …).
- Debounced à 10 secondes minimum entre deux uploads (config `s3.upload_min_interval_seconds`).
- Upload en background thread (n'impacte pas la réactivité de l'UI).

## Restore au boot

Au démarrage du container :
1. Si la DB locale est vide OU contient juste le default admin → restore
   depuis S3 (`s3://transcomonitor/db/transcomonitor.sqlite`).
2. Sinon, garde la DB locale (priorité au state local).

Ce mécanisme évite la perte de données entre redémarrages shinyapps.io
(filesystem `/tmp` éphémère).

## Versioning S3

Le bucket `transcomonitor` a le versioning activé. En cas de corruption :
1. Accéder à la console AWS S3 → bucket → versions.
2. Restaurer la version souhaitée du fichier `db/transcomonitor.sqlite`.
3. Redémarrer le container shinyapps.io (ou attendre le prochain restart) →
   la version restaurée sera téléchargée.

## Snapshots de versions gelées

Les gels (`frozen_versions`) produisent des snapshots immuables dans
`s3://transcomonitor/snapshots/<version_label>/` (XLSX + JSON + manifest SHA-256).
Ces snapshots ne sont jamais écrasés.
