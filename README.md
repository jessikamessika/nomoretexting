# call-brief (nomoretexting)

Turns a WhatsApp chat export (text + voice notes) into a very short pre-call agenda:

```
🔴 NEEDS AN ANSWER
- Saturday dinner: yes or no by Friday? (J)
🟡 GOOD TO KNOW
- Landlord raising rent: moving or staying? (J)
🟢 TO CHAT ABOUT
- Did you ever finish that book? (D)
⚪ MINOR ITEMS
- New coffee place on 5th: worth trying? (D)
- Sister's dog had puppies (J)
```

Runs locally. Uses Groq's free API for Whisper transcription and for the summary. No unofficial WhatsApp APIs: you export the chat by hand.

## Setup (once)

1. **Groq API key:** sign in at <https://console.groq.com>, open **API Keys**, then **Create API Key**, and copy the `gsk_…` value. The free plan needs no card.
2. **Virtual env + install** (Python 3.9+):
   ```bash
   cd nomoretexting
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. **Config:** `cp .env.example .env`, then edit it:
   - `GROQ_API_KEY`: your key
   - `MY_NAME`, `FRIEND_NAME`: as they appear in the export. A partial match is fine ("Dan" matches "Dan 🎸").
   - `DATE_ORDER`: `MDY` (US phone: 11/12/26 = Nov 12) or `DMY` (11/12/26 = 11 Dec). If a date proves it wrong (e.g. day 13 in the first slot), the script warns you and switches.
   - `TIMEZONE` defaults to `America/Chicago`. Export timestamps are your phone's local time.

`.env`, `state.json`, `transcripts_cache.json`, `briefs/` and `*.zip` are git-ignored because they contain your chats.

## How to use it (before each call)

Your phone exports the chat as a `.zip`. The script reads that zip directly, so don't unzip it. It can live anywhere. To keep things tidy, use the `exports/` folder in this project, which is git-ignored.

### 1. Export the chat on your phone

Always choose **Include media** / **Attach Media**. Without it, voice notes aren't in the zip and show up as "not included".

**iPhone**
1. Open WhatsApp, then the chat with your friend.
2. Tap their **name** at the top of the chat.
3. Scroll to the bottom and tap **Export Chat**.
4. Tap **Attach Media**.
5. In the share sheet, pick **AirDrop**, then your Mac. It lands in `~/Downloads` as `WhatsApp Chat - <Name>.zip`. (Or **Save to Files** → iCloud Drive and get it from Finder.)

**Android**
1. Open WhatsApp, then the chat.
2. Tap **⋮** (top right), then **More**, then **Export chat**.
3. Tap **Include media**.
4. Send it to your laptop: Google Drive, email to yourself, or Quick Share. You get a `.zip` with `WhatsApp Chat with <Name>.txt` inside.
5. If your phone shares loose files instead of one zip, select them all on the laptop and **Compress** them into a zip.

WhatsApp caps exports *with media* at the most recent ~10,000 messages. That's plenty for "since the last call."

### Android: trim on the phone (recommended)

A full export with media can be hundreds of MB, mostly photos and videos that call-brief doesn't use. [trim_export.py](trim_export.py) keeps only the chat text and the last 30 days of voice notes, which is usually a few MB. With the setup below, you share the export to Termux and it does the rest.

**One-time setup on the phone (about 10 minutes):**
1. Install **Termux** and **Termux:API**. Use both from F-Droid, or both from Google Play; mixing sources breaks Termux:API.
2. Open Termux and run:
   ```bash
   pkg update && pkg install python termux-api
   termux-setup-storage          # tap Allow: gives Termux access to your Downloads folder
   ```
3. Get the two files onto the phone. Easiest: email them to yourself, or put them in Google Drive, and save them to **Downloads**:
   - `trim_export.py`
   - `android/termux-file-editor`
4. Back in Termux, run:
   ```bash
   cp ~/storage/downloads/trim_export.py ~/
   mkdir -p ~/bin && cp ~/storage/downloads/termux-file-editor ~/bin/
   chmod +x ~/bin/termux-file-editor
   ```
5. Install the **Google Drive app** on the phone. On the laptop, install **Google Drive for desktop** and make a `call-brief` folder in My Drive. In `.env`, set `EXPORTS_DIR` to that folder, for example `~/Library/CloudStorage/GoogleDrive-<you>/My Drive/call-brief`.

**Before each call:**
1. In WhatsApp, open the chat, then **⋮** → **More** → **Export chat** → **Include media**.
2. In the share sheet, pick **Termux**. It trims the zip and opens a new share sheet.
3. Pick **Drive** and save to the `call-brief` folder.
4. On the laptop, run `python brief.py`. With no path, it uses the newest zip in `EXPORTS_DIR`.

If it's been more than 30 days since your last call, change `--days` in `~/bin/termux-file-editor`. Use `--days 0` to keep every voice note.

### 2. Put the zip somewhere the script can reach

```bash
cd ~/Projects/nomoretexting
mkdir -p exports
mv ~/Downloads/WhatsApp\ Chat*.zip exports/
```

Any path works. You can also skip this step and point the script straight at `~/Downloads/...`. Put the path in quotes, because the filename has spaces.

### 3. Run it

```bash
cd ~/Projects/nomoretexting
source .venv/bin/activate          # once per terminal window
python brief.py "exports/WhatsApp Chat - Dan.zip"
```

- **First time ever:** there's no saved cutoff yet, so it covers the last 3 days. To set the window yourself, add `--since "2026-11-12 15:00"` (for example, when your last call ended) or `--since 5d`.
- **Every time after:** just run it with no `--since`. It picks up from the last message in the previous export (saved automatically in `state.json`).
- **Not sure the chat was read correctly?** Add `--dry-run`. It prints the parsed timeline and calls no APIs.

### 4. Read the agenda

It prints in the terminal and is also saved as `briefs/brief_YYYY-MM-DD_HHMM.md`. Then go have the call.

### 5. Clean up (optional)

Old zips can be deleted. The script only needs the newest one, and each export contains the full history anyway. Transcripts are cached in `transcripts_cache.json`, so voice notes that appear again in the next export aren't re-transcribed.

### How the cutoff works

After a successful run, `state.json` stores the time of the **last message in that export**. The next run starts from there, so nothing sent between exporting and running is missed. The cutoff is inclusive, and Android timestamps only go to the minute, so the final message or two may appear in two briefs in a row. That's on purpose: better than losing one.

Options:

```bash
python brief.py export.zip --since 3d              # 90m, 12h, 3d, 2w, "2026-11-12", "2026-11-12 15:00"
python brief.py export.zip --dry-run               # parse + show timeline only; no API calls, saves nothing
python brief.py export.zip --since 2d --no-save    # a brief that doesn't move the saved cutoff
```

To reset the cutoff, delete `state.json`.

## Test plan (before using real data)

**1. Parser check with fake exports, no API key needed.** This makes three tiny zips with the tricky cases: iOS invisible characters, multi-line messages, system lines, edited tags, missing media, Android `(file attached)`, 24-hour time, and day-first dates.

```bash
mkdir -p /tmp/cb && cd /tmp/cb && python3 - <<'PYEOF'
import zipfile
L, N = "‎", " "  # iOS invisible mark, narrow no-break space
ios = f"""[11/10/26, 9:00:00{N}AM] Dan: {L}Messages and calls are end-to-end encrypted. No one outside of this chat, not even WhatsApp, can read or listen to them.
[11/10/26, 9:01:00{N}AM] Jess: old message before the cutoff
[11/12/26, 3:04:12{N}PM] Jess: ! Can you confirm Saturday dinner by Friday?
[11/12/26, 3:05:40{N}PM] Dan: {L}<attached: 00000012-AUDIO-2026-11-12-15-05-40.opus>
[11/12/26, 3:06:00{N}PM] Jess: Also my landlord is raising rent
second line of the same message
third line: with a colon
[11/12/26, 3:07:00{N}PM] Dan: {L}image omitted
[11/12/26, 3:09:00{N}PM] Jess: {L}Missed voice call
[11/12/26, 3:10:00{N}PM] Dan: did you ever finish that book? <This message was edited>
[11/12/26, 3:11:00{N}PM] Jess: {L}<attached: 00000014-AUDIO-2026-11-12-15-11-00.opus>
"""
android = """11/10/26, 9:00 AM - Messages and calls are end-to-end encrypted. No one outside of this chat, not even WhatsApp, can read or listen to them. Tap to learn more.
11/12/26, 3:04 PM - Jess: 🔴 Did you book the flights??
11/12/26, 3:05 PM - Dan: PTT-20261112-WA0003.opus (file attached)
11/12/26, 3:06 PM - Dan: IMG-20261112-WA0004.jpg (file attached)
look at this view
11/12/26, 15:07 - Jess: 24h-style line mixed in
11/12/26, 3:09 PM - Dan: This message was deleted
"""
dmy = """13/11/2026, 15:04 - Jess: day-first date, 24h time
13/11/2026, 15:05 - Dan: PTT-20261113-WA0001.opus (file attached)
"""
def z(name, chatname, text, media):
    with zipfile.ZipFile(name, "w") as f:
        f.writestr(chatname, text)
        for m in media: f.writestr(m, b"OggS fake audio")
