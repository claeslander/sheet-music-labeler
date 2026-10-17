"""Write note names (C, D, E♭, F♯, ... or Do, Re, Mi, ...) next to every note of a score PDF.

Works on PDFs exported from notation software, where every musical symbol is a font
glyph with an exact position, so the result is exact rather than a guess. Scanned
scores (images) need OMR instead, see omr_label.py.

Usage: python note_names.py input.pdf [output.pdf] [--style letters|solfege] [--color red|blue|green|black]
"""
import argparse
import sys
from pathlib import Path

import pymupdf

# Glyph meanings per music font. Only Aloisen (the font in rachman_op3_1.pdf) is mapped so far.
FONT_PROFILES = {
    "Aloisen": {
        "noteheads": set("t|AYZI"),  # filled, half, whole, flagged eighths/sixteenths
        "accidentals": {'"': -1, "!": +1, "#": 0, "%": -2},
        "treble": "Ä",
        "bass": "Å",
        "dot": "«",
    },
}

LETTERS = ["C", "D", "E", "F", "G", "A", "B"]
SOLFEGE = ["Do", "Re", "Mi", "Fa", "Sol", "La", "Si"]
ACC_TEXT = {-2: "♭♭", -1: "♭", 0: "", 1: "♯", 2: "♯♯"}
COLORS = {"red": (0.78, 0.08, 0.12), "blue": (0.05, 0.3, 0.75),
          "green": (0.0, 0.5, 0.25), "black": (0, 0, 0)}

# diatonic step of the bottom staff line, counted from C0 (C0=0, D0=1, ...)
BOTTOM_LINE = {"treble": 4 * 7 + 2, "bass": 2 * 7 + 4}  # E4, G2

LABEL_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf"
CHECK_COLOR = (0.9, 0.42, 0.0)  # orange: "not sure, please check"
NOTEHEAD_W = 6.0 / 20  # notehead width per point of glyph size


class UnsupportedPDF(Exception):
    pass


def find_staves(page):
    """Staves as dicts with the y of the top/bottom line and the line spacing."""
    ys = sorted({round(it[1].y, 1) for d in page.get_drawings() for it in d["items"]
                 if it[0] == "l" and abs(it[1].y - it[2].y) < 0.1 and abs(it[1].x - it[2].x) > 200})
    staves, group = [], []
    for y in ys:
        if group and y - group[-1] > 8:
            staves.append(group)
            group = []
        group.append(y)
    staves.append(group)
    return [{"top": g[0], "bottom": g[-1], "sp": (g[-1] - g[0]) / 4} for g in staves if len(g) == 5]


def find_barlines(page):
    """Barlines as (x, y_top, y_bottom)."""
    return [(it[1].x0, it[1].y0, it[1].y1) for d in page.get_drawings() for it in d["items"]
            if it[0] == "re" and it[1].width < 3 and it[1].height > 15]


def music_glyphs(page):
    """All glyphs drawn in a known music font, plus the profile of that font."""
    out, profile = [], None
    for span in page.get_texttrace():
        name = next((f for f in FONT_PROFILES if f in span["font"]), None)
        if name is None:
            continue
        profile = FONT_PROFILES[name]
        for code, _, origin, bbox in span["chars"]:
            out.append({"g": chr(code), "x": origin[0], "y": origin[1], "size": span["size"],
                        "x1": bbox[2]})
    return out, profile


def nearest_staff(staves, y):
    return min(range(len(staves)), key=lambda i: abs(y - (staves[i]["top"] + staves[i]["bottom"]) / 2))


def step_of(staff, y):
    """Half-spaces above the bottom line (0 = bottom line, 1 = first space, ...)."""
    return round((staff["bottom"] - y) / (staff["sp"] / 2))


def analyse_page(page):
    """Return every note on the page: dict with staff, x, y, size, x1, letter (0-6), alter (-2..2)."""
    staves = find_staves(page)
    glyphs, prof = music_glyphs(page)
    if prof is None:
        return [], []
    bars = find_barlines(page)
    clefs = {prof["treble"]: "treble", prof["bass"]: "bass"}
    accs = prof["accidentals"]

    per_staff = [[] for _ in staves]
    for g in glyphs:
        if g["g"] in prof["noteheads"] or g["g"] in accs or g["g"] in clefs or g["g"] == prof["dot"]:
            g["staff"] = nearest_staff(staves, g["y"])
            per_staff[g["staff"]].append(g)

    notes = []
    for staff, items in zip(staves, per_staff):
        items.sort(key=lambda g: g["x"])
        bar_xs = sorted(x for x, y0, y1 in bars if y0 <= staff["top"] + 1 and y1 >= staff["bottom"] - 1)
        heads = [g for g in items if g["g"] in prof["noteheads"]]

        # a run of accidentals right after a clef is a key signature, even when its last sign
        # happens to sit close to the first note on the same line/space
        key_sig, last, gap = set(), None, 0
        for g in items:
            if g["g"] in clefs:
                last, gap = g["x"], 5 * staff["sp"]  # first sign: within a few spaces of the clef
            elif g["g"] in accs and last is not None and 0 < g["x"] - last < gap:
                key_sig.add(id(g))
                last, gap = g["x"], 1.6 * staff["sp"]  # next signs: about one space apart
            else:
                last = None

        # attach each other accidental to the closest note to its right on the same line/space
        for g in items:
            if g["g"] in accs:
                g["step"] = step_of(staff, g["y"])
                if id(g) in key_sig:
                    g["note"] = None
                    continue
                cands = [n for n in heads if 0 < n["x"] - g["x"] < 30 and step_of(staff, n["y"]) == g["step"]]
                g["note"] = min(cands, key=lambda n: n["x"]) if cands else None
                if g["note"] is not None:
                    g["note"]["acc"] = accs[g["g"]]

        clef, key, measure, bar_i = None, {}, {}, 0
        for g in items:
            while bar_i < len(bar_xs) and bar_xs[bar_i] < g["x"]:
                measure = {}  # accidentals only last until the barline
                bar_i += 1
            if g["g"] in clefs:
                clef = clefs[g["g"]]
            elif g["g"] in accs and g["note"] is None and clef:
                # a free-standing accidental belongs to a key signature (applies to every octave)
                key[(BOTTOM_LINE[clef] + g["step"]) % 7] = accs[g["g"]]
            elif g["g"] in prof["noteheads"] and clef:
                diatonic = BOTTOM_LINE[clef] + step_of(staff, g["y"])
                if "acc" in g:
                    measure[diatonic] = g["acc"]
                g["letter"] = diatonic % 7
                g["alter"] = measure.get(diatonic, key.get(diatonic % 7, 0))
                g["pitch"] = diatonic
                g["w"] = NOTEHEAD_W * g["size"]
                g["small"] = g["size"] < 18  # grace/cue notes
                notes.append(g)
    dots = [g for g in glyphs if g["g"] == prof["dot"]]
    return notes, dots


