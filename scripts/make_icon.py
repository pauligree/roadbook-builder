"""Draw the 1024px launcher icon: rounded tile, A4 page with an elevation
profile coloured blue -> purple -> red like the roadbook's grade scale."""
import sys

from PIL import Image, ImageDraw, ImageFilter

S = 1024
SS = 2  # supersample for smooth edges
W = S * SS


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def grade_color(t):
    blue, purple, red = (40, 70, 235), (140, 40, 160), (225, 45, 45)
    return lerp(blue, purple, t * 2) if t < 0.5 else lerp(purple, red, (t - 0.5) * 2)


img = Image.new("RGBA", (W, W), (0, 0, 0, 0))

# background tile: vertical gradient, macOS-style rounded square
bg = Image.new("RGBA", (W, W))
bd = ImageDraw.Draw(bg)
top, bottom = (36, 52, 92), (20, 28, 52)
for y in range(W):
    bd.line([(0, y), (W, y)], fill=lerp(top, bottom, y / W) + (255,))
mask = Image.new("L", (W, W), 0)
m = 100 * SS
ImageDraw.Draw(mask).rounded_rectangle([m, m, W - m, W - m], radius=185 * SS, fill=255)
img.paste(bg, (0, 0), mask)

# page with soft shadow
px0, py0, px1, py1 = 290 * SS, 190 * SS, 734 * SS, 818 * SS
shadow = Image.new("L", (W, W), 0)
ImageDraw.Draw(shadow).rounded_rectangle([px0 + 14 * SS, py0 + 22 * SS, px1 + 14 * SS, py1 + 22 * SS],
                                         radius=22 * SS, fill=130)
shadow = shadow.filter(ImageFilter.GaussianBlur(18 * SS))
img.paste(Image.new("RGBA", (W, W), (0, 0, 0, 255)), (0, 0), shadow)
d = ImageDraw.Draw(img)
d.rounded_rectangle([px0, py0, px1, py1], radius=22 * SS, fill=(250, 250, 247, 255))

# header lines
d.rounded_rectangle([px0 + 40 * SS, py0 + 44 * SS, px0 + 300 * SS, py0 + 70 * SS], radius=13 * SS, fill=(26, 26, 46, 255))
d.rounded_rectangle([px0 + 40 * SS, py0 + 92 * SS, px0 + 220 * SS, py0 + 108 * SS], radius=8 * SS, fill=(150, 155, 170, 255))

# map panel
mx0, my0, mx1, my1 = px0 + 40 * SS, py0 + 140 * SS, px1 - 40 * SS, py0 + 430 * SS
mw, mh = mx1 - mx0, my1 - my0
panel = Image.new("RGBA", (mw, mh), (224, 233, 214, 255))
pd = ImageDraw.Draw(panel)
for i in range(1, 5):  # contour-ish rings, clipped to the panel
    r = i * 70 * SS
    cx, cy = 250 * SS, 170 * SS
    pd.ellipse([cx - r, cy - r * 0.7, cx + r, cy + r * 0.7], outline=(190, 205, 178, 255), width=4 * SS)
pmask = Image.new("L", (mw, mh), 0)
ImageDraw.Draw(pmask).rounded_rectangle([0, 0, mw - 1, mh - 1], radius=10 * SS, fill=255)
img.paste(panel, (mx0, my0), pmask)

# track on the map
track = [(0.05, 0.85), (0.2, 0.72), (0.3, 0.45), (0.45, 0.35), (0.55, 0.55), (0.7, 0.3), (0.85, 0.2), (0.95, 0.12)]
pts = [(mx0 + x * (mx1 - mx0), my0 + y * (my1 - my0)) for x, y in track]
d.line(pts, fill=(26, 26, 26, 255), width=22 * SS, joint="curve")
for i in range(len(pts) - 1):
    d.line([pts[i], pts[i + 1]], fill=grade_color(i / (len(pts) - 2)) + (255,), width=13 * SS)
for (x, y), c in ((pts[0], (27, 138, 58)), (pts[-1], (192, 57, 43))):
    r = 20 * SS
    d.ellipse([x - r, y - r, x + r, y + r], fill=c + (255,), outline=(255, 255, 255, 255), width=6 * SS)

# elevation profile
ex0, ey0, ex1, ey1 = px0 + 40 * SS, py0 + 470 * SS, px1 - 40 * SS, py1 - 40 * SS
prof = [0.75, 0.6, 0.35, 0.15, 0.4, 0.55, 0.3, 0.1, 0.45, 0.7, 0.5]
ppts = [(ex0 + i / (len(prof) - 1) * (ex1 - ex0), ey0 + v * (ey1 - ey0)) for i, v in enumerate(prof)]
d.polygon(ppts + [(ex1, ey1), (ex0, ey1)], fill=(232, 234, 240, 255))
for i in range(len(ppts) - 1):
    climbing = ppts[i + 1][1] < ppts[i][1]
    d.line([ppts[i], ppts[i + 1]], fill=((225, 45, 45) if climbing else (40, 70, 235)) + (255,), width=11 * SS)
d.line([(ex0, ey1), (ex1, ey1)], fill=(60, 60, 70, 255), width=4 * SS)

img.resize((S, S), Image.LANCZOS).save(sys.argv[1])
