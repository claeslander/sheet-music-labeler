"""Recognise music symbols by their shape, in any music font embedded in a PDF.

A PDF exported from notation software draws every symbol as a character of a music font,
and embeds that font. The font's name and character codes differ per program (and are
sometimes hidden, as in "Microsoft Print to PDF" output), but the shapes do not: a sharp
looks like a sharp in every font. So we draw each character the PDF uses from the embedded
font program and compare it with the same symbols drawn from a set of known music fonts
(glyph_refs.npz, made by tools/make_glyph_refs.py).

The comparison also tells where the symbol's reference point lies: the centre of a
notehead, the line a clef sits on, the line or space of an accidental.
"""
import functools
import hashlib
import io
import logging
import tempfile
from pathlib import Path

import cv2
import numpy as np

logging.getLogger("fontTools").setLevel(logging.ERROR)  # embedded subsets often have odd header fields

PX = 12  # pixels per staff space when comparing shapes
REFS_FILE = Path(__file__).with_name("glyph_refs.npz")

# The symbols we read, with their SMuFL code points in the reference fonts
SYMBOLS = {
    "notehead": [0xE0A4, 0xE0A3, 0xE0A2, 0xE0A0, 0xE0A1],  # black, half, whole, double whole
    # notehead and stem in one glyph, as older fonts have
    "note": [0xE1D3, 0xE1D4, 0xE1D5, 0xE1D6, 0xE1D7, 0xE1D8, 0xE1D9, 0xE1DA],
    "flat": [0xE260], "natural": [0xE261], "sharp": [0xE262], "doublesharp": [0xE263], "doubleflat": [0xE264],
    "gclef": [0xE050, 0xE052, 0xE053, 0xE07A], "fclef": [0xE062, 0xE064, 0xE07C], "cclef": [0xE05C, 0xE07B],
    "percussionclef": [0xE069, 0xE06A], "tabclef": [0xE06D, 0xE06E],
    "dot": [0xE1E7],
}
# Look-alikes that must not be taken for one of the symbols above
OTHERS = ([0xE4E3, 0xE4E4, 0xE4E5, 0xE4E6, 0xE4E7, 0xE4E8]  # rests
          + list(range(0xE240, 0xE24A))  # flags
          + list(range(0xE080, 0xE08C))  # time signature digits, common/cut time
          + [0xE520, 0xE521, 0xE522, 0xE524, 0xE52F]  # dynamics p m f s z
          + [0xE0A9, 0xE0A8, 0xE0D8, 0xE0D9, 0xE0DB, 0xE101, 0xE102]  # x, diamond, slash noteheads
          + [0xE4C0, 0xE4A0, 0xE4AC, 0xE047, 0xE048, 0xE650, 0xE655, 0xE566, 0xE56C, 0xE567, 0xE000,
             0xE043, 0xE4CE, 0xE4D1, 0xE26A, 0xE26B, 0xE8E0, 0xE8E1]
          + list(range(0xE880, 0xE88A)))  # tuplet digits
# How big each symbol is in any font: ink height and width, in staff spaces (roomy ranges
# around what the reference fonts have). A glyph of another size is not that symbol, however
# similar its shape, so the similarity itself can be judged leniently: fonts differ in style.
SIZES = {
    "notehead": (0.75, 2.4, 0.9, 3.2), "note": (3.0, 6.0, 0.9, 3.0),
    "sharp": (2.0, 5.0, 0.7, 2.0), "flat": (1.8, 4.2, 0.6, 1.3), "natural": (2.0, 5.2, 0.5, 1.2),
    "doublesharp": (0.8, 2.2, 0.8, 1.7), "doubleflat": (1.8, 4.2, 1.1, 2.3),
    "gclef": (3.5, 11.5, 1.5, 3.6), "fclef": (1.8, 6.5, 1.6, 4.2), "cclef": (2.3, 6.2, 1.5, 3.6),
    "percussionclef": (1.6, 4.5, 0.8, 1.9), "tabclef": (3.4, 7.5, 0.9, 3.0), "dot": (0.3, 0.8, 0.3, 0.9),
}
MIN_SCORE = 0.45  # least similarity to count as a symbol
STRICT = {"percussionclef": 0.7, "tabclef": 0.7}  # a false one would silence a whole staff
MUSIC_SCORE = 0.72  # how similar to any music symbol a glyph must be to make its font look like music
MARGIN = 0.04  # and by how much it must beat the best look-alike
ONE_GLYPH = {"sharp", "flat", "natural", "doublesharp", "doubleflat", "gclef", "fclef", "cclef"}


class _Flatten:
    """A fontTools pen that turns outlines into polygons (lists of (x, y) in font units)."""

    def __init__(self, glyphset=None):
        from fontTools.pens.basePen import BasePen

        outer = self

        class Pen(BasePen):
            def _moveTo(self, p):
                outer.contours.append([p])

            def _lineTo(self, p):
                outer.contours[-1].append(p)

            def _curveToOne(self, p1, p2, p3):
                p0 = outer.contours[-1][-1]
                for t in np.linspace(0, 1, 9)[1:]:  # the cubic curve in 8 straight pieces
                    a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t * t, t ** 3
                    outer.contours[-1].append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
                                               a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))

            def _closePath(self):
                pass

        self.contours = []
        self.pen = Pen(glyphset)


