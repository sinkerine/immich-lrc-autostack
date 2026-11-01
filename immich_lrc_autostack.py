#!/usr/bin/env python3
"""Mirror Lightroom Classic folder stacks into Immich stacks.

Reads the newest Lightroom Classic catalog backup (``*.zip`` or ``*.lrcat``)
in each backups directory, maps catalog file paths to Immich assets by
``originalPath``, and creates Immich stacks in Lightroom order. Existing Immich
stacks that contain a Lightroom stack member (e.g. RAW+JPG pairs) are merged
in and keep their cover.

When several catalogs (e.g. one per machine) cover the same folder, one catalog
owns each folder: the newest backup, or the one with the latest edit there.
With --unstack, photos this tool stacked earlier are taken out of Immich stacks
that the owning catalog no longer has. Files are never modified or deleted.

Only the Python standard library is required.
"""
import argparse
import collections
import json
import os
import posixpath
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

__version__ = "0.2.0"
SAMPLE_SIZE = 500
OWNER_POLICIES = ("newest-backup", "latest-edit", "all")


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
                   help="directory holding one catalog's backups; searched recursively for the newest "
                        "*.zip or *.lrcat. Repeat for several catalogs/machines [LRC_BACKUPS_DIRS, comma-separated]")
    p.add_argument("--library-path", default=os.environ.get("IMMICH_LIBRARY_PATH", "/"),
                   help="photo folder as Immich sees it (asset originalPath prefix), "
                        "e.g. /mnt/photos [IMMICH_LIBRARY_PATH]")
    p.add_argument("--path-map", action="append", default=None, metavar="LRC_PREFIX=IMMICH_PREFIX",
                   help="explicit path mapping, e.g. 'P:/Photos/=/mnt/photos/'. Folders not covered are "
                        "auto-detected [PATH_MAP, semicolon-separated]")
    p.add_argument("--folder-owner", choices=OWNER_POLICIES,
                   default=os.environ.get("FOLDER_OWNER", "newest-backup"),
                   help="which catalog wins when several cover the same folder: the newest backup, the one "
                        "with the most recent edit in that folder, or all (combine stacks) [FOLDER_OWNER]")
    p.add_argument("--unstack", action="store_true", default=env_bool("UNSTACK", False),
                   help="also remove photos from Immich stacks when the owning catalog no longer stacks "
                        "them together; only photos this tool has managed are touched. Needs --state-file "
                        "and an API key with stack.update and stack.delete [UNSTACK]")
    p.add_argument("--state-file", default=os.environ.get("STATE_FILE"),
                   help="JSON file remembering which photos this tool manages [STATE_FILE]")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--apply", dest="dry_run", action="store_false", default=None,
                      help="change stacks (default is a dry run) [DRY_RUN=false]")
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
    args.path_map = [tuple(m.split("=", 1)) for m in raw_maps]
    if not all(len(m) == 2 and m[0] and m[1] for m in args.path_map):
        p.error("--path-map must look like LRC_PREFIX=IMMICH_PREFIX")
    if args.unstack and not args.state_file:
        p.error("--unstack needs --state-file (or STATE_FILE)")
    if args.unstack and args.folder_owner == "all":
        p.error("--unstack cannot be combined with --folder-owner all")
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
                data = resp.read()
                return json.loads(data) if data else None
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
        """Return {stack id: (cover id, [asset ids])}."""
        return {s["id"]: (s["primaryAssetId"], [a["id"] for a in s["assets"]]) for s in self.call("GET", "/stacks")}

    def create_stack(self, asset_ids):
        return self.call("POST", "/stacks", {"assetIds": asset_ids})

    def remove_from_stack(self, stack_id, asset_id):
        self.call("DELETE", f"/stacks/{stack_id}/assets/{asset_id}")

    def delete_stack(self, stack_id):
        self.call("DELETE", f"/stacks/{stack_id}")


class Stacks:
    """Local view of Immich stacks, kept current as changes are made."""

    def __init__(self, stacks):
        self.stacks, self.by_asset = {}, {}
        for sid, (cover, ids) in stacks.items():
            self.put(sid, cover, ids)

    def put(self, sid, cover, ids):
        for old in {self.by_asset.get(i) for i in ids} - {None, sid}:
            self.drop(old)
        self.stacks[sid] = (cover, list(ids))
        for i in ids:
            self.by_asset[i] = sid

    def drop(self, sid):
        for i in self.stacks.pop(sid, (None, []))[1]:
            if self.by_asset.get(i) == sid:
                del self.by_asset[i]

    def remove(self, sid, asset):
        cover, ids = self.stacks[sid]
        self.stacks[sid] = (cover, [i for i in ids if i != asset])
        self.by_asset.pop(asset, None)


