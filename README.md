# Sheet Music Labeler

Writes the name of every note (C D E♭ … or Do Re Mi …) next to the note in a piano score, so you can read sheet music while you are still learning it.

## Install (once)

Needs Python 3.11 or newer. Developed and tested on Debian Linux.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

The first time you label a scan, the OMR model downloads its weights (about 130 MB).

**Optional, for scans:** install [Audiveris](https://github.com/Audiveris/audiveris) as well. It checks the scan as a third reader, which removes names drawn on things that are not notes (fingerings, hairpins, ottava lines) and about a third of the orange marks. On Linux the easiest way is Flathub:

```bash
flatpak install flathub org.audiveris.audiveris
```

On Windows or macOS, install it from its [releases page](https://github.com/Audiveris/audiveris/releases) and make sure the `audiveris` command works, or set the `AUDIVERIS` environment variable to its path. Without Audiveris everything works as before.

## Use the app

1. Start it from the project folder:

   ```bash
   .venv/bin/python app.py
   ```

   Your browser opens http://localhost:8765 (open it yourself if it doesn't). Keep the terminal open while you use the app; Ctrl+C stops it.

2. Add a score: drag a PDF, scan or photo (PDF, PNG or JPG) onto the box on the left, or click the box and pick a file.

3. Wait for the result. It appears on the right:
   - a PDF exported from notation software takes a few seconds;
   - a scan or photo is detected automatically and read by the OMR model, which takes about 10-25 seconds per page (about 25 seconds with Audiveris, which runs at the same time).

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
| How | reads the notation font's glyphs and their exact positions from the PDF | the local [homr](https://github.com/liebharc/homr) OMR model, checked by independent readings (see below) |
| Speed | under a second per page | about 10-25 s per page on a CPU, about 25 s with Audiveris (restyling is instant) |
| Test piece (Rachmaninoff, Élégie op. 3 no. 1, 1,869 notes) | every note | 95.0% of notes named right, 7% marked orange; none of the unmarked names on a note is wrong |

The app tries the exact reader first and falls back to OMR automatically.

### Orange = please check

On scans, every note is read twice, and with Audiveris installed three times:

1. by homr's transformer model, which outputs pitches including accidentals;
2. by geometry, without the model: the notehead's position on the staff, the clef, the key signature read from the printed signs, and sharps/flats/naturals detected and classified in the image, with accidentals lasting until the barline;
3. by [Audiveris](https://github.com/Audiveris/audiveris), whose symbol classifier tells noteheads apart from fingerings, dynamics, hairpins, rests and ottava lines.

When the model and the geometry disagree, or only one of them saw a note, the name is drawn in orange, and page 1 says how many notes to check. Audiveris settles two cases: a notehead that only the geometry saw and Audiveris did not is dropped (it is usually a fingering or a hairpin), and a disagreement of one accidental step (♮ against ♯, say) is not marked when Audiveris reads exactly what the model read. Audiveris does not name notes on its own: on the test piece it names 88% right, against 95% for the model.

On the test piece 7% of the notes get marked (10% without Audiveris). Of the names that are not marked, none on a note is wrong; 16 sit slightly off their note. About 3% of the notes are not detected at all and get no name. A dense 19th-century engraving (Chopin, Sonata op. 58, finale, 14 pages) gets about 11% marked.

## Limits

- The exact reader knows two notation fonts so far: **Aloisen** and **Leland** (MuseScore 4's default). PDFs from Finale, Sibelius, LilyPond, Dorico or MuseScore with another font currently fall back to OMR, until their glyphs are added to `FONT_PROFILES` in `note_names.py`.
- OMR misses clef changes in the middle of a line. Those notes get marked orange.
- Accidentals tied across a barline are not carried over (rare). Double sharps and flats in scans are often read as single ones, and marked orange.
- Names use real ♭ and ♯ signs when a font with them is installed (DejaVu Sans on Linux, Arial Unicode on macOS, Segoe UI Symbol on Windows); otherwise they are written as b and #.

## Files

- `note_names.py`: the exact reader, plus the label layout and drawing shared by both routes
- `omr_label.py`: the scan route (homr, notehead splitting, accidental detection, the check between the readers)
- `audiveris.py`: runs Audiveris and names the noteheads it finds
- `app.py`, `app.html`: the local web app

## License

MIT, see [LICENSE](LICENSE). The libraries it builds on, [PyMuPDF](https://github.com/pymupdf/PyMuPDF) and [homr](https://github.com/liebharc/homr), are licensed under the AGPL-3.0, which applies to them when you redistribute or host the app together with them. Audiveris (also AGPL-3.0) is not included: it is a separate program that the app runs if you have installed it.
