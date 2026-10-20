"""Read scanned scores with Audiveris (https://audiveris.github.io), an OMR engine whose symbol
classifier tells noteheads apart from fingerings, dynamics, hairpins, rests, ottava lines and
time signatures. We use its noteheads (exact boxes) and name them ourselves from its clefs, key
signatures, accidentals (linked to their notehead) and barlines.

Audiveris is a separate install: the `audiveris` command, $AUDIVERIS, or the Flathub app
org.audiveris.audiveris.
"""
import os
import shutil
import subprocess
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

CLEF_REF = {"G": 4 * 7 + 4, "F": 3 * 7 + 3, "C": 4 * 7 + 0}  # G4, F3, C4: the pitch on the clef's line
ALTERS = {"SHARP": 1, "FLAT": -1, "NATURAL": 0, "DOUBLE_SHARP": 2, "DOUBLE_FLAT": -2}
SHARP_ORDER, FLAT_ORDER = [3, 0, 4, 1, 5, 2, 6], [6, 2, 5, 1, 4, 0, 3]  # F C G D A E B / B E A D G C F
FLATPAK_APP = "org.audiveris.audiveris"


class Unavailable(Exception):
    pass


def command(*dirs):
    """The command line that starts Audiveris, giving it access to `dirs` when sandboxed."""
    exe = os.environ.get("AUDIVERIS") or shutil.which("audiveris") or shutil.which("Audiveris")
    if exe:
        return [exe]
    if shutil.which("flatpak"):
        found = subprocess.run(["flatpak", "info", FLATPAK_APP], capture_output=True)
        if found.returncode == 0:
            return ["flatpak", "run", *[f"--filesystem={d}" for d in dirs], FLATPAK_APP]
    raise Unavailable("Audiveris is not installed")


def start(src, workdir):
    """Start Audiveris on a PDF or image in the background (it takes about as long as homr,
    so both run at the same time). Collect the result with finish()."""
    workdir = Path(workdir)
    inp = workdir / ("input" + Path(src).suffix.lower())
    shutil.copyfile(src, inp)
    return subprocess.Popen([*command(workdir), "-batch", "-transcribe", "-save", "-output", str(workdir), "--", str(inp)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def finish(proc, workdir):
    """Wait for Audiveris and read its result: {page number (1-based): (notes, image size)}."""
    proc.wait()
    omr = Path(workdir) / "input.omr"
    if not omr.exists():
        raise Unavailable("Audiveris produced no result")
    return read_omr(omr)


def key_alter(letter, fifths):
    order = SHARP_ORDER if fifths > 0 else FLAT_ORDER
    return (1 if fifths > 0 else -1) if letter in order[:abs(fifths)] else 0


def centre(el):
    b = el.find("bounds")
    x, y, w, h = (float(b.get(k)) for k in "xywh")
    return x + w / 2, y + h / 2, w, h


def read_sheet(xml, carry):
    """Named noteheads of one sheet (page), in the sheet's pixel coordinates.

    carry: clef and key per staff position in the system (0 = top staff), passed from one
    system and page to the next, for systems where Audiveris did not see them."""
    root = ET.fromstring(xml)
    pic = root.find("picture")
    size = float(pic.get("width")), float(pic.get("height"))
    inters = {el.get("id"): el for el in root.iter() if el.get("id") and el.find("bounds") is not None}
    attached = {}  # head id -> accidental
    for rel in root.iter("relation"):
        if rel.find("alter-head") is not None and rel.get("source") in inters:
            shape = inters[rel.get("source")].get("shape")
            if shape in ALTERS:
                attached[rel.get("target")] = ALTERS[shape]

    notes = []
    for s, system in enumerate(root.iter("system")):
        staff_ids = [st.get("id") for part in system.iter("part") for st in part.findall("staff")]
        pos = {sid: i for i, sid in enumerate(staff_ids)}
        per_staff = {sid: [] for sid in staff_ids}
        for el in system.iter():
            sid = el.get("staff")
            if sid in per_staff and el.get("id") in inters:
                per_staff[sid].append(el)
        for sid, items in per_staff.items():
            i = pos[sid]
            clef, fifths = carry.get(i, (None, 0))
            items.sort(key=lambda el: centre(el)[0])
            measure = {}
            for el in items:
                tag, shape = el.tag, el.get("shape") or ""
                if tag == "barline":
                    measure = {}  # accidentals last until the barline
                elif tag == "clef" and shape[:1] in CLEF_REF:
                    clef = (CLEF_REF[shape[0]] + (7 if "8VA" in shape else -7 if "8VB" in shape else 0),
                            int(el.get("pitch")))
                elif tag == "key":
                    fifths = int(el.get("fifths", 0))  # KEY_CANCEL (naturals) has none
                elif tag == "head" and clef:
                    ref, ref_pitch = clef
                    diatonic = ref + ref_pitch - int(el.get("pitch"))  # Audiveris pitch counts downwards
                    if el.get("id") in attached:
                        measure[diatonic] = attached[el.get("id")]
                    x, y, w, h = centre(el)
                    notes.append({"system": s, "staff_pos": i, "x": x, "y": y, "w": w, "h": h,
                                  "pitch": diatonic, "letter": diatonic % 7,
                                  "alter": measure.get(diatonic, key_alter(diatonic % 7, fifths)),
                                  "small": "SMALL" in shape, "grade": float(el.get("ctx-grade") or el.get("grade"))})
            carry[i] = (clef, fifths)
    return notes, size


def read_omr(omr):
    """{page number (1-based): (named noteheads, (width, height) of the page image they refer to)}.
    Pages Audiveris could not read (title pages, text) are missing."""
    pages, carry = {}, {}
    with zipfile.ZipFile(omr) as z:
        names = sorted((n for n in z.namelist() if n.startswith("sheet#") and n.endswith(".xml")),
                       key=lambda n: int(n.split("#")[1].split("/")[0]))
        for n in names:
            pages[int(n.split("#")[1].split("/")[0])] = read_sheet(z.read(n), carry)
    return pages


def read(src):
    with tempfile.TemporaryDirectory() as tmp:
        return finish(start(src, tmp), tmp)
