"""Storage benchmark for the reader's caches — run it before and after moving
page-cache/cover-cache to another disk, compare the two JSON files.

Runs INSIDE the kometa container (same mount path the app reads through):
    docker exec -i -e LABEL=pre kometa python - < ops/bench-cache.py

Writes /data/bench/<label>-<stamp>.json. The book selection is frozen in
/data/bench/selection.json on the first run so every run hits the same pages.
Drop caches first (VM + macOS) or pass 1 is measuring RAM, not the disk.
"""
import json
import os
import shutil
import statistics
import sys
import time
import urllib.request

sys.path.insert(0, "/app")
import kometa.db as db                       # noqa: E402
from kometa import reader                   # noqa: E402

LABEL = os.environ.get("LABEL", "run")
BASE = "http://127.0.0.1:6969"
OUT = "/data/bench"
CACHE = reader.PAGE_CACHE_DIR
SCRATCH = os.path.join(CACHE, "_bench")      # '_' prefix: eviction never touches it
os.makedirs(OUT, exist_ok=True)


def stats(ms):
    ms = sorted(ms)
    return {"n": len(ms), "total_s": round(sum(ms) / 1000, 2),
            "p50_ms": round(statistics.median(ms), 1) if ms else None,
            "p95_ms": round(ms[int(len(ms) * 0.95) - 1], 1) if ms else None,
            "max_ms": round(ms[-1], 1) if ms else None}


def get(url):
    t = time.perf_counter()
    with urllib.request.urlopen(url, timeout=120) as r:
        body = r.read()
    return (time.perf_counter() - t) * 1000, len(body)


def cached_pages(book):
    """(page, width) pairs this book has rendered and still current on disk."""
    vdir = os.path.join(CACHE, str(book["id"]), reader._version(book["size"], book["mtime"]))
    out = []
    for f in os.listdir(vdir) if os.path.isdir(vdir) else []:
        n, _, w = f.removesuffix(".jpg").partition("_")
        if n.isdigit() and w.isdigit():
            out.append((int(n), int(w)))
    return sorted(out)


def select():
    path = os.path.join(OUT, "selection.json")
    if os.path.exists(path):
        return json.load(open(path))
    ids = sorted(int(d) for d in os.listdir(CACHE) if d.isdigit())
    warm, cold = [], []
    for i in ids:
        b = db.get_book(i, db.DB_PATH)
        if not b or not b.get("page_count"):
            continue
        pages = cached_pages(b)
        if len(warm) < 20 and len(pages) >= 15:
            warm.append(i)
        elif len(cold) < 5 and (b["page_count"] or 0) >= 10 and len(pages) >= 10:
            cold.append(i)
    sel = {"warm": warm, "cold": cold}
    json.dump(sel, open(path, "w"))
    return sel


def warm_hits(ids):
    reqs = []
    for i in ids:
        b = db.get_book(i, db.DB_PATH)
        reqs += [(i, n, w) for n, w in cached_pages(b)]
    passes = []
    for _ in range(2):
        ms, nbytes = [], 0
        for i, n, w in reqs:
            t, size = get(f"{BASE}/api/books/{i}/pages/{n}?w={w}")
            ms.append(t)
            nbytes += size
        s = stats(ms)
        s["MB_s"] = round(nbytes / 1e6 / s["total_s"], 1) if s["total_s"] else None
        passes.append(s)
    return {"pages": len(reqs), "pass1": passes[0], "pass2": passes[1]}


def cold_renders(ids):
    ms = []
    for i in ids:
        b = db.get_book(i, db.DB_PATH)
        shutil.rmtree(os.path.join(CACHE, str(i)), ignore_errors=True)
        for n in range(1, 11):
            ms.append(get(f"{BASE}/api/books/{i}/pages/{n}?w={reader.DEFAULT_WIDTH}")[0])
    return stats(ms)


def drop(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    finally:
        os.close(fd)


def raw_io():
    os.makedirs(SCRATCH, exist_ok=True)
    out = {}
    big = os.path.join(SCRATCH, "seq.bin")
    block = os.urandom(8 << 20)
    t = time.perf_counter()
    with open(big, "wb") as fh:
        for _ in range(128):                 # 1 GiB
            fh.write(block)
        fh.flush()
        os.fsync(fh.fileno())
    out["seq_write_MB_s"] = round(1024 / (time.perf_counter() - t), 1)
    drop(big)
    t = time.perf_counter()
    with open(big, "rb") as fh:
        while fh.read(8 << 20):
            pass
    out["seq_read_MB_s"] = round(1024 / (time.perf_counter() - t), 1)

    small = os.urandom(256 << 10)            # ~ a rendered page
    sdir = os.path.join(SCRATCH, "small")
    os.makedirs(sdir, exist_ok=True)
    t = time.perf_counter()
    for k in range(2000):
        with open(os.path.join(sdir, f"{k}.jpg"), "wb") as fh:
            fh.write(small)
    os.sync()
    out["small_write_files_s"] = round(2000 / (time.perf_counter() - t), 1)
    for k in range(2000):
        drop(os.path.join(sdir, f"{k}.jpg"))
    t = time.perf_counter()
    for k in range(2000):
        with open(os.path.join(sdir, f"{k}.jpg"), "rb") as fh:
            fh.read()
    out["small_read_files_s"] = round(2000 / (time.perf_counter() - t), 1)
    shutil.rmtree(SCRATCH, ignore_errors=True)
    return out


def eviction_scan():
    """The walk _evict_if_needed does over the whole cache, twice."""
    runs = []
    for _ in range(2):
        t = time.perf_counter()
        files = total = 0
        for r, _, fs in os.walk(CACHE):
            for f in fs:
                total += os.path.getsize(os.path.join(r, f))
                files += 1
        runs.append(round(time.perf_counter() - t, 2))
    return {"files": files, "GB": round(total / 1e9, 2), "pass1_s": runs[0], "pass2_s": runs[1]}


sel = select()
result = {"label": LABEL, "at": time.strftime("%F %T"), "cache_dir_device": os.stat(CACHE).st_dev,
          "selection": sel}
for name, fn in (("warm_hits", lambda: warm_hits(sel["warm"])),
                 ("eviction_scan", eviction_scan),
                 ("raw_io", raw_io),
                 ("cold_renders", lambda: cold_renders(sel["cold"]))):
    print(f"… {name}", file=sys.stderr, flush=True)
    result[name] = fn()
path = os.path.join(OUT, f"{LABEL}-{time.strftime('%Y%m%d-%H%M')}.json")
json.dump(result, open(path, "w"), indent=1)
print(json.dumps(result, indent=1))
