# Photo Sorter (sort_photo)

Outil CLI Python pour **scanner** un répertoire de photos/vidéos, **extraire des métadonnées** (optionnellement via ExifTool), et **générer un plan d’actions** (dry-run) pour classer une photothèque selon les règles décrites dans `spec.md`.

> Important : à la date du **2026-08-05**, l’application implémente l’essentiel du moteur métier de la spec : scan, hashing, ExifTool, pairing Live Photo, clustering événementiel journalier GPS-aware, fusion de séjours multi-jours, géocodage inverse, scoring de confiance (full auto), décision volumétrique mensuelle/trimestrielle/événementielle, exécution copy/move/quarantine, et reclassification incrémentale (mensuel ↔ trimestriel inclus). Voir « Limites connues » plus bas pour ce qui reste simplifié ou non implémenté.

---

## Installation

### Prérequis
- Python 3.11+
- (Optionnel mais recommandé) ExifTool

Sur DietPi / Debian :
```bash
apt-get update
apt-get install -y libimage-exiftool-perl
```

Sur Windows (via [winget](https://learn.microsoft.com/windows/package-manager/winget/)) :
```powershell
winget install --id=OliverBetz.ExifTool -e
```

### Installer le projet

#### Méthode recommandée — script `run.sh` (DietPi / Debian / Linux)

Un script à la racine gère automatiquement le venv et l'installation :

```bash
./run.sh --help
./run.sh plan --help
./run.sh plan --config config.yaml --env .env
```

Le script crée le venv dans `venv/` si nécessaire, installe le projet, puis passe tous les arguments à la CLI.

#### Méthode manuelle (venv)

Sur DietPi/Debian, `pip install` système est bloqué (PEP 668). Il faut utiliser un venv :

```bash
python3 -m venv venv
source venv/bin/activate
pip install -e .
```

Une fois activé :
```bash
photo-sorter --help
# ou
python -m photo_sorter.cli --help
```

> Le venv doit être réactivé (`source venv/bin/activate`) à chaque nouvelle session terminal.

---

## Configuration

La configuration est volontairement séparée en 2 fichiers :
- `.env` : **chemins** + **mode copy/move** + **sécurité dry-run**
- `config.yaml` : **règles métier** (seuils, politiques)

### 1) `.env`
Un exemple est fourni : `.env.example`.

Variables :
- `SOURCE_ROOT` (obligatoire) : dossier scanné récursivement
- `TARGET_ROOT` (obligatoire) : racine de l’arborescence triée (future)
- `OUTPUT_ROOT` (optionnel) : dossier des rapports (défaut : `TARGET_ROOT/reports`)
- `QUARANTINE_ROOT` (optionnel) : dossier quarantaine (défaut : `../_quarantine`)
- `REVIEW_ROOT` (optionnel) : dossier “à revoir” (défaut : `../_A_REVOIR`)
- `MOVE_MODE` : `copy` ou `move` (défaut : `copy`)
- `DRY_RUN_DEFAULT` : `true/false` (défaut : `true`)

Notes :
- `TARGET_ROOT` **ne doit pas** être à l’intérieur de `SOURCE_ROOT` (protection anti-boucle).
- L’arborescence de `SOURCE_ROOT` n’est pas utilisée comme règle métier : tous les fichiers médias sont traités comme un corpus global (racine + sous-dossiers).

### 2) `config.yaml`
Le fichier `config.yaml` contient les paramètres métier (voir `spec.md` pour la signification).

Paramètres actuellement chargés et utilisés :
- ExifTool
  - `exiftool_path` (défaut : `exiftool`)
- Clustering
  - `cluster_time_window_minutes` (défaut : 45)
  - `same_place_radius_m` (défaut : 300) : rayon "même zone" en mètres (§8.3)
  - `place_change_radius_m` (défaut : 1000) : rayon "changement de zone significatif" en mètres (§8.3)
  - `stay_radius_km` (défaut : 25) : rayon de continuité géographique pour fusionner des jours en séjour (§9)
  - `max_gap_days_in_stay` (défaut : 1) : nombre de jours "vides" tolérés dans un séjour
  - `usual_place_min_distinct_days` (défaut : 10) : nb de jours distincts (dans le batch en cours) au-delà duquel une zone GPS est considérée "habituelle" (§7.2, simplifié)
- Seuils volumétriques
  - `event_min_items` (défaut : 20)
  - `month_min_items` (défaut : 15)
  - `quarter_fallback_enabled` (défaut : true)
- Live Photos
  - `live_subfolder_suffix` (défaut : `" - live"`)
  - `orphan_live_policy` (défaut : `review`) : `review | video_single | ignore`
- Doublons
  - `hash_algorithm` (défaut : `md5`) : `md5 | sha256`
  - `duplicate_policy` (défaut : `quarantine`) : `quarantine | skip | report_only`
- Géocodage (extraction GPS + reverse geocoding, voir section dédiée plus bas)
  - `geocode_cache` (défaut : `./cache/geocode.json`)
  - `geocode_provider` (défaut : `nominatim`, seul fournisseur supporté actuellement)
- Confiance (§12)
  - `confidence_review_enabled` (défaut : `false`) : si `true`, un score < 3 force `action=review` (`A_REVOIR/low_confidence/`) au lieu de copy/move. Par défaut (`false`), le score est calculé et stocké dans `plan.jsonl` (champ `confidence`) mais ne bloque jamais une décision (mode "full auto").
- Exécution
  - `log_level` (défaut : `INFO`)

---

## Lieu (ville) dans les noms de dossier événement/séjour

Les dossiers événement (`YYYY.MM.DD <libellé>, <ville>`) et séjour (`YYYY.MM.DD~DD <libellé>, <ville>`) incluent automatiquement la ville, résolue par géocodage inverse à partir du centroïde GPS des fichiers du cluster/séjour :

- **Service utilisé** : [Nominatim](https://nominatim.org/) (OpenStreetMap), gratuit, sans clé API. Requêtes en `https://nominatim.openstreetmap.org/reverse`, avec un `User-Agent` explicite comme l'exige leur [politique d'usage](https://operations.osmfoundation.org/policies/nominatim/).
- **Limitation de débit** : 1 requête/s max (imposé côté client), donc pas de risque de dépasser le quota gratuit.
- **Cache local** : chaque coordonnée résolue est mise en cache dans `geocode_cache` (JSON), donc une même zone n'est jamais re-interrogée deux fois.
- **Robustesse** : en cas d'erreur réseau/timeout, ou si le fichier n'a pas de GPS, le lieu retombe sur `Lieu inconnu` (aucun blocage du plan).
- Seuls les clusters/séjours qui deviennent un **dossier dédié** (événement ou séjour) déclenchent un appel de géocodage — les dossiers mensuels/trimestriels "routine" n'incluent pas de lieu et n'appellent donc pas ce service.

---

## Utilisation (CLI)

### Workflow typique

```bash
# 1. Vérifier la configuration (chemins, exiftool…)
./run.sh doctor

# 2. Scanner les fichiers et générer le plan — aucun fichier n'est touché
./run.sh plan

# 3. Simuler l'exécution (dry-run) — affiche ce qui serait fait, rien ne bouge
./run.sh run

# 4. Exécuter pour de vrai — UNIQUEMENT avec --execute
./run.sh run --execute
```

> **Important** : `run` **sans** `--execute` est toujours un dry-run, peu importe `DRY_RUN_DEFAULT` dans `.env`.
> Seul `--execute` déclenche les opérations réelles (copie/déplacement de fichiers).

---

### `doctor`
Valide la configuration et affiche les chemins résolus.
```bash
./run.sh doctor
```

### `plan`
Scanne `SOURCE_ROOT` et écrit `OUTPUT_ROOT/plan.jsonl`. **Aucun fichier n'est modifié.**

```bash
./run.sh plan
# Sans ExifTool (plan minimal) :
./run.sh plan --skip-exiftool
```

Options :
- `--batch-size` : taille des lots envoyés à ExifTool (défaut : 200)
- `--skip-exiftool` : n'appelle pas ExifTool, `exif` sera `{}` dans le plan
- `--mode copy|move` : intention d'exécution dans le plan (par défaut `MOVE_MODE`)
- `--reclassify-scope auto|off` : en `auto`, les mois impactés par des fichiers nouveaux/modifiés sont re-évalués

### `run`
Exécute les opérations du plan généré.

```bash
# Dry-run (défaut) — affiche ce qui serait fait, ne touche rien
./run.sh run

# Exécution réelle — copie/déplace les fichiers
./run.sh run --execute
```

Options :
- `--execute` : **requis** pour effectuer les opérations réelles (sinon dry-run)
- `--mode copy|move` : override de `MOVE_MODE`
- `--plan-file` : nom du plan sous `OUTPUT_ROOT` (défaut `plan.jsonl`)

Comportement :
- `copy` : copie vers `TARGET_ROOT/<destination>`
- `move` : déplacement direct vers `TARGET_ROOT/<destination>`
- `quarantine` : copie vers `QUARANTINE_ROOT/<destination>`
- journalisation des runs dans `OUTPUT_ROOT/state.sqlite`

Itératif :
- la base `state.sqlite` conserve les empreintes et placements déjà appliqués
- lors d'un nouveau `plan`, les fichiers nouveaux/modifiés marquent des mois impactés
- les placements de ces mois sont comparés au schéma de destination courant
- si une migration est nécessaire, des actions `move` de reclassification sont ajoutées automatiquement au plan
---

## Formats de fichiers générés

### `plan.jsonl`
Chaque ligne est un objet JSON (JSON Lines).

Champs actuellement écrits :
- `source` : chemin absolu du fichier source
- `sha` : hash du fichier (selon `hash_algorithm`, défaut `md5`)
- `size` : taille en octets
- `exif` : dictionnaire ExifTool (vide si `--skip-exiftool`)
- `kind` : `file` ou `duplicate_exact` (premier niveau)
- `reference_datetime` : datetime de référence (EXIF/QuickTime/fallback filesystem)
- `action` : `copy | move | skip | quarantine | review`
- `destination` : chemin relatif cible (ou `null`)
- `reason` : raison de la décision
- `bucket_key` : mois (`YYYY-MM`) concerné par la décision
- `confidence` : score de confiance (§12), renseigné uniquement pour les dossiers dédiés événement/séjour, `null` sinon (voir plus bas)

---

## Règles métier (spec) : ce qui est en place vs ce qui est prévu

### Implémenté aujourd’hui
- Scan récursif des médias sous `SOURCE_ROOT` (extensions photo/vidéo), sans tenir compte de l’arborescence source
- Hashing streaming (`md5` par défaut)
- Appel ExifTool en JSON (si disponible) + gestion d’erreur explicite si ExifTool absent
- Calcul de `reference_datetime` par priorité (EXIF photo → QuickTime vidéo → dates fichier)
- Détection des unités logiques : `photo_single`, `video_single`, `live_pair`, `orphan_live_mov`
- Pairing Live Photo iPhone (basename → `ContentIdentifier` → proximité temporelle < 2s, dans cet ordre)
- Clustering événementiel journalier (fenêtre temporelle configurable `cluster_time_window_minutes`, **sans** fusion/séparation GPS pour l’instant)
- Décision volumétrique 2 passes (§10 spec) : dossier événement dédié si `event_min_items` atteint, sinon dossier mensuel ou trimestriel (`month_min_items`, `quarter_fallback_enabled`)
- Sous-dossier `- live` automatique pour les `.mov` Live, dans le dossier parent choisi
- Gestion des orphelins Live selon `orphan_live_policy` (`review` | `video_single` | `ignore`)
- Extraction GPS (EXIF photo + QuickTime vidéo) et géocodage inverse via Nominatim/OpenStreetMap (gratuit, avec cache local JSON et limitation à 1 requête/s), utilisé pour suffixer les dossiers événement/séjour avec la ville (`<lieu>`) — repli sur `Lieu inconnu` si pas de GPS ou pas de réseau
- Héritage GPS pour les vidéos sans GPS (photo la plus proche en temps, ±20 min) + estimation journalière prudente si le jour ne contient qu’un seul cluster (§7)
- Clustering journalier GPS-aware (§8.3) : fusion/séparation selon zone (`same_place_radius_m`, `place_change_radius_m`), tolérance de fusion si même zone malgré un écart temporel plus grand, séparation au retour vers une zone habituelle
- Détection des « zones habituelles » (§7.2) simplifiée : une zone est habituelle si elle apparaît sur au moins `usual_place_min_distinct_days` jours distincts **du batch en cours** (pas encore d’apprentissage historique inter-runs, voir limites)
- Fusion des séjours multi-jours (§9) : clusters journaliers consécutifs (ou 1 jour vide max) fusionnés si même secteur GPS (`stay_radius_km`) et pas de retour vers une zone habituelle ; qualification `Vacances`/`Sortie`/`Séjour`/`Evenement` (§9.3, simplifiée)
- Nommage des dossiers événement/séjour avec lieu + compression de plage de dates (§11.4) : `YYYY.MM.DD`, `YYYY.MM.DD~DD`, `YYYY.MM.DD~MM.DD`, `YYYY.MM.DD~YYYY.MM.DD`
- Scoring de confiance (§12) calculé et stocké sur chaque item événement/séjour dédié, à titre informatif : **le mode reste full auto par défaut** (`confidence_review_enabled: false`), le score ne bloque jamais une décision sauf si ce paramètre est activé
- Génération d’un artefact `plan.jsonl` dans `OUTPUT_ROOT` avec mapping `source -> destination`
- Déduplication exacte minimale par hash (état historique + doublons du run)
- Exécution réelle copy/move/quarantine via `run --execute`
- Persistance SQLite (`state.sqlite`) pour runs, opérations et placements déjà traités
- Reclassification incrémentale : détecte les mois impactés par de nouveaux fichiers ; si le run recalcule le seuil mensuel/trimestriel (`month_min_items`) pour ce mois différemment de ce qui était déjà en place, les fichiers déjà placés en dossier mensuel (`YYYY.MM Vie de famille`) ou trimestriel (`YYYY.Qn Vie de famille`) — photos et sous-dossier `- live` inclus — sont redéplacés vers le bon dossier, en s'alignant sur le choix fait pour les nouveaux fichiers de ce mois. Les dossiers vidés par ce redéplacement sont automatiquement nettoyés. **Ne recalcule pas** encore les dossiers événementiels/séjours déjà placés (voir limites ci-dessous).
- Sécurité : validation basique des chemins (évite `TARGET_ROOT` dans `SOURCE_ROOT`)

### Limites connues / prévu par `spec.md` (non implémenté à ce stade)

1) **Zones habituelles (§7.2)** :
- Apprentissage simplifié : uniquement à partir du batch en cours de traitement (fréquence par jours distincts), pas d’historique persistant inter-runs. Un lieu réellement habituel mais peu photographié récemment peut donc ne pas être reconnu comme tel.

2) **Qualification de séjour (§9.3)** :
- Pas de distinction fine « lieu inhabituel éloigné » vs « ambigu » : la qualification utilise un simple booléen habituel/inhabituel.

3) **Scoring de confiance (§12)** :
- Le score est calculé et stocké, mais ne déclenche pas de revue humaine par défaut (choix assumé « full auto »). Activer `confidence_review_enabled: true` dans `config.yaml` pour restaurer le comportement de la spec (score < 3 → `A_REVOIR`).

