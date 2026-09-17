"""Render 3 casino-chip logo variants with a perfectly centered M (PIL anchor='mm')."""
from PIL import Image, ImageDraw, ImageFont
import math, os

OUT = os.path.expanduser("~/workspace/muse-arena/logo_previews")
os.makedirs(OUT, exist_ok=True)

S = 480
C = S // 2
NAVY = (20, 29, 51)
GOLD = (245, 179, 36)
GOLD_LT = (255, 233, 168)
GOLD_DK = (180, 83, 9)
WHITE = (253, 246, 227)
CYAN = (34, 211, 238)

def font(px):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"):
        if os.path.exists(p):
            return ImageFont.truetype(p, px)
    return ImageFont.load_default()

def gold_disc(d, r, glow=True):
    """Vertical gold gradient disc."""
    for y in range(C - r, C + r):
        t = (y - (C - r)) / (2 * r)
        col = tuple(int(GOLD_LT[i] + (GOLD_DK[i] - GOLD_LT[i]) * t) for i in range(3))
        d.line([(C - r, y), (C + r, y)], fill=col)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).ellipse([C - r, C - r, C + r, C + r], fill=255)
    return mask

def apply_mask(img, mask):
    img.putalpha(mask)
    return img

def edge_spots(d, r, n, color, w):
    for i in range(n):
        a = 2 * math.pi * i / n - math.pi / 2
        x1 = C + (r - w) * math.cos(a); y1 = C + (r - w) * math.sin(a)
        x2 = C + r * math.cos(a); y2 = C + r * math.sin(a)
        d.line([(x1, y1), (x2, y2)], fill=color, width=w // 2, joint="curve")

def centered_m(d, ch_r, m_color, px):
    f = font(px)
    d.text((C, C), "M", font=f, fill=m_color, anchor="mm")

def base_canvas():
    return Image.new("RGBA", (S, S), NAVY + (255,))

def save(img, name):
    p = os.path.join(OUT, name)
    img.save(p)
    print("wrote", p)

R = 200

# ---- Variant A: classic refined — gold chip, white edge spots, navy center, gold M
img = base_canvas(); d = ImageDraw.Draw(img)
mask = gold_disc(d, R)
apply_mask(img, mask)
d = ImageDraw.Draw(img)
d.ellipse([C - R, C - R, C + R, C + R], outline=(120, 60, 8), width=3)
edge_spots(d, R - 6, 8, WHITE, 44)
d.ellipse([C - 128, C - 128, C + 128, C + 128], fill=NAVY)
d.ellipse([C - 128, C - 128, C + 128, C + 128], outline=GOLD, width=6)
centered_m(d, 128, GOLD, 150)
save(img, "chip_A_classic.png")

# ---- Variant B: knockout — gold chip, white spots, GOLD center, navy M
img = base_canvas(); d = ImageDraw.Draw(img)
mask = gold_disc(d, R)
apply_mask(img, mask)
d = ImageDraw.Draw(img)
d.ellipse([C - R, C - R, C + R, C + R], outline=(120, 60, 8), width=3)
edge_spots(d, R - 6, 8, WHITE, 44)
d.ellipse([C - 128, C - 128, C + 128, C + 128], fill=GOLD)
d.ellipse([C - 128, C - 128, C + 128, C + 128], outline=NAVY, width=6)
centered_m(d, 128, NAVY, 150)
save(img, "chip_B_knockout.png")

# ---- Variant C: midnight — navy chip, gold spots, gold rim, gold M
img = base_canvas(); d = ImageDraw.Draw(img)
d.ellipse([C - R, C - R, C + R, C + R], fill=(16, 24, 44))
d.ellipse([C - R, C - R, C + R, C + R], outline=GOLD_DK, width=3)
edge_spots(d, R - 6, 8, GOLD, 44)
d.ellipse([C - 128, C - 128, C + 128, C + 128], fill=(10, 16, 32))
d.ellipse([C - 128, C - 128, C + 128, C + 128], outline=GOLD, width=6)
centered_m(d, 128, GOLD, 150)
# cyan sparkle accent
d.ellipse([C + 120, C - 160, C + 150, C - 130], fill=CYAN)
save(img, "chip_C_midnight.png")

print("done")
