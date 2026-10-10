#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
旅行每日 Vlog 生成器：把某一天拍的照片剪成一条 720p 小视频（Day 1、Day 2 …）。

素材来源（按拍摄时间合并、去重、排序）：
  1. SD 卡里的 JPG（默认 /Volumes/PP02/DCIM），RAW 一律不读
  2. Mac「照片」图库里同一天的 JPG（通过 osxphotos 读取并导出）

风格：
  · 片头卡：模糊底图 + DAY N + 日期星期 + 副标题
  · 每张照片缓慢推拉（Ken Burns），照片之间叠化转场
  · 竖图放在相纸边框里，背景是同一张照片的模糊放大
  · 轻微暖调胶片色 + 暗角 + 右下角橙色胶片日期戳
  · 片尾卡 + 淡出到黑
  · 配乐：可以用 --music 指定自己的音乐；不指定时现场合成一段免版权的
    轻氛围钢琴/铺底（每天和弦走向不同）

用法（在 Mac 终端里）：
  python3 make_day_video.py --day 1                       # 做 10 月 1 日
  python3 make_day_video.py --day 1 --title "川渝 · 成都"   # 自定义副标题
  python3 make_day_video.py --day 1 --list                # 只列出会用到哪些照片
  python3 make_day_video.py --all                         # 一次做 9 天
