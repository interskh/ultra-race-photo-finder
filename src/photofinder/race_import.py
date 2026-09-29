import fcntl
import os
import signal
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from photofinder import config, db, races
from photofinder.index.stages import ALBUMS, MANIFEST_NAME
from photofinder.sources import yipai

INDEX = db.INDEX_NAME
LOCKS = {"index.lock": "a `photofinder index` run", ".download.lock": "a yipai download"}
SERVE_LOCK = "serve.lock"
KILL_AFTER = "PHOTOFINDER_RACE_IMPORT_KILL_AFTER"


class ImportRefused(Exception):
    pass


@dataclass
class Plan:
    slug: str
    name: str
    url: str
    title: str
    site_id: str
    key: str
    source: Path
    old_prefixes: tuple[str, ...]
    album: Path
    race: Path
    old_exports: Path
    new_exports: Path


def plan(slug: str, name: str, source: Path, url: str, title: str | None = None) -> Plan:
    if not races.SLUG.fullmatch(slug):
        raise ImportRefused(f"race slug {slug!r} must be lowercase letters, digits and dashes (e.g. 2026-gongga100)")
    if not name.strip():
        raise ImportRefused("race name must not be empty")
    try:
        platform, site_id = races.parse_url(url)
    except races.RaceError as e:
        raise ImportRefused(str(e))
    if platform != "yipai":
        raise ImportRefused(f"race import moves a yipai collection; {url!r} is a {platform} album")
    source = Path(os.path.abspath(source))
    if source.name != site_id:
        raise ImportRefused(f"the URL's orderId {site_id} does not match the collection directory {source.name}")
    key = f"yipai-{site_id}"
    reg = races.load()
    if reg.race(slug):
        raise ImportRefused(f"race {slug} is already registered; race import creates the race itself, "
                            f"so use an unregistered slug (or remove {slug} from {races.registry_path()} "
                            "if it has no albums yet)")
    if owner := reg.owner(key):
        raise ImportRefused(f"album {key} is already imported into {owner.slug}")
    exports = config.DATA_ROOT / "exports"
    p = Plan(slug, name.strip(), url.strip(), (title or "").strip() or name.strip(), site_id, key, source,
             forms(source),
             Path(os.path.abspath(races.album_dir(slug, key))), Path(os.path.abspath(races.race_dir(slug))),
             exports / source.name, exports / slug)
    if link := next((d for d in (source, p.album) if d.is_symlink()), None):
        raise ImportRefused(f"{link} is a symlink; pass the real collection directory")
    if source.exists() and p.album.exists():
        raise ImportRefused(f"both {source} and {p.album} exist; resolve by hand")
    here = current(p)
    if here is None:
        raise ImportRefused(f"no collection at {source} (or {p.album} from an interrupted import)")
    if (here / INDEX).exists() and (p.race / INDEX).exists():
        raise ImportRefused(f"both {here / INDEX} and {p.race / INDEX} exist; ambiguous, resolve by hand")
    if not (here / INDEX).exists() and not (p.race / INDEX).exists():
        raise ImportRefused(f"no {INDEX} in {here} or {p.race}")
    if p.old_exports.exists() and p.new_exports.exists() and p.old_exports != p.new_exports:
        raise ImportRefused(f"both {p.old_exports} and {p.new_exports} exist; resolve by hand")
    if not (here / MANIFEST_NAME).is_file():
        raise ImportRefused(f"no {MANIFEST_NAME} in {here}")
    with closing(sqlite3.connect(f"{(here / MANIFEST_NAME).as_uri()}?mode=ro", uri=True)) as m:
        orders = {o for (o,) in m.execute("select distinct order_id from photos where order_id is not null")}
    if orders - {site_id}:
        raise ImportRefused(f"{here / MANIFEST_NAME} lists order ids {sorted(orders)}, not {site_id} from the URL")
    return p


def current(p: Plan) -> Path | None:
    return p.source if p.source.is_dir() else p.album if p.album.is_dir() else None


