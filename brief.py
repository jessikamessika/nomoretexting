#!/usr/bin/env python3
"""call-brief: turn a WhatsApp "Export chat" .zip into a short pre-call agenda.

    python brief.py export.zip --since "2026-11-12 15:00"
    python brief.py export.zip              # since the last successful run
    python brief.py export.zip --since 3d   # relative: 90m, 12h, 3d, 2w
    python brief.py export.zip --dry-run    # parse only: no API calls, nothing saved
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# ---------------------------------------------------------------- config ----

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
CACHE_FILE = ROOT / "transcripts_cache.json"
BRIEFS_DIR = ROOT / "briefs"

load_dotenv(ROOT / ".env")

TZ = ZoneInfo(os.getenv("TIMEZONE", "America/Chicago"))
DATE_ORDER = os.getenv("DATE_ORDER", "MDY").strip().upper()  # MDY or DMY
MY_NAME = os.getenv("MY_NAME", "").strip()
FRIEND_NAME = os.getenv("FRIEND_NAME", "").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-large-v3-turbo")
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE") or None  # e.g. "en"; blank = auto-detect
# Max estimated input tokens per LLM call. The free tier allows ~8K tokens per
# minute for gpt-oss-120b (input + output), so keep this well under that.
CHUNK_TOKENS = int(os.getenv("CHUNK_TOKENS", "4000"))
MAX_OUTPUT_TOKENS = 4000  # incl. reasoning tokens; ~3K real input + prompt + 4K output stays under 8K/min
LLM_GAP_SECONDS = 60      # pause between LLM calls so each one gets a fresh per-minute budget
DEFAULT_WINDOW = "3d"  # used only when there's no --since and no state.json yet
MAX_AUDIO_BYTES = 25 * 1024 * 1024  # Groq free-tier upload limit

AUDIO_EXT = (".opus", ".ogg", ".oga", ".m4a", ".mp3", ".aac", ".amr", ".wav")

# ---------------------------------------------------------- unzip & find ----


def unzip(zip_path: Path, dest: Path) -> None:
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(dest)  # extractall strips absolute paths and ".."
    except (zipfile.BadZipFile, FileNotFoundError) as e:
        sys.exit(f"Can't open {zip_path}: {e}")


def find_chat_and_media(root: Path) -> tuple[Path, dict[str, Path]]:
    files = [p for p in root.rglob("*") if p.is_file()
             and "__MACOSX" not in p.parts and not p.name.startswith("._")]
    txts = [p for p in files if p.suffix.lower() == ".txt"]
    chat = (next((p for p in txts if p.name == "_chat.txt"), None)
            or next((p for p in txts if p.name.lower().startswith("whatsapp chat")), None)
            or (txts[0] if len(txts) == 1 else None))
    if chat is None:
        sys.exit("Couldn't find the chat text file (_chat.txt or 'WhatsApp Chat with ….txt') in the zip.")
    media = {p.name: p for p in files if p != chat}
    return chat, media

# --------------------------------------------------------------- parsing ----

# Invisible direction marks / zero-width chars iOS and Android sprinkle in.
# U+200E (LRM) is handled separately because iOS uses it to flag system lines.
LRM = "‎"
_CLEAN = {ord(c): None for c in "​‏‪‫‬‭‮⁦⁧⁨⁩﻿"}
_CLEAN.update({0x202F: " ", 0x00A0: " ", 0x2007: " "})  # narrow / no-break spaces -> space

DATE = r"\d{1,4}[./-]\d{1,2}[./-]\d{1,4}"
TIME = r"\d{1,2}[:.]\d{2}(?:[:.]\d{2})?(?:\s?[AaPp]\.?\s?[Mm]\.?)?"
IOS_RE = re.compile(rf"^\[({DATE}),?\s+({TIME})\]\s?(.*)$")         # [11/12/26, 3:04:12 PM] Name: text
ANDROID_RE = re.compile(rf"^({DATE}),?\s+({TIME})\s?[-–]\s(.*)$")   # 11/12/26, 3:04 PM - Name: text
TIME_PARTS = re.compile(r"(\d{1,2})[:.](\d{2})(?:[:.](\d{2}))?\s*([AaPp])?")
SENDER_RE = re.compile(r"^([^:\n]{1,80}?):(?:\s|$)(.*)$", re.S)

ATTACH_IOS = re.compile(r"<attached:\s*([^>]+)>")                    # <attached: 00000012-AUDIO-….opus>
ATTACH_ANDROID = re.compile(r"^(\S+\.[A-Za-z0-9]{2,5})\s+\([^)\n]*\)\s*(?:\n(.*))?$", re.S)  # PTT-….opus (file attached)
WA_NAME = re.compile(r"^(PTT|AUD|IMG|VID|DOC|STK)-\d{8}-WA\d+", re.I)
OMITTED = re.compile(r"\b(image|video|audio|sticker|gif|document|contact card) omitted$", re.I)
EDITED = re.compile(r"\s*<This message was edited>\s*$", re.I)
SYSTEM_RE = re.compile(
    r"^(messages and calls are end-to-end encrypted|missed (voice|video) call|"
    r"this message was deleted|you deleted this message|waiting for this message|"
    r".*security code (changed|with)|.*turned (on|off) disappearing messages|null$)", re.I)


@dataclass
class Msg:
    ts: datetime
    sender: str | None
    text: str
    kind: str = "text"            # text | voice | media | system
    attachment: str | None = None
    file: Path | None = None
    transcript: str | None = None


def read_raw(chat: Path) -> list[list[str]]:
    """Split the export into [format, date, time, body] records; join continuation lines."""
    raws: list[list[str]] = []
    for line in chat.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.translate(_CLEAN)
        probe = line.lstrip(LRM)
        m = IOS_RE.match(probe) or ANDROID_RE.match(probe)
        if m:
            fmt = "iOS" if m.re is IOS_RE else "Android"
            raws.append([fmt, m.group(1), m.group(2), m.group(3)])
        elif raws:  # multi-line message
            raws[-1][3] += "\n" + line
    return raws


def resolve_date_order(dates: list[str]) -> str:
    first_max = second_max = 0
    for d in dates:
        p = re.split(r"[./-]", d)
        if len(p[0]) == 4:
            return "YMD"
        first_max, second_max = max(first_max, int(p[0])), max(second_max, int(p[1]))
    order = DATE_ORDER if DATE_ORDER in ("MDY", "DMY") else "MDY"
    if order == "MDY" and first_max > 12:
        print(f"! DATE_ORDER=MDY but a date starts with {first_max}; using DMY.", file=sys.stderr)
        return "DMY"
    if order == "DMY" and second_max > 12:
        print(f"! DATE_ORDER=DMY but a date has {second_max} in the middle; using MDY.", file=sys.stderr)
        return "MDY"
    return order


def make_dt(d: str, t: str, order: str) -> datetime:
    p = [int(x) for x in re.split(r"[./-]", d)]
    y, mo, da = {"YMD": (p[0], p[1], p[2]), "DMY": (p[2], p[1], p[0]), "MDY": (p[2], p[0], p[1])}[order]
    if y < 100:
        y += 2000
    tm = TIME_PARTS.match(t)
    h, mi, s = int(tm.group(1)), int(tm.group(2)), int(tm.group(3) or 0)
    if tm.group(4):  # 12-hour clock
        h = h % 12 + (12 if tm.group(4).lower() == "p" else 0)
    return datetime(y, mo, da, h, mi, s, tzinfo=TZ)  # export times are the phone's local time


def classify(m: Msg, lrm: bool, media: dict[str, Path]) -> None:
    t = m.text
    name = caption = None
    a = ATTACH_IOS.search(t)
    if a:
        name, caption = a.group(1).strip(), ATTACH_IOS.sub("", t).strip()
    else:
        b = ATTACH_ANDROID.match(t)
        if b and (b.group(1) in media or WA_NAME.match(b.group(1))):
            name, caption = b.group(1), (b.group(2) or "").strip()
    if name:
        m.attachment, m.file, m.text = name, media.get(name), caption
        m.kind = "voice" if name.lower().endswith(AUDIO_EXT) else "media"
    elif t.lower() == "<media omitted>":           # exported "without media"
        m.kind, m.text = "media", ""
    elif OMITTED.search(t):                        # iOS "audio omitted", "image omitted", …
        m.kind = "voice" if "audio" in t.lower() else "media"
        m.attachment, m.text = OMITTED.search(t).group(0), ""
    elif m.sender is None or SYSTEM_RE.match(t) or lrm:
        m.kind = "system"                          # iOS marks system text with a leading LRM


def build_messages(raws: list[list[str]], order: str, media: dict[str, Path]) -> list[Msg]:
    msgs = []
    for _fmt, d, t, body in raws:
        try:
            ts = make_dt(d, t, order)
        except (ValueError, AttributeError):
            continue  # a continuation line that happened to look like a header
        sm = SENDER_RE.match(body)
        sender, text = (sm.group(1).replace(LRM, "").strip(), sm.group(2)) if sm else (None, body)
        lrm = text.startswith(LRM)
        text = EDITED.sub("", text.replace(LRM, "")).strip()
        m = Msg(ts, sender, text)
        classify(m, lrm, media)
        msgs.append(m)
    return msgs


def resolve_people(msgs: list[Msg]) -> dict:
    """Map export sender names to MY_NAME / FRIEND_NAME and assign initials."""
    senders = Counter(m.sender for m in msgs if m.sender and m.kind != "system")
    mapping = {}
    for s in senders:
        mapping[s] = s
        for want in (MY_NAME, FRIEND_NAME):
            if want and (want.casefold() in s.casefold() or s.casefold() in want.casefold()):
                mapping[s] = want
                break
    for m in msgs:
        if m.sender in mapping:
            m.sender = mapping[m.sender]
    ranked = [mapping[s] for s, _ in senders.most_common()]
    me = MY_NAME or (ranked[0] if ranked else "Me")
    friend = FRIEND_NAME or next((n for n in ranked if n != me), "Friend")
    initials: dict[str, str] = {}
    for n in dict.fromkeys([me, friend, *ranked]):
        i = n[:1].upper()
        initials[n] = i if i not in initials.values() else n[:2].title()
    return {"me": me, "friend": friend, "initials": initials, "mapping": mapping}

# -------------------------------------------------------------- timeline ----


def fmt_ts(ts: datetime) -> str:
    return f"{ts:%a %b} {ts.day} {ts.hour % 12 or 12}:{ts:%M} {ts:%p}"


def media_label(name: str | None) -> str:
    n = (name or "").lower()
    if n.endswith((".jpg", ".jpeg", ".png", ".heic")) or any(k in n for k in ("photo", "img-", "image")):
        return "photo"
    if n.endswith((".mp4", ".mov")) or "vid" in n:
        return "video"
    if n.endswith(".webp") or "stk-" in n or "sticker" in n:
        return "sticker"
    return "attachment" if not n else "file"


def timeline_line(m: Msg) -> str:
    head = f"[{fmt_ts(m.ts)}] {m.sender}"
    if m.kind == "voice":
        line = f"{head} (voice): {m.transcript or '[voice note not included in export]'}"
        return line + (f" (caption: {m.text})" if m.text else "")
    if m.kind == "media":
        return f"{head}: [{media_label(m.attachment)}] {m.text}".rstrip()
    return f"{head}: {m.text}".replace("\n", "\n    ")

# ------------------------------------------------------------------ groq ----


def make_client():
    from groq import Groq
    key = os.getenv("GROQ_API_KEY")
    if not key:
        sys.exit("GROQ_API_KEY is missing. Put it in .env (see README).")
    return Groq(api_key=key, max_retries=0)  # we do our own retries below


def with_retries(fn, what: str, tries: int = 6):
    import groq
    delay = 2.0
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except groq.RateLimitError as e:
            ra = e.response.headers.get("retry-after")
            wait = float(ra) + 1 if ra else delay
            if wait > 300:
                sys.exit(f"Groq rate limit on {what}: retry in ~{wait / 60:.0f} min (probably a daily cap).")
            reason = "rate-limited"
        except (groq.APIConnectionError, groq.InternalServerError):
            wait, reason = delay, "connection/server error"
        if attempt == tries:
            break
        print(f"  {reason} on {what}; retrying in {wait:.0f}s ({attempt}/{tries})", file=sys.stderr)
        time.sleep(wait)
        delay = min(delay * 2, 60)
    sys.exit(f"Gave up on {what} after {tries} attempts.")


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(path)  # atomic: never leaves a half-written file


def transcribe(client, path: Path, cache: dict, people: dict) -> str:
    if path.name in cache:
        return cache[path.name]["text"]
    if path.stat().st_size > MAX_AUDIO_BYTES:
        return "[voice note too large to transcribe]"
    # Groq accepts .ogg but doesn't list .opus; WhatsApp .opus files are Ogg-Opus, so relabel.
    upload_name = path.stem + ".ogg" if path.suffix.lower() == ".opus" else path.name
    kwargs = dict(file=(upload_name, path.read_bytes()), model=WHISPER_MODEL, temperature=0.0,
                  prompt=f"Voice note between friends {people['me']} and {people['friend']}.")
    if WHISPER_LANGUAGE:
        kwargs["language"] = WHISPER_LANGUAGE
    result = with_retries(lambda: client.audio.transcriptions.create(**kwargs), f"transcribing {path.name}")
    text = (result.text or "").strip() or "[inaudible]"
    cache[path.name] = {"text": text, "model": WHISPER_MODEL, "at": datetime.now(TZ).isoformat()}
    save_json(CACHE_FILE, cache)  # save after each one so a crash loses nothing
    return text


_last_llm_call = 0.0


def llm(client, system: str, user: str, effort: str, what: str = "LLM call") -> str:
    global _last_llm_call
    wait = LLM_GAP_SECONDS - (time.monotonic() - _last_llm_call)
    if _last_llm_call and wait > 0:
        print(f"  waiting {wait:.0f}s for the per-minute token limit…", file=sys.stderr)
        time.sleep(wait)
    print(f"  {what}: sending ~{est_tokens(system + user)} tokens to {LLM_MODEL}…", file=sys.stderr)
    kwargs = dict(model=LLM_MODEL, max_completion_tokens=MAX_OUTPUT_TOKENS,
                  messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    if "gpt-oss" in LLM_MODEL:  # reasoning model: reasoning tokens count against rate limits
        kwargs["extra_body"] = {"reasoning_effort": effort}
    start = time.monotonic()
    resp = with_retries(lambda: client.chat.completions.create(**kwargs), what)
    _last_llm_call = time.monotonic()
    used = getattr(resp, "usage", None)
    print(f"  {what}: done in {_last_llm_call - start:.0f}s"
          + (f" ({used.total_tokens} tokens used)" if used else ""), file=sys.stderr)
    out = (resp.choices[0].message.content or "").strip()
    if not out:
        sys.exit(f"The model returned nothing (finish_reason={resp.choices[0].finish_reason}).")
    return out

# --------------------------------------------------------------- prompts ----


def final_prompt(p: dict) -> str:
    me, fr, ini = p["me"], p["friend"], p["initials"]
    return f"""You prepare a pre-call agenda for two close friends, {me} ({ini[me]}) and {fr} ({ini[fr]}).
{fr} rarely reads messages, so this agenda is how they catch up; the call is where things get answered.
Input is their WhatsApp messages in time order. Lines marked "(voice)" are auto-transcribed voice notes and may contain transcription errors.

