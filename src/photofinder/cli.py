import argparse
import fcntl
import logging
import os
import sys
import time
from contextlib import closing
from pathlib import Path

import httpx

from photofinder import config, db, evaluate, models, race_import, races, search
from photofinder.index import stages
from photofinder.memory import FootprintExceeded
from photofinder.sources import pailixiang, yipai
from photofinder.sources.base import AlbumDownloader
from photofinder.sources.common import AlreadyRunning, Blocked

log = logging.getLogger("photofinder")
ADAPTERS = {"pailixiang": pailixiang}
LOCK_NAME = "index.lock"
SERVE_LOCK_NAME = "serve.lock"
COLLECTION_HELP = "race slug (registered in data/races.json) or collection directory"


def lock_index(collection: Path):
    f = open(collection / LOCK_NAME, "a")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        sys.exit(f"another `photofinder index` run is active on {collection}")
    return f


def lock_serve(collection: Path, port: int):
    path = config.DATA_ROOT / SERVE_LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.seek(0)
        who = f.read().strip() or "unknown"
        f.close()
        sys.exit(f"a photofinder server is already running ({who}); only one runs at a time, stop it first")
    f.truncate(0)
    f.write(f"pid {os.getpid()}, http://127.0.0.1:{port}/, {collection}")
    f.flush()
    return f


def cmd_index(args):
    t0 = time.monotonic()
    with lock_index(args.collection), closing(db.connect(args.collection)) as conn:
        pipeline = [stages.scan, stages.detect, stages.embed_persons, stages.embed_scenes]
        if args.ocr:
            pipeline.append(stages.ocr_bibs)
        for stage in pipeline:
            t = time.monotonic()
            try:
                counts = stage(conn, args.collection)
            except FootprintExceeded as e:
                sys.exit(f"{stage.__name__}: {e}")
            models.unload()
            log.info("%s finished in %.1fs: %s", stage.__name__, time.monotonic() - t, counts)
    log.info("index finished in %.1fs", time.monotonic() - t0)


def area(box) -> float:
    return (box[2] - box[0]) * (box[3] - box[1])


def fmt_box(box) -> str:
    return "(" + ",".join(f"{v:.0f}" for v in box[:4]) + ")"


def default_out(collection: Path, kind: str = "search", out_dir: Path | None = None) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return (out_dir or config.DATA_ROOT / "exports") / f"{collection.resolve().name}-{kind}-{stamp}.jpg"


def query_box(args, img) -> tuple:
    if args.whole:
        return (0.0, 0.0, float(img.width), float(img.height))
    boxes = sorted(models.detect_persons([img])[0], key=area, reverse=True)
    models.unload()
    for i, box in enumerate(boxes):
        print(f"box {i}: {fmt_box(box)} conf={box[4]:.2f}")
    if not boxes:
        sys.exit(f"no person detected in {args.photo}; rerun with --whole to search with the whole image")
    box = args.box or 0
    if not 0 <= box < len(boxes):
        sys.exit(f"--box {box} out of range; valid boxes are 0..{len(boxes) - 1}")
    return boxes[box][:4]


def parse_time(flag: str, value: str | None, minute_end=False) -> str | None:
    try:
        return search.parse_time(value, minute_end)
    except ValueError as e:
        sys.exit(f"{flag} {e}")


