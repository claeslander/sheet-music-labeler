"""Note names for scanned scores (or photos), using the local homr OMR model.

homr finds the noteheads (segmentation network) and, separately, reads each staff into
a sequence of pitches (transformer). The transformer output carries no reliable
positions, so we align that sequence with the detected noteheads by matching vertical
positions. Every note is also read a second time without the model: from its position,
the clef, the printed key signature and the accidentals we detect ourselves. Notes where
the two readings disagree (or that only one of them saw) are marked orange: "please check".

Usage: python omr_label.py input.pdf|png|jpg|tif|... [output.pdf] [--style letters|solfege] [--color red|...]
Expect ~25 s per page on a CPU.
"""
import argparse
import hashlib
import io
import itertools
import os
import re
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np
import pymupdf

import audiveris as av
import note_names as nn

os.environ.setdefault("ORT_LOGGING_LEVEL", "3")

LIFT = {"#": 1, "##": 2, "b": -1, "bb": -2, "N": 0, "_": 0, ".": 0}
CLEF_REF = {"G": 4 * 7 + 4, "F": 3 * 7 + 3, "C": 4 * 7 + 0}  # G4, F3, C4: the pitch on the clef's line
SHARP_ORDER, FLAT_ORDER = [3, 0, 4, 1, 5, 2, 6], [6, 2, 5, 1, 4, 0, 3]  # F C G D A E B / B E A D G C F
SKIP = 1.5  # alignment cost of an unmatched note (in half staff spaces)
DPI = 300
MAX_SIDES = (2500, 3600)  # px: no page is rendered bigger than an A4 page at 300 dpi
IMAGE_TYPES = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp")


def _homr():
    """Import homr lazily: it is heavy and only needed for scanned scores."""
    import homr.main as hm
    from homr.staff_parsing import parse_staffs
    from homr.transformer.configs import Config

    hm.autocrop = lambda img: img  # keep the page geometry so we can map positions back
    return hm, parse_staffs, Config


def recognise(png):
    """Run homr on one page image. Returns (systems, image_shape, masks).

    systems: list of (staff, tokens), staff is a homr Staff (5 or 10 lines), tokens its symbols.
    masks: homr's per-pixel segmentation of noteheads and of clefs/keys/accidentals,
    in the same coordinates.
    """
    hm, parse_staffs, Config = _homr()
    cfg = hm.ProcessingConfig(False, False, False, False, -1, False, False, False)
    hm.download_weights(False, False, False)
    captured = {}
    load = hm.load_and_preprocess_predictions

    def keep_predictions(*args, **kwargs):
        result = load(*args, **kwargs)
        captured["pred"] = result[0]
        return result

    hm.load_and_preprocess_predictions = keep_predictions
    try:
        multi, image, debug, _title = hm.detect_staffs_in_image(str(png), cfg)
    finally:
        hm.load_and_preprocess_predictions = load
    config = Config()
    config.use_gpu_inference = False
    voices = parse_staffs(debug, multi, image, config)
    systems = []
    for v, tokens in enumerate(voices):
        staffs = [m.staffs[v] for m in multi if len(m.staffs) > v]
        segments, cur = [], []
        for t in tokens:
            if t.rhythm == "newline":
                segments.append(cur)
                cur = []
            else:
                cur.append(t)
        if cur:
            segments.append(cur)
        if len(segments) != len(staffs):
            segments = match_segments(segments, staffs)
        systems += list(zip(staffs, segments))
    pred = captured["pred"]
    return systems, image.shape, {"notehead": pred.notehead, "accidental": pred.clefs_keys}


def match_segments(segments, staffs):
    """The model's output, one segment per staff, when it skipped a staff or two: the staffs those
    segments belong to are the ones whose notehead counts fit their note counts best. The skipped
    staffs get no segment (they are read by geometry alone); with more missing, all of them."""
    missing = len(staffs) - len(segments)
    if not 0 < missing <= 2:
        return [[] for _ in staffs]
    notes = [sum(t.rhythm.startswith("note") for t in seg) for seg in segments]
    heads = [len(st.get_notes()) for st in staffs]
    best = min(itertools.combinations(range(len(staffs)), missing),
               key=lambda skip: sum(abs(n - h) for n, h in zip(notes, [h for i, h in enumerate(heads)
                                                                       if i not in skip])))
    it = iter(segments)
    return [[] if i in best else next(it) for i in range(len(staffs))]


