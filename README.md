# SynoPixtri

Automatic photo and video sorter for Synology NAS, driven by a small web interface.

> Not affiliated with or endorsed by Synology Inc. Early version (0.3), use on a copy of your photos first.

Drop your photos in an **inbox** (for example with the DS file mobile backup). SynoPixtri checks the inbox on a
schedule and files everything into a dated library, without you opening the app:

```
library/2026/2026.03 Vie de famille/            routine photos of the month
library/2026/2026.03.14 À nommer, Lyon/         an event (enough photos in one day)
library/2026/2026.07.21~08.03 À nommer, Brest/  a multi-day stay
          └─ ... - live/                         Live Photo videos
```

Rename an "À nommer" folder in File Station when you feel like it: SynoPixtri notices, keeps your name, and adds
later photos of that period to it.

## What it does

- Reads dates and GPS with ExifTool (iPhone videos are converted from UTC to local time correctly).
- Pairs Live Photos (still + short video), keeps orphan Live videos in the `- live` subfolder.
- Waits for files to be stable (not being uploaded) and for a period to be quiet before creating an event.
- Moves exact duplicates to a quarantine folder and unreadable or undated files to a review folder.
- Never deletes anything. Every move is journaled; **a whole pass can be undone** from the dashboard.
- Emergency brake: a pass that would move an unusually large number of files pauses until you confirm.
- Planned runs inside the hours you choose (handy when the NAS is off at night), plus a catch-up at startup.

### Settings in the web interface

- **Zones**: draw a circle (or give a polygon) for work, a place to ignore, home or a frequent place. Media taken in a
  work or exclusion zone are set aside (or left alone, or sent to review), optionally only on some weekdays or hours,
  with a volume exception: a day with many media in the zone goes to review instead. Home and frequent places set
  their own event threshold and place name.
- **Rules** "if ... then ...", evaluated before any grouping: conditions on zone, period, weekday, hours, photo or
  video, device, file name, extension, source sub-folder, size, dimensions, duplicates, GPS and content analysis;
  actions: set aside (reversible), leave in place, send to review, file in a folder pattern, or tag. A live preview
  tells how many inbox files a rule would catch. Ready-made templates (screenshots, messaging images, `.AAE` leftovers,
  tiny images, blurry or dark photos, documents, look-alikes). Every change is a version you can go back to.
- **Why**: each journal line explains the decision (which rule, which zone, which threshold).
- **Naming templates** for event and monthly folders, with a live preview.
- **Set-aside screen**: restore files to the inbox in one click. Nothing is deleted unless you opt in to "delete after
  N days" and confirm it.
- **Validation mode** (optional): new events wait in a list where you rename, merge, split or keep them as routine.
- **Content analysis** (Pillow only, on thumbnails, a few images per pass): blur, dark, screenshot, document and
  near-duplicate suggestions. They never act on their own: only a rule you enabled uses them. HEIC files are not analysed.
- **Reminder** for folders still named "À nommer": ntfy, webhook or e-mail.
- **Duplicates**, **timeline** and **locked folders** (a locked folder receives nothing new).

## Run it

### On a Synology NAS: Package Center (recommended)

SynoPixtri ships as a real Synology package (`.spk`): installed from Package Center with a small wizard (photo
folder, password, owner, port), started and stopped from there, with an icon in the DSM main menu. It runs the
Docker image inside, so the **Docker** package must be installed (DSM 6 or 7, amd64 models such as the DS1812+).
The icon opens the web interface in a browser tab; it is not a native DSM window.

Easiest, once: add the package source.

1. Package Center -> **Settings** -> **General**: set *Trust level* to *Any publisher*.
2. **Package Sources** -> Add: name `SynoPixtri`, location `https://galaticlag.github.io/synopixtri/packages.json`.
   Also tick *Enable beta packages* in Settings -> General until a stable release is tagged.
3. Package Center -> **Community** -> SynoPixtri -> Install, then fill the wizard.

Or manually: download `synopixtri-edge.spk` from the [releases](https://github.com/galaticlag/synopixtri/releases),
then Package Center -> **Manual Install**. To update, install the newer package over the old one: your settings
and the database are kept.

Maintainers: the source is served by GitHub Pages. Enable it once in the repository (Settings -> Pages -> Source:
*GitHub Actions*). Each push to `main` builds the package; a `vX.Y.Z` tag publishes a stable release.

### With Docker only

The image is published on every push to `main` as `ghcr.io/galaticlag/synopixtri:edge` (and `:latest`, `:X.Y.Z`
for tags). Use it with `docker pull`, Portainer, or the DSM Docker app. Mount your photo share on `/photos` and a
data folder on `/data`, and set `SYNOPIXTRI_PASSWORD` (see `docker-compose.yml`). If you also set the repository
secrets `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN`, the image is published on Docker Hub too, which the DSM 6
Docker "Registry" tab can search.

### Build it yourself

```bash
docker build -t synopixtri .
docker run -d --name synopixtri -p 8080:8080 \
  -e SYNOPIXTRI_PASSWORD=change-me \
  -v /volume1/photo:/photos -v /volume1/docker/synopixtri:/data \
  synopixtri
```

Open `http://<nas>:8080`. Put your photos in `<photos>/inbox`; the library is created in `<photos>/library`.
Mount the whole photo tree **once** (as above) so that moving between inbox and library is an atomic rename.
A `docker-compose.yml` is provided for Portainer stacks. Put the app behind the DSM reverse proxy for HTTPS.

| Variable | Default | |
|---|---|---|
| `SYNOPIXTRI_PHOTOS_ROOT` | `/photos` | Root containing the inbox and the library |
| `SYNOPIXTRI_DATA_DIR` | `/data` | Database and caches |
| `SYNOPIXTRI_PASSWORD` | none | HTTP basic auth password. Without it the API is open |
| `SYNOPIXTRI_PORT` | `8080` | |

Everything else (schedule, thresholds, home position, labels) is edited in the web interface.

## Develop

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

Needs [ExifTool](https://exiftool.org) on the path. The image is based on Debian 11 on purpose, see the Dockerfile.

## License

[GNU General Public License v3.0](LICENSE).