def cmd_search(args):
    if not (args.photo or args.text or args.scene):
        sys.exit("give at least one of --photo, --text, --scene")
    if not args.photo and (args.box is not None or args.whole):
        sys.exit(f"{'--whole' if args.whole else '--box'} needs --photo")
    if args.bib is not None and not args.bib.strip():
        sys.exit("--bib needs a number, e.g. --bib 8038")
    filters = search.Filters(parse_time("--from", args.start), parse_time("--to", args.end, minute_end=True),
                             tuple(args.photographer or ()), tuple(args.album or ()),
                             args.bib.strip() if args.bib else None, groups=tuple(args.group or ()))
    if not (args.collection / db.INDEX_NAME).is_file():
        sys.exit(f"no index in {args.collection}; run `photofinder index {args.collection}` first")
    if args.photo and not args.photo.is_file():
        sys.exit(f"query photo {args.photo} not found")
    if args.top < 1:
        sys.exit("--top must be at least 1")
    img = query = None
    if args.photo:
        try:
            img = models.load_image(args.photo)
        except Exception as e:
            sys.exit(f"cannot read query photo {args.photo}: {stages.error_text(e)}")
    with closing(db.connect(args.collection)) as conn:
        try:
            persons = search.load_persons(conn)
            warnings = [search.check_filters(conn, filters)]
            if args.scene:
                search.load_scenes(conn, persons)
                warnings.append(search.check_scenes(conn))
        except search.MissingEmbeddings as e:
            sys.exit(str(e))
        for warning in filter(None, warnings):
            print(warning)
        refs, exclude = {}, []
        if img is not None:
            query = models.crop(img, query_box(args, img))
            refs["osnet"], refs["siglip"] = models.embed_crops([query])
        texts = {k: t for k, t in (("text", args.text), ("scene", args.scene)) if t}
        if texts:
            vecs = models.encode_text(list(texts.values()))
            refs.update({k: v[None] for k, v in zip(texts, vecs)})
        models.unload()
        if args.photo and (found := search.find_photo(conn, args.collection, args.photo)):
            exclude.append(found[0])
            print(f"query photo is indexed as {found[1]}; excluded from results")
        results = search.search(conn, refs, args.top, exclude, persons=persons, filters=filters)
    if not results:
        print("no photos match the filters" if filters else "no results")
        return
    for r in results:
        print(f"{r.rank:>3} {r.score:.4f} {r.relpath} box={fmt_box(r.box)} {r.taken_at or '-'} "
              f"{r.photographer or '-'} {r.album or '-'} {r.grp or '-'}")
    tiles = [(query, "query")] if query is not None else []
    for r in results:
        img = models.load_image(args.collection / r.relpath)
        if args.photo or args.text:
            img = models.crop(img, r.box)
        tiles.append((img, f"#{r.rank} {r.score:.3f} id {r.source_photo_id or r.photo_id}"))
    out = args.out or default_out(args.collection)
    search.contact_sheet(tiles, out)
    print(f"contact sheet: {out}")


def print_frequent(conn):
    print("most frequent OCR bibs (4-digit first): text photos photographers")
    for text, photos, who in evaluate.frequent_bibs(conn):
        print(f"  {text:>6} {photos:>4} {who:>3}")


def print_rows(title, rows):
    print(title)
    print(f"  {'config':<16} " + " ".join(f"{h:>6}" for h in ("R@10", "R@50", "xR@50", "sR@10", "sR@50"))
          + f" {'refs':>5} {'xrefs':>5} {'GT':>4}")
    for r in rows:
        print(f"  {r.config:<16} " + " ".join(f"{v:>6.3f}" for v in (r.r10, r.r50, r.x50, r.s10, r.s50))
              + f" {r.refs:>5} {r.xrefs:>5} {r.photos:>4}")


def cmd_eval(args):
    if not (args.collection / db.INDEX_NAME).is_file():
        sys.exit(f"no index in {args.collection}; run `photofinder index {args.collection}` first")
    if args.refs < 1:
        sys.exit("--refs must be at least 1")
    bibs = [b.strip() for b in args.bib or () if b.strip()]
    with closing(db.connect(args.collection)) as conn:
        conn.execute("begin")
        try:
            persons = search.load_persons(conn)
            warning = search.check_filters(conn, search.Filters(bib="eval"))
        except search.MissingEmbeddings as e:
            sys.exit(str(e))
        if warning:
            print(warning)
        if not bibs:
            print("no --bib given")
        weights = evaluate.configs()
        default = {k: search.WEIGHTS[k] for k in ("osnet", "siglip")}
        per_bib, missing = [], False
        for bib in bibs:
            truth = evaluate.ground_truth(conn, bib, persons, args.refs)
            if len(truth.photos) < evaluate.MIN_PHOTOS or not truth.refs:
                print(f"bib {bib}: {len(truth.photos)} photo(s) with OCR text {bib!r}; need at least "
                      f"{evaluate.MIN_PHOTOS} with embedded persons to evaluate")
                missing = True
                continue
            rows = evaluate.evaluate_bib(persons, truth, weights)
            per_bib.append(rows)
            print_rows(f"bib {bib}", rows)
            out = default_out(args.collection, f"eval-{bib}", args.out_dir)
            evaluate.sheet(conn, args.collection, persons, truth, default, out)
            print(f"contact sheet: {out}")
        if len(per_bib) > 1:
            print_rows(f"mean over {len(per_bib)} bibs", evaluate.mean_over_bibs(per_bib))
        if missing or not bibs:
            print_frequent(conn)


def cmd_serve(args):
    if not (args.collection / db.INDEX_NAME).is_file():
        sys.exit(f"no index in {args.collection}; run `photofinder index {args.collection}` first")
    import uvicorn
    from photofinder.web import app as web
    models.half_precision = True
    with lock_serve(args.collection.resolve(), args.port):
        try:
            app = web.create_app(args.collection)
        except search.MissingEmbeddings as e:
            sys.exit(str(e))
        print(f"serving {args.collection} at http://127.0.0.1:{args.port}/", flush=True)
        uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1)


