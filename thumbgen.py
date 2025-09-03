# thumbgen.py
from PIL import Image, ImageDraw
import textwrap, os

def make_thumb(title, cat="정보", size=(768,768), out="thumb.webp", quality=75):
    img = Image.new("RGB", size, (245,247,250))
    d = ImageDraw.Draw(img)
    # 카테고리 배지
    d.rounded_rectangle((24,24,200,72), radius=14, fill=(30,30,30))
    d.text((40,36), cat, fill=(255,255,255))
    # 제목 (간단 래핑)
    wrapped = textwrap.fill(title, width=14)
    d.text((40,120), wrapped, fill=(30,30,30), spacing=6)
    img.save(out, "WEBP", quality=quality)
    return out
