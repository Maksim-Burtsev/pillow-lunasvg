import base64
import io
import subprocess
import sys
import textwrap
import threading
import time

import pytest
from PIL import Image, UnidentifiedImageError

import pillow_lunasvg

NS = 'xmlns="http://www.w3.org/2000/svg"'
HALF_RED = f'<svg {NS} width="10" height="10"><rect width="5" height="10" fill="red"/></svg>'.encode()


def open_svg(svg: str | bytes) -> Image.Image:
    if isinstance(svg, str):
        svg = svg.encode()
    return Image.open(io.BytesIO(svg))


def test_versions():
    assert pillow_lunasvg.__version__ == "0.1.0"
    assert pillow_lunasvg.lunasvg_version.count(".") == 2


def test_registration():
    assert Image.registered_extensions()[".svg"] == "SVG"
    assert Image.MIME["SVG"] == "image/svg+xml"
    assert "SVG" not in Image.SAVE


def test_open_path_bytesio_and_formats(tmp_path):
    path = tmp_path / "a.svg"
    path.write_bytes(HALF_RED)
    with Image.open(path) as im:
        assert (im.format, im.mode, im.size) == ("SVG", "RGBA", (10, 10))
        assert im.format_description == "Scalable Vector Graphics"
        im.load()
    with Image.open(io.BytesIO(HALF_RED), formats=["SVG"]) as im:
        assert im.format == "SVG"


def test_exact_pixels():
    im = open_svg(HALF_RED)
    im.load()
    for y in (0, 9):
        assert im.getpixel((0, y)) == (255, 0, 0, 255)
        assert im.getpixel((4, y)) == (255, 0, 0, 255)
        assert im.getpixel((5, y)) == (0, 0, 0, 0)
        assert im.getpixel((9, y)) == (0, 0, 0, 0)


def test_straight_alpha():
    im = open_svg(f'<svg {NS} width="4" height="4"><rect width="4" height="4" fill="red" fill-opacity="0.5"/></svg>')
    r, g, b, a = im.getpixel((1, 1))
    assert (r, g, b) == (255, 0, 0)
    assert 126 <= a <= 129


@pytest.mark.parametrize(
    ("attrs", "size"),
    [
        ('width="30" height="20"', (30, 20)),
        ('viewBox="0 0 40 10"', (40, 10)),
        ('width="10.2" height="3.5"', (11, 4)),
        ('width="20" viewBox="0 0 10 5"', (20, 10)),
    ],
)
def test_sizes(attrs, size):
    assert open_svg(f"<svg {NS} {attrs}></svg>").size == size


def test_no_size_uses_content_bounds():
    # LunaSVG falls back to the right/bottom edge of the painted content.
    im = open_svg(f'<svg {NS}><rect x="2" y="3" width="5" height="4"/></svg>')
    assert im.size == (7, 7)


def test_no_size_and_no_content_raises():
    with pytest.raises(OSError, match="width/height or viewBox"):
        open_svg(f"<svg {NS}></svg>")


def test_load_scale_is_crisp():
    im = open_svg(f'<svg {NS} width="10" height="10"><rect width="4" height="10" fill="red"/></svg>')
    im.load(scale=2.5)
    assert im.size == (25, 25)
    assert im.getpixel((9, 12)) == (255, 0, 0, 255)
    assert im.getpixel((10, 12)) == (0, 0, 0, 0)


def test_first_load_wins():
    im = open_svg(HALF_RED)
    im.load(scale=2)
    im.load(scale=3)
    assert im.size == (20, 20)


@pytest.mark.parametrize("scale", [0, -1, float("nan"), float("inf")])
def test_invalid_scale(scale):
    with pytest.raises(ValueError):
        open_svg(HALF_RED).load(scale=scale)


def test_pillow_integration(tmp_path):
    im = open_svg(HALF_RED)
    im.thumbnail((5, 5))
    assert im.size == (5, 5)
    rgb = open_svg(HALF_RED).convert("RGB")
    assert rgb.getpixel((0, 0)) == (255, 0, 0)
    out = tmp_path / "out.png"
    open_svg(HALF_RED).save(out)
    with Image.open(out) as png:
        assert png.format == "PNG"
        assert png.getpixel((0, 0)) == (255, 0, 0, 255)
        assert png.getpixel((9, 0)) == (0, 0, 0, 0)