def cmd_race_add(args):
    try:
        race = races.add_race(args.slug, args.name)
    except races.RaceError as e:
        sys.exit(str(e))
    print(f"registered race {race.slug} ({race.name}) in {races.registry_path()}")


def cmd_race_import(args):
    try:
        race_import.run(args.slug, args.name, args.source_dir, args.url, args.title,
                        say=lambda s: print(s, flush=True))
    except race_import.ImportRefused as e:
        sys.exit(str(e))


def cmd_album_add(args):
    try:
        album = races.check_album(races.load(), args.race, args.url, args.title)
    except races.RaceError as e:
        sys.exit(str(e))
    title = args.title
    if title is None and album.platform in ADAPTERS:
        try:
            with album_client(album.platform) as client:
                title = album_adapter(album.platform, client, album.site_id).meta()["title"]
        except (Blocked, KeyError, TypeError) as e:
            sys.exit(f"could not fetch the album title from {album.platform} ({e}); "
                     f"rerun with --title \"<title>\"")
    try:
        album = races.add_album(args.race, args.url, title)
    except races.RaceError as e:
        sys.exit(str(e))
    print(f"registered {album.key} ({album.title or 'no title; the Album filter shows the key'}) "
          f"in race {args.race}")
    print(f"next: scripts/download.sh {args.race}, then uv run photofinder index {args.race}")


def select_albums(slug: str, key: str | None = None) -> list[races.Album]:
    reg = races.load()
    race = reg.race(slug)
    if race is None:
        known = ", ".join(r.slug for r in reg.races) or "none registered"
        sys.exit(f"{slug} is not a registered race (races: {known})")
    if key is None:
        if not race.albums:
            sys.exit(f"race {slug} has no albums yet")
        return race.albums
    albums = [a for a in race.albums if a.key == key]
    if not albums:
        keys = ", ".join(a.key for a in race.albums) or "none"
        sys.exit(f"race {slug} has no album {key} (albums: {keys})")
    return albums


def yipai_client():
    return httpx.Client(timeout=60, follow_redirects=True)


def yipai_downloader(client, order_id: str, out_dir: Path):
    return yipai.Downloader(client, order_id, out_dir, concurrency=6, page_delay=3.0)


def album_client(platform: str):
    return httpx.Client(headers=ADAPTERS[platform].HEADERS, timeout=60, follow_redirects=True)


def album_adapter(platform: str, client, site_id: str):
    return ADAPTERS[platform].Adapter(client, site_id)


def album_downloader(client, adapter, out_dir: Path):
    return AlbumDownloader(client, adapter, out_dir, concurrency=4, page_delay=3.0, img_delay=(0.2, 0.6), tries=5,
                           max_consecutive_failures=20)