z("fake_ios.zip", "_chat.txt", ios, ["00000012-AUDIO-2026-11-12-15-05-40.opus"])
z("fake_android.zip", "WhatsApp Chat with Dan.txt", android, ["PTT-20261112-WA0003.opus", "IMG-20261112-WA0004.jpg"])
z("fake_dmy.zip", "WhatsApp Chat with Dan.txt", dmy, ["PTT-20261113-WA0001.opus"])
PYEOF
cd -   # back to the project
python brief.py /tmp/cb/fake_ios.zip     --since "2026-11-12 15:00" --dry-run
python brief.py /tmp/cb/fake_android.zip --since "2026-11-12 15:00" --dry-run
python brief.py /tmp/cb/fake_dmy.zip     --since 30d --dry-run
```

What to check:
- The 9:00/9:01 AM messages and "end-to-end encrypted" / "Missed voice call" / "deleted" lines are **not** in the timeline.
- The landlord message shows as **one** message with three lines.
- Voice notes show `found in zip`. iOS `…0014…opus` shows `MISSING from zip` (on purpose).
- `<This message was edited>` is gone. The 15:07 line reads as 3:07 PM.
- The DMY file prints a warning and reads as **Fri Nov 13**.
- Senders map to the right names and initials.

**2. End-to-end with a real (synthetic) voice note**, using your API key and about 2 requests:

```bash
say -o /tmp/cb/PTT-20261112-WA0009.m4a --file-format=m4af --data-format=aac \
  "Hey, can you send me the address for Saturday? Also I adopted a cat."
