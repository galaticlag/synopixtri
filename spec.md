# Spécification complète : Tri automatique photos/vidéos sur NAS Synology

**Version :** 1.1  
**Date :** 2026-08-02  
**Usage :** Ce document sert de base (SPEC.md) pour un développement assisté IDE + Copilot. Il décrit l'ensemble des règles métier, structures de données, algorithmes et paramètres pour automatiser le classement d'une photothèque personnelle stockée sur un NAS Synology DS1812+ accessible via SMB/Samba.

---

## 1. Contexte et objectifs

### 1.1 Situation actuelle
- Stockage : NAS Synology DS1812+, partage SMB monté localement.
- Organisation manuelle actuelle : `YYYY/YYYY.MM Libellé` ou `YYYY/YYYY.MM.DD Événement, Lieu`.
- Sources : majoritairement iPhone (HEIC, JPG, MOV, MP4) dont Live Photos (paire image + `.mov` même nom).
- Volumétrie : plusieurs années, plusieurs dizaines de milliers de fichiers.

### 1.2 Objectifs
- Classer **physiquement** les fichiers dans une arborescence de dossiers (pas seulement taguer).
- Automatiser 80–95 % du travail ; ne laisser qu'un renommage final humain si besoin.
- Gérer correctement : dates EXIF/QuickTime, GPS, Live Photos iPhone, événements mono-jour, séjours multi-jours, doublons.
- Produire un plan d'actions lisible (dry-run) avant tout déplacement réel.

