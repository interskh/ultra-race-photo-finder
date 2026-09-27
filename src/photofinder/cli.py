import argparse
import fcntl
import logging
import sys
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

from photofinder import config, db, evaluate, models, search
from photofinder.index import stages

log = logging.getLogger("photofinder")
LOCK_NAME = "index.lock"
TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")


def lock_index(collection: Path):
    f = open(collection / LOCK_NAME, "a")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        sys.exit(f"another `photofinder index` run is active on {collection}")
    return f


def cmd_index(args):
    t0 = time.monotonic()
    with lock_index(args.collection), closing(db.connect(args.collection)) as conn:
        for stage in (stages.scan, stages.detect, stages.embed_persons, stages.embed_scenes, stages.ocr_bibs):
            t = time.monotonic()
            counts = stage(conn, args.collection)
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
    if value is None:
        return None
    for fmt in TIME_FORMATS:
        try:
            t = datetime.strptime(value, fmt)
        except ValueError:
            continue
        return t.replace(second=59).strftime(TIME_FORMATS[0]) if minute_end and fmt == TIME_FORMATS[1] \
            else t.strftime(TIME_FORMATS[0])
    sys.exit(f"{flag} {value!r} is not a time; use 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD HH:MM:SS'")


def cmd_search(args):
    if not (args.photo or args.text or args.scene):
        sys.exit("give at least one of --photo, --text, --scene")
    if not args.photo and (args.box is not None or args.whole):
        sys.exit(f"{'--whole' if args.whole else '--box'} needs --photo")
    if args.bib is not None and not args.bib.strip():
        sys.exit("--bib needs a number, e.g. --bib 8038")
    filters = search.Filters(parse_time("--from", args.start), parse_time("--to", args.end, minute_end=True),
                             tuple(args.photographer or ()), tuple(args.album or ()),
                             args.bib.strip() if args.bib else None)
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
              f"{r.photographer or '-'} {r.album or '-'}")
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


def main(argv=None):
    ap = argparse.ArgumentParser(prog="photofinder", description="Find your own photos in a race photo collection")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("index", help="scan, detect, embed and OCR bibs of a collection into <collection>/index.sqlite")
    p.add_argument("collection", type=Path)
    p.set_defaults(func=cmd_index)
    p = sub.add_parser("search", help="rank indexed photos by a query photo, person text and/or scene text")
    p.add_argument("collection", type=Path)
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
    p.add_argument("--album", action="append", help="album name (repeatable)")
    p.add_argument("--bib", help="only persons whose OCR'd bib contains this text")
    p.add_argument("--top", type=int, default=24, help="number of photos to return (default 24)")
    p.add_argument("--out", type=Path, help="contact sheet JPEG (default data/exports/<collection>-search-<time>.jpg)")
    p.set_defaults(func=cmd_search)
    p = sub.add_parser("eval", help="recall of photo search for OCR'd bibs, using stored embeddings only")
    p.add_argument("collection", type=Path)
    p.add_argument("--bib", action="append", help="bib number used as ground truth (repeatable)")
    p.add_argument("--refs", type=int, default=20, help="max reference persons per bib (default 20)")
    p.add_argument("--out-dir", type=Path, help="contact sheet directory (default data/exports)")
    p.set_defaults(func=cmd_eval)
    args = ap.parse_args(argv)

    if not args.collection.is_dir():
        sys.exit(f"collection {args.collection} is not a directory")
    config.require_mounted()
    config.setup_model_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args.func(args)


if __name__ == "__main__":
    main()
