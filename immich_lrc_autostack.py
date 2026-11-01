#!/usr/bin/env python3
"""Mirror Lightroom Classic folder stacks into Immich stacks.

Reads the newest Lightroom Classic catalog backup (``*.zip`` or ``*.lrcat``)
in each backups directory, maps catalog file paths to Immich assets by
``originalPath``, and creates Immich stacks in Lightroom order. Existing Immich
stacks that contain a Lightroom stack member (e.g. RAW+JPG pairs) are merged
in and keep their cover. It never unstacks or deletes anything.

Only the Python standard library is required.
"""
import argparse
import collections
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

__version__ = "0.1.0"
SAMPLE_SIZE = 500


def log(msg):
    print(time.strftime("%Y-%m-%dT%H:%M:%S"), msg, flush=True)


def env_bool(name, default):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def env_list(name, sep=","):
    return [v.strip() for v in os.environ.get(name, "").split(sep) if v.strip()]


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Mirror Lightroom Classic stacks from catalog backups into Immich stacks.",
        epilog="Every option can also be set with the environment variable shown in brackets.",
    )
    p.add_argument("--api-url", default=os.environ.get("IMMICH_API_URL", "http://immich-server:2283/api"),
                   help="Immich API base URL, ending in /api [IMMICH_API_URL]")
    p.add_argument("--api-key-file", default=os.environ.get("IMMICH_API_KEY_FILE"),
                   help="read the API key from this file instead of IMMICH_API_KEY [IMMICH_API_KEY_FILE]")
    p.add_argument("--backups-dir", action="append", default=None,
                   help="directory holding Lightroom catalog backups; searched recursively for the newest "
                        "*.zip or *.lrcat. Repeat for several catalogs [LRC_BACKUPS_DIRS, comma-separated]")
    p.add_argument("--library-path", default=os.environ.get("IMMICH_LIBRARY_PATH", "/"),
                   help="photo folder as Immich sees it (asset originalPath prefix), "
                        "e.g. /mnt/photos [IMMICH_LIBRARY_PATH]")
    p.add_argument("--path-map", action="append", default=None, metavar="LRC_PREFIX=IMMICH_PREFIX",
                   help="explicit path mapping, e.g. 'P:/Photos/=/mnt/photos/'. Folders not covered are "
                        "auto-detected [PATH_MAP, semicolon-separated]")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--apply", dest="dry_run", action="store_false", default=None,
                      help="create stacks (default is a dry run) [DRY_RUN=false]")
    mode.add_argument("--dry-run", dest="dry_run", action="store_true",
                      help="only print what would change [DRY_RUN=true]")
    p.add_argument("--interval", type=int, default=int(os.environ.get("INTERVAL", "0")),
                   help="seconds between runs; 0 runs once and exits [INTERVAL]")
    p.add_argument("--version", action="version", version=__version__)
    args = p.parse_args(argv)

    args.backups_dir = args.backups_dir or env_list("LRC_BACKUPS_DIRS")
    if not args.backups_dir:
        p.error("at least one --backups-dir (or LRC_BACKUPS_DIRS) is required")
    raw_maps = args.path_map or env_list("PATH_MAP", ";")
    try:
        args.path_map = [tuple(m.split("=", 1)) for m in raw_maps]
        assert all(len(m) == 2 and m[0] and m[1] for m in args.path_map)
    except AssertionError:
        p.error("--path-map must look like LRC_PREFIX=IMMICH_PREFIX")
    if args.dry_run is None:
        args.dry_run = env_bool("DRY_RUN", True)
    if args.api_key_file:
        with open(args.api_key_file) as f:
            args.api_key = f.read().strip()
    else:
        args.api_key = os.environ.get("IMMICH_API_KEY", "").strip()
    if not args.api_key:
        p.error("set IMMICH_API_KEY or --api-key-file")
    args.api_url = args.api_url.rstrip("/")
    args.library_path = args.library_path.rstrip("/") + "/"
    return args


class Immich:
    def __init__(self, url, key):
        self.url, self.key = url, key

    def call(self, method, path, body=None):
        req = urllib.request.Request(
            self.url + path,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"x-api-key": self.key, "Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{method} {path}: HTTP {e.code} {e.read()[:300]!r}") from None

    def assets(self, prefix):
        """Return {lowercased originalPath: asset id} for assets under prefix."""
        assets, cursor = {}, None
        while True:
            body = {"filter": {"originalPath": {"startsWith": prefix}}, "withStacked": True, "size": 1000}
            if cursor:
                body["cursor"] = cursor
            page = self.call("POST", "/search/metadata", body)["assets"]
            for a in page["items"]:
                assets[a["originalPath"].lower()] = a["id"]
            cursor = page.get("nextCursor")
            if not cursor:
                return assets

    def stacks(self):
        """Return ({asset id: stack id}, {stack id: (cover id, [asset ids])})."""
        by_asset, stacks = {}, {}
        for s in self.call("GET", "/stacks"):
            ids = [a["id"] for a in s["assets"]]
            stacks[s["id"]] = (s["primaryAssetId"], ids)
            for i in ids:
                by_asset[i] = s["id"]
        return by_asset, stacks

    def create_stack(self, asset_ids):
        return self.call("POST", "/stacks", {"assetIds": asset_ids})


def newest_backup(backups_dir):
    found = []
    for root, _, files in os.walk(backups_dir):
        found += [os.path.join(root, f) for f in files if f.lower().endswith((".zip", ".lrcat"))]
    return max(found, key=os.path.getmtime) if found else None


