# -*- coding: utf-8 -*-
"""
Бот бесплатного марафона Code & Cash (2 дня).

Как это работает, коротко:
0. Пользователь жмёт /start (или пишет «старт») и получает условия:
   а) нажать «Перейти в TikTok» и подписаться, б) подать заявку в закрытый канал.
   Пока не выполнит оба условия, бот не запускается.
1. После этого: картинка + приветствие, кнопка "Стартуем".
   В этот момент тебе (админу) приходит сообщение «Марафон начал» с именем, @username,
   id и порядковым номером участника
2. Читает правила, жмёт "Погнали"
3. Получает день 1 с картинкой, выбирает нишу (сайты / боты / дашборды)
4. Получает задание, делает что-то руками, присылает результат в бота
5. Результат пересылается тебе (админу) вместе с именем, @username и id участника
   и кнопкой "Открыть день 2"
6. Ты жмёшь кнопку, участнику приходит "Куратор всё принял" с зелёной кнопкой ✅ОТКРЫВАЮ
7. Участник жмёт ОТКРЫВАЮ, получает день 2 по своей нише с синей кнопкой 🎉Итоги
8. 🎉Итоги: итог марафона -> "Что такое C&C Family?" -> "Как войти?"

Все тексты и картинки редактируются в content.py и папке images/,
сюда лезть не обязательно, если просто хочешь поменять слова или картинку.

Настройка:
1. pip install -r requirements.txt
2. Скопируй .env.example в .env и впиши туда:
   - BOT_TOKEN (токен от @BotFather)
   - ADMIN_CHAT_ID (твой личный chat_id в Telegram, куда будут падать работы на проверку)
3. Добавь бота АДМИНИСТРАТОРОМ закрытого канала с правом «Приглашать пользователей»
   (иначе он не увидит заявки на вступление)
4. python bot.py
"""

import asyncio
import html
import json
import logging
import os
import re
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    ChatJoinRequest,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    User,
)
from dotenv import load_dotenv

import content

# ---------------------------------------------------------------------------
# Настройка и хранение состояния
# ---------------------------------------------------------------------------

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
CONTACT_HANDLE = os.getenv("CONTACT_HANDLE", "@SUN9ISE")
# 1 = ты (админ) при /start пропускаешь проверку подписки, 0 = проходишь как все
ADMIN_BYPASS_SUBSCRIPTION = os.getenv("ADMIN_BYPASS_SUBSCRIPTION", "0") == "1"
# Необязательно: числовой id закрытого канала (вида -1001234567890). Если не задан,
# бот сам определит канал по первой заявке, пришедшей по ссылке из content.JOIN_REQUEST_URL.
JOIN_CHAT_ID_ENV = int(os.getenv("JOIN_CHAT_ID", "0") or 0)

if not BOT_TOKEN:
    raise SystemExit("Не задан BOT_TOKEN. Проверь файл .env")

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "users_state.json"
JOINS_FILE = BASE_DIR / "join_requests.json"  # кто подал заявку в закрытый канал
STATS_FILE = BASE_DIR / "marathon_stats.json"  # сколько человек уже начали марафон

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("marathon_bot")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Лимит Telegram на подпись к фото
CAPTION_LIMIT = 1024

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def get_user(state: dict, user_id: int) -> dict:
    key = str(user_id)
    if key not in state:
        state[key] = {"stage": "new", "niche": None}
    return state[key]


def advance(user_id: int, allowed_from: set, to_stage: str) -> bool:
    """
    Атомарно (без await между проверкой и записью) переводит участника
    на следующий этап, если он сейчас на одном из разрешённых.
    Защита от двойных нажатий: второе нажатие вернёт False и ничего не отправит.
    """
    state = load_state()
    user = get_user(state, user_id)
    if user["stage"] not in allowed_from:
        return False
    user["stage"] = to_stage
    save_state(state)
    return True


