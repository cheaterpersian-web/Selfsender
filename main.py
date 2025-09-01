import asyncio
import os
from datetime import datetime, timedelta
from typing import List, Optional

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError


SESSION_DIR = "sessions"
SESSION_NAME = "user_session"


def ask_str(prompt: str, allow_empty: bool = False) -> str:
    while True:
        value = input(prompt).strip()
        if value or allow_empty:
            return value
        print("ورودی نمی‌تواند خالی باشد.")


def ask_int(prompt: str, min_value: Optional[int] = None, max_value: Optional[int] = None) -> int:
    while True:
        raw = input(prompt).strip()
        try:
            number = int(raw)
        except ValueError:
            print("لطفاً یک عدد صحیح وارد کنید.")
            continue
        if min_value is not None and number < min_value:
            print(f"عدد باید ≥ {min_value} باشد.")
            continue
        if max_value is not None and number > max_value:
            print(f"عدد باید ≤ {max_value} باشد.")
            continue
        return number


async def ensure_login(client: TelegramClient, phone_number: str) -> None:
    await client.connect()
    if await client.is_user_authorized():
        return

    print("در حال ارسال کد ورود به تلگرام...")
    await client.send_code_request(phone_number)
    code = ask_str("کد ارسال شده را وارد کنید: ")
    try:
        await client.sign_in(phone=phone_number, code=code)
    except SessionPasswordNeededError:
        password = ask_str("رمز عبور 2FA را وارد کنید: ")
        await client.sign_in(password=password)


async def fetch_groups(client: TelegramClient):
    dialogs = await client.get_dialogs(limit=300)
    groups = [d for d in dialogs if d.is_group]
    return groups


def parse_indexes(raw: str, max_index: int) -> List[int]:
    cleaned = raw.replace(" ", "")
    result: List[int] = []
    if not cleaned:
        return result
    for part in cleaned.split(','):
        if not part:
            continue
        try:
            idx = int(part)
        except ValueError:
            raise ValueError(f"فرمت نامعتبر: {part}")
        if idx < 1 or idx > max_index:
            raise ValueError(f"اندیس خارج از محدوده: {idx}")
        result.append(idx)
    return sorted(set(result))


async def schedule_messages(client: TelegramClient) -> None:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    phone = ask_str("شماره موبایل با کد کشور (مثلاً +98912...): ")

    await ensure_login(client, phone)

    print("\nدر حال دریافت لیست گروه‌ها...")
    groups = await fetch_groups(client)
    if not groups:
        print("گروهی یافت نشد. ابتدا در تلگرام عضو یک گروه شوید.")
        return

    print("\nگروه‌های شما:")
    for i, dialog in enumerate(groups, start=1):
        title = dialog.name or "(بدون نام)"
        print(f"{i}. {title}")

    while True:
        try:
            indexes = parse_indexes(ask_str("شماره گروه‌ها را با کاما وارد کنید (مثلاً 1,3,5): "), len(groups))
            if not indexes:
                print("حداقل یک گروه را انتخاب کنید.")
                continue
            break
        except ValueError as e:
            print(str(e))

    target_entities = []
    for idx in indexes:
        dialog = groups[idx - 1]
        input_peer = await client.get_input_entity(dialog.entity)
        target_entities.append(input_peer)

    message_text = ask_str("متن پیام را وارد کنید: ")

    print("\nنوع زمان‌بندی را انتخاب کنید:")
    print("1) یک‌بار در تاریخ/ساعت مشخص")
    print("2) تکرارشونده هر N دقیقه/ساعت")
    choice = ask_int("انتخاب شما (1 یا 2): ", min_value=1, max_value=2)

    scheduler = AsyncIOScheduler()
    scheduler.start()

    async def send_to_all():
        for entity in target_entities:
            try:
                await client.send_message(entity, message_text)
            except Exception as ex:
                print(f"خطا در ارسال به یک گروه: {ex}")

    if choice == 1:
        while True:
            when_str = ask_str("تاریخ و ساعت را به فرم  YYYY-MM-DD HH:MM  وارد کنید: ")
            try:
                run_dt = datetime.strptime(when_str, "%Y-%m-%d %H:%M")
                break
            except ValueError:
                print("فرمت نامعتبر. نمونه: 2025-01-31 14:30")
        scheduler.add_job(send_to_all, "date", run_date=run_dt)
        print(f"یک ارسال برای {run_dt} زمان‌بندی شد.")
    else:
        unit = ask_int("واحد زمانی را انتخاب کنید: 1) دقیقه  2) ساعت: ", min_value=1, max_value=2)
        every = ask_int("هر چند واحد یک‌بار ارسال شود؟ (عدد مثبت): ", min_value=1)
        if unit == 1:
            seconds = every * 60
            human = f"هر {every} دقیقه"
        else:
            seconds = every * 3600
            human = f"هر {every} ساعت"
        start_after = ask_int("چند ثانیه بعد از الان شروع شود؟ (پیش‌فرض 5): ", min_value=0)
        start_dt = datetime.now() + timedelta(seconds=start_after or 5)
        scheduler.add_job(send_to_all, "interval", seconds=seconds, next_run_time=start_dt)
        print(f"ارسال تکرارشونده {human}، شروع از {start_dt} زمان‌بندی شد.")

    print("\nبرنامه در حال اجراست. برای خروج Ctrl+C را بزنید.")
    try:
        # زنده نگه داشتن برنامه برای اجرای زمان‌بندی‌ها
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        print("در حال خروج...")
        scheduler.shutdown(wait=False)


async def app():
    api_id = ask_int("API ID را وارد کنید: ")
    api_hash = ask_str("API HASH را وارد کنید: ")

    os.makedirs(SESSION_DIR, exist_ok=True)

    client = TelegramClient(f"{SESSION_DIR}/{SESSION_NAME}", api_id=api_id, api_hash=api_hash)
    async with client:
        await schedule_messages(client)


if __name__ == "__main__":
    asyncio.run(app())

