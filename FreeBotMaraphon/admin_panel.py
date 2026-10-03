# -*- coding: utf-8 -*-
"""
Всё, что доступно только админу:

* /edit      - менять текст и картинку двух последних сообщений марафона прямо в боте
* /panel     - открыть мини-приложение (статистика, работы участников, рассылка)
* /count     - показать счётчик «Марафон закончил до конца»
* /setcount N - задать номер, который получит следующий закончивший
* веб-сервер для мини-приложения (запускается, если в .env задан WEBAPP_URL)

Мини-приложение открывается только у админа: сервер проверяет подпись Telegram
(initData) и id пользователя, любой другой получает 403.
"""

import asyncio
import hashlib
import hmac
import html
import json
import logging
import re
import time
from types import SimpleNamespace
from urllib.parse import parse_qsl

from aiogram import F, Router
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import BufferedInputFile, InputMediaPhoto
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    FSInputFile,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)
from aiohttp import web

import storage

log = logging.getLogger("marathon_bot")

ctx = SimpleNamespace(bot=None, admin_id=0, token="", send_step=None, webapp_url="")
router = Router()
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

_pending: dict | None = None  # что админ сейчас редактирует: {"key": ..., "mode": "text"|"image"}


def init(bot, admin_id: int, token: str, send_step, webapp_url: str) -> None:
    ctx.bot, ctx.admin_id, ctx.token = bot, admin_id, token
    ctx.send_step, ctx.webapp_url = send_step, webapp_url


def is_admin(event) -> bool:
    return bool(event.from_user) and event.from_user.id == ctx.admin_id


router.message.filter(is_admin)
router.callback_query.filter(is_admin)


def clear_pending() -> None:
    global _pending
    _pending = None


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


# ---------------------------------------------------------------------------
# /edit: тексты и картинки последних двух сообщений
# ---------------------------------------------------------------------------

def kb_menu() -> InlineKeyboardMarkup:
    rows = [[_btn(m["title"], f"adm_sel:{k}")] for k, m in storage.EDITABLE.items()]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_item(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("📝 Изменить текст", f"adm_text:{key}"), _btn("🖼 Изменить картинку", f"adm_img:{key}")],
        [_btn("👁 Показать как у участника", f"adm_view:{key}")],
        [_btn("♻️ Текст по умолчанию", f"adm_rtext:{key}"), _btn("♻️ Картинка по умолчанию", f"adm_rimg:{key}")],
        [_btn("⬅️ Назад", "adm_menu:")],
    ])


def _set_override(key: str, field: str, value) -> None:
    data = storage.load_overrides()
    if value is None:
        data.get(key, {}).pop(field, None)
    else:
        data.setdefault(key, {})[field] = value
    storage.save_overrides(data)


async def _preview(chat_id: int, key: str, text: str | None = None) -> None:
    await ctx.send_step(
        chat_id, text if text is not None else storage.message_text(key),
        storage.EDITABLE[key]["image_key"], None, disable_preview=True,
    )


@router.message(Command("edit"))
async def cmd_edit(message: Message):
    clear_pending()
    await message.answer("Какое сообщение меняем?", reply_markup=kb_menu())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message):
    clear_pending()
    await message.answer("Отменил.")


@router.callback_query(F.data.startswith("adm_"))
async def cb_admin(callback: CallbackQuery):
    global _pending
    action, _, key = callback.data.partition(":")
    await callback.answer()
    chat_id = callback.message.chat.id

    if action == "adm_menu":
        clear_pending()
        await callback.message.edit_text("Какое сообщение меняем?", reply_markup=kb_menu())
        return
    if key not in storage.EDITABLE:
        return
    title = html.escape(storage.EDITABLE[key]["title"])

    if action == "adm_sel":
        clear_pending()
        await callback.message.edit_text(f"<b>{title}</b>\n\nЧто поменять?", reply_markup=kb_item(key))
    elif action == "adm_view":
        await _preview(chat_id, key)
    elif action == "adm_text":
        _pending = {"key": key, "mode": "text"}
        await ctx.bot.send_message(chat_id, "Сейчас текст такой (можешь скопировать и поправить):")
        await ctx.bot.send_message(
            chat_id, storage.message_text(key), link_preview_options=NO_PREVIEW
        )
        await ctx.bot.send_message(
            chat_id,
            "Пришли новый текст одним сообщением. Жирный, курсив и ссылки сохранятся "
            "(форматируй прямо в Telegram). Отмена: /cancel",
        )
    elif action == "adm_img":
        _pending = {"key": key, "mode": "image"}
        await ctx.bot.send_message(
            chat_id, "Пришли новую картинку (как фото или как файл). Отмена: /cancel"
        )
    elif action == "adm_rtext":
        _set_override(key, "text", None)
        await ctx.bot.send_message(chat_id, "Вернул текст по умолчанию:")
        await _preview(chat_id, key)
    elif action == "adm_rimg":
        old = storage.load_overrides().get(key, {}).get("image")
        _set_override(key, "image", None)
        if old:
            (storage.DATA_DIR / old).unlink(missing_ok=True)
        await ctx.bot.send_message(chat_id, "Вернул картинку по умолчанию:")
        await _preview(chat_id, key)


