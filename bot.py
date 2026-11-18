"""
Демо-бот «ИИ-администратор» для образовательного центра.

Что умеет:
  • отвечает на вопросы о курсах, ценах, адресе (ИИ через Claude, если задан ключ;
    без ключа — кнопками и пересылкой вопроса администратору);
  • записывает на пробный урок: кто учится → курс → время → имя → телефон;
  • спрашивает согласие на обработку персональных данных перед записью;
  • присылает администратору каждую заявку и каждый вопрос, на который не смог ответить;
  • напоминает о пробном уроке за 24 и за 2 часа, даёт подтвердить или перенести;
  • команды администратора: /leads, /export, /reply.

Все данные школы — в school.json. Под нового клиента меняется только этот файл.
"""

from __future__ import annotations

import asyncio
import csv
import html
import io
import json
import logging
import os
import re
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv

# ---------------------------------------------------------------- настройки

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_FILE = BASE / "admin_chat.txt"


def load_admin_id() -> int:
    """ID чата администратора: из .env, иначе из admin_chat.txt (первый, кто нажал /start)."""
    env = (os.getenv("ADMIN_CHAT_ID") or "").strip()
    if env.lstrip("-").isdigit():
        return int(env)
    try:
        return int(ADMIN_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


ADMIN_CHAT_ID = load_admin_id()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5").strip()
SCHOOL_FILE = os.getenv("SCHOOL_FILE", "school.json")
DB_PATH = BASE / os.getenv("DB_FILE", "leads.db")

SCHOOL = json.loads((BASE / SCHOOL_FILE).read_text(encoding="utf-8"))
TZ = timezone(timedelta(hours=SCHOOL.get("utc_offset", 5)))
CAPACITY = SCHOOL.get("trial", {}).get("capacity_per_slot", 3)

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

BTN_BOOK = "Записаться на пробный урок"
BTN_COURSES = "Курсы и цены"
BTN_CONTACTS = "Адрес и график"
BTN_ASK = "Задать вопрос"

ACTIVE_STATUSES = ("записан", "подтвердил")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

# ---------------------------------------------------------------- база

def db_exec(sql: str, params: tuple = ()) -> int:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid


def db_query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchall()


def init_db() -> None:
    db_exec(
        """CREATE TABLE IF NOT EXISTS leads(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            student TEXT,
            course TEXT,
            name TEXT,
            phone TEXT,
            slot TEXT,
            wish TEXT,
            status TEXT NOT NULL DEFAULT 'записан',
            reminded_24h INTEGER NOT NULL DEFAULT 0,
            reminded_2h INTEGER NOT NULL DEFAULT 0
        )"""
    )
    db_exec("CREATE TABLE IF NOT EXISTS consents(user_id INTEGER PRIMARY KEY, at TEXT NOT NULL)")
    db_exec(
        """CREATE TABLE IF NOT EXISTS questions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL, user_id INTEGER, username TEXT,
            text TEXT, answer TEXT, escalated INTEGER NOT NULL DEFAULT 0
        )"""
    )


def now_local() -> datetime:
    return datetime.now(TZ)


def has_consent(user_id: int) -> bool:
    return bool(db_query("SELECT 1 FROM consents WHERE user_id=?", (user_id,)))


def save_consent(user_id: int) -> None:
    db_exec(
        "INSERT OR REPLACE INTO consents(user_id, at) VALUES(?, ?)",
        (user_id, now_local().isoformat(timespec="seconds")),
    )

# ---------------------------------------------------------------- расписание

def fmt_slot(iso: str | None) -> str:
    if not iso:
        return "время согласует администратор"
    dt = datetime.fromisoformat(iso)
    return f"{DAY_RU[dt.weekday()]} {dt.day:02d}.{dt.month:02d} в {dt:%H:%M}"


def booked_count(iso: str) -> int:
    rows = db_query(
        f"SELECT COUNT(*) AS n FROM leads WHERE slot=? AND status IN ({','.join('?' * len(ACTIVE_STATUSES))})",
        (iso, *ACTIVE_STATUSES),
    )
    return rows[0]["n"]


def upcoming_slots(limit: int = 6) -> list[str]:
    """Ближайшие свободные слоты пробных уроков, начиная с завтрашнего дня."""
    result: list[str] = []
    today = now_local().date()
    for shift in range(1, 22):
        day = today + timedelta(days=shift)
        for t in SCHOOL.get("slots", {}).get(DAY_KEYS[day.weekday()], []):
            hh, mm = (int(x) for x in t.split(":"))
            iso = datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ).isoformat(timespec="minutes")
            if booked_count(iso) < CAPACITY:
                result.append(iso)
            if len(result) >= limit:
                return result
    return result