def bottom_of(clef):
    """Diatonic step of the bottom staff line for a homr clef token like clef_G2 / clef_F4."""
    m = re.match(r"clef_([GFC])(\d)", clef)
    return CLEF_REF[m.group(1)] - 2 * (int(m.group(2)) - 1) if m else None


def events_of(tokens, key=0):
    """Group the token stream into time events (notes joined by 'chord'), tracking clefs,
    the key signature (homr only writes it where it is printed, so it is passed in from
    the previous system) and the bar number within the system."""
    clef = {"upper": nn.BOTTOM_LINE["treble"], "lower": nn.BOTTOM_LINE["bass"]}
    events, join, bar, time_sig = [], False, 0, False
    for t in tokens:
        r = t.rhythm
        if r == "chord":
            join = True
            continue
        if r == "barline":
            bar += 1
        if r.startswith("clef"):
            b = bottom_of(r)
            if b is not None:
                clef[t.position if t.position in clef else "upper"] = b
        elif r.startswith("keySignature_"):
            key = int(r.split("_")[1])
        elif r.startswith("timeSignature"):
            time_sig = True
        elif r.startswith("note") or r.startswith("rest"):
            item = None
            m = re.match(r"([A-G])(-?\d)", t.pitch or "")
            if r.startswith("note") and m:
                half = t.position if t.position in clef else "upper"
                d = "CDEFGAB".index(m.group(1)) + 7 * int(m.group(2))
                item = {"half": half, "step": d - clef[half], "pitch": d, "alter": LIFT.get(t.lift, 0)}
            if join and events:
                events[-1]["notes"].append(item)
            else:
                events.append({"notes": [item], "clef": dict(clef), "key": key, "bar": bar, "time_before": time_sig})
                time_sig = False
            join = False
            continue
        join = False
    kept, carry = [], False
    for e in events:
        e["notes"] = [n for n in e["notes"] if n]
        carry = carry or e["time_before"]  # a rest after the time signature passes the mark on
        if e["notes"]:
            e["time_before"], carry = carry, False
            kept.append(e)
    if (carry or time_sig) and kept:
        kept[-1]["time_after"] = True  # time signature at the end of the line
    return kept, key


def time_signature_zones(events, heads):
    """x ranges where the model read a time signature: between the noteheads matched to the
    event before it and those matched to the event after it. Digits there look like noteheads
    to the segmentation, so unmatched heads in these ranges are dropped."""
    xs = {}
    for h in heads:
        if "event" in h:
            xs.setdefault(id(h["event"]), []).append(h["x"])
    zones = []
    for i, e in enumerate(events):
        if e.get("time_before"):
            left = next((max(xs[id(p)]) for p in reversed(events[:i]) if id(p) in xs), float("-inf"))
            right = next((min(xs[id(n)]) for n in events[i:] if id(n) in xs), float("inf"))
            zones.append((left, right))
        if e.get("time_after"):
            zones.append((max(xs.get(id(e), [float("-inf")])), float("inf")))
    return zones


def lines_at(staff, x):
    """Staff line y positions at x, interpolated between homr's grid points
    (the grid has gaps where beams or chords cover the lines)."""
    grid = staff.grid
    right = next((i for i, p in enumerate(grid) if p.x >= x), len(grid) - 1)
    left = max(right - 1, 0)
    a, b = grid[left], grid[right]
    t = 0.0 if b.x == a.x else min(max((x - a.x) / (b.x - a.x), 0.0), 1.0)
    return sorted(ya + t * (yb - ya) for ya, yb in zip(sorted(a.y), sorted(b.y)))


