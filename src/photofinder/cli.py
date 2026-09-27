import argparse
import logging
import sys
import time
from contextlib import closing
from pathlib import Path

from photofinder import config, db, models
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


def main(argv=None):
    ap = argparse.ArgumentParser(prog="photofinder", description="Find your own photos in a race photo collection")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("index", help="scan, detect and embed a collection into <collection>/index.sqlite")
    p.add_argument("collection", type=Path)
    p.set_defaults(func=cmd_index)
    args = ap.parse_args(argv)

    if not args.collection.is_dir():
        sys.exit(f"collection {args.collection} is not a directory")
    config.require_mounted()
    config.setup_model_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args.func(args)


if __name__ == "__main__":
    main()
