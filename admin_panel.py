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
import base64
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

ctx = SimpleNamespace(bot=None, admin_id=0, token="", send_msg=None, webapp_url="", test_start=None)
router = Router()
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)



def init(bot, admin_id: int, token: str, send_msg, webapp_url: str, test_start=None) -> None:
    ctx.bot, ctx.admin_id, ctx.token = bot, admin_id, token
    ctx.send_msg, ctx.webapp_url, ctx.test_start = send_msg, webapp_url, test_start


def is_admin(event) -> bool:
    return bool(event.from_user) and event.from_user.id == ctx.admin_id


router.message.filter(is_admin)
router.callback_query.filter(is_admin)


# ---------------------------------------------------------------------------
# /edit: тексты и картинки теперь меняются в панели (вкладка «Сообщения»)
# ---------------------------------------------------------------------------

def clear_pending() -> None:
    """Оставлено для совместимости: раньше тут сбрасывался режим редактирования в чате."""


@router.message(Command("edit"))
async def cmd_edit(message: Message):
    await cmd_panel(message)


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
    await message.answer(
        "Панель куратора. Тексты и картинки сообщений бота меняются во вкладке «Сообщения».",
        reply_markup=markup,
    )


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


_last_backup_sig = None


def _data_signature():
    return tuple((f.name, f.stat().st_mtime_ns, f.stat().st_size) for f in storage.DATA_FILES if f.exists())


async def send_backup(reason: str, force: bool = False) -> bool:
    """Шлёт админу файлы с историей (если они изменились с прошлой отправки)."""
    global _last_backup_sig
    sig = _data_signature()
    if not sig or (sig == _last_backup_sig and not force):
        return False
    try:
        for f in storage.DATA_FILES:
            if f.exists():
                await ctx.bot.send_document(
                    ctx.admin_id, FSInputFile(f), caption=f"Резервная копия ({reason})" if f == storage.STATE_FILE else None
                )
        _last_backup_sig = sig
        return True
    except TelegramAPIError:
        log.exception("Не удалось отправить резервную копию")
        return False


async def backup_loop(every_hours: float = 6) -> None:
    """Раз в несколько часов присылает копию данных, если что-то менялось. Страховка на случай
    потери диска: файлы всегда есть у тебя в чате и возвращаются пересылкой боту."""
    await asyncio.sleep(120)
    while True:
        await send_backup("авто")
        await asyncio.sleep(every_hours * 3600)


def import_data_file(name: str, raw: bytes) -> str | None:
    """Кладёт присланный файл на место. Возвращает текст ошибки или None, если всё хорошо."""
    if name == "restore.json":
        return import_bundle(raw)
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


def import_bundle(raw: bytes) -> str | None:
    """restore.json: участники, счётчики и работы из архива. Текущие данные не затираются:
    уже известные боту участники остаются как есть, счётчики берутся наибольшие."""
    global last_import_note
    try:
        data = json.loads(raw.decode("utf-8"))
        users, stats_in, works = data["users_state"], data["stats"], data["submissions"]
        assert isinstance(users, dict) and isinstance(stats_in, dict) and isinstance(works, list)
    except (ValueError, KeyError, AssertionError, UnicodeDecodeError):
        return "Это не файл восстановления: внутри не те данные."
    state = storage.load_state()
    added = 0
    for uid, u in users.items():
        if uid not in state:
            state[uid] = u
            added += 1
    storage.save_state(state)
    stats = storage.load_stats()
    for k, v in stats_in.items():
        stats[k] = max(int(stats.get(k, 0)), int(v))
    storage.save_stats(stats)
    subs = storage.load_submissions()
    new_works = 0
    if not any(s.get("legacy") for s in subs):  # повторная загрузка не плодит дубли
        subs += works
        new_works = len(works)
        subs.sort(key=lambda s: s["at"])
        for i, s in enumerate(subs, 1):
            s["id"] = i
        storage.write_json(storage.SUBMISSIONS_FILE, subs)
    last_import_note = f"участников добавлено: {added}, работ добавлено: {new_works}"
    return None


last_import_note = ""


def _is_data_file(message: Message) -> bool:
    d = message.document
    return bool(d and (d.file_name in {f.name for f in storage.DATA_FILES} or d.file_name == "restore.json"))


@router.message(_is_data_file)
async def admin_import(message: Message):
    buf = await ctx.bot.download(message.document.file_id)
    err = import_data_file(message.document.file_name, buf.getvalue())
    if err:
        await message.answer(f"Не принял {html.escape(message.document.file_name)}: {err}")
    else:
        if message.document.file_name == "restore.json":
            await message.answer(f"✅ История восстановлена: {last_import_note}. Открой /panel и проверь.")
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
        "test_mode": bool(storage.get_setting("test_mode", True)),
        "audiences": {k: len(storage.audience_ids(k, ctx.admin_id)) for k in ("all", "finished", "unfinished")},
    })


async def api_settings_post(request: web.Request):
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "Некорректный запрос"}, status=400)
    if "test_mode" in body:
        storage.set_setting("test_mode", bool(body["test_mode"]))
    return web.json_response({"ok": True, "test_mode": bool(storage.get_setting("test_mode", True))})


