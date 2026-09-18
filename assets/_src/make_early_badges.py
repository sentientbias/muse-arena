#!/usr/bin/env python3
"""Post-process the 20 early-badge generations into assets/badge-<slug>.png:
256x256 RGBA, background keyed to transparent via border-connected flood fill
on background-like pixels (numpy BFS, no PIL floodfill threshold pitfalls)."""
import glob, os
from collections import deque
import numpy as np
from PIL import Image

SRC = os.path.expanduser("~/workspace/muse-arena/assets/_src")
DST = os.path.expanduser("~/workspace/muse-arena/assets")

SLUGS = [
    "early-first-game", "early-day-one", "early-first-100",
    "early-founding-week", "volume-10", "volume-25", "volume-50",
    "volume-100", "volume-250", "streak-3w", "streak-5w", "streak-10w",
    "grind-day-max", "grind-night-owl", "grind-early-bird",
    "grind-weekend", "milestone-first-win", "milestone-first-tourney",
    "milestone-first-stake", "milestone-comeback",
]

def find_src(n):
    pats = [p for p in glob.glob(
        os.path.join(SRC, "media-generation-early-badge-%d-*.png" % n))
        if not p.endswith(".json")]
    assert len(pats) == 1, (n, pats)
    return pats[0]

def key_background(im):
    """Return RGBA with border-connected background-like pixels transparent."""
    rgb = np.array(im.convert("RGB")).astype(np.int16)
    h, w, _ = rgb.shape
    corners = np.stack([rgb[0, 0], rgb[0, w-1], rgb[h-1, 0], rgb[h-1, w-1]])
    bg = np.median(corners, axis=0)
    dist = np.abs(rgb - bg).sum(axis=2)
    bglike = dist < 90  # per-pixel L1 distance from background color
    seen = np.zeros((h, w), dtype=bool)
    dq = deque()
    for x in range(w):
        for y in (0, h - 1):
            if bglike[y, x]:
                dq.append((y, x)); seen[y, x] = True
    for y in range(h):
        for x in (0, w - 1):
            if bglike[y, x] and not seen[y, x]:
                dq.append((y, x)); seen[y, x] = True
    while dq:
        y, x = dq.popleft()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ny, nx = y + dy, x + dx
            if 0 <= ny < h and 0 <= nx < w and not seen[ny, nx] \
                    and bglike[ny, nx]:
                seen[ny, nx] = True
                dq.append((ny, nx))
    rgba = im.convert("RGBA")
    alpha = np.array(rgba)[:, :, 3].copy()
    alpha[seen] = 0
    rgba.putalpha(Image.fromarray(alpha))
    return rgba, seen.sum()

for n, slug in enumerate(SLUGS, 1):
    im = Image.open(find_src(n)).convert("RGB").resize((256, 256), Image.LANCZOS)
    rgba, ntrans = key_background(im)
    out = os.path.join(DST, "badge-%s.png" % slug)
    rgba.save(out)
    opaque = 65536 - ntrans
    print("%-32s opaque=%d transparent=%d" % (os.path.basename(out),
          opaque, ntrans))
    # badge should occupy a real chunk of the square, not everything/nothing
    assert 8000 < opaque < 60000, "suspicious keying for " + slug
print("OK: 20 badges written")