async def send_image(chat_id: int, image_key: str) -> None:
    """Отправляет картинку из content.IMAGES без подписи, отдельным сообщением."""
    path = BASE_DIR / content.IMAGES[image_key]
    if not path.exists():
        log.warning("Картинка не найдена: %s", path)
        return
    await bot.send_photo(chat_id=chat_id, photo=FSInputFile(path))


def _visible_len(html_text: str) -> int:
    """Длина текста без тегов (в единицах UTF-16, как считает Telegram, с запасом)."""
    plain = html.unescape(re.sub(r"<[^>]+>", "", html_text))
    return len(plain.encode("utf-16-le")) // 2


async def send_step(
    chat_id: int,
    text: str,
    image_key: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
    disable_preview: bool = False,
) -> None:
    """
    Отправляет шаг марафона одним сообщением: картинка + текст подписью + кнопка под ним.
    Если текст длиннее лимита подписи (1024), картинка уходит отдельно, а текст с кнопкой
    следом. Без картинки просто текст с кнопкой.
    """
    path = BASE_DIR / content.IMAGES[image_key] if image_key else None
    if path is not None and not path.exists():
        log.warning("Картинка не найдена: %s", path)
        path = None

    preview = NO_PREVIEW if disable_preview else None

    if path is None:
        await bot.send_message(
            chat_id=chat_id, text=text, reply_markup=reply_markup, link_preview_options=preview
        )
    elif _visible_len(text) <= CAPTION_LIMIT:
        await bot.send_photo(
            chat_id=chat_id, photo=FSInputFile(path), caption=text, reply_markup=reply_markup
        )
    else:
        await bot.send_photo(chat_id=chat_id, photo=FSInputFile(path))
        await bot.send_message(
            chat_id=chat_id, text=text, reply_markup=reply_markup, link_preview_options=preview
        )


async def strip_buttons(callback: CallbackQuery) -> None:
    """Убирает кнопки под сообщением, на котором нажали (чтобы не жали дважды)."""
    try:
        await bot.edit_message_reply_markup(
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            reply_markup=None,
        )
    except TelegramAPIError:
        pass


# ---------------------------------------------------------------------------
# Клавиатуры
# ---------------------------------------------------------------------------

def btn(text: str, callback_data: str | None = None, url: str | None = None,
        style: str | None = None) -> InlineKeyboardButton:
    """
    style: None (обычная), "success" (зелёная), "primary" (синяя), "danger" (красная).
    Цвет кнопок работает в актуальных версиях Telegram, в старых кнопка будет обычной.
    """
    return InlineKeyboardButton(text=text, callback_data=callback_data, url=url, style=style)


def kb_sub_gate() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn(content.BTN_TIKTOK, callback_data="tt_go")],
            [btn(content.BTN_JOIN_REQUEST, url=content.JOIN_REQUEST_URL)],
            [btn(content.BTN_SUB_CHECK, callback_data="check_sub")],
        ]
    )


def kb_tiktok_link() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[btn(content.BTN_TIKTOK_OPEN, url=content.TIKTOK_URL)]]
    )


def kb_go() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[btn(content.BTN_GO, "go")]])


def kb_start_day1() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[btn(content.BTN_START_DAY1, "start_day1")]]
    )


def kb_niches() -> InlineKeyboardMarkup:
    buttons = [
        [btn(label, f"niche_{key}")] for key, label in content.NICHE_BUTTONS.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def kb_submit() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[btn(content.BTN_SUBMIT, "submit")]])


def kb_open_day2() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[btn(content.BTN_OPEN_DAY2, "open_day2", style="success")]]
    )


def kb_results() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[btn(content.BTN_RESULTS, "results", style="primary")]]
    )


def kb_about_family() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[btn(content.BTN_ABOUT_FAMILY, "about_family")]]
    )


def kb_how_to_join() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[btn(content.BTN_HOW_TO_JOIN, "how_to_join")]]
    )


def admin_approve_button(user_id: int) -> InlineKeyboardButton:
    return btn(content.ADMIN_APPROVE_BUTTON_TEXT, f"approve_{user_id}")


def kb_admin_approve(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[admin_approve_button(user_id)]])


