"""Write note names (C, D, E♭, F♯, ... or Do, Re, Mi, ...) next to every note of a score PDF.

Works on PDFs exported from notation software, where every musical symbol is a font
glyph with an exact position, so the result is exact rather than a guess. Scanned
scores (images) need OMR instead, see omr_label.py.

Usage: python note_names.py input.pdf [output.pdf] [--style letters|solfege] [--color red|blue|green|black]
"""
import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

import pymupdf

import glyph_shapes

# Glyph meanings of some music fonts: Aloisen (the font in rachman_op3_1.pdf) and LilyPond's Emmentaler.
# Any other font is read by the shapes of its glyphs (see music_glyphs).
FONT_PROFILES = {
    "Aloisen": {
        "noteheads": set("t|AYZI"),  # filled, half, whole, flagged eighths/sixteenths
        "accidentals": {'"': -1, "!": +1, "#": 0, "%": -2},
        "treble": "Ä",
        "bass": "Å",
        "dot": "«",
    },
    # LilyPond's font. A PDF embeds a subset of it whose character codes differ per file, so its
    # glyphs are matched by name, read from the embedded font itself. LilyPond before 2.12
    # names accidentals by their alteration in quarter tones.
    "Emmentaler": {
        "glyph_names": True,
        "noteheads": {"noteheads.s0", "noteheads.s1", "noteheads.s2", "noteheads.sM1"},
        "accidentals": {"accidentals.flat": -1, "accidentals.sharp": +1, "accidentals.natural": 0,
                        "accidentals.flatflat": -2, "accidentals.doublesharp": +2,
                        "accidentals.M2": -1, "accidentals.-2": -1, "accidentals.2": +1,
                        "accidentals.0": 0, "accidentals.M4": -2, "accidentals.-4": -2,
                        "accidentals.4": +2},
        "treble": {"clefs.G", "clefs.G_change"},
        "bass": {"clefs.F", "clefs.F_change"},
        "dot": "dots.dot",
        "head_w": 0.325,  # notehead width per point of glyph size
    },
}

LETTERS = ["C", "D", "E", "F", "G", "A", "B"]
SOLFEGE = ["Do", "Re", "Mi", "Fa", "Sol", "La", "Si"]
COLORS = {"red": (0.78, 0.08, 0.12), "blue": (0.05, 0.3, 0.75),
          "green": (0.0, 0.5, 0.25), "black": (0, 0, 0)}

# diatonic step of the bottom staff line, counted from C0 (C0=0, D0=1, ...)
BOTTOM_LINE = {"treble": 4 * 7 + 2, "bass": 2 * 7 + 4}  # E4, G2

# The symbols we read, in every font: noteheads ("note" is a notehead with its stem, as one
# glyph), accidentals, clefs and augmentation dots
NOTEHEADS = {"notehead", "note"}
ACCIDENTALS = {"doubleflat": -2, "flat": -1, "natural": 0, "sharp": 1, "doublesharp": 2}
CLEF_REF = {"gclef": 4 * 7 + 4, "fclef": 3 * 7 + 3, "cclef": 4 * 7}  # G4, F3, C4: the pitch on the clef's line
CLEFS = {*CLEF_REF, "percussionclef", "tabclef"}  # notes after the last two get no name
# Fonts not worth reading by shape: text fonts, and the text-symbol fonts of notation programs
# (tempo marks, dynamics)
TEXT_FONT = re.compile(r"Text|Times|Arial|Helvetica|Century|C059|DejaVu|Liberation|Nimbus|FreeS|Edwin|Georgia|"
                       r"Garamond|Palatino|Calibri|Cambria|Verdana|Courier|TeXGyre|^LM|^CM[A-Z]|Bitstream|Segoe|"
                       r"Bookman|Baskerville|Book Antiqua|Minion|Myriad|Gentium|Linux|Academico|Campania", re.I)

# Fonts with real ♭ and ♯ signs, tried in order (Linux, macOS, Windows).
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSansCondensed-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSansCondensed-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "C:/Windows/Fonts/seguisym.ttf",
]


def find_label_font():
    """First installed font that has ♭ and ♯, or None to fall back to PyMuPDF's built-in Helvetica."""
    for path in FONT_CANDIDATES:
        if Path(path).is_file():
            font = pymupdf.Font(fontfile=path)
            if font.has_glyph(ord("♭")) and font.has_glyph(ord("♯")):
                return path
    return None


LABEL_FONT = find_label_font()
# Helvetica has no ♭/♯, so write them as b and # there
ACC_TEXT = ({-2: "♭♭", -1: "♭", 0: "", 1: "♯", 2: "♯♯"} if LABEL_FONT
            else {-2: "bb", -1: "b", 0: "", 1: "#", 2: "##"})
CHECK_COLOR = (0.9, 0.42, 0.0)  # orange: "not sure, please check"
NOTEHEAD_W = 6.0 / 20  # notehead width per point of glyph size


class UnsupportedPDF(Exception):
    pass


def drawings(page):
    """The page's vector drawings, read once per page (several steps need them)."""
    if not hasattr(page, "_drawings"):
        page._drawings = page.get_drawings()
    return page._drawings