def staff_unit(staff):
    """Distance between two staff lines (homr's own average is skewed on grand staffs)."""
    ys = sorted(staff.grid[len(staff.grid) // 2].y)
    return (ys[4] - ys[0]) / 4


def split_noteheads(mask, unit):
    """Notehead centres from the segmentation mask. Chord noteheads touch and form one blob,
    so each blob is peeled apart with a notehead-shaped template, best fit first."""
    rx, ry = max(2, round(0.62 * unit)), max(2, round(0.45 * unit))
    kernel = np.zeros((2 * ry + 1, 2 * rx + 1), np.uint8)
    cv2.ellipse(kernel, (rx, ry), (rx, ry), 0, 0, 360, 1, -1)
    kernel_f = kernel.astype(np.float32) / kernel.sum()
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8))
    centres = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < 0.35 * kernel.sum():
            continue
        x0, y0 = max(x - rx, 0), max(y - ry, 0)
        crop = (labels[y0:y + h + ry, x0:x + w + rx] == i).astype(np.float32)
        while True:
            cover = cv2.filter2D(crop, -1, kernel_f, borderType=cv2.BORDER_CONSTANT)
            cy, cx = np.unravel_index(np.argmax(cover), cover.shape)
            if cover[cy, cx] < 0.5:
                break
            centres.append((x0 + cx, y0 + cy))
            # erase a slightly larger ellipse so the next-best fit is a different notehead
            cv2.ellipse(crop, (int(cx), int(cy)), (int(rx * 1.05), int(ry * 1.1)), 0, 0, 360, 0, -1)
    return centres


def heads_of(staff, centres=None):
    """Detected noteheads with their half (upper/lower staff) and step above that half's bottom line.

    centres: notehead centres from split_noteheads (already assigned to this staff); without
    them homr's own notehead list is used."""
    heads = []
    unit0 = staff_unit(staff)
    points = centres if centres is not None else [n.center for n in staff.get_notes()]
    for x, y in points:
        ys = lines_at(staff, x)
        if len(ys) >= 10:
            mid = (ys[4] + ys[5]) / 2
            lines = ys[:5] if y < mid else ys[5:10]
            half = "upper" if y < mid else "lower"
        else:
            lines, half = ys[:5], "upper"
        unit = (lines[-1] - lines[0]) / 4 or unit0
        heads.append({"x": float(x), "y": float(y), "half": half, "unit": unit, "w": 1.3 * unit,
                      "step": round((lines[-1] - y) / (unit / 2))})
    heads.sort(key=lambda h: h["x"])
    return heads


def clusters_of(heads):
    out = []
    for h in heads:
        if out and h["x"] - out[-1][0]["x"] < 1.2 * h["unit"]:
            out[-1].append(h)
        else:
            out.append([h])
    return out


def step_cost(a, b):
    """How badly a model note and a notehead differ in height; exactly an octave apart is a
    small cost, not a mismatch (the model reads 8va passages at sounding pitch)."""
    d = abs(a - b)
    return min(d, 3, 0.25 + abs(d - 7))


def pair_cost(tokens, heads):
    """Best monotonic pairing of tokens and noteheads by step (per staff half). Returns cost, pairs."""
    total, pairs = 0.0, []
    for half in ("upper", "lower"):
        a = sorted([t for t in tokens if t["half"] == half], key=lambda t: t["step"])
        b = sorted([h for h in heads if h["half"] == half], key=lambda h: h["step"])
        n, m = len(a), len(b)
        D = [[0.0] * (m + 1) for _ in range(n + 1)]
        for i in range(1, n + 1):
            D[i][0] = i * SKIP
        for j in range(1, m + 1):
            D[0][j] = j * SKIP
        for i in range(1, n + 1):
            for j in range(1, m + 1):
                D[i][j] = min(D[i - 1][j] + SKIP, D[i][j - 1] + SKIP,
                              D[i - 1][j - 1] + step_cost(a[i - 1]["step"], b[j - 1]["step"]))
        total += D[n][m]
        i, j = n, m
        while i and j:
            if D[i][j] == D[i - 1][j - 1] + step_cost(a[i - 1]["step"], b[j - 1]["step"]):
                pairs.append((a[i - 1], b[j - 1]))
                i, j = i - 1, j - 1
            elif D[i][j] == D[i - 1][j] + SKIP:
                i -= 1
            else:
                j -= 1
    return total, pairs