# ---------------------------------------------------------------------------
# Проверка подписки
# ---------------------------------------------------------------------------

_last_admin_alert = 0.0


async def alert_admin(text: str) -> None:
    """Пишет админу о проблеме, не чаще раза в 10 минут, чтобы не спамить."""
    global _last_admin_alert
    now = time.monotonic()
    if _last_admin_alert and now - _last_admin_alert < 600:
        return
    _last_admin_alert = now
    try:
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=text)
    except TelegramAPIError:
        log.exception("Не удалось написать админу")


def load_joins() -> dict:
    if JOINS_FILE.exists():
        return json.loads(JOINS_FILE.read_text(encoding="utf-8"))
    return {"chat_id": None, "users": {}}


def save_joins(joins: dict) -> None:
    JOINS_FILE.write_text(json.dumps(joins, ensure_ascii=False, indent=2), encoding="utf-8")


def known_join_chat_id() -> int | None:
    """Id закрытого канала: из .env или запомненный по первой заявке."""
    return JOIN_CHAT_ID_ENV or load_joins().get("chat_id")


def _link_core(link: str) -> str:
    link = link.strip().replace("https://", "").replace("http://", "")
    return link.rstrip("…").rstrip(".")


def link_matches(event_link: str | None, our_link: str) -> bool:
    """
    Telegram присылает в заявке ссылку, по которой человек пришёл. Если ссылку создал не бот,
    вторая часть заменяется на «…», поэтому сравниваем по началу.
    """
    if not event_link:
        return False
    a, b = _link_core(event_link), _link_core(our_link)
    return len(a) >= len("t.me/+") + 6 and b.startswith(a)


@dp.chat_join_request()
async def on_join_request(event: ChatJoinRequest):
    """Запоминаем, кто подал заявку в наш закрытый канал."""
    joins = load_joins()
    link = event.invite_link.invite_link if event.invite_link else None

    chat_known = known_join_chat_id()
    ours = (chat_known is not None and event.chat.id == chat_known) or link_matches(
        link, content.JOIN_REQUEST_URL
    )
    if not ours:
        log.info(
            "Заявка не в наш канал: chat=%s (%s), ссылка=%s. Если это нужный канал, "
            "впиши JOIN_CHAT_ID=%s в .env",
            event.chat.id, event.chat.title, link, event.chat.id,
        )
        return

    if not joins.get("chat_id") and not JOIN_CHAT_ID_ENV:
        joins["chat_id"] = event.chat.id  # запоминаем канал для проверки уже принятых участников
        log.info("Определил закрытый канал: %s (id %s)", event.chat.title, event.chat.id)
    joins["users"][str(event.from_user.id)] = int(time.time())
    save_joins(joins)


async def check_join_request(user_id: int) -> bool | None:
    """
    True  - человек подал заявку в закрытый канал (или уже в нём состоит)
    False - заявки не видно
    None  - проверить не получилось
    """
    joins = load_joins()
    if str(user_id) in joins["users"]:
        return True

    chat_id = known_join_chat_id()
    if not chat_id:
        # Канал ещё не определён: скорее всего бот не админ канала или заявку подали до запуска
        await alert_admin(
            "⚠️ Участник нажал «Я подписался», но заявок в закрытый канал бот ещё не видел. "
            "Проверь, что бот добавлен АДМИНИСТРАТОРОМ канала с правом «Приглашать "
            "пользователей», и что заявку подали уже после запуска бота. "
            "Если заявка была отправлена раньше, её надо отменить и подать заново."
        )
        return False

    # заявку могли уже одобрить: тогда человек просто участник канала
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
    except TelegramAPIError as e:
        log.error("Не удалось проверить участника закрытого канала: %s", e)
        return False
    if member.status in ("creator", "administrator", "member"):
        return True
    if member.status == "restricted":
        return bool(getattr(member, "is_member", False))
    return False  # left / kicked


async def send_sub_gate(chat_id: int) -> None:
    await bot.send_message(
        chat_id=chat_id,
        text=content.SUB_GATE,
        reply_markup=kb_sub_gate(),
        link_preview_options=NO_PREVIEW,
    )


