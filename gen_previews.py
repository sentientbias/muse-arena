"""Generate 5 game preview thumbnails (480x300) for the landing game cards."""
from PIL import Image, ImageDraw, ImageFont
import os

OUT = os.path.expanduser("~/workspace/muse-arena/assets")
os.makedirs(OUT, exist_ok=True)
W, H = 480, 300

NAVY = (20, 29, 51); NAVY2 = (13, 20, 38); FELT = (14, 42, 34)
GOLD = (245, 179, 36); GOLD_LT = (255, 217, 122); CYAN = (34, 211, 238)
RED = (220, 60, 60); WHITE = (245, 242, 235); DARK = (10, 16, 32)

def font(px, bold=True):
    p = "/usr/share/fonts/truetype/dejavu/DejaVuSans%s.ttf" % ("-Bold" if bold else "")
    return ImageFont.truetype(p, px)

def base(color=NAVY2):
    return Image.new("RGB", (W, H), color)

def card(d, x, y, w, h, rank, suit, face_down=False):
    d.rounded_rectangle([x, y, x + w, y + h], 8, fill=(30, 40, 66) if face_down else WHITE,
                        outline=GOLD if not face_down else (60, 70, 95), width=2)
    if face_down:
        d.rounded_rectangle([x + 8, y + 8, x + w - 8, y + h - 8], 5, outline=(70, 82, 110), width=2)
        return
    col = RED if suit in "♥♦" else DARK
    f = font(20); d.text((x + 7, y + 5), rank, font=f, fill=col)
    d.text((x + 7, y + 28), suit, font=font(22), fill=col)
    d.text((x + w / 2, y + h / 2 - 8), suit, font=font(34), fill=col, anchor="mm")

def chip(d, cx, cy, r, col=GOLD):
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col, outline=(120, 60, 8), width=2)
    d.ellipse([cx - r * .62, cy - r * .62, cx + r * .62, cy + r * .62], outline=WHITE, width=2)

def save(im, n):
    p = os.path.join(OUT, n); im.save(p); print("wrote", p)

# ---------- 1. checkers ----------
im = base(); d = ImageDraw.Draw(im)
bs, ox, oy = 30, (W - 8 * 30) // 2, (H - 8 * 30) // 2
for r in range(8):
    for c in range(8):
        col = (42, 54, 84) if (r + c) % 2 == 0 else (196, 158, 74)
        d.rectangle([ox + c * bs, oy + r * bs, ox + (c + 1) * bs, oy + (r + 1) * bs], fill=col)
setup = [("R", r, c) for r in range(3) for c in range(8) if (r + c) % 2 == 1] + \
        [("G", r, c) for r in range(5, 8) for c in range(8) if (r + c) % 2 == 1]
for col, r, c in setup:
    if (col, r, c) in [("R", 1, 2), ("R", 0, 1), ("G", 6, 5)]:  # a few captured
        continue
    cx, cy = ox + c * bs + bs / 2, oy + r * bs + bs / 2
    d.ellipse([cx - 11, cy - 11, cx + 11, cy + 11], fill=RED if col == "R" else GOLD,
              outline=(255, 255, 255, 120), width=2)
    d.ellipse([cx - 6, cy - 6, cx + 6, cy + 6], outline=(0, 0, 0, 60), width=1)
save(im, "prev-checkers.png")

# ---------- 2. connect four ----------
im = base(); d = ImageDraw.Draw(im)
cols, rows, s = 7, 6, 34
ox, oy = (W - cols * s) // 2, (H - rows * s) // 2 + 6
d.rounded_rectangle([ox - 8, oy - 8, ox + cols * s + 8, oy + rows * s + 8], 14, fill=(28, 40, 72),
                    outline=GOLD, width=3)
grid = [[""] * cols for _ in range(rows)]
moves = [(5, 3, "G"), (5, 2, "C"), (4, 3, "G"), (5, 4, "C"), (3, 3, "G"),
         (4, 4, "C"), (5, 5, "G"), (2, 3, "G")]
for r, c, v in moves: grid[r][c] = v
for r in range(rows):
    for c in range(cols):
        cx, cy = ox + c * s + s / 2, oy + r * s + s / 2
        v = grid[r][c]
        d.ellipse([cx - 13, cy - 13, cx + 13, cy + 13],
                  fill=GOLD if v == "G" else CYAN if v == "C" else NAVY2,
                  outline=(90, 100, 130) if not v else None, width=2)
save(im, "prev-connect4.png")

# ---------- 3. tic-tac-toe ----------
im = base(); d = ImageDraw.Draw(im)
s, ox, oy = 84, (W - 3 * 84) // 2, (H - 3 * 84) // 2
for i in (1, 2):
    d.line([(ox + i * s, oy + 6), (ox + i * s, oy + 3 * s - 6)], fill=(70, 84, 116), width=5)
    d.line([(ox + 6, oy + i * s), (ox + 3 * s - 6, oy + i * s)], fill=(70, 84, 116), width=5)
board = ["X", "O", "X", "O", "X", "", "", "", "O"]
f = font(52)
for i, v in enumerate(board):
    if not v: continue
    cx, cy = ox + (i % 3) * s + s / 2, oy + (i // 3) * s + s / 2
    if v == "X": d.text((cx, cy), "✕", font=f, fill=GOLD, anchor="mm")
    else: d.ellipse([cx - 24, cy - 24, cx + 24, cy + 24], outline=CYAN, width=8)
save(im, "prev-tictactoe.png")

# ---------- 4. poker ----------
im = base(FELT); d = ImageDraw.Draw(im)
d.ellipse([W/2-260, -140, W/2+260, 200], outline=(24, 62, 50), width=3)
d.text((W/2, 26), "POT  240", font=font(26), fill=GOLD, anchor="mm")
cw, chh, gap = 62, 86, 10
tx = W/2 - (5 * cw + 4 * gap) / 2
for i, (rk, st) in enumerate([("A", "♠"), ("K", "♥"), ("Q", "♦"), ("J", "♣"), ("10", "♠")]):
    card(d, tx + i * (cw + gap), 70, cw, chh, rk, st)
card(d, W/2 - 110, 190, cw, chh, "A", "♥"); card(d, W/2 - 40, 190, cw, chh, "K", "♦")
chip(d, W - 90, 230, 26); chip(d, W - 90, 196, 26); chip(d, 90, 230, 26, CYAN)
d.text((W/2 - 75, 290), "your hand", font=font(16), fill=(160, 190, 175), anchor="mm")
save(im, "prev-poker.png")

# ---------- 5. blackjack ----------
im = base(FELT); d = ImageDraw.Draw(im)
d.ellipse([W/2-260, -140, W/2+260, 200], outline=(24, 62, 50), width=3)
d.text((W/2, 26), "DEALER", font=font(18), fill=(160, 190, 175), anchor="mm")
card(d, W/2 - 70, 56, 62, 86, "K", "♠"); card(d, W/2 + 8, 56, 62, 86, "", "", face_down=True)
d.text((W/2, 168), "YOU · 21", font=font(22), fill=GOLD, anchor="mm")
card(d, W/2 - 70, 196, 62, 86, "A", "♥"); card(d, W/2 + 8, 196, 62, 86, "10", "♦")
chip(d, 80, 240, 24); chip(d, W - 80, 240, 24, CYAN)
d.text((W/2, 282), "BLACKJACK PAYS 3:2", font=font(15), fill=(160, 190, 175), anchor="mm")
save(im, "prev-blackjack.png")
print("done")