def chords(notes):
    """Group noteheads on the same staff that sit (almost) on top of each other."""
    groups = []
    for n in sorted(notes, key=lambda n: (n["staff"], n["x"])):
        g = groups[-1] if groups else None
        if g and g[0]["staff"] == n["staff"] and n["x"] - g[-1]["x"] < 1.25 * n["w"] and n["x"] - g[0]["x"] < 2.2 * n["w"]:
            g.append(n)
        else:
            groups.append([n])
    return groups


def layout(notes, dots, fontsize, names):
    """Place a label to the right of each chord, one per notehead, without overlaps.

    A note may carry its own "fs" (font size) and an "uncertain" flag (drawn muted).
    """
    labels = []
    for chord in chords(notes):
        w = chord[0]["w"]
        fs = chord[0].get("fs", fontsize) * (0.75 if chord[0]["small"] else 1)
        right = max(n["x"] + n["w"] for n in chord)
        ys = [n["y"] for n in chord]
        for d in dots:  # augmentation dots sit right of the chord: put the label after them
            if d["staff"] == chord[0]["staff"] and 0 <= d["x"] - right < 0.8 * w and min(ys) - 0.7 * w < d["y"] < max(ys) + 0.7 * w:
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


def draw_labels(page, labels, rgb):
    """Draw labels with a soft white backdrop so they stay legible over stems and beams."""
    font = pymupdf.Font(fontfile=LABEL_FONT)
    page.insert_font(fontname="lbl", fontfile=LABEL_FONT)
    shape = page.new_shape()
    for x, y, text, fs, _ in labels:
        w = font.text_length(text, fs)
        shape.draw_rect(pymupdf.Rect(x - 0.06 * fs, y - fs * 0.45, x + w + 0.06 * fs, y + fs * 0.42))
    shape.finish(fill=(1, 1, 1), color=None, fill_opacity=0.82)
    shape.commit()
    for x, y, text, fs, uncertain in labels:
        page.insert_text((x, y + fs * 0.36), text, fontname="lbl", fontsize=fs,
                         color=CHECK_COLOR if uncertain else rgb)


def draw_check_marks(page, notes):
    """An orange ring around every notehead whose name we are not sure about."""
    shape = page.new_shape()
    for n in notes:
        if n.get("uncertain"):
            w = n["w"]
            shape.draw_oval(pymupdf.Rect(n["x"] - 0.3 * w, n["y"] - 0.62 * w, n["x"] + 1.3 * w, n["y"] + 0.62 * w))
    shape.finish(color=CHECK_COLOR, width=0.7)
    shape.commit()


def draw_legend(page, count):
    """One line at the top of the first page explaining the orange marks."""
    page.insert_font(fontname="lbl", fontfile=LABEL_FONT)
    text = f"Orange = not sure, please check ({count} note{'s' if count != 1 else ''})"
    page.insert_text((page.rect.x0 + 24, page.rect.y0 + 16), text, fontname="lbl", fontsize=7, color=CHECK_COLOR)


def annotate(src, dst, style="letters", color="red", fontsize=5.4):
    doc = pymupdf.open(src)
    if doc.needs_pass:
        raise UnsupportedPDF("this PDF needs a password to open")
    names = SOLFEGE if style == "solfege" else LETTERS
    rgb = COLORS.get(color, COLORS["red"])
    total = 0
    for page in doc:
        notes, dots = analyse_page(page)
        if not notes:
            continue
        total += len(notes)
        draw_labels(page, layout(notes, dots, fontsize, names), rgb)
    if total == 0:
        raise UnsupportedPDF("no notes found: this is probably a scanned score or uses a music font "
                             "that is not mapped yet (known: " + ", ".join(FONT_PROFILES) + ")")
    doc.save(dst, garbage=3, deflate=True)
    return total


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
        n = annotate(a.input, out, a.style, a.color, a.size)
    except UnsupportedPDF as e:
        sys.exit(f"Cannot label {a.input}: {e}")
    print(f"{n} notes labelled -> {out}")


if __name__ == "__main__":
    main()