"""

import argparse
import datetime as dt
import glob
import math
import os
import shutil
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

try:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
except ImportError:
    sys.exit("缺少依赖，请先运行：pip3 install pillow numpy osxphotos")

W, H, FPS = 1280, 720, 30          # 输出 720p
BW, BH = 1440, 810                 # 推拉用的底图，比 720p 稍大，推近后依然清晰
XFADE = 18                         # 叠化帧数（0.6 秒）
JPG_EXT = {".jpg", ".jpeg"}
MUSIC_EXT = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".aiff", ".aif"}
WEEKDAYS = "一二三四五六日"

LATIN_FONTS = [
    "/System/Library/Fonts/Supplemental/Futura.ttc",
    "/System/Library/Fonts/Avenir Next.ttc",
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
CJK_FONTS = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
]
MONO_FONTS = [
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Supplemental/Courier New Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
]


# ───────────────────────────── 素材收集 ─────────────────────────────

@dataclass
class Item:
    path: Path
    taken: dt.datetime      # 拍摄时间（当地时间，不带时区）
    source: str             # "SD卡" / "照片图库"
    name: str               # 原始文件名，用于去重


def exif_datetime(path):
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            sub = exif.get_ifd(0x8769)
            raw = sub.get(36867) or sub.get(36868) or exif.get(306)
            if raw:
                return dt.datetime.strptime(str(raw).strip()[:19], "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass
    return None


def scan_sd(sd_root, offset):
    """扫描整张 SD 卡里的 JPG，返回全部 Item（只扫一次，按天过滤由调用方做）。"""
    items, raw_skipped = [], 0
    for root, dirs, files in os.walk(sd_root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if name.startswith("."):          # 跳过 macOS 在 exFAT 上生成的 ._ 文件
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in JPG_EXT:
                raw_skipped += 1
                continue
            p = Path(root) / name
            t = exif_datetime(p) or dt.datetime.fromtimestamp(p.stat().st_mtime)
            items.append(Item(p, t + offset, "SD卡", name))
    return items, raw_skipped


_photos_db = None


def heic_to_jpg(src):
    dst = src.with_suffix(".jpg")
    subprocess.run(["sips", "-s", "format", "jpeg", str(src), "--out", str(dst)],
                   check=True, capture_output=True)
    return dst


def collect_library(day, dest, include_heic, icloud, library):
    """从 Mac「照片」图库导出某一天的 JPG。"""
    global _photos_db
    try:
        import osxphotos
    except ImportError:
        print("  ⚠️  没装 osxphotos，跳过照片图库（pip3 install osxphotos）")
        return []
    if _photos_db is None:
        print("  正在读取照片图库（第一次会慢一点）…")
        _photos_db = osxphotos.PhotosDB(dbfile=library) if library else osxphotos.PhotosDB()

    dest.mkdir(parents=True, exist_ok=True)
    out, missing, failed, non_jpg = [], 0, 0, 0
    for p in _photos_db.photos(images=True, movies=False):
        if p.date is None:
            continue
        taken = p.date.replace(tzinfo=None)       # 拍摄地当地时间
        if taken.date() != day or getattr(p, "screenshot", False):
            continue
        uti = (getattr(p, "uti_original", None) or p.uti or "").lower()
        suffix = Path(p.original_filename or "").suffix.lower()
        is_jpg = uti == "public.jpeg" or suffix in JPG_EXT
        is_heic = uti in ("public.heic", "public.heif") or suffix in (".heic", ".heif")
        if not (is_jpg or (include_heic and is_heic)):
            non_jpg += 1
            continue
        if p.ismissing and not icloud:
            missing += 1
            continue
        try:
            paths = p.export(str(dest), use_photos_export=bool(p.ismissing), increment=True)
        except Exception:
            paths = []
        if not paths:
            failed += 1
            continue
        fp = Path(paths[0])
        if fp.suffix.lower() not in JPG_EXT:
            fp = heic_to_jpg(fp)
        out.append(Item(fp, taken, "照片图库", p.original_filename or fp.name))
    if non_jpg:
        print(f"  照片图库：{non_jpg} 张非 JPG（HEIC/RAW 等）已跳过" +
              ("" if include_heic else "，想要 iPhone 的 HEIC 可以加 --include-heic"))
    if missing:
        print(f"  照片图库：{missing} 张原图只在 iCloud 上，已跳过（加 --icloud 会尝试下载）")
    if failed:
        print(f"  照片图库：{failed} 张导出失败，已跳过")
    return out


def merge_items(sd_items, lib_items):
    """合并两边素材；SD 卡里的照片如果已经导入过照片图库，只保留一份（SD 卡那份）。"""
    merged, dupes = list(sd_items), 0
    for li in lib_items:
        if any(li.name.upper() == si.name.upper() and abs((li.taken - si.taken).total_seconds()) < 180
               for si in sd_items):
            dupes += 1
            continue
        merged.append(li)
    merged.sort(key=lambda it: it.taken)
    return merged, dupes


# ───────────────────────────── 画面工具 ─────────────────────────────

def load_font(candidates, size):
    for c in candidates:
        try:
            return ImageFont.truetype(c, size)
        except Exception:
            continue
    if candidates is CJK_FONTS:      # 新版 macOS 的苹方在 AssetsV2 里
        for c in glob.glob("/System/Library/AssetsV2/**/PingFang*.ttc", recursive=True):
            try:
                return ImageFont.truetype(c, size)
            except Exception:
                continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def draw_spaced(draw, text, font, cy, spacing, fill, cx=W // 2):
    """以 (cx, cy) 为中心画带字间距的文字。"""
    widths = [font.getlength(ch) for ch in text]
    total = sum(widths) + spacing * (len(text) - 1)
    x = cx - total / 2
    for ch, w in zip(text, widths):
        draw.text((x, cy), ch, font=font, fill=fill, anchor="lm")
        x += w + spacing


def with_shadow(layer, radius=10, strength=170):
    """给透明文字层加一圈柔和阴影。"""
    alpha = layer.getchannel("A")
    shadow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    shadow.putalpha(alpha.filter(ImageFilter.GaussianBlur(radius)).point(lambda a: min(255, a * strength // 255)))
    return Image.alpha_composite(shadow, layer)


def film_grade(img):
    """轻微暖调 + 抬一点黑位，像胶片扫描。"""
    def curve(gain, lift):
        return [min(255, int(lift + (255 - lift) * (0.5 - 0.5 * math.cos(math.pi * (x / 255) ** 0.95)) ** 0.92 * gain))
                for x in range(256)]
    lut = curve(1.02, 10) + curve(1.0, 9) + curve(0.95, 12)
    return img.point(lut)


def vignette_mask():
    y, x = np.ogrid[0:H, 0:W]
    r = np.sqrt(((x - W / 2) / (W / 2)) ** 2 + ((y - H / 2) / (H / 2)) ** 2) / math.sqrt(2)
    v = 1 - 0.38 * np.clip((r - 0.45) / 0.55, 0, 1) ** 1.6
    return Image.fromarray((v * 255).astype(np.uint8), "L")


def load_photo(path):
    im = Image.open(path)
    im.draft("RGB", (BW * 2, BH * 2))         # JPEG 快速降采样解码，大图也很快
    im = ImageOps.exif_transpose(im)
    return im.convert("RGB")


def blurred_bg(im, darken=0.55):
    small = ImageOps.fit(im, (BW // 8, BH // 8), Image.BILINEAR)
    small = small.filter(ImageFilter.GaussianBlur(6))
    bg = small.resize((BW, BH), Image.BICUBIC)
    return Image.eval(bg, lambda v: int(v * darken))


def make_canvas(im):
    """横图直接裁成 16:9；竖图 / 方图放在相纸边框里，背景用模糊放大的自己。"""
    if im.width / im.height >= 1.3:
        return ImageOps.fit(im, (BW, BH), Image.LANCZOS), False
    canvas = blurred_bg(im)
    ph = int(BH * 0.86)
    pw = int(im.width * ph / im.height)
    photo = im.resize((pw, ph), Image.LANCZOS)
    border = 12
    framed = Image.new("RGB", (pw + border * 2, ph + border * 2), (244, 240, 232))
    framed.paste(photo, (border, border))
    x, y = (BW - framed.width) // 2, (BH - framed.height) // 2
    shadow = Image.new("L", (BW, BH), 0)
    ImageDraw.Draw(shadow).rectangle([x + 8, y + 12, x + framed.width + 8, y + framed.height + 12], fill=150)
    shadow = shadow.filter(ImageFilter.GaussianBlur(14))
    canvas = Image.composite(Image.new("RGB", (BW, BH), (0, 0, 0)), canvas, shadow)
    canvas.paste(framed, (x, y))
    return canvas, True


def date_stamp(day):
    """右下角的橙色胶片日期戳，例如 '26 10 01。"""
    font = load_font(MONO_FONTS, 26)
    text = day.strftime("'%y %m %d")
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    pos = (W - 44, H - 40)
    d.text(pos, text, font=font, fill=(255, 140, 40, 255), anchor="rs")
    glow = layer.filter(ImageFilter.GaussianBlur(3))
    layer = Image.alpha_composite(glow, layer)
    bbox = layer.getbbox()
    return layer.crop(bbox), bbox[:2]


