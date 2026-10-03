# -*- coding: utf-8 -*-
"""
Хранилище данных бота: всё, что лежит в json-файлах рядом с bot.py.

Запись атомарная (сначала временный файл, потом os.replace), поэтому если бот
упадёт посреди записи, файл с участниками не повредится. Если файл всё же оказался
битым, он переименовывается в *.corrupt-<время>, а бот продолжает работать с чистого листа.
"""

import json
import logging
import os
import time
from pathlib import Path

import content

log = logging.getLogger("marathon_bot")

BASE_DIR = Path(__file__).parent  # код, тексты, картинки, webapp
# Данные участников лежат отдельно. На Railway это подключённый том (путь Railway даёт сам,
# переменная RAILWAY_VOLUME_MOUNT_PATH), иначе папка с ботом.
DATA_DIR = Path(os.getenv("DATA_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or BASE_DIR)
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "users_state.json"          # этап каждого участника
JOINS_FILE = DATA_DIR / "join_requests.json"        # кто подал заявку в закрытый канал
STATS_FILE = DATA_DIR / "marathon_stats.json"       # счётчики «начал» и «закончил»
SUBMISSIONS_FILE = DATA_DIR / "submissions.json"    # все присланные работы (для панели)
OVERRIDES_FILE = DATA_DIR / "overrides.json"        # тексты/картинки, изменённые через /edit
BROADCASTS_FILE = DATA_DIR / "broadcasts.json"      # история рассылок
SETTINGS_FILE = DATA_DIR / "settings.json"          # настройки из панели (тест-режим)
CUSTOM_IMAGES_DIR = DATA_DIR / "custom_images"

# Файлы, которые нужно беречь (их отдаёт /backup и принимает загрузка обратно)
DATA_FILES = [STATE_FILE, STATS_FILE, JOINS_FILE, SUBMISSIONS_FILE, OVERRIDES_FILE, BROADCASTS_FILE, SETTINGS_FILE]
LIST_FILES = {SUBMISSIONS_FILE.name, BROADCASTS_FILE.name}  # остальные файлы это словари


# ---------------------------------------------------------------------------
# Базовые функции
# ---------------------------------------------------------------------------

def read_json(path: Path, factory):
    if not path.exists():
        return factory()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        backup = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        try:
            path.replace(backup)
        except OSError:
            pass
        log.error("Файл %s повреждён, сохранил копию как %s и начал заново", path.name, backup.name)
        return factory()


def write_json(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Участники
# ---------------------------------------------------------------------------

def load_state() -> dict:
    return read_json(STATE_FILE, dict)


def save_state(state: dict) -> None:
    write_json(STATE_FILE, state)


def get_user(state: dict, user_id: int) -> dict:
    key = str(user_id)
    if key not in state:
        state[key] = {"stage": "new", "niche": None, "created_at": int(time.time())}
    return state[key]


STAGE_ORDER = ["new", "welcomed", "rules_shown", "day1_niche_choice", "day1_task_given", "waiting_submission",
               "submitted", "approved", "day2_unlocked", "final_shown", "about_shown", "join_shown"]


def stage_index(stage: str) -> int:
    return STAGE_ORDER.index(stage) if stage in STAGE_ORDER else -1


def advance(user_id: int, allowed_from: set, to_stage: str, recover: bool = False) -> bool:
    """
    Атомарно (без await между проверкой и записью) переводит участника на следующий
    этап, если он сейчас на одном из разрешённых. Защита от двойных нажатий и от
    нажатий на старые кнопки: во всех остальных случаях вернёт False.
    """
    state = load_state()
    user = get_user(state, user_id)
    # recover: кнопка из старого сообщения, а бот помнит человека на более раннем этапе (данные потерялись
    # или восстановлены по приблизительным данным): раз кнопка у него есть, пускаем вперёд.
    # Назад (на уже пройденный шаг) не пускаем: так работает защита от двойных нажатий.
    behind = stage_index(user["stage"]) < stage_index(to_stage)
    if user["stage"] not in allowed_from and not (recover and behind):
        return False
    user["stage"] = to_stage
    save_state(state)
    return True


def mark_blocked(user_id: int) -> None:
    state = load_state()
    if str(user_id) in state and not state[str(user_id)].get("blocked"):
        state[str(user_id)]["blocked"] = True
        save_state(state)


# ---------------------------------------------------------------------------
# Счётчики и заявки в канал
# ---------------------------------------------------------------------------

def load_stats() -> dict:
    return read_json(STATS_FILE, dict)


def save_stats(stats: dict) -> None:
    write_json(STATS_FILE, stats)


def load_joins() -> dict:
    joins = read_json(JOINS_FILE, lambda: {"chat_id": None, "users": {}})
    joins.setdefault("users", {})
    return joins


def save_joins(joins: dict) -> None:
    write_json(JOINS_FILE, joins)


# ---------------------------------------------------------------------------
# Воронка для панели
# ---------------------------------------------------------------------------

DAY1_DONE_STAGES = {"approved", "day2_unlocked", "final_shown", "about_shown", "join_shown"}
SUBMITTED_STAGES = {"submitted"} | DAY1_DONE_STAGES


def funnel(admin_id: int) -> dict:
    """Цифры воронки без тебя (админа): тесты в статистику не попадают."""
    state = load_state()
    users = [u for uid, u in state.items() if str(uid) != str(admin_id)]
    stages: dict[str, int] = {}
    for u in users:
        stages[u.get("stage", "new")] = stages.get(u.get("stage", "new"), 0) + 1
    return {
        "launched": len(users),  # нажали /start
        "marathon": sum(1 for u in users if u.get("stage") != "new"),  # прошли условия
        "submitted": sum(1 for u in users if u.get("stage") in SUBMITTED_STAGES),
        "day1": sum(1 for u in users if u.get("stage") in DAY1_DONE_STAGES),
        "finished": sum(1 for u in users if u.get("stage") == "join_shown"),
        "blocked": sum(1 for u in users if u.get("blocked")),
        "stages": stages,
    }


def audience_ids(kind: str, admin_id: int) -> list[int]:
    """Кому слать рассылку. Заблокировавшие бота и ты сам не входят."""
    out = []
    for uid, u in load_state().items():
        if str(uid) == str(admin_id) or u.get("blocked"):
            continue
        stage = u.get("stage", "new")
        if kind == "finished" and stage != "join_shown":
            continue
        if kind == "unfinished" and stage == "join_shown":
            continue
        out.append(int(uid))
    return out


# ---------------------------------------------------------------------------
# Присланные работы
# ---------------------------------------------------------------------------

def load_submissions() -> list:
    return read_json(SUBMISSIONS_FILE, list)


def add_submission(user_id: int, name: str, username: str | None, niche: str | None,
                   group: str | None, item: dict, text: str) -> int:
    """Записывает работу. Сообщения одного альбома склеиваются в одну запись."""
    subs = load_submissions()
    if group:
        for s in reversed(subs):
            if s["user_id"] == user_id and s.get("group") == group:
                s["items"].append(item)
                s["text"] = s["text"] or text
                write_json(SUBMISSIONS_FILE, subs)
                return s["id"]
    sid = (subs[-1]["id"] + 1) if subs else 1
    subs.append({
        "id": sid, "user_id": user_id, "name": name, "username": username,
        "niche": niche, "group": group, "at": int(time.time()),
        "text": text, "items": [item] if item else [],
    })
    write_json(SUBMISSIONS_FILE, subs)
    return sid


# ---------------------------------------------------------------------------
# Сообщения бота: тексты и картинки (меняются в панели, вкладка «Сообщения»)
# ---------------------------------------------------------------------------
# Чтобы добавить новое сообщение в панель: добавь запись сюда и отправляй его из бота
# через send_msg(chat_id, "ключ"). mode "separate": картинка уходит отдельным сообщением,
# иначе картинка и текст идут одним сообщением (текст подписью).

def _t(name: str, niche: str | None = None):
    if niche is None:
        return lambda: getattr(content, name)
    return lambda: getattr(content, name)[niche]


def _day2(niche: str):
    return lambda: content.DAY2_HEADER + content.DAY2_NICHE_CONTENT[niche]


MESSAGES = [
    {"key": "sub_gate", "group": "Старт", "title": "Условие: TikTok и заявка в канал", "text": _t("SUB_GATE")},
    {"key": "tiktok_link", "group": "Старт", "title": "Ссылка на TikTok", "text": _t("TIKTOK_LINK_MESSAGE")},
    {"key": "welcome", "group": "Старт", "title": "Приветствие", "text": _t("WELCOME"),
     "image": "welcome", "mode": "separate", "vars": ["name"]},
    {"key": "rules", "group": "Старт", "title": "Правила марафона", "text": _t("RULES")},
    {"key": "day1_intro", "group": "День 1", "title": "День 1: выбор ниши", "text": _t("DAY1_INTRO"),
     "image": "day1_intro", "mode": "separate"},
    {"key": "niche_sites", "group": "День 1", "title": "Задание: сайты", "text": _t("NICHE_PICKED", "sites")},
    {"key": "niche_bots", "group": "День 1", "title": "Задание: боты и автоматизации", "text": _t("NICHE_PICKED", "bots")},
    {"key": "niche_dashboards", "group": "День 1", "title": "Задание: дашборды", "text": _t("NICHE_PICKED", "dashboards")},
    {"key": "submit_prompt", "group": "День 1", "title": "Жду результат", "text": _t("SUBMIT_PROMPT")},
    {"key": "submit_too_short", "group": "День 1", "title": "Просьба прислать сам результат", "text": _t("SUBMIT_TOO_SHORT")},
    {"key": "submit_received", "group": "День 1", "title": "Работа принята на проверку", "text": _t("SUBMIT_RECEIVED_USER"),
     "vars": ["contact"]},
    {"key": "submit_failed", "group": "День 1", "title": "Не удалось отправить работу", "text": _t("SUBMIT_FAILED")},
    {"key": "approved", "group": "День 2 и итоги", "title": "Куратор принял работу", "text": _t("APPROVED_USER"), "image": "approved"},
    {"key": "day2_sites", "group": "День 2 и итоги", "title": "День 2: сайты", "text": _day2("sites"), "image": "day2_sites"},
    {"key": "day2_bots", "group": "День 2 и итоги", "title": "День 2: боты и автоматизации", "text": _day2("bots"), "image": "day2_bots"},
    {"key": "day2_dashboards", "group": "День 2 и итоги", "title": "День 2: дашборды", "text": _day2("dashboards"),
     "image": "day2_dashboards"},
    {"key": "final", "group": "День 2 и итоги", "title": "Итог марафона", "text": _t("FINAL_MESSAGE")},
    {"key": "about", "group": "Финал", "title": "Что такое C&C Family?", "text": _t("ABOUT_FAMILY"), "image": "ccfm"},
    {"key": "join", "group": "Финал", "title": "Как войти? (вторая волна)", "text": _t("HOW_TO_JOIN"), "image": "join",
     "vars": ["contact", "tg_url", "reviews_url"]},
]
MESSAGE_BY_KEY = {m["key"]: m for m in MESSAGES}


def load_overrides() -> dict:
    return read_json(OVERRIDES_FILE, dict)


def save_overrides(data: dict) -> None:
    write_json(OVERRIDES_FILE, data)


def builtin_vars() -> dict:
    return {
        "contact": os.getenv("CONTACT_HANDLE", "@SUN9ISE"),
        "tg_url": content.TG_CHANNEL_URL,
        "reviews_url": content.SITE_REVIEWS_URL,
    }


def fill(text: str, **vars) -> str:
    """Подставляет {name}, {contact} и т.п. Остальные фигурные скобки в тексте не трогает."""
    for k, v in {**builtin_vars(), **vars}.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def default_text(key: str) -> str:
    return MESSAGE_BY_KEY[key]["text"]()


def message_raw(key: str) -> str:
    """Текущий текст с невставленными {плейсхолдерами}: твой из панели или стандартный."""
    return load_overrides().get(key, {}).get("text") or default_text(key)


def message_text(key: str, **vars) -> str:
    return fill(message_raw(key), **vars)


def default_image_path(key: str) -> Path | None:
    image_key = MESSAGE_BY_KEY[key].get("image")
    return BASE_DIR / content.IMAGES[image_key] if image_key else None


def message_image(key: str) -> Path | None:
    """Картинка сообщения: загруженная в панели, иначе стандартная, а если ты её убрал, то None."""
    ov = load_overrides().get(key, {})
    custom = ov.get("image")
    if custom and (DATA_DIR / custom).exists():
        return DATA_DIR / custom
    if ov.get("no_image"):
        return None
    return default_image_path(key)


# ---------------------------------------------------------------------------
# Счётчик «Марафон закончил до конца»
# ---------------------------------------------------------------------------

def finished_start() -> int:
    """Номер, который получит первый закончивший (по умолчанию 130, меняется в .env)."""
    try:
        return int(os.getenv("FINISHED_START", "130") or 130)
    except ValueError:
        return 130


def peek_next_finished() -> int:
    return load_stats().get("finished", finished_start() - 1) + 1


def next_finished_number() -> int:
    stats = load_stats()
    stats["finished"] = stats.get("finished", finished_start() - 1) + 1
    save_stats(stats)
    return stats["finished"]


def remove_submission(sid: int) -> None:
    subs = [s for s in load_submissions() if s["id"] != sid]
    write_json(SUBMISSIONS_FILE, subs)


# ---------------------------------------------------------------------------
# Настройки из панели
# ---------------------------------------------------------------------------

def get_setting(name: str, default=None):
    return read_json(SETTINGS_FILE, dict).get(name, default)


def set_setting(name: str, value) -> None:
    data = read_json(SETTINGS_FILE, dict)
    data[name] = value
    write_json(SETTINGS_FILE, data)
