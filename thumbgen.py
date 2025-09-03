# thumbgen.py
# 캐릭터 캐리커처(빅헤드) 스타일 썸네일 생성 - 비용 0원 (Pillow only)

from PIL import Image, ImageDraw, ImageFont
import textwrap, math, random

def _to_rgb(hex_or_tuple):
    if isinstance(hex_or_tuple, tuple):
        return hex_or_tuple
    s = hex_or_tuple.lstrip("#")
    return tuple(int(s[i:i+2], 16) for i in (0, 2, 4))

def _vgrad(size, top="#f5f7fa", bottom="#e6eeff"):
    w, h = size
    top = _to_rgb(top); bottom = _to_rgb(bottom)
    img = Image.new("RGB", size, top)
    drw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(1, h - 1)
        r = int(top[0]*(1-t) + bottom[0]*t)
        g = int(top[1]*(1-t) + bottom[1]*t)
        b = int(top[2]*(1-t) + bottom[2]*t)
        drw.line([(0, y), (w, y)], fill=(r, g, b))
    return img

def _load_font(size=28):
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except Exception:
        return ImageFont.load_default()

def _wrap_text(draw, text, font, max_width):
    words = text.split()
    line, lines = "", []
    for w in words:
        test = (line + " " + w).strip()
        if draw.textlength(test, font=font) <= max_width:
            line = test
        else:
            if line: lines.append(line)
            line = w
    if line: lines.append(line)
    return "\n".join(lines)

def _draw_badge(d, xy, text, fill="#1f2937", fg="#ffffff", r=14, pad=10, font=None):
    x, y = xy
    font = font or _load_font(26)
    tw = d.textlength(text, font=font)
    w = int(tw + pad*2); h = int(font.size + pad*1.2)
    d.rounded_rectangle((x, y, x+w, y+h), radius=r, fill=_to_rgb(fill))
    d.text((x+pad, y+(h-font.size)//2 - 1), text, fill=_to_rgb(fg), font=font)

def _draw_caricature(d, cx, cy, scale=1.0, skin=("#ffddc1"), hair=("#0f172a"), shirt=("#2563eb")):
    skin = _to_rgb(skin); hair = _to_rgb(hair); shirt = _to_rgb(shirt)
    hw, hh = int(140*scale), int(170*scale)
    d.ellipse((cx-hw, cy-hh-30*scale, cx+hw, cy+hh-30*scale), fill=skin)
    d.pieslice((cx-hw, cy-hh-50*scale, cx+hw, cy+hh-90*scale), start=0, end=180, fill=hair)
    d.polygon([(cx-hw*0.8, cy-hh*0.8), (cx, cy-hh*1.0), (cx+hw*0.7, cy-hh*0.85)], fill=hair)
    eye_r = max(6, int(8*scale)); dx = int(45*scale)
    d.ellipse((cx-dx-eye_r, cy-20-eye_r, cx-dx+eye_r, cy-20+eye_r), fill=(30,30,30))
    d.ellipse((cx+dx-eye_r, cy-20-eye_r, cx+dx+eye_r, cy-20+eye_r), fill=(30,30,30))
    blush = (255, 170, 170)
    d.ellipse((cx-dx-18, cy+5, cx-dx+18, cy+25), fill=blush)
    d.ellipse((cx+dx-18, cy+5, cx+dx+18, cy+25), fill=blush)
    mouth_w = int(80*scale)
    d.arc((cx-mouth_w//2, cy+25, cx+mouth_w//2, cy+55), start=0, end=180, fill=(120,60,60), width=3)
    d.rectangle((cx-18, cy+60, cx+18, cy+80), fill=skin)
    bw, bh = int(160*scale), int(140*scale)
    d.rounded_rectangle((cx-bw, cy+80, cx+bw, cy+80+bh), radius=18, fill=shirt)

def make_thumb(title, cat="정보", size=(768,768), out="thumb.webp", quality=75, accent="#2563eb"):
    img = _vgrad(size, top="#f6f9ff", bottom="#e8f0ff")
    d = ImageDraw.Draw(img)
    cx, cy = size[0]//2, int(size[1]*0.46)
    palette = [
        ("#ffddc1", "#111827", "#2563eb"),
        ("#ffd7b3", "#0f172a", "#16a34a"),
        ("#ffe1c9", "#111827", "#dc2626"),
        ("#ffd9c0", "#1f2937", "#7c3aed"),
    ]
    skin, hair, shirt = random.choice(palette)
    _draw_caricature(d, cx, cy, scale=1.0, skin=skin, hair=hair, shirt=shirt)
    _draw_badge(d, (24, 24), cat, fill="#0f172a", fg="#ffffff", r=14, pad=12, font=_load_font(26))
    pad = 28
    title_font = _load_font(34)
    maxw = size[0] - pad*2
    wrapped = _wrap_text(d, title, title_font, maxw)
    tw, th = d.multiline_textbbox((0,0), wrapped, font=title_font, spacing=6)[2:]
    bg_h = th + pad
    box = (pad-6, size[1]-bg_h-24, size[0]-pad+6, size[1]-24)
    d.rounded_rectangle(box, radius=16, fill=(255,255,255,240))
    d.multiline_text((pad, size[1]-bg_h-10), wrapped, fill=(15,23,42), font=title_font, spacing=6)
    img.save(out, "WEBP", quality=quality)
    return out