def take_locks(paths: list[tuple[Path, str]]) -> list:
    held = []
    for path, who in paths:
        f = open(path, "a")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.close()
            release(held)
            raise ImportRefused(f"{who} holds {path}; stop it first")
        held.append(f)
    return held


def release(held: list):
    for f in held:
        f.close()


def lock_paths(p: Plan) -> list[tuple[Path, str]]:
    here = current(p)
    paths = [(here / name, who) for name, who in LOCKS.items()]
    if p.race.is_dir():
        paths.append((p.race / "index.lock", "a `photofinder index` run on the race"))
    return paths + [(config.DATA_ROOT / SERVE_LOCK, "a photofinder server")]


def snapshot(conn: sqlite3.Connection) -> dict:
    return {"profiles": conn.execute("select id, name, created_at from profiles order by id").fetchall(),
            "labels": conn.execute("select profile_id, person_id, label, created_at from labels "
                                   "order by profile_id, person_id").fetchall()}


def backup(p: Plan) -> tuple[Path, dict]:
    src = next(d / INDEX for d in (current(p), p.race) if (d / INDEX).exists())
    folder = config.DATA_ROOT / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{p.source.name}-index-{time.strftime('%Y%m%d-%H%M%S')}"
    dest, n = folder / f"{stem}.sqlite", 1
    while dest.exists():
        dest, n = folder / f"{stem}-{n}.sqlite", n + 1
    tmp = dest.with_name(dest.name + ".part")
    with closing(sqlite3.connect(src)) as conn, closing(sqlite3.connect(tmp)) as out:
        conn.backup(out)
        snap = snapshot(conn)
    os.replace(tmp, dest)
    return dest, snap


def move_collection(p: Plan):
    p.album.parent.mkdir(parents=True, exist_ok=True)
    if p.source.is_dir():
        os.rename(p.source, p.album)


def add_catalog(p: Plan):
    with closing(sqlite3.connect(p.album / MANIFEST_NAME)) as m:
        m.execute(f"create view if not exists catalog as {yipai.CATALOG_SELECT}")
        m.commit()


def move_index(p: Plan):
    src = p.album / INDEX
    if src.exists():
        with closing(sqlite3.connect(src)) as conn:
            busy, _, _ = conn.execute("pragma wal_checkpoint(TRUNCATE)").fetchone()
        if busy or any((p.album / (INDEX + s)).exists() for s in ("-wal", "-shm")):
            raise ImportRefused(f"{src} is still open in another process; stop it first")
        os.rename(src, p.race / INDEX)


def rewrite_index(p: Plan):
    with closing(db.connect(p.race)) as conn:
        conn.execute("begin immediate")
        conn.execute("update photos set relpath = ? || relpath where relpath not like 'albums/%'",
                     (f"{ALBUMS}/{p.key}/",))
        conn.execute("update photos set album_key = ?, album = ?", (p.key, p.title))
        conn.execute("update photos set photographer_uid = 'yipai:' || photographer_uid "
                     "where photographer_uid is not null and photographer_uid not like 'yipai:%'")
        conn.commit()


def move_exports(p: Plan) -> bool:
    if p.old_exports.exists() and p.old_exports != p.new_exports:
        os.rename(p.old_exports, p.new_exports)
        return True
    return False


def forms(path: Path) -> tuple[str, ...]:
    return tuple(dict.fromkeys((str(path), str(path.resolve()))))


def replacements(p: Plan) -> list[tuple[str, str]]:
    return [(old + "/", str(p.album) + "/") for old in p.old_prefixes] + [
        (old + "/", str(p.new_exports) + "/") for old in forms(p.old_exports)]


def fix_csvs(p: Plan) -> int:
    fixed = 0
    for path in sorted(p.new_exports.glob("*/photos.csv")):
        raw = path.read_bytes()
        bom = raw.startswith(b"\xef\xbb\xbf")
        text = raw.decode("utf-8-sig")
        new = text
        for old, to in replacements(p):
            if old != to:
                new = new.replace(old, to)
        if new != text:
            yipai.write_atomic(path, new.encode("utf-8-sig" if bom else "utf-8"))
            fixed += 1
    return fixed