# ───────────────────────────── 片段 ─────────────────────────────

class PhotoClip:
    def __init__(self, item, frames, rng, stamp):
        self.item, self.frames, self.stamp = item, frames, stamp
        self.base = None
        self.zoom_in = rng.random() < 0.6
        self.p0 = (rng.uniform(-0.8, 0.8), rng.uniform(-0.6, 0.6))
        self.p1 = (rng.uniform(-0.8, 0.8), rng.uniform(-0.6, 0.6))

    def _prepare(self):
        canvas, framed = make_canvas(load_photo(self.item.path))
        self.base = film_grade(canvas)
        lo, hi = (1.02, 1.06) if framed else (1.03, 1.12)
        self.z0, self.z1 = (lo, hi) if self.zoom_in else (hi, lo)

    def frame(self, i):
        if self.base is None:
            self._prepare()
        u = i / max(1, self.frames - 1)
        u = 0.75 * u + 0.25 * (0.5 - 0.5 * math.cos(math.pi * u))
        z = self.z0 + (self.z1 - self.z0) * u
        bw, bh = BW / z, BH / z
        px = self.p0[0] + (self.p1[0] - self.p0[0]) * u
        py = self.p0[1] + (self.p1[1] - self.p0[1]) * u
        x0 = (BW - bw) / 2 * (1 + px)
        y0 = (BH - bh) / 2 * (1 + py)
        im = self.base.transform((W, H), Image.EXTENT, (x0, y0, x0 + bw, y0 + bh), Image.BILINEAR)
        stamp, pos = self.stamp
        im.paste(stamp, pos, stamp)
        return im

    def release(self):
        self.base = None