def open_catalog(path, tmp):
    """Copy or extract the catalog into tmp so the backup is never touched."""
    dst = os.path.join(tmp, "catalog.lrcat")
    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            name = next((n for n in z.namelist() if n.lower().endswith(".lrcat")), None)
            if not name:
                raise RuntimeError(f"{path}: no .lrcat inside")
            with z.open(name) as src, open(dst, "wb") as out:
                shutil.copyfileobj(src, out)
    else:
        shutil.copyfile(path, dst)
    return sqlite3.connect(f"file:{dst}?mode=ro", uri=True)


def read_catalog(db):
    """Return (stacks as ordered lists of (root, rel path), {root: [rel paths]})."""
    image_path, by_root = {}, collections.defaultdict(list)
    for root, rel, image in db.execute("""
        select rf.absolutePath, fo.pathFromRoot || f.baseName || '.' || f.extension, i.id_local
        from Adobe_images i
        join AgLibraryFile f on f.id_local = i.rootFile
        join AgLibraryFolder fo on fo.id_local = f.folder
        join AgLibraryRootFolder rf on rf.id_local = fo.rootFolder"""):
        image_path[image] = (root, rel)
        by_root[root].append(rel)
    stacks = collections.defaultdict(list)
    for stack, _, image in db.execute(
        "select stack, cast(position as real), image from AgLibraryFolderStackImage order by stack, 2"
    ):
        if image in image_path and image_path[image] not in stacks[stack]:  # skip virtual-copy duplicates
            stacks[stack].append(image_path[image])
    return [s for s in stacks.values() if len(s) > 1], by_root


def detect_prefixes(by_root, assets, path_map):
    """Map each Lightroom root folder to an Immich path prefix (explicit map, else suffix voting)."""
    by_name = collections.defaultdict(list)
    for key in assets:
        by_name[key.rsplit("/", 1)[-1]].append(key)
    prefixes = {}
    for root, rels in by_root.items():
        explicit = next((dst + root[len(src):] for src, dst in path_map if root.lower().startswith(src.lower())), None)
        if explicit:
            prefixes[root] = explicit.lower()
            log(f"  folder {root!r} -> {explicit!r} (mapped)")
            continue
        votes = collections.Counter()
        for rel in rels[:SAMPLE_SIZE]:
            rel = rel.lower()
            for key in by_name.get(rel.rsplit("/", 1)[-1], []):
                if key.endswith("/" + rel):
                    votes[key[: -len(rel)]] += 1
        sampled = min(len(rels), SAMPLE_SIZE)
        prefix, count = votes.most_common(1)[0] if votes else (None, 0)
        if count and count >= max(min(3, sampled), sampled // 5):
            prefixes[root] = prefix
            log(f"  folder {root!r} -> {prefix!r} ({count}/{sampled} sampled files matched)")
        else:
            log(f"  folder {root!r} -> not found in Immich, skipped")
    return prefixes


def plan(lr_stack, prefixes, assets, by_asset, stacks):
    """Return ordered asset ids (cover first) for one Lightroom stack, or None if nothing to do."""
    members = [assets.get(prefixes.get(root, "\0") + rel.lower()) for root, rel in lr_stack]
    members = [m for m in members if m]
    if len(members) < 2:
        return None
    # Pull in whole existing stacks that touch a member, keeping Lightroom order.
    ordered = []
    for m in members:
        sid = by_asset.get(m)
        group = [m] + ([] if sid is None else [stacks[sid][0]] + stacks[sid][1])
        ordered += [i for i in dict.fromkeys(group) if i not in ordered]
    # Cover: the existing cover of the Lightroom top image's stack, else the Lightroom top image.
    top_sid = by_asset.get(members[0])
    cover = stacks[top_sid][0] if top_sid else members[0]
    ordered = [cover] + [i for i in ordered if i != cover]
    sids = {by_asset.get(i) for i in ordered}
    if len(sids) == 1 and None not in sids and stacks[next(iter(sids))][0] == cover:
        return None  # already one stack with the right cover
    return ordered


def run_once(args):
    log(f"starting (dry_run={args.dry_run})")
    immich = Immich(args.api_url, args.api_key)
    assets = immich.assets(args.library_path)
    by_asset, stacks = immich.stacks()
    log(f"{len(assets)} assets under {args.library_path}, {len(stacks)} existing stacks")
    changed = unchanged = 0
    for backups_dir in args.backups_dir:
        backup = newest_backup(backups_dir)
        if not backup:
            log(f"{backups_dir}: no catalog backup found")
            continue
        log(f"{backups_dir}: using {backup}")
        with tempfile.TemporaryDirectory() as tmp:
            db = open_catalog(backup, tmp)
            try:
                lr_stacks, by_root = read_catalog(db)
            finally:
                db.close()
        prefixes = detect_prefixes(by_root, assets, args.path_map)
        for lr_stack in lr_stacks:
            ids = plan(lr_stack, prefixes, assets, by_asset, stacks)
            if ids is None:
                unchanged += 1
                continue
            changed += 1
            log(f"  stack {lr_stack[0][1]} ({len(lr_stack)} in Lightroom) -> {len(ids)} assets")
            if args.dry_run:
                continue
            s = immich.create_stack(ids)
            # Refresh the local view so later Lightroom stacks see this one.
            for sid in {by_asset.get(i) for i in ids} - {None}:
                stacks.pop(sid, None)
            new_ids = [a["id"] for a in s["assets"]]
            stacks[s["id"]] = (s["primaryAssetId"], new_ids)
            for i in new_ids:
                by_asset[i] = s["id"]
    verb = "would be created/merged" if args.dry_run else "created/merged"
    log(f"done: {changed} stacks {verb}, {unchanged} unchanged or not in Immich")


def main(argv=None):
    args = parse_args(argv)
    while True:
        try:
            run_once(args)
        except Exception as e:  # keep looping; the next run retries
            log(f"error: {e}")
            if not args.interval:
                return 1
        if not args.interval:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
