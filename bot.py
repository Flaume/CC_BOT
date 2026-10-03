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

Тексты и картинки сообщений меняются в панели (вкладка «Сообщения»); стандартные лежат в content.py и папке images/,
сюда лезть не обязательно, если просто хочешь поменять слова или картинку.

Только для админа (подробности в README):
- /edit: поменять текст и картинку последних двух сообщений прямо в боте
- /panel: мини-приложение (статистика, работы участников, рассылка)
- уведомление «Марафон закончил до конца» со счётчиком (/count, /setcount)

Настройка:
1. pip install -r requirements.txt
2. Скопируй .env.example в .env и впиши туда:
   - BOT_TOKEN (токен от @BotFather)
   - ADMIN_CHAT_ID (твой личный chat_id в Telegram, куда будут падать работы на проверку)
   - WEBAPP_URL (по желанию: публичная https-ссылка на бота для мини-приложения)
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

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
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

import admin_panel
import content
import storage
from storage import (
    advance,
    get_user,
    load_joins,
    load_state,
    load_stats,
    save_joins,
    save_state,
    save_stats,
)

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

BASE_DIR = storage.BASE_DIR
# Для мини-приложения админа: публичная https-ссылка на этот бот и порт, который он слушает
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()
WEBAPP_PORT = int(os.getenv("PORT") or os.getenv("WEBAPP_PORT") or 8080)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("marathon_bot")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
participant_router = Router()  # хендлеры участников; роутер админа подключается раньше него

# Лимит Telegram на подпись к фото
CAPTION_LIMIT = 1024

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def _visible_len(html_text: str) -> int:
    """Длина текста без тегов (в единицах UTF-16, как считает Telegram, с запасом)."""
    plain = html.unescape(re.sub(r"<[^>]+>", "", html_text))
    return len(plain.encode("utf-16-le")) // 2


_UNSET = object()


async def send_msg(
    chat_id: int,
    key: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    disable_preview: bool = False,
    text: str | None = None,
    image=_UNSET,
    **vars,
) -> None:
    """
    Отправляет сообщение бота по ключу из storage.MESSAGES: текст и картинка берутся с учётом
    того, что ты изменил в панели. text/image нужны только для предпросмотра в панели.
    Картинка и текст идут одним сообщением (подпись). Если текст длиннее лимита подписи (1024),
    а также у сообщений с режимом separate, картинка уходит отдельно, а текст с кнопкой следом.
    """
    meta = storage.MESSAGE_BY_KEY[key]
    text = storage.message_text(key, **vars) if text is None else storage.fill(text, **vars)
    path = storage.message_image(key) if image is _UNSET else image
    if path is not None and not path.exists():
        log.warning("Картинка не найдена: %s", path)
        path = None
    preview = NO_PREVIEW if disable_preview else None

    if path is None:
        await bot.send_message(
            chat_id=chat_id, text=text, reply_markup=reply_markup, link_preview_options=preview
        )
    elif meta.get("mode") != "separate" and _visible_len(text) <= CAPTION_LIMIT:
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


@participant_router.chat_join_request()
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
    await send_msg(chat_id, "sub_gate", kb_sub_gate(), disable_preview=True)


# Текст уведомления админу о том, что человек начал марафон.
# Лежит здесь, а не в content.py, чтобы для этой функции хватало заменить один файл bot.py.
MARATHON_STARTED_ADMIN = (
    "<b>Марафон начал</b>\n\n"
    "Имя: {name}\n"
    "Username: {username}\n"
    "ID: <code>{user_id}</code>\n\n"
    "Человек по счету: <b>{number}</b>"
)


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
    if user.id == ADMIN_CHAT_ID:
        return  # свои проверки админ не считает и уведомлений о них не получает
    try:
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
    try:
        await send_msg(chat_id, "welcome", kb_go(), name=name)
    except TelegramForbiddenError:
        log.warning("Участник %s заблокировал бота", user.id)
        storage.mark_blocked(user.id)


# ---------------------------------------------------------------------------
# Хендлеры участника
# ---------------------------------------------------------------------------

START_WORDS = {"старт", "start"}