def _has_pending(message: Message) -> bool:
    return _pending is not None and not (message.text or "").startswith("/")


@router.message(_has_pending)
async def admin_input(message: Message):
    global _pending
    pending = _pending
    key, mode = pending["key"], pending["mode"]

    if mode == "text":
        if not message.text:
            await message.answer("Жду именно текст. Отмена: /cancel")
            return
        new_text = message.html_text
        try:
            await _preview(message.chat.id, key, new_text)  # заодно проверяем, что Telegram принимает
        except TelegramAPIError as e:
            await message.answer(f"Telegram не принял этот текст: {html.escape(str(e))}\nПопробуй ещё раз или /cancel")
            return
        _set_override(key, "text", new_text)
        _pending = None
        await message.answer("✅ Текст сохранён. Так его увидят участники (выше).")
        return

    # mode == "image"
    file_id, suffix = None, ".jpg"
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        file_id = message.document.file_id
        name = (message.document.file_name or "").lower()
        suffix = next((s for s in (".png", ".jpeg", ".webp") if name.endswith(s)), ".jpg")
    if not file_id:
        await message.answer("Жду картинку (фото или файл-картинку). Отмена: /cancel")
        return

    storage.CUSTOM_IMAGES_DIR.mkdir(exist_ok=True)
    rel = f"custom_images/{key}_{int(time.time())}{suffix}"
    await ctx.bot.download(file_id, destination=storage.DATA_DIR / rel)
    old = storage.load_overrides().get(key, {}).get("image")
    _set_override(key, "image", rel)
    try:
        await _preview(message.chat.id, key)
    except TelegramAPIError as e:
        _set_override(key, "image", old)
        (storage.DATA_DIR / rel).unlink(missing_ok=True)
        await message.answer(f"Telegram не принял эту картинку: {html.escape(str(e))}\nПопробуй другую или /cancel")
        return
    if old:
        (storage.DATA_DIR / old).unlink(missing_ok=True)
    _pending = None
    await message.answer("✅ Картинка сохранена. Так её увидят участники (выше).")


# ---------------------------------------------------------------------------
# /panel, /count, /setcount
# ---------------------------------------------------------------------------

@router.message(Command("panel"))
async def cmd_panel(message: Message):
    if not ctx.webapp_url:
        await message.answer(
            "Мини-приложение выключено: в .env не задан WEBAPP_URL (публичная https-ссылка "
            "на этот бот). Как её получить, написано в README."
        )
        return
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📊 Открыть панель", web_app=WebAppInfo(url=ctx.webapp_url))
    ]])
    await message.answer("Панель куратора:", reply_markup=markup)


@router.message(Command("count"))
async def cmd_count(message: Message):
    f = storage.funnel(ctx.admin_id)
    await message.answer(
        f"Следующий закончивший получит номер: <b>{storage.peek_next_finished()}</b>\n"
        f"По данным бота реально дошли до конца: <b>{f['finished']}</b>\n\n"
        "Изменить номер: /setcount 130"
    )