@pytest.mark.parametrize(("fmt", "mode"), [("PNG", "RGBA"), ("JPEG", "RGB")])
def test_other_formats_still_open(fmt, mode):
    buf = io.BytesIO()
    Image.new(mode, (3, 3)).save(buf, fmt)
    buf.seek(0)
    with Image.open(buf) as im:
        assert im.format == fmt


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"   \n",
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title></channel></rss>',
        HALF_RED[: len(HALF_RED) // 2],
        b"<svg",
    ],
)
def test_unidentified(data):
    with pytest.raises(UnidentifiedImageError):
        Image.open(io.BytesIO(data))


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        (b"<svg xmlns=", True),
        (b"\xef\xbb\xbf  <?xml version", True),
        (b"\n<!-- comment", True),
        (b"<!doctype svg", True),
        (b"  \t\r\n", True),
        (b"", True),
        (b"\x89PNG\r\n\x1a\n", False),
        (b"\xff\xd8\xff\xe0", False),
        (b"GIF89a", False),
        (b"<html>", False),
    ],
)
def test_accept(prefix, expected):
    assert pillow_lunasvg._accept(prefix[:16]) is expected


def test_external_image_is_not_loaded(tmp_path):
    png = tmp_path / "red.png"
    Image.new("RGBA", (10, 10), (255, 0, 0, 255)).save(png)
    svg = f'<svg {NS} width="10" height="10"><image href="{png.as_posix()}" width="10" height="10"/></svg>'
    im = open_svg(svg)
    im.load()
    assert im.getbbox() is None


def test_data_uri_image_is_loaded():
    buf = io.BytesIO()
    Image.new("RGBA", (10, 10), (255, 0, 0, 255)).save(buf, "PNG")
    uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    im = open_svg(f'<svg {NS} width="10" height="10"><image href="{uri}" width="10" height="10"/></svg>')
    assert im.getpixel((5, 5)) == (255, 0, 0, 255)


def test_decompression_bomb_at_open():
    with pytest.raises(Image.DecompressionBombError):
        open_svg(f'<svg {NS} width="100000" height="100000"></svg>')


def test_decompression_bomb_at_load():
    im = open_svg(f'<svg {NS} width="1000" height="1000"></svg>')
    with pytest.raises(Image.DecompressionBombError):
        im.load(scale=20)


