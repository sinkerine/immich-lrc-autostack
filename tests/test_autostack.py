"""End-to-end tests against a fake Immich and generated catalogs. Run: python3 -m unittest"""
import contextlib
import io
import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import immich_lrc_autostack as m  # noqa: E402

LIB = "/mnt/photos/"


def make_catalog(path, root, files, stacks, mtime):
    """files: {rel: touchTime}; stacks: [[rel, ...]] in Lightroom order."""
    db = sqlite3.connect(path)
    db.executescript("""
        create table AgLibraryRootFolder (id_local integer primary key, absolutePath);
        create table AgLibraryFolder (id_local integer primary key, rootFolder, pathFromRoot);
        create table AgLibraryFile (id_local integer primary key, folder, baseName, extension);
        create table Adobe_images (id_local integer primary key, rootFile, touchTime);
        create table AgLibraryFolderStackImage (id_local integer primary key, stack, position, image);
    """)
    db.execute("insert into AgLibraryRootFolder values (1, ?)", (root,))
    folders = {}
    for n, (rel, touch) in enumerate(files.items(), 1):
        d, name = rel.rsplit("/", 1) if "/" in rel else ("", rel)
        fid = folders.setdefault(d, len(folders) + 1)
        db.execute("insert or ignore into AgLibraryFolder values (?, 1, ?)", (fid, d + "/" if d else ""))
        base, ext = name.rsplit(".", 1)
        db.execute("insert into AgLibraryFile values (?, ?, ?, ?)", (n, fid, base, ext))
        db.execute("insert into Adobe_images values (?, ?, ?)", (n, n, touch))
    ids = {rel: n for n, rel in enumerate(files, 1)}
    for s, members in enumerate(stacks, 1):
        for pos, rel in enumerate(members, 1):
            db.execute("insert into AgLibraryFolderStackImage (stack, position, image) values (?, ?, ?)",
                       (s, str(float(pos)), ids[rel]))
    db.commit()
    db.close()
    os.utime(path, (mtime, mtime))


class FakeImmich:
    def __init__(self, paths):
        self.paths = {f"id-{p}": LIB + p for p in paths}
        self.stack_of, self.covers, self.n = {}, {}, 0

    def assets(self, prefix):
        return {p.lower(): i for i, p in self.paths.items() if p.startswith(prefix)}

    def stacks(self):
        out = {}
        for sid, cover in self.covers.items():
            out[sid] = (cover, [i for i, s in self.stack_of.items() if s == sid])
        return out

    def create_stack(self, ids):
        merged = {i for i in ids}
        for sid, cover in list(self.covers.items()):
            if cover in ids:
                merged |= {i for i, s in self.stack_of.items() if s == sid}
                del self.covers[sid]
        self.n += 1
        sid = f"s{self.n}"
        self.covers[sid] = ids[0]
        for i in merged:
            self.stack_of[i] = sid
        return {"id": sid, "primaryAssetId": ids[0], "assets": [{"id": i} for i in merged]}

    def remove_from_stack(self, sid, i):
        assert self.covers[sid] != i and self.stack_of[i] == sid
        del self.stack_of[i]

    def delete_stack(self, sid):
        del self.covers[sid]
        self.stack_of = {i: s for i, s in self.stack_of.items() if s != sid}

    def groups(self):
        """{frozenset of file names: cover file name}"""
        out = {}
        for sid, cover in self.covers.items():
            out[frozenset(self.paths[i][len(LIB):] for i, s in self.stack_of.items() if s == sid)] = \
                self.paths[cover][len(LIB):]
        return out


class AutostackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.files = ["trip/a.arw", "trip/b.arw", "trip/c.arw", "trip/a-hdr.dng", "trip/a-hdr.jpg",
                      "home/x.arw", "home/y.arw"]
        self.immich = FakeImmich(self.files)
        m.Immich = lambda url, key: self.immich
        m.log = lambda msg: None
        self.state = os.path.join(self.tmp.name, "state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def catalog(self, name, stacks, mtime, touch=None, files=None):
        d = os.path.join(self.tmp.name, name, "2025-01-01 1200")
        os.makedirs(d, exist_ok=True)
        files = files or [f for f in self.files if not f.endswith(".jpg")]
        make_catalog(os.path.join(d, "Lightroom Catalog.lrcat"), "P:/Photos/",
                     {f: (touch or {}).get(f, 1.0) for f in files}, stacks, mtime)
        return os.path.join(self.tmp.name, name)

    def run_tool(self, *extra):
        dirs = [a for n in ("desk", "laptop") if os.path.isdir(a := os.path.join(self.tmp.name, n))]
        argv = [f"--backups-dir={d}" for d in dirs] + [f"--library-path={LIB}", "--apply",
                                                       f"--state-file={self.state}", *extra]
        os.environ["IMMICH_API_KEY"] = "test"
        m.run_once(m.parse_args(argv))

    def test_creates_stack_and_keeps_existing_cover(self):
        self.immich.create_stack(["id-trip/a-hdr.jpg", "id-trip/a-hdr.dng"])  # e.g. immich-stack pair
        self.catalog("desk", [["trip/a-hdr.dng", "trip/a.arw", "trip/b.arw"]], 100)
        self.run_tool()
        self.assertEqual(self.immich.groups(), {
            frozenset({"trip/a-hdr.jpg", "trip/a-hdr.dng", "trip/a.arw", "trip/b.arw"}): "trip/a-hdr.jpg"})
        before = dict(self.immich.covers)
        self.run_tool()
        self.assertEqual(self.immich.covers, before)  # idempotent

    def test_newest_backup_owns_folder(self):
        self.catalog("desk", [["trip/a.arw", "trip/b.arw"], ["home/x.arw", "home/y.arw"]], 100)
        self.catalog("laptop", [["trip/c.arw", "trip/b.arw"]], 200,
                     files=["trip/a.arw", "trip/b.arw", "trip/c.arw", "trip/a-hdr.dng"])
        self.run_tool()
        self.assertEqual(self.immich.groups(), {
            frozenset({"trip/b.arw", "trip/c.arw"}): "trip/c.arw",  # laptop is newer for trip/
            frozenset({"home/x.arw", "home/y.arw"}): "home/x.arw",  # only desk has home/
        })

    def test_latest_edit_owns_folder(self):
        self.catalog("desk", [["trip/a.arw", "trip/b.arw"]], 100, touch={"trip/a.arw": 50.0})
        self.catalog("laptop", [["trip/c.arw", "trip/b.arw"]], 200, touch={"trip/c.arw": 10.0})
        self.run_tool("--folder-owner=latest-edit")
        self.assertEqual(self.immich.groups(), {frozenset({"trip/a.arw", "trip/b.arw"}): "trip/a.arw"})

    def test_newer_catalog_without_stacks_wins(self):
        self.catalog("desk", [["home/x.arw", "home/y.arw"]], 100)
        self.run_tool("--unstack")
        self.catalog("laptop", [], 200)  # newer catalog has home/ unstacked
        self.run_tool("--unstack")
        self.assertEqual(self.immich.groups(), {})

    def test_folder_only_in_one_catalog_is_kept(self):
        self.catalog("desk", [["home/x.arw", "home/y.arw"]], 100)
        self.catalog("laptop", [["trip/a.arw", "trip/b.arw"]], 200, files=["trip/a.arw", "trip/b.arw"])
        self.run_tool()
        self.assertEqual(len(self.immich.groups()), 2)

    def test_all_combines_catalogs(self):
        self.catalog("desk", [["trip/a.arw", "trip/b.arw"]], 100)
        self.catalog("laptop", [["trip/c.arw", "trip/b.arw"]], 200)
        self.run_tool("--folder-owner=all")
        self.assertEqual(self.immich.groups(),
                         {frozenset({"trip/a.arw", "trip/b.arw", "trip/c.arw"}): "trip/c.arw"})

    def test_unstack_follows_owner(self):
        self.immich.create_stack(["id-trip/a-hdr.jpg", "id-trip/a-hdr.dng"])
        self.catalog("desk", [["trip/a-hdr.dng", "trip/a.arw", "trip/b.arw", "trip/c.arw"]], 100)
        self.run_tool("--unstack")
        # Newer laptop backup: c moved out, and x/y unstacked in a folder only desk had stacked.
        self.catalog("laptop", [["trip/a-hdr.dng", "trip/a.arw", "trip/b.arw"]], 200)
        self.run_tool("--unstack")
        self.assertEqual(self.immich.groups(), {
            frozenset({"trip/a-hdr.jpg", "trip/a-hdr.dng", "trip/a.arw", "trip/b.arw"}): "trip/a-hdr.jpg"})

    def test_unstack_leaves_unmanaged_stacks(self):
        self.immich.create_stack(["id-home/x.arw", "id-home/y.arw"])  # made by hand in Immich
        self.catalog("desk", [], 100)
        self.run_tool("--unstack")
        self.assertEqual(self.immich.groups(), {frozenset({"home/x.arw", "home/y.arw"}): "home/x.arw"})

    def test_unstack_whole_stack_and_regroup(self):
        self.catalog("desk", [["trip/a.arw", "trip/b.arw", "trip/c.arw"]], 100)
        self.run_tool("--unstack")
        self.catalog("laptop", [["trip/b.arw", "trip/c.arw"]], 200)
        self.run_tool("--unstack")
        self.assertEqual(self.immich.groups(), {frozenset({"trip/b.arw", "trip/c.arw"}): "trip/b.arw"})

    def test_dry_run_changes_nothing(self):
        self.catalog("desk", [["trip/a.arw", "trip/b.arw"]], 100)
        os.environ["IMMICH_API_KEY"] = "test"
        m.run_once(m.parse_args([f"--backups-dir={self.tmp.name}/desk", f"--library-path={LIB}",
                                 f"--state-file={self.state}"]))
        self.assertEqual(self.immich.groups(), {})
        self.assertFalse(os.path.exists(self.state))



class ScheduleTest(unittest.TestCase):
    def setUp(self):
        self.tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()

    def tearDown(self):
        if self.tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.tz
        time.tzset()

    def at(self, hh, mm):
        return time.mktime((2025, 6, 1, hh, mm, 0, 0, 0, -1))

    def test_later_today(self):
        self.assertEqual(m.seconds_until_next([(3, 0)], self.at(1, 30)), 90 * 60)

    def test_tomorrow(self):
        self.assertEqual(m.seconds_until_next([(3, 0)], self.at(3, 0)), 24 * 3600)

    def test_several_times(self):
        self.assertEqual(m.seconds_until_next([(3, 0), (15, 0)], self.at(4, 0)), 11 * 3600)

    def test_parse(self):
        os.environ["IMMICH_API_KEY"] = "test"
        args = m.parse_args(["--backups-dir=/x", "--run-at=15:00", "--run-at=3:05"])
        self.assertEqual(args.run_at, [(3, 5), (15, 0)])
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            m.parse_args(["--backups-dir=/x", "--run-at=25:00"])


if __name__ == "__main__":
    unittest.main()