# ---------------------------------------------------------------- тексты

def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_BOOK)],
            [KeyboardButton(text=BTN_COURSES), KeyboardButton(text=BTN_CONTACTS)],
            [KeyboardButton(text=BTN_ASK)],
        ],
        resize_keyboard=True,
    )


def book_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Записаться на пробный урок", callback_data="book")]]
    )


def courses_text() -> str:
    lines = [f"<b>Курсы {SCHOOL['name']}</b>\n"]
    for c in SCHOOL["courses"]:
        lines.append(f"<b>{c['name']}</b> ({c['for']})\n{c['format']}\n{c['price']}\n")
    trial = SCHOOL.get("trial", {})
    lines.append(f"Пробный урок — {trial.get('price', 'бесплатно')}, {trial.get('duration_min', 45)} минут.")
    return "\n".join(lines)


def contacts_text() -> str:
    return (
        f"<b>{SCHOOL['name']}</b>\n"
        f"Адрес: {SCHOOL['address']}\n"
        f"График: {SCHOOL['hours']}\n"
        f"Телефон: {SCHOOL['phone']}"
    )


def tg_name(m) -> str:
    u = m.from_user
    return html.escape(f"@{u.username}" if u.username else (u.full_name or str(u.id)))


def esc(value) -> str:
    """Экранирует пользовательский текст для HTML-сообщений Telegram."""
    return html.escape(str(value or ""))

# ---------------------------------------------------------------- ИИ-ответы

SYSTEM_PROMPT = f"""Ты — вежливый администратор образовательного центра «{SCHOOL['name']}» в городе {SCHOOL.get('city', '')}.
Отвечай коротко, до 4 предложений, на языке клиента (русский или казахский).
Используй только факты из данных центра ниже. Не выдумывай цены, даты, скидки и обещания.
Если ответа в данных нет или вопрос требует решения человека (индивидуальные скидки, жалобы, возвраты, особые условия),
напиши, что передашь вопрос администратору, и поставь в самом конце метку [ADMIN].
Где уместно, предложи записаться на бесплатный пробный урок кнопкой «{BTN_BOOK}».

Данные центра (JSON):
{json.dumps(SCHOOL, ensure_ascii=False)}"""

_claude = None
if ANTHROPIC_API_KEY:
    from anthropic import AsyncAnthropic

    _claude = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


def faq_match(text: str) -> str | None:
    """Простой поиск по FAQ, когда ИИ не подключён."""
    words = {w for w in re.findall(r"\w{4,}", text.lower())}
    best, score = None, 0
    for item in SCHOOL.get("faq", []):
        q_words = {w for w in re.findall(r"\w{4,}", item["q"].lower())}
        s = len(words & q_words)
        if s > score:
            best, score = item["a"], s
    return best if score >= 1 else None


async def answer_question(text: str) -> tuple[str, bool]:
    """Возвращает (ответ, нужно_ли_звать_администратора)."""
    if _claude:
        try:
            resp = await _claude.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=400,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": text[:2000]}],
            )
            answer = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
            escalate = "[ADMIN]" in answer
            answer = answer.replace("[ADMIN]", "").strip()
            if answer:
                return answer, escalate
        except Exception:
            log.exception("Ошибка запроса к Claude")
    found = faq_match(text)
    if found:
        return found, False
    return "Передал ваш вопрос администратору, он ответит в ближайшее время.", True

