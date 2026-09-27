import argparse
import logging
import sys
import time
from contextlib import closing
from pathlib import Path

from photofinder import config, db, models, search
from photofinder.index import stages

log = logging.getLogger("photofinder")


def cmd_index(args):
    t0 = time.monotonic()
    with closing(db.connect(args.collection)) as conn:
        for stage in (stages.scan, stages.detect, stages.embed_persons):
            t = time.monotonic()
            counts = stage(conn, args.collection)
            models.unload()
            log.info("%s finished in %.1fs: %s", stage.__name__, time.monotonic() - t, counts)
    log.info("index finished in %.1fs", time.monotonic() - t0)


def area(box) -> float:
    return (box[2] - box[0]) * (box[3] - box[1])


def fmt_box(box) -> str:
    return "(" + ",".join(f"{v:.0f}" for v in box[:4]) + ")"


def default_out(collection: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return config.DATA_ROOT / "exports" / f"{collection.resolve().name}-search-{stamp}.jpg"


def query_box(args, img) -> tuple:
    if args.whole:
        return (0.0, 0.0, float(img.width), float(img.height))
    boxes = sorted(models.detect_persons([img])[0], key=area, reverse=True)
    models.unload()
    for i, box in enumerate(boxes):
        print(f"box {i}: {fmt_box(box)} conf={box[4]:.2f}")
    if not boxes:
        sys.exit(f"no person detected in {args.photo}; rerun with --whole to search with the whole image")
    if not 0 <= args.box < len(boxes):
        sys.exit(f"--box {args.box} out of range; valid boxes are 0..{len(boxes) - 1}")
    return boxes[args.box][:4]


def cmd_search(args):
    if not (args.collection / db.INDEX_NAME).is_file():
        sys.exit(f"no index in {args.collection}; run `photofinder index {args.collection}` first")
    if not args.photo.is_file():
        sys.exit(f"query photo {args.photo} not found")
    if args.top < 1:
        sys.exit("--top must be at least 1")
    try:
        img = models.load_image(args.photo)
    except Exception as e:
        sys.exit(f"cannot read query photo {args.photo}: {stages.error_text(e)}")
    with closing(db.connect(args.collection)) as conn:
        try:
            persons = search.load_persons(conn)
        except search.MissingEmbeddings as e:
            sys.exit(str(e))
        box = query_box(args, img)
        query = models.crop(img, box)
        osnet, siglip = models.embed_crops([query])
        models.unload()
        exclude = []
        if found := search.find_photo(conn, args.collection, args.photo):
            exclude.append(found[0])
            print(f"query photo is indexed as {found[1]}; excluded from results")
        results = search.search(conn, {"osnet": osnet, "siglip": siglip}, args.top, exclude, persons=persons)
    for r in results:
        print(f"{r.rank:>3} {r.score:.4f} {r.relpath} box={fmt_box(r.box)} {r.taken_at or '-'} "
              f"{r.photographer or '-'} {r.album or '-'}")
    tiles = [(query, "query")]
    for r in results:
        crop = models.crop(models.load_image(args.collection / r.relpath), r.box)
        tiles.append((crop, f"#{r.rank} {r.score:.3f} id {r.source_photo_id or r.photo_id}"))
    out = args.out or default_out(args.collection)
    search.contact_sheet(tiles, out)
    print(f"contact sheet: {out}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="photofinder", description="Find your own photos in a race photo collection")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("index", help="scan, detect and embed a collection into <collection>/index.sqlite")
    p.add_argument("collection", type=Path)
    p.set_defaults(func=cmd_index)
    p = sub.add_parser("search", help="rank indexed photos by similarity to a person in a query photo")
    p.add_argument("collection", type=Path)
    p.add_argument("--photo", type=Path, required=True, help="query photo")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--box", type=int, default=0,
                   help="detected person to search for; boxes are numbered by area, largest first (default 0)")
    g.add_argument("--whole", action="store_true", help="skip detection and use the whole image as the query")
    p.add_argument("--top", type=int, default=24, help="number of photos to return (default 24)")
    p.add_argument("--out", type=Path, help="contact sheet JPEG (default data/exports/<collection>-search-<time>.jpg)")
    p.set_defaults(func=cmd_search)
    args = ap.parse_args(argv)

    if not args.collection.is_dir():
        sys.exit(f"collection {args.collection} is not a directory")
    config.require_mounted()
    config.setup_model_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args.func(args)


if __name__ == "__main__":
    main()