def _render(contours, scale):
    """Fill outlines (font units, y up) at `scale` px per unit. Returns (image, origin x, origin y)
    with the image cropped to the ink plus a small margin, y down; None for an empty glyph."""
    pts = [np.array(c, dtype=np.float64) * scale for c in contours if len(c) > 2]
    if not pts:
        return None
    allp = np.concatenate(pts)
    x0, y0 = np.floor(allp.min(0)) - 2
    x1, y1 = np.ceil(allp.max(0)) + 2
    w, h = int(x1 - x0) + 1, int(y1 - y0) + 1
    if w > 40 * PX or h > 40 * PX:
        return None
    acc = np.zeros((h, w), np.int16)
    for p in pts:  # nonzero winding: add each contour with the sign of its direction
        q = np.stack([p[:, 0] - x0, y1 - p[:, 1]], 1)
        area = np.sum(q[:-1, 0] * q[1:, 1] - q[1:, 0] * q[:-1, 1]) + q[-1, 0] * q[0, 1] - q[0, 0] * q[-1, 1]
        m = np.zeros((h, w), np.uint8)
        cv2.fillPoly(m, [np.round(q * 4).astype(np.int32)], 1, lineType=cv2.LINE_8, shift=2)
        acc += m.astype(np.int16) * (1 if area > 0 else -1)
    img = (acc != 0).astype(np.uint8) * 255
    if not img.any():
        return None
    return img, -x0, y1  # origin (0, 0) of the glyph in image pixels


class FontProgram:
    """Outlines of an embedded font program, by glyph id."""

    def __init__(self, data, kind):
        self.kind, self.upm, self._glyph = kind, 1000, None
        if kind in ("ttf", "otf"):
            from fontTools.ttLib import TTFont
            font = TTFont(io.BytesIO(data), lazy=True)
            if "head" in font:
                self.upm = font["head"].unitsPerEm
            order = font.getGlyphOrder()
            if "glyf" in font:
                glyf = font["glyf"]
                self._glyph = lambda gid, pen: glyf[order[gid]].draw(pen, glyf)
            elif "CFF " in font:
                cff = font["CFF "].cff
                self._init_cff(cff)
        elif kind in ("cff", "cid"):
            from fontTools.cffLib import CFFFontSet
            cff = CFFFontSet()
            cff.decompile(io.BytesIO(data), None)
            self._init_cff(cff)
        elif kind in ("pfa", "pfb"):
            from fontTools.t1Lib import T1Font
            with tempfile.NamedTemporaryFile(suffix="." + kind) as f:
                f.write(data)
                f.flush()
                t1 = T1Font(f.name, encoding="latin-1")  # the clear-text header may have © etc.
                t1.parse()
            glyphs = t1.getGlyphSet()
            names = list(t1.font["CharStrings"].keys())
            matrix = t1.font.get("FontMatrix", [0.001])
            self.upm = round(1 / matrix[0]) if matrix[0] else 1000
            self._glyph = lambda gid, pen: glyphs[names[gid]].draw(pen)

    def _init_cff(self, cff):
        top = cff[cff.fontNames[0]]
        matrix = getattr(top, "FontMatrix", [0.001])
        self.upm = round(1 / matrix[0]) if matrix[0] else 1000
        strings, charset = top.CharStrings, top.charset
        if hasattr(top, "ROS"):  # CID-keyed: the PDF gives the CID, the subset keeps it in the name
            self._glyph = lambda cid, pen: strings[f"cid{cid:05d}"].draw(pen)
        else:
            self._glyph = lambda gid, pen: strings[charset[gid]].draw(pen)

    def outline(self, gid):
        if self._glyph is None:
            return []
        f = _Flatten()
        try:
            self._glyph(gid, f.pen)
        except Exception:
            return []
        return f.contours


@functools.lru_cache(maxsize=1)
def references():
    """The reference symbols: list of (kind, image, origin x, origin y), at PX px per space."""
    data = np.load(REFS_FILE, allow_pickle=False)
    refs = []
    for i, kind in enumerate(data["kinds"]):
        h, w = data["shapes"][i]
        img = data["pixels"][data["offsets"][i]:data["offsets"][i] + h * w].reshape(h, w)
        refs.append((str(kind), img, *data["origins"][i]))
    return refs


