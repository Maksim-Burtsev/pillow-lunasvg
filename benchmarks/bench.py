#!/usr/bin/env python3
"""Benchmark pillow-lunasvg vs resvg-py vs cairosvg: SVG -> PNG speed and fidelity.

Corpora are downloaded on first run into benchmarks/corpus/ (git-ignored):
- simple-icons (CC0), release tag below: real-world single-colour icons;
- resvg's own feature test suite (MIT/Apache-2.0), all categories except text
  (text output depends on installed fonts, which differ between renderers).

Speed: every file is rendered to PNG bytes with its longest side = N px;
renderers are interleaved per file, each file timed as the min of REPEATS runs.
Fidelity: RGBA at 256 px compared against resvg on premultiplied pixels.

Usage: python benchmarks/bench.py [--limit N] [--out DIR]
Needs: pillow-lunasvg, resvg-py, cairosvg (+ system cairo), numpy, matplotlib.
"""
import argparse
import collections
import datetime
import io
import pickle
import platform
import statistics
import subprocess
import tarfile
import time
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

import pillow_lunasvg

SIMPLE_ICONS = ("16.31.0", "a538b54a37be7ab07420198de7a9f9985e2a4528")
RESVG = ("v0.48.1", "68b14c4c3bccdb60344c777406486b54c36ec1a4")
SIZES = (256, 1024)
REPEATS = 3
# A pixel differs if a premultiplied RGBA channel differs by more than the
# tolerance (out of 255); a file matches if at most FILE_TOL of its pixels differ.
# Strict (~10%) also counts anti-aliasing of thin strokes; loose (25%) mostly
# catches missing or different features.
PIXEL_TOLS = (26, 64)
FILE_TOL = 0.01
HERE = Path(__file__).parent
# resvg-py needs 1,095 s (18 min) for this file at 1024 px (feMorphology
# with a huge radius); left out of every measurement.
EXCLUDE = {"filters/feMorphology/huge-radius.svg"}
Image.MAX_IMAGE_PIXELS = None  # trusted corpus


def fetch(url: str, dst: Path) -> Path:
    if not dst.exists():
        print(f"downloading {url}")
        tgz = dst.with_suffix(".tar.gz")
        urllib.request.urlretrieve(url, tgz)
        with tarfile.open(tgz) as tf:
            tf.extractall(dst.parent, filter="data")
        tgz.unlink()
    return dst


def load_corpora() -> dict[str, list[tuple[str, bytes]]]:
    root = HERE / "corpus"
    root.mkdir(exist_ok=True)
    tag = SIMPLE_ICONS[0]
    si = fetch(f"https://github.com/simple-icons/simple-icons/archive/refs/tags/{tag}.tar.gz",
               root / f"simple-icons-{tag}")
    tag = RESVG[0]
    rv = fetch(f"https://github.com/linebender/resvg/archive/refs/tags/{tag}.tar.gz",
               root / f"resvg-{tag.lstrip('v')}")
    tests = rv / "crates/resvg/tests/tests"
    return {
        "simple-icons": [(p.name, p.read_bytes()) for p in sorted((si / "icons").glob("*.svg"))],
        "resvg tests": [(str(p.relative_to(tests)), p.read_bytes())
                        for p in sorted(tests.rglob("*.svg"))
                        if p.relative_to(tests).parts[0] != "text" and str(p.relative_to(tests)) not in EXCLUDE],
    }


def target_size(data: bytes, n: int) -> tuple[float, tuple[int, int]]:
    """Scale and pixel size (longest side n) taken from pillow-lunasvg's intrinsic size."""
    im = Image.open(io.BytesIO(data))
    scale = n / max(im.size)
    return scale, (max(1, round(im.width * scale)), max(1, round(im.height * scale)))


def r_lunasvg(data, scale, wh, png=True):
    im = Image.open(io.BytesIO(data))
    im.load(scale=scale)
    if not png:
        return im
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def r_resvg(data, scale, wh):
    import resvg_py
    # skip_system_fonts: no text in these corpora; loading the font database
    # on every call would roughly double resvg-py's time.
    return resvg_py.svg_to_bytes(svg_string=data.decode("utf-8"), width=wh[0], height=wh[1],
                                 skip_system_fonts=True)


def r_cairosvg(data, scale, wh):
    import cairosvg
    return cairosvg.svg2png(bytestring=data, output_width=wh[0], output_height=wh[1])