def is_start_word(message: Message) -> bool:
    text = (message.text or "").strip().lower().strip("/!.,")
    return text in START_WORDS


async def admin_test_start() -> None:
    """Кнопка «Пройти марафон заново» в панели: сброс прогресса админа и сразу приветствие."""
    state = load_state()
    user = get_user(state, ADMIN_CHAT_ID)
    user["stage"] = "new"
    user["niche"] = None
    user.pop("tt_tapped_at", None)
    user.pop("submit_group", None)
    save_state(state)
    await begin_marathon(ADMIN_CHAT_ID, User(id=ADMIN_CHAT_ID, is_bot=False, first_name="Админ"))


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
        user.pop("submit_group", None)
        save_state(state)
        admin_panel.clear_pending()
    elif user["stage"] != "new":
        await message.answer(
            "Ты уже начал марафон раньше, повторно пройти его нельзя 🙌\n\n"
            "Если что-то пошло не так, напиши мне лично: " + CONTACT_HANDLE
        )
        return
    else:
        save_state(state)  # запоминаем нового участника

    if is_admin and (ADMIN_BYPASS_SUBSCRIPTION or storage.get_setting("test_mode", True)):
        await begin_marathon(message.chat.id, message.from_user)
        return

    # Без подписки бот не запускается: сначала условия
    await send_sub_gate(message.chat.id)


@participant_router.message(CommandStart())
async def cmd_start(message: Message):
    await handle_start(message)


@participant_router.message(F.text, is_start_word)
async def txt_start(message: Message):
    """Участник написал слово «старт» вместо нажатия кнопки Start."""
    state = load_state()
    user = get_user(state, message.from_user.id)
    if user["stage"] == "waiting_submission":
        # человек сейчас сдаёт работу, а не запускает бота
        await handle_submission(message)
        return
    await handle_start(message)


@participant_router.callback_query(F.data == "tt_go")
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
    await send_msg(callback.message.chat.id, "tiktok_link", kb_tiktok_link(), disable_preview=True)


@participant_router.callback_query(F.data == "check_sub")
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


@participant_router.callback_query(F.data == "go")
async def cb_go(callback: CallbackQuery):
    if not advance(callback.from_user.id, {"welcomed"}, "rules_shown", recover=True):
        await callback.answer()
        return
    await callback.answer()
    await strip_buttons(callback)
    await send_msg(callback.message.chat.id, "rules", kb_start_day1())


@participant_router.callback_query(F.data == "start_day1")
async def cb_start_day1(callback: CallbackQuery):
    if not advance(callback.from_user.id, {"rules_shown"}, "day1_niche_choice", recover=True):
        await callback.answer()
        return
    await callback.answer()
    await strip_buttons(callback)
    await send_msg(callback.message.chat.id, "day1_intro", kb_niches())


@participant_router.callback_query(F.data.startswith("niche_"))
async def cb_niche_chosen(callback: CallbackQuery):
    niche = callback.data.removeprefix("niche_")
    state = load_state()
    user = get_user(state, callback.from_user.id)
    if niche not in content.NICHE_PICKED or storage.stage_index(user["stage"]) >= storage.stage_index("day1_task_given"):
        await callback.answer()
        return
    user["stage"] = "day1_task_given"
    user["niche"] = niche
    save_state(state)

    await callback.answer()
    await strip_buttons(callback)
    await send_msg(callback.message.chat.id, f"niche_{niche}", kb_submit(), disable_preview=True)


@participant_router.callback_query(F.data == "submit")
async def cb_submit(callback: CallbackQuery):
    if not advance(callback.from_user.id, {"day1_task_given"}, "waiting_submission", recover=True):
        await callback.answer()
        return
    await callback.answer()
    await strip_buttons(callback)
    await send_msg(callback.message.chat.id, "submit_prompt")


SUBMIT_TYPES = {"photo", "document", "text", "video", "video_note", "voice", "animation", "audio"}


def _looks_like_work(message: Message) -> bool:
    """Скрин/файл принимаем сразу. Текст только если это ссылка или развёрнутый ответ,
    чтобы фраза «сейчас пришлю» не считалась сданной работой."""
    if message.text is None:
        return True
    text = message.text.strip()
    has_link = "http" in text.lower() or any(
        e.type in ("url", "text_link") for e in (message.entities or [])
    )
    return has_link or len(text) >= 40