class CardClip:
    """片头 / 片尾卡：模糊底图缓慢推进，文字淡入。"""

    def __init__(self, bg_path, text_layer, frames, fade_in_black=0, fade_out_black=0, text_delay=6):
        self.bg_path, self.text, self.frames = bg_path, text_layer, frames
        self.fade_in_black, self.fade_out_black, self.text_delay = fade_in_black, fade_out_black, text_delay
        self.base = None

    def frame(self, i):
        if self.base is None:
            bg = blurred_bg(load_photo(self.bg_path), darken=0.5) if self.bg_path else Image.new("RGB", (BW, BH), (24, 22, 20))
            self.base = film_grade(bg)
        z = 1.0 + 0.05 * i / max(1, self.frames - 1)
        bw, bh = BW / z, BH / z
        x0, y0 = (BW - bw) / 2, (BH - bh) / 2
        im = self.base.transform((W, H), Image.EXTENT, (x0, y0, x0 + bw, y0 + bh), Image.BILINEAR)
        k = min(1.0, max(0.0, (i - self.text_delay) / 24))
        if k > 0:
            alpha = self.text.getchannel("A").point(lambda a: int(a * k))
            im.paste(self.text, (0, 0), alpha)
        black = Image.new("RGB", (W, H), (0, 0, 0))
        if self.fade_in_black and i < self.fade_in_black:
            im = Image.blend(black, im, i / self.fade_in_black)
        if self.fade_out_black and i >= self.frames - self.fade_out_black:
            im = Image.blend(im, black, (i - (self.frames - self.fade_out_black) + 1) / self.fade_out_black)
        return im

    def release(self):
        self.base = None