RENDERERS = {"pillow-lunasvg": r_lunasvg, "resvg-py": r_resvg, "cairosvg": r_cairosvg}


def to_rgba(png: bytes, wh) -> np.ndarray | None:
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    if im.size != wh:
        return None
    a = np.asarray(im).astype(np.int32)
    a[..., :3] = a[..., :3] * a[..., 3:] // 255  # premultiply: ignore colour of transparent pixels
    return a


def mismatch(a: np.ndarray | None, ref: np.ndarray | None) -> dict[int, float] | None:
    """Share of differing pixels per tolerance, or None if not comparable."""
    if a is None or ref is None:
        return None
    diff = np.abs(a - ref).max(axis=2)
    return {tol: float((diff > tol).mean()) for tol in PIXEL_TOLS}


def matches(v, tol) -> bool:
    return v is not None and v[tol] <= FILE_TOL


def fidelity_pass(corpora, limit):
    """Render every file once at 256 px; compare with resvg-py."""
    errors = collections.Counter()  # (corpus, renderer)
    fidelity = {}  # (corpus, renderer) -> {file: mismatch() result}
    for cname, files in corpora.items():
        for name, data in files[:limit] if limit else files:
            try:
                scale, wh = target_size(data, 256)
            except Exception:
                scale, wh = None, (256, 256)
            pixels = {}
            for rname, fn in RENDERERS.items():
                try:
                    pixels[rname] = to_rgba(fn(data, scale, wh), wh)
                except Exception:
                    errors[(cname, rname)] += 1
            for rname in ("pillow-lunasvg", "cairosvg"):
                fidelity.setdefault((cname, rname), {})[name] = mismatch(pixels.get(rname), pixels.get("resvg-py"))
    return errors, fidelity


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="files per corpus (quick smoke run)")
    ap.add_argument("--out", default=str(HERE))
    ap.add_argument("--reuse-speed", action="store_true", help="reuse timings from the last run, redo fidelity")
    args = ap.parse_args()
    out_dir = Path(args.out)
    corpora = load_corpora()

    raw = HERE / "corpus" / "speed.pickle"
    if args.reuse_speed:
        speed, lunasvg_raw = pickle.loads(raw.read_bytes())
    else:
        speed, lunasvg_raw = speed_pass(corpora, args.limit)
        raw.write_bytes(pickle.dumps((speed, lunasvg_raw)))
    errors, fidelity = fidelity_pass(corpora, args.limit)
    if not args.limit:
        plot(speed, out_dir / "speed.png")
        samples(corpora, out_dir / "samples.png")
    write_report(out_dir / "results.md", corpora, args.limit, speed, lunasvg_raw, errors, fidelity)
    print(f"wrote {out_dir}/results.md")


def speed_pass(corpora, limit):
    speed = {}  # (corpus, size, renderer) -> list of per-file seconds
    lunasvg_raw = {}  # (corpus, size) -> list of seconds without PNG encoding
    for cname, files in corpora.items():
        files = files[:limit] if limit else files
        # Warm up every renderer (lazy imports, first-call init).
        for fn in RENDERERS.values():
            fn(corpora["simple-icons"][0][1], 1.0, (24, 24))
        for i, (name, data) in enumerate(files):
            if i % 500 == 0:
                print(f"{cname}: {i}/{len(files)}", flush=True)
            # Filters are timed out: only resvg implements them, so their time
            # would compare real work with skipped work. Still used for fidelity.
            if name.startswith("filters/"):
                continue
            try:
                sizes = {n: target_size(data, n) for n in SIZES}
            except Exception:
                sizes = {n: (None, (n, n)) for n in SIZES}
            for n, (scale, wh) in sizes.items():
                for rname, fn in RENDERERS.items():
                    best, png = None, None
                    for _ in range(REPEATS):
                        t0 = time.perf_counter()
                        try:
                            png = fn(data, scale, wh)
                        except Exception:
                            png = None
                            break
                        t = time.perf_counter() - t0
                        best = t if best is None else min(best, t)
                    if png is not None:
                        speed.setdefault((cname, n, rname), []).append(best)
                if scale is not None:
                    best = min(_timed(lambda: r_lunasvg(data, scale, wh, png=False)) for _ in range(REPEATS))
                    lunasvg_raw.setdefault((cname, n), []).append(best)
        print(f"{cname}: {len(files)} files timed")
    return speed, lunasvg_raw