cd /tmp/cb && printf '11/12/26, 3:04 PM - Dan: PTT-20261112-WA0009.m4a (file attached)\n11/12/26, 3:05 PM - Jess: wait what kind of cat\n' > chat.txt \
  && zip -q e2e.zip chat.txt PTT-20261112-WA0009.m4a && cd -
python brief.py /tmp/cb/e2e.zip --since 3d --no-save
```

Expect a transcript in `transcripts_cache.json`, and an agenda with the address question under 🔴 and the cat under 🟢. Run it again: it should say `0 need transcribing`.

**3. Real export:** run `--dry-run` first and eyeball the timeline. Then do a real run with `--no-save` until you like the output.

## Groq free-tier limits that matter here

From Groq's docs ([rate limits](https://console.groq.com/docs/rate-limits), [speech-to-text](https://console.groq.com/docs/speech-to-text), [deprecations](https://console.groq.com/docs/deprecations)), checked 2026-11-12. Your exact numbers are at <https://console.groq.com/settings/limits>, which is the source of truth.

| Model | Requests | Tokens / audio |
|---|---|---|
| `whisper-large-v3-turbo` | 20/min, 2,000/day | 7,200 audio-sec/hour (2 h), 28,800/day (8 h) |
| `openai/gpt-oss-120b` | 30/min, 1,000/day | **8,000 tokens/min**, 200,000/day |

What this means:
- **The 8K tokens/minute cap is the real constraint**, not the 131K context window. Reasoning tokens count too. A timeline over about 4,000 tokens (`CHUNK_TOKENS`, roughly 12,000 characters) is condensed in chunks first, then summarized. On 429 errors the script waits the `retry-after` seconds Groq returns. A long backlog may take a few minutes. That's normal.
- **Every Whisper request is billed as at least 10 seconds of audio**, and files over **25 MB** are skipped (voice notes are tiny). At 20 requests/min, 40 voice notes take about 2 minutes on the first run. The cache makes re-runs free.
- If a wait would be over 5 minutes (you hit a daily cap), the script stops with a message. Cached transcripts are kept.
- **`llama-3.3-70b-versatile` was shut down on 2026-08-16.** Groq recommends `openai/gpt-oss-120b`, which is the default. To change models later, set `LLM_MODEL` / `WHISPER_MODEL` in `.env`.
- Groq doesn't list `.opus` as a supported format, but WhatsApp's `.opus` files are Ogg-Opus, so the script uploads them labelled `.ogg` (a supported format).
- Rate limits are per *organization*, so other projects on the same Groq account share them.