### 1.3 Principes directeurs
- **Date + GPS + règles métier** = cœur de valeur (pas d'IA visuelle nécessaire en MVP).
- **Unité logique** = photo seule, vidéo seule, ou Live Pair (photo + `.mov` associé).
- **Conservatisme** : jamais de suppression auto sur doublon logique ; cas ambigus isolés en `A_REVOIR`.
- **Configurabilité** : tous les seuils et politiques externalisés.

---

## 2. Arborescence cible

### 2.1 Structure racine
```
/<année>/
  /YYYY.MM Vie de famille/
  /YYYY.MM Vie de famille/YYYY.MM Vie de famille - live/
  /YYYY.Qn Vie de famille/                # fallback trimestriel
  /YYYY.Qn Vie de famille/YYYY.Qn Vie de famille - live/
  /YYYY.MM.DD <libellé>, <lieu>/
  /YYYY.MM.DD <libellé>, <lieu>/YYYY.MM.DD <libellé>, <lieu> - live/
  /YYYY.MM.DD~DD <libellé>, <lieu>/       # multi-jours même mois
  /YYYY.MM.DD~DD <libellé>, <lieu>/... - live/
  /YYYY.MM.DD~MM.DD <libellé>, <lieu>/    # multi-jours mois
  /YYYY.MM.DD~MM.DD <libellé>, <lieu>/... - live/
  /YYYY.MM.DD~YYYY.MM.DD <libellé>, <lieu>/  # rare
  /A_REVOIR/
  /A_REVOIR/live/
  /DUPLICATES/                             # quarantaine
```

### 2.2 Formats de nommés

| Type | Format | Exemple |
|------|--------|---------|
| Mensuel routine | `YYYY.MM Vie de famille` | `2021.01 Vie de famille` |
| Trimestriel routine | `YYYY.Qn Vie de famille` | `2021.Q1 Vie de famille` |
| Événement jour unique | `YYYY.MM.DD <libellé>, <lieu>` | `2021.01.14 Anniversaire XX, Provins` |
| Multi-jours même mois | `YYYY.MM.DD~DD <libellé>, <lieu>` | `2021.01.13~16 Sortie à Lille` |
| Multi-jours mois différents | `YYYY.MM.DD~MM.DD <libellé>, <lieu>` | `2021.07.21~08.10 Vacances Vendée` |
| Multi-jours années différentes | `YYYY.MM.DD~YYYY.MM.DD <libellé>, <lieu>` | `2024.12.28~2025.01.03 Séjour montagne` |

### 2.3 Règle `- live`
- Le sous-dossier ` - live` suit **toujours** le dossier parent choisi (événement, mensuel, ou trimestriel).
- Le sous-dossier est créé même s'il n'y a qu'un seul couple Live.
- Exemples :
  - `2021/2021.01.14 Anniversaire XX, Provins/2021.01.14 Anniversaire XX, Provins - live/`
  - `2021/2021.01.13~16 Sortie à Lille/2021.01.13~16 Sortie à Lille - live/`
  - `2021/2021.07.21~08.10 Vacances au XXX, Vendée/2021.07.21~08.10 Vacances au XXX, Vendée - live/`
  - `2021/2021.Q1 Vie de famille/2021.Q1 Vie de famille - live/`

---

## 3. Unités logiques médias

Le moteur ne raisonne pas fichier, mais, mais doit transformer les fichiers en unités logiques **unitélogiques` |
|----------|
| `photo_single` | Photo seule (HEIC/JPG) |  | Photo seule (HEIC/JPG) |
| `video_single` | Vidéo seule (`.MOV/MP4` non-Live) | | `video_single` | ` | `live_pair` | Paire photo + `.mov` Live | ` | Vidéo seule (MOV` non-Live)` |
` | Vidéo seule | `photo_single` + `.MOV/MP4` non-Live` |
| Vidéo seule (HEIC/JPG` + `.mov` Live | 
| ` | `live_pairMOV` seule` | ` | `.mov` seulMOV` non-Live` |
| `orphan_live_mov` | `.mov` Live probable sans photo trouvée | ` video_single` | `.mov` Live orphelin` | ` |_live_mov` `.mov` Live orphelin`  | ` |
| `duplicate_exact` | ` | `duplicate_exact` | ` Doublon binaire exact | `duplicate_exact` |  ` (hash identique) | ` |
| `review_case` | ` | `review_case` | Cas ambigu nécessitant revue manuelle |  | `review_case` | Cas ambigu nécessitant revue manuelle |

> **Note** : Seuls `photo_single` et `video_single` comptent pour les seuils volumétriques (voir §10). Les `live_pair` sont comptabilisés à 1 côté photo + 1 côté live-mov pour le sous-dossier ` - live`.

---

## 4. Extraction des métadonnées (ExifTool)

### 4.1 Outil
ExifTool en mode JSON (`exiftool -j -G -time:all -gps:all -quicktime:all -file:all -Common:all <fichiers>`). ExifTool lit EXIF (photos) **et** QuickTime (vidéos iPhone), indispensable pour unifier la chaîne. [web:42][web:37]

### 4.2 Champs clés à extraire par fichier

| Groupe | Champs |
|--------|--------|
| Identité | `SourceFile`, `FileName`, `Extension`, `FileSize`, `FileModifyDate`, `FileCreateDate` |
| Hash | `MD5` (ou SHA256) calculé à part |
| Dates photo | `EXIF:DateTimeOriginal`, `EXIF:CreateDate`, `EXIF:ModifyDate` |
| Dates vidéo | `QuickTime:CreateDate`, `QuickTime:MediaCreateDate`, `QuickTime:ContentIdentifier`, `QuickTime:TrackCreateDate` |
| GPS | `EXIF:GPSLatitude`, `EXIF:GPSLongitude`, `EXIF:GPSLatitudeRef`, `EXIF:GPSLongitudeRef`, `QuickTime:GPS*` |
| Vidéo | `QuickTime:Duration`, `QuickTime:VideoFrameRate` |
| Live | `QuickTime:ContentIdentifier` (identifiant de couplage Live Photo) |

### 4.3 Date de référence unique (par unité logique)

**Ordre de priorité absolu :**

1. `EXIF:DateTimeOriginal` (photo)  
2. `QuickTime:CreateDate` / `QuickTime:MediaCreateDate` (vidéo)  
3. Autres dates métadonnées (`FileModifyDate`, `FileCreateDate`)  
4. Date système fichier (dernier recours)

**Règle Live Pair** : la date de référence du couple = date de la **photo** (plus fiable que le `.mov` qui peut être décalé). [web:53][web:64]

---

## 5. Détection et consolidation Live Photos iPhone

### 5.1 Algorithme de détection (dans cet ordre)

1. **Même nom de base** : `IMG_1234.HEIC` + `IMG_1234.MOV` (insensible à la casse, extension à part). [web:50][web:56]
2. **Même `ContentIdentifier`** : marqueur Apple de couplage Live Photo. [web:55][web:57]
3. **Proximité temporelle extrême** (< 2 s) + même dossier source + même appareil → secours si noms altérés. [web:53][web:64]

### 5.2 Règles de classement Live (impératives)

| Règle | Description |
|-------|-------------|
| **Photo principale** | Reste dans le dossier parent de l'événement/mois/trimestre. |
| **Fichier `.mov` Live** | Déplacé dans un sous-dossier nommé exactement `<nom_dossier_parent> - live`. |
| **Sous-dossier systématique** | Créé même pour un seul couple Live. |
| **Vidéos non-Live** | Ne vont **jamais** dans ce sous-dossier. |
| **Orphelins Live** | `orphan_live_mov` → classe en `video_single` ou `A_REVOIR` selon config. |

### 5.3 Cas limites
- Photo sans `.mov` → `photo_single`.
- `.mov` sans photo → `orphan_live_mov` (ne crée pas de sous-dossier `-live` seul).
- Lien ambigu (nom identique mais `ContentIdentifier` différent ou absent) → `review_case`.

---

## 6. Déduplication

### 6.1 Doublons exacts
- **Définition** : hash binaire identique (MD5/SHA256).
- **Action** : ignorés à l'import, ou déplacés vers `DUPLICATES/`, ou signalés dans rapport. **Jamais** suppression auto silencieuse.

### 6.2 Doublons logiques (quasi-doublons)
**Critères** (plusieurs réunis = signal) :
- Timestamp très proche (± quelques secondes).
- Taille fichier proche (± 5 %).
- Durée vidéo proche.
- Nom de base identique ou très proche (ex: `IMG_1234` vs `IMG_1234(1)`).
- Même scène probable (même temps + même lieu).

**Action** : signalés dans rapport `review_case` → revue humaine. **Pas de suppression auto.**

---

## 7. Détection et qualification du lieu

### 7.1 Sources de lieu (priorité décroissante)
1. GPS exact du fichier (photo ou vidéo). [web:37]
2. GPS hérité : vidéo sans GPS → emprunte GPS de la photo la plus proche dans le temps (fenêtre ± 10–20 min, même cluster). [web:34]
3. Estimation journalière prudente : si plusieurs médias du même jour ont un GPS, les autres sans GPS peuvent hériter de la zone dominante **seulement si** le jour ne contient qu'un seul cluster événementiel. [web:34][web:69][web:89]
4. Inconnu.

### 7.2 Lieux habituels (apprentissage)
- Construire une base de « zones de vie » à partir de l'historique : regroupement par geohash (précision ~100–300 m) ou DBSCAN sur coordonnées.
- Étiquettes suggérées : `Domicile`, `Famille`, `Travail`, `Habituels`.
- Un lieu est « habituel » s'il concentre > X % des médias sur une période glissante (ex: 12 mois).

### 7.3 Lieux inhabituels / rares
- Tout lieu hors zones habituelles.
- Utilisé comme **signal fort** d'événement/sortie/vacances.

---

## 8. Clustering événementiel (niveau jour)

### 8.1 Entrée
Liste de toutes les unités logiques triées par `reference_datetime` croissante.

### 8.2 Fenêtre temporelle initiale
- Seuil par défaut : **45 minutes** d'écart entre deux unités consécutives pour rester dans le même cluster journalier. [web:32]
- Configurable : `CLUSTER_TIME_WINDOW_MINUTES`.

### 8.3 Règles de fusion / séparation au sein d'une journée

| Condition | Action |
|-----------|--------|
| Écart < seuil **et** même zone GPS (rayon `SAME_PLACE_RADIUS_M`) | Fusionner (même événement). |
| Écart > seuil **mais** même zone GPS + continuité visuelle probable | Fusionner (tolérance). |
| Écart > seuil **et** changement zone GPS significatif (> `PLACE_CHANGE_RADIUS_M`) | Séparer (nouvel événement). |
| Retour vers zone habituelle après zone inhabituelle | Séparer (fin de l'événement/sortie). |

### 8.4 Sortie journalière
Chaque cluster journalier produit :
- `event_id` (ex: `evt_20210721_01`)
- Liste d'unités logiques
- `start_dt`, `end_dt`
- `place` (GPS centroïde + libellé ville/région)
- `is_usual_place` (bool)
- `media_count` = `photo_single` + `video_single` (hors Live)
- `live_count` = nombre de `live_pair`
- `has_photos`, `has_videos`

---

## 9. Détection des séjours multi-jours (vacances, sorties prolongées)

### 9.1 Principe
Fusionner des clusters journaliers consécutifs en un **séjour** si continuité géographique + pas de retour domicile.

### 9.2 Règles de fusion multi-jours

| Condition | Action |
|-----------|--------|
| Jours consécutifs (ou 1 jour vide max) **et** même secteur GPS (rayon `STAY_RADIUS_KM` ~ 10–50 km) | Fusionner en un séjour. |
| Jour vide autorisé si jours J-1 et J+1 sont dans le même secteur **et** pas de retour zone habituelle | Fusionner. |
| Retour vers zone habituelle entre deux séquences | **Ne pas fusionner** (deux séjours distincts). |
| Deux week-ends séparés au même lieu, avec retour routine entre | **Ne pas fusionner**. |

### 9.3 Qualification du séjour (libellé provisoire)

| Critères | Libellé suggéré |
|----------|-----------------|
| Durée ≥ 3 jours **et** lieu inhabituel éloigné | `Vacances` |
| 1–2 jours hors lieu habituel | `Sortie` |
| Durée ≥ 3 jours lieu inhabituel mais ambigu | `Séjour` |
| Autre | `Evenement` |

Le libellé final est **provisoire** ; l'utilisateur renommera si besoin. L'essentiel est le bon regroupement.

---

## 10. Seuils volumétriques : événement dédié vs mois vs trimestre

### 10.1 Philosophie
- Les gros événements sortent dans leur propre dossier.
- Le reliquat « routine » (peu de médias, lieux habituels) peut être trop maigre pour justifier un dossier mensuel → bascule en trimestre.

### 10.2 Paramètres configurables

| Paramètre | Défaut suggéré | Description |
|-----------|----------------|-------------|
| `EVENT_MIN_ITEMS` | **20** | Nb min d'éléments **hors Live** (`photo_single` + `video_single`) pour créer un dossier événement dédié. |
| `MONTH_MIN_ITEMS` | **15** | Nb min d'éléments **hors Live** restants dans le mois pour garder un dossier mensuel. |
| `QUARTER_FALLBACK_ENABLED` | `true` | Active le basculement trimestriel si mois sous le seuil. |

### 10.3 Algorithme de décision (deux passes)

#### Passe 1 — Extraction des événements forts
Pour chaque cluster journalier ou séjour multi-jours :
```
si cluster.type in [vacances, sortie, evenement, sejour] 
   et media_count >= EVENT_MIN_ITEMS :
    → crée un dossier dédié (format selon durée 1 jour ou multi-jours)
