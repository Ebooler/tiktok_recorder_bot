import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from telethon import TelegramClient, events, Button
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault

from core.tiktok_api import TikTokAPI
from utils.utils import read_cookies
from utils.video_management import VideoManagement

SRC_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SRC_DIR / "bot_config.json"
RECORDINGS_DIR = SRC_DIR.parent / "recordings"

TIKTOK_URL_RE = re.compile(r"(https?://)?(www\.|vm\.)?tiktok\.com\S*")
USERNAME_RE = re.compile(r"^@([\w.]{2,24})$")
ROOM_ID_RE = re.compile(r"^\d{10,20}$")

DURATION_CHOICES = [
    ("30 min", 1800), ("1h", 3600),
    ("3h", 10800), ("Senza limite", 0),
]
WATCH_INTERVAL_SECONDS = 90

with open(CONFIG_PATH, "r") as f:
    config = json.load(f)

BOT_TOKEN = config["bot_token"]
API_ID = int(config["api_id"])
API_HASH = config["api_hash"]
ALLOWED_USER_ID = int(config["allowed_user_id"])

client = TelegramClient(str(SRC_DIR / "bot_session"), API_ID, API_HASH)

jobs = {}  # job_id -> job dict (kind: "recording" | "watch")
pending = {}  # pending_id -> {chat_id, flag, value, label} awaiting a button tap


def is_allowed(event) -> bool:
    return event.sender_id == ALLOWED_USER_ID


def parse_target(text: str):
    """Returns (cli_flag, value, label) or None if not recognized."""
    text = text.strip()
    if TIKTOK_URL_RE.search(text):
        return "-url", text, text
    m = USERNAME_RE.match(text)
    if m:
        user = m.group(1)
        return "-user", user, f"@{user}"
    if ROOM_ID_RE.match(text):
        return "-room_id", text, f"room_id {text}"
    return None


def _check_live_sync(flag: str, value: str):
    api = TikTokAPI(proxy=None, cookies=read_cookies())
    if flag == "-url":
        user, room_id = api.get_room_and_user_from_url(value)
    elif flag == "-user":
        user = value
        room_id = api.get_room_id_from_user(user)
    else:
        room_id = value
        user = api.get_user_from_room_id(room_id)
    alive = bool(room_id) and api.is_room_alive(room_id)
    return alive, user, room_id


async def check_live(flag: str, value: str):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _check_live_sync, flag, value)


def spawn_recording(flag: str, value: str, job_dir: Path, duration: int | None):
    log_file = open(job_dir / "log.txt", "w", encoding="utf-8")
    cmd = [
        sys.executable, str(SRC_DIR / "main.py"),
        flag, value,
        "-output", str(job_dir),
        "-no-update-check",
    ]
    if duration:
        cmd += ["-duration", str(duration)]

    popen_kwargs = dict(cwd=str(SRC_DIR), stdout=log_file, stderr=subprocess.STDOUT)
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    process = subprocess.Popen(cmd, **popen_kwargs)
    return process, log_file


def stop_recording(process: subprocess.Popen):
    if os.name == "nt":
        # CTRL_BREAK_EVENT does not map to a graceful KeyboardInterrupt in the
        # child on Windows, so the recorder never reaches its own conversion
        # step. Kill it and finish the FLV->MP4 conversion ourselves instead.
        process.terminate()
    else:
        process.send_signal(signal.SIGINT)


def _active_job_for_target(chat_id: int, flag: str, value: str):
    for job in jobs.values():
        if job["chat_id"] == chat_id and job["target_key"] == (flag, value):
            return job
    return None


async def start_job(chat_id: int, flag: str, value: str, label: str, duration: int | None):
    job_id = uuid.uuid4().hex[:8]
    job_dir = RECORDINGS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        process, log_file = spawn_recording(flag, value, job_dir, duration)
    except Exception as e:
        await client.send_message(chat_id, f"Errore nell'avvio della registrazione: {e}")
        return

    jobs[job_id] = {
        "kind": "recording",
        "process": process,
        "log_file": log_file,
        "output_dir": job_dir,
        "label": label,
        "target_key": (flag, value),
        "started_at": time.time(),
        "chat_id": chat_id,
    }

    duration_note = f" (max {duration // 60} min)" if duration else ""
    await client.send_message(
        chat_id,
        f"🔴 Registrazione avviata: {label}{duration_note}\nTi avviso appena finisce.",
        buttons=[Button.inline("⏹ Ferma", data=f"stopjob|{job_id}")],
    )
    asyncio.create_task(watch_recording(job_id))