def align(events, clusters):
    """Monotonic alignment of time events to x-clusters of noteheads (one event may span two clusters)."""
    n, m = len(events), len(clusters)
    INF = float("inf")
    D = [[INF] * (m + 1) for _ in range(n + 1)]
    back = [[None] * (m + 1) for _ in range(n + 1)]
    D[0][0] = 0
    for i in range(n + 1):
        for j in range(m + 1):
            if D[i][j] == INF:
                continue
            if i < n and D[i][j] + SKIP * len(events[i]["notes"]) < D[i + 1][j]:
                D[i + 1][j] = D[i][j] + SKIP * len(events[i]["notes"])
                back[i + 1][j] = (i, j, None)
            if j < m and D[i][j] + SKIP * len(clusters[j]) < D[i][j + 1]:
                D[i][j + 1] = D[i][j] + SKIP * len(clusters[j])
                back[i][j + 1] = (i, j, None)
            for span in (1, 2):
                if i < n and j + span <= m:
                    heads = [h for c in clusters[j:j + span] for h in c]
                    c, pairs = pair_cost(events[i]["notes"], heads)
                    c += 0.5 * (span - 1)
                    if D[i][j] + c < D[i + 1][j + span]:
                        D[i + 1][j + span] = D[i][j] + c
                        back[i + 1][j + span] = (i, j, (pairs, events[i], heads))
    matches, i, j = [], n, m
    while (i, j) != (0, 0):
        pi, pj, info = back[i][j]
        if info:
            matches.append(info)
        i, j = pi, pj
    return matches[::-1]


def key_alter(letter, key):
    order = SHARP_ORDER if key > 0 else FLAT_ORDER
    return (1 if key > 0 else -1) if letter in order[:abs(key)] else 0


def synthetic_head(staff, tok, heads):
    """A notehead the detector missed but the model read, placed in the chord it belongs to."""
    x = sum(h["x"] for h in heads) / len(heads)
    ys = lines_at(staff, x)
    lines = ys[5:10] if tok["half"] == "lower" and len(ys) >= 10 else ys[:5]
    unit = (lines[-1] - lines[0]) / 4
    return {"x": x, "y": lines[-1] - tok["step"] * unit / 2, "half": tok["half"], "unit": unit,
            "w": heads[0]["w"], "step": tok["step"]}


def assign_to_staffs(centres, staffs):
    """Give each notehead centre to the nearest staff (ledger notes included)."""
    out = [[] for _ in staffs]
    for x, y in centres:
        best = None
        for i, st in enumerate(staffs):
            u = staff_unit(st)
            if st.min_x - u <= x <= st.max_x + u and st.min_y - 6 * u <= y <= st.max_y + 6 * u:
                d = abs(y - (st.min_y + st.max_y) / 2)
                if best is None or d < best[0]:
                    best = (d, i)
        if best:
            out[best[1]].append((x, y))
    return out


def label_page(systems, masks, key=0):
    """Note dicts (image coordinates) for every staff on a page, and the key signature
    in force at the end of the page (to pass on to the next page)."""
    if not systems:
        return [], key
    unit = float(np.median([staff_unit(st) for st, _ in systems]))
    staffs = [st for st, _ in systems]
    centres = assign_to_staffs(split_noteheads(masks["notehead"], unit), staffs)
    accs = find_accidentals(masks["accidental"], unit)
    acc_per_staff = assign_to_staffs([(x, y) for x, y, _ in accs], staffs)
    acc_lookup = {(x, y): a for x, y, a in accs}
    notes = []
    for idx, ((staff, tokens), cs, ac) in enumerate(zip(systems, centres, acc_per_staff)):
        system_notes, key = label_system(staff, tokens, cs, [(x, y, acc_lookup[(x, y)]) for x, y in ac], key)
        for n in system_notes:
            n["system"] = idx
            notes.append(n)
    return notes, key