def moved_target(p: Plan, target: str) -> str | None:
    for old in p.old_prefixes:
        if target == old or target.startswith(old + "/"):
            return str(p.album) + target[len(old):]
    return None


def repoint_links(p: Plan) -> int:
    subsets = config.DATA_ROOT / "subsets"
    if not subsets.is_dir():
        return 0
    n = 0
    for sub in sorted(subsets.iterdir()):
        if sub.is_symlink() or not sub.is_dir():
            continue
        photos = sub / "photos"
        entries = list(sub.iterdir())
        if photos.is_dir() and not photos.is_symlink():
            entries += list(photos.iterdir())
        for link in entries:
            if not link.is_symlink() or (to := moved_target(p, os.readlink(link))) is None:
                continue
            tmp = link.with_name(f".{link.name}.{os.getpid()}.tmp")
            tmp.unlink(missing_ok=True)
            os.symlink(to, tmp)
            os.replace(tmp, link)
            n += 1
    return n


def verify(p: Plan, before: dict) -> dict:
    with closing(db.connect(p.race)) as conn:
        after = snapshot(conn)
        relpaths = [r for (r,) in conn.execute("select relpath from photos")]
        persons = conn.execute("select count(*) from persons").fetchone()[0]
    problems = [f"{k} differ from the backup ({len(before[k])} before, {len(after[k])} after)"
                for k in ("profiles", "labels") if after[k] != before[k]]
    missing = [r for r in relpaths if not (p.race / r).is_file()]
    if missing:
        problems.append(f"{len(missing)} indexed photos missing, e.g. {missing[0]}")
    if problems:
        raise ImportRefused("verification failed, not registering: " + "; ".join(problems))
    return {"photos": len(relpaths), "persons": persons, "profiles": len(after["profiles"]),
            "labels": len(after["labels"])}


def register(p: Plan):
    reg = races.load()
    if reg.race(p.slug) or reg.owner(p.key):
        raise ImportRefused(f"race {p.slug} or album {p.key} was registered meanwhile")
    reg.races.append(races.Race(p.slug, p.name, [races.Album(p.key, "yipai", p.site_id, p.url, p.title)]))
    races.save(reg)


def checkpoint(step: str, say):
    say(f"done: {step}")
    if os.environ.get(KILL_AFTER) == step:
        os.kill(os.getpid(), signal.SIGKILL)


def run(slug: str, name: str, source: Path, url: str, title: str | None = None, say=print) -> dict:
    p = plan(slug, name, source, url, title)
    race_locked = p.race.is_dir()
    held = take_locks(lock_paths(p))
    try:
        if not race_locked:
            p.race.mkdir(parents=True)
            held += take_locks([(p.race / "index.lock", "a `photofinder index` run on the race")])
        path, before = backup(p)
        checkpoint("backup", say)
        move_collection(p)
        checkpoint("move_collection", say)
        add_catalog(p)
        checkpoint("add_catalog", say)
        move_index(p)
        checkpoint("move_index", say)
        rewrite_index(p)
        checkpoint("rewrite_index", say)
        moved = move_exports(p)
        checkpoint("move_exports", say)
        csvs = fix_csvs(p)
        checkpoint("fix_csvs", say)
        links = repoint_links(p)
        checkpoint("repoint_links", say)
        counts = verify(p, before)
        checkpoint("verify", say)
        register(p)
    finally:
        release(held)
    exports = p.new_exports.is_dir()
    summary = {**counts, "backup": str(path), "symlinks": links, "exports": exports, "csvs": csvs}
    say(f"imported {p.source} into race {p.slug} ({p.name}) as album {p.key} \"{p.title}\" at {p.album}")
    say(f"  photos {counts['photos']}, persons {counts['persons']}, profiles {counts['profiles']}, "
        f"labels {counts['labels']} (unchanged)")
    say(f"  backup {path}")
    say(f"  exports {'in ' + str(p.new_exports) if exports else 'none'} ({'moved now' if moved else 'not moved now'}); "
        f"{csvs} photos.csv rewritten; {links} subset symlinks re-pointed")
    return summary
