# sheet-music-labeler

Writes the name of every note (C D E♭ … or Do Re Mi …) next to the note in a piano score, so you can read sheet music while you are still learning it. Runs entirely on your own computer.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # once
.venv/bin/python app.py      # opens http://localhost:8765; drop in a PDF, scan or photo
```

Or from the command line:

```bash
.venv/bin/python notenamen.py score.pdf [out.pdf] [--style solfege] [--color blue] [--size 6]
.venv/bin/python omr_label.py scan.pdf|photo.jpg [out.pdf]
```

## How it reads a score

Note names follow the notation exactly as written: the letter comes from the line or space the note sits on, the ♯/♭/♮ from the key signature and from accidentals earlier in the same bar. A note is never respelled (a B♭ stays B♭, it does not become A♯).

| | `notenamen.py`: exact | `omr_label.py`: scans and photos |
|---|---|---|
| Works on | PDFs exported from notation software | any image of a score |
| How | reads the notation font's glyphs and their exact positions from the PDF | the local [homr](https://github.com/liebharc/homr) OMR model, checked by a second, independent reading (see below) |
| Speed | under a second per page | about 10-25 s per page on a CPU (restyling is instant) |
| Test piece (Rachmaninoff, Élégie op. 3 no. 1, 1,869 notes) | every note | 95.9% of notes right; of the notes *not* marked orange, 99.9% are right |

The app tries the exact reader first and falls back to OMR automatically.

### Orange = please check

On scans, every note is read twice:

1. by homr's transformer model, which outputs pitches including accidentals;
2. by geometry, without the model: the notehead's position on the staff, the clef, the key signature read from the printed signs, and sharps/flats/naturals detected and classified in the image, with accidentals lasting until the barline.

When the two disagree, or only one of them saw a note, the name is drawn in orange with a ring around the notehead, and page 1 says how many notes to check. On the test piece about 10% of the notes get marked. The notes that are not marked are almost always right (2 errors in 1,642). Some notes (about 2%) are not detected at all and get no name.

## Limits

- The exact reader knows one notation font so far: **Aloisen**. PDFs from MuseScore, Finale, Sibelius, LilyPond or Dorico use other fonts and currently fall back to OMR, until their glyphs are added to `FONT_PROFILES` in `notenamen.py`.
- OMR misses clef changes in the middle of a line. Those notes get marked orange.
- Accidentals tied across a barline are not carried over (rare).

## Files

- `notenamen.py`: the exact reader, plus the label layout and drawing shared by both routes
- `omr_label.py`: the scan route (homr, notehead splitting, accidental detection, the two-reader check)
- `app.py`, `app.html`: the local web app