@router.message(Command("setcount"))
async def cmd_setcount(message: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg.isdigit() or int(arg) < 1:
        await message.answer("Напиши число, например: /setcount 130")
        return
    stats = storage.load_stats()
    stats["finished"] = int(arg) - 1
    storage.save_stats(stats)
    await message.answer(f"Готово, следующий закончивший получит номер {arg}.")


# ---------------------------------------------------------------------------
# /backup и загрузка файлов обратно (перенос на другой сервер, защита от потери данных)
# ---------------------------------------------------------------------------

@router.message(Command("backup"))
async def cmd_backup(message: Message):
    sent = 0
    for f in storage.DATA_FILES:
        if f.exists():
            await message.answer_document(FSInputFile(f))
            sent += 1
    await message.answer(
        f"Отправил файлов: {sent}. Это вся история бота. Чтобы вернуть её (или перенести на другой "
        "сервер), просто пришли эти файлы обратно боту в этот чат."
        if sent else "Данных пока нет, нечего сохранять."
    )


def import_data_file(name: str, raw: bytes) -> str | None:
    """Кладёт присланный файл на место. Возвращает текст ошибки или None, если всё хорошо."""
    target = next((f for f in storage.DATA_FILES if f.name == name), None)
    if target is None:
        return "Такого файла бот не знает."
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return "Это не похоже на файл бота: внутри не json."
    if not isinstance(data, list if name in storage.LIST_FILES else dict):
        return "Содержимое файла не подходит: бот ждёт другой формат."
    storage.write_json(target, data)
    return None


def _is_data_file(message: Message) -> bool:
    d = message.document
    return bool(d and d.file_name in {f.name for f in storage.DATA_FILES})


@router.message(_is_data_file)
async def admin_import(message: Message):
    buf = await ctx.bot.download(message.document.file_id)
    err = import_data_file(message.document.file_name, buf.getvalue())
    if err:
        await message.answer(f"Не принял {html.escape(message.document.file_name)}: {err}")
    else:
        await message.answer(
            f"✅ {html.escape(message.document.file_name)} загружен и уже работает. "
            "Он заменил текущие данные в этом файле."
        )


# ---------------------------------------------------------------------------
# Проверка, что запрос к мини-приложению пришёл от админа
# ---------------------------------------------------------------------------

INIT_DATA_MAX_AGE = 3 * 24 * 3600


def validate_init_data(init_data: str) -> dict | None:
    """Проверяет подпись Telegram и возвращает данные пользователя (или None)."""
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received = parsed.pop("hash", "")
        if not received:
            return None
        check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", ctx.token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received):
            return None
        if time.time() - int(parsed.get("auth_date", "0")) > INIT_DATA_MAX_AGE:
            return None
        return json.loads(parsed.get("user", "{}"))
    except (ValueError, TypeError):
        return None


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.path.startswith("/api/"):
        user = validate_init_data(request.headers.get("X-Init-Data", ""))
        if not user or user.get("id") != ctx.admin_id:
            return web.json_response({"error": "forbidden"}, status=403)
    return await handler(request)


# ---------------------------------------------------------------------------
# API мини-приложения
# ---------------------------------------------------------------------------

async def api_stats(request: web.Request):
    return web.json_response({
        "funnel": storage.funnel(ctx.admin_id),
        "next_finished": storage.peek_next_finished(),
        "audiences": {k: len(storage.audience_ids(k, ctx.admin_id)) for k in ("all", "finished", "unfinished")},
    })


async def api_works(request: web.Request):
    try:
        offset = max(0, int(request.query.get("offset", 0)))
        limit = max(1, min(50, int(request.query.get("limit", 20))))
    except ValueError:
        return web.json_response({"error": "bad_request"}, status=400)
    subs = list(reversed(storage.load_submissions()))
    state = storage.load_state()
    out = []
    for s in subs[offset:offset + limit]:
        stage = state.get(str(s["user_id"]), {}).get("stage")
        out.append({
            "id": s["id"], "user_id": s["user_id"], "name": s["name"], "username": s["username"],
            "niche": s["niche"], "at": s["at"], "text": s["text"],
            "approved": stage in storage.DAY1_DONE_STAGES,
            "items": [{"i": i, "type": it["type"], "mime": it.get("mime"), "name": it.get("name")}
                      for i, it in enumerate(s["items"])],
        })
    return web.json_response({"works": out, "total": len(subs), "has_more": offset + limit < len(subs)})


async def api_file(request: web.Request):
    try:
        sid, idx = int(request.match_info["sid"]), int(request.match_info["idx"])
        sub = next(s for s in storage.load_submissions() if s["id"] == sid)
        item = sub["items"][idx]
    except (ValueError, StopIteration, IndexError):
        return web.json_response({"error": "not_found"}, status=404)
    try:
        buf = await ctx.bot.download(item["file_id"])
    except TelegramBadRequest:
        return web.json_response({"error": "too_big"}, status=413)  # бот не может скачать файлы больше 20 МБ
    except TelegramAPIError:
        return web.json_response({"error": "telegram_error"}, status=502)
    return web.Response(
        body=buf.getvalue(), content_type=item.get("mime") or "application/octet-stream",
        headers={"Cache-Control": "private, max-age=3600"},
    )


# --- рассылка ---------------------------------------------------------------

_ALLOWED_TAG = re.compile(
    r'^</?(b|i|u|s|code|pre|blockquote|tg-spoiler)>$|^</a>$|^<a href="[^"<>]+">$', re.IGNORECASE
)
_BARE_AMP = re.compile(r"&(?!(?:amp|lt|gt|quot|#\d+|#x[0-9a-fA-F]+);)")


def sanitize_html(text: str) -> str:
    """Оставляет только теги, которые понимает Telegram, а остальное экранирует."""
    out = []
    for part in re.split(r"(<[^<>]*>)", text):
        if part.startswith("<") and part.endswith(">") and _ALLOWED_TAG.match(part):
            out.append(_BARE_AMP.sub("&amp;", part))  # & внутри ссылки тоже экранируем
        else:
            out.append(_BARE_AMP.sub("&amp;", part).replace("<", "&lt;").replace(">", "&gt;"))
    return "".join(out)


_bc = {"running": False, "audience": "", "total": 0, "sent": 0, "failed": 0, "blocked": 0, "text": ""}
_bc_task = None


async def _deliver(uid: int, text: str, photos: list[str]) -> None:
    if not photos:
        await ctx.bot.send_message(uid, text)
    elif len(photos) == 1:
        await ctx.bot.send_photo(uid, photos[0], caption=text or None)
    else:  # несколько фото уходят альбомом, подпись у первого
        await ctx.bot.send_media_group(uid, [
            InputMediaPhoto(media=f, caption=(text or None) if i == 0 else None) for i, f in enumerate(photos)
        ])


async def _send_one(uid: int, text: str, photos: list[str]) -> str:
    try:
        await _deliver(uid, text, photos)
        return "sent"
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
        try:
            await _deliver(uid, text, photos)
            return "sent"
        except TelegramAPIError:
            return "failed"
    except TelegramForbiddenError:
        storage.mark_blocked(uid)
        return "blocked"
    except TelegramAPIError:
        return "failed"


async def _run_broadcast(ids: list[int], text: str, photos: list[str]) -> None:
    pause = 0.06 * max(1, len(photos))  # альбом = несколько сообщений, держим запас под лимиты Telegram
    for uid in ids:
        _bc[await _send_one(uid, text, photos)] += 1
        await asyncio.sleep(pause)
    _bc["running"] = False
    history = storage.read_json(storage.BROADCASTS_FILE, list)
    history.append({k: _bc[k] for k in ("audience", "total", "sent", "failed", "blocked", "text")} | {"at": int(time.time())})
    storage.write_json(storage.BROADCASTS_FILE, history[-20:])
    try:
        await ctx.bot.send_message(
            ctx.admin_id,
            f"📣 Рассылка завершена: доставлено {_bc['sent']} из {_bc['total']}, "
            f"заблокировали бота {_bc['blocked']}, ошибок {_bc['failed']}.",
        )
    except TelegramAPIError:
        pass


MAX_PHOTOS = 10


async def api_broadcast_post(request: web.Request):
    global _bc_task
    photos: list[bytes] = []
    try:
        if request.content_type.startswith("multipart/"):
            form = await request.post()
            raw_text, raw_aud, test = form.get("text"), form.get("audience"), form.get("test") == "1"
            photos = [u.file.read() for u in form.getall("photo", []) if hasattr(u, "file")]
        else:
            body = await request.json()
            raw_text, raw_aud, test = body.get("text"), body.get("audience"), bool(body.get("test"))
    except (ValueError, AttributeError):
        return web.json_response({"error": "Некорректный запрос"}, status=400)
    text = sanitize_html(str(raw_text or "").strip())
    audience = raw_aud if raw_aud in ("all", "finished", "unfinished") else "all"
    if not text and not photos:
        return web.json_response({"error": "Напиши текст или добавь фото"}, status=400)
    if len(photos) > MAX_PHOTOS:
        return web.json_response({"error": f"Максимум {MAX_PHOTOS} фото в одной рассылке"}, status=400)
    if any(len(x) > 10 * 1024 * 1024 for x in photos):
        return web.json_response({"error": "Одно из фото больше 10 МБ, Telegram такое не принимает"}, status=400)
    if photos and len(re.sub(r"<[^>]+>", "", text)) > 1024:
        return web.json_response({"error": "Подпись к фото: максимум 1024 символа"}, status=400)
    if len(text) > 4000:
        return web.json_response({"error": "Слишком длинный текст (максимум около 4000 символов)"}, status=400)
    if _bc["running"] and not test:
        return web.json_response({"error": "Предыдущая рассылка ещё идёт"}, status=409)

    photo_ids: list[str] = []
    try:  # сначала всё уходит тебе: видно, как это выглядит, и ловятся ошибки. Фото грузим один раз,
        # дальше всем уходит по file_id
        if len(photos) == 1:
            sent = await ctx.bot.send_photo(ctx.admin_id, BufferedInputFile(photos[0], "photo.jpg"), caption=text or None)
            photo_ids = [sent.photo[-1].file_id]
        elif photos:
            sent = await ctx.bot.send_media_group(ctx.admin_id, [
                InputMediaPhoto(media=BufferedInputFile(x, f"photo{i}.jpg"), caption=(text or None) if i == 0 else None)
                for i, x in enumerate(photos)
            ])
            photo_ids = [m.photo[-1].file_id for m in sent]
        else:
            await ctx.bot.send_message(ctx.admin_id, text)
    except TelegramAPIError as e:
        return web.json_response({"error": f"Telegram не принял это: {e}"}, status=400)
    if test:
        return web.json_response({"ok": True, "test": True})

    ids = storage.audience_ids(audience, ctx.admin_id)
    if not ids:
        return web.json_response({"error": "В этой группе пока никого нет"}, status=400)
    _bc.update(running=True, audience=audience, total=len(ids), sent=0, failed=0, blocked=0,
               text=(re.sub(r"<[^>]+>", "", text) or "📷 фото")[:120])
    _bc_task = asyncio.create_task(_run_broadcast(ids, text, photo_ids))
    return web.json_response({"ok": True, "total": len(ids)})


async def api_broadcast_get(request: web.Request):
    history = storage.read_json(storage.BROADCASTS_FILE, list)
    return web.json_response({"status": _bc, "history": list(reversed(history[-5:]))})


# --- статика ----------------------------------------------------------------

WEBAPP_DIR = storage.BASE_DIR / "webapp"


async def page_index(request: web.Request):
    return web.FileResponse(WEBAPP_DIR / "index.html", headers={"Cache-Control": "no-cache"})


async def page_logo(request: web.Request):
    return web.FileResponse(WEBAPP_DIR / "logo.png")


async def start_web(port: int) -> web.AppRunner:
    app = web.Application(middlewares=[auth_middleware], client_max_size=64 * 1024 * 1024)
    app.add_routes([
        web.get("/", page_index),
        web.get("/logo.png", page_logo),
        web.get("/health", lambda r: web.Response(text="ok")),
        web.get("/api/stats", api_stats),
        web.get("/api/works", api_works),
        web.get(r"/api/file/{sid:\d+}/{idx:\d+}", api_file),
        web.get("/api/broadcast", api_broadcast_get),
        web.post("/api/broadcast", api_broadcast_post),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("Мини-приложение слушает порт %s, ссылка для Telegram: %s", port, ctx.webapp_url)
    try:  # кнопка «Панель» рядом с полем ввода у админа
        await ctx.bot.set_chat_menu_button(
            chat_id=ctx.admin_id,
            menu_button=MenuButtonWebApp(text="Панель", web_app=WebAppInfo(url=ctx.webapp_url)),
        )
    except TelegramAPIError:
        log.exception("Не удалось поставить кнопку «Панель»")
    return runner