def _timed(fn) -> float:
    t0 = time.perf_counter()
    fn()
    return time.perf_counter() - t0


def plot(speed, out_png: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(8, 2.6), facecolor="white")
    colors = {"pillow-lunasvg": "#0b7285", "resvg-py": "#868e96", "cairosvg": "#ced4da"}
    names = list(RENDERERS)
    for ax, n in zip(axes, SIZES):
        vals = [statistics.median(speed[("simple-icons", n, r)]) * 1000 for r in names]
        bars = ax.barh(names[::-1], vals[::-1], color=[colors[r] for r in names[::-1]])
        for b, v in zip(bars, vals[::-1]):
            ax.text(b.get_width(), b.get_y() + b.get_height() / 2, f" {v:.2f}", va="center", fontsize=9)
        ax.set_title(f"{n}x{n} px", fontsize=11)
        ax.set_xlim(0, max(vals) * 1.25)
        ax.set_xlabel("median ms per icon, SVG to PNG (lower is better)", fontsize=8)
        ax.grid(axis="x", alpha=0.25, lw=0.6)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
    n_files = len(speed[("simple-icons", SIZES[0], names[0])])
    cpu = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip()
    fig.suptitle(f"simple-icons, {n_files:,} SVGs, {cpu}", fontsize=12)
    fig.tight_layout()
    fig.savefig(out_png, dpi=110, facecolor="white")


SAMPLE_FILES = [
    ("simple-icons", "python.svg"),
    ("simple-icons", "github.svg"),
    ("resvg tests", "paint-servers/radialGradient/fr=0.5.svg"),
    ("resvg tests", "masking/mask/simple-case.svg"),
    ("resvg tests", "painting/marker/with-viewBox-1.svg"),
    ("resvg tests", "filters/feGaussianBlur/simple-case.svg"),
]


def samples(corpora, out_png: Path) -> None:
    by_name = {(c, n): d for c, files in corpora.items() for n, d in files}
    cell, pad, label_w = 96, 8, 110
    names = list(RENDERERS)
    rows = [(c, n) for c, n in SAMPLE_FILES if (c, n) in by_name]
    sheet = Image.new("RGB", (label_w + len(rows) * (cell + pad), 20 + len(names) * (cell + pad)), "white")
    draw = ImageDraw.Draw(sheet)
    for j, key in enumerate(rows):
        data = by_name[key]
        scale, wh = target_size(data, cell)
        x = label_w + j * (cell + pad)
        parts = key[1].removesuffix(".svg").split("/")
        draw.text((x, 4), (parts[1] if len(parts) > 1 else parts[0])[:16], fill="black")
        for i, r in enumerate(names):
            y = 20 + i * (cell + pad)
            if j == 0:
                draw.text((4, y + cell // 2), r, fill="black")
            im = Image.open(io.BytesIO(RENDERERS[r](data, scale, wh))).convert("RGBA")
            sheet.paste(im, (x, y), im)
            draw.rectangle((x - 1, y - 1, x + cell, y + cell), outline="#dee2e6")
    sheet.save(out_png, optimize=True)


# Written by hand from inspecting mismatching files of the 2026-09-17 run;
# not regenerated by this script.
OBSERVATIONS = """
Typical causes of pillow-lunasvg mismatches, from looking at the failing files:

- Filters are not implemented in LunaSVG: the element is drawn without the effect (e.g. no blur). This is most of the `filters/` failures; the matching ones are mostly tests where the filter is invalid or a no-op, or uses `enable-background`, which resvg does not implement either.
- `<image>` with an embedded SVG (`embedded-svg*.svg`, `embedded-svgz.svg`) or WebP is not drawn; PNG, JPEG and GIF data URIs are drawn.
- Embedded raster images are scaled without smoothing, so upscaled PNG/JPEG looks blocky (1-6% of pixels differ).
- Patterns tile with a sub-pixel phase shift against resvg (2-31% of pixels differ); in `pattern/simple-case.svg` the two look the same.
- The CSS `transform` property (in `<style>` or `style=""`), `transform-origin`, `mix-blend-mode` and `context-fill`/`context-stroke` are not supported.
- `masking/mask/simple-case.svg` (mask filled with a gradient whose stops mix colour and opacity) renders empty.

cairosvg's low strict score on the resvg tests is mostly anti-aliasing of the 1 px frame; with the loose tolerance it matches 52.8% vs pillow-lunasvg's 59.0%, and it raised errors on 61 tests.

## Install footprint

Collected by hand, not by this script: pillow-lunasvg wheel sizes from the `wheels.yml` CI run 35139334260 (commit 653c187), others from PyPI on 2026-09-17. KB = 1,000 bytes.

| package | wheel size (CPython 3.13) | system libraries needed |
|---|---|---|
| pillow-lunasvg 0.1.0 | 407 KB manylinux x86_64, 1,445 KB musllinux x86_64 (bundles libstdc++), 259 KB macOS arm64, 467 KB win_amd64 | none |
| resvg-py 0.5.0 | 1,406 KB manylinux x86_64, 1,622 KB musllinux x86_64, 1,217 KB macOS arm64, 1,242 KB win_amd64 | none |
| cairosvg 2.9.1 + cairocffi, cffi, pycparser, cssselect2, tinycss2, defusedxml, webencodings | 470 KB total on manylinux x86_64 | libcairo (Homebrew `cairo` pulls 17 more formulae, 142 MB installed on this Mac); on Windows, cairo has to come from GTK/MSYS2 |
""".splitlines()


def md_table(header, rows) -> list[str]:
    return ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)] + [
        "| " + " | ".join(r) + " |" for r in rows]