sinon :
    → rattache au bucket "routine" du mois concerné
```

#### Passe 2 — Organisation du reliquat routine
Pour chaque mois `YYYY.MM` :
```
routine_count = somme des media_count des clusters non extraits
si routine_count >= MONTH_MIN_ITEMS :
    → dossier mensuel "YYYY.MM Vie de famille"
sinon si QUARTER_FALLBACK_ENABLED :
    → dossier trimestriel "YYYY.Qn Vie de famille"  (Q1=Jan-Mar, Q2=Apr-Jun, etc.)
sinon :
    → dossier mensuel quand même (fallback minimal)
```

### 10.4 Conséquences sur les sous-dossiers `- live`
- Le sous-dossier `- live` suit **toujours** le dossier parent choisi (événement, mensuel, ou trimestriel).
- Exemple : `2021/2021.Q1 Vie de famille/2021.Q1 Vie de famille - live/`

---

## 11. Nommage des dossiers

### 11.1 Formats autorisés

| Type | Format | Exemple |
|------|--------|---------|
| Mensuel routine | `YYYY.MM Vie de famille` | `2021.01 Vie de famille` |
| Trimestriel routine | `YYYY.Qn Vie de famille` | `2021.Q1 Vie de famille` |
| Événement 1 jour | `YYYY.MM.DD <libellé>, <lieu>` | `2021.01.14 Anniversaire XX, Provins` |
| Multi-jours même mois | `YYYY.MM.DD~DD <libellé>, <lieu>` | `2021.01.13~16 Sortie à Lille` |
| Multi-jours mois différents | `YYYY.MM.DD~MM.DD <libellé>, <lieu>` | `2021.07.21~08.10 Vacances Vendée` |
| Multi-jours années différentes | `YYYY.MM.DD~YYYY.MM.DD <libellé>, <lieu>` | `2024.12.28~2025.01.03 Séjour` |

### 11.2 Règles de libellé `<libellé>`

1. Si séjour qualifié → `Vacances`, `Sortie`, `Séjour`, `Evenement`.
2. Sinon si cluster dense + lieu inhabituel → `Evenement`.
3. Sinon → `Evenement` (neutre).
4. L'utilisateur renomme ensuite (ex: `Anniversaire XX`).

### 11.3 Règles de lieu `<lieu>`
1. Ville via géocodage inverse (OpenStreetMap/Nominatim, cache local).
2. Région/département si ville absente.
3. Zone connue personnalisée (ex: `Vendée`, `Lille`).
4. `Lieu inconnu` en dernier recours.

### 11.4 Compression de plage de dates
| Cas | Format |
|-----|--------|
| Même jour | `YYYY.MM.DD` |
| Même mois | `YYYY.MM.DD~DD` |
| Mois différents, même année | `YYYY.MM.DD~MM.DD` |
| Années différentes | `YYYY.MM.DD~YYYY.MM.DD` |

---

## 12. Scoring de confiance (pour décider auto vs revue)

### 12.1 Barème (exemple)

| Critère | Points |
|---------|--------|
| Lieu inhabituel | +3 |
| `media_count` ≥ 30 | +2 |
| Durée cluster 1h–8h | +2 |
| Mélange photos + vidéos | +1 |
| Week-end / soirée (18h–23h) | +1 |
| Lieu domicile très fréquent + peu de médias | -2 |
| Métadonnées faibles / incohérentes | -2 |
| Seuil `EVENT_MIN_ITEMS` non atteint | -1 |

### 12.2 Décision par score

| Score | Action |
|-------|--------|
| ≥ 6 | **Auto-move** dossier dédié (événement/séjour) |
| 3–5 | **Auto-move** dossier générique correct (mois/trimestre) |
| < 3 | **Review** → `A_REVOIR/` + rapport |

---

## 13. Pipeline de traitement (ordre recommandé)

1. **Scan** fichiers source (récursif, filtre extensions connues).
2. **Extraction métadonnées** ExifTool → JSON.
3. **Calcul hash** (MD5/SHA256) pour doublons.
4. **Détection doublons exacts** → marquer `duplicate_exact`.
5. **Détection Live Photos** → consolider en `live_pair` / `orphan_live_mov`.
6. **Calcul date de référence** par unité logique.
7. **Estimation / propagation lieu** (GPS, héritage photo→vidéo, estimation journalière prudente).
8. **Clustering journalier** (fenêtre temporelle + GPS).
9. **Fusion multi-jours** (séjours/vacances).
10. **Qualification type** (vacances, sortie, événement, routine).
11. **Passe 1** : extraction événements forts (`media_count >= EVENT_MIN_ITEMS`).
12. **Passe 2** : agrégation reliquat routine par mois → décision mensuel vs trimestriel.
13. **Génération noms de dossiers** + sous-dossiers `- live`.
14. **Scoring confiance** par unité.
15. **Dry-run** : export plan CSV/JSON + rapport HTML.
16. **Validation humaine** (optionnelle).
17. **Exécution** : `move` (ou `copy` pour premier test) vers arborescence cible.
18. **Journal** : log structuré (JSONL) de chaque action.

---

## 14. Paramètres configurables (fichier `config.yaml`)

```yaml
# Chemins
source_root: "/mnt/nas_photos/incoming"
target_root: "/mnt/nas_photos/sorted"
quarantine_root: "/mnt/nas_photos/_quarantine"
review_root: "/mnt/nas_photos/_A_REVOIR"