async def watch_recording(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return
    chat_id = job["chat_id"]

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, job["process"].wait)
    job["log_file"].close()
    jobs.pop(job_id, None)

    output_dir = job["output_dir"]

    # If the child was killed before it could convert the raw stream itself
    # (happens on a forced stop on Windows), finish the job here using the
    # same conversion utility the recorder itself uses.
    for raw in output_dir.glob("*_flv.mp4"):
        await loop.run_in_executor(None, VideoManagement.convert_flv_to_mp4, str(raw))

    video_files = sorted(output_dir.glob("*.mp4"))

    if not video_files:
        log_text = (output_dir / "log.txt").read_text(encoding="utf-8", errors="ignore")
        tail = "\n".join(log_text.strip().splitlines()[-15:])
        await client.send_message(
            chat_id,
            f"Registrazione di {job['label']} terminata senza produrre un file video.\n"
            f"Ultime righe di log:\n```\n{tail}\n```",
        )
    else:
        for video in video_files:
            await client.send_message(chat_id, f"Registrazione di {job['label']} terminata. Invio {video.name}...")
            await client.send_file(chat_id, str(video), force_document=True)
        await client.send_message(chat_id, "Fatto.")

    shutil.rmtree(output_dir, ignore_errors=True)


async def start_watch(chat_id: int, flag: str, value: str, label: str):
    job_id = uuid.uuid4().hex[:8]
    stop_event = asyncio.Event()
    jobs[job_id] = {
        "kind": "watch",
        "label": label,
        "target_key": (flag, value),
        "started_at": time.time(),
        "chat_id": chat_id,
        "stop_event": stop_event,
    }
    asyncio.create_task(watch_loop(job_id, flag, value, stop_event))


async def watch_loop(job_id: str, flag: str, value: str, stop_event: asyncio.Event):
    while not stop_event.is_set():
        try:
            alive, _user, _room_id = await check_live(flag, value)
        except Exception:
            alive = False

        if alive:
            job = jobs.pop(job_id, None)
            if not job:
                return
            await client.send_message(job["chat_id"], f"🔴 {job['label']} è live! Avvio la registrazione.")
            await start_job(job["chat_id"], flag, value, job["label"], None)
            return

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=WATCH_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue

    jobs.pop(job_id, None)


async def stop_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return
    if job["kind"] == "watch":
        job["stop_event"].set()
        await client.send_message(job["chat_id"], f"Monitoraggio di {job['label']} interrotto.")
    else:
        stop_recording(job["process"])
        await client.send_message(job["chat_id"], f"Fermo {job['label']}, attendi conversione e invio del file...")


@client.on(events.NewMessage(pattern="/start"))
async def start_handler(event):
    if not is_allowed(event):
        return
    await event.respond(
        "Ciao! Mandami uno di questi per una diretta TikTok:\n"
        "• un link (https://www.tiktok.com/@utente/live)\n"
        "• uno username preceduto da @ (es. @utente)\n"
        "• un room_id numerico\n\n"
        "Se è già live ti chiedo la durata massima di registrazione.\n"
        "Se non è live, posso avvisarti e avviare la registrazione automaticamente appena inizia.\n\n"
        "/status per vedere cosa è attivo, /stop per fermare."
    )


@client.on(events.NewMessage(pattern="/status"))
async def status_handler(event):
    if not is_allowed(event):
        return
    my_jobs = [j for j in jobs.values() if j["chat_id"] == event.chat_id]
    if not my_jobs:
        await event.respond("Nessuna registrazione o monitoraggio attivo.")
        return
    lines = []
    for j in my_jobs:
        elapsed = int((time.time() - j["started_at"]) / 60)
        kind = "🔴 Registrazione" if j["kind"] == "recording" else "🔔 In attesa (watch)"
        lines.append(f"{kind}: {j['label']} — da {elapsed} min")
    await event.respond("\n".join(lines))


