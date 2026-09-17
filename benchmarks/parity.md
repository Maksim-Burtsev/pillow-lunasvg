# Pixel parity with upstream LunaSVG

The plugin is compared with LunaSVG's own `svg2png` example, built from the
same pinned commit (`2a6a43a`) without any of this project's code. A file
counts as identical only if every pixel of the RGBA output is equal.

Date: 2026-09-17, pillow-lunasvg 0.1.0, Apple M4, macOS 26.5.1.

| corpus | files | identical | differs | neither renders |
|---|---|---|---|---|
| resvg v0.48.1 feature tests (`text/` excluded) | 1342 | 1341 | 0 | 1 |
| simple-icons 16.31.0 | 3460 | 3460 | 0 | 0 |

The one file neither renders is `structure/svg/explicit-svg-namespace.svg`
(a root written as `<svg:svg>`), which LunaSVG does not parse.

Reproduce (the corpora are downloaded by `benchmarks/bench.py`):

```bash
cmake -S third_party/lunasvg -B build-upstream -DLUNASVG_BUILD_EXAMPLES=ON \
    -DBUILD_SHARED_LIBS=OFF -DCMAKE_BUILD_TYPE=Release
cmake --build build-upstream --target svg2png
python benchmarks/parity.py build-upstream/examples/svg2png
```