def _submission_item(message: Message) -> dict | None:
    """Описание вложения для панели (file_id нужен, чтобы показать файл в мини-приложении)."""
    if message.photo:
        return {"type": "photo", "file_id": message.photo[-1].file_id, "mime": "image/jpeg"}
    if message.document:
        d = message.document
        return {"type": "document", "file_id": d.file_id, "mime": d.mime_type, "name": d.file_name}
    if message.video:
        return {"type": "video", "file_id": message.video.file_id, "mime": message.video.mime_type or "video/mp4"}
    if message.video_note:
        return {"type": "video", "file_id": message.video_note.file_id, "mime": "video/mp4"}
    if message.animation:
        return {"type": "video", "file_id": message.animation.file_id, "mime": message.animation.mime_type or "video/mp4"}
    if message.voice:
        return {"type": "audio", "file_id": message.voice.file_id, "mime": message.voice.mime_type or "audio/ogg"}
    if message.audio:
        return {"type": "audio", "file_id": message.audio.file_id, "mime": message.audio.mime_type or "audio/mpeg",
                "name": message.audio.file_name}
    return None


@participant_router.message(F.content_type.in_(SUBMIT_TYPES))
async def handle_submission(message: Message):
    """Ловит присланную участником работу, сохраняет её для панели и пересылает админу."""
    u = message.from_user
    state = load_state()
    user = get_user(state, u.id)
    group = message.media_group_id
    item = _submission_item(message)
    text = (message.text or message.caption or "").strip()
    full_name = " ".join(p for p in (u.first_name, u.last_name) if p) or "без имени"

    # остальные части альбома: человек прислал несколько скринов сразу
    if user["stage"] == "submitted" and group and user.get("submit_group") == group:
        if u.id != ADMIN_CHAT_ID:
            storage.add_submission(u.id, full_name, u.username, user.get("niche"), group, item, text)
        try:
            await bot.forward_message(
                chat_id=ADMIN_CHAT_ID, from_chat_id=message.chat.id, message_id=message.message_id
            )
        except TelegramAPIError:
            log.exception("Не удалось переслать часть альбома")
        return

    if user["stage"] != "waiting_submission":
        return  # не относится к сдаче работы, игнорируем

    if not _looks_like_work(message):
        await send_msg(message.chat.id, "submit_too_short")
        return

    user["stage"] = "submitted"
    user["submit_group"] = group
    save_state(state)
    sid = None if u.id == ADMIN_CHAT_ID else storage.add_submission(
        u.id, full_name, u.username, user.get("niche"), group, item, text
    )

    niche_label = content.NICHE_BUTTONS.get(user["niche"], "не указана")
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
        # пересылаем оригинал, чтобы куратор видел файл/скрин как есть
        await bot.forward_message(
            chat_id=ADMIN_CHAT_ID, from_chat_id=message.chat.id, message_id=message.message_id
        )
        try:
            await bot.send_message(chat_id=ADMIN_CHAT_ID, text=caption, reply_markup=markup)
        except TelegramBadRequest as e:
            # у людей с закрытой приватностью Telegram может отклонить кнопку-ссылку по id.
            # Тогда шлём без неё: id и username всё равно в тексте.
            log.warning("Кнопку «Написать» отправить не удалось (%s), шлю без неё", e)
            await bot.send_message(
                chat_id=ADMIN_CHAT_ID, text=caption, reply_markup=kb_admin_approve(u.id)
            )
    except TelegramAPIError:
        # админу не доставилось: откатываем, чтобы человек мог прислать работу ещё раз
        log.exception("Не удалось отправить работу админу")
        if sid is not None:
            storage.remove_submission(sid)
        state = load_state()
        get_user(state, u.id)["stage"] = "waiting_submission"
        save_state(state)
        await send_msg(message.chat.id, "submit_failed")
        return

    await send_msg(message.chat.id, "submit_received", contact=CONTACT_HANDLE)


# ---------------------------------------------------------------------------
# Хендлер куратора (тебя)
# ---------------------------------------------------------------------------