def find_accidentals(mask, unit):
    """Sharps, flats and naturals in homr's clef/key/accidental mask, told apart by where
    their left and right strokes start and end (rules measured on engraved scores; the
    mask often clips a natural's bottom, so the rules avoid relying on that):
    flat: left stem runs to the bottom, the bowl on the right stops above it;
    sharp: both strokes start at the top;
    natural: right stroke runs to the bottom but starts lower than the left one.
    Returns (x_right, y_anchor, alter); y_anchor is the height of the notehead it belongs to."""
    out = []
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8))
    for i in range(1, n):
        x, y, w, h, _ = stats[i]
        if not (1.4 * unit <= h <= 3.5 * unit and 0.4 * unit <= w <= 1.7 * unit):
            continue  # clefs and merged pairs are bigger, dots and fragments smaller
        comp = labels[y:y + h, x:x + w] == i
        third = max(1, w // 3)
        left = np.where(comp[:, :third].any(1))[0]
        right = np.where(comp[:, -third:].any(1))[0]
        if len(left) == 0 or len(right) == 0:
            continue
        l_top, l_bot = left.min() / h, (left.max() + 1) / h
        r_top, r_bot = right.min() / h, (right.max() + 1) / h
        if l_bot > 0.95 and r_top > 0.4 and (r_bot < 0.88 or w > 1.25 * unit):
            out.append((x + w, y + h - 0.75 * unit, -2 if w > 1.25 * unit else -1))
        elif l_top < 0.2 and r_top < 0.2:
            out.append((x + w, y + 1.45 * unit, 1))
        elif r_bot > 0.85 and r_top >= 0.2:
            out.append((x + w, y + 1.37 * unit, 0))
    return out


def attach_accidentals(heads, accidentals):
    """Give each accidental to the nearest notehead to its right on the same line/space.
    Returns the accidentals that belong to no notehead (key signatures)."""
    loose = []
    for ax, ay, alter in accidentals:
        best = None
        for h in heads:
            gap = h["x"] - h["w"] / 2 - ax
            if -0.3 * h["unit"] <= gap <= 4 * h["unit"] and abs(h["y"] - ay) < 0.4 * h["unit"]:
                if best is None or gap < best[0]:
                    best = (gap, h)
        if best:
            best[1]["drawn_acc"] = alter
        else:
            loose.append((ax, ay, alter))
    return loose


def read_key(staff, half, signs):
    """Number of flats (negative) or sharps in a key signature. A signature is always the
    start of the order B E A D G C F (flats) or F C G D A E B (sharps), so the sign furthest
    along that order gives the count, even when the detector missed one in between."""
    flats = [(ax, ay) for ax, ay_, a in [(s[0], s[2], s[1]) for s in signs] if a < 0 for ay in [ay_]]
    sharps = [(s[0], s[2]) for s in signs if s[1] > 0]
    use, order, sign = (flats, FLAT_ORDER, -1) if len(flats) >= len(sharps) else (sharps, SHARP_ORDER, 1)
    if not use:
        return 0  # only naturals: the key is cancelled
    furthest = 0
    for ax, ay in use:
        ys = lines_at(staff, ax)
        lines = ys[5:10] if half == "lower" and len(ys) >= 10 else ys[:5]
        step = round((lines[-1] - ay) / ((lines[-1] - lines[0]) / 8))
        bottom = nn.BOTTOM_LINE["bass" if half == "lower" else "treble"]
        letter = (bottom + step) % 7
        if letter in order:
            furthest = max(furthest, order.index(letter) + 1)
    return sign * max(furthest, len(use))


def key_signatures(staff, loose, first_note_x):
    """Key signatures per staff half as [(x, key)], read from groups of loose accidentals:
    a group of two or more, or any group before the first note (a one-flat/sharp key).
    key: negative = number of flats, positive = number of sharps, 0 = all cancelled."""
    out = {"upper": [], "lower": []}
    for half in out:
        accs = []
        for ax, ay, alter in loose:
            ys = lines_at(staff, ax)
            if len(ys) >= 10:
                if (ay < (ys[4] + ys[5]) / 2) != (half == "upper"):
                    continue
            elif half == "lower":
                continue
            accs.append((ax, alter, ay))
        accs.sort()
        groups, unit = [], staff_unit(staff)
        for acc in accs:
            ax = acc[0]
            # everything before the first note is one signature, even if a sign was missed
            same_start = groups and ax < first_note_x and groups[-1][-1][0] < first_note_x
            if groups and (same_start or ax - groups[-1][-1][0] < 1.6 * unit):
                groups[-1].append(acc)
            else:
                groups.append([acc])
        for g in groups:
            if len(g) < 2 and g[0][0] > first_note_x:
                continue
            out[half].append((g[0][0], read_key(staff, half, g)))
    return out


def label_system(staff, tokens, centres=None, accidentals=(), key=0):
    """Return note dicts (image coordinates) ready for note_names.layout.

    Every note is read twice: by homr's transformer (pitch incl. accidental) and by
    geometry (notehead position + clef, and the accidentals we find ourselves + key
    signature + barlines). Where the two disagree the note gets "check": True.
    Returns (notes, key signature at the end of the system)."""
    heads = heads_of(staff, centres)
    loose = attach_accidentals(heads, accidentals)
    first_x = min((h["x"] for h in heads), default=staff.max_x)
    printed_keys = key_signatures(staff, loose, first_x)
    events, end_key = events_of(tokens, key)
    # the printed key signature beats the model's (homr tends to repeat the first one)
    geo_key = {half: (ks[0][1] if ks and ks[0][0] < first_x else key) for half, ks in printed_keys.items()}
    for pairs, ev, cluster in align(events, clusters_of(heads)):
        for tok, h in pairs:
            h["event"], h["tok"] = ev, tok
        paired = {id(t) for t, _ in pairs}
        for tok in ev["notes"]:
            if id(tok) not in paired and cluster:
                h = synthetic_head(staff, tok, cluster)
                if h:
                    h.update(event=ev, tok=tok, synthetic=True)
                    heads.append(h)
    zones = time_signature_zones(events, heads)
    heads = [h for h in heads if "tok" in h or not any(lo < h["x"] < hi for lo, hi in zones)]
    heads.sort(key=lambda h: h["x"])
    ctx = events[0] if events else {"clef": {"upper": nn.BOTTOM_LINE["treble"], "lower": nn.BOTTOM_LINE["bass"]},
                                    "key": key, "bar": 0}
    measure, bar = {}, 0
    notes = []
    for h in heads:
        ctx = h.get("event", ctx)
        if ctx["bar"] != bar:
            measure, bar = {}, ctx["bar"]  # accidentals only last until the barline
        for kx, k in printed_keys[h["half"]]:
            if kx < h["x"]:
                geo_key[h["half"]] = k
        geo = ctx["clef"][h["half"]] + h["step"]
        if "drawn_acc" in h:
            measure[(h["half"], geo)] = h["drawn_acc"]
        h["geo_pitch"] = geo
        h["geo_alter"] = measure.get((h["half"], geo), key_alter(geo % 7, geo_key[h["half"]]))
        tok = h.get("tok")
        if tok:
            h["pitch"], h["alter"] = tok["pitch"], tok["alter"]
            if abs(h["step"] - tok["step"]) == 1:  # the notehead's centre was a little off: trust the model
                h["y"] += (h["step"] - tok["step"]) * h["unit"] / 2
            # labels show no octave, so an octave apart (an 8va passage the model reads as sounding) agrees
            h["check"] = not h.get("synthetic") and (tok["pitch"] % 7, tok["alter"]) != (geo % 7, h["geo_alter"])
        else:
            h["pitch"], h["alter"], h["uncertain"] = geo, h["geo_alter"], True
        h["letter"] = h["pitch"] % 7
        notes.append(h)
    if printed_keys["upper"]:
        end_key = printed_keys["upper"][-1][1]
    return notes, end_key


def apply_audiveris(notes, aud, shape):
    """Use Audiveris (a third, independent reader) on one page's notes:
    - a notehead only our geometry saw, where Audiveris has none either, is dropped (fingerings,
      hairpins and ottava lines leak into homr's notehead mask; Audiveris classifies them);
    - a note marked only because the model and our geometry disagree by one accidental step
      (e.g. ♮ against ♯) is unmarked when Audiveris reads exactly what the model read. On the test
      piece the model was right in all of those cases; other disagreements stay marked."""
    an, (aw, ah) = aud
    fx, fy = shape[1] / aw, shape[0] / ah
    out = []
    for n in notes:
        a = min((a for a in an if abs(a["x"] * fx - n["x"]) < 0.8 * n["unit"] and abs(a["y"] * fy - n["y"]) < 0.6 * n["unit"]),
                key=lambda a: abs(a["y"] * fy - n["y"]), default=None)
        if a is None and "tok" not in n:
            continue
        if (n.get("check") and a and (a["letter"], a["alter"]) == (n["pitch"] % 7, n["alter"])
                and n["geo_pitch"] % 7 == n["pitch"] % 7 and abs(n["geo_alter"] - n["alter"]) == 1):
            n["check"] = False
        out.append(n)
    return out


def open_as_pdf(src):
    """Open a PDF, or turn an image (every page of a TIFF) into a PDF so we can draw on it."""
    src = Path(src)
    if src.suffix.lower() not in IMAGE_TYPES:
        return pymupdf.open(src)
    try:
        return pymupdf.open("pdf", pymupdf.open(src).convert_to_pdf())
    except (RuntimeError, ValueError):  # a type PyMuPDF doesn't read (WebP): convert with Pillow
        from PIL import Image, ImageSequence
        doc = pymupdf.open()
        for frame in ImageSequence.Iterator(Image.open(src)):
            buf = io.BytesIO()
            frame.convert("RGB").save(buf, "PNG")
            page = doc.new_page(width=frame.width, height=frame.height)
            page.insert_image(page.rect, stream=buf.getvalue())
        return doc


def render(page, path):
    """Save the page as a greyscale image for the OMR model: a scanned page at the resolution of
    its image, anything else at 300 dpi, but no bigger than an A4 page at 300 dpi. (An image file
    without a resolution counts as 72 dpi: 300 dpi would make a photo's page huge.)"""
    zoom = DPI / 72
    images = page.get_image_info()
    if len(images) == 1 and abs(pymupdf.Rect(images[0]["bbox"]) & page.rect) > 0.8 * abs(page.rect):
        zoom = images[0]["width"] / pymupdf.Rect(images[0]["bbox"]).width  # the image's own pixels
    short, long_ = sorted((page.rect.width, page.rect.height))
    zoom = min(zoom, MAX_SIDES[0] / short, MAX_SIDES[1] / long_)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY)
    img = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.stride)[:, :pix.w]
    if os.environ.get("OMR_NO_FLATTEN"):
        cv2.imwrite(str(path), img)
        return
    cv2.imwrite(str(path), even_light(img))