def write_report(path, corpora, limit, speed, lunasvg_raw, errors, fidelity) -> None:
    import PIL
    import cairosvg
    import importlib.metadata as md

    def sh(cmd):
        return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()

    cairo_version = sh(["pkg-config", "--modversion", "cairo"]) or "unknown"
    ms = lambda xs: f"{statistics.median(xs) * 1000:.2f}"
    tot = lambda xs: f"{sum(xs):.2f}"
    counts = {c: len(f[:limit] if limit else f) for c, f in corpora.items()}
    lines = [
        "# pillow-lunasvg benchmark",
        "",
        f"Date: {datetime.date.today().isoformat()}. Machine: {sh(['sysctl', '-n', 'machdep.cpu.brand_string'])}, "
        f"macOS {platform.mac_ver()[0]}, Python {platform.python_version()}.",
        "",
        f"Versions: pillow-lunasvg {pillow_lunasvg.__version__} built locally from commit "
        f"{sh(['git', '-C', str(HERE), 'rev-parse', '--short', 'HEAD'])} (LunaSVG {pillow_lunasvg.lunasvg_version}), "
        f"Pillow {PIL.__version__}, resvg-py {md.version('resvg-py')}, cairosvg {cairosvg.__version__} "
        f"(cairocffi {md.version('cairocffi')}, cairo {cairo_version} from Homebrew).",
        "",
        "Corpora (downloaded at run time, not vendored):",
        f"- simple-icons {SIMPLE_ICONS[0]} (commit {SIMPLE_ICONS[1]}), CC0: "
        f"{counts['simple-icons']} icons from `icons/`, all 24x24 viewBox, mostly one `<path>` each.",
        f"- resvg {RESVG[0]} (commit {RESVG[1]}), MIT/Apache-2.0: {counts['resvg tests']} feature tests from "
        "`crates/resvg/tests/tests/`, every category except `text/` (fonts differ between renderers). "
        "Small (mostly 200x200) files that each exercise one SVG feature, including edge cases and "
        "filters. `filters/feMorphology/huge-radius.svg` is left out: resvg-py needs 1,095 s for it at "
        "1024 px (one run).",
        "",
        "## Speed: SVG bytes to PNG bytes",
        "",
        f"Longest side scaled to N px. Each file timed as the min of {REPEATS} runs, renderers interleaved "
        "per file. pillow-lunasvg = `Image.open` + `load(scale=)` + `save(PNG)` with Pillow's default zlib "
        "level; resvg-py = `svg_to_bytes(width=, height=, skip_system_fonts=True)`; cairosvg = "
        "`svg2png(output_width=, output_height=)`. The render-only row is pillow-lunasvg without the PNG "
        "encode (RGBA pixels in memory), which is what a thumbnail pipeline that re-encodes to "
        "WebP/JPEG pays. Files a renderer rejected are left out of its numbers. `filters/` tests are not "
        "timed: only resvg implements filters, so its time there is real work the other two skip "
        "(several feMorphology tests take 1-45 s each in resvg-py at 1024 px).",
        "",
    ]
    rows = []
    for cname in corpora:
        for n in SIZES:
            for r in RENDERERS:
                xs = speed.get((cname, n, r), [])
                if xs:
                    rows.append([cname, f"{n}", r, ms(xs), tot(xs), f"{len(xs)}"])
            xs = lunasvg_raw.get((cname, n), [])
            if xs:
                rows.append([cname, f"{n}", "pillow-lunasvg, render only", ms(xs), tot(xs), f"{len(xs)}"])
    lines += md_table(["corpus", "px", "renderer", "median ms/file", "total s", "files"], rows)
    lines += [
        "",
        "![speed chart](speed.png)",
        "",
        "## Fidelity against resvg (256 px)",
        "",
        f"Both images are compared as premultiplied RGBA (so the colour of fully transparent pixels "
        f"does not count). A pixel differs if any channel differs by more than the tolerance; a file "
        f"matches if at most {FILE_TOL:.0%} of its pixels differ. Strict tolerance = {PIXEL_TOLS[0]}/255, "
        f"loose = {PIXEL_TOLS[1]}/255. The strict one also counts anti-aliasing differences: every resvg "
        "test has a 1 px frame stroke, and a few levels of alpha difference along it are enough to fail "
        "a file. resvg is the reference because it has the most complete SVG support of the three, not "
        "because it is always right.",
        "",
    ]
    rows = []
    for cname in corpora:
        for r in ("pillow-lunasvg", "cairosvg"):
            res = fidelity.get((cname, r), {})
            cells = []
            for tol in PIXEL_TOLS:
                ok = sum(matches(v, tol) for v in res.values())
                cells.append(f"{ok}/{len(res)} ({ok / len(res):.1%})")
            rows.append([cname, r, *cells, f"{sum(v is None for v in res.values())}"])
    lines += md_table(["corpus", "renderer", "matching, strict", "matching, loose", "not comparable"], rows)
    lines += [
        "",
        "Not comparable = one of the two renderers raised an error or produced a different pixel size.",
        "",
        "resvg tests by category (strict / loose):",
        "",
    ]
    cats = sorted({k.split("/")[0] for k in fidelity.get(("resvg tests", "pillow-lunasvg"), {})})
    rows = []
    for cat in cats:
        row = [cat]
        for r in ("pillow-lunasvg", "cairosvg"):
            res = {k: v for k, v in fidelity[("resvg tests", r)].items() if k.startswith(cat + "/")}
            a, b = (sum(matches(v, tol) for v in res.values()) for tol in PIXEL_TOLS)
            row.append(f"{a / len(res):.0%} / {b / len(res):.0%} of {len(res)}")
        rows.append(row)
    lines += md_table(["category", "pillow-lunasvg", "cairosvg"], rows)
    worst = collections.Counter()
    for k, v in fidelity.get(("resvg tests", "pillow-lunasvg"), {}).items():
        if not matches(v, PIXEL_TOLS[1]):
            worst["/".join(k.split("/")[:2])] += 1
    lines += ["", "resvg test subdirectories with the most pillow-lunasvg mismatches (loose): " +
              ", ".join(f"`{k}` ({c})" for k, c in worst.most_common(12)) + "."]
    lines += ["", "Renderer errors at 256 px: " + ", ".join(
        f"{r} on {c}: {errors[(c, r)]}" for c in corpora for r in RENDERERS) + "."]
    lines += OBSERVATIONS
    lines += [
        "",
        "Samples (top to bottom: pillow-lunasvg, resvg-py, cairosvg):",
        "",
        "![samples](samples.png)",
        "",
        "Reproduce: `brew install cairo`, then in a fresh environment "
        "`pip install . resvg-py cairosvg numpy matplotlib` and "
        "`DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib python benchmarks/bench.py`. "
        "The run overwrites results.md, speed.png and samples.png, and keeps raw timings in "
        "`benchmarks/corpus/speed.pickle`; `--reuse-speed` redoes only the fidelity part. This file: one "
        "full run (about 40 minutes), then `--reuse-speed` after the fidelity tolerances were added. "
        "The machine was not idle (another development session was running), which is why each file "
        "is timed as the min of several runs.",
    ]
    path.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