# ---------------------------------------------------------------- админ

async def notify_admin(bot: Bot, text: str) -> None:
    if not ADMIN_CHAT_ID:
        log.warning("ADMIN_CHAT_ID не задан, уведомление не отправлено: %s", text)
        return
    try:
        await bot.send_message(ADMIN_CHAT_ID, text)
    except Exception:
        log.exception("Не удалось отправить уведомление администратору")


def is_admin(m: Message) -> bool:
    return bool(ADMIN_CHAT_ID) and m.chat.id == ADMIN_CHAT_ID

# ---------------------------------------------------------------- сценарий записи

class Booking(StatesGroup):
    consent = State()
    student = State()
    course = State()
    slot = State()
    wish = State()
    name = State()
    phone = State()
    confirm = State()


router = Router()


@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    global ADMIN_CHAT_ID
    await state.clear()
    if not ADMIN_CHAT_ID and m.chat.type == "private":
        ADMIN_CHAT_ID = m.chat.id
        ADMIN_FILE.write_text(str(m.chat.id), encoding="utf-8")
        log.info("Администратор назначен: %s", m.chat.id)
        await m.answer(
            "Вы назначены администратором этого бота: сюда будут приходить заявки и вопросы клиентов.\n"
            "Команды: /leads — последние заявки, /export — все заявки файлом.\n\n"
            "Ниже — то, что видит клиент:"
        )
    await m.answer(
        f"Здравствуйте! Я помощник центра «{SCHOOL['name']}».\n"
        "Расскажу о курсах и ценах, отвечу на вопросы и запишу на бесплатный пробный урок — в любое время.",
        reply_markup=main_menu(),
    )


@router.message(Command("myid"))
async def cmd_myid(m: Message):
    await m.answer(f"ID этого чата: <code>{m.chat.id}</code>\nВставьте его в ADMIN_CHAT_ID в файле .env и перезапустите бота.")


@router.message(F.text == BTN_COURSES)
async def show_courses(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(courses_text(), reply_markup=book_button())


@router.message(F.text == BTN_CONTACTS)
async def show_contacts(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(contacts_text(), reply_markup=book_button())


@router.message(F.text == BTN_ASK)
async def ask_prompt(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Напишите ваш вопрос одним сообщением — отвечу сразу.")


async def start_booking(m: Message, state: FSMContext, user_id: int):
    await state.clear()
    if not has_consent(user_id):
        await state.set_state(Booking.consent)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(text="Согласен", callback_data="consent:yes"),
                InlineKeyboardButton(text="Не сейчас", callback_data="consent:no"),
            ]]
        )
        await m.answer(
            "Чтобы записать вас, понадобятся имя и телефон.\n"
            f"Нажимая «Согласен», вы даёте согласие центру «{SCHOOL['name']}» на обработку этих данных "
            "для связи по записи. Мы не передаём их третьим лицам.",
            reply_markup=kb,
        )
        return
    await ask_student(m, state)


async def ask_student(m: Message, state: FSMContext):
    await state.set_state(Booking.student)
    types = SCHOOL.get("student_types", ["Ребёнок", "Подросток", "Взрослый"])
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=f"student:{i}")] for i, t in enumerate(types)]
    )
    await m.answer("Кто будет учиться?", reply_markup=kb)


@router.message(F.text == BTN_BOOK)
async def book_from_menu(m: Message, state: FSMContext):
    await start_booking(m, state, m.from_user.id)


