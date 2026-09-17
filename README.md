# pillow-lunasvg

[![PyPI](https://img.shields.io/pypi/v/pillow-lunasvg.svg)](https://pypi.org/project/pillow-lunasvg/)
[![Python versions](https://img.shields.io/pypi/pyversions/pillow-lunasvg.svg)](https://pypi.org/project/pillow-lunasvg/)
[![CI](https://github.com/Maksim-Burtsev/pillow-lunasvg/actions/workflows/wheels.yml/badge.svg)](https://github.com/Maksim-Burtsev/pillow-lunasvg/actions/workflows/wheels.yml)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

SVG read support for [Pillow](https://python-pillow.org), powered by
[LunaSVG](https://github.com/sammycage/lunasvg), a small C++ SVG renderer.

```python
import pillow_lunasvg  # registers the plugin
from PIL import Image

im = Image.open("logo.svg")        # RGBA, intrinsic size
im.load(scale=4)                    # optional: crisp render at 4x, before any other use
im.save("logo.png")
```

```bash
pip install pillow-lunasvg
```

Wheels for Linux (glibc and musl; x86_64, aarch64), macOS (arm64, x86_64) and
Windows (AMD64), CPython 3.10–3.14. No system libraries: LunaSVG and its
rasterizer plutovg are compiled into the wheel. Building the sdist needs CMake
and a C++17 compiler.

## Usage

`Image.open` reads the SVG and sets `size` from its `width`/`height` or
`viewBox`, rounded up to whole pixels. Pixels are rendered on the first
`load()`, which Pillow calls for you on any pixel access. The result is RGBA
on a transparent background.

**Render at a different size.** Call `load(scale=...)` yourself, before
anything else touches the pixels; the first `load()` wins, later calls return
what is already rendered. For a crisp thumbnail, render the vector close to
the target size instead of downscaling a small bitmap:

```python
im = Image.open("icon.svg")
im.load(scale=512 / max(im.size))   # longest side = 512 px
im.thumbnail((512, 512))
```

Sizes are checked against `Image.MAX_IMAGE_PIXELS` like any other Pillow
image, at `open` and again at `load(scale=)`.

**White background** (e.g. for JPEG):

```python
im = Image.open("logo.svg")
bg = Image.new("RGBA", im.size, "white")
Image.alpha_composite(bg, im).convert("RGB").save("logo.jpg")
```

**Fonts.** `<text>` uses system fonts. Servers and containers often have
none; register font files at startup:

```python
pillow_lunasvg.add_font_file("/fonts/Inter-Regular.ttf", "Inter")
pillow_lunasvg.add_font_file("/fonts/DejaVuSans.ttf")  # no family: fallback for any text
```

`add_font_file` raises `OSError` if the file cannot be loaded. The LunaSVG
version is in `pillow_lunasvg.lunasvg_version`.

## Why

Pillow does not read SVG, and
[python-pillow/Pillow#3509](https://github.com/python-pillow/Pillow/issues/3509)
has been open since 2018; the maintainers point to a third-party plugin. The
usual workaround, cairosvg, needs the system cairo library, and
"no library called cairo was found" is a common install failure, especially
on Windows and in slim containers. pillow-lunasvg is a regular wheel with
nothing else to install, and it plugs into `Image.open`, so existing Pillow
code (`thumbnail`, `convert`, `save`) works on SVG files unchanged.

## Benchmarks

3,460 real-world icons from [simple-icons](https://github.com/simple-icons/simple-icons)
16.31.0, SVG bytes to PNG bytes, Apple M4, median per icon:

![SVG to PNG time per icon](https://raw.githubusercontent.com/Maksim-Burtsev/pillow-lunasvg/main/benchmarks/speed.png)

| | 256 px | 1024 px | matches resvg at 256 px (strict) |
|---|---|---|---|
| **pillow-lunasvg** | **1.00 ms** | **12.56 ms** | 99.0% of icons |
| resvg-py 0.5.0 | 2.84 ms | 34.85 ms | (reference) |
| cairosvg 2.9.1 | 2.17 ms | 23.63 ms | 99.4% of icons |

- 2.8× faster than resvg-py and 2.2× faster than cairosvg at 256 px (2.8× and
  1.9× at 1024 px). Most of pillow-lunasvg's time is Pillow's PNG encoder:
  rendering alone takes 0.11 ms at 256 px and 0.88 ms at 1024 px.
- **Where it loses:** on resvg's feature test suite (1,341 files, text
  excluded) only 59% of pillow-lunasvg renders match resvg (cairosvg: 53%).
  Filters are not implemented (17–21% of filter tests match), and neither are
  SVG inside `<image>`, WebP images, the CSS `transform` property,
  `transform-origin`, `mix-blend-mode` and `context-fill`. Filter tests are
  left out of the timings, because only resvg does that work.
- Wheels are 259–467 KB (1.4 MB on musllinux, which bundles libstdc++)
  and need no system libraries. resvg-py wheels are 1.2–1.6 MB, also
  self-contained; cairosvg needs the system cairo library.

Methodology, per-category fidelity, observed failure causes, sample renders
and exact commands: [benchmarks/results.md](benchmarks/results.md).

## What is supported

LunaSVG covers most of SVG 1.1 and SVG Tiny 1.2 static content: shapes and
paths, fills and strokes (dashes, caps, joins, markers), linear and radial
gradients, patterns, clipping paths and masks, `<use>` and `<symbol>`,
nested `<svg>`, transforms, opacity, CSS `<style>` sheets and `style`
attributes, `<text>` with system or registered fonts, and embedded raster
images in `data:` URIs.

Not supported:

- filters (`<filter>`, e.g. `feGaussianBlur` drop shadows): the element is drawn without the filter effect;
- animation (SMIL) and scripts;
- external file references (`<image href="photo.png">`, external CSS or fonts), by design, see Security;
- `.svgz` (gzip-compressed SVG);
- saving SVG (read-only plugin);
- `draft()`, per-call background colour, a standalone `render()` API;
- PyPy, free-threaded CPython builds and Windows ARM64 wheels.

Open an issue if you need one of these.

## Security

The plugin is meant to be usable for thumbnailing untrusted uploads.

- **External references are disabled** at build time
  (`LUNASVG_DISABLE_EXTERNAL_RESOURCES`): an SVG cannot make the renderer read
  local files or URLs. Images embedded as `data:` URIs still render; they are
  decoded by the stb_image copy vendored in plutovg.
- **Pixel limits** follow `Image.MAX_IMAGE_PIXELS`: `Image.open` and
  `load(scale=)` raise `DecompressionBombWarning`/`DecompressionBombError`
  like other Pillow formats. With the limit disabled, a size LunaSVG cannot
  allocate raises `OSError`.
- **Nesting depth** is limited to 256 levels of elements, including depth
  added by `<use>` expansion (the renderer is recursive; deeper documents
  would overflow the stack). Deeper documents raise `OSError`.
- **`<use>` expansion** is limited to 200 000 elements. A short chain of
  `<use>` elements, each copying a group that uses the previous one twice,
  otherwise expands exponentially; such documents raise `OSError`. Self-referencing and
  mutually referencing `<use>` elements are skipped by LunaSVG.
- **Dashes** are limited to about 1 000 000 per document: past that budget
  the remaining dashed strokes are drawn solid instead of hanging on a
  `stroke-dasharray` far smaller than the path. Dash lengths in `%` cannot be
  bounded before rendering, so those strokes are always drawn solid.
- XML entity expansion ("billion laughs") does not apply: LunaSVG skips the
  DOCTYPE internal subset and never expands custom entities.

These checks are tested against hostile inputs in a subprocess, but a
renderer written in C++ is not a sandbox. For untrusted input, run rendering
in a worker process with a timeout and a memory limit (for example
`resource.setrlimit(RLIMIT_AS, ...)` in a `multiprocessing` worker, or your
task queue's time and memory limits).

## Thread safety

Safe to use from multiple threads. Parsing and rendering release the GIL,
but all LunaSVG calls in a process are serialized by one lock (LunaSVG keeps
a global font cache), so one render runs at a time per process. For parallel
throughput, use processes.

## License

MIT (see [LICENSE](LICENSE)). The wheel bundles:

- [LunaSVG](https://github.com/sammycage/lunasvg), MIT;
- [plutovg](https://github.com/sammycage/plutovg), MIT; its rasterizer is
  derived from FreeType and is also under the FreeType License (FTL);
- [stb_image](https://github.com/nothings/stb) and related stb headers vendored
  in plutovg, MIT / public domain.

Their license texts ship inside every wheel
(`pillow_lunasvg-*.dist-info/licenses/`).
