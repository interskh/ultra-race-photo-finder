import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from photofinder import config

REGISTRY_NAME = "races.json"
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SITE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
HOSTS = {"yipai": "yipai360.com", "pailixiang": "pailixiang.com", "xxpie": "xxpie.com", "photoplus": "photoplus.cn"}
PLATFORMS = tuple(HOSTS)


class RaceError(ValueError):
    pass


@dataclass
class Album:
    key: str
    platform: str
    site_id: str
    url: str
    title: str | None = None


@dataclass
class Race:
    slug: str
    name: str
    albums: list[Album] = field(default_factory=list)


@dataclass
class Registry:
    races: list[Race] = field(default_factory=list)

    def race(self, slug: str) -> Race | None:
        return next((r for r in self.races if r.slug == slug), None)

    def require(self, slug: str) -> Race:
        found = self.race(slug)
        if found is None:
            raise RaceError(f"no race {slug!r} registered; add it with `photofinder race add {slug} \"<name>\"`")
        return found

    def owner(self, key: str) -> Race | None:
        return next((r for r in self.races if any(a.key == key for a in r.albums)), None)


def registry_path() -> Path:
    return config.DATA_ROOT / REGISTRY_NAME


def race_dir(slug: str) -> Path:
    return config.DATA_ROOT / "races" / slug


def album_dir(slug: str, key: str) -> Path:
    return race_dir(slug) / "albums" / key


def load() -> Registry:
    path = registry_path()
    if not path.is_file():
        return Registry()
    data = json.loads(path.read_text(encoding="utf-8"))
    return Registry([Race(r["slug"], r["name"], [Album(**a) for a in r.get("albums", [])])
                     for r in data.get("races", [])])


def save(reg: Registry):
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(asdict(reg), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def race(slug: str) -> Race:
    return load().require(slug)


def add_race(slug: str, name: str) -> Race:
    if not SLUG.fullmatch(slug):
        raise RaceError(f"race slug {slug!r} must be lowercase letters, digits and dashes, "
                        "starting with a letter or digit (e.g. 2026-gongga100)")
    if not name.strip():
        raise RaceError("race name must not be empty")
    reg = load()
    if reg.race(slug):
        raise RaceError(f"race {slug!r} is already registered")
    new = Race(slug, name.strip())
    reg.races.append(new)
    save(reg)
    return new


def platform_of(host: str) -> str | None:
    return next((p for p, d in HOSTS.items() if host == d or host.endswith("." + d)), None)


def parse_url(url: str) -> tuple[str, str]:
    u = urlparse(url.strip())
    platform = platform_of((u.hostname or "").lower())
    if platform is None:
        raise RaceError(f"unsupported album URL {url!r}; supported sites: " + ", ".join(HOSTS.values()))
    query = parse_qs(u.query)
    if platform == "yipai":
        site_id = query.get("orderId", [""])[0]
    elif platform == "xxpie":
        site_id = query.get("album_id", [""])[0]
    elif platform == "pailixiang":
        m = re.fullmatch(r"/album/(a\d+)/?", u.path)
        site_id = m[1] if m else ""
    else:
        m = re.fullmatch(r"/live/(\d+)/?", u.path)
        site_id = m[1] if m else ""
    if not SITE_ID.fullmatch(site_id):
        raise RaceError(f"cannot find the {platform} album id in {url!r}")
    return platform, site_id


def check_album(reg: Registry, slug: str, url: str, title: str | None = None) -> Album:
    platform, site_id = parse_url(url)
    reg.require(slug)
    album = Album(f"{platform}-{site_id}", platform, site_id, url.strip(), title)
    if owner := reg.owner(album.key):
        raise RaceError(f"album {album.key} already belongs to race {owner.slug}")
    return album


def add_album(slug: str, url: str, title: str | None = None) -> Album:
    reg = load()
    album = check_album(reg, slug, url, title)
    reg.require(slug).albums.append(album)
    save(reg)
    return album