@router.callback_query(F.data == "book")
async def book_from_button(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await start_booking(c.message, state, c.from_user.id)


@router.callback_query(Booking.consent, F.data.startswith("consent:"))
async def got_consent(c: CallbackQuery, state: FSMContext):
    await c.answer()
    if c.data == "consent:yes":
        save_consent(c.from_user.id)
        await c.message.edit_reply_markup()
        await ask_student(c.message, state)
    else:
        await state.clear()
        await c.message.edit_reply_markup()
        await c.message.answer(
            "Хорошо. Если передумаете — нажмите «Записаться на пробный урок» "
            f"или позвоните нам: {SCHOOL['phone']}.",
            reply_markup=main_menu(),
        )


@router.callback_query(Booking.student, F.data.startswith("student:"))
async def got_student(c: CallbackQuery, state: FSMContext):
    await c.answer()
    types = SCHOOL.get("student_types", ["Ребёнок", "Подросток", "Взрослый"])
    student = types[int(c.data.split(":")[1])]
    await state.update_data(student=student)
    await c.message.edit_text(f"Кто будет учиться: <b>{student}</b>")
    await state.set_state(Booking.course)
    rows = [[InlineKeyboardButton(text=co["name"], callback_data=f"course:{i}")] for i, co in enumerate(SCHOOL["courses"])]
    rows.append([InlineKeyboardButton(text="Пока не знаю, подскажите на уроке", callback_data="course:-1")])
    await c.message.answer("Какой курс интересует?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(Booking.course, F.data.startswith("course:"))
async def got_course(c: CallbackQuery, state: FSMContext):
    await c.answer()
    idx = int(c.data.split(":")[1])
    course = SCHOOL["courses"][idx]["name"] if idx >= 0 else "подобрать на пробном уроке"
    await state.update_data(course=course)
    await c.message.edit_text(f"Курс: <b>{course}</b>")
    await ask_slot(c.message, state)


async def ask_slot(m: Message, state: FSMContext):
    await state.set_state(Booking.slot)
    slots = upcoming_slots()
    rows = [[InlineKeyboardButton(text=fmt_slot(s), callback_data=f"slot:{s}")] for s in slots]
    rows.append([InlineKeyboardButton(text="Нет удобного времени", callback_data="slot:none")])
    trial = SCHOOL.get("trial", {})
    await m.answer(
        f"Выберите время пробного урока ({trial.get('duration_min', 45)} минут, {trial.get('price', 'бесплатно')}):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(Booking.slot, F.data.startswith("slot:"))
async def got_slot(c: CallbackQuery, state: FSMContext):
    await c.answer()
    value = c.data.split(":", 1)[1]
    if value == "none":
        await state.update_data(slot=None)
        await c.message.edit_text("Время: подберём вместе с администратором")
        await state.set_state(Booking.wish)
        await c.message.answer("Напишите, в какие дни и часы вам удобно — администратор подберёт время.")
        return
    if booked_count(value) >= CAPACITY:
        await c.message.edit_text("Это время только что заняли.")
        await ask_slot(c.message, state)
        return
    await state.update_data(slot=value, wish=None)
    await c.message.edit_text(f"Время: <b>{fmt_slot(value)}</b>")
    await name_or_summary(c.message, state)


@router.message(Booking.wish, F.text)
async def got_wish(m: Message, state: FSMContext):
    await state.update_data(wish=m.text[:300])
    await name_or_summary(m, state)


async def name_or_summary(m: Message, state: FSMContext):
    """При переносе урока имя и телефон уже известны — сразу к подтверждению."""
    data = await state.get_data()
    if data.get("name") and data.get("phone"):
        await show_summary(m, state)
    else:
        await ask_name(m, state)


async def ask_name(m: Message, state: FSMContext):
    await state.set_state(Booking.name)
    await m.answer("Как к вам обращаться? Если учится ребёнок — напишите и его имя.")


@router.message(Booking.name, F.text)
async def got_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text.strip()[:100])
    await state.set_state(Booking.phone)
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Отправить мой номер", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await m.answer("Оставьте номер телефона — нажмите кнопку ниже или напишите его.", reply_markup=kb)


def normalize_phone(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] in "78":
        digits = digits[1:]
    if len(digits) == 10:
        return f"+7 {digits[:3]} {digits[3:6]} {digits[6:8]} {digits[8:]}"
    if 10 <= len(digits) <= 15:
        return "+" + digits
    return None


@router.message(Booking.phone, F.contact | F.text)
async def got_phone(m: Message, state: FSMContext):
    raw = m.contact.phone_number if m.contact else (m.text or "")
    phone = normalize_phone(raw)
    if not phone:
        await m.answer("Не похоже на номер телефона. Напишите, например: +7 701 123 45 67")
        return
    await state.update_data(phone=phone)
    await m.answer("Почти готово.", reply_markup=main_menu())
    await show_summary(m, state)


async def show_summary(m: Message, state: FSMContext):
    data = await state.get_data()
    await state.set_state(Booking.confirm)
    summary = (
        "<b>Проверьте запись:</b>\n"
        f"Кто учится: {esc(data['student'])}\n"
        f"Курс: {esc(data['course'])}\n"
        f"Время: {fmt_slot(data.get('slot'))}\n"
        + (f"Пожелание по времени: {esc(data['wish'])}\n" if data.get("wish") else "")
        + f"Имя: {esc(data['name'])}\n"
        f"Телефон: {esc(data['phone'])}"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="Подтвердить", callback_data="confirm:yes"),
            InlineKeyboardButton(text="Изменить", callback_data="confirm:edit"),
        ]]
    )
    await m.answer(summary, reply_markup=kb)


