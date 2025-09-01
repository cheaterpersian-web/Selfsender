import os
import asyncio
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError


SESSION_DIR = "sessions"
SESSION_NAME = "user_session"


app = FastAPI(title="Telegram Scheduler (Web)")
templates = Jinja2Templates(directory="templates")


def ensure_sessions_dir() -> None:
    os.makedirs(SESSION_DIR, exist_ok=True)


async def is_authorized(client: Optional[TelegramClient]) -> bool:
    if client is None:
        return False
    try:
        await client.connect()
        return await client.is_user_authorized()
    except Exception:
        return False


@app.on_event("startup")
async def on_startup() -> None:
    ensure_sessions_dir()
    app.state.client: Optional[TelegramClient] = None
    app.state.scheduler: AsyncIOScheduler = AsyncIOScheduler()
    app.state.scheduler.start()
    app.state.settings: Dict[str, Any] = {}
    app.state.entity_cache: Dict[str, Any] = {}
    app.state.jobs_meta: Dict[str, Any] = {}


@app.on_event("shutdown")
async def on_shutdown() -> None:
    try:
        app.state.scheduler.shutdown(wait=False)
    except Exception:
        pass
    client: Optional[TelegramClient] = app.state.client
    if client is not None:
        try:
            await client.disconnect()
        except Exception:
            pass


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    if await is_authorized(app.state.client):
        return RedirectResponse(url="/schedule", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/send-code")
async def send_code(api_id: int = Form(...), api_hash: str = Form(...), phone: str = Form(...)):
    ensure_sessions_dir()
    client = TelegramClient(f"{SESSION_DIR}/{SESSION_NAME}", api_id=api_id, api_hash=api_hash)
    app.state.client = client
    app.state.settings = {"api_id": api_id, "api_hash": api_hash, "phone": phone}

    await client.connect()
    if await client.is_user_authorized():
        return RedirectResponse(url="/schedule", status_code=302)

    await client.send_code_request(phone)
    return RedirectResponse(url="/verify", status_code=302)


@app.get("/verify", response_class=HTMLResponse)
async def verify_get(request: Request):
    settings = app.state.settings or {}
    phone = settings.get("phone")
    error = request.query_params.get("error")
    return templates.TemplateResponse("verify.html", {"request": request, "phone": phone, "error": error})


@app.post("/verify")
async def verify_post(code: str = Form(...), password: Optional[str] = Form(None)):
    client: Optional[TelegramClient] = app.state.client
    if client is None:
        return RedirectResponse(url="/", status_code=302)

    settings = app.state.settings or {}
    phone = settings.get("phone")
    if not phone:
        return RedirectResponse(url="/", status_code=302)

    try:
        await client.sign_in(phone=phone, code=code)
    except SessionPasswordNeededError:
        if not password:
            return RedirectResponse(url="/verify?error=رمز عبور 2FA لازم است", status_code=302)
        await client.sign_in(password=password)
    except Exception:
        return RedirectResponse(url="/verify?error=کد نامعتبر است", status_code=302)

    return RedirectResponse(url="/schedule", status_code=302)


async def fetch_groups(client: TelegramClient):
    dialogs = await client.get_dialogs(limit=300)
    groups = [d for d in dialogs if d.is_group]
    return groups


@app.get("/schedule", response_class=HTMLResponse)
async def schedule_get(request: Request):
    client: Optional[TelegramClient] = app.state.client
    if not await is_authorized(client):
        return RedirectResponse(url="/", status_code=302)

    groups = await fetch_groups(client)
    app.state.entity_cache = {str(d.entity.id): d.entity for d in groups}
    group_list = [{"id": str(d.entity.id), "title": d.name or "(بدون نام)"} for d in groups]
    return templates.TemplateResponse("schedule.html", {"request": request, "groups": group_list})


@app.post("/schedule")
async def schedule_post(request: Request):
    client: Optional[TelegramClient] = app.state.client
    if not await is_authorized(client):
        return RedirectResponse(url="/", status_code=302)

    form = await request.form()
    selected_ids: List[str] = form.getlist("groups")
    message_text: str = (form.get("message") or "").strip()
    stype: str = (form.get("stype") or "").strip()

    if not selected_ids or not message_text or stype not in {"once", "interval"}:
        return RedirectResponse(url="/schedule", status_code=302)

    entities = []
    for sid in selected_ids:
        entity = app.state.entity_cache.get(sid)
        if entity is None:
            try:
                entity = await client.get_input_entity(int(sid))
            except Exception:
                continue
        entities.append(entity)

    async def send_to_all(targets: List[Any], text: str):
        for t in targets:
            try:
                await client.send_message(t, text)
            except Exception as ex:
                print(f"send error: {ex}")

    scheduler: AsyncIOScheduler = app.state.scheduler

    job_id = None
    meta: Dict[str, Any] = {
        "targets": len(entities),
        "message_preview": message_text[:64],
        "created_at": datetime.utcnow().isoformat(timespec="seconds"),
    }

    if stype == "once":
        run_at_raw: str = (form.get("run_at") or "").strip()
        run_at_raw = run_at_raw.replace("T", " ")
        try:
            run_dt = datetime.strptime(run_at_raw, "%Y-%m-%d %H:%M")
        except ValueError:
            return RedirectResponse(url="/schedule", status_code=302)
        job = scheduler.add_job(send_to_all, "date", run_date=run_dt, args=[entities, message_text])
        job_id = job.id
        meta["type"] = "once"
        meta["run_at"] = run_dt.isoformat(timespec="minutes")
    else:
        unit = (form.get("unit") or "minute").strip()
        every_raw = (form.get("every") or "1").strip()
        start_after_raw = (form.get("start_after") or "5").strip()
        try:
            every = max(1, int(every_raw))
            start_after = max(0, int(start_after_raw))
        except ValueError:
            return RedirectResponse(url="/schedule", status_code=302)

        seconds = every * 60 if unit == "minute" else every * 3600
        next_run = datetime.now() + timedelta(seconds=start_after)
        job = scheduler.add_job(send_to_all, "interval", seconds=seconds, next_run_time=next_run, args=[entities, message_text])
        job_id = job.id
        meta["type"] = "interval"
        meta["every_seconds"] = seconds
        meta["start_at"] = next_run.isoformat(timespec="seconds")

    if job_id:
        app.state.jobs_meta[job_id] = meta

    return RedirectResponse(url="/jobs", status_code=302)


@app.get("/jobs", response_class=HTMLResponse)
async def jobs_get(request: Request):
    scheduler: AsyncIOScheduler = app.state.scheduler
    jobs = []
    for job in scheduler.get_jobs():
        meta = app.state.jobs_meta.get(job.id, {})
        jobs.append({
            "id": job.id,
            "next_run_time": job.next_run_time.isoformat(sep=" ", timespec="seconds") if job.next_run_time else "-",
            "trigger": str(job.trigger),
            "meta": meta,
        })
    return templates.TemplateResponse("jobs.html", {"request": request, "jobs": jobs})


@app.post("/jobs/{job_id}/cancel")
async def jobs_cancel(job_id: str):
    scheduler: AsyncIOScheduler = app.state.scheduler
    try:
        scheduler.remove_job(job_id)
        app.state.jobs_meta.pop(job_id, None)
    except Exception:
        pass
    return RedirectResponse(url="/jobs", status_code=302)

