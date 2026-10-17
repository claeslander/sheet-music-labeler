# sheet-music-labeler

Writes the name of every note (C D E♭ … or Do Re Mi …) next to the note in a piano score, so you can read sheet music while you are still learning it. Runs entirely on your own computer.

## Install (once)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

The first time you label a scan, the OMR model downloads its weights (about 130 MB).

## Use the app

1. Start it from the project folder:

   ```bash
   .venv/bin/python app.py
   ```

   Your browser opens http://localhost:8765 (open it yourself if it doesn't). Keep the terminal open while you use the app; Ctrl+C stops it.

2. Add a score: drag a PDF, scan or photo (PDF, PNG or JPG) onto the box on the left, or click the box and pick a file.

3. Wait for the result. It appears on the right:
   - a PDF exported from notation software takes a few seconds;
   - a scan or photo is detected automatically and read by the OMR model, which takes about 10-25 seconds per page.

4. Adjust it if you like. The score is redrawn straight away (scans are not read again):
   - **Names**: letters (C D E) or solfège (Do Re Mi)
   - **Colour**: red, blue, green or black
   - **Label size**: the size of the names

5. Click **Download labelled PDF**. The file gets `_note_names` added to its name.

For a scan, the status on the left (and the top of page 1 in the PDF) says how many notes are marked orange: check those, the app is not sure about them (see below).

## Use the command line

```bash
.venv/bin/python note_names.py score.pdf                 # PDF from notation software
.venv/bin/python omr_label.py scan.pdf                  # scan or photo (PDF, PNG, JPG)
```

The result is saved next to the input as `score_note_names.pdf` (or `scan_note_names_omr.pdf`); pass a second file name to choose another. Options for both: `--style solfege`, `--color red|blue|green|black`, `--size 6` (label size in points).

## How it reads a score

Note names follow the notation exactly as written: the letter comes from the line or space the note sits on, the ♯/♭/♮ from the key signature and from accidentals earlier in the same bar. A note is never respelled (a B♭ stays B♭, it does not become A♯).

| | `note_names.py`: exact | `omr_label.py`: scans and photos |
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

- The exact reader knows one notation font so far: **Aloisen**. PDFs from MuseScore, Finale, Sibelius, LilyPond or Dorico use other fonts and currently fall back to OMR, until their glyphs are added to `FONT_PROFILES` in `note_names.py`.
- OMR misses clef changes in the middle of a line. Those notes get marked orange.
- Accidentals tied across a barline are not carried over (rare).

## Files

- `note_names.py`: the exact reader, plus the label layout and drawing shared by both routes
- `omr_label.py`: the scan route (homr, notehead splitting, accidental detection, the two-reader check)
- `app.py`, `app.html`: the local web app