# Текст уведомления админу о том, что человек начал марафон.
# Лежит здесь, а не в content.py, чтобы для этой функции хватало заменить один файл bot.py.
MARATHON_STARTED_ADMIN = (
    "<b>Марафон начал</b>\n\n"
    "Имя: {name}\n"
    "Username: {username}\n"
    "ID: <code>{user_id}</code>\n\n"
    "Человек по счету: <b>{number}</b>"
)


def load_stats() -> dict:
    if STATS_FILE.exists():
        return json.loads(STATS_FILE.read_text(encoding="utf-8"))
    return {}


def save_stats(stats: dict) -> None:
    STATS_FILE.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")


def next_marathon_number() -> int:
    """
    Порядковый номер участника, который только что начал марафон.

    При самом первом вызове бот сам считает всех, кто уже начал марафон раньше
    (по users_state.json, без тебя как админа), и продолжает счёт с этого числа.
    Дальше просто +1 на каждого нового. Вызывается сразу после перевода человека
    на этап «welcomed», поэтому он уже входит в подсчёт.
    """
    stats = load_stats()
    if "started" not in stats:
        state = load_state()
        stats["started"] = sum(
            1
            for uid, u in state.items()
            if u.get("stage") != "new" and str(uid) != str(ADMIN_CHAT_ID)
        )
    else:
        stats["started"] += 1
    save_stats(stats)
    return stats["started"]


async def notify_marathon_started(user: User) -> None:
    """Пишет админу, что человек начал марафон. Любые сбои тут не должны ломать марафон."""
    try:
        if user.id == ADMIN_CHAT_ID:
            number = "тест, в счёт не идёт"  # админ проходит марафон для проверки
        else:
            number = str(next_marathon_number())

        full_name = " ".join(p for p in (user.first_name, user.last_name) if p) or "без имени"
        username = f"@{user.username}" if user.username else "нет username"
        await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=MARATHON_STARTED_ADMIN.format(
                name=html.escape(full_name),
                username=html.escape(username),
                user_id=user.id,
                number=number,
            ),
        )
    except Exception:
        log.exception("Не удалось отправить уведомление «Марафон начал»")


async def begin_marathon(chat_id: int, user: User) -> None:
    """Условия выполнены: сообщаем админу и показываем участнику приветствие (один раз)."""
    if not advance(user.id, {"new"}, "welcomed"):
        return
    await notify_marathon_started(user)
    name = html.escape(user.first_name or "друг")
    await send_image(chat_id, "welcome")
    await bot.send_message(
        chat_id=chat_id, text=content.WELCOME.format(name=name), reply_markup=kb_go()
    )


# ---------------------------------------------------------------------------
# Хендлеры участника
# ---------------------------------------------------------------------------

START_WORDS = {"старт", "start"}


def is_start_word(message: Message) -> bool:
    text = (message.text or "").strip().lower().strip("/!.,")
    return text in START_WORDS


async def handle_start(message: Message):
    state = load_state()
    user = get_user(state, message.from_user.id)
    is_admin = message.from_user.id == ADMIN_CHAT_ID

    if is_admin:
        # админу сбрасываем прогресс каждый раз, чтобы можно было
        # проходить марафон сколько угодно раз для проверки
        user["stage"] = "new"
        user["niche"] = None
        user.pop("tt_tapped_at", None)
        save_state(state)
    elif user["stage"] != "new":
        await message.answer(
            "Ты уже начал марафон раньше, повторно пройти его нельзя 🙌\n\n"
            "Если что-то пошло не так, напиши мне лично: " + CONTACT_HANDLE
        )
        return
    else:
        save_state(state)  # запоминаем нового участника

    if is_admin and ADMIN_BYPASS_SUBSCRIPTION:
        await begin_marathon(message.chat.id, message.from_user)
        return

    # Без подписки бот не запускается: сначала условия
    await send_sub_gate(message.chat.id)


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await handle_start(message)