4) **Doublons logiques / quasi-doublons (§6.2)** :
- Non implémenté (seul le hash exact est géré).

5) **Reclassification incrémentale complète** :
- La bascule mensuel ↔ trimestriel est recalculée automatiquement (en s'alignant sur la décision prise pour les nouveaux fichiers du mois impacté). En revanche, les fichiers déjà placés en dossier événementiel ou séjour ne sont pas re-évalués automatiquement (nécessiterait de reconstruire le clustering complet — pairing Live Photo, GPS, jours — du mois à partir de l’historique, pas seulement des nouveaux fichiers). Si aucun nouveau fichier routinier n'atterrit dans un mois impacté ce run, le repli reste le dossier mensuel simple par défaut (heuristique conservatrice).

---

## Dépannage

### ExifTool introuvable
Si `plan` échoue avec une erreur ExifTool, soit :
- installe ExifTool (DietPi/Debian : `apt-get install libimage-exiftool-perl`)
- soit génère un plan minimal :
```bash
python -m photo_sorter.cli plan --env-file .env --config config.yaml --skip-exiftool
```

---

## Roadmap immédiate (prochaine implémentation)
- Reclassification incrémentale complète pour les dossiers événement/séjour (recalcul de clustering complet, pas seulement mensuel/trimestriel)
- Apprentissage des zones habituelles à partir de l’historique persistant (pas seulement le batch en cours)
- Détection des quasi-doublons (§6.2)
- Rapports CSV/HTML de synthèse du plan

