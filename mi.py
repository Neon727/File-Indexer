#!/usr/bin/env python3
"""mi — a tiny file indexer. Usage:
    python mi.py index ~/Documents D:\\
    python mi.py search budget 2024
    python mi.py stats
"""
import argparse, os, sqlite3, sys, time
from pathlib import Path

DB = Path(os.environ.get("MI_DB", Path.home() / ".mi" / "index.db"))

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS files (
  id       INTEGER PRIMARY KEY,
  path     TEXT NOT NULL UNIQUE,
  name     TEXT NOT NULL,
  ext      TEXT,
  dir      TEXT NOT NULL,
  size     INTEGER,
  mtime    INTEGER,
  scan_gen INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_files_ext ON files(ext);

CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
  name, path, tokenize='unicode61'
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.executescript(SCHEMA)
    # migrate pre-scan_gen DBs in place
    cols = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
    if "scan_gen" not in cols:
        conn.execute("ALTER TABLE files ADD COLUMN scan_gen INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    return conn

def scan(root):
    root = os.path.abspath(root)
    for dirpath, _dirnames, filenames in os.walk(root, onerror=lambda e: None):
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue
            ext = os.path.splitext(fn)[1].lower().lstrip(".")
            yield p, fn, ext, dirpath, st.st_size, int(st.st_mtime)

def upsert(conn, row, gen):
    cur = conn.execute(
        """INSERT INTO files(path,name,ext,dir,size,mtime,scan_gen)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(path) DO UPDATE SET
             name=excluded.name, ext=excluded.ext, dir=excluded.dir,
             size=excluded.size, mtime=excluded.mtime, scan_gen=excluded.scan_gen
           RETURNING id""",
        (*row, gen),
    )
    fid = cur.fetchone()[0]
    conn.execute("DELETE FROM files_fts WHERE rowid=?", (fid,))
    conn.execute("INSERT INTO files_fts(rowid,name,path) VALUES(?,?,?)",
                 (fid, row[1], row[0]))

def root_prefix(root):
    return os.path.abspath(root).rstrip(os.sep) + os.sep + "%"

def bump_gen(conn, root):
    key = "gen:" + os.path.abspath(root)
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    gen = (int(row[0]) if row else 0) + 1
    conn.execute("INSERT OR REPLACE INTO meta VALUES(?, ?)", (key, str(gen)))
    return gen

def prune(conn, root, gen):
    prefix = root_prefix(root)
    stale = conn.execute(
        "SELECT COUNT(*) FROM files WHERE path LIKE ? AND scan_gen < ?",
        (prefix, gen),
    ).fetchone()[0]
    if not stale:
        return 0
    conn.execute(
        "DELETE FROM files_fts WHERE rowid IN "
        "(SELECT id FROM files WHERE path LIKE ? AND scan_gen < ?)",
        (prefix, gen),
    )
    conn.execute(
        "DELETE FROM files WHERE path LIKE ? AND scan_gen < ?",
        (prefix, gen),
    )
    return stale

def cmd_index(args):
    conn = connect()
    total_n, total_pruned, t0 = 0, 0, time.time()
    with conn:
        for root in args.paths:
            if not os.path.exists(root):
                print(f"skip (not found): {root}", file=sys.stderr)
                continue
            gen = bump_gen(conn, root)
            n = 0
            for row in scan(root):
                upsert(conn, row, gen)
                n += 1
                if n % 2000 == 0:
                    print(f"\r  {root}: {n:,} files...", end="", file=sys.stderr)
            total_n += n
            if not args.no_prune:
                total_pruned += prune(conn, root, gen)
        conn.execute("INSERT OR REPLACE INTO meta VALUES('last_index', ?)",
                     (str(int(time.time())),))
    print(f"\rIndexed {total_n:,} files"
          + (f", pruned {total_pruned:,} stale" if total_pruned else "")
          + f" in {time.time()-t0:.1f}s", file=sys.stderr)

def cmd_search(args):
    conn = connect()
    terms = args.query.replace('"', " ").split()
    if not terms:
        return
    fts_q = " ".join(t + "*" for t in terms)
    sql = """
      SELECT f.path, f.size, f.mtime
      FROM files_fts s JOIN files f ON f.id = s.rowid
      WHERE files_fts MATCH ?
      ORDER BY bm25(files_fts, 10.0, 1.0), f.mtime DESC
      LIMIT ?
    """
    try:
        rows = conn.execute(sql, (fts_q, args.limit)).fetchall()
    except sqlite3.OperationalError:
        like = "%" + "%".join(terms) + "%"
        rows = conn.execute(
            "SELECT path,size,mtime FROM files WHERE name LIKE ? ORDER BY mtime DESC LIMIT ?",
            (like, args.limit),
        ).fetchall()
    for path, size, mtime in rows:
        size_s = f"{size/1e6:.1f}MB" if size >= 1e6 else f"{size//1024}KB"
        date_s = time.strftime("%Y-%m-%d", time.localtime(mtime))
        print(f"{date_s}  {size_s:>8}  {path}")
    if not rows:
        print("no matches", file=sys.stderr)

def cmd_stats(args):
    conn = connect()
    total = conn.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files").fetchone()
    print(f"{total[0]:,} files, {total[1]/1e9:.2f} GB indexed")
    print("\ntop extensions:")
    for ext, c in conn.execute(
        "SELECT COALESCE(ext,'(none)'), COUNT(*) c FROM files GROUP BY ext ORDER BY c DESC LIMIT 10"
    ):
        print(f"  {c:>8,}  .{ext}")

def main():
    p = argparse.ArgumentParser(prog="mi")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("index", help="scan and index folders")
    pi.add_argument("paths", nargs="+")
    pi.add_argument("--no-prune", action="store_true",
                    help="don't delete stale entries under scanned roots")
    pi.set_defaults(func=cmd_index)

    ps = sub.add_parser("search", help="search the index")
    ps.add_argument("query")
    ps.add_argument("-n", "--limit", type=int, default=20)
    ps.set_defaults(func=cmd_search)

    pt = sub.add_parser("stats", help="index stats")
    pt.set_defaults(func=cmd_stats)

    args = p.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()