@dp.message(F.text, is_start_word)
async def txt_start(message: Message):
    """Участник написал слово «старт» вместо нажатия кнопки Start."""
    state = load_state()
    user = get_user(state, message.from_user.id)
    if user["stage"] == "waiting_submission":
        # человек сейчас сдаёт работу, а не запускает бота
        await handle_submission(message)
        return
    await handle_start(message)


@dp.callback_query(F.data == "tt_go")
async def cb_tiktok(callback: CallbackQuery):
    """Нажатие «Перейти в TikTok»: запоминаем момент и выдаём ссылку."""
    state = load_state()
    user = get_user(state, callback.from_user.id)
    if user["stage"] != "new":
        await callback.answer()
        return
    if not user.get("tt_tapped_at"):
        user["tt_tapped_at"] = time.time()  # повторные нажатия отсчёт не сбрасывают
        save_state(state)

    await callback.answer()
    await bot.send_message(
        chat_id=callback.message.chat.id,
        text=content.TIKTOK_LINK_MESSAGE,
        reply_markup=kb_tiktok_link(),
        link_preview_options=NO_PREVIEW,
    )


@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(callback: CallbackQuery):
    user_id = callback.from_user.id

    state = load_state()
    user = get_user(state, user_id)
    if user["stage"] != "new":
        await callback.answer()  # уже прошёл проверку (двойное нажатие)
        return

    # 1) TikTok: должен был нажать кнопку и потратить время на подписку
    tapped_at = user.get("tt_tapped_at")
    if not tapped_at:
        await callback.answer(content.SUB_TIKTOK_NOT_TAPPED_ALERT, show_alert=True)
        return
    wait = int(content.TIKTOK_MIN_SECONDS - (time.time() - tapped_at))
    if wait > 0:
        await callback.answer(
            content.SUB_TIKTOK_TOO_FAST_ALERT.format(seconds=wait + 1), show_alert=True
        )
        return

    # 2) Заявка в закрытый канал: проверяется по-настоящему
    sent = await check_join_request(user_id)
    if sent is None:
        await callback.answer(content.SUB_CHECK_FAILED_ALERT, show_alert=True)
        return
    if not sent:
        await callback.answer(content.SUB_NO_REQUEST_ALERT, show_alert=True)
        return

    await callback.answer()
    await strip_buttons(callback)
    await begin_marathon(callback.message.chat.id, callback.from_user)


@dp.callback_query(F.data == "go")
async def cb_go(callback: CallbackQuery):
    state = load_state()
    user = get_user(state, callback.from_user.id)
    user["stage"] = "rules_shown"
    save_state(state)

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(content.RULES, reply_markup=kb_start_day1())
    await callback.answer()


@dp.callback_query(F.data == "start_day1")
async def cb_start_day1(callback: CallbackQuery):
    state = load_state()
    user = get_user(state, callback.from_user.id)
    user["stage"] = "day1_niche_choice"
    save_state(state)

    await callback.message.edit_reply_markup(reply_markup=None)
    await send_image(callback.message.chat.id, "day1_intro")
    await callback.message.answer(content.DAY1_INTRO, reply_markup=kb_niches())
    await callback.answer()


@dp.callback_query(F.data.startswith("niche_"))
async def cb_niche_chosen(callback: CallbackQuery):
    niche = callback.data.replace("niche_", "")
    state = load_state()
    user = get_user(state, callback.from_user.id)
    user["stage"] = "day1_task_given"
    user["niche"] = niche
    save_state(state)

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(content.NICHE_PICKED[niche], reply_markup=kb_submit())
    await callback.answer()


@dp.callback_query(F.data == "submit")
async def cb_submit(callback: CallbackQuery):
    state = load_state()
    user = get_user(state, callback.from_user.id)
    user["stage"] = "waiting_submission"
    save_state(state)

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(content.SUBMIT_PROMPT)
    await callback.answer()