def download_album(album: races.Album, out_dir: Path) -> dict[str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(out_dir / "download.log")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(handler)
    try:
        yip = album.platform == "yipai"
        with yipai_client() if yip else album_client(album.platform) as client:
            dl = (yipai_downloader(client, album.site_id, out_dir) if yip else
                  album_downloader(client, album_adapter(album.platform, client, album.site_id), out_dir))
            try:
                counts = dl.run()
            except AlreadyRunning as e:
                sys.exit(str(e))
            except Blocked as e:
                log.error("%s stopped: %s (rerun later to resume)", album.key, e)
                sys.exit(2)
            finally:
                dl.close()
        log.info("%s finished: %s", album.key, counts)
        return counts
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()


def cmd_download(args):
    logging.getLogger("httpx").setLevel(logging.WARNING)
    unfinished = []
    for album in select_albums(args.race, args.album_key):
        if album.platform != "yipai" and album.platform not in ADAPTERS:
            print(f"skipping {album.key}: {album.platform} downloads are not supported yet", flush=True)
            continue
        if set(download_album(album, races.album_dir(args.race, album.key))) - {"done"}:
            unfinished.append(album.key)
    if unfinished:
        sys.exit(f"not every photo downloaded in {', '.join(unfinished)}; rerun later to resume")


def resolve_collection(arg: Path) -> Path:
    if arg.is_dir():
        return arg
    slug = str(arg)
    reg = races.load()
    if races.SLUG.fullmatch(slug) and reg.race(slug):
        d = races.race_dir(slug)
        if not d.is_dir():
            sys.exit(f"race {slug} has no directory {d} yet; add and download its albums first")
        return d
    known = ", ".join(r.slug for r in reg.races) or "none registered"
    sys.exit(f"{arg} is neither a directory nor a registered race (races: {known})")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="photofinder", description="Find your own photos in a race photo collection")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("index", help="scan, detect and embed a collection into <collection>/index.sqlite")
    p.add_argument("collection", type=Path, help=COLLECTION_HELP)
    p.add_argument("--ocr", action="store_true", help="also read bib numbers with Apple Vision (slow, optional)")
    p.set_defaults(func=cmd_index)
    p = sub.add_parser("search", help="rank indexed photos by a query photo, person text and/or scene text")
    p.add_argument("collection", type=Path, help=COLLECTION_HELP)
    p.add_argument("--photo", type=Path, help="query photo")
    p.add_argument("--text", help='person description, e.g. "orange vest black shorts"')
    p.add_argument("--scene", help='scene description, e.g. "mountain" or "雪山"')
    g = p.add_mutually_exclusive_group()
    g.add_argument("--box", type=int,
                   help="detected person to search for; boxes are numbered by area, largest first (default 0)")
    g.add_argument("--whole", action="store_true", help="skip detection and use the whole image as the query")
    p.add_argument("--from", dest="start", help="earliest camera-local time, 'YYYY-MM-DD HH:MM[:SS]'")
    p.add_argument("--to", dest="end", help="latest camera-local time, 'YYYY-MM-DD HH:MM[:SS]'")
    p.add_argument("--photographer", action="append", help="photographer nickname or uid (repeatable)")
    p.add_argument("--album", action="append", help="source album name (repeatable)")
    p.add_argument("--group", action="append", help="group within an album, e.g. a yipai tag (repeatable)")
    p.add_argument("--bib", help="only persons whose OCR'd bib contains this text")
    p.add_argument("--top", type=int, default=24, help="number of photos to return (default 24)")
    p.add_argument("--out", type=Path, help="contact sheet JPEG (default data/exports/<collection>-search-<time>.jpg)")
    p.set_defaults(func=cmd_search)
    p = sub.add_parser("eval", help="recall of photo search for OCR'd bibs, using stored embeddings only")
    p.add_argument("collection", type=Path, help=COLLECTION_HELP)
    p.add_argument("--bib", action="append", help="bib number used as ground truth (repeatable)")
    p.add_argument("--refs", type=int, default=20, help="max reference persons per bib (default 20)")
    p.add_argument("--out-dir", type=Path, help="contact sheet directory (default data/exports)")
    p.set_defaults(func=cmd_eval)
    p = sub.add_parser("serve", help="local web page for searching and labelling a collection")
    p.add_argument("collection", type=Path, help=COLLECTION_HELP)
    p.add_argument("--port", type=int, default=8000, help="port on 127.0.0.1 (default 8000)")
    p.set_defaults(func=cmd_serve)
    p = sub.add_parser("download", help="download a race's albums into data/races/<race>/albums/ (resumable)")
    p.add_argument("race", help="registered race slug")
    p.add_argument("album_key", nargs="?", help="only this album, e.g. yipai-<orderId> (default: every album)")
    p.set_defaults(func=cmd_download)
    p = sub.add_parser("race", help="manage the race registry (data/races.json)")
    rsub = p.add_subparsers(dest="race_command", required=True)
    p = rsub.add_parser("add", help="register a race")
    p.add_argument("slug", help="lowercase id used on the command line, e.g. 2026-gongga100")
    p.add_argument("name", help='display name, e.g. "2026 贡嘎100"')
    p.set_defaults(func=cmd_race_add)
    p = rsub.add_parser("import", help="move a legacy yipai collection (data/yipai/<orderId>) into a new race")
    p.add_argument("slug", help="new race slug, e.g. 2026-gongga100")
    p.add_argument("name", help='display name, e.g. "2026 贡嘎100"')
    p.add_argument("source_dir", type=Path, help="the collection directory, e.g. data/yipai/<orderId>")
    p.add_argument("--url", required=True, help="the yipai gallery URL (its orderId must match the directory)")
    p.add_argument("--title", help="album title shown in the Album filter (default: the race name)")
    p.set_defaults(func=cmd_race_import)
    p = sub.add_parser("album", help="manage a race's albums (data/races.json)")
    asub = p.add_subparsers(dest="album_command", required=True)
    p = asub.add_parser("add", help="add a gallery album to a race (yipai360, pailixiang, xxpie, photoplus URL)")
    p.add_argument("race", help="registered race slug")
    p.add_argument("url", help="album URL, e.g. https://live.pailixiang.com/album/a13800138000")
    p.add_argument("--title", help="album title shown in the Album filter (default: fetched from the site)")
    p.set_defaults(func=cmd_album_add)
    args = ap.parse_args(argv)

    config.require_mounted()
    if hasattr(args, "collection"):
        args.collection = resolve_collection(args.collection)
        config.setup_model_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args.func(args)


if __name__ == "__main__":
    main()
