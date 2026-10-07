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

### On a Synology NAS (prebuilt image, no SSH)

A GitHub Action builds the image on every push to `main` and tags releases `vX.Y.Z`.

1. Create the folders in File Station: your photo share (for example `photo`, with an `inbox` sub-folder) and
   `docker/synopixtri` for the data.
2. In the DSM **Docker** app, open **Registry**, search `synopixtri`, download the image (tag `latest` for a
   release, `edge` for the newest development build). This works once the maintainer has set the Docker Hub
   secrets (see below).
3. **Image** -> select it -> **Launch**. Map port 8080, and two folders: `/volume1/photo` -> `/photos` and
   `/volume1/docker/synopixtri` -> `/data`. Set the environment variable `SYNOPIXTRI_PASSWORD`.
   Or paste `docker-compose.yml` in a Portainer stack.
4. To update: download the image again, then recreate the container (the data lives in `/data`).

The image is also published to `ghcr.io/galaticlag/synopixtri` (usable with `docker pull` or Portainer; the DSM 6
Registry search cannot see it).

**Maintainers**: to publish on Docker Hub, create an access token on hub.docker.com (Account settings -> Security),
then add the repository secrets `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` (GitHub: Settings -> Secrets and
variables -> Actions). The next push publishes `<user>/synopixtri`.

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