class Catalog:
    def __init__(self, backups_dir, backup):
        self.backups_dir, self.backup = backups_dir, backup
        self.mtime = os.path.getmtime(backup)
        self.stacks = []  # ordered lists of (root, rel path)
        self.files = collections.defaultdict(list)  # root -> [rel path]
        self.touch = {}  # (root, rel path) -> last edit time
        self.prefixes = {}  # root -> Immich path prefix (lowercase)

    def asset_path(self, root, rel):
        prefix = self.prefixes.get(root)
        return None if prefix is None else prefix + rel.lower()


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


def read_catalog(cat):
    with tempfile.TemporaryDirectory() as tmp:
        db = open_catalog(cat.backup, tmp)
        try:
            image_path = {}
            for root, rel, image, touch in db.execute("""
                select rf.absolutePath, fo.pathFromRoot || f.baseName || '.' || f.extension, i.id_local,
                       coalesce(i.touchTime, 0)
                from Adobe_images i
                join AgLibraryFile f on f.id_local = i.rootFile
                join AgLibraryFolder fo on fo.id_local = f.folder
                join AgLibraryRootFolder rf on rf.id_local = fo.rootFolder"""):
                image_path[image] = (root, rel)
                if (root, rel) not in cat.touch:
                    cat.files[root].append(rel)
                cat.touch[(root, rel)] = max(cat.touch.get((root, rel), 0), touch)
            stacks = collections.defaultdict(list)
            for stack, _, image in db.execute(
                "select stack, cast(position as real), image from AgLibraryFolderStackImage order by stack, 2"
            ):
                if image in image_path and image_path[image] not in stacks[stack]:  # skip virtual copies
                    stacks[stack].append(image_path[image])
        finally:
            db.close()
    cat.stacks = [s for s in stacks.values() if len(s) > 1]


def detect_prefixes(cat, assets, path_map):
    """Map each Lightroom root folder to an Immich path prefix (explicit map, else suffix voting)."""
    by_name = collections.defaultdict(list)
    for key in assets:
        by_name[key.rsplit("/", 1)[-1]].append(key)
    for root, rels in cat.files.items():
        explicit = next((dst + root[len(src):] for src, dst in path_map if root.lower().startswith(src.lower())), None)
        if explicit:
            cat.prefixes[root] = explicit.lower()
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
            cat.prefixes[root] = prefix
            log(f"  folder {root!r} -> {prefix!r} ({count}/{sampled} sampled files matched)")
        else:
            log(f"  folder {root!r} -> not found in Immich, skipped")


def assign_owners(catalogs, assets, policy):
    """Return {Immich folder: catalog index} for folders with photos in Immich."""
    score = collections.defaultdict(dict)  # folder -> {catalog index: (key...)}
    for n, cat in enumerate(catalogs):
        for (root, rel), touch in cat.touch.items():
            path = cat.asset_path(root, rel)
            if path is None or path not in assets:
                continue
            folder = posixpath.dirname(path)
            best = score[folder].get(n, (0, 0))
            score[folder][n] = max(best, (touch, cat.mtime) if policy == "latest-edit" else (cat.mtime, touch))
    owners = {f: max(c, key=c.get) for f, c in score.items()}
    contested = collections.Counter(
        (catalogs[owners[f]].backups_dir, len(c)) for f, c in score.items() if len(c) > 1
    )
    for (backups_dir, n), count in sorted(contested.items()):
        log(f"  {count} folder(s) shared by {n} catalogs owned by {backups_dir} ({policy})")
    return owners


def desired_groups(catalogs, assets, owners, policy):
    """Return ordered Lightroom stacks as asset id lists, from each folder's owning catalog."""
    groups = []
    for n, cat in enumerate(catalogs):
        for lr_stack in cat.stacks:
            ids = []
            for root, rel in lr_stack:
                path = cat.asset_path(root, rel)
                if path in assets and (policy == "all" or owners.get(posixpath.dirname(path)) == n):
                    ids.append(assets[path])
            if len(ids) > 1:
                groups.append((lr_stack[0][1], len(lr_stack), ids))
    return groups


def stem(path):
    return posixpath.splitext(posixpath.basename(path))[0]