@dp.message(F.content_type.in_({"photo", "document", "text", "video"}))
async def handle_submission(message: Message):
    """Ловит присланную участником работу и пересылает админу."""
    state = load_state()
    user = get_user(state, message.from_user.id)

    if user["stage"] != "waiting_submission":
        return  # не относится к сдаче работы, игнорируем

    user["stage"] = "submitted"
    save_state(state)

    niche_label = content.NICHE_BUTTONS.get(user["niche"], "не указана")
    u = message.from_user

    # пересылаем оригинальное сообщение админу, чтобы куратор видел файл/скрин как есть
    await bot.forward_message(
        chat_id=ADMIN_CHAT_ID, from_chat_id=message.chat.id, message_id=message.message_id
    )

    # карточка участника: имя, @username, id и ссылка на профиль,
    # чтобы можно было сразу зайти к человеку и написать
    full_name = " ".join(p for p in (u.first_name, u.last_name) if p) or "без имени"
    username = f"@{u.username}" if u.username else "нет username"
    caption = content.SUBMIT_RECEIVED_ADMIN_CAPTION.format(
        name=html.escape(full_name),
        username=html.escape(username),
        user_id=u.id,
        profile_link=f'<a href="tg://user?id={u.id}">открыть профиль</a>',
        niche=niche_label,
    )
    write_url = f"https://t.me/{u.username}" if u.username else f"tg://user?id={u.id}"
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [admin_approve_button(u.id)],
            [btn(content.ADMIN_WRITE_BUTTON_TEXT, url=write_url)],
        ]
    )
    try:
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=caption, reply_markup=markup)
    except TelegramBadRequest as e:
        # у людей с закрытой приватностью Telegram может отклонить кнопку-ссылку по id
        # (BUTTON_USER_PRIVACY_RESTRICTED). Тогда шлём без неё: id и username всё равно в тексте.
        log.warning("Кнопку «Написать» отправить не удалось (%s), шлю без неё", e)
        await bot.send_message(
            chat_id=ADMIN_CHAT_ID, text=caption, reply_markup=kb_admin_approve(u.id)
        )

    await message.answer(content.SUBMIT_RECEIVED_USER.format(contact=CONTACT_HANDLE))


# ---------------------------------------------------------------------------
# Хендлер куратора (тебя)
# ---------------------------------------------------------------------------

DAY2_IMAGE_BY_NICHE = {
    "sites": "day2_sites",
    "bots": "day2_bots",
    "dashboards": "day2_dashboards",
}

# этапы, на которых день 2 участнику уже открыт
ALREADY_APPROVED_STAGES = {
    "approved", "day2_unlocked", "final_shown", "about_shown", "join_shown",
}

_approving: set[int] = set()