def staff_segments(page):
    """Horizontal strokes that may be (part of) a staff line, as (y, length, stroke width): lines
    drawn as strokes, or thin filled shapes. The edges of thicker filled shapes (beams) don't count.

    Some programs draw each staff line in one piece, others (MuseScore) one piece per bar;
    short ones are ledger lines.
    """
    out = []
    for d in drawings(page):
        if "s" in d["type"]:
            out += [(round(it[1].y, 1), abs(it[1].x - it[2].x), d.get("width") or 0) for it in d["items"]
                    if it[0] == "l" and abs(it[1].y - it[2].y) < 0.1 and abs(it[1].x - it[2].x) > 25]
        elif d["rect"].height < 1.5 and d["rect"].width > 25 and all(it[0] in ("l", "re", "qu") for it in d["items"]):
            # a thin filled rectangle (not a tie or slur, which are curves)
            out.append((round((d["rect"].y0 + d["rect"].y1) / 2, 1), d["rect"].width, d["rect"].height))
    return out


def find_staves(page):
    """Staves as dicts with the y of the top/bottom line and the line spacing: five evenly spaced
    long horizontal lines (a hairpin or slur level with a staff line doesn't break that)."""
    length = defaultdict(float)
    for y, n, _ in staff_segments(page):
        length[y] += n
    # long enough (a short final system too), and not next to a much longer line (a hairpin drawn
    # as strokes along a staff line); lines closer than half a point are one line drawn twice
    cand = [y for y, n in length.items() if n > 50
            and not any(abs(z - y) < 2.6 and length[z] > 3 * n for z in length)]
    ys = []
    for y in sorted(cand):
        if ys and y - ys[-1] < 0.6:
            ys[-1] = max(ys[-1], y, key=length.get)
        else:
            ys.append(y)
    staves, i = [], 0
    while i + 4 < len(ys):
        gaps = [ys[i + k + 1] - ys[i + k] for k in range(4)]
        sp = (ys[i + 4] - ys[i]) / 4
        if 2 < sp < 15 and max(gaps) - min(gaps) < 0.15 * sp:
            staves.append({"top": ys[i], "bottom": ys[i + 4], "sp": sp})
            i += 5
        else:
            i += 1
    return staves


