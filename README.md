# pillow-lunasvg

SVG read support for [Pillow](https://python-pillow.org), powered by
[LunaSVG](https://github.com/sammycage/lunasvg).

```python
import pillow_lunasvg  # registers the plugin
from PIL import Image

im = Image.open("logo.svg")        # RGBA, intrinsic size
im.load(scale=4)                    # optional: crisp render at 4x, before any other use
im.save("logo.png")
```

Work in progress: not released yet.
