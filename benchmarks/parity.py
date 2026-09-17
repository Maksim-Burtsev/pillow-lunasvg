"""Pixel parity with upstream LunaSVG: the plugin vs LunaSVG's own svg2png.

Build svg2png from the pinned submodule, then run over the benchmark corpora
(benchmarks/bench.py downloads them into benchmarks/corpus/):

    cmake -S third_party/lunasvg -B build-upstream -DLUNASVG_BUILD_EXAMPLES=ON \
        -DBUILD_SHARED_LIBS=OFF -DCMAKE_BUILD_TYPE=Release
    cmake --build build-upstream --target svg2png
    python benchmarks/parity.py build-upstream/examples/svg2png
"""

import collections
import pathlib
import subprocess
import sys
import tempfile

import pillow_lunasvg  # noqa: F401
from PIL import Image, ImageChops

CORPUS = pathlib.Path(__file__).parent / "corpus"
ROOTS = {
    "resvg tests": CORPUS / "resvg-0.48.1/crates/resvg/tests/tests",
    "simple-icons": CORPUS / "simple-icons-16.31.0/icons",
}
Image.MAX_IMAGE_PIXELS = None  # trusted corpus


def compare(svg2png: str, svg: pathlib.Path, tmp: pathlib.Path) -> str:
    for old in tmp.glob("*.png"):
        old.unlink()
    try:
        subprocess.run([svg2png, str(svg)], cwd=tmp, capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        pass
    outputs = list(tmp.glob("*.png"))
    try:
        ours = Image.open(svg, formats=["SVG"]).convert("RGBA")
    except Exception:
        ours = None
    if not outputs or ours is None:
        return "neither renders" if not outputs and ours is None else "only one renders"
    theirs = Image.open(outputs[0]).convert("RGBA")
    if theirs.size != ours.size:
        return "differs"
    return "differs" if ImageChops.difference(theirs, ours).getbbox() else "identical"


def main() -> None:
    svg2png = str(pathlib.Path(sys.argv[1]).resolve())
    with tempfile.TemporaryDirectory() as tmp:
        for name, root in ROOTS.items():
            files = sorted(p for p in root.rglob("*.svg") if p.relative_to(root).parts[0] != "text")
            results = collections.Counter()
            for svg in files:
                outcome = compare(svg2png, svg, pathlib.Path(tmp))
                results[outcome] += 1
                if outcome not in ("identical", "neither renders"):
                    print(f"  {outcome}: {svg.relative_to(root)}")
            print(f"{name}: {len(files)} files, {dict(results)}")


if __name__ == "__main__":
    main()