def unstack(groups, managed, owned_ids, paths, view, immich, dry_run):
    """Remove managed photos from Immich stacks that mix photos Lightroom no longer stacks together."""
    group_of = {i: n for n, (_, _, ids) in enumerate(groups) for i in ids}
    removed = deleted = 0
    for sid, (cover, ids) in list(view.stacks.items()):
        parts = collections.OrderedDict()
        for i in ids:
            if i in managed and i in owned_ids:
                parts.setdefault(group_of.get(i, ("single", i)), []).append(i)
        if len(parts) < 2:
            continue
        keep = next((k for k, v in parts.items() if cover in v), None)
        if keep is None:  # cover is not ours (e.g. an exported JPG): keep the biggest Lightroom group,
            groups_only = [k for k in parts if not isinstance(k, tuple)]  # else the cover's RAW/JPG sibling
            keep = max(groups_only, key=lambda k: len(parts[k])) if groups_only else next(
                (k for k, v in parts.items() if stem(paths[v[0]]) == stem(paths.get(cover, ""))), next(iter(parts)))
        drop = [i for k, v in parts.items() if k != keep for i in v]
        log(f"  unstack {len(drop)} photo(s) from stack of {posixpath.basename(paths.get(cover, cover))}")
        for i in drop:
            if not dry_run:
                immich.remove_from_stack(sid, i)
            view.remove(sid, i)
            removed += 1
        if len(view.stacks[sid][1]) < 2:
            if not dry_run:
                immich.delete_stack(sid)
            view.drop(sid)
            deleted += 1
    return removed, deleted


def plan(ids, view):
    """Return ordered asset ids (cover first) for one Lightroom stack, or None if nothing to do."""
    # Pull in whole existing stacks that touch a member, keeping Lightroom order.
    ordered = []
    for m in ids:
        sid = view.by_asset.get(m)
        group = [m] + ([] if sid is None else [view.stacks[sid][0]] + view.stacks[sid][1])
        ordered += [i for i in dict.fromkeys(group) if i not in ordered]
    # Cover: the existing cover of the Lightroom top image's stack, else the Lightroom top image.
    top_sid = view.by_asset.get(ids[0])
    cover = view.stacks[top_sid][0] if top_sid else ids[0]
    ordered = [cover] + [i for i in ordered if i != cover]
    sids = {view.by_asset.get(i) for i in ordered}
    if len(sids) == 1 and None not in sids and view.stacks[next(iter(sids))][0] == cover:
        return None  # already one stack with the right cover
    return ordered


def load_state(path):
    if not path or not os.path.exists(path):
        return set()
    with open(path) as f:
        return set(json.load(f).get("managed", []))


def save_state(path, managed):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"version": 1, "managed": sorted(managed)}, f)
    os.replace(tmp, path)


def run_once(args):
    log(f"starting (dry_run={args.dry_run}, folder_owner={args.folder_owner}, unstack={args.unstack})")
    immich = Immich(args.api_url, args.api_key)
    assets = immich.assets(args.library_path)
    view = Stacks(immich.stacks())
    paths = {i: p for p, i in assets.items()}
    log(f"{len(assets)} assets under {args.library_path}, {len(view.stacks)} existing stacks")

    catalogs = []
    for backups_dir in args.backups_dir:
        backup = newest_backup(backups_dir)
        if not backup:
            log(f"{backups_dir}: no catalog backup found")
            continue
        log(f"{backups_dir}: using {backup}")
        cat = Catalog(backups_dir, backup)
        read_catalog(cat)
        detect_prefixes(cat, assets, args.path_map)
        catalogs.append(cat)
    if not catalogs:
        return

    owners = assign_owners(catalogs, assets, args.folder_owner)
    groups = desired_groups(catalogs, assets, owners, args.folder_owner)
    managed = load_state(args.state_file) | {i for _, _, ids in groups for i in ids}

    if args.unstack:
        owned_ids = {i for p, i in assets.items() if posixpath.dirname(p) in owners}
        removed, deleted = unstack(groups, managed, owned_ids, paths, view, immich, args.dry_run)
    else:
        removed = deleted = 0

    changed = unchanged = 0
    for top, size, ids in groups:
        ordered = plan(ids, view)
        if ordered is None:
            unchanged += 1
            continue
        changed += 1
        log(f"  stack {top} ({size} in Lightroom) -> {len(ordered)} assets")
        if args.dry_run:
            view.put(f"dry-run-{changed}", ordered[0], ordered)
        else:
            s = immich.create_stack(ordered)
            view.put(s["id"], s["primaryAssetId"], [a["id"] for a in s["assets"]])

    if args.state_file and not args.dry_run:
        save_state(args.state_file, managed)
    verb = "would be " if args.dry_run else ""
    log(f"done: {changed} stacks {verb}created/merged, {unchanged} already match"
        + (f", {removed} photos {verb}unstacked, {deleted} stacks {verb}deleted" if args.unstack else ""))


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
