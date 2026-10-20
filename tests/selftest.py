"""Check that an installation works from start to finish.

Reads tests/fur_elise.png (the opening of Beethoven's Für Elise, engraved with Verovio) the way the
app reads a scan, and compares every note name with the right one. Run by install.sh; you can also
run it yourself:

    .venv/bin/python tests/selftest.py

Exit code 0: everything works. 1: something is broken. 2: works, but without Audiveris.
"""
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Every note in the image, right hand then left hand, bar by bar
RIGHT = "E D♯ | E D♯ E B D C | A C E A | B E G♯ B | C E E D♯ | E D♯ E B D C | A C E A | B E C B | A"
LEFT = "A E A | E E G♯ | A E A | A E A | E E G♯ | A E A"
EXPECTED = Counter(n for n in (RIGHT + " " + LEFT).split() if n != "|")
ALTER = {-2: "♭♭", -1: "♭", 0: "", 1: "♯", 2: "♯♯"}


def main():
    try:
        import app  # noqa: F401  the web app and the exact reader import cleanly
        import omr_label
    except Exception as e:
        print(f"FAILED: the app does not start: {e!r}")
        return 1

    messages = []
    try:
        pages = omr_label.read_notes(ROOT / "tests" / "fur_elise.png", progress=messages.append)
    except Exception as e:
        print(f"FAILED: reading the test score crashed: {e!r}")
        return 1

    found = Counter("CDEFGAB"[n["letter"]] + ALTER[n["alter"]] for page in pages for n in page)
    if found != EXPECTED:
        missing, extra = EXPECTED - found, found - EXPECTED
        print(f"FAILED: {sum(found.values())} of {sum(EXPECTED.values())} notes read, "
              f"missing {dict(missing) or 'none'}, extra {dict(extra) or 'none'}")
        return 1
    print(f"OK: all {sum(EXPECTED.values())} notes of the test score named correctly")

    if "checking with Audiveris…" not in messages:
        print("Audiveris was not found, so scans are read without it")
        return 2
    skipped = [m for m in messages if m.startswith("Audiveris check skipped")]
    if skipped:
        print(f"Audiveris is installed but did not work: {skipped[0]}")
        return 2
    print("OK: Audiveris checked the test score")
    return 0


if __name__ == "__main__":
    sys.exit(main())
