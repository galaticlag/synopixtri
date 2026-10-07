"""Build the Synology package (.spk) and the Package Center source file.

    python packaging/spk/build.py --image image.tar.gz --out dist --build 12 [--beta] [--base-url URL]

``image.tar.gz`` is the output of ``docker save synopixtri:spk | gzip``. Needs Pillow (icons are drawn here,
no binary file lives in the repository).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import tarfile
import time
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PACKAGE = "synopixtri"
APP = "SYNO.SDS.SynoPixtri.Application"
# Synology CPU platforms that run an amd64 image (the DS1812+ is "cedarview").
ARCH = "x86_64 cedarview bromolow avoton braswell broadwell broadwellnk apollolake denverton grantley kvmx64 geminilake"
DESC = "Trie automatiquement vos photos et vidéos reçues dans un dossier d'arrivée vers une bibliothèque datée."
SIZES = (16, 24, 32, 48, 64, 72, 256)


def icon(size: int) -> bytes:
    """A photo frame with a mountain and a sun on a blue tile."""
    s = 512
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((16, 16, s - 16, s - 16), radius=96, fill=(31, 78, 140, 255))
    d.rounded_rectangle((96, 120, s - 96, s - 140), radius=28, fill=(255, 255, 255, 255))
    d.rectangle((124, 148, s - 124, s - 168), fill=(173, 216, 245, 255))
    d.polygon([(124, s - 168), (230, 230), (310, 330), (350, 280), (s - 124, s - 168)], fill=(46, 125, 80, 255))
    d.ellipse((330, 170, 390, 230), fill=(255, 196, 40, 255))
    d.rectangle((96, s - 112, s - 96, s - 92), fill=(255, 255, 255, 255))
    buf = io.BytesIO()
    img.resize((size, size), Image.LANCZOS).save(buf, "PNG")
    return buf.getvalue()


def lf(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n")


def add(tar: tarfile.TarFile, name: str, data: bytes, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = len(data), mode, int(time.time())
    info.uname = info.gname = "root"
    tar.addfile(info, io.BytesIO(data))


def project_version() -> str:
    match = re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    if not match:
        raise SystemExit("version not found in pyproject.toml")
    return match.group(1)


def package_tgz(image: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.GNU_FORMAT) as tar:
        config = {
            ".url": {
                APP: {
                    "type": "url",
                    "title": "SynoPixtri",
                    "desc": "Tri automatique des photos",
                    "icon": "images/icon_{0}.png",
                    "protocol": "http",
                    "port": "8080",
                    "url": "/",
                    "allUsers": True,
                    "grantPrivilege": "local",
                    "advanceGrantPrivilege": True,
                }
            }
        }
        add(tar, "ui/config", json.dumps(config, indent=2).encode())
        for size in SIZES:
            add(tar, f"ui/images/icon_{size}.png", icon(size))
        add(tar, "image.tar.gz", image.read_bytes())
    return buf.getvalue()


def info(version: str, checksum: str) -> bytes:
    fields = {
        "package": PACKAGE,
        "version": version,
        "os_min_ver": "6.0-7321",
        "description": DESC,
        "description_fre": DESC,
        "displayname": "SynoPixtri",
        "maintainer": "SynoPixtri",
        "maintainer_url": "https://github.com/galaticlag/synopixtri",
        "arch": ARCH,
        "startable": "yes",
        "install_dep_packages": "Docker",
        "dsmuidir": "ui",
        "dsmappname": APP,
        "checksum": checksum,
        "thirdparty": "yes",
    }
    return ("\n".join(f'{k}="{v}"' for k, v in fields.items()) + "\n").encode()


def build_spk(image: Path, version: str, out: Path) -> Path:
    payload = package_tgz(image)
    spk = out / f"{PACKAGE}-{version}.spk"
    with tarfile.open(spk, "w", format=tarfile.GNU_FORMAT) as tar:
        add(tar, "INFO", info(version, hashlib.md5(payload).hexdigest()))
        add(tar, "package.tgz", payload)
        add(tar, "PACKAGE_ICON.PNG", icon(72))
        add(tar, "PACKAGE_ICON_256.PNG", icon(256))
        add(tar, "conf/privilege", lf(HERE / "conf" / "privilege"))
        for script in sorted((HERE / "scripts").iterdir()):
            add(tar, f"scripts/{script.name}", lf(script), 0o755)
        for wizard in sorted((HERE / "WIZARD_UIFILES").iterdir()):
            add(tar, f"WIZARD_UIFILES/{wizard.name}", lf(wizard))
    return spk


def source_entry(spk: Path, version: str, base_url: str, beta: bool) -> dict:
    md5 = hashlib.md5(spk.read_bytes()).hexdigest()
    return {
        "package": PACKAGE,
        "version": version,
        "dname": "SynoPixtri",
        "desc": DESC,
        "link": f"{base_url}/{spk.name}",
        "md5": md5,
        "size": spk.stat().st_size,
        "thumbnail": [f"{base_url}/icon_72.png", f"{base_url}/icon_256.png"],
        "qinst": False,
        "qstart": False,
        "qupgrade": False,
        "deppkgs": "Docker",
        "maintainer": "SynoPixtri",
        "maintainer_url": "https://github.com/galaticlag/synopixtri",
        "distributor": "SynoPixtri",
        "distributor_url": "https://github.com/galaticlag/synopixtri",
        "changelog": "https://github.com/galaticlag/synopixtri/releases",
        "beta": beta,
        "price": 0,
        "download_count": 0,
        "recent_download_count": 0,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True, type=Path, help="docker save output, gzipped")
    ap.add_argument("--out", default="dist", type=Path)
    ap.add_argument("--build", type=int, default=1, help="package build number (must increase at each release)")
    ap.add_argument("--beta", action="store_true", help="flag the package as beta in the source file")
    ap.add_argument("--base-url", default="https://galaticlag.github.io/synopixtri")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    version = f"{project_version()}-{args.build:04d}"
    spk = build_spk(args.image, version, args.out)
    (args.out / "icon_72.png").write_bytes(icon(72))
    (args.out / "icon_256.png").write_bytes(icon(256))
    entry = source_entry(spk, version, args.base_url.rstrip("/"), args.beta)
    (args.out / "packages.json").write_text(json.dumps({"packages": [entry], "keyrings": []}, indent=2), encoding="utf-8")
    print(f"{spk} ({spk.stat().st_size // 1_000_000} MB), md5 {entry['md5']}")


if __name__ == "__main__":
    main()