def title_layer(day_num, day, subtitle):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cream = (246, 240, 228, 255)
    draw_spaced(d, f"{day:%Y.%m.%d}  周{WEEKDAYS[day.weekday()]}", load_font(CJK_FONTS, 24), 262, 5, cream)
    draw_spaced(d, f"DAY {day_num}", load_font(LATIN_FONTS, 148), 360, 16, cream)
    d.line([(W // 2 - 60, 458), (W // 2 + 60, 458)], fill=(255, 150, 60, 255), width=2)
    if subtitle:
        draw_spaced(d, subtitle, load_font(CJK_FONTS, 34), 500, 10, cream)
    return with_shadow(layer)


def end_layer(day_num, day, n_photos, is_last):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cream = (246, 240, 228, 255)
    draw_spaced(d, "下次见" if is_last else "明天见", load_font(CJK_FONTS, 64), 330, 24, cream)
    d.line([(W // 2 - 40, 392), (W // 2 + 40, 392)], fill=(255, 150, 60, 255), width=2)
    draw_spaced(d, f"DAY {day_num}  ·  {day:%Y.%m.%d}  ·  {n_photos} 个瞬间", load_font(CJK_FONTS, 22), 430, 4, cream)
    return with_shadow(layer)


# ───────────────────────────── 配乐 ─────────────────────────────

CHORDS = {   # 相对主音的半音，已做好转位，听起来不跳
    "I": [0, 4, 7, 11], "ii": [2, 5, 9, 12], "iii": [-1, 4, 7, 11],
    "IV": [0, 5, 9, 12], "V": [-1, 2, 7, 11], "vi": [-3, 0, 4, 7],
}
PROGRESSIONS = [
    ["I", "V", "vi", "IV"], ["vi", "IV", "I", "V"], ["I", "vi", "IV", "V"],
    ["IV", "V", "iii", "vi"], ["I", "iii", "IV", "IV"], ["vi", "V", "IV", "V"],
    ["I", "IV", "vi", "V"], ["IV", "I", "V", "vi"], ["I", "ii", "IV", "V"],
]
TONICS = [60, 62, 57, 64, 59, 61, 55, 58, 63]     # 每天换一个调


def synth_music(path, seconds, seed):
    """合成一段温柔的氛围配乐：铺底和弦 + 钢琴感琶音 + 低音，再加一点混响。"""
    sr = 44100
    rng = np.random.default_rng(seed)
    bpm = 72 + (seed % 3) * 6
    beat = 60 / bpm
    bar = beat * 4
    tonic = TONICS[seed % len(TONICS)]
    prog = PROGRESSIONS[seed % len(PROGRESSIONS)]
    n = int((seconds + 3) * sr)
    L, R = np.zeros(n), np.zeros(n)

    def hz(m):
        return 440.0 * 2 ** ((m - 69) / 12)

    def add(sig, start, pan=0.0):
        s = int(start * sr)
        if s >= n:
            return
        e = min(n, s + len(sig))
        L[s:e] += sig[: e - s] * (1 - pan) * 0.5 * 1.4
        R[s:e] += sig[: e - s] * (1 + pan) * 0.5 * 1.4

    bars = int(seconds / bar) + 2
    for b in range(bars):
        chord = CHORDS[prog[b % 4]]
        t0 = b * bar
        # 铺底：轻微走音的正弦叠加，慢起慢收
        dur = bar + 1.2
        t = np.arange(int(dur * sr)) / sr
        env = np.minimum(1, t / 0.8) * np.minimum(1, (dur - t) / 1.2).clip(0)
        pad = sum(np.sin(2 * np.pi * hz(tonic + iv - 12) * t * d) for iv in chord for d in (0.998, 1.002))
        add(pad * env * 0.035, t0)
        # 低音：一、三拍
        for k in (0, 2):
            tb = np.arange(int(beat * 2 * sr)) / sr
            bass = np.sin(2 * np.pi * hz(tonic + chord[0] % 12 - 24) * tb) * np.exp(-tb * 1.6) * np.minimum(1, tb / 0.02)
            add(bass * 0.16, t0 + k * beat)
        # 琶音：八分音符，偶尔留白
        tones = sorted(chord) + [chord[0] + 12]
        pattern = [0, 2, 1, 3, 4, 3, 2, 1] if b % 2 == 0 else [0, 1, 2, 4, 3, 2, 1, 2]
        for k, idx in enumerate(pattern):
            if b == 0 and k < 4:
                continue
            if rng.random() < 0.18:
                continue
            tp = np.arange(int(1.6 * sr)) / sr
            f = hz(tonic + tones[idx] + 12)
            note = (np.sin(2 * np.pi * f * tp) + 0.25 * np.sin(4 * np.pi * f * tp) * np.exp(-tp * 6)) \
                * np.exp(-tp * 2.6) * np.minimum(1, tp / 0.004)
            add(note * rng.uniform(0.06, 0.09), t0 + k * beat / 2 + rng.uniform(0, 0.012), pan=rng.uniform(-0.5, 0.5))

    # 简单混响：指数衰减噪声做冲激响应，分块卷积
    ir_len = int(2.2 * sr)
    ti = np.arange(ir_len) / sr
    for ch_i, ch in enumerate((L, R)):
        ir = np.random.default_rng(seed * 7 + ch_i).standard_normal(ir_len) * np.exp(-ti * 3.2)
        ir /= np.sqrt(np.sum(ir ** 2))
        block = 10 * sr
        size = 1 << int(math.ceil(math.log2(block + ir_len)))
        IR = np.fft.rfft(ir, size)
        wet = np.zeros(n + ir_len)
        for s in range(0, n, block):
            seg = ch[s:s + block]
            y = np.fft.irfft(np.fft.rfft(seg, size) * IR, size)[: len(seg) + ir_len]
            wet[s:s + len(y)] += y
        ch += wet[:n] * 0.32

    mix = np.stack([L, R], axis=1)
    mix /= max(1e-9, np.abs(mix).max()) / 0.7
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes((mix * 32767).astype("<i2").tobytes())


def pick_music(music_arg, day_num):
    p = Path(music_arg).expanduser()
    if p.is_dir():
        files = sorted(f for f in p.iterdir() if f.suffix.lower() in MUSIC_EXT and not f.name.startswith("."))
        if not files:
            sys.exit(f"{p} 里没找到音乐文件")
        return files[(day_num - 1) % len(files)]
    if not p.exists():
        sys.exit(f"找不到音乐文件：{p}")
    return p


# ───────────────────────────── 渲染 ─────────────────────────────

def render(items, day_num, day, subtitle, out_path, music, sec, is_last, workdir):
    rng = np.random.default_rng(day_num * 1000 + 7)
    stamp = date_stamp(day)
    n = len(items)
    if sec is None:
        sec = min(3.5, max(2.2, 150 / n))      # 照片少就慢一点，照片多就快一点
    pf = int(sec * FPS) + XFADE

    clips = [CardClip(items[0].path, title_layer(day_num, day, subtitle), int(3.6 * FPS), fade_in_black=20, text_delay=10)]
    clips += [PhotoClip(it, pf, rng, stamp) for it in items]
    clips.append(CardClip(items[-1].path, end_layer(day_num, day, n, is_last), int(4.0 * FPS), fade_out_black=30))

    total = sum(c.frames for c in clips) - XFADE * (len(clips) - 1)
    seconds = total / FPS
    print(f"  每张 {sec:.1f} 秒，成片约 {int(seconds // 60)} 分 {int(seconds % 60)} 秒")

    if music:
        music_path = pick_music(music, day_num)
        audio_in = ["-stream_loop", "-1", "-i", str(music_path)]
        print(f"  配乐：{music_path.name}")
    else:
        music_path = workdir / f"music_day{day_num}.wav"
        synth_music(music_path, seconds, seed=day_num)
        audio_in = ["-i", str(music_path)]
        print("  配乐：自动合成的氛围配乐（想换成自己的歌用 --music）")

    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           *audio_in,
           "-map", "0:v", "-map", "1:a", "-t", f"{seconds:.3f}",
           "-af", f"afade=t=in:st=0:d=1.5,afade=t=out:st={max(0, seconds - 3.5):.3f}:d=3.5",
           "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out_path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    vig = vignette_mask()
    black = Image.new("RGB", (W, H), (0, 0, 0))
    done = 0

    def emit(im):
        nonlocal done
        proc.stdin.write(Image.composite(im, black, vig).tobytes())
        done += 1
        if done % (FPS * 5) == 0 or done == total:
            print(f"\r  渲染中 {done * 100 // total:3d}%", end="", flush=True)

    try:
        for i, clip in enumerate(clips):
            last = i == len(clips) - 1
            start = XFADE if i > 0 else 0
            end = clip.frames if last else clip.frames - XFADE
            for f in range(start, end):
                emit(clip.frame(f))
            if not last:
                nxt = clips[i + 1]
                for k in range(XFADE):
                    a, b = clip.frame(clip.frames - XFADE + k), nxt.frame(k)
                    emit(Image.blend(a, b, (k + 1) / (XFADE + 1)))
                clip.release()
    finally:
        proc.stdin.close()
        rc = proc.wait()
    print()
    if rc != 0:
        sys.exit("ffmpeg 出错了")


# ───────────────────────────── 主流程 ─────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="把某一天的照片剪成 720p 旅行 vlog")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--day", type=int, help="第几天（1 = 开始日期那天）")
    g.add_argument("--all", action="store_true", help="把 --days 天全部做完")
    ap.add_argument("--start", default="2026-10-01", help="Day 1 对应的日期，默认 2026-10-01")
    ap.add_argument("--days", type=int, default=9, help="一共几天，默认 9")
    ap.add_argument("--sd", default="/Volumes/PP02/DCIM", help="SD 卡 DCIM 路径")
    ap.add_argument("--no-sd", action="store_true", help="不读 SD 卡")
    ap.add_argument("--no-library", action="store_true", help="不读 Mac 照片图库")
    ap.add_argument("--library", help="指定 .photoslibrary 路径（默认系统图库）")
    ap.add_argument("--include-heic", action="store_true", help="照片图库里的 iPhone HEIC 也转成 JPG 一起用")
    ap.add_argument("--icloud", action="store_true", help="原图只在 iCloud 的照片也尝试下载")
    ap.add_argument("--sd-offset-min", type=float, default=0, help="相机时钟偏差（分钟），例如相机慢 8 分钟填 8")
    ap.add_argument("--title", default="川渝旅行", help="片头副标题，默认「川渝旅行」")
    ap.add_argument("--music", help="配乐文件，或一个音乐文件夹（按天轮流取用）")
    ap.add_argument("--sec", type=float, help="每张照片停留秒数（默认按照片数量自动）")
    ap.add_argument("--out", default="~/Movies/川渝Vlog", help="输出文件夹")
    ap.add_argument("--list", action="store_true", help="只列出会用到的照片，不渲染")
    args = ap.parse_args()

    if not shutil.which("ffmpeg") and not args.list:
        sys.exit("没找到 ffmpeg，请先运行：brew install ffmpeg")

    start = dt.date.fromisoformat(args.start)
    day_nums = range(1, args.days + 1) if args.all else [args.day]
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    sd_all = []
    if not args.no_sd:
        if not Path(args.sd).exists():
            sys.exit(f"找不到 SD 卡目录 {args.sd}，确认卡已插好，或加 --no-sd")
        print(f"扫描 SD 卡 {args.sd} …")
        sd_all, raw_skipped = scan_sd(args.sd, dt.timedelta(minutes=args.sd_offset_min))
        print(f"  找到 {len(sd_all)} 张 JPG，跳过 {raw_skipped} 个 RAW/其他文件")

    with tempfile.TemporaryDirectory(prefix="dayvlog_") as tmp:
        workdir = Path(tmp)
        for dn in day_nums:
            day = start + dt.timedelta(days=dn - 1)
            print(f"\n━━ Day {dn} · {day} ━━")
            sd_items = [it for it in sd_all if it.taken.date() == day]
            lib_items = [] if args.no_library else collect_library(
                day, workdir / f"lib_{day}", args.include_heic, args.icloud, args.library)
            items, dupes = merge_items(sd_items, lib_items)
            print(f"  SD 卡 {len(sd_items)} 张 + 照片图库 {len(lib_items) - dupes} 张"
                  + (f"（另有 {dupes} 张和 SD 卡重复，已去掉）" if dupes else "")
                  + f" = 共 {len(items)} 张")
            if args.list:
                for it in items:
                    print(f"    {it.taken:%H:%M:%S}  [{it.source}]  {it.name}")
                continue
            if not items:
                print("  这一天没有照片，跳过")
                continue
            out_path = out_dir / f"Day{dn:02d}_{day}.mp4"
            render(items, dn, day, args.title, out_path, args.music, args.sec,
                   is_last=(dn == args.days), workdir=workdir)
            print(f"  ✅ 完成：{out_path}")


if __name__ == "__main__":
    main()
