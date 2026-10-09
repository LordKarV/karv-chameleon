"""Contact sheet of PNGs: python scripts/sheet.py out.png a.png b.png ..."""
import sys
from PIL import Image

out, paths = sys.argv[1], sys.argv[2:]
ims = []
for p in paths:
    im = Image.open(p).convert("RGB")
    s = min(560 / im.width, 420 / im.height)
    ims.append(im.resize((max(1, int(im.width * s)), max(1, int(im.height * s)))))
cols = 3
rows = [ims[i:i + cols] for i in range(0, len(ims), cols)]
W = max(sum(i.width for i in r) + 12 * len(r) for r in rows)
H = sum(max(i.height for i in r) + 12 for r in rows)
sheet = Image.new("RGB", (W, H), "white")
y = 0
for r in rows:
    x = 0
    for i in r:
        sheet.paste(i, (x, y)); x += i.width + 12
    y += max(i.height for i in r) + 12
sheet.save(out)