@client.on(events.NewMessage(pattern="/stop"))
async def stop_handler(event):
    if not is_allowed(event):
        return
    my_jobs = {jid: j for jid, j in jobs.items() if j["chat_id"] == event.chat_id}
    if not my_jobs:
        await event.respond("Nessuna registrazione o monitoraggio attivo.")
        return
    if len(my_jobs) == 1:
        await stop_job(next(iter(my_jobs)))
        return
    buttons = [[Button.inline(f"⏹ {j['label']}", data=f"stopjob|{jid}")] for jid, j in my_jobs.items()]
    await event.respond("Quale vuoi fermare?", buttons=buttons)


@client.on(events.NewMessage)
async def target_handler(event):
    if not is_allowed(event):
        return
    text = (event.raw_text or "").strip()
    if not text or text.startswith("/"):
        return

    target = parse_target(text)
    if not target:
        await event.respond(
            "Non riconosciuto. Mandami un link TikTok, uno username (@utente) o un room_id numerico."
        )
        return
    flag, value, label = target

    if _active_job_for_target(event.chat_id, flag, value):
        await event.respond(f"C'è già un job attivo per {label}. Usa /status o /stop.")
        return

    status_msg = await event.respond(f"Controllo se {label} è live...")

    try:
        alive, _user, _room_id = await check_live(flag, value)
    except Exception as e:
        await status_msg.edit(f"Errore nel controllare {label}: {e}")
        return

    pending_id = uuid.uuid4().hex[:8]
    pending[pending_id] = {"chat_id": event.chat_id, "flag": flag, "value": value, "label": label}

    if alive:
        buttons = [
            [Button.inline(text, data=f"dur|{pending_id}|{seconds}") for text, seconds in DURATION_CHOICES[i:i + 2]]
            for i in range(0, len(DURATION_CHOICES), 2)
        ]
        await status_msg.edit(f"🔴 {label} è live. Durata massima di registrazione?", buttons=buttons)
    else:
        buttons = [
            [Button.inline("🔔 Avvisami quando va live", data=f"watch|{pending_id}")],
            [Button.inline("Annulla", data=f"cancel|{pending_id}")],
        ]
        await status_msg.edit(f"{label} non è live al momento.", buttons=buttons)


@client.on(events.CallbackQuery)
async def callback_handler(event):
    if not is_allowed(event):
        await event.answer("Non autorizzato.", alert=True)
        return

    data = event.data.decode()
    parts = data.split("|")
    action = parts[0]

    if action == "dur":
        pending_id, seconds_str = parts[1], parts[2]
        req = pending.pop(pending_id, None)
        if not req:
            await event.answer("Richiesta scaduta, rimanda il link.")
            return
        duration = int(seconds_str) or None
        await event.edit(f"Avvio registrazione: {req['label']}...", buttons=None)
        await start_job(req["chat_id"], req["flag"], req["value"], req["label"], duration)

    elif action == "watch":
        pending_id = parts[1]
        req = pending.pop(pending_id, None)
        if not req:
            await event.answer("Richiesta scaduta, rimanda il link.")
            return
        await event.edit(f"🔔 Ti avviserò quando {req['label']} va live e avvierò la registrazione.", buttons=None)
        await start_watch(req["chat_id"], req["flag"], req["value"], req["label"])

    elif action == "cancel":
        pending_id = parts[1]
        pending.pop(pending_id, None)
        await event.edit("Annullato.", buttons=None)

    elif action == "stopjob":
        job_id = parts[1]
        await stop_job(job_id)

    await event.answer()


async def set_commands():
    await client(SetBotCommandsRequest(
        scope=BotCommandScopeDefault(),
        lang_code="it",
        commands=[
            BotCommand("start", "Istruzioni"),
            BotCommand("status", "Registrazioni/monitoraggi attivi"),
            BotCommand("stop", "Ferma una registrazione o un monitoraggio"),
        ],
    ))


async def main():
    RECORDINGS_DIR.mkdir(exist_ok=True)
    await client.start(bot_token=BOT_TOKEN)
    await set_commands()
    print("Bot avviato.", flush=True)
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
