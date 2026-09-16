"""SVG read support for Pillow, powered by LunaSVG. Importing registers the plugin."""

from __future__ import annotations

import math
import os

from PIL import Image, ImageFile

from . import _lunasvg

__version__ = "0.1.0"
lunasvg_version: str = _lunasvg.lunasvg_version

__all__ = ["SvgImageFile", "__version__", "add_font_file", "lunasvg_version"]


def _accept(prefix: bytes) -> bool:
    if prefix.startswith(b"\xef\xbb\xbf"):
        prefix = prefix[3:]
    prefix = prefix.lstrip(b" \t\r\n")
    return (
        not prefix
        or prefix.startswith((b"<?xml", b"<svg", b"<!--"))
        or prefix[:9].upper() == b"<!DOCTYPE"
    )


class SvgImageFile(ImageFile.ImageFile):
    format = "SVG"
    format_description = "Scalable Vector Graphics"

    def _open(self) -> None:
        assert self.fp is not None
        doc = _lunasvg.load(self.fp.read())
        if doc is None:
            msg = "not an SVG file"
            raise SyntaxError(msg)
        if doc.width <= 0 or doc.height <= 0:
            msg = "SVG has no intrinsic size: add width/height or viewBox to the <svg> element"
            raise OSError(msg)
        self._doc: _lunasvg.Document | None = doc
        self._mode = "RGBA"
        self._size = _scaled_size(doc, 1.0)
        self.tile = []

    def load(self, scale: float = 1.0) -> Image.core.PixelAccess | None:  # type: ignore[name-defined]
        """Render the SVG. Only the first call renders; ``scale`` multiplies the intrinsic size."""
        if not (math.isfinite(scale) and scale > 0):
            msg = f"scale must be a finite number > 0, got {scale!r}"
            raise ValueError(msg)
        doc = self._doc
        if doc is not None:
            size = _scaled_size(doc, scale)
            Image._decompression_bomb_check(size)
            data = doc.render(size[0], size[1], scale)
            self._doc = None
            self.im = Image.core.new("RGBA", size)
            self._size = size
            self.frombytes(data)
            if self._exclusive_fp and self._close_exclusive_fp_after_loading and self.fp:
                self.fp.close()
            self.fp = None
        return Image.Image.load(self)


def _scaled_size(doc: _lunasvg.Document, scale: float) -> tuple[int, int]:
    return max(1, math.ceil(doc.width * scale)), max(1, math.ceil(doc.height * scale))


def add_font_file(
    path: str | os.PathLike[str], family: str = "", *, bold: bool = False, italic: bool = False
) -> None:
    """Register a TrueType/OpenType font for <text>. An empty family makes it a fallback."""
    path = os.fspath(path)
    if not _lunasvg.add_font_file(path, family, bold, italic):
        msg = f"cannot load font file {path!r}"
        raise OSError(msg)


Image.register_open(SvgImageFile.format, SvgImageFile, _accept)
Image.register_extension(SvgImageFile.format, ".svg")
Image.register_mime(SvgImageFile.format, "image/svg+xml")