async def api_test_start(request: web.Request):
    if not ctx.test_start:
        return web.json_response({"error": "Недоступно"}, status=400)
    try:
        await ctx.test_start()
    except TelegramAPIError as e:
        return web.json_response({"error": f"Не получилось отправить в чат: {e}"}, status=400)
    return web.json_response({"ok": True})


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
            "approved": stage in storage.DAY1_DONE_STAGES, "legacy": bool(s.get("legacy")),
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
    if item.get("b64"):  # восстановленная работа: картинка лежит внутри файла данных
        return web.Response(body=base64.b64decode(item["b64"]), content_type=item.get("mime") or "image/jpeg",
                            headers={"Cache-Control": "private, max-age=3600"})
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


# --- тексты и картинки сообщений бота -----------------------------------------

def _msg_info(meta: dict) -> dict:
    key = meta["key"]
    ov = storage.load_overrides().get(key, {})
    return {
        "key": key, "title": meta["title"], "group": meta["group"], "vars": meta.get("vars", []),
        "text": storage.message_raw(key), "text_custom": bool(ov.get("text")),
        "image": storage.message_image(key) is not None,
        "image_custom": bool(ov.get("image")) or bool(ov.get("no_image")),
    }


async def api_messages(request: web.Request):
    return web.json_response({"messages": [_msg_info(m) for m in storage.MESSAGES]})


async def api_message_image(request: web.Request):
    if request.match_info["key"] not in storage.MESSAGE_BY_KEY:
        return web.json_response({"error": "not_found"}, status=404)
    path = storage.message_image(request.match_info["key"])
    if not path or not path.exists():
        return web.json_response({"error": "no_image"}, status=404)
    return web.FileResponse(path, headers={"Cache-Control": "no-cache"})


def _image_ext(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


SAMPLE_VARS = {"name": "Имя"}


async def api_message_post(request: web.Request):
    key = request.match_info["key"]
    meta = storage.MESSAGE_BY_KEY.get(key)
    if not meta:
        return web.json_response({"error": "Такого сообщения нет"}, status=404)
    try:
        form = await request.post()
    except ValueError:
        return web.json_response({"error": "Некорректный запрос"}, status=400)
    preview_only = form.get("preview") == "1"
    overrides = storage.load_overrides()
    old = dict(overrides.get(key, {}))

    new_text = old.get("text")
    if form.get("reset_text") == "1":
        new_text = None
    elif form.get("text") is not None:
        new_text = sanitize_html(str(form.get("text")).strip())
        if not new_text:
            return web.json_response({"error": "Текст не может быть пустым"}, status=400)
        if len(re.sub(r"<[^>]+>", "", new_text)) > 4000:
            return web.json_response({"error": "Слишком длинный текст (максимум около 4000 символов)"}, status=400)

    up = form.get("image")
    img = up.file.read() if hasattr(up, "file") else None
    if img and len(img) > 10 * 1024 * 1024:
        return web.json_response({"error": "Картинка больше 10 МБ, Telegram такое не принимает"}, status=400)
    new_image, no_image, uploaded = old.get("image"), bool(old.get("no_image")), None
    if form.get("reset_image") == "1":
        new_image, no_image = None, False
    elif form.get("remove_image") == "1":
        new_image, no_image = None, True
    elif img:
        storage.CUSTOM_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        uploaded = f"custom_images/{key}_{int(time.time())}{_image_ext(img)}"
        (storage.DATA_DIR / uploaded).write_bytes(img)
        new_image, no_image = uploaded, False

    def cleanup_upload():
        if uploaded:
            (storage.DATA_DIR / uploaded).unlink(missing_ok=True)

    # сначала то, что получится, уходит тебе в чат: видно, как это выглядит, и ловятся ошибки разметки
    eff_text = new_text or storage.default_text(key)
    if new_image and (storage.DATA_DIR / new_image).exists():
        eff_image = storage.DATA_DIR / new_image
    else:
        eff_image = None if no_image else storage.default_image_path(key)
    try:
        await ctx.send_msg(ctx.admin_id, key, None, disable_preview=True, text=eff_text, image=eff_image, **SAMPLE_VARS)
    except TelegramAPIError as e:
        cleanup_upload()
        return web.json_response({"error": f"Telegram не принял это: {e}"}, status=400)
    if preview_only:
        cleanup_upload()
        return web.json_response({"ok": True, "preview": True})

    rec = {}
    if new_text:
        rec["text"] = new_text
    if new_image:
        rec["image"] = new_image
    if no_image:
        rec["no_image"] = True
    if rec:
        overrides[key] = rec
    else:
        overrides.pop(key, None)
    storage.save_overrides(overrides)
    if old.get("image") and old["image"] != new_image:  # старую загруженную картинку убираем с диска
        (storage.DATA_DIR / old["image"]).unlink(missing_ok=True)
    return web.json_response({"ok": True, "message": _msg_info(meta)})


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
        web.post("/api/settings", api_settings_post),
        web.post("/api/test-start", api_test_start),
        web.get(r"/api/file/{sid:\d+}/{idx:\d+}", api_file),
        web.get("/api/messages", api_messages),
        web.get(r"/api/messages/{key}/image", api_message_image),
        web.post(r"/api/messages/{key}", api_message_post),
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
