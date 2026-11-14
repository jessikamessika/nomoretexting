#!/usr/bin/env python3
"""Shrink a WhatsApp "Export chat → Include media" zip to what call-brief needs:
chat messages and voice notes from the last N days (default 4). Nothing older is kept.
Photos, videos, stickers and documents are dropped.

Runs anywhere with Python 3 (stdlib only), e.g. Termux on Android:
    python trim_export.py "WhatsApp Chat with Dan.zip"            # messages + voice notes from the last 4 days
    python trim_export.py export.zip --days 0                      # keep everything
    python trim_export.py export.zip --out-dir ~/storage/shared/call-brief
Prints the path of the trimmed zip on the last line of output.
"""
from __future__ import annotations

import argparse
import re
import sys
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

AUDIO_EXT = (".opus", ".ogg", ".oga", ".m4a", ".mp3", ".aac", ".amr", ".wav")
# Voice notes: PTT-YYYYMMDD-WA####.opus, e.g. PTT-20261106-WA0000.opus = 6 Nov 2026
AUDIO_DATE = re.compile(r"^PTT-(\d{4})(\d{2})(\d{2})-WA\d+")
# Chat lines: DD/MM/YY HH:MM - Name: text, e.g. "12/11/26 23:54 - Jessi Messi: hi" = 12 Nov 2026.
# Lines that don't match are continuations of the previous multi-line message.
MSG_START = re.compile(r"^(\d{2})/(\d{2})/(\d{2}) \d{2}:\d{2} - ")
OUTPUT_PREFIX = "call-brief_"
# Alberta time, fixed UTC-6 all year. Used for "today" so the cutoff doesn't depend on the device clock.
TZ = timezone(timedelta(hours=-6))


def _date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def file_date(name: str) -> date | None:
    m = AUDIO_DATE.match(name)
    return _date(int(m[1]), int(m[2]), int(m[3])) if m else None


def trim_chat(text: str, oldest: date) -> str:
    """Keep only messages dated on or after `oldest`. Undated lines follow their message."""
    out, keeping = [], False
    for line in text.splitlines(keepends=True):
        m = MSG_START.match(line)
        if m:
            d = _date(2000 + int(m[3]), int(m[2]), int(m[1]))
            keeping = d is not None and d >= oldest
        if keeping:
            out.append(line)
    return "".join(out)


def keep(name: str, oldest: date | None) -> bool:
    base = Path(name).name
    if base.startswith("._") or "__MACOSX" in name:
        return False
    if base.lower().endswith(".txt"):
        return True
    if not base.lower().endswith(AUDIO_EXT):
        return False
    d = file_date(base)
    return oldest is None or (d is not None and d >= oldest)  # drop audio whose date we can't read


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("zip", type=Path)
    ap.add_argument("--days", type=int, default=4,
                    help="keep messages and voice notes from the last N days (default 4; 0 = all)")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="where to write the trimmed zip (default: next to the input)")
    args = ap.parse_args()

    oldest = datetime.now(TZ).date() - timedelta(days=args.days) if args.days > 0 else None
    out_dir = (args.out_dir or args.zip.parent).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{OUTPUT_PREFIX}{datetime.now(TZ):%Y-%m-%d_%H%M%S}.zip"

    try:
        src = zipfile.ZipFile(args.zip)
    except (zipfile.BadZipFile, FileNotFoundError) as e:
        sys.exit(f"Can't open {args.zip}: {e}")

    kept = audio = 0
    with src, zipfile.ZipFile(out, "w") as dst:
        for info in src.infolist():
            if info.is_dir() or not keep(info.filename, oldest):
                continue
            is_txt = info.filename.lower().endswith(".txt")
            # Audio is already compressed; only deflate the text.
            data = src.read(info)
            if is_txt and oldest:
                data = trim_chat(data.decode("utf-8", errors="replace"), oldest).encode("utf-8")
            dst.writestr(Path(info.filename).name, data,
                         compress_type=zipfile.ZIP_DEFLATED if is_txt else zipfile.ZIP_STORED)
            kept += 1
            audio += not is_txt
    if not any(n.lower().endswith(".txt") for n in zipfile.ZipFile(out).namelist()):
        out.unlink()
        sys.exit("No chat .txt found in the zip. Is this a WhatsApp export?")

    # Remove earlier trimmed zips in the same folder so they don't pile up.
    for old in out_dir.glob(f"{OUTPUT_PREFIX}*.zip"):
        if old != out:
            old.unlink()

    mb = lambda p: p.stat().st_size / 1_048_576
    window = f"last {args.days} days" if oldest else "all"
    print(f"{mb(args.zip):.1f} MB → {mb(out):.1f} MB  ({audio} voice notes kept, {window})", file=sys.stderr)
    print(out)


if __name__ == "__main__":
    main()