Output exactly this shape and nothing else:

🔴 NEEDS AN ANSWER
- <item> ({ini[me]} or {ini[fr]})
🟡 GOOD TO KNOW
- <item> (initial)
🟢 TO CHAT ABOUT
- <item> (initial)
⚪ MINOR ITEMS
- <item> (initial)

What goes where:
- 🔴 unanswered questions, pending decisions, requests waiting on someone.
- 🟡 news, changes of plan, anything time-sensitive.
- 🟢 fun stories, random thoughts, things worth talking about.
- ⚪ every other real item that didn't make the top 3 of its section. List them all: the reader filters these themselves.

Rules:
- At most 3 items in each of 🔴 🟡 🟢; put the rest under ⚪ MINOR ITEMS, with no limit. Leave out a section that has nothing.
- Each item is at most 10 words and is a conversation hook (a question or prompt to start talking), not a recap.
  Good: "Saturday hike still on, or rain plan?"  Bad: "{fr} said he might hike Saturday."
- End each item with the initial of the person who raised it, in parentheses.
- Merge a topic that spans several messages into one item.
- A message starting with "!" or 🔴 is top priority: always include it, first in its section.
- The most important thing to catch: questions either person asked that were never answered later on. A question answered later in the messages is NOT unanswered.
- Skip greetings, reactions, and anything already resolved.
- No preamble, no closing remarks."""


def notes_prompt(p: dict) -> str:
    me, fr, ini = p["me"], p["friend"], p["initials"]
    return f"""You are condensing one part of a long WhatsApp conversation between {me} ({ini[me]}) and {fr} ({ini[fr]}) so a later step can build a call agenda.