def _similarity(img, ox, oy, ref):
    """How well a glyph image matches a reference (normalised correlation at the best shift,
    over the reference plus a margin, so extra or missing ink both count), and the shift of
    the reference's origin relative to the glyph's origin in pixels."""
    kind, rimg, rox, roy = ref
    m = PX // 2
    tmpl = cv2.copyMakeBorder(rimg, m, m, m, m, cv2.BORDER_CONSTANT, value=0)
    slack = PX
    pad_y = max(tmpl.shape[0] - img.shape[0], 0) // 2 + slack
    pad_x = max(tmpl.shape[1] - img.shape[1], 0) // 2 + slack
    big = cv2.copyMakeBorder(img, pad_y, pad_y, pad_x, pad_x, cv2.BORDER_CONSTANT, value=0)
    if big.shape[0] < tmpl.shape[0] or big.shape[1] < tmpl.shape[1]:
        return -1, 0, 0
    res = cv2.matchTemplate(big.astype(np.float32), tmpl.astype(np.float32), cv2.TM_CCOEFF_NORMED)
    _, score, _, (tx, ty) = cv2.minMaxLoc(res)
    # reference origin in `big` coordinates, relative to the glyph's origin
    dx = tx + m + rox - (pad_x + ox)
    dy = ty + m + roy - (pad_y + oy)
    return score, dx, dy


def classify_image(img, ox, oy):
    """Best symbol for a glyph image, (kind, score, dx, dy in pixels), and the best score of
    a look-alike."""
    h, w = img.shape
    rows, cols = np.flatnonzero(img.any(axis=1)), np.flatnonzero(img.any(axis=0))
    ink_h, ink_w = (rows[-1] - rows[0] + 1) / PX, (cols[-1] - cols[0] + 1) / PX  # in staff spaces
    best, best_other = (None, -1, 0, 0), -1
    for ref in references():
        rh, rw = ref[1].shape
        if not (0.55 < h / rh < 1.8 and 0.55 < w / rw < 1.8):
            continue
        if ref[0] != "other":
            h0, h1, w0, w1 = SIZES[ref[0]]
            if not (h0 <= ink_h <= h1 and w0 <= ink_w <= w1):
                continue
        score, dx, dy = _similarity(img, ox, oy, ref)
        if ref[0] == "other":
            best_other = max(best_other, score)
        elif score > best[1]:
            best = (ref[0], score, dx, dy)
    return best, best_other


_programs = {}


def classify_font(data, kind, glyphs, main=None):
    """Classify the glyphs of one embedded font, as (gid, font size / staff space) pairs: a glyph
    can be drawn at several sizes. Returns (gid, size) -> (kind, dx, dy, width, ink box, guess), with
    the offsets of the reference point, the ink width and the ink box (x0, y0, x1, y1, y up from the
    origin) as fractions of the font size; kind is None for a glyph that is no symbol we read, and
    guess the symbol it resembles most all the same.
    Also returns the share of the glyphs that look like any music symbol, look-alikes included
    (high for a music font, low for a text font).

    main: the size most of the font's glyphs are drawn at. A glyph drawn smaller (a grace note,
    a courtesy clef) is also compared as if drawn at that size."""
    key = hashlib.sha1(data).hexdigest()
    if key not in _programs:
        try:
            _programs[key] = (FontProgram(data, kind), {})
        except Exception:
            _programs[key] = (None, {})
    program, cache = _programs[key]
    out, music = {}, 0
    if program is None:
        return out, 0.0
    for gid, ratio in glyphs:
        ck = (gid, ratio)
        if ck not in cache:
            outline = program.outline(gid)
            best, best_other = (None, -1, 0, 0, 0), -1
            grows = (1, main / ratio) if main and ratio < 0.92 * main else (1,)
            for grow in grows:  # the best match, at the size drawn or (if smaller) the main size
                size_px = PX * ratio * grow  # the font size in pixels
                r = _render(outline, size_px / program.upm)
                if r is None:
                    break
                img, ox, oy = r
                (kind_, score, dx, dy), other = classify_image(img, ox, oy)
                best_other = max(best_other, other)
                score -= 0.02 * (grow != 1)
                if score > best[1]:
                    xs = [p[0] for c in outline for p in c]  # the ink width, in font units
                    best = (kind_, score, dx / size_px, dy / size_px, (max(xs) - min(xs)) / program.upm)
            pts = [p for c in outline for p in c] or [(0, 0)]
            box = tuple(v / program.upm for v in (min(p[0] for p in pts), min(p[1] for p in pts),
                                                  max(p[0] for p in pts), max(p[1] for p in pts)))
            cache[ck] = (best, best_other, box)
    # A symbol must beat every look-alike clearly. A font has one glyph for each accidental and
    # clef, though: the glyph that resembles one most may also only nearly beat the look-alikes
    # (a font in a style unlike the reference fonts).
    top = {}
    for ck in glyphs:
        (kind_, score, *_), _, _ = cache[ck]
        top[kind_] = max(top.get(kind_, -1), score)
    for ck in glyphs:
        best, other, box = cache[ck]
        kind_, score = best[0], best[1]
        if score >= STRICT.get(kind_, MIN_SCORE) and (
                score >= other + MARGIN or (kind_ in ONE_GLYPH and score >= other - 0.08 and score == top[kind_])):
            out[ck] = (kind_, *best[2:], box, kind_)
        else:
            out[ck] = (None, 0, 0, box[2] - box[0], box, kind_ if score >= 0.35 else None)
        music += max(score, other) >= MUSIC_SCORE
    return out, music / max(1, len(glyphs))
