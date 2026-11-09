import sys, json, numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter
MASCOT = {
    "right_eye": [(318, 418), (452, 410), (462, 470), (444, 498), (326, 496), (314, 458)],
    "right_lid": [(326, 452), (350, 466), (388, 474), (424, 468), (452, 452)],
    "left_eye": [(213, 446), (254, 444), (257, 516), (215, 518)],
    "left_lid": [(215, 484), (233, 490), (254, 484)],
    "mouth_open": (246, 622, 292, 648),
}  # copied from hermes-gadget-sdk face.py (mascot profile)
def mascot_frames(master):
    alpha = master.getchannel("A")
    blink = alpha.copy(); draw = ImageDraw.Draw(blink)
    draw.polygon(MASCOT["right_eye"], fill=255); draw.polygon(MASCOT["left_eye"], fill=255)
    draw.line(MASCOT["right_lid"], fill=0, width=14, joint="curve"); draw.line(MASCOT["left_lid"], fill=0, width=12, joint="curve")
    talk = alpha.copy(); ImageDraw.Draw(talk).ellipse(MASCOT["mouth_open"], fill=0)
    return {"idle": alpha, "blink": blink, "talk": talk}
import os
# Source: Hermes Agent assets/nous-girl-white rendered to a 1024 px PNG (MIT, see NOTICE).
master = Image.open(os.environ.get('NOUS_GIRL_PNG', 'nous-girl-white-1024.png')).convert('RGBA')
fr = mascot_frames(master)
# extra frames: wink (far/right eye only closed), smile-ish = talk small? keep official ones + wink
a = master.getchannel('A')
wink = a.copy(); d = ImageDraw.Draw(wink)
d.polygon(MASCOT['right_eye'], fill=255); d.line(MASCOT['right_lid'], fill=0, width=14, joint='curve')
fr['wink'] = wink
DENSE_RAMP = '%#@'
EK = float(sys.argv[2]) if len(sys.argv) > 2 else 3
COLS = int(sys.argv[1]) if len(sys.argv) > 1 else 80
CW, CH = 8, 16  # glyph sampling cell (aspect 1:2)
bbox = a.getbbox()
FONT = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf', 14)
CHARS = [chr(c) for c in range(32, 127) if chr(c) not in '`\\']
# 3x2 grid of sampling circles => 6-d shape vector
def vec(img):  # img float array CHxCW in [0,1]
    v = []
    for gy in range(3):
        for gx in range(2):
            v.append(img[gy*CH//3:(gy+1)*CH//3, gx*CW//2:(gx+1)*CW//2].mean())
    return np.array(v)
G = []
for ch in CHARS:
    im = Image.new('L', (CW, CH), 0); ImageDraw.Draw(im).text((0, -1), ch, fill=255, font=FONT)
    G.append(vec(np.asarray(im, float)/255))
G = np.array(G); G = G / max(G.max(), 1e-6)
def ink(alpha):
    # silhouette = everything not reachable from the corners through transparent pixels
    a = alpha.point(lambda v: 255 if v > 110 else 0)
    flood = a.copy()
    for xy in ((0, 0), (1023, 0), (0, 1023), (1023, 1023)):
        if flood.getpixel(xy) == 0: ImageDraw.floodfill(flood, xy, 128)
    f = np.asarray(flood); al = np.asarray(alpha, float)
    inside = f != 128
    out = np.where(inside, 255 - al, 0)          # ink = dark pixels inside her
    out = np.maximum(out, np.where(inside, 0, 0))
    return Image.fromarray(out.astype('uint8')), Image.fromarray(np.where(inside, al, 0).astype('uint8'))
def ascii_of(alpha):
    alpha, light = ink(alpha)
    src = alpha.crop(bbox)
    w, h = src.size
    rows = round(COLS * h / w * CW / CH)
    big = src.resize((COLS*CW, rows*CH), Image.LANCZOS)
    fill = np.asarray(big, float)/255
    arr = np.asarray(big.filter(ImageFilter.GaussianBlur(0.5)), float)/255
    out = []
    for r in range(rows):
        line = ''
        for c in range(COLS):
            v = vec(arr[r*CH:(r+1)*CH, c*CW:(c+1)*CW])
            f = v.mean()
            if f > 0.82: line += DENSE_RAMP[min(len(DENSE_RAMP)-1, int((f-0.82)/0.18*len(DENSE_RAMP)))]; continue
            if v.max() < 0.12: line += ' '; continue
            line += CHARS[int(((G - np.clip(v*0.9,0,1))**2).sum(1).argmin())]
        out.append(line.rstrip())
    return out
res = {k: ascii_of(fr[k]) for k in ('idle', 'blink', 'wink', 'talk')}
base = res['idle']
patch = {k: [[i, l] for i, l in enumerate(v) if l != base[i]] for k, v in res.items() if k != 'idle'}
json.dump({'base': base, 'patch': patch}, open(f'nous{COLS}.json', 'w'), ensure_ascii=False)
print(COLS, 'x', len(base), {k: len(v) for k, v in patch.items()})
# preview render
f = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf', 12)
cw = f.getlength('M'); lh = 14
im = Image.new('RGB', (int(cw*COLS)+20, lh*len(base)+20), (5, 7, 10)); d = ImageDraw.Draw(im)
for i, l in enumerate(base): d.text((10, 10+i*lh), l, fill=(230, 235, 240), font=f)
im.save(f'prev{COLS}.png')