def even_light(img):
    """Even out uneven lighting (a photo, a shadow, a yellowed page): divide by the paper's own
    brightness, estimated by spreading the light parts over the ink (which is never wider than
    a beam), so paper comes out white everywhere and ink keeps its contrast."""
    k = max(15, min(img.shape) // 40) | 1
    paper = cv2.dilate(img, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    paper = cv2.GaussianBlur(paper, (0, 0), k)
    out = img.astype(np.float32) / np.maximum(paper.astype(np.float32), 1) * 255
    return np.clip(out, 0, 255).astype(np.uint8)


_cache = {}  # file hash -> recognised notes per page; restyling must not re-run the model


def read_notes(src, progress=print):
    """Recognise every page once. Returns a list (per page) of note dicts in PDF coordinates."""
    digest = hashlib.sha1(Path(src).read_bytes()).hexdigest()
    if digest in _cache:
        return _cache[digest]
    pages, key, read = [], 0, []
    with tempfile.TemporaryDirectory() as tmp:
        doc = open_as_pdf(src)
        aud_src = src
        if Path(src).suffix.lower() in (".bmp", ".gif", ".webp"):  # image types Audiveris doesn't read
            aud_src = Path(tmp, "converted.png")
            render(doc[0], aud_src)
        try:
            aud_proc = av.start(aud_src, tmp)
        except (av.Unavailable, OSError):
            aud_proc = None  # without Audiveris: homr and our geometry only
        try:
            for page in doc:
                png = Path(tmp, f"p{page.number + 1}.png")
                render(page, png)
                progress(f"page {page.number + 1}: recognising…")
                try:
                    systems, shape, masks = recognise(png)
                except Exception as e:  # homr raises on pages without music (title pages, text)
                    progress(f"page {page.number + 1}: skipped ({e})")
                    systems, shape, masks = [], (1, 1), None
                page_notes, key = label_page(systems, masks, key)
                read.append((page_notes, shape))
        except BaseException:
            if aud_proc:
                aud_proc.kill()
            raise
        aud_pages = {}
        if aud_proc:
            progress("checking with Audiveris…")
            try:
                aud_pages = av.finish(aud_proc, tmp)
            except Exception as e:  # never let the extra check break the result
                progress(f"Audiveris check skipped ({e})")
        for page, (page_notes, shape) in zip(doc, read):
            if page.number + 1 in aud_pages:
                page_notes = apply_audiveris(page_notes, aud_pages[page.number + 1], shape)
            sx, sy = page.rect.width / shape[1], page.rect.height / shape[0]
            notes = []
            for n in page_notes:
                notes.append({"staff": n["system"] * 2 + (n["half"] == "lower"), "small": False,
                              "x": (n["x"] - n["w"] / 2) * sx, "y": n["y"] * sy, "w": n["w"] * sx,
                              "unit": n["unit"] * sy, "letter": n["letter"], "alter": n["alter"],
                              "pitch": n["pitch"],
                              # not sure: the model and the geometric reading disagree, or only geometry saw it
                              "uncertain": bool(n.get("uncertain") or n.get("check"))})
            pages.append(notes)
    _cache[digest] = pages
    return pages


def annotate(src, dst, style="letters", color="red", fontsize=5.4, progress=print, edits=None, labels=None, legend=True):
    """Like note_names.annotate, but returns (notes, notes not marked orange)."""
    names = nn.SOLFEGE if style == "solfege" else nn.LETTERS
    rgb = nn.color_rgb(color)
    edits = edits or {}
    pages = read_notes(src, progress)
    doc = open_as_pdf(src)
    confirmed = 0
    for page, notes in zip(doc, pages):
        for n in notes:  # label size follows the staff size of the scan
            n["fs"] = fontsize / 5.0 * n["unit"]
        boxes = []
        if notes:
            labelled, changed = nn.layout(notes, [], fontsize, names), edits.get(page.number, {})
            boxes = nn.draw_labels(page, labelled, rgb, changed)
            confirmed += nn.checked(labelled, changed)
        if labels is not None:
            labels.setdefault("pages", []).append(nn.label_boxes(page, boxes))
    total = sum(len(p) for p in pages)
    if total == 0:
        raise nn.UnsupportedPDF("no notes recognised")
    unsure = sum(n["uncertain"] for p in pages for n in p) - confirmed
    box = nn.draw_legend(doc[0], unsure) if unsure and legend else None
    if labels is not None:
        labels["legend"] = box and nn.fraction(doc[0], box)
    doc.save(dst, garbage=3, deflate=True)
    return total, total - unsure


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("input", type=Path)
    ap.add_argument("output", type=Path, nargs="?")
    ap.add_argument("--style", choices=["letters", "solfege"], default="letters")
    ap.add_argument("--color", type=nn.color_arg, default="red", help="red, blue, black or a hex colour like #1a7f37")
    ap.add_argument("--size", type=float, default=5.4, help="label size relative to a 5 pt staff space")
    a = ap.parse_args()
    out = a.output or a.input.with_name(a.input.stem + "_note_names_omr.pdf")
    try:
        total, ok = annotate(a.input, out, a.style, a.color, a.size, progress=lambda s: print(s, file=sys.stderr))
    except nn.UnsupportedPDF as e:
        sys.exit(f"Cannot label {a.input}: {e}")
    print(f"{total} notes labelled, {total - ok} marked orange to check -> {out}")


if __name__ == "__main__":
    main()