# этапы, на которых день 2 участнику уже открыт
ALREADY_APPROVED_STAGES = {
    "approved", "day2_unlocked", "final_shown", "about_shown", "join_shown",
}

_approving: set[int] = set()


@participant_router.callback_query(F.data.startswith("approve_"))
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
            await send_msg(target_user_id, "approved", kb_open_day2())
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

@participant_router.callback_query(F.data == "open_day2")
async def cb_open_day2(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not advance(user_id, {"approved"}, "day2_unlocked", recover=True):
        await callback.answer()
        return
    await callback.answer()
    await strip_buttons(callback)

    user = get_user(load_state(), user_id)
    niche = user.get("niche") if user.get("niche") in content.NICHE_PICKED else "sites"
    await send_msg(callback.message.chat.id, f"day2_{niche}", kb_results())


@participant_router.callback_query(F.data == "results")
async def cb_results(callback: CallbackQuery):
    if not advance(callback.from_user.id, {"day2_unlocked"}, "final_shown", recover=True):
        await callback.answer()
        return
    await callback.answer()
    await strip_buttons(callback)

    # итог марафона без картинки
    await send_msg(callback.message.chat.id, "final", kb_about_family())


@participant_router.callback_query(F.data == "about_family")
async def cb_about_family(callback: CallbackQuery):
    if not advance(callback.from_user.id, {"final_shown"}, "about_shown", recover=True):
        await callback.answer()
        return
    await callback.answer()
    await strip_buttons(callback)

    await send_msg(callback.message.chat.id, "about", kb_how_to_join())


@participant_router.callback_query(F.data == "how_to_join")
async def cb_how_to_join(callback: CallbackQuery):
    if not advance(callback.from_user.id, {"about_shown"}, "join_shown", recover=True):
        await callback.answer()
        return
    await callback.answer()
    await strip_buttons(callback)

    await send_msg(callback.message.chat.id, "join", None, disable_preview=True)


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

    admin_panel.init(bot, ADMIN_CHAT_ID, BOT_TOKEN, send_msg, WEBAPP_URL, admin_test_start)
    dp.include_router(admin_panel.router)  # админ-команды проверяются раньше хендлеров участников
    dp.include_router(participant_router)

    await check_channel_access()

    # Страховка данных: на Railway без подключённого тома всё стирается при каждом обновлении
    on_railway = any(k.startswith("RAILWAY_") for k in os.environ)
    if on_railway and not (os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or os.getenv("DATA_DIR")):
        log.warning("Постоянный том не подключён: данные сотрутся при обновлении!")
        try:
            await bot.send_message(
                ADMIN_CHAT_ID,
                "⚠️ <b>К боту не подключён постоянный том Railway.</b> Участники и работы сотрутся при "
                "следующем обновлении или перезапуске. Подключи Volume (путь /data), как в README.",
            )
        except TelegramAPIError:
            pass
    f = storage.funnel(ADMIN_CHAT_ID)
    works = len(storage.load_submissions())
    try:
        if f["launched"] or works:
            report = (
                "✅ Бот запущен. Данные на месте:\n"
                f"запустили бота: {f['launched']}\nсдали работу: {f['submitted']}\n"
                f"дошли до конца: {f['finished']}\nработ в панели: {works}"
            )
        else:
            report = (
                "ℹ️ Бот запущен, но данных пока нет. Если это не первый запуск, значит история не "
                "подхватилась: пришли боту файл restore.json или файлы из /backup."
            )
        await bot.send_message(ADMIN_CHAT_ID, report)
    except TelegramAPIError:
        pass
    asyncio.create_task(admin_panel.backup_loop())

    if WEBAPP_URL:
        if not WEBAPP_URL.startswith("https://"):
            log.warning("WEBAPP_URL должен начинаться с https://, иначе Telegram не откроет мини-приложение")
        try:
            await admin_panel.start_web(WEBAPP_PORT)
        except OSError as e:
            log.error("Не удалось запустить веб-сервер мини-приложения (порт %s): %s", WEBAPP_PORT, e)
    else:
        log.info("WEBAPP_URL не задан: мини-приложение выключено (остальной бот работает как обычно)")

    log.info("Бот марафона запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