def test_absurd_scale_without_pixel_limit(monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", None)
    im = open_svg(HALF_RED)
    with pytest.raises(OSError):
        im.load(scale=1e6)


def run_isolated(svg: str) -> str:
    """Open and render in a subprocess so a crash fails the test instead of pytest."""
    code = textwrap.dedent(
        """
        import io, sys
        import pillow_lunasvg
        from PIL import Image
        try:
            im = Image.open(io.BytesIO(sys.stdin.buffer.read()))
            im.load()
            print("bbox", im.getbbox())
        except OSError as e:
            print("OSError", e)
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], input=svg.encode(), capture_output=True, timeout=10
    )
    assert proc.returncode == 0, proc.stderr.decode()
    return proc.stdout.decode()


def test_hostile_deep_nesting():
    n = 100_000
    svg = f'<svg {NS} width="10" height="10">' + "<g>" * n + '<rect width="5" height="5"/>' + "</g>" * n + "</svg>"
    assert "nested deeper than" in run_isolated(svg)


def test_nesting_scan_is_not_fooled_by_comments_or_attributes():
    closers = "</g>" * 1000
    svg = (
        f'<svg {NS} width="10" height="10"><!-- {closers} --><g data-x="{closers}">'
        + "<g>" * 300 + "</g>" * 300 + "</g></svg>"
    )
    assert "nested deeper than" in run_isolated(svg)


def test_moderate_nesting_renders():
    n = 200
    svg = f'<svg {NS} width="10" height="10">' + "<g>" * n + '<rect width="5" height="5"/>' + "</g>" * n + "</svg>"
    assert "bbox (0, 0, 5, 5)" in run_isolated(svg)


def test_hostile_use_cycles():
    svg = (
        f'<svg {NS} width="10" height="10">'
        '<use id="self" href="#self"/>'
        '<use id="a" href="#b"/><use id="b" href="#a"/>'
        '<rect width="2" height="2"/></svg>'
    )
    assert "bbox (0, 0, 2, 2)" in run_isolated(svg)


def doubling_use_chain(levels: int, href: str = "href") -> str:
    parts = ['<g id="g0"><rect width="1" height="1"/></g>'] + [
        f'<g id="g{i}"><use {href}="#g{i - 1}"/><use {href}="#g{i - 1}"/></g>' for i in range(1, levels)
    ]
    return (
        f'<svg {NS} xmlns:xlink="http://www.w3.org/1999/xlink" width="10" height="10"><defs>'
        + "".join(parts)
        + f'</defs><use href="#g{levels - 1}"/></svg>'
    )


@pytest.mark.parametrize("href", ["href", "xlink:href"])
def test_hostile_use_expansion(href):
    start = time.monotonic()
    assert "too many elements" in run_isolated(doubling_use_chain(30, href))
    assert time.monotonic() - start < 2


def test_hostile_use_expansion_with_entity_encoded_href():
    svg = doubling_use_chain(30).replace('href="#', 'href="&#x23;')
    assert "too many elements" in run_isolated(svg)


def test_hostile_use_expanded_depth():
    def deep(inner: str) -> str:
        return "<g>" * 200 + inner + "</g>" * 200

    rect = '<rect width="1" height="1"/>'
    use = '<use href="#a"/>'
    svg = (
        f'<svg {NS} width="10" height="10"><defs><g id="a">{deep(rect)}</g>'
        f'<g id="b">{deep(use)}</g></defs><use href="#b"/></svg>'
    )
    assert "nested deeper than" in run_isolated(svg)


def test_icon_with_a_few_uses_renders():
    svg = (
        f'<svg {NS} width="10" height="10"><defs><symbol id="dot" viewBox="0 0 2 2">'
        '<rect width="2" height="2" fill="red"/></symbol></defs>'
        '<use href="#dot" width="2" height="2"/><use href="#dot" x="4" width="2" height="2"/>'
        '<use href="#dot" x="8" y="8" width="2" height="2"/></svg>'
    )
    assert "bbox (0, 0, 10, 10)" in run_isolated(svg)


@pytest.mark.parametrize(
    "dash",
    [
        'stroke-dasharray="0.0001"',
        'style="stroke-dasharray: 0.5"',
        'stroke-dasharray="0.0001em"',
        'font-size="0.00001" stroke-dasharray="1em"',
        'font-size="1e-5em" stroke-dasharray="1ex"',
        'stroke-dasharray="1%"',
        'font-size="0" stroke-dasharray="0.0001em"',
        'font-size="1e9" stroke-dasharray="0.0001em"',
    ],
)
def test_hostile_dasharray(dash):
    # Past the dash budget the path is stroked solid instead of hanging.
    # 2e6 keeps 26.6 fixed-point coordinates within a 32-bit long (Windows).
    svg = f'<svg {NS} width="100" height="100"><path d="M0 50 L2000000 50" stroke="black" stroke-width="4" {dash}/></svg>'
    assert "bbox (0, 48, 100, 52)" in run_isolated(svg)


def test_hostile_inherited_dasharray_on_polyline():
    points = " ".join(f"{x},{x % 2 * 100}" for x in range(20000))
    svg = f'<svg {NS} width="100" height="100"><g stroke-dasharray="0.01"><polyline points="{points}" stroke="black" fill="none"/></g></svg>'
    assert "bbox" in run_isolated(svg)


def test_normal_dashes_still_render():
    im = open_svg(f'<svg {NS} width="20" height="4"><path d="M0 2 L20 2" stroke="black" stroke-width="4" stroke-dasharray="5"/></svg>')
    assert im.getpixel((2, 2))[3] == 255
    assert im.getpixel((7, 2))[3] == 0


def test_em_dashes_still_render():
    im = open_svg(f'<svg {NS} width="60" height="4"><path d="M0 2 L60 2" stroke="black" stroke-width="4" stroke-dasharray="2em"/></svg>')
    assert im.getpixel((2, 2))[3] == 255
    assert im.getpixel((30, 2))[3] == 0


def test_hostile_billion_laughs():
    entities = '<!ENTITY lol "lol">' + "".join(
        f'<!ENTITY lol{i} "' + f"&lol{i - 1 if i > 1 else ''};" * 10 + '">' for i in range(1, 10)
    )
    svg = f'<?xml version="1.0"?><!DOCTYPE svg [{entities}]><svg {NS} width="10" height="10"><text>&lol9;</text></svg>'
    out = run_isolated(svg)
    assert out.startswith(("bbox", "OSError"))


def test_threads():
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (0, 255, 255), (255, 0, 255), (128, 128, 128), (255, 255, 255)]
    results: dict[int, Image.Image] = {}
    errors: list[BaseException] = []

    def work(i: int) -> None:
        try:
            r, g, b = colors[i]
            for _ in range(20):
                im = open_svg(f'<svg {NS} width="64" height="64"><circle cx="32" cy="32" r="30" fill="rgb({r},{g},{b})"/></svg>')
                im.load(scale=2)
            results[i] = im
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    for i, im in results.items():
        assert im.size == (128, 128)
        assert im.getpixel((64, 64)) == (*colors[i], 255)
        assert im.getpixel((0, 0)) == (0, 0, 0, 0)


def test_add_font_file_missing_path(tmp_path):
    with pytest.raises(OSError):
        pillow_lunasvg.add_font_file(tmp_path / "missing.ttf")
    with pytest.raises(OSError):
        pillow_lunasvg.add_font_file(str(tmp_path / "missing.ttf"), "Family", bold=True, italic=True)