Lines marked "(voice)" are transcribed voice notes. List, as terse bullets with date and who raised it (initial):
- every question or request, and whether it gets answered within this part (ANSWERED / OPEN)
- decisions pending, news, plan changes, time-sensitive info
- fun stories or topics worth chatting about
Mark messages that start with "!" or 🔴 as PRIORITY. Merge related messages, but keep small topics too (the reader wants every item). Max 400 words, bullets only, no preamble."""

# ------------------------------------------------------------- summarize ----


def est_tokens(text: str) -> int:
    return len(text) // 3 + 1  # conservative (emoji / non-English use more tokens per char)


def chunk_lines(lines: list[str], budget: int) -> list[str]:
    chunks, cur, size = [], [], 0
    for line in lines:
        while est_tokens(line) > budget:  # one huge voice transcript: hard-split it
            cut = budget * 3
            lines_head, line = line[:cut], "    …" + line[cut:]
            if cur:
                chunks.append("\n".join(cur))
                cur, size = [], 0
            chunks.append(lines_head)
        if size + est_tokens(line) > budget and cur:
            chunks.append("\n".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += est_tokens(line)
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def summarize(client, lines: list[str], people: dict) -> str:
    material, condensed, rounds = lines, False, 0
    # Too long for one call? Condense chunks into notes, repeat until it fits (normally one round).
    while est_tokens("\n".join(material)) > CHUNK_TOKENS and rounds < 3:
        chunks = chunk_lines(material, CHUNK_TOKENS)
        print(f"Timeline is long: summarizing {len(chunks)} chunks, then merging "
              f"(about {len(chunks)} min with the pauses)…", file=sys.stderr)
        notes = [f"### Part {i} of {len(chunks)}\n"
                 + llm(client, notes_prompt(people), c, "low", f"chunk {i}/{len(chunks)}")
                 for i, c in enumerate(chunks, 1)]
        material, condensed, rounds = "\n".join(notes).splitlines(), True, rounds + 1
    label = ("Notes condensed from a long conversation, in time order (an OPEN question in an early "
             "part may be answered in a later part):" if condensed else "Messages:")
    print("Writing agenda…", file=sys.stderr)
    return llm(client, final_prompt(people), label + "\n\n" + "\n".join(material), "low",
               "merging into agenda" if condensed else "agenda")

# ------------------------------------------------------------------- cli ----


def resolve_cutoff(since: str | None, now: datetime) -> tuple[datetime, str]:
    if since is None:
        state = load_json(STATE_FILE, {})
        # Self-updating cutoff: each run saves the time of its export's newest message to
        # state.json (see end of main()), so nothing sent after that export is skipped.
        last = state.get("last_message_at") or state.get("last_run")
        if last:
            return datetime.fromisoformat(last), "last message of previous export"
        print(f"No saved cutoff yet; defaulting to the last {DEFAULT_WINDOW}.", file=sys.stderr)
        since = DEFAULT_WINDOW
    rel = re.fullmatch(r"\s*(\d+)\s*([mhdw])\s*", since, re.I)
    if rel:
        n, unit = int(rel.group(1)), rel.group(2).lower()
        unit_name = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}[unit]
        return now - timedelta(**{unit_name: n}), since
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(since.strip(), fmt).replace(tzinfo=TZ), since
        except ValueError:
            pass
    sys.exit(f'Bad --since "{since}". Use "YYYY-MM-DD HH:MM", "YYYY-MM-DD", or 90m / 12h / 3d / 2w.')


def print_dry_run(fmt_counts, order, msgs, recent, people, cutoff):
    print(f"Format: {dict(fmt_counts)}   date order: {order}   timezone: {TZ.key}")
    print(f"Parsed {len(msgs)} messages ({sum(m.kind == 'system' for m in msgs)} system), "
          f"{fmt_ts(msgs[0].ts)} → {fmt_ts(msgs[-1].ts)}")
    print("Senders: " + ", ".join(f"{raw!r} → {name} ({people['initials'].get(name, '?')})"
                                  for raw, name in people["mapping"].items()))
    print(f"Since {fmt_ts(cutoff)}: {len(recent)} messages\n")
    for m in recent:
        if m.kind == "voice":
            m.transcript = (f"<{m.attachment}: {'found in zip' if m.file else 'MISSING from zip'}>"
                            if m.attachment else None)
        print(timeline_line(m))


def main() -> None:
    ap = argparse.ArgumentParser(description="Build a pre-call agenda from a WhatsApp chat export.")
    ap.add_argument("zip", type=Path, nargs="?",
                    help="WhatsApp export .zip (default: newest .zip in EXPORTS_DIR)")
    ap.add_argument("--since", help='cutoff: "YYYY-MM-DD HH:MM", "YYYY-MM-DD", or 90m/12h/3d/2w')
    ap.add_argument("--dry-run", action="store_true", help="parse and print the timeline; no API calls, nothing saved")
    ap.add_argument("--no-save", action="store_true", help="don't update the saved cutoff in state.json")
    args = ap.parse_args()
    if args.zip is None:
        exports = Path(os.path.expanduser(os.getenv("EXPORTS_DIR", str(ROOT / "exports"))))
        zips = sorted(exports.glob("*.zip"), key=lambda p: p.stat().st_mtime) if exports.is_dir() else []
        if not zips:
            sys.exit(f"No zip given and none found in {exports} (set EXPORTS_DIR in .env).")
        args.zip = zips[-1]
        print(f"Using newest export: {args.zip.name}", file=sys.stderr)

    run_at = datetime.now(TZ)
    cutoff, cutoff_src = resolve_cutoff(args.since, run_at)

    with tempfile.TemporaryDirectory(prefix="call-brief-") as tmp:
        unzip(args.zip, Path(tmp))
        chat, media = find_chat_and_media(Path(tmp))
        raws = read_raw(chat)
        if not raws:
            sys.exit(f"No messages recognised in {chat.name}. Is this a WhatsApp export? Try --dry-run.")
        order = resolve_date_order([r[1] for r in raws])
        msgs = build_messages(raws, order, media)
        people = resolve_people(msgs)
        # Next default cutoff = the newest message in THIS export (not the run time), so messages
        # sent between exporting and running aren't skipped. Inclusive (>=): Android timestamps
        # are per-minute, so the last message may repeat next time rather than risk losing one.
        last_ts = max(m.ts for m in msgs)
        # Filter BEFORE transcribing anything.
        recent = [m for m in msgs if m.ts >= cutoff and m.kind != "system"]

        if args.dry_run:
            print_dry_run(Counter(r[0] for r in raws), order, msgs, recent, people, cutoff)
            return
        if not recent:
            print(f"Nothing new since {fmt_ts(cutoff)} ({cutoff_src}). No brief written.")
            return

        client = make_client()
        voice = [m for m in recent if m.kind == "voice" and m.file]
        cache = load_json(CACHE_FILE, {})
        todo = sum(m.file.name not in cache for m in voice)
        print(f"{len(recent)} messages since {fmt_ts(cutoff)} ({cutoff_src}); "
              f"{len(voice)} voice notes, {todo} need transcribing.", file=sys.stderr)
        for m in voice:
            m.transcript = transcribe(client, m.file, cache, people)

        agenda = summarize(client, [timeline_line(m) for m in recent], people)

    header = (f"# Call brief: {fmt_ts(run_at)}\n"
              f"_{people['me']} & {people['friend']} · since {fmt_ts(cutoff)} · "
              f"{len(recent)} messages, {sum(m.kind == 'voice' for m in recent)} voice notes_\n")
    BRIEFS_DIR.mkdir(exist_ok=True)
    out = BRIEFS_DIR / f"brief_{run_at:%Y-%m-%d_%H%M}.md"
    out.write_text(header + "\n" + agenda + "\n")
    print("\n" + agenda + "\n")
    print(f"Saved {out}", file=sys.stderr)

    if not args.no_save:
        save_json(STATE_FILE, {"last_message_at": last_ts.isoformat(), "last_run": run_at.isoformat()})
        print(f"Next default cutoff: {fmt_ts(last_ts)} (last message in this export)", file=sys.stderr)


if __name__ == "__main__":
    main()