def find_barlines(page, staves, heads=()):
    """Barlines as (x, y_top, y_bottom): thin filled rectangles or vertical strokes that start
    and end on a staff's outer lines. A stem can do that too (LilyPond draws stems as rectangles,
    Finale and MuseScore as strokes as thick as their barlines), but it has a notehead at an end:
    so a line that is not thicker than the staff lines counts only without one."""
    # the stroke width of the staff lines themselves (filled shapes such as beams count 0)
    on_staff = lambda y: any(st["top"] - 0.5 <= y <= st["bottom"] + 0.5 for st in staves)
    widths = sorted(w for y, _, w in staff_segments(page) if w and on_staff(y))
    staff_w = widths[len(widths) // 2] if widths else 0
    edges = [y for st in staves for y in (st["top"], st["bottom"])]
    on_edge = lambda y: any(abs(y - e) < 1 for e in edges)
    sp = min((st["sp"] for st in staves), default=5)
    stem = lambda x, y0, y1: any(h["x"] - 1 <= x <= h["x"] + h["w"] + 1
                                 and min(abs(h["y"] - y0), abs(h["y"] - y1)) < 0.75 * sp for h in heads)
    out = []
    for d in drawings(page):
        for it in d["items"]:
            if it[0] == "re" and it[1].width < 3 and it[1].height > 15 and on_edge(it[1].y0) and on_edge(it[1].y1):
                x, y0, y1, w = it[1].x0, it[1].y0, it[1].y1, it[1].width
            elif (it[0] == "l" and abs(it[1].x - it[2].x) < 0.1 and abs(it[1].y - it[2].y) > 15
                  and on_edge(it[1].y) and on_edge(it[2].y)):
                x, y0, y1, w = it[1].x, min(it[1].y, it[2].y), max(it[1].y, it[2].y), d.get("width") or 0
            else:
                continue
            if (staff_w and w > 1.3 * staff_w) or (heads and not stem(x, y0, y1)):
                out.append((x, y0, y1))
    return out


def cff_index(b, p):
    """The entries of a CFF INDEX starting at byte p, and the offset just after it."""
    n = int.from_bytes(b[p:p + 2], "big")
    if n == 0:
        return [], p + 2
    size = b[p + 2]
    offs = [int.from_bytes(b[p + 3 + i * size:p + 3 + (i + 1) * size], "big") for i in range(n + 1)]
    base = p + 2 + (n + 1) * size
    return [b[base + offs[i]:base + offs[i + 1]] for i in range(n)], base + offs[-1]


def cff_top_dict(d):
    """The integer operands of a CFF Top DICT, per operator."""
    out, args, i = {}, [], 0
    while i < len(d):
        b0 = d[i]
        if b0 <= 21:  # operator (12 starts a two-byte one)
            op, i = (1200 + d[i + 1], i + 2) if b0 == 12 else (b0, i + 1)
            out[op], args = args, []
        elif b0 == 28:
            args.append(int.from_bytes(d[i + 1:i + 3], "big", signed=True))
            i += 3
        elif b0 == 29:
            args.append(int.from_bytes(d[i + 1:i + 5], "big", signed=True))
            i += 5
        elif b0 == 30:  # real number: skip its nibbles up to the end marker
            i += 1
            while d[i] & 0x0F != 0x0F and d[i] >> 4 != 0x0F:
                i += 1
            args.append(0)
            i += 1
        elif b0 <= 246:
            args.append(b0 - 139)
            i += 1
        elif b0 <= 250:
            args.append((b0 - 247) * 256 + d[i + 1] + 108)
            i += 2
        else:
            args.append(-(b0 - 251) * 256 - d[i + 1] - 108)
            i += 2
    return out


def cff_glyph_names(b):
    """Glyph names of an embedded CFF font, indexed by glyph id ([] if they can't be read).

    Only custom names are needed (standard ones such as "period" come back as None)."""
    try:
        _, p = cff_index(b, b[2])  # header, font names
        tops, p = cff_index(b, p)
        strings, _ = cff_index(b, p)
        top = cff_top_dict(tops[0])
        count = len(cff_index(b, top[17][0])[0])  # CharStrings: one per glyph
        charset = top.get(15, [0])[0]
        if charset <= 2:  # a predefined charset: standard names only
            return []
        sid = lambda s: strings[s - 391].decode("latin-1") if s >= 391 else None
        names, fmt, q = [".notdef"], b[charset], charset + 1
        while len(names) < count:
            first = int.from_bytes(b[q:q + 2], "big")
            if fmt == 0:
                names.append(sid(first))
                q += 2
                continue
            left = b[q + 2] if fmt == 1 else int.from_bytes(b[q + 2:q + 4], "big")
            names += [sid(first + k) for k in range(left + 1)]
            q += 3 if fmt == 1 else 4
        return names[:count]
    except (IndexError, KeyError):
        return []


def font_profile(fontname):
    return next((FONT_PROFILES[f] for f in FONT_PROFILES if f in fontname), None)


ENCODINGS = {"MacRomanEncoding": "mac_roman", "WinAnsiEncoding": "cp1252"}


def font_unicodes(doc, xref):
    """What a font can produce, as a test (unicode, gid) -> bool, read from its ToUnicode map or
    its encoding; None when that can't be told. For a Type0 font with the Identity encoding the
    character code is the glyph id, so the pair itself must be in the map."""
    obj = doc.xref_object(xref)
    tu = re.search(r"/ToUnicode (\d+) 0 R", obj)
    if tu:
        cmap = doc.xref_stream(int(tu.group(1))).decode("latin-1", "replace")
        codes = {}
        for block in re.findall(r"beginbfchar(.*?)endbfchar", cmap, re.S):
            for c, u in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
                codes[int(c, 16)] = chr(int(u[:4], 16))
        for block in re.findall(r"beginbfrange(.*?)endbfrange", cmap, re.S):
            for lo, hi, u in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
                for i in range(int(hi, 16) - int(lo, 16) + 1):
                    codes[int(lo, 16) + i] = chr(int(u[:4], 16) + i)
        if "/Identity-H" in obj:
            return lambda u, gid: codes.get(gid) == u
        produced = set(codes.values())
        return lambda u, gid: u in produced
    enc = re.search(r"/Encoding /(\w+)", obj)
    first, last = re.search(r"/FirstChar (\d+)", obj), re.search(r"/LastChar (\d+)", obj)
    if enc and enc.group(1) in ENCODINGS and first and last:
        codes = bytes(range(int(first.group(1)), int(last.group(1)) + 1))
        produced = set(codes.decode(ENCODINGS[enc.group(1)], "replace"))
        return lambda u, gid: u in produced
    return None


def profile_kind(prof, g):
    """What a glyph of a known font is: notehead, sharp, gclef, dot, ... or None."""
    if g in prof["noteheads"]:
        return "notehead"
    if g in prof["accidentals"]:
        return next(k for k, v in ACCIDENTALS.items() if v == prof["accidentals"][g])
    if g in prof["treble"]:
        return "gclef"
    if g in prof["bass"]:
        return "fclef"
    return "dot" if g == prof["dot"] else None


def music_glyphs(page, staves):
    """The music symbols on the page, as dicts: kind "g" (notehead, sharp, gclef, dot, ...),
    position of the symbol's reference point (x, y), font size, right edge x1 and width w.

    Fonts in FONT_PROFILES are read with their profile. Any other font embedded in the PDF is
    read by the shapes of its glyphs (glyph_shapes.py), when it looks like a music font."""
    # Fonts by name without the subset prefix. The text layer only names a span's font, and a
    # PDF can embed one font as several subsets under the same name: then each character goes
    # to the subset that can produce it, or else to the biggest one.
    fonts = defaultdict(list)
    for xref, ext, _, basefont, *_ in page.get_fonts():
        fonts[re.sub(r"-Identity-[HV]$", "", basefont.split("+")[-1])].append((xref, ext))
    names = {name: cff_glyph_names(page.parent.extract_font(subsets[0][0])[3]) for name, subsets in fonts.items()
             if (font_profile(name) or {}).get("glyph_names") and subsets[0][1] == "cff"}
    out, unknown = [], defaultdict(list)
    for span in page.get_texttrace():
        name = span["font"].split("+")[-1]  # some writers name fonts "CIDFont+F1"
        prof = font_profile(name)
        if prof is None:
            if name in fonts and not TEXT_FONT.search(name):
                unknown[name].append(span)
            continue
        by_name = names.get(name, []) if prof.get("glyph_names") else None
        for code, gid, origin, bbox in span["chars"]:
            if by_name is None:
                g = chr(code)
            elif 0 <= gid < len(by_name) and by_name[gid]:
                g = by_name[gid]
            else:
                continue
            kind = profile_kind(prof, g)
            if kind:
                out.append({"g": kind, "x": origin[0], "y": origin[1], "size": span["size"], "x1": bbox[2],
                            "w": prof.get("head_w", NOTEHEAD_W) * span["size"]})
    for name, spans in unknown.items():
        subsets = [(xref, ext) for xref, ext in fonts[name] if ext != "n/a"]  # n/a: Type3, no font program
        if not subsets:
            continue
        if len(subsets) == 1:
            out += shape_glyphs(page, subsets[0], [(span, ch) for span in spans for ch in span["chars"]], staves)
            continue
        sizes = {xref: len(page.parent.extract_font(xref)[3]) for xref, _ in subsets}
        can = {xref: font_unicodes(page.parent, xref) for xref, _ in subsets}
        chars = defaultdict(list)
        for span in spans:
            for ch in span["chars"]:
                fit = [(xref, ext) for xref, ext in subsets if can[xref] is None or can[xref](chr(ch[0]), ch[1])]
                chars[max(fit or subsets, key=lambda s: sizes[s[0]])].append((span, ch))
        for subset, uses in chars.items():
            out += shape_glyphs(page, subset, uses, staves)
    return out


def shape_glyphs(page, font, uses, staves):
    """The music symbols drawn in a font we have no profile for, recognised by their shapes.
    uses: (span, character) pairs."""
    xref, ext = font
    # each glyph, at each size it is drawn at (font size / staff space)
    ratio = lambda span, origin: round(span["size"] / staves[nearest_staff(staves, origin[1])]["sp"], 1)
    pairs = {(gid, ratio(span, origin)) for span, (_, gid, origin, _) in uses}
    sizes = [ratio(span, origin) for span, (_, gid, origin, _) in uses]
    main = max(set(sizes), key=sizes.count)  # the size most of it is drawn at
    classes, music = glyph_shapes.classify_font(page.parent.extract_font(xref)[3], ext, pairs, main)
    if music < 0.5 and not {kind for kind, *_ in classes.values()} & CLEFS:
        return []  # a text font: the odd letter may look like a symbol
    out = []
    for span, (_, gid, origin, _) in uses:
        if (gid, ratio(span, origin)) in classes:
            size = span["size"]
            kind, dx, dy, w, (bx0, by0, bx1, by1), guess = classes[(gid, ratio(span, origin))]
            x = origin[0] + dx * size
            # other glyphs of a music font are kept as "unknown", with their ink box: an unknown
            # glyph where a line's clef belongs is read as a clef by its size and place
            out.append({"g": kind or "unknown", "x": x, "y": origin[1] + dy * size, "size": size,
                        "x1": x + w * size, "w": NOTEHEAD_W * size if kind == "note" else w * size,
                        "top": origin[1] - by1 * size, "bottom": origin[1] - by0 * size, "guess": guess})
    # a notehead with stem in one glyph is how some fonts draw notes, but also the note of a
    # tempo mark: count it only when the font uses it for many notes
    if sum(g["g"] == "note" for g in out) < 5:
        out = [g for g in out if g["g"] != "note"]
    return out


def nearest_staff(staves, y):
    return min(range(len(staves)), key=lambda i: abs(y - (staves[i]["top"] + staves[i]["bottom"]) / 2))


def step_of(staff, y):
    """Half-spaces above the bottom line (0 = bottom line, 1 = first space, ...)."""
    return round((staff["bottom"] - y) / (staff["sp"] / 2))


SHARP_ORDER = [3, 0, 4, 1, 5, 2, 6]  # F C G D A E B; flats go the other way


def in_key_order(signs):
    """Whether (letter, alter) signs read as a key signature: groups of one kind of sign, each
    a stretch of the standard order (naturals cancel any part of the old key), and never both
    sharps and flats."""
    if len({alter for _, alter in signs if alter}) > 1 or any(abs(alter) > 1 for _, alter in signs):
        return False
    i = 0
    while i < len(signs):
        j = i
        while j < len(signs) and signs[j][1] == signs[i][1]:
            j += 1
        letters = [letter for letter, _ in signs[i:j]]
        orders = [SHARP_ORDER, SHARP_ORDER[::-1]] if signs[i][1] == 0 else \
                 [SHARP_ORDER if signs[i][1] > 0 else SHARP_ORDER[::-1]]
        if not any(o[k:k + len(letters)] == letters for o in orders for k in range(8 - len(letters))):
            return False
        i = j
    return True


def clef_bottom(staff, clef):
    """Diatonic step of the staff's bottom line after this clef (None after a percussion or tab
    clef). The clef's reference point sits on the line it names: G4, F3 or C4."""
    if clef["g"] not in CLEF_REF:
        return None
    line = step_of(staff, clef["y"])
    if line not in (0, 2, 4, 6, 8):  # not on a line of this staff: the usual position
        line = {"gclef": 2, "fclef": 6, "cclef": 4}[clef["g"]]
    return CLEF_REF[clef["g"]] - line


def clef_by_shape(staff, glyphs):
    """A clef at the start of a line that wasn't recognised by its shape (a font in an unusual
    style): the clef it resembles most, if a big enough glyph, or else told by its size and place.
    A treble clef sticks out above and below the staff, an alto or tenor clef is about as tall as
    the staff, a bass clef fills its upper part."""
    sp = staff["sp"]
    for g in sorted(glyphs, key=lambda g: g["x"]):
        h = (g["bottom"] - g["top"]) / sp
        above, below = (staff["top"] - g["top"]) / sp, (g["bottom"] - staff["bottom"]) / sp  # sticks out
        if g.get("guess") in CLEF_REF and h >= 2.3:
            line = 2 * round((staff["bottom"] - (g["top"] + g["bottom"]) / 2) / sp)  # the line at its middle
            line = {"gclef": 2, "fclef": 6}.get(g["guess"], line)
            return {"g": g["guess"], "x": g["x"], "y": staff["bottom"] - line * sp / 2, "size": g["size"]}
        if h >= 5 and above > 0.5 and below > 0.5:
            return {"g": "gclef", "x": g["x"], "y": staff["bottom"] - sp, "size": g["size"]}
        if 3.3 <= h < 5 and above > -0.6 and below > -0.6:
            line = 2 * round((staff["bottom"] - (g["top"] + g["bottom"]) / 2) / sp)  # the line at its middle
            return {"g": "cclef", "x": g["x"], "y": staff["bottom"] - line * sp / 2, "size": g["size"]}
        if 2.3 <= h < 4.2 and above > -0.7 and below < -0.3:
            return {"g": "fclef", "x": g["x"], "y": staff["bottom"] - 3 * sp, "size": g["size"]}
    return None


def key_changes(items, heads, staff):
    """Ids of the signs of key signatures in the middle of a line (after a barline, or even
    in the middle of a bar).

    That is a run of two or more signs about a space apart, in key signature order, unless each
    of them sits on a note of the first chord after it (then they are that chord's accidentals)."""
    out, sp, bottom, run = set(), staff["sp"], None, []
    for g in items + [None]:
        is_acc = g is not None and g["g"] in ACCIDENTALS and bottom is not None
        if is_acc and run and 0.3 * sp < g["x"] - run[-1]["x"] < 1.8 * sp:
            run.append(g)
            continue
        signs = [((bottom + step_of(staff, a["y"])) % 7, ACCIDENTALS[a["g"]]) for a in run]
        if len(run) >= 2 and in_key_order(signs):
            after = [h for h in heads if h["x"] > run[-1]["x"]]
            first = min((h["x"] for h in after), default=0)
            chord = {step_of(staff, h["y"]) for h in after if h["x"] - first < 2 * sp}
            if not all(step_of(staff, a["y"]) in chord for a in run):
                out |= {id(a) for a in run}
        run = [g] if is_acc else []
        if g is not None and g["g"] in CLEFS:
            bottom = clef_bottom(staff, g)
    return out


def ledger_lines(page):
    """Short horizontal strokes (or thin filled rectangles) that may be ledger lines: (y, x0, x1)."""
    out = []
    for d in drawings(page):
        for it in d["items"]:
            if it[0] == "l" and "s" in d["type"] and abs(it[1].y - it[2].y) < 0.1 and 3 < abs(it[1].x - it[2].x) < 30:
                out.append((it[1].y, min(it[1].x, it[2].x), max(it[1].x, it[2].x)))
            elif it[0] == "re" and "f" in d["type"] and it[1].height < 1.5 and 3 < it[1].width < 30:
                out.append(((it[1].y0 + it[1].y1) / 2, it[1].x0, it[1].x1))
    return out


def staff_of(staves, ledgers, g):
    """The staff a notehead belongs to: the nearest, unless it lies between two staves and only
    the other one's ledger lines reach it (a left-hand note high above the bass staff)."""
    i = nearest_staff(staves, g["y"])
    st = staves[i]
    j = i + 1 if g["y"] > st["bottom"] else i - 1 if g["y"] < st["top"] else None
    if j is None or not 0 <= j < len(staves):
        return i
    x = g["x"] + g["w"] / 2

    def reached(s):  # all ledger lines from the staff s out to the note are there
        sp, below = s["sp"], g["y"] > s["bottom"]
        edge = s["bottom"] if below else s["top"]
        n = int(abs(g["y"] - edge) / sp + 0.25)
        return n >= 1 and all(any(abs(y - (edge + (k if below else -k) * sp)) < 0.3 * sp and x0 < x < x1
                                  for y, x0, x1 in ledgers) for k in range(1, n + 1))
    return j if reached(staves[j]) and not reached(st) else i


def find_ties(page):
    """Flat curves that may be ties (or slurs), as (left x, left y, right x, right y): the tips
    of each curved path. Notation programs draw them as thin filled or stroked curves."""
    out = []
    for d in drawings(page):
        r = d["rect"]
        if r.width < 3 or r.height > r.width / 2 or not any(it[0] == "c" for it in d["items"]):
            continue
        pts = [p for it in d["items"] if it[0] in ("c", "l") for p in it[1:]]
        left, right = min(pts, key=lambda p: p.x), max(pts, key=lambda p: p.x)
        out.append((left.x, left.y, right.x, right.y))
    return out


def find_systems(page, staves):
    """For each staff, its position in its system (0 = top staff). Staves in one system are
    joined by a vertical line at the start of the system."""
    edges = [y for st in staves for y in (st["top"], st["bottom"])]
    on_edge = lambda y: any(abs(y - e) < 1 for e in edges)  # not the edge of the page, say
    joins = [(y0, y1) for y0, y1 in
             ((it[1].y0, it[1].y1) if it[0] == "re" else sorted((it[1].y, it[2].y))
              for d in drawings(page) for it in d["items"]
              if (it[0] == "re" and it[1].width < 3 and it[1].height > 30)
              or (it[0] == "l" and abs(it[1].x - it[2].x) < 0.1 and abs(it[1].y - it[2].y) > 30))
             if on_edge(y0) and on_edge(y1)]
    pos = [0] * len(staves)
    for i in range(1, len(staves)):
        if any(y0 <= staves[i - 1]["bottom"] + 1 and y1 >= staves[i]["top"] - 1 for y0, y1 in joins):  # bridges the gap
            pos[i] = pos[i - 1] + 1
    return pos


def analyse_page(page, carry=None):
    """Return every note on the page: dict with staff, x, y, size, x1, letter (0-6), alter (-2..2).

    A bar can continue on the next line (and page): pass the same dict as carry for every
    page, in order, and accidentals still in force at a line break are kept in it."""
    staves = find_staves(page)
    glyphs = music_glyphs(page, staves) if staves else []
    if not any(g["g"] in NOTEHEADS for g in glyphs):
        return [], []
    carry = {} if carry is None else carry
    bars = find_barlines(page, staves, [g for g in glyphs if g["g"] in NOTEHEADS])
    positions = find_systems(page, staves)
    ties = find_ties(page)
    # grace and cue notes, and the accidentals of ornaments, are smaller than the usual noteheads
    sizes = sorted(g["size"] for g in glyphs if g["g"] in NOTEHEADS)
    small = lambda g: g["size"] < 0.85 * sizes[len(sizes) // 2]
    unknown = [g for g in glyphs if g["g"] == "unknown"]
    glyphs = [g for g in glyphs if g["g"] != "unknown"]

    per_staff = [[] for _ in staves]
    ledgers = ledger_lines(page)
    for g in sorted(glyphs, key=lambda g: g["g"] not in NOTEHEADS):  # noteheads first
        if g["g"] in NOTEHEADS:
            g["staff"] = staff_of(staves, ledgers, g)
        else:
            g["staff"] = nearest_staff(staves, g["y"])
            st = staves[g["staff"]]
            if g["g"] in CLEFS and not st["top"] - 2 * st["sp"] < g["y"] < st["bottom"] + 2 * st["sp"]:
                continue  # the clef of something we don't read as a staff (a tablature, say)
            if g["g"] in ACCIDENTALS:  # an accidental goes with the notehead to its right
                head = min((h for h in glyphs if h["g"] in NOTEHEADS and abs(h["y"] - g["y"]) < 1
                            and 0 < h["x"] - g["x"] < 30), key=lambda h: h["x"], default=None)
                if head:
                    g["staff"] = head["staff"]
        per_staff[g["staff"]].append(g)

    lines = []  # per staff: its barlines, and its noteheads
    for staff, items in zip(staves, per_staff):
        items.sort(key=lambda g: g["x"])
        lines.append((sorted(x for x, y0, y1 in bars if y0 <= staff["top"] + 1 and y1 >= staff["bottom"] - 1),
                      [g for g in items if g["g"] in NOTEHEADS]))
    # music without barlines, as opposed to one long bar (a cadenza) on a page that has them
    unbarred = not any(heads[0]["x"] < x < heads[-1]["x"] for bar_xs, heads in lines if heads for x in bar_xs)

    notes = []
    for i, (staff, items, pos, (bar_xs, heads)) in enumerate(zip(staves, per_staff, positions, lines)):
        mine = [g for g in unknown if nearest_staff(staves, g["y"]) == i]
        if heads and not any(g["g"] in CLEFS and g["x"] < heads[0]["x"] for g in items):
            clef = clef_by_shape(staff, [g for g in mine if g["x"] < heads[0]["x"]])
            if clef:
                items.insert(0, clef)
        if heads:  # clef changes later in the line: a glyph on the staff that resembles a clef
            for g in mine:
                mid = (g["top"] + g["bottom"]) / 2
                if (g["x"] > heads[0]["x"] and g.get("guess") in CLEF_REF and staff["top"] < mid < staff["bottom"]
                        and g["bottom"] - g["top"] > 2 * staff["sp"] * g["size"] / sizes[len(sizes) // 2]):
                    clef = clef_by_shape(staff, [g])
                    if clef:
                        items.append(clef)
            items.sort(key=lambda g: g["x"])
        # a run of accidentals right after the clef that opens the line is a key signature, even
        # when its last sign happens to sit close to the first note. Not when the signs are out of
        # key signature order, or one has a note on its line/space within two spaces (a key
        # signature keeps over three): then they are the first chord's accidentals, as are
        # naturals alone (a key without sharps or flats prints nothing).
        runs, last, gap = [], None, 0
        for g in items:
            if g["g"] in CLEFS and not any(h["x"] < g["x"] for h in heads):
                last, gap = g["x"], 5 * staff["sp"]  # first sign: within a few spaces of the clef
                runs.append((clef_bottom(staff, g), []))
            elif g["g"] in ACCIDENTALS and last is not None and 0 < g["x"] - last < gap:
                runs[-1][1].append(g)
                last, gap = g["x"], 1.6 * staff["sp"]  # next signs: about one space apart
            else:
                last = None
        on_note = lambda a: any(0 < h["x"] - a["x"] < 2 * staff["sp"]
                                and step_of(staff, h["y"]) == step_of(staff, a["y"]) for h in heads)
        signs = lambda bottom, run: [((bottom + step_of(staff, g["y"])) % 7, ACCIDENTALS[g["g"]])
                                     for g in run]
        key_sig = {id(g) for bottom, run in runs if bottom is not None
                   if any(ACCIDENTALS[g["g"]] for g in run) and not any(map(on_note, run))
                   and in_key_order(signs(bottom, run))
                   for g in run}
        key_sig |= key_changes(items, heads, staff)

        # attach each other accidental to the closest note to its right on the same line/space
        # that has none yet (two signs before two notes on one line, a ♮ and a ♭ in two voices,
        # go to them in order)
        for g in items:
            if g["g"] in ACCIDENTALS:
                g["step"] = step_of(staff, g["y"])
                if id(g) in key_sig:
                    g["note"] = None
                    continue
                cands = [n for n in heads if 0 < n["x"] - g["x"] < 30 and step_of(staff, n["y"]) == g["step"]
                         and small(n) == small(g) and "acc" not in n]
                g["note"] = min(cands, key=lambda n: n["x"]) if cands else None
                if g["note"] is None and small(g):
                    g["note"] = False  # a small sign on no small note belongs to an ornament
                if g["note"]:
                    g["note"]["acc"] = ACCIDENTALS[g["g"]]

        # Without barlines it is unclear how long an accidental lasts: to the end of the line,
        # or shorter (LilyPond hides barlines but still resets accidentals at them). It is kept
        # to the end of the line, but marked on notes other than one repeated straight after.
        barless = heads and unbarred
        # the line's opening barline doesn't end a bar that continues from the line before
        bottom, key = None, {}
        bar_i = sum(heads[0]["x"] > x for x in bar_xs) if heads else 0
        measure = dict(carry.get(pos, {}))
        chord_x, chord, prev_chord = None, {}, {}  # diatonic -> marked
        sp = staff["sp"]
        # A tie keeps a note's accidental into the next bar (and from the end of the line before).
        # It runs from just after one notehead to just before the next one on the same line/space.
        tie_from = lambda t, n: (n["x"] - 0.3 * sp <= t[0] <= n["x"] + n["w"] + 1.2 * sp
                                 and abs(t[1] - n["y"]) < 1.2 * sp)
        tie_to = lambda t, n: n["x"] - 1.5 * sp <= t[2] <= n["x"] + 0.6 * n["w"] and abs(t[3] - n["y"]) < 1.2 * sp
        last_on = {}  # diatonic -> the last note on it
        tied_in = carry.get(("tie", pos), {})  # diatonic -> alteration tied over the line break
        for g in items:
            while bar_i < len(bar_xs) and bar_xs[bar_i] < g["x"]:
                measure = {}  # accidentals only last until the barline
                bar_i += 1
            if g["g"] in CLEFS:
                bottom = clef_bottom(staff, g)
            elif g["g"] in ACCIDENTALS and g["note"] is None and bottom is not None:
                # a free-standing accidental belongs to a key signature (applies to every octave)
                key[(bottom + g["step"]) % 7] = ACCIDENTALS[g["g"]]
            elif g["g"] in NOTEHEADS and bottom is not None:
                diatonic = bottom + step_of(staff, g["y"])
                if "acc" in g:
                    measure[diatonic] = g["acc"]
                if chord_x is None or g["x"] - chord_x > 0.5 * g["w"]:
                    chord_x, chord, prev_chord = g["x"], {}, chord
                g["letter"] = diatonic % 7
                g["alter"] = measure.get(diatonic, key.get(diatonic % 7, 0))
                if "acc" not in g and diatonic not in measure:
                    t = next((t for t in ties if tie_to(t, g)), None)
                    prev = last_on.get(diatonic)
                    if t and prev and tie_from(t, prev):
                        g["alter"] = prev["alter"]
                    elif t and diatonic in tied_in and t[0] < heads[0]["x"]:
                        g["alter"] = tied_in[diatonic]
                last_on[diatonic] = g
                if barless and "acc" not in g and prev_chord.get(diatonic, True) \
                        and g["alter"] != key.get(diatonic % 7, 0):
                    g["uncertain"] = True
                chord[diatonic] = g.get("uncertain", False)
                g["pitch"] = diatonic
                g["small"] = small(g)
                notes.append(g)
        # notes tied over the end of the line
        carry[("tie", pos)] = {d: n["alter"] for d, n in last_on.items()
                               if any(tie_from(t, n) and t[2] > heads[-1]["x"] + heads[-1]["w"] for t in ties)}
        # a line with no barline after its last note ends in the middle of a bar
        if heads and not unbarred and (not bar_xs or bar_xs[-1] < heads[-1]["x"]):
            carry[pos] = measure
        else:
            carry.pop(pos, None)
    dots = [g for g in glyphs if g["g"] == "dot"]
    return notes, dots


def chords(notes):
    """Group noteheads on the same staff that sit (almost) on top of each other.

    Heads moved aside sit one head width apart (a whole column of them up to about 1.16);
    consecutive notes, even when LilyPond spaces them tightly, about 1.24 or more."""
    groups = []
    for n in sorted(notes, key=lambda n: (n["staff"], n["x"])):
        g = groups[-1] if groups else None
        same_chord = (g and g[0]["staff"] == n["staff"]
                      and n["x"] - g[-1]["x"] < 1.2 * n["w"] and n["x"] - g[0]["x"] < 2.2 * n["w"])
        if same_chord:
            g.append(n)
        else:
            groups.append([n])
    return groups


def layout(notes, dots, fontsize, names):
    """Place a label to the right of each chord, one per notehead, without overlaps.

    A note may carry its own "fs" (font size) and an "uncertain" flag (drawn orange).
    """
    labels = []
    for chord in chords(notes):
        w = chord[0]["w"]
        fs = chord[0].get("fs", fontsize) * (0.75 if chord[0]["small"] else 1)
        right = max(n["x"] + n["w"] for n in chord)
        ys = [n["y"] for n in chord]
        for d in dots:  # augmentation dots sit right of the chord: put the label after them
            if (d["staff"] == chord[0]["staff"] and 0 <= d["x"] - right < 0.8 * w
                    and min(ys) - 0.7 * w < d["y"] < max(ys) + 0.7 * w):
                right = max(right, d["x1"])
        x = right + 0.13 * w
        # one label per pitch, top to bottom; spread them so they never overlap
        seen, items = set(), []
        for n in sorted(chord, key=lambda n: n["y"]):
            text = names[n["letter"]] + ACC_TEXT[n["alter"]]
            if (n["pitch"], text) in seen:
                continue
            seen.add((n["pitch"], text))
            items.append([n["y"], text, n.get("uncertain", False)])
        gap = fs * 0.92
        for i in range(1, len(items)):
            items[i][0] = max(items[i][0], items[i - 1][0] + gap)
        shift = (sum(n["y"] for n in chord) / len(chord)) - (sum(i[0] for i in items) / len(items))
        for y, text, uncertain in items:
            labels.append((x, y + shift, text, fs, uncertain))
    return labels


def label_fontname(page):
    """Make the label font available on the page and return the name to draw with."""
    if LABEL_FONT is None:
        return "helv"
    page.insert_font(fontname="lbl", fontfile=LABEL_FONT)
    return "lbl"


def draw_labels(page, labels, rgb):
    """Draw labels with a soft white backdrop so they stay legible over stems and beams."""
    font = pymupdf.Font(fontfile=LABEL_FONT) if LABEL_FONT else pymupdf.Font("helv")
    shape = page.new_shape()
    for x, y, text, fs, _ in labels:
        w = font.text_length(text, fs)
        shape.draw_rect(pymupdf.Rect(x - 0.06 * fs, y - fs * 0.45, x + w + 0.06 * fs, y + fs * 0.42))
    shape.finish(fill=(1, 1, 1), color=None, fill_opacity=0.82)
    shape.commit()
    # all labels in one go (one insert_text per label is slow on long scores); a Shape, unlike a
    # TextWriter, also gets the place right on pages whose visible part is cropped
    fontname = label_fontname(page)
    shape = page.new_shape()
    for x, y, text, fs, uncertain in labels:
        shape.insert_text((x, y + fs * 0.36), text, fontname=fontname, fontsize=fs,
                          color=CHECK_COLOR if uncertain else rgb)
    shape.commit()


def draw_legend(page, count):
    """One line at the top of the first page explaining the orange marks."""
    fontname = label_fontname(page)
    text = f"Orange = not sure, please check ({count} note{'s' if count != 1 else ''})"
    page.insert_text((page.rect.x0 + 24, page.rect.y0 + 16), text, fontname=fontname, fontsize=7, color=CHECK_COLOR)


def annotate(src, dst, style="letters", color="red", fontsize=5.4):
    doc = pymupdf.open(src)
    if doc.needs_pass:
        raise UnsupportedPDF("this PDF needs a password to open")
    names = SOLFEGE if style == "solfege" else LETTERS
    rgb = COLORS.get(color, COLORS["red"])
    total, unsure, carry = 0, 0, {}
    for page in doc:
        notes, dots = analyse_page(page, carry)
        if not notes:
            continue
        total += len(notes)
        unsure += sum(n.get("uncertain", False) for n in notes)
        draw_labels(page, layout(notes, dots, fontsize, names), rgb)
    if total == 0:
        raise UnsupportedPDF("no notes found: this is probably a scanned score, or a PDF that draws its "
                             "notes as shapes rather than with a music font")
    if unsure:
        draw_legend(doc[0], unsure)
    doc.save(dst, garbage=3, deflate=True)
    return total, unsure


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("input", type=Path)
    ap.add_argument("output", type=Path, nargs="?")
    ap.add_argument("--style", choices=["letters", "solfege"], default="letters")
    ap.add_argument("--color", choices=list(COLORS), default="red")
    ap.add_argument("--size", type=float, default=5.4, help="label font size in points")
    a = ap.parse_args()
    out = a.output or a.input.with_name(a.input.stem + "_note_names.pdf")
    try:
        n, unsure = annotate(a.input, out, a.style, a.color, a.size)
    except UnsupportedPDF as e:
        sys.exit(f"Cannot label {a.input}: {e}")
    print(f"{n} notes labelled" + (f", {unsure} marked orange to check" if unsure else "") + f" -> {out}")


if __name__ == "__main__":
    main()