# ExifTool
exiftool_path: "exiftool"

# Clustering
cluster_time_window_minutes: 45
same_place_radius_m: 300
place_change_radius_m: 1000
stay_radius_km: 25
max_gap_days_in_stay: 1

# Seuils volumétriques
event_min_items: 20
month_min_items: 15
quarter_fallback_enabled: true

# Live Photos
live_subfolder_suffix: " - live"
orphan_live_policy: "review"  # "review" | "video_single" | "ignore"

# Doublons
duplicate_policy: "quarantine"  # "quarantine" | "skip" | "report_only"

# Lieu
usual_places:
  - name: "Domicile"
    lat: 48.86
    lon: 2.35
    radius_km: 2
  - name: "Famille"
    lat: 48.80
    lon: 2.40
    radius_km: 3

# Géocodage
geocode_cache: "./cache/geocode.json"
geocode_provider: "nominatim"

# Scoring
scoring:
  unusual_place: 3
  media_count_ge_30: 2
  duration_1h_8h: 2
  mixed_photo_video: 1
  weekend_evening: 1
  usual_place_few_media: -2
  weak_metadata: -2
  below_event_min_items: -1

decision_thresholds:
  auto_move_dedicated: 6
  auto_move_generic: 3

# Exécution
dry_run_default: true
move_mode: "move"  # "move" | "copy"
log_level: "INFO"
```

---

## 15. Sorties attendues du moteur

### 15.1 Plan d'actions (JSONL — une ligne par unité logique)

```json
{
  "source_path": "incoming/2021/07/IMG_1234.HEIC",
  "unit_type": "live_pair",
  "reference_datetime": "2021-07-21T18:42:10+02:00",
  "event_id": "evt_20210721_vendee_01",
  "stay_id": "stay_20210721_0810_vendee",
  "target_folder": "2021/2021.07.21~08.10 Vacances au XXX, Vendée",
  "target_live_folder": "2021/2021.07.21~08.10 Vacances au XXX, Vendée/2021.07.21~08.10 Vacances au XXX, Vendée - live",
  "confidence": 0.92,
  "score": 7,
  "decision": "auto_move",
  "reasons": [
    "live_pair_detected",
    "gps_in_vacation_zone",
    "multi_day_stay_detected",
    "media_count_ge_30"
  ],
  "hash": "a1b2c3d4...",
  "is_live_photo": true,
  "live_mov_source": "incoming/2021/07/IMG_1234.MOV"
}
```

### 15.2 Rapports annexes
- `report_summary.csv` : résumé par dossier cible (nb fichiers, type, score moyen).
- `report_review.csv` : tous les `review_case` + `orphan_live_mov` + doublons logiques.
- `report_duplicates.csv` : doublons exacts détectés.
- `plan.html` : visualisation navigable (arborescence + compteurs).

---

## 16. Garde-fous techniques

| Garde-fou | Description |
|-----------|-------------|
| **Dry-run par défaut** | Aucune écriture sans confirmation explicite. |
| **Mode copy d'abord** | Premiers tests en `copy` pour vérifier l'arborescence. |
| **Pas de suppression auto** | Doublons logiques jamais supprimés ; exacts mis en quarantaine. |
| **Journal immuable** | Chaque décision loguée (JSONL) pour audit / rollback. |
| **Règles externalisées** | Tous les seuils dans `config.yaml`, pas de *hard-coding*. |
| **Cas ambigus isolés** | `A_REVOIR/` hors flux principal. |
| **Idempotence** | Relancer le script sur le même source ne duplique pas (détection hash + destination existante). |

---

## 17. MVP recommandé (périmètre minimal viable)

| Composant | Technologie |
|-----------|-------------|
| Langage | Python 3.11+ |
| Métadonnées | `exiftool` (subprocess JSON) + `exiftool` wrapper optionnel (`exiftool` Python) |
| Hash | `hashlib` (MD5 pour rapidité, SHA256 si besoin) |
| GPS / Géocodage | `geopy` + cache local JSON (Nominatim, rate-limited) |
| Clustering | Python pur (pas de ML requis) |
| Config | `yaml` (`pyyaml`) |
| Rapports | `csv`, `json`, `jinja2` pour HTML |
| Exécution | CLI (`typer` ou `argparse`) |
| Logs | `loguru` ou `structlog` |
| Tests | `pytest` + jeux de fixtures (quelques centaines de fichiers représentatifs) |

**Périmètre MVP fonctionnel :**
- Scan + ExifTool + hash.
- Live Pair detection (nom + ContentIdentifier).
- Clustering journalier (fenêtre 45 min + GPS).
- Fusion multi-jours (règles §9).
- Seuils `EVENT_MIN_ITEMS` / `MONTH_MIN_ITEMS` + fallback trimestre.
- Génération noms dossiers + sous-dossiers `- live`.
- Dry-run + plan CSV/HTML.
- Mode `copy` puis `move`.

> **Note** : ce MVP couvre déjà 85–95 % du besoin sans IA visuelle. L'IA (détection gâteau, OCR, reconnaissance visuelle) peut venir en surcouche ultérieure pour affiner les libellés.

---

## 18. Structure de projet suggérée

```
photo-sorter/
├── config.yaml
├── pyproject.toml
├── src/
│   └── photo_sorter/
│       ├── __init__.py
│       ├── cli.py
│       ├── config.py
│       ├── models.py          # dataclasses : MediaUnit, Cluster, Stay, PlanItem
│       ├── exif.py            # extraction ExifTool
│       ├── hashing.py
│       ├── live.py            # détection Live Pairs
│       ├── geo.py             # GPS, géocodage, lieux habituels
│       ├── cluster.py         # clustering journalier
│       ├── stay.py            # fusion multi-jours
│       ├── naming.py          # génération noms dossiers
│       ├── scoring.py
│       ├── planner.py         # passes 1 & 2, décision finale
│       ├── executor.py        # dry-run, copy, move, logs
│       └── reports.py         # CSV, HTML
├── tests/
│   ├── fixtures/
│   └── test_*.py
└── scripts/
    └── run_dry_run.sh