@router.callback_query(Booking.confirm, F.data.startswith("confirm:"))
async def got_confirm(c: CallbackQuery, state: FSMContext, bot: Bot):
    await c.answer()
    if c.data == "confirm:edit":
        await c.message.edit_reply_markup()
        await state.update_data(name=None, phone=None)
        await ask_student(c.message, state)
        return
    data = await state.get_data()
    slot = data.get("slot")
    if slot and booked_count(slot) >= CAPACITY:
        await c.message.edit_text("Пока вы заполняли, это время заняли. Выберите другое:")
        await ask_slot(c.message, state)
        return
    # если урок уже скоро, «ранние» напоминания не нужны
    r24 = r2 = 0
    if slot:
        left = datetime.fromisoformat(slot) - now_local()
        r24 = int(left <= timedelta(hours=24))
        r2 = int(left <= timedelta(hours=2))
    lead_id = db_exec(
        """INSERT INTO leads(created_at, user_id, username, student, course, name, phone, slot, wish,
                             status, reminded_24h, reminded_2h)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            now_local().isoformat(timespec="seconds"), c.from_user.id, c.from_user.username,
            data["student"], data["course"], data["name"], data["phone"], slot, data.get("wish"),
            "записан" if slot else "ждёт время", r24, r2,
        ),
    )
    await state.clear()
    await c.message.edit_reply_markup()
    if slot:
        text = (
            f"Готово! Ждём вас <b>{fmt_slot(slot)}</b>.\n"
            f"Адрес: {SCHOOL['address']}\n"
            "Напомним за день и за 2 часа до урока."
        )
    else:
        text = "Готово! Администратор свяжется с вами и подберёт удобное время."
    await c.message.answer(text, reply_markup=main_menu())
    await notify_admin(
        bot,
        f"<b>Новая запись на пробный урок #{lead_id}</b>\n"
        f"Кто учится: {esc(data['student'])}\n"
        f"Курс: {esc(data['course'])}\n"
        f"Время: {fmt_slot(slot)}\n"
        + (f"Пожелание: {esc(data['wish'])}\n" if data.get("wish") else "")
        + f"Имя: {esc(data['name'])}\n"
        f"Телефон: {esc(data['phone'])}\n"
        f"Telegram: {tg_name(c)}",
    )

# ---------------------------------------------------------------- напоминания

def reminder_kb(lead_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="Приду", callback_data=f"rem:ok:{lead_id}"),
            InlineKeyboardButton(text="Перенести", callback_data=f"rem:move:{lead_id}"),
        ]]
    )


async def reminder_loop(bot: Bot):
    while True:
        try:
            now = now_local()
            rows = db_query(
                f"""SELECT * FROM leads WHERE slot IS NOT NULL
                    AND status IN ({','.join('?' * len(ACTIVE_STATUSES))})
                    AND (reminded_24h=0 OR reminded_2h=0)""",
                ACTIVE_STATUSES,
            )
            for r in rows:
                left = datetime.fromisoformat(r["slot"]) - now
                if left <= timedelta(0):
                    continue
                if not r["reminded_2h"] and left <= timedelta(hours=2):
                    # флаг ставим до отправки: если клиент заблокировал бота, не повторяем каждую минуту
                    db_exec("UPDATE leads SET reminded_2h=1, reminded_24h=1 WHERE id=?", (r["id"],))
                    text = (
                        f"Напоминаем: пробный урок сегодня в {datetime.fromisoformat(r['slot']):%H:%M}.\n"
                        f"Адрес: {SCHOOL['address']}"
                    )
                elif not r["reminded_24h"] and left <= timedelta(hours=24):
                    db_exec("UPDATE leads SET reminded_24h=1 WHERE id=?", (r["id"],))
                    text = f"Напоминаем: пробный урок {fmt_slot(r['slot'])}.\nПодтвердите, пожалуйста:"
                else:
                    continue
                try:
                    await bot.send_message(r["user_id"], text, reply_markup=reminder_kb(r["id"]))
                except Exception:
                    log.exception("Не удалось отправить напоминание по заявке #%s", r["id"])
        except Exception:
            log.exception("Ошибка в цикле напоминаний")
        await asyncio.sleep(60)


@router.callback_query(F.data.startswith("rem:"))
async def reminder_answer(c: CallbackQuery, state: FSMContext, bot: Bot):
    await c.answer()
    _, action, lead_id = c.data.split(":")
    rows = db_query("SELECT * FROM leads WHERE id=? AND user_id=?", (int(lead_id), c.from_user.id))
    if not rows:
        return
    lead = rows[0]
    await c.message.edit_reply_markup()
    if action == "ok":
        db_exec("UPDATE leads SET status='подтвердил' WHERE id=?", (lead["id"],))
        await c.message.answer("Спасибо, ждём вас!")
        await notify_admin(bot, f"Запись #{lead['id']} ({esc(lead['name'])}, {fmt_slot(lead['slot'])}) подтверждена.")
    else:
        db_exec("UPDATE leads SET status='перенос' WHERE id=?", (lead["id"],))
        await notify_admin(bot, f"Запись #{lead['id']} ({esc(lead['name'])}, {fmt_slot(lead['slot'])}) — клиент переносит.")
        await state.set_data({
            "student": lead["student"], "course": lead["course"],
            "name": lead["name"], "phone": lead["phone"],
        })
        await c.message.answer("Без проблем, выберите новое время.")
        await ask_slot(c.message, state)

# ---------------------------------------------------------------- команды администратора

@router.message(Command("leads"))
async def cmd_leads(m: Message):
    if not is_admin(m):
        return
    rows = db_query("SELECT * FROM leads ORDER BY id DESC LIMIT 10")
    if not rows:
        await m.answer("Заявок пока нет.")
        return
    lines = [
        f"#{r['id']} · {fmt_slot(r['slot'])} · {esc(r['name'])} · {esc(r['phone'])} · {esc(r['course'])} · {r['status']}"
        for r in rows
    ]
    await m.answer("<b>Последние заявки</b>\n" + "\n".join(lines))


@router.message(Command("export"))
async def cmd_export(m: Message):
    if not is_admin(m):
        return
    rows = db_query("SELECT * FROM leads ORDER BY id")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["№", "Создана", "Кто учится", "Курс", "Имя", "Телефон", "Время урока", "Пожелание", "Статус", "Telegram"])
    for r in rows:
        w.writerow([
            r["id"], r["created_at"], r["student"], r["course"], r["name"], r["phone"],
            fmt_slot(r["slot"]) if r["slot"] else "", r["wish"] or "", r["status"],
            f"@{r['username']}" if r["username"] else r["user_id"],
        ])
    data = ("﻿" + buf.getvalue()).encode("utf-8")  # BOM, чтобы Excel открыл кириллицу
    await m.answer_document(BufferedInputFile(data, filename=f"zayavki_{now_local():%Y-%m-%d}.csv"))


@router.message(Command("reply"))
async def cmd_reply(m: Message, bot: Bot):
    if not is_admin(m):
        return
    parts = (m.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].lstrip("-").isdigit():
        await m.answer("Формат: /reply <id пользователя> <текст ответа>")
        return
    try:
        await bot.send_message(int(parts[1]), parts[2])
        await m.answer("Отправлено.")
    except Exception as e:
        await m.answer(f"Не удалось отправить: {e}")

# ---------------------------------------------------------------- свободные вопросы

@router.message(StateFilter(None), F.text & ~F.text.startswith("/"))
async def free_question(m: Message, bot: Bot):
    await bot.send_chat_action(m.chat.id, "typing")
    answer, escalate = await answer_question(m.text)
    db_exec(
        "INSERT INTO questions(at, user_id, username, text, answer, escalated) VALUES(?,?,?,?,?,?)",
        (now_local().isoformat(timespec="seconds"), m.from_user.id, m.from_user.username, m.text, answer, int(escalate)),
    )
    await m.answer(answer, parse_mode=None, reply_markup=book_button())
    if escalate:
        await notify_admin(
            bot,
            f"<b>Вопрос, на который бот не ответил</b>\nОт: {tg_name(m)} (id <code>{m.from_user.id}</code>)\n"
            f"Текст: {esc(m.text)}\n\nОтветить: <code>/reply {m.from_user.id} ваш ответ</code>",
        )

# ---------------------------------------------------------------- запуск

TOKEN_RE = re.compile(r"^\d{6,}:[\w-]{30,}$")


def ask_token_and_save() -> str:
    """Первый запуск: просим вставить токен в окне и сохраняем его в .env."""
    print("\nНужен токен бота от @BotFather (кнопка Copy в настройках бота).")
    while True:
        token = input("Вставьте токен и нажмите Enter: ").strip().strip('"').strip("'")
        if TOKEN_RE.match(token):
            break
        print("Не похоже на токен. Он выглядит так: 1234567890:AAE...")
    env_path = BASE / ".env"
    template = BASE / ".env.example"
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else (
        template.read_text(encoding="utf-8") if template.exists() else "BOT_TOKEN=\n"
    )
    if re.search(r"^BOT_TOKEN=.*$", text, flags=re.M):
        text = re.sub(r"^BOT_TOKEN=.*$", f"BOT_TOKEN={token}", text, count=1, flags=re.M)
    else:
        text = f"BOT_TOKEN={token}\n" + text
    env_path.write_text(text, encoding="utf-8")
    print("Токен сохранён в .env — в следующий раз вводить не нужно.\n")
    return token


async def main(token: str):
    init_db()
    bot = Bot(token, default=DefaultBotProperties(parse_mode="HTML"))
    me = await bot.get_me()
    log.info("Подключён бот @%s", me.username)
    dp = Dispatcher()
    dp.include_router(router)
    reminders = asyncio.create_task(reminder_loop(bot))  # держим ссылку, чтобы задачу не собрал GC
    log.info("Бот запущен. ИИ-ответы: %s. Админ-чат: %s", "включены" if _claude else "выключены", ADMIN_CHAT_ID or "не задан")
    await dp.start_polling(bot)


if __name__ == "__main__":
    token = BOT_TOKEN
    if not token:
        if not sys.stdin or not sys.stdin.isatty():
            raise SystemExit("Не задан BOT_TOKEN. Укажите его в файле .env.")
        token = ask_token_and_save()
    try:
        asyncio.run(main(token))
    except KeyboardInterrupt:
        print("Бот остановлен.")