@dp.callback_query(F.data.startswith("approve_"))
async def cb_approve(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_CHAT_ID:
        await callback.answer("Только куратор может это нажать", show_alert=True)
        return

    target_user_id = int(callback.data.replace("approve_", ""))
    if target_user_id in _approving:  # двойное нажатие
        await callback.answer()
        return
    _approving.add(target_user_id)
    try:
        state = load_state()
        user = get_user(state, target_user_id)
        if user["stage"] in ALREADY_APPROVED_STAGES:
            await callback.answer("Этому участнику день 2 уже открыт", show_alert=True)
            await _strip_approve_button(callback)
            return

        # шаг "работа принята": картинка + текст + зелёная кнопка ✅ОТКРЫВАЮ
        try:
            await send_step(
                target_user_id, content.APPROVED_USER, "approved", kb_open_day2()
            )
        except TelegramAPIError as e:
            log.error("Не удалось отправить участнику %s: %s", target_user_id, e)
            await callback.answer(
                "Не удалось отправить участнику (возможно, он заблокировал бота)",
                show_alert=True,
            )
            return

        state = load_state()  # заново: пока слали сообщение, файл мог измениться
        get_user(state, target_user_id)["stage"] = "approved"
        save_state(state)

        await _strip_approve_button(callback)
        await callback.answer("Готово, участнику пришла кнопка «ОТКРЫВАЮ»")
    finally:
        _approving.discard(target_user_id)


async def _strip_approve_button(callback: CallbackQuery) -> None:
    """Убирает у админа только кнопку approve, кнопка «Написать участнику» остаётся."""
    markup = getattr(callback.message, "reply_markup", None)
    rows = []
    if markup:
        for row in markup.inline_keyboard:
            kept = [b for b in row if not (b.callback_data or "").startswith("approve_")]
            if kept:
                rows.append(kept)
    try:
        await bot.edit_message_reply_markup(
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
        )
    except TelegramAPIError:
        pass


# ---------------------------------------------------------------------------
# Хендлеры участника после проверки: день 2 -> итоги -> C&C Family
# ---------------------------------------------------------------------------

@dp.callback_query(F.data == "open_day2")
async def cb_open_day2(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not advance(user_id, {"approved"}, "day2_unlocked"):
        await callback.answer()
        return
    await callback.answer()
    await strip_buttons(callback)

    user = get_user(load_state(), user_id)
    niche = user.get("niche") or "sites"
    day2_text = content.DAY2_HEADER + content.DAY2_NICHE_CONTENT[niche]
    await send_step(
        callback.message.chat.id, day2_text, DAY2_IMAGE_BY_NICHE[niche], kb_results()
    )


@dp.callback_query(F.data == "results")
async def cb_results(callback: CallbackQuery):
    if not advance(callback.from_user.id, {"day2_unlocked"}, "final_shown"):
        await callback.answer()
        return
    await callback.answer()
    await strip_buttons(callback)

    # итог марафона без картинки
    await send_step(callback.message.chat.id, content.FINAL_MESSAGE, None, kb_about_family())


@dp.callback_query(F.data == "about_family")
async def cb_about_family(callback: CallbackQuery):
    if not advance(callback.from_user.id, {"final_shown"}, "about_shown"):
        await callback.answer()
        return
    await callback.answer()
    await strip_buttons(callback)

    await send_step(callback.message.chat.id, content.ABOUT_FAMILY, "ccfm", kb_how_to_join())


@dp.callback_query(F.data == "how_to_join")
async def cb_how_to_join(callback: CallbackQuery):
    if not advance(callback.from_user.id, {"about_shown"}, "join_shown"):
        await callback.answer()
        return
    await callback.answer()
    await strip_buttons(callback)

    text = content.HOW_TO_JOIN.format(
        spots_left_text=content.spots_left_text(),
        contact=CONTACT_HANDLE,
        tg_url=content.TG_CHANNEL_URL,
        reviews_url=content.SITE_REVIEWS_URL,
    )
    await send_step(callback.message.chat.id, text, None, None, disable_preview=True)


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

async def check_channel_access() -> None:
    """При старте проверяем, что бот админ закрытого канала (если канал уже известен)."""
    chat_id = known_join_chat_id()
    if not chat_id:
        log.info(
            "Закрытый канал пока не определён: определю по первой заявке (ссылка %s). "
            "Бот должен быть админом канала с правом «Приглашать пользователей».",
            content.JOIN_REQUEST_URL,
        )
        return

    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id=chat_id, user_id=me.id)
        if member.status == "administrator" and getattr(member, "can_invite_users", True):
            log.info("Приём заявок в закрытый канал (id %s) работает", chat_id)
            return
        if member.status == "creator":
            return
        problem = "бот в закрытом канале не администратор или у него нет права «Приглашать пользователей»"
    except TelegramAPIError as e:
        problem = f"нет доступа к закрытому каналу ({e})"

    log.warning("Заявки в канал не будут видны: %s", problem)
    try:
        await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=(
                "⚠️ Проверка заявок не заработает: " + problem + ".\n\n"
                "Сделай бота администратором закрытого канала с правом «Приглашать пользователей», "
                "иначе никто не сможет пройти условия."
            ),
        )
    except TelegramAPIError:
        log.exception("Не удалось написать админу")


async def main():
    if not ADMIN_CHAT_ID:
        raise SystemExit("Не задан ADMIN_CHAT_ID. Проверь файл .env")

    await check_channel_access()
    log.info("Бот марафона запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
