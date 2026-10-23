"""Draw the reference symbols for glyph_shapes.py from a set of music fonts.

python tools/make_glyph_refs.py FONT.otf [FONT.otf ...]

SMuFL fonts are read by code point. LilyPond's Emmentaler (emmentaler-20.otf) is not SMuFL
and is read by glyph name. The fonts used for glyph_refs.npz: Bravura, Leland, Petaluma,
Gootville, MuseJazz, Finale Maestro, Finale Broadway and MuseScore's Emmentaler (from
github.com/musescore/MuseScore/tree/main/fonts), and LilyPond 2.26's emmentaler-20.otf.
"""
import sys
from pathlib import Path

import numpy as np
from fontTools.ttLib import TTFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import glyph_shapes as gs  # noqa: E402

LILYPOND = {
    "notehead": ["noteheads.s2", "noteheads.s1", "noteheads.s0", "noteheads.sM1"],
    "flat": ["accidentals.flat"], "natural": ["accidentals.natural"], "sharp": ["accidentals.sharp"],
    "doublesharp": ["accidentals.doublesharp"], "doubleflat": ["accidentals.flatflat"],
    "gclef": ["clefs.G", "clefs.G_change"], "fclef": ["clefs.F", "clefs.F_change"],
    "cclef": ["clefs.C", "clefs.C_change"], "percussionclef": ["clefs.percussion"], "tabclef": ["clefs.tab"],
    "dot": ["dots.dot"],
    "other": ["rests.0", "rests.1", "rests.2", "rests.3", "rests.4", "flags.u3", "flags.d3", "flags.u4", "flags.d4",
              "flags.u5", "flags.d5", "flags.u6", "flags.d6", "flags.ugrace", "flags.dgrace",
              "timesig.C44", "timesig.C22", "scripts.ufermata", "scripts.sforzato", "scripts.trill",
              "scripts.turn", "scripts.mordent", "scripts.segno", "scripts.coda", "pedal.Ped",
              "noteheads.s2cross", "noteheads.s2diamond", "accidentals.leftparen", "accidentals.rightparen"]
               + "zero one two three four five six seven eight nine".split(),
}


def glyphs_of(path):
    font = TTFont(path)
    upm = font["head"].unitsPerEm
    gset = font.getGlyphSet()
    cmap = font.getBestCmap()
    if "emmentaler" in path.name.lower():
        names = {kind: [n for n in ns if n in gset] for kind, ns in LILYPOND.items()}
    else:
        wanted = dict(gs.SYMBOLS, other=gs.OTHERS)
        names = {kind: [cmap[c] for c in cps if c in cmap] for kind, cps in wanted.items()}
    for kind, ns in names.items():
        for n in ns:
            f = gs._Flatten(gset)
            gset[n].draw(f.pen)
            yield kind, f.contours, upm, f"{path.stem}:{n}"


def main():
    kinds, labels, shapes, origins, chunks, offset, offsets = [], [], [], [], [], 0, []
    for p in sys.argv[1:]:
        count = 0
        for kind, contours, upm, label in glyphs_of(Path(p)):
            r = gs._render(contours, gs.PX * 4 / upm)  # one em is four staff spaces
            if r is None:
                continue
            img, ox, oy = r
            kinds.append(kind)
            labels.append(label)
            shapes.append(img.shape)
            origins.append((ox, oy))
            offsets.append(offset)
            chunks.append(img.ravel())
            offset += img.size
            count += 1
        print(p, count, "symbols")
    np.savez_compressed(gs.REFS_FILE, kinds=np.array(kinds), labels=np.array(labels), shapes=np.array(shapes, np.int32),
                        origins=np.array(origins, np.float32), offsets=np.array(offsets, np.int64),
                        pixels=np.concatenate(chunks).astype(np.uint8))
    print(gs.REFS_FILE, len(kinds), "references,", gs.REFS_FILE.stat().st_size // 1024, "KB")


if __name__ == "__main__":
    main()