```

---

## 19. Exemples de prompts Copilot utiles

> **Prompt 1 — Modèles de données**  
> « Crée les dataclasses Python `MediaUnit`, `LivePair`, `DailyCluster`, `MultiDayStay`, `PlanItem` avec tous les champs nécessaires selon la SPEC (dates, GPS, hash, type, comptages, scoring). Inclue des méthodes `to_dict()` et `from_dict()` pour sérialisation JSON. »

> **Prompt 2 — Extraction ExifTool**  
> « Écris une fonction `extract_metadata(file_paths: list[Path]) -> list[dict]` qui appelle ExifTool en batch (JSON), gère les erreurs, et retourne une liste normalisée avec les champs : SourceFile, FileName, Extension, FileSize, DateTimeOriginal, CreateDate, MediaCreateDate, QuickTime:CreateDate, QuickTime:ContentIdentifier, GPSLatitude, GPSLongitude, Duration, FileModifyDate. »

> **Prompt 3 — Détection Live Pairs**  
> « Implémente `detect_live_pairs(units: list[MediaUnit]) -> tuple[list[LivePair], list[MediaUnit], list[MediaUnit]]` qui retourne (pairs confirmées, orphelins live, unités restantes). Règles : même nom de base (insensible casse) OU même ContentIdentifier OU proximité < 2s même dossier. »

> **Prompt 4 — Clustering journalier**  
> « Fonction `cluster_daily(units: list[MediaUnit], window_min=45, same_place_radius_m=300) -> list[DailyCluster]` : trie par date, groupe tant que l'écart < window_min ET (même zone GPS OU pas de GPS des deux côtés). Retourne clusters avec start/end, centroïde GPS, liste unités, comptages photo/video/live. »

> **Prompt 5 — Fusion multi-jours**  
> « Fonction `merge_multi_day(clusters: list[DailyCluster], stay_radius_km=25, max_gap_days=1, usual_places=list[Place]) -> list[MultiDayStay]` : fusionne clusters consécutifs si même secteur GPS et pas de retour zone habituelle. Gère 1 jour vide autorisé. Qualifie le séjour (Vacances/Sortie/Séjour/Evenement). »

> **Prompt 6 — Planification deux passes**  
> « Implémente `build_plan(stays: list[MultiDayStay], clusters: list[DailyCluster], config: Config) -> list[PlanItem]` : Passe 1 extrait les séjours/clusters avec media_count >= EVENT_MIN_ITEMS vers dossiers dédiés. Passe 2 agrège le reliquat par mois, décide mensuel vs trimestriel (MONTH_MIN_ITEMS, QUARTER_FALLBACK_ENABLED). Génère target_folder + target_live_folder. »

> **Prompt 7 — Nommage dossiers**  
> « Fonction `format_folder_name(stay_or_cluster, config) -> str` selon les formats de la SPEC : mensuel `YYYY.MM Vie de famille`, trimestriel `YYYY.Qn Vie de famille`, événement 1 jour `YYYY.MM.DD Libellé, Lieu`, multi-jours même mois `YYYY.MM.DD~DD Libellé, Lieu`, multi-jours mois différents `YYYY.MM.DD~MM.DD Libellé, Lieu`, multi-jours années différentes `YYYY.MM.DD~YYYY.MM.DD Libellé, Lieu`. »

> **Prompt 8 — Dry-run & rapports**  
> « Écris `generate_dry_run_report(plan: list[PlanItem], output_dir: Path)` qui produit : `plan.jsonl` (1 ligne/PlanItem), `summary.csv` (par dossier cible), `review.csv` (items decision=review), `duplicates.csv`, et `plan.html` (arborescence navigable avec compteurs). »

> **Prompt 9 — Exécution**  
> « Implémente `execute_plan(plan: list[PlanItem], mode: Literal["copy","move"], dry_run: bool)` : crée les dossiers cibles, déplace/copie les fichiers (photo + live .mov vers sous-dossier -live), loggue chaque action en JSONL, gère les erreurs (permissions, espace disque), idempotent (skip si dest existe même hash). »

> **Prompt 10 — CLI Typer**  
> « Crée `cli.py` avec commandes : `scan`, `plan`, `report`, `run`, `undo`. Options globales : `--config`, `--dry-run/--execute`, `--mode copy|move`, `--log-level`. »

---

## 20. Checklist de validation (Definition of Done)

- [ ] Scan complet du dossier source sans erreur.
- [ ] ExifTool extrait toutes les dates, GPS, ContentIdentifier.
- [ ] Hash calculé pour 100 % des fichiers.
- [ ] Live Pairs détectées : > 95 % de rappel sur jeu de test.
- [ ] Doublons exacts identifiés et mis en quarantaine.
- [ ] Clustering journalier cohérent (pas de fusion aberrante).
- [ ] Séjours multi-jours détectés (vacances connues retrouvées).
- [ ] Passe 1 : événements forts extraits correctement.
- [ ] Passe 2 : reliquat routine bien réparti mois/trimestre.
- [ ] Nommage dossiers conforme aux formats SPEC.
- [ ] Sous-dossiers `- live` créés au bon endroit.
- [ ] Dry-run produit plan complet + 4 rapports.
- [ ] Exécution `copy` puis `move` sans perte / duplication.
- [ ] Logs JSONL permettent audit complet.
- [ ] Config YAML couvre tous les paramètres listés.
- [ ] Tests unitaires passent sur fixtures représentatives.

---

*Fin de la spécification. Ce document est conçu pour être copié-collé comme `SPEC.md` dans le repo, puis utilisé comme référence unique pour les prompts Copilot ci-dessus.*