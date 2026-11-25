import copy
from difflib import SequenceMatcher
import hashlib
import html
import io
import json
import os
import re
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import telebot
from telebot import types

try:
    from PIL import Image, ImageDraw, ImageFont

    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False

# =========================================================
# КОНФИГУРАЦИЯ
# =========================================================
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
ADMIN_ID_RAW = (os.getenv("ADMIN_ID") or "").strip()
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
SUPABASE_KEY = (os.getenv("SUPABASE_KEY") or "").strip()
SUPABASE_STORAGE_BUCKET = (os.getenv("SUPABASE_STORAGE_BUCKET") or "tk-media").strip()
DONATIONALERTS_URL = (
    os.getenv("DONATIONALERTS_URL") or "https://www.donationalerts.com/r/your_link"
).strip()
MAX_BULK_FILES = int((os.getenv("MAX_BULK_FILES") or "30").strip())
WATCHDOG_STALL_SEC = int((os.getenv("WATCHDOG_STALL_SEC") or "180").strip())

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в переменных окружения")

ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW.isdigit() else 0
SUPABASE_ENABLED = bool(SUPABASE_URL and SUPABASE_KEY)
SUPABASE_STORAGE_ENABLED = bool(SUPABASE_ENABLED and SUPABASE_STORAGE_BUCKET)

DISHWARE_ALLOWED_USERNAMES = {
    "deadkesha",
    "aveinx",
    "dragonmaycry07",
    "vladizavrrr",
}

DATA_DIR = os.getenv("DATA_DIR", "data")
MEDIA_DIR = os.path.join(DATA_DIR, "user_media")
FONT_CACHE_PATH = os.path.join(DATA_DIR, "DejaVuSans.ttf")
FONT_BOLD_CACHE_PATH = os.path.join(DATA_DIR, "DejaVuSans-Bold.ttf")
LOCAL_DB_FILE = os.path.join(DATA_DIR, "tk_bot_state.json")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(MEDIA_DIR, exist_ok=True)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# =========================================================
# КНОПКИ И СПИСКИ
# =========================================================
BTN_RECIPES = "🍸 Рецепты"
BTN_DISHWARE = "🥃 Посуда"
BTN_HELP = "🆘 Помощь"
BTN_USERS = "👥 Пользователи"
BTN_SUPPORT = "💛 Поддержка"
BTN_SUGGEST_COCKTAIL = "💡 Предложить коктейль"

BTN_REC_AUTHOR = "✨ Авторские"
BTN_REC_CLASSIC = "🥃 Классические"
BTN_REC_INFUSIONS = "🫙 Наливки"
BTN_REC_MENU = "🍹 Коктейли меню"
BTN_REC_PREMIXES = "🧪 Премиксы"
BTN_MEDIA_LIST = "🖼 Карточки"
BTN_ADD_TTK_TEXT = "➕ Добавить ТТК текстом"
BTN_MANAGE_TTK = "✏️ Ред. / Удалить ТТК"
BTN_CLEAN_TITLES = "🧹 Очистить названия"

BTN_BULK_MEDIA = "📥 Массовая загрузка"
BTN_DISH_SUMMARY = "📊 Подсчёт остатков"
BTN_BACK = "⬅️ Назад"
BTN_DONE = "✅ Готово"
BTN_CANCEL = "❌ Отмена"

BTN_TTK_RENAME = "📝 Переименовать"
BTN_TTK_CAT = "🗂 Категория"
BTN_TTK_REPLACE_FILE = "🔄 Заменить файл"
BTN_TTK_DELETE = "🗑 Удалить ТТК"
BTN_TTK_DELETE_IN_TAB = "🗑 Удалить в этой вкладке"
BTN_TTK_CHANGE_TAB = "📂 Сменить вкладку"
BTN_TTK_NEXT = "➡️ Дальше"
BTN_TTK_PREV = "⬅️ Пред."

CB_CARD_PREFIX = "c:"
CB_PAGE_PREFIX = "p:"
CB_SUG_APPROVE_PREFIX = "sa:"
CB_SUG_REJECT_PREFIX = "sr:"

CAT_CODES = {
    "": "all",
    "авторские": "author",
    "классические": "classic",
    "наливки": "infusions",
    "коктейли меню": "menu",
    "премиксы": "premixes",
}
CODE_TO_CAT = {v: k for k, v in CAT_CODES.items()}

TTK_CATEGORIES = [
    "авторские",
    "классические",
    "наливки",
    "коктейли меню",
    "премиксы",
]

INITIAL_DISHWARE = [
    ("Бокал Красный Винник", 24),
    ("Бокал пивной 0.5", 60),
    ("Бокал Рокс", 24),
    ("Бокал Айфриш", 12),
    ("Бокал 375", 24),
    ("Бокал снифтер", 12),
    ("Бокал аллегро", 12),
    ("Бокал индеец", 6),
    ("Бокал флютте", 12),
    ("Графин 1л", 6),
    ("Графин 1.5л", 6),
    ("Бокал Кружка банка", 10),
    ("Штоф 0.5", 6),
]

DATA_LOCK = threading.RLock()
USER_STATES: dict[int, dict] = {}
LAST_SEEN_SYNC: dict[int, float] = {}
CALLBACK_CARD_MAP: dict[str, str] = {}
SUGGESTIONS_STORE: dict[str, dict] = {}

LAST_POLLING_HEARTBEAT = time.monotonic()
POLLING_ACTIVE = False

DB: dict = {
    "recipes": [],
    "dishware": [],
    "dishware_logs": [],
    "users": [],
    "support": {"id": "main", "text": DONATIONALERTS_URL},
    "activity_logs": [],
    "media_cards": [],
}


def mark_polling_alive() -> None:
    global LAST_POLLING_HEARTBEAT, POLLING_ACTIVE
    LAST_POLLING_HEARTBEAT = time.monotonic()
    POLLING_ACTIVE = True


def trigger_self_restart(reason: str) -> None:
    print(f"[WATCHDOG] Перезапуск процесса: {reason}", flush=True)
    time.sleep(0.3)
    os._exit(1)


def polling_watchdog_loop() -> None:
    while True:
        time.sleep(20)
        if not POLLING_ACTIVE:
            continue
        idle_for = time.monotonic() - LAST_POLLING_HEARTBEAT
        if idle_for > WATCHDOG_STALL_SEC:
            trigger_self_restart(f"polling не подавал признаков жизни {idle_for:.0f}с")


# =========================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =========================================================
def now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


def norm_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def search_clean_text(value: str | None) -> str:
    cleaned = re.sub(r"[_\-—–/\\.,:;!?()\"'«»№*+#]+", " ", (value or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def clean_uploaded_title(raw_title: str | None) -> str:
    s = (raw_title or "").strip()
    if not s:
        return ""
    s = re.sub(r"^(?:page[\s_]+)?(?:tk|тк)[\s_]+(?:tk|тк)?[\s_]*(?:коктейль|напиток|премикс)?[\s_]*", "", s, flags=re.I)
    s = re.sub(r"(?:[_\s]+№[_\s]*\d+)?(?:[_\s]+c[_\s]+\d{1,2}[_\s.]\d{1,2}[_\s.]\d{2,4}).*$", "", s, flags=re.I)
    s = re.sub(r"[_\s]+№[_\s]*\d+.*$", "", s, flags=re.I)
    s = re.sub(r"[^0-9A-Za-zА-Яа-яЁё()\s]+", " ", s)
    s = re.sub(r"\(\s+", "(", s)
    s = re.sub(r"\s+\)", ")", s)
    s = re.sub(r"\s+", " ", s).strip(" -_")
    return s or (raw_title or "").strip()


def exact_title_from_message(msg: types.Message) -> str | None:
    if msg.content_type == "document" and msg.document and msg.document.file_name:
        stem, _ = os.path.splitext(msg.document.file_name)
        cleaned = clean_uploaded_title(stem)
        if cleaned:
            return cleaned
    cap = (msg.caption or "").strip()
    if cap:
        cleaned = clean_uploaded_title(cap)
        if cleaned:
            return cleaned
    return None


def is_admin(user_id: int) -> bool:
    if ADMIN_ID and user_id == ADMIN_ID:
        return True
    with DATA_LOCK:
        for u in DB.get("users", []):
            if int(u.get("user_id") or 0) == user_id and bool(u.get("is_admin")):
                return True
    return False


def can_manage_dishware(user_id: int, username: str | None = "") -> bool:
    if is_admin(user_id):
        return True
    uname = (username or "").strip().lstrip("@").lower()
    if uname and uname in DISHWARE_ALLOWED_USERNAMES:
        return True
    if user_id:
        with DATA_LOCK:
            for u in DB.get("users", []):
                if int(u.get("user_id") or 0) == int(user_id):
                    stored_u = str(u.get("username") or "").strip().lstrip("@").lower()
                    if stored_u and stored_u in DISHWARE_ALLOWED_USERNAMES:
                        return True
    return False


def require_admin_msg(msg: types.Message) -> bool:
    uid = msg.from_user.id if msg.from_user else 0
    if is_admin(uid):
        return True
    safe_send_text(
        msg.chat.id,
        "⛔ Это действие доступно только администратору. Ты можешь нажать «💡 Предложить коктейль».",
        reply_markup=main_menu_keyboard(uid),
    )
    return False


def require_dishware_msg(msg: types.Message) -> bool:
    uid = msg.from_user.id if msg.from_user else 0
    uname = (msg.from_user.username or "") if msg.from_user else ""
    if can_manage_dishware(uid, uname):
        return True
    safe_send_text(
        msg.chat.id,
        "⛔ Раздел посуды и подсчёт остатков доступны только авторизованным пользователям и администратору.",
        reply_markup=main_menu_keyboard(uid, uname),
    )
    return False


def get_admin_ids() -> list[int]:
    ids = set()
    if ADMIN_ID:
        ids.add(ADMIN_ID)
    with DATA_LOCK:
        for u in DB.get("users", []):
            if bool(u.get("is_admin")) and u.get("user_id"):
                ids.add(int(u["user_id"]))
    return list(ids)


def safe_send_text(chat_id: int, text: str, reply_markup=None):
    mark_polling_alive()
    try:
        return bot.send_message(chat_id, text, reply_markup=reply_markup)
    except Exception as e:
        print(f"[WARN] send_message failed ({chat_id}): {e}", flush=True)
        return None


def parse_number_selection(raw: str, max_index: int) -> list[int]:
    cleaned = (raw or "").strip()
    if not re.fullmatch(r"[\d\s,\-]+", cleaned):
        return []
    result: list[int] = []
    seen: set[int] = set()
    for part in cleaned.split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            bounds = token.split("-", 1)
            if bounds[0].strip().isdigit() and bounds[1].strip().isdigit():
                a = int(bounds[0].strip())
                b = int(bounds[1].strip())
                if a > b:
                    a, b = b, a
                for n in range(a, b + 1):
                    if 1 <= n <= max_index and n not in seen:
                        seen.add(n)
                        result.append(n)
        elif token.isdigit():
            n = int(token)
            if 1 <= n <= max_index and n not in seen:
                seen.add(n)
                result.append(n)
    return result


# =========================================================
# КЛАВИАТУРЫ
# =========================================================
def main_menu_keyboard(user_id: int, username: str = "") -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(types.KeyboardButton(BTN_RECIPES), types.KeyboardButton(BTN_SUGGEST_COCKTAIL))
    if can_manage_dishware(user_id, username):
        if is_admin(user_id):
            kb.row(types.KeyboardButton(BTN_DISHWARE), types.KeyboardButton(BTN_USERS))
        else:
            kb.row(types.KeyboardButton(BTN_DISHWARE))
    elif is_admin(user_id):
        kb.row(types.KeyboardButton(BTN_USERS))
    kb.row(types.KeyboardButton(BTN_HELP), types.KeyboardButton(BTN_SUPPORT))
    return kb


def recipes_menu_keyboard(user_id: int) -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(types.KeyboardButton(BTN_REC_AUTHOR), types.KeyboardButton(BTN_REC_CLASSIC))
    kb.row(types.KeyboardButton(BTN_REC_INFUSIONS), types.KeyboardButton(BTN_REC_MENU))
    kb.row(types.KeyboardButton(BTN_REC_PREMIXES), types.KeyboardButton(BTN_MEDIA_LIST))
    kb.row(types.KeyboardButton(BTN_SUGGEST_COCKTAIL))
    if is_admin(user_id):
        kb.row(types.KeyboardButton(BTN_ADD_TTK_TEXT), types.KeyboardButton(BTN_MANAGE_TTK))
        kb.row(types.KeyboardButton(BTN_BULK_MEDIA), types.KeyboardButton(BTN_CLEAN_TITLES))
    kb.row(types.KeyboardButton(BTN_BACK))
    return kb


def manage_tab_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(types.KeyboardButton("Авторские"), types.KeyboardButton("Классические"))
    kb.row(types.KeyboardButton("Наливки"), types.KeyboardButton("Коктейли меню"))
    kb.row(types.KeyboardButton("Премиксы"), types.KeyboardButton("Без категории"))
    kb.row(types.KeyboardButton("Все карточки"), types.KeyboardButton(BTN_BACK))
    return kb


def _register_card_callback_key(card_id: str) -> str:
    short_key = re.sub(r"[^0-9A-Za-z_-]", "", str(card_id))[-24:] or uuid.uuid4().hex[:10]
    CALLBACK_CARD_MAP[short_key] = str(card_id)
    return short_key


def card_buttons_keyboard(cards: list[dict]) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    for card in cards:
        cid = str(card.get("id") or "")
        short_key = _register_card_callback_key(cid)
        title = str(card.get("title") or "Без названия")[:64]
        kb.add(types.InlineKeyboardButton(text=title, callback_data=f"{CB_CARD_PREFIX}{short_key}"))
    return kb


def ttk_popup_keyboard(page_cards: list[dict], page: int, total_pages: int, cat_key: str) -> types.InlineKeyboardMarkup:
    kb = card_buttons_keyboard(page_cards)

    nav_buttons = []
    if total_pages > 1:
        cat_code = CAT_CODES.get(cat_key, "all")
        prev_page = page - 1 if page > 0 else total_pages - 1
        next_page = page + 1 if page + 1 < total_pages else 0
        nav_buttons.append(
            types.InlineKeyboardButton(text="⬅️ Назад", callback_data=f"{CB_PAGE_PREFIX}{prev_page}:{cat_code}")
        )
        nav_buttons.append(
            types.InlineKeyboardButton(text="➡️ Вперёд", callback_data=f"{CB_PAGE_PREFIX}{next_page}:{cat_code}")
        )
        kb.row(*nav_buttons)
    return kb


def suggestion_review_keyboard(suggestion_id: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(
        types.InlineKeyboardButton("✅ Добавить", callback_data=f"{CB_SUG_APPROVE_PREFIX}{suggestion_id}"),
        types.InlineKeyboardButton("❌ Отклонить", callback_data=f"{CB_SUG_REJECT_PREFIX}{suggestion_id}"),
    )
    return kb


def dishware_menu_keyboard(user_id: int) -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    with DATA_LOCK:
        items = list(DB.get("dishware", []))
    for i in range(0, len(items), 2):
        row = []
        d1 = items[i]
        row.append(types.KeyboardButton(f"{d1.get('name')} — {int(d1.get('qty') or 0)}шт"))
        if i + 1 < len(items):
            d2 = items[i + 1]
            row.append(types.KeyboardButton(f"{d2.get('name')} — {int(d2.get('qty') or 0)}шт"))
        kb.row(*row)
    kb.row(types.KeyboardButton(BTN_DISH_SUMMARY))
    if is_admin(user_id):
        kb.row(types.KeyboardButton(BTN_BULK_MEDIA))
    kb.row(types.KeyboardButton(BTN_BACK))
    return kb


def dishware_item_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(types.KeyboardButton("+1"), types.KeyboardButton("-1"))
    kb.row(types.KeyboardButton("+5"), types.KeyboardButton("-5"))
    kb.row(types.KeyboardButton(BTN_DISH_SUMMARY), types.KeyboardButton(BTN_BACK))
    return kb


def ttk_list_keyboard(page: int, total_pages: int) -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    nav = []
    if page > 0:
        nav.append(types.KeyboardButton(BTN_TTK_PREV))
    if page + 1 < total_pages:
        nav.append(types.KeyboardButton(BTN_TTK_NEXT))
    if nav:
        kb.row(*nav)
    kb.row(types.KeyboardButton(BTN_TTK_DELETE_IN_TAB), types.KeyboardButton(BTN_TTK_CHANGE_TAB))
    kb.row(types.KeyboardButton(BTN_BACK))
    return kb


def ttk_edit_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(types.KeyboardButton(BTN_TTK_RENAME), types.KeyboardButton(BTN_TTK_CAT))
    kb.row(types.KeyboardButton(BTN_TTK_REPLACE_FILE), types.KeyboardButton(BTN_TTK_DELETE))
    kb.row(types.KeyboardButton(BTN_BACK))
    return kb


def ttk_category_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(types.KeyboardButton("Авторские"), types.KeyboardButton("Классические"))
    kb.row(types.KeyboardButton("Наливки"), types.KeyboardButton("Коктейли меню"))
    kb.row(types.KeyboardButton("Премиксы"), types.KeyboardButton("Без категории"))
    kb.row(types.KeyboardButton(BTN_BACK))
    return kb


def back_only_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(types.KeyboardButton(BTN_BACK))
    return kb


def bulk_done_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(types.KeyboardButton(BTN_DONE), types.KeyboardButton(BTN_CANCEL))
    return kb


# =========================================================
# SUPABASE REST API + STORAGE + ЛОКАЛЬНЫЙ СНИМОК
# =========================================================
def sb_request(method: str, table: str, query: str = "", payload=None, prefer: str = ""):
    mark_polling_alive()
    if not SUPABASE_ENABLED:
        return None
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    if query:
        url = f"{url}?{query}"

    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer

    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=7) as resp:
            raw = resp.read().decode("utf-8", errors="replace").strip()
            if not raw:
                return []
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"[WARN] Supabase {method} {table}?{query} HTTP {e.code}: {body}", flush=True)
        return None
    except Exception as e:
        print(f"[WARN] Supabase {method} {table} failed: {e}", flush=True)
        return None


def sb_select(table: str, query: str = ""):
    return sb_request("GET", table, query=query)


def sb_upsert(table: str, row: dict, on_conflict: str = "id"):
    q = f"on_conflict={urllib.parse.quote(on_conflict)}"
    return sb_request(
        "POST",
        table,
        query=q,
        payload=row,
        prefer="resolution=merge-duplicates,return=representation",
    )


def sb_delete(table: str, query: str):
    return sb_request("DELETE", table, query=query)


def _extract_support_url(row: dict | None) -> str:
    if isinstance(row, dict):
        for key in ("text", "url", "link", "value"):
            val = str(row.get(key) or "").strip()
            if val.startswith(("http://", "https://")):
                return val
    return DONATIONALERTS_URL


def save_support_url(new_url: str, uid: int, username: str) -> tuple[bool, str]:
    cleaned = (new_url or "").strip()
    if not cleaned.startswith(("http://", "https://")):
        return False, "Ссылка должна начинаться с https:// или http://"

    with DATA_LOCK:
        DB["support"] = {"id": "main", "text": cleaned}
        sync_local_from_memory()

    if SUPABASE_ENABLED:
        res = sb_upsert("tk_support", {"id": "main", "text": cleaned}, "id")
        if res is None:
            sb_upsert("tk_support", {"id": "main", "url": cleaned}, "id")

    log_activity(uid, username, "SET_SUPPORT_URL", cleaned)
    return True, cleaned


def _storage_object_path(card_id: str, ext: str = ".png") -> str:
    clean_ext = ext if ext.startswith(".") else f".{ext}"
    clean_ext = re.sub(r"[^0-9A-Za-z.]", "", clean_ext)[:8] or ".png"
    return f"cards/{card_id}{clean_ext}"


def _content_type_for_ext(ext: str) -> str:
    low_ext = (ext or "").lower()
    if low_ext in (".jpg", ".jpeg"):
        return "image/jpeg"
    if low_ext == ".webp":
        return "image/webp"
    return "image/png"


def storage_upload_bytes(object_path: str, raw_bytes: bytes, content_type: str = "image/png") -> str | None:
    mark_polling_alive()
    if not SUPABASE_STORAGE_ENABLED or not raw_bytes:
        return None
    encoded_path = urllib.parse.quote(object_path.lstrip("/"), safe="/")
    url = f"{SUPABASE_URL}/storage/v1/object/{urllib.parse.quote(SUPABASE_STORAGE_BUCKET)}/{encoded_path}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": content_type,
        "x-upsert": "true",
    }
    req = urllib.request.Request(url, data=raw_bytes, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            if 200 <= resp.status < 300:
                return object_path
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"[WARN] Supabase Storage upload failed ({object_path}) HTTP {e.code}: {body}", flush=True)
    except Exception as e:
        print(f"[WARN] Supabase Storage upload error ({object_path}): {e}", flush=True)
    return None


def storage_download_bytes(object_path: str) -> bytes | None:
    mark_polling_alive()
    if not SUPABASE_STORAGE_ENABLED or not object_path:
        return None
    encoded_path = urllib.parse.quote(object_path.lstrip("/"), safe="/")
    candidates = [
        f"{SUPABASE_URL}/storage/v1/object/{urllib.parse.quote(SUPABASE_STORAGE_BUCKET)}/{encoded_path}",
        f"{SUPABASE_URL}/storage/v1/object/public/{urllib.parse.quote(SUPABASE_STORAGE_BUCKET)}/{encoded_path}",
    ]
    for url in candidates:
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
        }
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = resp.read()
                if data:
                    return data
        except Exception:
            continue
    return None


def storage_delete_object(object_path: str) -> None:
    mark_polling_alive()
    if not SUPABASE_STORAGE_ENABLED or not object_path:
        return
    encoded_path = urllib.parse.quote(object_path.lstrip("/"), safe="/")
    url = f"{SUPABASE_URL}/storage/v1/object/{urllib.parse.quote(SUPABASE_STORAGE_BUCKET)}/{encoded_path}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }
    req = urllib.request.Request(url, headers=headers, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=7):
            pass
    except Exception as e:
        print(f"[WARN] Supabase Storage delete failed ({object_path}): {e}", flush=True)


def sync_local_from_memory() -> None:
    try:
        snapshot = copy.deepcopy(DB)
        with open(LOCAL_DB_FILE, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[WARN] sync_local_from_memory failed: {e}", flush=True)


def seed_default_dishware_if_empty(data: dict) -> None:
    if data.get("dishware"):
        return
    seeded = []
    for idx, (name, qty) in enumerate(INITIAL_DISHWARE, start=1):
        row = {
            "id": f"dish_{idx:02d}",
            "name": name,
            "qty": int(qty),
        }
        seeded.append(row)
        sb_upsert("tk_dishware", row, "id")
    data["dishware"] = seeded


def load_local_fallback() -> dict:
    fallback = {
        "recipes": [],
        "dishware": [],
        "dishware_logs": [],
        "users": [],
        "support": {"id": "main", "text": DONATIONALERTS_URL},
        "activity_logs": [],
        "media_cards": [],
    }
    if os.path.isfile(LOCAL_DB_FILE):
        try:
            with open(LOCAL_DB_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    fallback.update(loaded)
        except Exception as e:
            print(f"[WARN] load_local_fallback failed: {e}", flush=True)
    return fallback


def load_data() -> None:
    mark_polling_alive()
    with DATA_LOCK:
        data = load_local_fallback()
        DB.clear()
        DB.update(data)

    if not SUPABASE_ENABLED:
        with DATA_LOCK:
            seed_default_dishware_if_empty(DB)
            sync_local_from_memory()
        return

    local_cat_by_id = {
        str(c.get("id")): str(c.get("category") or "")
        for c in data.get("media_cards", [])
        if c.get("id")
    }

    recipes = sb_select("tk_recipes", "order=created_at.asc")
    if recipes is not None:
        data["recipes"] = recipes

    dishware = sb_select("tk_dishware", "order=id.asc")
    if dishware is not None:
        data["dishware"] = dishware

    seed_default_dishware_if_empty(data)

    dishware_logs = sb_select("tk_dishware_logs", "order=id.desc&limit=500")
    if dishware_logs is not None:
        data["dishware_logs"] = dishware_logs

    users = sb_select("tk_users", "limit=200")
    if users is not None:
        data["users"] = users

    support_rows = sb_select("tk_support", "id=eq.main&limit=1")
    if support_rows:
        extracted = _extract_support_url(support_rows[0])
        data["support"] = {"id": "main", "text": extracted}
    else:
        current_sup = _extract_support_url(data.get("support"))
        data["support"] = {"id": "main", "text": current_sup}

    activity_logs = sb_select("tk_activity_logs", "limit=800")
    if activity_logs is not None:
        data["activity_logs"] = activity_logs

    media_rows = sb_select("tk_media_cards", "order=created_at.asc")
    if media_rows is not None:
        data["media_cards"] = [
            {
                "id": str(m.get("id")),
                "title": str(m.get("title") or ""),
                "category": str(m.get("category") or local_cat_by_id.get(str(m.get("id"))) or ""),
                "photo_file_id": m.get("photo_file_id"),
                "photo_path": m.get("photo_path"),
                "storage_path": m.get("storage_path"),
                "created_at": str(m.get("created_at") or now_str()),
            }
            for m in media_rows
        ]
    data.setdefault("media_cards", [])

    with DATA_LOCK:
        DB.clear()
        DB.update(data)
        sync_local_from_memory()
    mark_polling_alive()


def log_activity(user_id: int, username: str, action: str, details: str = "") -> None:
    raw_uid = f"act_{uuid.uuid4().hex[:12]}"
    entry = {
        "id": raw_uid,
        "user_id": int(user_id),
        "username": username or "",
        "action": action,
        "details": details,
        "created_at": now_str(),
    }
    with DATA_LOCK:
        DB.setdefault("activity_logs", []).insert(0, entry)
        DB["activity_logs"] = DB["activity_logs"][:800]
        sync_local_from_memory()

    sb_row = {
        "id": entry["id"],
        "user_id": entry["user_id"],
        "username": entry["username"],
        "action": f"{action}: {details}" if details else action,
        "created_at": entry["created_at"],
        "ts": entry["created_at"],
    }
    res = sb_upsert("tk_activity_logs", sb_row, "id")
    if res is None:
        num_id = int(hashlib.sha256(raw_uid.encode("utf-8")).hexdigest()[:12], 16)
        sb_row_num = dict(sb_row, id=num_id)
        sb_upsert("tk_activity_logs", sb_row_num, "id")


def touch_user(msg: types.Message) -> None:
    mark_polling_alive()
    user = msg.from_user
    if not user:
        return
    uid = int(user.id)
    now_ts = time.time()
    with DATA_LOCK:
        users = DB.setdefault("users", [])
        existing = next((u for u in users if int(u.get("user_id") or 0) == uid), None)
        row = {
            "user_id": uid,
            "uid": uid,
            "username": user.username or "",
            "is_admin": bool(existing.get("is_admin")) if existing else (uid == ADMIN_ID),
            "last_seen": now_str(),
        }
        if existing:
            existing.update(row)
        else:
            users.append(row)
        sync_local_from_memory()

    if now_ts - LAST_SEEN_SYNC.get(uid, 0.0) > 60.0:
        LAST_SEEN_SYNC[uid] = now_ts
        res = sb_upsert("tk_users", row, "user_id")
        if res is None:
            sb_upsert("tk_users", {"id": uid, **row}, "id")


# =========================================================
# ПОСУДА
# =========================================================
def match_dishware_button(text: str) -> dict | None:
    clean = re.sub(r"\s*—\s*\d+шт\s*$", "", (text or "").strip())
    norm_clean = norm_text(clean)
    if not norm_clean:
        return None
    with DATA_LOCK:
        for d in DB.get("dishware", []):
            if norm_text(d.get("name")) == norm_clean:
                return d
    return None


def adjust_dishware_qty(item_id: str, delta: int | None, set_exact: int | None, uid: int, username: str) -> dict | None:
    with DATA_LOCK:
        items = DB.get("dishware", [])
        target = next((d for d in items if str(d.get("id")) == str(item_id)), None)
        if not target:
            return None
        old_qty = int(target.get("qty") or 0)
        if set_exact is not None:
            new_qty = max(0, int(set_exact))
            diff = new_qty - old_qty
        else:
            diff = int(delta or 0)
            new_qty = max(0, old_qty + diff)

        target["qty"] = new_qty
        sync_local_from_memory()
        sb_upsert(
            "tk_dishware",
            {
                "id": str(target["id"]),
                "name": str(target.get("name") or ""),
                "qty": new_qty,
            },
            "id",
        )

        dlog = {
            "id": f"dlog_{uuid.uuid4().hex[:12]}",
            "dishware_id": str(target["id"]),
            "name": str(target.get("name") or ""),
            "delta": diff,
            "qty_after": new_qty,
            "user_id": int(uid),
            "username": username or "",
            "created_at": now_str(),
        }
        DB.setdefault("dishware_logs", []).insert(0, dlog)
        DB["dishware_logs"] = DB["dishware_logs"][:500]
        sb_upsert("tk_dishware_logs", dlog, "id")

    log_activity(uid, username, "DISHWARE_QTY", f"{target.get('name')}: {old_qty} -> {new_qty} ({diff:+d})")
    return target


def format_dishware_summary() -> str:
    with DATA_LOCK:
        items = list(DB.get("dishware", []))
    if not items:
        return "Посуды пока нет."
    total_pcs = sum(int(d.get("qty") or 0) for d in items)
    lines = [f"• {esc(d.get('name') or '')}: <b>{int(d.get('qty') or 0)}шт</b>" for d in items]
    lines.append(f"\n<b>Итого позиций:</b> {len(items)} | <b>Всего штук:</b> {total_pcs}шт")
    return "📊 <b>Подсчёт остатков посуды:</b>\n\n" + "\n".join(lines)


# =========================================================
# РАССЫЛКА ПОЛЬЗОВАТЕЛЯМ (/broadcast)
# =========================================================
def collect_all_recipient_ids() -> list[int]:
    ids: set[int] = set()
    with DATA_LOCK:
        for u in DB.get("users", []):
            raw_uid = u.get("user_id")
            if raw_uid is not None and str(raw_uid).isdigit():
                val = int(raw_uid)
                if val > 0:
                    ids.add(val)
        for a in DB.get("activity_logs", []):
            raw_uid = a.get("user_id")
            if raw_uid is not None and str(raw_uid).isdigit():
                val = int(raw_uid)
                if val > 0:
                    ids.add(val)
    return sorted(ids)


def send_broadcast_to_all(announcement_text: str, admin_uid: int, admin_username: str) -> tuple[int, int]:
    recipients = collect_all_recipient_ids()
    sent_ok = 0
    sent_fail = 0
    payload = f"📢 <b>Обновление бота</b>\n\n{esc(announcement_text)}"

    for uid in recipients:
        mark_polling_alive()
        try:
            res = bot.send_message(uid, payload)
            if res:
                sent_ok += 1
            else:
                sent_fail += 1
        except Exception as e:
            sent_fail += 1
            print(f"[WARN] broadcast failed for uid={uid}: {e}", flush=True)
        time.sleep(0.05)

    log_activity(admin_uid, admin_username, "BROADCAST", f"sent={sent_ok}, failed={sent_fail}")
    return sent_ok, sent_fail


# =========================================================
# ТТК / КАРТОЧКИ + ПОДСЧЁТ ВЫХОДА + PNG (tk_media_cards)
# =========================================================
def _download_font_if_needed(target_path: str, urls: list[str]) -> str | None:
    if os.path.isfile(target_path) and os.path.getsize(target_path) > 10000:
        return target_path
    for url in urls:
        mark_polling_alive()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = resp.read()
            if len(data) > 50000:
                with open(target_path, "wb") as f:
                    f.write(data)
                return target_path
        except Exception as e:
            print(f"[WARN] Font download failed ({url}): {e}", flush=True)
    return None


def ensure_cyrillic_fonts() -> tuple[str | None, str | None]:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    regular_candidates = [
        os.path.join(base_dir, "DejaVuSans.ttf"),
        os.path.join(base_dir, "font.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
    ]
    bold_candidates = [
        os.path.join(base_dir, "DejaVuSans-Bold.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "C:\\Windows\\Fonts\\arialbd.ttf",
    ]

    reg_path = next((p for p in regular_candidates if p and os.path.isfile(p) and os.path.getsize(p) > 10000), None)
    if not reg_path:
        reg_path = _download_font_if_needed(
            FONT_CACHE_PATH,
            [
                "https://github.com/dejavu-fonts/dejavu-fonts/raw/master/ttf/DejaVuSans.ttf",
                "https://raw.githubusercontent.com/dejavu-fonts/dejavu-fonts/master/ttf/DejaVuSans.ttf",
            ],
        )

    bold_path = next((p for p in bold_candidates if p and os.path.isfile(p) and os.path.getsize(p) > 10000), None)
    if not bold_path:
        bold_path = _download_font_if_needed(
            FONT_BOLD_CACHE_PATH,
            [
                "https://github.com/dejavu-fonts/dejavu-fonts/raw/master/ttf/DejaVuSans-Bold.ttf",
                "https://raw.githubusercontent.com/dejavu-fonts/dejavu-fonts/master/ttf/DejaVuSans-Bold.ttf",
            ],
        ) or reg_path

    return reg_path, bold_path


def load_ttk_card_fonts():
    reg_path, bold_path = ensure_cyrillic_fonts()
    if reg_path and bold_path:
        try:
            return {
                "eyebrow": ImageFont.truetype(bold_path, 17),
                "title": ImageFont.truetype(bold_path, 38),
                "sec_head": ImageFont.truetype(bold_path, 21),
                "item": ImageFont.truetype(reg_path, 24),
                "item_bold": ImageFont.truetype(bold_path, 24),
                "prep": ImageFont.truetype(reg_path, 23),
            }
        except Exception as e:
            print(f"[WARN] ImageFont.truetype failed: {e}", flush=True)
    fallback = ImageFont.load_default()
    return {
        "eyebrow": fallback,
        "title": fallback,
        "sec_head": fallback,
        "item": fallback,
        "item_bold": fallback,
        "prep": fallback,
    }


def _parse_qty_value_and_unit(qty_str: str) -> tuple[float | None, str | None]:
    q = (qty_str or "").strip().lower().replace(",", ".")
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(мл|л|г|гр|кг|шт)$", q)
    if not m:
        return None, None
    val = float(m.group(1))
    unit = m.group(2)
    if unit == "л":
        return val * 1000.0, "мл"
    if unit in ("г", "гр"):
        return val, "г"
    if unit == "кг":
        return val * 1000.0, "г"
    if unit == "мл":
        return val, "мл"
    if unit == "шт":
        return val, "шт"
    return None, None


def _format_number_clean(num: float) -> str:
    if abs(num - round(num)) < 1e-6:
        return str(int(round(num)))
    return f"{num:.1f}".rstrip("0").rstrip(".")


def _compute_output_row(ingredients: list[tuple[str, str]]) -> tuple[str, str] | None:
    if not ingredients:
        return None

    for name, qty in ingredients:
        if norm_text(name).startswith("выход") or norm_text(qty).startswith("выход"):
            return None

    vol_ml = 0.0
    mass_g = 0.0
    pcs_cnt = 0.0

    for _, qty in ingredients:
        val, kind = _parse_qty_value_and_unit(qty)
        if val is None or not kind:
            continue
        if kind == "мл":
            vol_ml += val
        elif kind == "г":
            mass_g += val
        elif kind == "шт":
            pcs_cnt += val

    parts: list[str] = []
    if vol_ml > 0:
        if vol_ml >= 1000 and abs((vol_ml / 1000.0) - round(vol_ml / 1000.0, 2)) < 1e-6:
            parts.append(f"{_format_number_clean(vol_ml)} мл ({_format_number_clean(vol_ml / 1000.0)} л)")
        else:
            parts.append(f"{_format_number_clean(vol_ml)} мл")
    if mass_g > 0:
        if mass_g >= 1000:
            parts.append(f"{_format_number_clean(mass_g)} г ({_format_number_clean(mass_g / 1000.0)} кг)")
        else:
            parts.append(f"{_format_number_clean(mass_g)} г")
    if pcs_cnt > 0 and not parts:
        parts.append(f"{_format_number_clean(pcs_cnt)} шт")

    if not parts:
        return None
    return ("Выход готового напитка", " + ".join(parts))


def _split_ingredient_line_or_clause(raw_item: str) -> tuple[str, str]:
    s = re.sub(r"\s+", " ", (raw_item or "").strip(" •-–—;"))
    if not s:
        return "", ""

    qty_pattern = r"(\d+(?:[.,]\d+)?\s*(?:мл|г|гр|кг|л|шт|дэш|кап|барн\.?\s*л\.?|ч\.?\s*л\.?|ст\.?\s*л\.?))"
    matches = list(re.finditer(qty_pattern, s, flags=re.I))
    if matches:
        last_m = matches[-1]
        qty_str = re.sub(r"\s+", "", last_m.group(1).strip())
        before = s[: last_m.start()].strip(" ,:;—–-")
        after = s[last_m.end() :].strip(" ,:;—–-")
        name_part = " ".join(part for part in (before, after) if part).strip(" ,:;—–-")
        if name_part:
            return name_part, qty_str

    m_sep = re.match(r"^(.+?)\s*(?:\t|\s{2,}|[:—–-])\s*(\d+(?:[.,]\d+)?\s*\S*)$", s)
    if m_sep:
        return m_sep.group(1).strip(" •-–—"), m_sep.group(2).strip()

    return s, ""


def _parse_ingredient_chunk(ing_chunk: str) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for line in (ing_chunk or "").splitlines():
        clean_line = line.strip()
        if not clean_line:
            continue
        if re.match(r"^(состав|ингредиенты)[:：]?$", norm_text(clean_line)):
            continue
        parts = [p.strip() for p in re.split(r"[,;]+", clean_line) if p.strip()]
        for part in parts:
            name, qty = _split_ingredient_line_or_clause(part)
            if name or qty:
                items.append((name or part, qty))
    return items


def _parse_ttk_sections(title: str, body_text: str) -> tuple[list[tuple[str, str]], list[str]]:
    raw_text = (body_text or "").strip()
    if not raw_text:
        return [], [title]

    m_prep_marker = re.search(
        r"(?im)^(?:приготовление|способ приготовления|метод|описание)\s*[:：—–-]?\s*(.*)$",
        raw_text,
    )
    if m_prep_marker:
        comp_part = raw_text[: m_prep_marker.start()].strip()
        first_prep_line = m_prep_marker.group(1).strip()
        rest_prep = raw_text[m_prep_marker.end() :].strip()
        prep_combined = "\n".join(x for x in (first_prep_line, rest_prep) if x)
        ingredients = _parse_ingredient_chunk(comp_part)
        prep_lines = [ln.strip() for ln in prep_combined.splitlines() if ln.strip()]
    else:
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", raw_text) if p.strip()]
        if len(paragraphs) >= 2:
            comp_block = "\n".join(paragraphs[:-1])
            prep_block = paragraphs[-1]
            ingredients = _parse_ingredient_chunk(comp_block)
            prep_lines = [ln.strip() for ln in prep_block.splitlines() if ln.strip()]
        else:
            ingredients = []
            prep_lines = []
            for ln in raw_text.splitlines():
                clean = ln.strip()
                if not clean:
                    continue
                parsed_here = _parse_ingredient_chunk(clean)
                has_qty_or_list = any(q for _, q in parsed_here) or ("," in clean and len(clean) <= 140)
                if has_qty_or_list and not prep_lines:
                    ingredients.extend(parsed_here)
                else:
                    prep_lines.append(clean)

    output_row = _compute_output_row(ingredients)
    if output_row:
        ingredients.append(output_row)

    if not ingredients and not prep_lines:
        prep_lines.append(title)

    return ingredients, prep_lines


def _split_multi_ttk_blocks(raw_text: str) -> list[str]:
    text = (raw_text or "").strip()
    if not text:
        return []

    explicit_chunks = re.split(r"(?m)^\s*(?:-{3,}|={3,}|\*{3,})\s*$", text)
    blocks: list[str] = []

    for chunk in explicit_chunks:
        clean_chunk = chunk.strip()
        if not clean_chunk:
            continue

        paras = [p.strip() for p in re.split(r"\n\s*\n+", clean_chunk) if p.strip()]
        if len(paras) <= 3:
            blocks.append(clean_chunk)
            continue

        i = 0
        while i < len(paras):
            title_p = paras[i]
            first_line = title_p.splitlines()[0].strip() if title_p else ""
            looks_like_title = (
                len(title_p.splitlines()) == 1
                and len(first_line) <= 60
                and not re.match(r"^(?:приготовление|способ приготовления|метод|описание|состав|ингредиенты)\b", first_line, flags=re.I)
                and not re.search(r"\d+(?:[.,]\d+)?\s*(?:мл|г|гр|кг|л|шт)\b", first_line, flags=re.I)
            )
            if looks_like_title and i + 2 < len(paras):
                comp_p = paras[i + 1]
                prep_p = paras[i + 2]
                if i + 3 < len(paras):
                    next_first = paras[i + 3].splitlines()[0].strip()
                    next_is_title = (
                        len(paras[i + 3].splitlines()) == 1
                        and len(next_first) <= 60
                        and not re.match(r"^(?:приготовление|способ приготовления|метод|описание|состав|ингредиенты)\b", next_first, flags=re.I)
                        and not re.search(r"\d+(?:[.,]\d+)?\s*(?:мл|г|гр|кг|л|шт)\b", next_first, flags=re.I)
                    )
                    if next_is_title:
                        blocks.append(f"{title_p}\n\n{comp_p}\n\n{prep_p}")
                        i += 3
                        continue

            blocks.append("\n\n".join(paras[i:]))
            break

    return blocks


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return int(bbox[2] - bbox[0])


def render_ttk_png_bytes(title: str, body_text: str, category: str = "") -> bytes | None:
    if not PIL_AVAILABLE:
        return None

    fonts = load_ttk_card_fonts()
    ingredients, prep_lines = _parse_ttk_sections(title, body_text)

    canvas_w = 1200
    pad_x = 44
    pad_y = 40
    inner_x0 = pad_x + 44
    inner_x1 = canvas_w - pad_x - 44
    inner_w = inner_x1 - inner_x0
    measure_draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))

    title_lines = textwrap.wrap(title.upper(), width=34) or [title.upper()]

    wrapped_ing: list[tuple[str, str, bool]] = []
    for name, qty in ingredients:
        is_output_row = norm_text(name).startswith("выход")
        max_name_w = int(inner_w * 0.62) if qty else inner_w
        wrapped_name = []
        cur = ""
        for word in name.split():
            trial = f"{cur} {word}".strip()
            if _text_width(measure_draw, trial, fonts["item_bold" if is_output_row else "item"]) <= max_name_w:
                cur = trial
            else:
                if cur:
                    wrapped_name.append(cur)
                cur = word
        if cur:
            wrapped_name.append(cur)
        for idx, w_line in enumerate(wrapped_name or [name]):
            wrapped_ing.append((w_line, qty if idx == 0 else "", is_output_row))

    wrapped_prep: list[str] = []
    for p_line in prep_lines:
        cur = ""
        for word in p_line.split():
            trial = f"{cur} {word}".strip()
            if _text_width(measure_draw, trial, fonts["prep"]) <= inner_w:
                cur = trial
            else:
                if cur:
                    wrapped_prep.append(cur)
                cur = word
        if cur:
            wrapped_prep.append(cur)

    ing_box_h = 66 + len(wrapped_ing) * 44 + 18 if wrapped_ing else 0
    prep_box_h = 66 + len(wrapped_prep) * 38 + 20 if wrapped_prep else 0
    header_h = 66 + len(title_lines) * 46 + 24
    total_h = pad_y * 2 + header_h + (ing_box_h + 22 if ing_box_h else 0) + prep_box_h + 24

    img = Image.new("RGB", (canvas_w, total_h), color=(24, 27, 34))
    draw = ImageDraw.Draw(img)

    card_x0 = pad_x
    card_y0 = pad_y
    card_x1 = canvas_w - pad_x
    card_y1 = total_h - pad_y

    draw.rounded_rectangle([card_x0, card_y0, card_x1, card_y1], radius=26, fill=(248, 249, 251))
    draw.rounded_rectangle([card_x0, card_y0, card_x1, card_y0 + 10], radius=5, fill=(217, 119, 6))

    y = card_y0 + 30
    cat_badge = f"ТЕХНОЛОГИЧЕСКАЯ КАРТА  •  {category.upper()}" if category else "ТЕХНОЛОГИЧЕСКАЯ КАРТА"
    draw.text((inner_x0, y), cat_badge, fill=(180, 95, 10), font=fonts["eyebrow"])
    y += 32

    for t_line in title_lines:
        draw.text((inner_x0, y), t_line, fill=(17, 24, 39), font=fonts["title"])
        y += 45

    y += 14

    if wrapped_ing:
        sec_top = y
        sec_bot = y + ing_box_h
        draw.rounded_rectangle([inner_x0 - 20, sec_top, inner_x1 + 20, sec_bot], radius=16, fill=(255, 255, 255), outline=(226, 232, 240), width=2)
        sy = sec_top + 18
        draw.text((inner_x0, sy), "СОСТАВ", fill=(17, 24, 39), font=fonts["sec_head"])
        sy += 38

        for idx, (ing_name, ing_qty, is_output_row) in enumerate(wrapped_ing):
            if is_output_row:
                draw.line([(inner_x0, sy - 6), (inner_x1, sy - 6)], fill=(203, 213, 225), width=2)
            n_font = fonts["item_bold"] if is_output_row else fonts["item"]
            q_font = fonts["item_bold"]
            n_color = (180, 83, 9) if is_output_row else (30, 41, 59)
            q_color = (180, 83, 9) if is_output_row else (15, 23, 42)
            draw.text((inner_x0, sy), ing_name, fill=n_color, font=n_font)
            if ing_qty:
                qw = _text_width(draw, ing_qty, q_font)
                draw.text((inner_x1 - qw, sy), ing_qty, fill=q_color, font=q_font)
            sy += 34
            if idx < len(wrapped_ing) - 1 and ing_qty and not is_output_row:
                draw.line([(inner_x0, sy + 3), (inner_x1, sy + 3)], fill=(241, 245, 249), width=1)
            sy += 10

        y = sec_bot + 22

    if wrapped_prep:
        sec_top = y
        sec_bot = y + prep_box_h
        draw.rounded_rectangle([inner_x0 - 20, sec_top, inner_x1 + 20, sec_bot], radius=16, fill=(255, 255, 255), outline=(226, 232, 240), width=2)
        py = sec_top + 18
        draw.text((inner_x0, py), "ПРИГОТОВЛЕНИЕ", fill=(17, 24, 39), font=fonts["sec_head"])
        py += 38

        for p_line in wrapped_prep:
            draw.text((inner_x0, py), p_line, fill=(30, 41, 59), font=fonts["prep"])
            py += 38

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _store_card_bytes(
    card_id: str,
    raw_bytes: bytes,
    ext: str = ".png",
    upload_to_storage: bool = True,
) -> tuple[str | None, str | None]:
    clean_ext = ext if ext.startswith(".") else f".{ext}"
    clean_ext = re.sub(r"[^0-9A-Za-z.]", "", clean_ext)[:8] or ".png"
    local_path = os.path.join(MEDIA_DIR, f"{card_id}{clean_ext}")
    saved_local = None
    try:
        with open(local_path, "wb") as out:
            out.write(raw_bytes)
        saved_local = local_path
    except Exception as e:
        print(f"[WARN] write local media file failed ({local_path}): {e}", flush=True)

    storage_path = None
    if upload_to_storage and SUPABASE_STORAGE_ENABLED:
        obj_path = _storage_object_path(card_id, clean_ext)
        storage_path = storage_upload_bytes(obj_path, raw_bytes, _content_type_for_ext(clean_ext))

    return saved_local, storage_path


def persist_media_card(card: dict) -> bool:
    sync_local_from_memory()
    if not SUPABASE_ENABLED:
        return True
    full_row = {
        "id": str(card["id"]),
        "title": str(card.get("title") or ""),
        "category": str(card.get("category") or ""),
        "photo_file_id": card.get("photo_file_id"),
        "photo_path": card.get("photo_path"),
        "storage_path": card.get("storage_path"),
        "created_at": str(card.get("created_at") or now_str()),
    }
    res = sb_upsert("tk_media_cards", full_row, "id")
    if res is not None:
        return True
    fallback_row = {
        k: v
        for k, v in full_row.items()
        if k not in ("category", "storage_path")
    }
    res2 = sb_upsert("tk_media_cards", fallback_row, "id")
    if res2 is not None:
        return True
    print(f"[WARN] persist_media_card failed for {card.get('id')}", flush=True)
    return False


def create_single_ttk_block(block_text: str, category: str, uid: int, username: str) -> tuple[bool, dict | str]:
    raw = (block_text or "").strip()
    if not raw:
        return False, "Блок ТТК пуст."

    paras = [p.strip() for p in re.split(r"\n\s*\n+", raw) if p.strip()]
    if len(paras) >= 2 and len(paras[0].splitlines()) == 1 and len(paras[0]) <= 80:
        title = clean_uploaded_title(paras[0])
        body = "\n\n".join(paras[1:])
    else:
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        if not lines:
            return False, "Блок ТТК пуст."
        if len(lines) == 1:
            single = lines[0]
            split_match = re.split(r"\s+[—–-]\s+|:\s+", single, maxsplit=1)
            if len(split_match) == 2 and split_match[0].strip():
                title = clean_uploaded_title(split_match[0].strip())
                body = split_match[1].strip()
            else:
                title = clean_uploaded_title(single)
                body = ""
        else:
            title = clean_uploaded_title(lines[0])
            body = "\n".join(lines[1:])

    if not title:
        return False, "Не удалось определить название ТТК."

    card_id = f"media_{uuid.uuid4().hex[:10]}"
    png_bytes = render_ttk_png_bytes(title, body, category=category)
    local_path, storage_path = (
        _store_card_bytes(card_id, png_bytes, ext=".png", upload_to_storage=True)
        if png_bytes
        else (None, None)
    )

    card = {
        "id": card_id,
        "title": title,
        "category": category,
        "photo_file_id": None,
        "photo_path": local_path,
        "storage_path": storage_path,
        "created_at": now_str(),
    }

    with DATA_LOCK:
        cards = DB.setdefault("media_cards", [])
        existing = next(
            (
                c
                for c in cards
                if norm_text(c.get("title")) == norm_text(title)
                and norm_text(c.get("category")) == norm_text(category)
            ),
            None,
        )
        if existing:
            old_storage = existing.get("storage_path")
            if old_storage and old_storage != storage_path:
                storage_delete_object(str(old_storage))
            existing["photo_path"] = local_path
            existing["storage_path"] = storage_path
            existing["photo_file_id"] = None
            existing["created_at"] = card["created_at"]
            if category:
                existing["category"] = category
            card = existing
        else:
            cards.append(card)
        persist_media_card(card)

    log_activity(uid, username, "TTK_CREATE_TEXT", f"{title} [{category or 'без категории'}]")
    return True, card


def create_ttk_from_text(raw_text: str, category: str, uid: int, username: str) -> tuple[bool, list[dict] | str]:
    blocks = _split_multi_ttk_blocks(raw_text)
    if not blocks:
        return False, "Текст пуст — напиши название, состав и описание приготовления ТТК."

    created_cards: list[dict] = []
    errors: list[str] = []
    for blk in blocks:
        ok, res = create_single_ttk_block(blk, category, uid, username)
        if ok and isinstance(res, dict):
            created_cards.append(res)
        elif isinstance(res, str):
            errors.append(res)

    if not created_cards:
        return False, errors[0] if errors else "Не удалось создать карточки ТТК."
    return True, created_cards


def clean_existing_media_titles(uid: int, username: str) -> list[tuple[str, str]]:
    changed: list[tuple[str, str]] = []
    with DATA_LOCK:
        cards = DB.get("media_cards", [])
        for card in cards:
            old_title = str(card.get("title") or "")
            new_title = clean_uploaded_title(old_title)
            if new_title and new_title != old_title:
                card["title"] = new_title
                persist_media_card(card)
                changed.append((old_title, new_title))

    if changed:
        log_activity(uid, username, "CLEAN_TITLES", f"обновлено: {len(changed)}")
    return changed


def delete_media_card_by_id(card_id: str, uid: int, username: str) -> dict | None:
    with DATA_LOCK:
        cards = DB.get("media_cards", [])
        idx = next((i for i, c in enumerate(cards) if str(c.get("id")) == str(card_id)), None)
        if idx is None:
            return None
        removed = cards.pop(idx)
        sync_local_from_memory()

    local_path = removed.get("photo_path")
    if local_path and os.path.isfile(local_path):
        try:
            os.remove(local_path)
        except Exception as e:
            print(f"[WARN] remove local media file failed: {e}", flush=True)

    storage_path = removed.get("storage_path") or _storage_object_path(str(card_id), ".png")
    storage_delete_object(storage_path)

    sb_delete("tk_media_cards", f"id=eq.{urllib.parse.quote(str(card_id))}")
    log_activity(
        uid,
        username,
        "TTK_DELETE",
        f"{removed.get('title') or card_id} [{removed.get('category') or 'без категории'}]",
    )
    return removed


def delete_media_cards_bulk(card_ids: list[str], uid: int, username: str) -> list[dict]:
    removed_list: list[dict] = []
    for cid in card_ids:
        rem = delete_media_card_by_id(cid, uid, username)
        if rem:
            removed_list.append(rem)
    return removed_list


def update_media_card_title(card_id: str, new_title: str, uid: int, username: str) -> dict | None:
    cleaned = clean_uploaded_title(new_title) or new_title.strip()
    with DATA_LOCK:
        cards = DB.get("media_cards", [])
        card = next((c for c in cards if str(c.get("id")) == str(card_id)), None)
        if not card:
            return None
        old_title = str(card.get("title") or "")
        card["title"] = cleaned
        persist_media_card(card)

    log_activity(uid, username, "TTK_RENAME", f"{old_title} -> {cleaned}")
    return card


def update_media_card_category(card_id: str, new_category: str, uid: int, username: str) -> dict | None:
    with DATA_LOCK:
        cards = DB.get("media_cards", [])
        card = next((c for c in cards if str(c.get("id")) == str(card_id)), None)
        if not card:
            return None
        card["category"] = new_category
        persist_media_card(card)

    log_activity(uid, username, "TTK_CAT", f"{card.get('title')} -> {new_category or 'без категории'}")
    return card


def replace_media_card_file(card_id: str, msg: types.Message, uid: int, username: str) -> tuple[bool, dict | str]:
    file_id = None
    ext = ".png"
    if msg.content_type == "document" and msg.document:
        file_id = msg.document.file_id
        _, raw_ext = os.path.splitext(msg.document.file_name or "")
        if raw_ext:
            ext = raw_ext.lower()
    elif msg.content_type == "photo" and msg.photo:
        file_id = msg.photo[-1].file_id
        ext = ".jpg"
    else:
        return False, "Отправьте новый файл (например, .png) или картинку."

    downloaded = None
    try:
        file_info = bot.get_file(file_id)
        downloaded = bot.download_file(file_info.file_path)
    except Exception as e:
        print(f"[WARN] replace download_file failed: {e}", flush=True)

    local_path, storage_path = (
        _store_card_bytes(str(card_id), downloaded, ext=ext, upload_to_storage=True)
        if downloaded
        else (None, None)
    )

    with DATA_LOCK:
        cards = DB.get("media_cards", [])
        card = next((c for c in cards if str(c.get("id")) == str(card_id)), None)
        if not card:
            return False, "Карточка не найдена."
        card["photo_file_id"] = file_id
        card["photo_path"] = local_path
        if storage_path:
            card["storage_path"] = storage_path
        persist_media_card(card)

    log_activity(uid, username, "TTK_REPLACE_FILE", str(card.get("title") or card_id))
    return True, card


def save_incoming_media_card(msg: types.Message, active_category: str = "") -> tuple[bool, dict | str]:
    exact_title = exact_title_from_message(msg)
    if not exact_title:
        return False, "Не удалось определить название ТТК — отправьте файл с названием или добавьте подпись."

    file_id = None
    ext = ".png"

    if msg.content_type == "document" and msg.document:
        file_id = msg.document.file_id
        _, raw_ext = os.path.splitext(msg.document.file_name or "")
        if raw_ext:
            ext = raw_ext.lower()
    elif msg.content_type == "photo" and msg.photo:
        file_id = msg.photo[-1].file_id
        ext = ".jpg"
    else:
        return False, "Поддерживаются только файлы/документы (например, .png) и изображения."

    card_id = f"media_{uuid.uuid4().hex[:10]}"
    downloaded = None
    try:
        file_info = bot.get_file(file_id)
        downloaded = bot.download_file(file_info.file_path)
    except Exception as e:
        print(f"[WARN] download_file failed ({exact_title}): {e}", flush=True)

    local_path, storage_path = (
        _store_card_bytes(card_id, downloaded, ext=ext, upload_to_storage=True)
        if downloaded
        else (None, None)
    )

    card = {
        "id": card_id,
        "title": exact_title,
        "category": active_category,
        "photo_file_id": file_id,
        "photo_path": local_path,
        "storage_path": storage_path,
        "created_at": now_str(),
    }

    with DATA_LOCK:
        cards = DB.setdefault("media_cards", [])
        existing = next(
            (
                c
                for c in cards
                if norm_text(c.get("title")) == norm_text(exact_title)
                and norm_text(c.get("category")) == norm_text(active_category)
            ),
            None,
        )
        if existing:
            existing["title"] = exact_title
            existing["photo_file_id"] = card["photo_file_id"]
            existing["photo_path"] = card["photo_path"]
            if storage_path:
                existing["storage_path"] = storage_path
            existing["created_at"] = card["created_at"]
            if active_category:
                existing["category"] = active_category
            card = existing
        else:
            cards.append(card)
        persist_media_card(card)

    return True, card


def _bytes_to_jpeg_stream(raw_bytes: bytes) -> io.BytesIO:
    if PIL_AVAILABLE:
        try:
            with Image.open(io.BytesIO(raw_bytes)) as im:
                rgb = im.convert("RGB")
                buf = io.BytesIO()
                buf.name = "card.jpg"
                rgb.save(buf, format="JPEG", quality=92)
                buf.seek(0)
                return buf
        except Exception as e:
            print(f"[WARN] Pillow convert bytes failed: {e}", flush=True)
    buf = io.BytesIO(raw_bytes)
    buf.name = "card.png"
    buf.seek(0)
    return buf


def _build_photo_stream(card: dict):
    photo_path = card.get("photo_path")
    file_id = card.get("photo_file_id")
    storage_path = card.get("storage_path")
    card_id = str(card.get("id") or "")

    if photo_path and os.path.isfile(photo_path):
        try:
            with open(photo_path, "rb") as f:
                return _bytes_to_jpeg_stream(f.read())
        except Exception as e:
            print(f"[WARN] read photo_path failed ({photo_path}): {e}", flush=True)

    candidate_storage_paths = []
    if storage_path:
        candidate_storage_paths.append(str(storage_path))
    if card_id:
        ext = os.path.splitext(photo_path or "")[1] or ".png"
        candidate_storage_paths.append(_storage_object_path(card_id, ext))
        if ext != ".png":
            candidate_storage_paths.append(_storage_object_path(card_id, ".png"))

    for sp in candidate_storage_paths:
        cloud_bytes = storage_download_bytes(sp)
        if cloud_bytes:
            if photo_path:
                try:
                    os.makedirs(os.path.dirname(photo_path), exist_ok=True)
                    with open(photo_path, "wb") as out:
                        out.write(cloud_bytes)
                except Exception:
                    pass
            return _bytes_to_jpeg_stream(cloud_bytes)

    if file_id:
        try:
            file_info = bot.get_file(file_id)
            downloaded = bot.download_file(file_info.file_path)
            if downloaded:
                ext = os.path.splitext(photo_path or file_info.file_path or "")[1] or ".png"
                saved_local, uploaded_sp = _store_card_bytes(card_id, downloaded, ext=ext, upload_to_storage=True)
                with DATA_LOCK:
                    if saved_local:
                        card["photo_path"] = saved_local
                    if uploaded_sp:
                        card["storage_path"] = uploaded_sp
                    if saved_local or uploaded_sp:
                        persist_media_card(card)
                return _bytes_to_jpeg_stream(downloaded)
        except Exception as e:
            print(f"[WARN] download by photo_file_id failed ({file_id}): {e}", flush=True)

    return None


def safe_send_media_card(chat_id: int, card: dict, reply_markup=None) -> None:
    mark_polling_alive()
    title_raw = clean_uploaded_title(str(card.get("title") or "")) or str(card.get("title") or "")
    caption = f"<b>{esc(title_raw)}</b>"

    photo_payload = _build_photo_stream(card)
    if photo_payload is not None:
        try:
            sent = bot.send_photo(chat_id, photo_payload, caption=caption, reply_markup=reply_markup)
            if sent and sent.photo:
                new_fid = sent.photo[-1].file_id
                if new_fid and new_fid != card.get("photo_file_id"):
                    with DATA_LOCK:
                        card["photo_file_id"] = new_fid
                        persist_media_card(card)
            return
        except Exception as e:
            print(f"[WARN] send_photo failed ({title_raw}): {e}", flush=True)

    safe_send_text(
        chat_id,
        f"{caption}\n<i>⚠️ Фото недоступно в хранилище (файл не найден). Загрузи карточку заново.</i>",
        reply_markup=reply_markup,
    )


def _similarity_score(query_clean: str, title_clean: str) -> float:
    if not query_clean or not title_clean:
        return 0.0
    if query_clean == title_clean:
        return 1.0
    full_ratio = SequenceMatcher(None, query_clean, title_clean).ratio()
    q_words = query_clean.split()
    t_words = title_clean.split()

    best_word_ratio = 0.0
    for qw in q_words:
        if qw in title_clean:
            best_word_ratio = max(best_word_ratio, 0.85)
        for tw in t_words:
            best_word_ratio = max(best_word_ratio, SequenceMatcher(None, qw, tw).ratio())

    return max(full_ratio, best_word_ratio * 0.92)


def find_media_matches(query: str) -> list[dict]:
    q_raw = norm_text(query)
    q_clean = search_clean_text(query)
    if not q_raw and not q_clean:
        return []

    with DATA_LOCK:
        cards = list(DB.get("media_cards", []))

    exact = [
        c for c in cards
        if norm_text(c.get("title")) == q_raw
        or (q_clean and search_clean_text(c.get("title")) == q_clean)
    ]
    if exact:
        return exact

    keywords = [w for w in q_clean.split() if len(w) >= 1]
    if not keywords:
        return [c for c in cards if q_raw in norm_text(c.get("title"))]

    scored: list[tuple[float, dict]] = []
    for c in cards:
        title_clean = search_clean_text(c.get("title"))
        title_words = title_clean.split()
        matched_count = 0
        for kw in keywords:
            if kw in title_words or any(kw in tw for tw in title_words) or kw in title_clean:
                matched_count += 1

        sim = _similarity_score(q_clean, title_clean)
        if matched_count == len(keywords):
            score = 10.0 + matched_count + (1.5 if q_clean in title_clean else 0.0) + sim
            scored.append((score, c))
        elif matched_count > 0:
            score = 5.0 + matched_count * 1.5 + sim
            scored.append((score, c))
        elif sim >= 0.52:
            scored.append((sim * 4.0, c))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [c for _, c in scored[:8]]


def _get_filtered_cards(category_key: str) -> list[dict]:
    with DATA_LOCK:
        all_cards = list(DB.get("media_cards", []))
    if category_key == "__uncategorized__":
        return [c for c in all_cards if not norm_text(c.get("category"))]
    if category_key:
        return [c for c in all_cards if norm_text(c.get("category")) == category_key]
    return all_cards


def _manage_tab_label(manage_key: str) -> str:
    if manage_key == "__all__":
        return "Все карточки"
    if manage_key == "__uncategorized__":
        return "Без категории"
    return manage_key.capitalize() if manage_key else "Все карточки"


def _get_manage_tab_cards(manage_key: str) -> list[dict]:
    if manage_key == "__all__":
        return _get_filtered_cards("")
    return _get_filtered_cards(manage_key)


def _build_popup_page_payload(cards: list[dict], header_title: str, page: int, category_key: str):
    per_page = 8
    total_pages = max(1, (len(cards) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start_idx = page * per_page
    chunk = cards[start_idx : start_idx + per_page]

    page_note = f" (стр. {page + 1}/{total_pages})" if total_pages > 1 else ""
    text = f"<b>{esc(header_title)}</b>{esc(page_note)} — выбери позицию из списка:"
    kb = ttk_popup_keyboard(chunk, page, total_pages, category_key)
    return text, kb


def show_ttk_popup_page(
    msg: types.Message,
    header_title: str,
    page: int = 0,
    category_key: str = "",
) -> None:
    uid = msg.from_user.id
    cards = _get_filtered_cards(category_key)
    if not cards:
        safe_send_text(
            msg.chat.id,
            f"Раздел <b>{esc(header_title)}</b> пока пуст.",
            reply_markup=recipes_menu_keyboard(uid),
        )
        return

    text, kb = _build_popup_page_payload(cards, header_title, page, category_key)
    safe_send_text(msg.chat.id, text, reply_markup=kb)


def ask_manage_tab(msg: types.Message) -> None:
    if not require_admin_msg(msg):
        return
    uid = msg.from_user.id
    USER_STATES[uid] = {"mode": "ttk_choose_tab"}
    safe_send_text(
        msg.chat.id,
        "✏️ <b>Управление ТТК</b>\n\n"
        "Выбери вкладку — покажу карточки и удаление только внутри неё, чтобы случайно не задеть другие вкладки:",
        reply_markup=manage_tab_keyboard(),
    )


def show_ttk_manage_page(msg: types.Message, page: int = 0, manage_key: str = "__all__") -> None:
    if not require_admin_msg(msg):
        return
    uid = msg.from_user.id

    cards = _get_manage_tab_cards(manage_key)
    tab_label = _manage_tab_label(manage_key)

    if not cards:
        USER_STATES[uid] = {"mode": "ttk_choose_tab"}
        safe_send_text(
            msg.chat.id,
            f"Во вкладке «<b>{esc(tab_label)}</b>» карточек нет. Выбери другую вкладку:",
            reply_markup=manage_tab_keyboard(),
        )
        return

    per_page = 10
    total_pages = max(1, (len(cards) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start_idx = page * per_page
    chunk = cards[start_idx : start_idx + per_page]

    USER_STATES[uid] = {
        "mode": "ttk_select",
        "page": page,
        "manage_key": manage_key,
    }

    lines = []
    for i, c in enumerate(chunk, start=start_idx + 1):
        cat_tag = f" <i>[{esc(c.get('category') or 'без категории')}]</i>" if manage_key == "__all__" else ""
        lines.append(f"<b>{i}.</b> {esc(c.get('title') or '')}{cat_tag}")

    safe_send_text(
        msg.chat.id,
        f"✏️ <b>Вкладка: {esc(tab_label)}</b> (стр. {page + 1}/{total_pages}, всего {len(cards)}):\n\n"
        + "\n".join(lines)
        + "\n\n• Напиши <b>номер</b> (например, <code>1</code>) или точное название для редактирования/удаления.\n"
        "• Нажми <b>🗑 Удалить в этой вкладке</b> или напиши номера через запятую (<code>1, 3, 5</code> / <code>1-3</code>), "
        "чтобы удалить карточки только из вкладки «<b>" + esc(tab_label) + "</b>».",
        reply_markup=ttk_list_keyboard(page, total_pages),
    )


def open_ttk_card_actions(msg: types.Message, card: dict, manage_key: str = "__all__", page: int = 0) -> None:
    if not require_admin_msg(msg):
        return
    uid = msg.from_user.id
    USER_STATES[uid] = {
        "mode": "ttk_card_actions",
        "card_id": card.get("id"),
        "manage_key": manage_key,
        "page": page,
    }
    cat_label = card.get("category") or "без категории"
    safe_send_text(
        msg.chat.id,
        f"📄 <b>{esc(card.get('title') or '')}</b>\n"
        f"Вкладка: <b>{esc(cat_label)}</b>\n\n"
        "Выбери действие:",
        reply_markup=ttk_edit_keyboard(),
    )


# =========================================================
# ПРЕДЛОЖЕНИЯ КОКТЕЙЛЕЙ ОТ ПОЛЬЗОВАТЕЛЕЙ
# =========================================================
def submit_cocktail_suggestion(msg: types.Message, raw_text: str, category: str = "авторские") -> tuple[bool, str]:
    user = msg.from_user
    uid = int(user.id) if user else 0
    username = (user.username or "") if user else ""
    first_name = (user.first_name or "") if user else ""

    blocks = _split_multi_ttk_blocks(raw_text)
    if not blocks:
        return False, "Текст пуст — укажи название, состав и метод приготовления."

    first_block = blocks[0]
    paras = [p.strip() for p in re.split(r"\n\s*\n+", first_block) if p.strip()]
    if len(paras) >= 2 and len(paras[0].splitlines()) == 1 and len(paras[0]) <= 80:
        title = clean_uploaded_title(paras[0]) or paras[0]
    else:
        lines = [ln.strip() for ln in first_block.splitlines() if ln.strip()]
        title = clean_uploaded_title(lines[0]) if lines else "Новый коктейль"

    ing_preview, prep_preview = _parse_ttk_sections(title, first_block)
    total_line = next((q for n, q in ing_preview if norm_text(n).startswith("выход")), "")

    sug_id = f"sug_{uuid.uuid4().hex[:10]}"
    SUGGESTIONS_STORE[sug_id] = {
        "id": sug_id,
        "raw_text": raw_text.strip(),
        "title": title,
        "category": category,
        "author_id": uid,
        "author_username": username,
        "author_name": first_name,
        "status": "pending",
        "created_at": now_str(),
    }

    log_activity(uid, username, "SUGGEST_COCKTAIL", f"{title} [{category}]")

    sender_tag = f"@{username}" if username else f"id {uid}"
    ing_formatted = "\n".join(
        f"  • {esc(n)}" + (f" — <b>{esc(q)}</b>" if q else "")
        for n, q in ing_preview
        if not norm_text(n).startswith("выход")
    )
    out_text = f"\n<b>Итоговый выход:</b> {esc(total_line)}" if total_line else ""
    prep_text = "\n".join(f"  {esc(p)}" for p in prep_preview)

    admin_msg = (
        f"💡 <b>Предложен новый коктейль</b>\n"
        f"От: {esc(first_name)} ({esc(sender_tag)}, <code>{uid}</code>)\n"
        f"Вкладка: <b>{esc(category or 'без категории')}</b>\n\n"
        f"<b>Название:</b> {esc(title)}\n"
        f"<b>Состав:</b>\n{ing_formatted or '  —'}{out_text}\n"
        f"<b>Приготовление:</b>\n{prep_text or '  —'}"
    )

    admins = get_admin_ids()
    notified = 0
    for aid in admins:
        sent = bot.send_message(aid, admin_msg, reply_markup=suggestion_review_keyboard(sug_id))
        if sent:
            notified += 1

    if notified == 0:
        print(f"[WARN] No admin chat available for suggestion {sug_id}", flush=True)

    return True, title


# =========================================================
# РЕЦЕПТЫ, ПОМОЩЬ, ПОЛЬЗОВАТЕЛИ, ПОДДЕРЖКА
# =========================================================
def get_cards_for_category(category_label: str) -> tuple[str, list[dict]]:
    clean_cat = norm_text(re.sub(r"^[^\wа-яё]+\s*", "", category_label, flags=re.I))
    with DATA_LOCK:
        all_cards = list(DB.get("media_cards", []))
    matched = [c for c in all_cards if norm_text(c.get("category")) == clean_cat]
    return clean_cat, matched


def show_recipe_category(msg: types.Message, category_label: str, page: int = 0) -> None:
    clean_cat, _ = get_cards_for_category(category_label)
    show_ttk_popup_page(msg, category_label, page=page, category_key=clean_cat)


def show_media_cards_list(msg: types.Message, page: int = 0) -> None:
    show_ttk_popup_page(msg, "🖼 Карточки", page=page, category_key="")


def run_clean_titles_report(msg: types.Message) -> None:
    if not require_admin_msg(msg):
        return
    uid = msg.from_user.id
    username = msg.from_user.username or ""

    changed = clean_existing_media_titles(uid, username)
    if not changed:
        safe_send_text(
            msg.chat.id,
            "✅ Все названия в базе уже очищены — изменений не потребовалось.",
            reply_markup=recipes_menu_keyboard(uid),
        )
        return

    preview = "\n".join(f"• {esc(old)} → <b>{esc(new)}</b>" for old, new in changed[:25])
    extra = f"\n…и ещё {len(changed) - 25}" if len(changed) > 25 else ""
    safe_send_text(
        msg.chat.id,
        f"🧹 Очищено названий: <b>{len(changed)}</b>\n\n{preview}{extra}",
        reply_markup=recipes_menu_keyboard(uid),
    )


def show_users_and_actions(msg: types.Message) -> None:
    if not require_admin_msg(msg):
        return
    with DATA_LOCK:
        users = list(DB.get("users", []))[:20]
        logs = list(DB.get("activity_logs", []))[:25]

    user_lines = []
    for u in users:
        uname = f"@{u.get('username')}" if u.get("username") else "без username"
        user_lines.append(
            f"• {esc(uname)} (id <code>{esc(u.get('user_id'))}</code>) — {esc(u.get('last_seen') or '')}"
        )

    action_lines = []
    for a in logs:
        act_user = f"@{a.get('username')}" if a.get("username") else f"id {a.get('user_id')}"
        details = f" — {a.get('details')}" if a.get("details") else ""
        action_lines.append(
            f"• [{esc(a.get('created_at') or '')}] {esc(act_user)}: <b>{esc(a.get('action') or '')}</b>{esc(details)}"
        )

    text = (
        "👥 <b>Пользователи:</b>\n"
        + ("\n".join(user_lines) if user_lines else "Пока нет")
        + "\n\n📜 <b>Последние действия:</b>\n"
        + ("\n".join(action_lines) if action_lines else "Пока нет")
    )
    safe_send_text(msg.chat.id, text[:4000], reply_markup=back_only_keyboard())


def send_support_link(msg: types.Message) -> None:
    uid = msg.from_user.id
    uname = msg.from_user.username or ""
    with DATA_LOCK:
        url = _extract_support_url(DB.get("support"))
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("💛 Поддержать на DonationAlerts", url=url))
    safe_send_text(
        msg.chat.id,
        f"💛 <b>Поддержка проекта</b>\n{esc(url)}",
        reply_markup=kb,
    )
    safe_send_text(msg.chat.id, "Главное меню:", reply_markup=main_menu_keyboard(uid, uname))


def ask_bulk_category(msg: types.Message) -> None:
    if not require_admin_msg(msg):
        return
    uid = msg.from_user.id
    USER_STATES[uid] = {"mode": "bulk_choose_cat"}
    safe_send_text(
        msg.chat.id,
        "📥 В какую вкладку загрузить файлы при массовой загрузке?\n\nВыбери вкладку на клавиатуре ниже:",
        reply_markup=ttk_category_keyboard(),
    )


def start_bulk_mode(msg: types.Message, active_category: str = "") -> None:
    if not require_admin_msg(msg):
        return
    uid = msg.from_user.id
    USER_STATES[uid] = {
        "mode": "media_bulk_add",
        "count": 0,
        "max_files": MAX_BULK_FILES,
        "titles": [],
        "category": active_category,
    }
    cat_label = active_category or "без категории"
    safe_send_text(
        msg.chat.id,
        f"📥 <b>Режим массовой загрузки включён</b> (вкладка: <b>{esc(cat_label)}</b>).\n\n"
        f"За один сеанс можно загрузить до <b>{MAX_BULK_FILES}</b> файлов (сейчас загружено: <b>0/{MAX_BULK_FILES}</b>).\n"
        "Отправляй файлы подряд — лишние символы и служебные хвосты уберутся автоматически, "
        "обычные скобки <code>()</code> сохранятся.\n\n"
        "Когда закончишь — нажми <b>✅ Готово</b>.",
        reply_markup=bulk_done_keyboard(),
    )


def finish_bulk_mode(msg: types.Message) -> None:
    uid = msg.from_user.id
    uname = msg.from_user.username or ""
    st = USER_STATES.pop(uid, {})
    count = int(st.get("count") or 0)
    titles = st.get("titles") or []
    cat_label = str(st.get("category") or "") or "без категории"
    preview = "\n".join(f"• {esc(t)}" for t in titles[:20])
    extra = f"\n…и ещё {len(titles) - 20}" if len(titles) > 20 else ""
    safe_send_text(
        msg.chat.id,
        f"✅ Загрузка завершена (вкладка: <b>{esc(cat_label)}</b>). Сохранено файлов: <b>{count}</b>"
        + (f"\n\n{preview}{extra}" if preview else ""),
        reply_markup=main_menu_keyboard(uid, uname),
    )


# =========================================================
# ОБРАБОТЧИКИ TELEGRAM
# =========================================================
@bot.callback_query_handler(
    func=lambda call: call.data
    and (
        call.data.startswith(CB_CARD_PREFIX)
        or call.data.startswith(CB_PAGE_PREFIX)
        or call.data.startswith(CB_SUG_APPROVE_PREFIX)
        or call.data.startswith(CB_SUG_REJECT_PREFIX)
    )
)
def handle_ttk_callback(call: types.CallbackQuery):
    mark_polling_alive()
    data = call.data or ""
    uid = call.from_user.id if call.from_user else 0
    username = (call.from_user.username or "") if call.from_user else ""

    if data.startswith(CB_SUG_APPROVE_PREFIX) or data.startswith(CB_SUG_REJECT_PREFIX):
        is_approve = data.startswith(CB_SUG_APPROVE_PREFIX)
        sug_id = data[len(CB_SUG_APPROVE_PREFIX) :] if is_approve else data[len(CB_SUG_REJECT_PREFIX) :]
        if not is_admin(uid):
            try:
                bot.answer_callback_query(call.id, "Только администратор может принимать решения по предложениям.", show_alert=True)
            except Exception:
                pass
            return

        sug = SUGGESTIONS_STORE.get(sug_id)
        if not sug:
            try:
                bot.answer_callback_query(call.id, "Предложение не найдено или устарело.", show_alert=True)
            except Exception:
                pass
            return

        if sug.get("status") != "pending":
            try:
                bot.answer_callback_query(call.id, "Решение по этому предложению уже принято.", show_alert=True)
            except Exception:
                pass
            return

        sug["status"] = "approved" if is_approve else "rejected"
        author_id = int(sug.get("author_id") or 0)
        title = str(sug.get("title") or "Коктейль")
        cat = str(sug.get("category") or "авторские")

        try:
            bot.answer_callback_query(call.id, "Добавлено в ТТК" if is_approve else "Отклонено")
        except Exception:
            pass

        if is_approve:
            ok, res = create_ttk_from_text(str(sug.get("raw_text") or ""), cat, uid, username)
            if ok and isinstance(res, list) and call.message:
                for card in res:
                    safe_send_media_card(call.message.chat.id, card)
            if call.message:
                try:
                    bot.edit_message_text(
                        f"{call.message.text or ''}\n\n✅ <b>Добавлено в базу ({esc(cat or 'без категории')})</b>",
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                    )
                except Exception:
                    pass
            if author_id:
                safe_send_text(author_id, f"🎉 Твоё предложение «<b>{esc(title)}</b>» одобрено и добавлено в меню!")
        else:
            if call.message:
                try:
                    bot.edit_message_text(
                        f"{call.message.text or ''}\n\n❌ <b>Отклонено администратором</b>",
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                    )
                except Exception:
                    pass
            if author_id:
                safe_send_text(author_id, f"ℹ️ Предложение «<b>{esc(title)}</b>» не добавлено.")
        return

    if data.startswith(CB_CARD_PREFIX):
        short_key = data[len(CB_CARD_PREFIX) :]
        card_id = CALLBACK_CARD_MAP.get(short_key) or short_key
        with DATA_LOCK:
            cards = list(DB.get("media_cards", []))
            card = next((c for c in cards if str(c.get("id")) == card_id), None)
            if not card:
                card = next((c for c in cards if str(c.get("id")).endswith(short_key)), None)
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass
        if not card:
            if call.message:
                safe_send_text(call.message.chat.id, "Карточка не найдена.")
            return
        log_activity(uid, username, "OPEN_CARD_POPUP", str(card.get("title") or ""))
        if call.message:
            safe_send_media_card(call.message.chat.id, card)
        return

    if data.startswith(CB_PAGE_PREFIX):
        rest = data[len(CB_PAGE_PREFIX) :]
        page_str, cat_code = (rest.split(":", 1) + ["all"])[:2]
        page = int(page_str) if page_str.isdigit() else 0
        cat_key = CODE_TO_CAT.get(cat_code, "")
        cards = _get_filtered_cards(cat_key)
        header_map = {
            "авторские": BTN_REC_AUTHOR,
            "классические": BTN_REC_CLASSIC,
            "наливки": BTN_REC_INFUSIONS,
            "коктейли меню": BTN_REC_MENU,
            "премиксы": BTN_REC_PREMIXES,
            "": BTN_MEDIA_LIST,
        }
        header_title = header_map.get(cat_key, cat_key or BTN_MEDIA_LIST)
        text, kb = _build_popup_page_payload(cards, header_title, page, cat_key)
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass
        if call.message:
            try:
                bot.edit_message_text(
                    text,
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=kb,
                )
            except Exception:
                safe_send_text(call.message.chat.id, text, reply_markup=kb)


@bot.message_handler(commands=["start"])
def cmd_start(msg: types.Message):
    touch_user(msg)
    uid = msg.from_user.id
    uname = msg.from_user.username or ""
    USER_STATES.pop(uid, None)
    log_activity(uid, uname, "START")
    safe_send_text(
        msg.chat.id,
        "Привет! Выбери раздел в меню или напиши название для поиска.",
        reply_markup=main_menu_keyboard(uid, uname),
    )


@bot.message_handler(commands=["bulk"])
def cmd_bulk(msg: types.Message):
    touch_user(msg)
    if not require_admin_msg(msg):
        return
    ask_bulk_category(msg)


@bot.message_handler(commands=["clean_titles"])
def cmd_clean_titles(msg: types.Message):
    touch_user(msg)
    if not require_admin_msg(msg):
        return
    run_clean_titles_report(msg)


@bot.message_handler(commands=["broadcast"])
def cmd_broadcast(msg: types.Message):
    touch_user(msg)
    if not require_admin_msg(msg):
        return
    uid = msg.from_user.id
    USER_STATES[uid] = {"mode": "admin_broadcast"}
    total_users = len(collect_all_recipient_ids())
    safe_send_text(
        msg.chat.id,
        f"📢 <b>Рассылка всем пользователям</b> (найдено получателей: <b>{total_users}</b>).\n\n"
        "Отправь следующим сообщением текст обновления — он уйдёт всем пользователям.\n"
        "Чтобы отменить, нажми <b>⬅️ Назад</b>.",
        reply_markup=back_only_keyboard(),
    )


@bot.message_handler(commands=["set_support"])
def cmd_set_support(msg: types.Message):
    touch_user(msg)
    if not require_admin_msg(msg):
        return
    uid = msg.from_user.id
    uname = msg.from_user.username or ""
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].strip():
        ok, res = save_support_url(parts[1].strip(), uid, uname)
        if not ok:
            safe_send_text(msg.chat.id, f"⚠️ {esc(res)}", reply_markup=main_menu_keyboard(uid, uname))
            return
        safe_send_text(
            msg.chat.id,
            f"✅ Ссылка на DonationAlerts обновлена:\n{esc(res)}",
            reply_markup=main_menu_keyboard(uid, uname),
        )
        return

    USER_STATES[uid] = {"mode": "admin_set_support"}
    with DATA_LOCK:
        cur_url = _extract_support_url(DB.get("support"))
    safe_send_text(
        msg.chat.id,
        f"💛 Текущая ссылка поддержки:\n{esc(cur_url)}\n\n"
        "Отправь следующим сообщением новую ссылку на DonationAlerts (например, <code>https://www.donationalerts.com/r/твой_ник</code>) "
        "или нажми <b>⬅️ Назад</b>.",
        reply_markup=back_only_keyboard(),
    )


@bot.message_handler(content_types=["document", "photo"])
def handle_media_upload(msg: types.Message):
    touch_user(msg)
    if not require_admin_msg(msg):
        return
    uid = msg.from_user.id
    username = msg.from_user.username or ""
    st = USER_STATES.get(uid) or {}

    if st.get("mode") == "ttk_wait_replace_file":
        card_id = st.get("card_id")
        ok, res = replace_media_card_file(card_id, msg, uid, username)
        if not ok:
            safe_send_text(msg.chat.id, f"⚠️ {esc(res)}", reply_markup=back_only_keyboard())
            return
        USER_STATES.pop(uid, None)
        safe_send_text(
            msg.chat.id,
            f"✅ Файл обновлён для ТТК: <b>{esc(res.get('title') or '')}</b>",
            reply_markup=recipes_menu_keyboard(uid),
        )
        return

    if st.get("mode") == "media_bulk_add":
        max_files = int(st.get("max_files") or MAX_BULK_FILES)
        current_count = int(st.get("count") or 0)
        if current_count >= max_files:
            safe_send_text(
                msg.chat.id,
                f"⚠️ Достигнут лимит загрузки за один сеанс (<b>{max_files}/{max_files}</b>). Нажми <b>✅ Готово</b>.",
                reply_markup=bulk_done_keyboard(),
            )
            return

    active_cat = str(st.get("category") or "")
    ok, res = save_incoming_media_card(msg, active_category=active_cat)
    if not ok:
        safe_send_text(msg.chat.id, f"⚠️ {esc(res)}")
        return

    card = res
    title = str(card.get("title") or "")
    log_activity(uid, username, "MEDIA_ADD", f"{title} [{active_cat or 'без категории'}]")

    if st.get("mode") == "media_bulk_add":
        st["count"] = int(st.get("count") or 0) + 1
        max_files = int(st.get("max_files") or MAX_BULK_FILES)
        st.setdefault("titles", []).append(title)
        remaining = max(0, max_files - st["count"])
        safe_send_text(
            msg.chat.id,
            f"➕ Сохранено в «<b>{esc(active_cat or 'без категории')}</b>» "
            f"(<b>{st['count']}/{max_files}</b>, осталось <b>{remaining}</b>): <b>{esc(title)}</b>",
            reply_markup=bulk_done_keyboard(),
        )
    else:
        cat_text = active_cat or "без категории"
        safe_send_text(
            msg.chat.id,
            f"✅ Сохранено в «<b>{esc(cat_text)}</b>»: <b>{esc(title)}</b>\n"
            f"<i>Чтобы выбрать вкладку при загрузке, используй «📥 Массовая загрузка» или «🗂 Категория» в «✏️ Ред. / Удалить ТТК».</i>",
            reply_markup=main_menu_keyboard(uid, username),
        )


@bot.message_handler(content_types=["text"])
def handle_text(msg: types.Message):
    touch_user(msg)
    uid = msg.from_user.id
    username = msg.from_user.username or ""
    text = (msg.text or "").strip()
    low = norm_text(text)
    st = USER_STATES.get(uid) or {}

    if st.get("mode") == "admin_set_support":
        if not require_admin_msg(msg):
            USER_STATES.pop(uid, None)
            return
        if text == BTN_BACK:
            USER_STATES.pop(uid, None)
            safe_send_text(msg.chat.id, "Изменение отменено. Главное меню:", reply_markup=main_menu_keyboard(uid, username))
            return
        ok, res = save_support_url(text, uid, username)
        if not ok:
            safe_send_text(msg.chat.id, f"⚠️ {esc(res)}", reply_markup=back_only_keyboard())
            return
        USER_STATES.pop(uid, None)
        safe_send_text(
            msg.chat.id,
            f"✅ Ссылка на DonationAlerts обновлена:\n{esc(res)}",
            reply_markup=main_menu_keyboard(uid, username),
        )
        return

    if st.get("mode") == "admin_broadcast":
        if not require_admin_msg(msg):
            USER_STATES.pop(uid, None)
            return
        if text == BTN_BACK:
            USER_STATES.pop(uid, None)
            safe_send_text(msg.chat.id, "Рассылка отменена. Главное меню:", reply_markup=main_menu_keyboard(uid, username))
            return
        if not text:
            safe_send_text(msg.chat.id, "Текст рассылки не может быть пустым.", reply_markup=back_only_keyboard())
            return
        USER_STATES.pop(uid, None)
        safe_send_text(msg.chat.id, "⏳ Отправляю уведомление всем пользователям...")
        ok_cnt, fail_cnt = send_broadcast_to_all(text, uid, username)
        safe_send_text(
            msg.chat.id,
            f"✅ Рассылка завершена.\nДоставлено: <b>{ok_cnt}</b> | Ошибок/заблокировано: <b>{fail_cnt}</b>",
            reply_markup=main_menu_keyboard(uid, username),
        )
        return

    if st.get("mode") == "suggest_choose_cat":
        if text == BTN_BACK:
            USER_STATES.pop(uid, None)
            safe_send_text(msg.chat.id, "Главное меню:", reply_markup=main_menu_keyboard(uid, username))
            return
        chosen_cat = "" if low == "без категории" else low
        if chosen_cat and chosen_cat not in TTK_CATEGORIES:
            safe_send_text(msg.chat.id, "Выбери вкладку на клавиатуре ниже:", reply_markup=ttk_category_keyboard())
            return
        USER_STATES[uid] = {"mode": "suggest_wait_text", "category": chosen_cat}
        safe_send_text(
            msg.chat.id,
            f"Вкладка: <b>{esc(chosen_cat or 'без категории')}</b>.\n\n"
            "Напиши предложение по схеме:\n"
            "1. Название коктейля\n"
            "<i>(пустая строка)</i>\n"
            "2. Состав (например: <code>Водка 600мл, сок лимона 150мл, яблочный сок 1200мл</code>)\n"
            "<i>(пустая строка)</i>\n"
            "3. Метод приготовления\n\n"
            "После отправки администратор получит уведомление и решит, добавлять ли коктейль.",
            reply_markup=back_only_keyboard(),
        )
        return

    if st.get("mode") == "suggest_wait_text":
        if text == BTN_BACK:
            USER_STATES[uid] = {"mode": "suggest_choose_cat"}
            safe_send_text(msg.chat.id, "Выбери вкладку для предлагаемого коктейля:", reply_markup=ttk_category_keyboard())
            return
        cat = str(st.get("category") or "авторские")
        USER_STATES.pop(uid, None)
        ok, res = submit_cocktail_suggestion(msg, text, category=cat)
        if not ok:
            safe_send_text(msg.chat.id, f"⚠️ {esc(res)}", reply_markup=main_menu_keyboard(uid, username))
            return
        safe_send_text(
            msg.chat.id,
            f"✅ Предложение «<b>{esc(res)}</b>» отправлено администратору на рассмотрение!",
            reply_markup=main_menu_keyboard(uid, username),
        )
        return

    if st.get("mode") == "bulk_choose_cat":
        if not require_admin_msg(msg):
            USER_STATES.pop(uid, None)
            return
        if text == BTN_BACK:
            USER_STATES.pop(uid, None)
            safe_send_text(msg.chat.id, "Главное меню:", reply_markup=main_menu_keyboard(uid, username))
            return
        chosen_cat = "" if low == "без категории" else low
        if chosen_cat and chosen_cat not in TTK_CATEGORIES:
            safe_send_text(msg.chat.id, "Выбери вкладку на клавиатуре ниже:", reply_markup=ttk_category_keyboard())
            return
        start_bulk_mode(msg, active_category=chosen_cat)
        return

    if st.get("mode") == "media_bulk_add":
        if not require_admin_msg(msg):
            USER_STATES.pop(uid, None)
            return
        if text == BTN_DONE:
            finish_bulk_mode(msg)
            return
        if text == BTN_CANCEL:
            USER_STATES.pop(uid, None)
            safe_send_text(msg.chat.id, "❌ Загрузка отменена.", reply_markup=main_menu_keyboard(uid, username))
            return
        max_files = int(st.get("max_files") or MAX_BULK_FILES)
        cur = int(st.get("count") or 0)
        safe_send_text(
            msg.chat.id,
            f"Сейчас активен режим массовой загрузки (<b>{cur}/{max_files}</b>). Кидай файлы/PNG или нажми <b>✅ Готово</b>.",
            reply_markup=bulk_done_keyboard(),
        )
        return

    if st.get("mode") == "add_ttk_choose_cat":
        if not require_admin_msg(msg):
            USER_STATES.pop(uid, None)
            return
        if text == BTN_BACK:
            USER_STATES.pop(uid, None)
            safe_send_text(msg.chat.id, "🍸 Выбери раздел рецептов:", reply_markup=recipes_menu_keyboard(uid))
            return
        chosen_cat = "" if low == "без категории" else low
        if chosen_cat and chosen_cat not in TTK_CATEGORIES:
            safe_send_text(msg.chat.id, "Выбери категорию на клавиатуре ниже:", reply_markup=ttk_category_keyboard())
            return
        USER_STATES[uid] = {"mode": "add_ttk_wait_text", "category": chosen_cat}
        safe_send_text(
            msg.chat.id,
            f"Категория: <b>{esc(chosen_cat or 'без категории')}</b>.\n\n"
            "Пиши удобно по абзацам (через одну пустую строку):\n"
            "1. Название коктейля или премикса\n"
            "<i>(пустая строка)</i>\n"
            "2. Состав (например: <code>Водка 600мл, сок лимона 150мл, яблочный сок 1200мл</code>)\n"
            "<i>(пустая строка)</i>\n"
            "3. Метод приготовления\n\n"
            "Итоговый выход напитка посчитается автоматически и добавится в конец состава перед приготовлением.\n"
            "Чтобы добавить сразу несколько позиций в одном сообщении, отделяй их строкой <code>---</code>.",
            reply_markup=back_only_keyboard(),
        )
        return

    if st.get("mode") == "add_ttk_wait_text":
        if not require_admin_msg(msg):
            USER_STATES.pop(uid, None)
            return
        if text == BTN_BACK:
            USER_STATES[uid] = {"mode": "add_ttk_choose_cat"}
            safe_send_text(msg.chat.id, "Выбери категорию для новой ТТК:", reply_markup=ttk_category_keyboard())
            return
        cat = str(st.get("category") or "")
        USER_STATES.pop(uid, None)
        ok, res = create_ttk_from_text(text, cat, uid, username)
        if not ok:
            safe_send_text(msg.chat.id, f"⚠️ {esc(res)}", reply_markup=recipes_menu_keyboard(uid))
            return
        created_cards = res
        if not PIL_AVAILABLE:
            names = ", ".join(esc(c.get("title") or "") for c in created_cards)
            safe_send_text(
                msg.chat.id,
                f"⚠️ Сохранено карточек: <b>{len(created_cards)}</b> ({names}), но модуль Pillow не установлен — PNG не сгенерирован.",
                reply_markup=recipes_menu_keyboard(uid),
            )
            return

        for idx, card in enumerate(created_cards):
            is_last = idx == len(created_cards) - 1
            safe_send_media_card(
                msg.chat.id,
                card,
                reply_markup=recipes_menu_keyboard(uid) if is_last else None,
            )
        return

    if st.get("mode") == "ttk_choose_tab":
        if not require_admin_msg(msg):
            USER_STATES.pop(uid, None)
            return
        if text == BTN_BACK:
            USER_STATES.pop(uid, None)
            safe_send_text(msg.chat.id, "🍸 Выбери раздел рецептов:", reply_markup=recipes_menu_keyboard(uid))
            return
        if low == "все карточки":
            show_ttk_manage_page(msg, 0, manage_key="__all__")
            return
        if low == "без категории":
            show_ttk_manage_page(msg, 0, manage_key="__uncategorized__")
            return
        if low in TTK_CATEGORIES:
            show_ttk_manage_page(msg, 0, manage_key=low)
            return
        safe_send_text(msg.chat.id, "Выбери вкладку на клавиатуре ниже:", reply_markup=manage_tab_keyboard())
        return

    if st.get("mode") == "ttk_select":
        if not require_admin_msg(msg):
            USER_STATES.pop(uid, None)
            return
        page = int(st.get("page") or 0)
        manage_key = str(st.get("manage_key") or "__all__")
        tab_label = _manage_tab_label(manage_key)

        if text == BTN_BACK:
            USER_STATES.pop(uid, None)
            safe_send_text(msg.chat.id, "🍸 Выбери раздел рецептов:", reply_markup=recipes_menu_keyboard(uid))
            return
        if text == BTN_TTK_CHANGE_TAB:
            ask_manage_tab(msg)
            return
        if text == BTN_TTK_NEXT:
            show_ttk_manage_page(msg, page + 1, manage_key=manage_key)
            return
        if text == BTN_TTK_PREV:
            show_ttk_manage_page(msg, page - 1, manage_key=manage_key)
            return

        cards = _get_manage_tab_cards(manage_key)

        if text == BTN_TTK_DELETE_IN_TAB:
            USER_STATES[uid] = {
                "mode": "ttk_wait_tab_delete_nums",
                "page": page,
                "manage_key": manage_key,
            }
            safe_send_text(
                msg.chat.id,
                f"🗑 Удаление карточек <b>только из вкладки «{esc(tab_label)}»</b> (всего {len(cards)}).\n"
                "Напиши номер или номера через запятую / диапазон (например: <code>1</code>, <code>1, 3, 5</code> или <code>1-3</code>):",
                reply_markup=back_only_keyboard(),
            )
            return

        if "," in text or "-" in text:
            selected_nums = parse_number_selection(text, len(cards))
            if not selected_nums:
                safe_send_text(
                    msg.chat.id,
                    f"⚠️ Некорректные номера во вкладке «{esc(tab_label)}». "
                    f"Укажи числа от 1 до {len(cards)} через запятую (например, <code>1, 2, 4</code>).",
                    reply_markup=ttk_list_keyboard(page, max(1, (len(cards) + 9) // 10)),
                )
                return
            target_ids = [str(cards[n - 1].get("id")) for n in selected_nums]
            removed_items = delete_media_cards_bulk(target_ids, uid, username)
            removed_names = "\n".join(f"• {esc(r.get('title') or '')}" for r in removed_items)
            safe_send_text(
                msg.chat.id,
                f"🗑 Удалено из вкладки «<b>{esc(tab_label)}</b>»: <b>{len(removed_items)}</b>\n\n{removed_names}",
            )
            show_ttk_manage_page(msg, page, manage_key=manage_key)
            return

        if text.isdigit():
            idx = int(text) - 1
            if 0 <= idx < len(cards):
                open_ttk_card_actions(msg, cards[idx], manage_key=manage_key, page=page)
                return
            safe_send_text(
                msg.chat.id,
                f"⚠️ Во вкладке «{esc(tab_label)}» нет ТТК с номером {text} (всего {len(cards)}).",
            )
            return

        target = next((c for c in cards if norm_text(c.get("title")) == low), None)
        if target:
            open_ttk_card_actions(msg, target, manage_key=manage_key, page=page)
            return

        safe_send_text(
            msg.chat.id,
            f"Введи номер ТТК из вкладки «{esc(tab_label)}», несколько номеров через запятую (<code>1, 2, 3</code>) или точное название.",
        )
        return

    if st.get("mode") == "ttk_wait_tab_delete_nums":
        if not require_admin_msg(msg):
            USER_STATES.pop(uid, None)
            return
        page = int(st.get("page") or 0)
        manage_key = str(st.get("manage_key") or "__all__")
        tab_label = _manage_tab_label(manage_key)
        if text == BTN_BACK:
            show_ttk_manage_page(msg, page, manage_key=manage_key)
            return
        cards = _get_manage_tab_cards(manage_key)
        selected_nums = parse_number_selection(text, len(cards))
        if not selected_nums:
            safe_send_text(
                msg.chat.id,
                f"⚠️ Некорректные номера. Укажи номера от 1 до {len(cards)} для вкладки «{esc(tab_label)}».",
                reply_markup=back_only_keyboard(),
            )
            return
        target_ids = [str(cards[n - 1].get("id")) for n in selected_nums]
        removed_items = delete_media_cards_bulk(target_ids, uid, username)
        removed_names = "\n".join(f"• {esc(r.get('title') or '')}" for r in removed_items)
        safe_send_text(
            msg.chat.id,
            f"🗑 Удалено из вкладки «<b>{esc(tab_label)}</b>»: <b>{len(removed_items)}</b>\n\n{removed_names}",
        )
        show_ttk_manage_page(msg, page, manage_key=manage_key)
        return

    if st.get("mode") == "ttk_card_actions":
        if not require_admin_msg(msg):
            USER_STATES.pop(uid, None)
            return
        card_id = st.get("card_id")
        manage_key = str(st.get("manage_key") or "__all__")
        page = int(st.get("page") or 0)
        with DATA_LOCK:
            card = next((c for c in DB.get("media_cards", []) if str(c.get("id")) == str(card_id)), None)
        if not card or text == BTN_BACK:
            show_ttk_manage_page(msg, page, manage_key=manage_key)
            return

        if text == BTN_TTK_RENAME:
            USER_STATES[uid] = {
                "mode": "ttk_wait_rename",
                "card_id": card_id,
                "manage_key": manage_key,
                "page": page,
            }
            safe_send_text(
                msg.chat.id,
                f"Текущее название: <b>{esc(card.get('title') or '')}</b>\nВведи новое название ТТК:",
                reply_markup=back_only_keyboard(),
            )
            return

        if text == BTN_TTK_CAT:
            USER_STATES[uid] = {
                "mode": "ttk_wait_cat",
                "card_id": card_id,
                "manage_key": manage_key,
                "page": page,
            }
            safe_send_text(
                msg.chat.id,
                f"Выбери категорию для <b>{esc(card.get('title') or '')}</b>:",
                reply_markup=ttk_category_keyboard(),
            )
            return

        if text == BTN_TTK_REPLACE_FILE:
            USER_STATES[uid] = {
                "mode": "ttk_wait_replace_file",
                "card_id": card_id,
                "manage_key": manage_key,
                "page": page,
            }
            safe_send_text(
                msg.chat.id,
                f"Отправь новый файл (например, .png) или картинку для <b>{esc(card.get('title') or '')}</b>:",
                reply_markup=back_only_keyboard(),
            )
            return

        if text == BTN_TTK_DELETE:
            removed = delete_media_card_by_id(str(card_id), uid, username)
            if removed:
                safe_send_text(msg.chat.id, f"🗑 Удалено: <b>{esc(removed.get('title') or '')}</b>")
            show_ttk_manage_page(msg, page, manage_key=manage_key)
            return

    if st.get("mode") == "ttk_wait_rename":
        if not require_admin_msg(msg):
            USER_STATES.pop(uid, None)
            return
        card_id = st.get("card_id")
        manage_key = str(st.get("manage_key") or "__all__")
        page = int(st.get("page") or 0)
        if text == BTN_BACK:
            with DATA_LOCK:
                card = next((c for c in DB.get("media_cards", []) if str(c.get("id")) == str(card_id)), None)
            if card:
                open_ttk_card_actions(msg, card, manage_key=manage_key, page=page)
            else:
                show_ttk_manage_page(msg, page, manage_key=manage_key)
            return
        if not text:
            safe_send_text(msg.chat.id, "Название не может быть пустым. Введи текст:")
            return
        updated = update_media_card_title(str(card_id), text, uid, username)
        if updated:
            safe_send_text(msg.chat.id, f"✅ Новое название: <b>{esc(updated.get('title') or '')}</b>")
            open_ttk_card_actions(msg, updated, manage_key=manage_key, page=page)
        else:
            show_ttk_manage_page(msg, page, manage_key=manage_key)
        return

    if st.get("mode") == "ttk_wait_cat":
        if not require_admin_msg(msg):
            USER_STATES.pop(uid, None)
            return
        card_id = st.get("card_id")
        manage_key = str(st.get("manage_key") or "__all__")
        page = int(st.get("page") or 0)
        if text == BTN_BACK:
            with DATA_LOCK:
                card = next((c for c in DB.get("media_cards", []) if str(c.get("id")) == str(card_id)), None)
            if card:
                open_ttk_card_actions(msg, card, manage_key=manage_key, page=page)
            else:
                show_ttk_manage_page(msg, page, manage_key=manage_key)
            return
        chosen_cat = "" if low == "без категории" else low
        if chosen_cat and chosen_cat not in TTK_CATEGORIES:
            safe_send_text(msg.chat.id, "Выбери категорию на клавиатуре ниже:", reply_markup=ttk_category_keyboard())
            return
        updated = update_media_card_category(str(card_id), chosen_cat, uid, username)
        if updated:
            safe_send_text(
                msg.chat.id,
                f"✅ Категория обновлена: <b>{esc(chosen_cat or 'без категории')}</b>",
            )
            next_key = chosen_cat if manage_key not in ("__all__",) else manage_key
            open_ttk_card_actions(msg, updated, manage_key=next_key, page=0)
        else:
            show_ttk_manage_page(msg, page, manage_key=manage_key)
        return

    if st.get("mode") == "help_wait_text":
        if text == BTN_BACK:
            USER_STATES.pop(uid, None)
            safe_send_text(msg.chat.id, "Главное меню:", reply_markup=main_menu_keyboard(uid, username))
            return
        USER_STATES.pop(uid, None)
        log_activity(uid, username, "HELP_REQUEST", text[:200])
        admins = get_admin_ids()
        sender_label = f"@{username}" if username else f"id {uid}"
        report = (
            f"🆘 <b>Запрос в помощь</b>\n"
            f"От: {esc(msg.from_user.first_name or '')} ({esc(sender_label)}, <code>{uid}</code>)\n\n"
            f"{esc(text)}"
        )
        for aid in admins:
            safe_send_text(aid, report)
        safe_send_text(
            msg.chat.id,
            "✅ Запрос отправлен администратору.",
            reply_markup=main_menu_keyboard(uid, username),
        )
        return

    if st.get("mode") == "dishware_item":
        if not require_dishware_msg(msg):
            USER_STATES.pop(uid, None)
            return
        item_id = st.get("item_id")
        if text == BTN_BACK:
            USER_STATES.pop(uid, None)
            safe_send_text(msg.chat.id, "🥃 Посуда:", reply_markup=dishware_menu_keyboard(uid))
            return
        if text == BTN_DISH_SUMMARY:
            safe_send_text(msg.chat.id, format_dishware_summary(), reply_markup=dishware_item_keyboard())
            return

        delta = None
        exact = None
        if text in ("+1", "-1", "+5", "-5"):
            delta = int(text)
        elif re.fullmatch(r"[+-]\d+", text):
            delta = int(text)
        elif re.fullmatch(r"\d+", text):
            exact = int(text)

        if delta is not None or exact is not None:
            updated = adjust_dishware_qty(item_id, delta, exact, uid, username)
            if updated:
                safe_send_text(
                    msg.chat.id,
                    f"🥃 <b>{esc(updated.get('name'))}</b>: <b>{int(updated.get('qty') or 0)}шт</b>",
                    reply_markup=dishware_item_keyboard(),
                )
            return

    if text == BTN_BACK:
        USER_STATES.pop(uid, None)
        safe_send_text(msg.chat.id, "Главное меню:", reply_markup=main_menu_keyboard(uid, username))
        return

    if text == BTN_SUGGEST_COCKTAIL or low in ("предложить коктейль", "предложить"):
        USER_STATES[uid] = {"mode": "suggest_choose_cat"}
        safe_send_text(
            msg.chat.id,
            "💡 Выбери вкладку, куда предлагаешь добавить коктейль:",
            reply_markup=ttk_category_keyboard(),
        )
        return

    if text == BTN_ADD_TTK_TEXT:
        if not require_admin_msg(msg):
            return
        USER_STATES[uid] = {"mode": "add_ttk_choose_cat"}
        safe_send_text(msg.chat.id, "Выбери категорию для новой ТТК:", reply_markup=ttk_category_keyboard())
        return

    if text == BTN_MANAGE_TTK:
        ask_manage_tab(msg)
        return

    if text == BTN_CLEAN_TITLES:
        run_clean_titles_report(msg)
        return

    if text == BTN_BULK_MEDIA:
        ask_bulk_category(msg)
        return

    if text == BTN_MEDIA_LIST:
        log_activity(uid, username, "OPEN_CARDS")
        show_media_cards_list(msg, page=0)
        return

    if text == BTN_RECIPES or low == "рецепты":
        log_activity(uid, username, "OPEN_RECIPES")
        safe_send_text(msg.chat.id, "🍸 Выбери раздел рецептов:", reply_markup=recipes_menu_keyboard(uid))
        return

    if text in (BTN_REC_AUTHOR, BTN_REC_CLASSIC, BTN_REC_INFUSIONS, BTN_REC_MENU, BTN_REC_PREMIXES):
        log_activity(uid, username, "RECIPES_CAT", text)
        show_recipe_category(msg, text, page=0)
        return

    if text == BTN_DISHWARE or low == "посуда":
        if not require_dishware_msg(msg):
            return
        USER_STATES.pop(uid, None)
        log_activity(uid, username, "OPEN_DISHWARE")
        safe_send_text(
            msg.chat.id,
            "🥃 Выбери позицию посуды — можно записать привоз в плюс (<code>+10</code>), списание в минус (<code>-3</code>) или точный остаток:",
            reply_markup=dishware_menu_keyboard(uid),
        )
        return

    if text == BTN_DISH_SUMMARY or low in ("подсчёт остатков", "подсчет остатков"):
        if not require_dishware_msg(msg):
            return
        log_activity(uid, username, "DISHWARE_SUMMARY")
        safe_send_text(msg.chat.id, format_dishware_summary(), reply_markup=dishware_menu_keyboard(uid))
        return

    matched_dish = match_dishware_button(text)
    if matched_dish:
        if not require_dishware_msg(msg):
            return
        USER_STATES[uid] = {
            "mode": "dishware_item",
            "item_id": matched_dish.get("id"),
            "name": matched_dish.get("name"),
        }
        safe_send_text(
            msg.chat.id,
            f"🥃 <b>{esc(matched_dish.get('name'))}</b> — сейчас <b>{int(matched_dish.get('qty') or 0)}шт</b>.\n"
            "Нажми <b>+1 / -1 / +5 / -5</b> или отправь число (например, <code>+10</code> — привезли, <code>-3</code> — взяли со склада, <code>24</code> — точный остаток).",
            reply_markup=dishware_item_keyboard(),
        )
        return

    if text == BTN_HELP or low == "помощь":
        USER_STATES[uid] = {"mode": "help_wait_text"}
        safe_send_text(
            msg.chat.id,
            "🆘 Опиши проблему текстом и отправь сообщение — запрос придёт администратору.",
            reply_markup=back_only_keyboard(),
        )
        return

    if text == BTN_USERS or low == "пользователи":
        if not require_admin_msg(msg):
            return
        log_activity(uid, username, "OPEN_USERS")
        show_users_and_actions(msg)
        return

    if text == BTN_SUPPORT or low == "поддержка":
        log_activity(uid, username, "OPEN_SUPPORT")
        send_support_link(msg)
        return

    print(f"[TEXT_SEARCH] uid={uid} query={text!r}", flush=True)
    log_activity(uid, username, "SEARCH", text[:120])
    matches = find_media_matches(text)
    if matches:
        kb = card_buttons_keyboard(matches)
        safe_send_text(
            msg.chat.id,
            f"🔎 Похожие названия по запросу «<b>{esc(text)}</b>» — выбери карточку:",
            reply_markup=kb,
        )
        return

    safe_send_text(msg.chat.id, f"Ничего не найдено по запросу «{esc(text)}».", reply_markup=main_menu_keyboard(uid, username))


# =========================================================
# HTTP HEALTH-CHECK ДЛЯ RENDER
# =========================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        mark_polling_alive()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return


def start_web_server() -> None:
    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"[WEB] Listening on port {port}", flush=True)
    server.serve_forever()


# =========================================================
# ТОЧКА ВХОДА С WATCHDOG АВТОПЕРЕЗАПУСКОМ
# =========================================================
def run_polling_supervisor() -> None:
    while True:
        mark_polling_alive()
        try:
            bot.infinity_polling(
                skip_pending=True,
                timeout=15,
                long_polling_timeout=15,
                logger_level=0,
            )
            mark_polling_alive()
        except Exception as e:
            print(f"[WARN] infinity_polling упал с ошибкой: {e}", flush=True)
            mark_polling_alive()
        time.sleep(2)


def main() -> None:
    print(
        f"[NEW BUILD v10.37] Запуск бота (supabase: {SUPABASE_ENABLED}, "
        f"storage: {SUPABASE_STORAGE_ENABLED} [{SUPABASE_STORAGE_BUCKET}], pil: {PIL_AVAILABLE})...",
        flush=True,
    )
    with DATA_LOCK:
        DB.clear()
        DB.update(load_local_fallback())

    threading.Thread(target=start_web_server, daemon=True).start()
    threading.Thread(target=polling_watchdog_loop, daemon=True).start()
    threading.Thread(target=load_data, daemon=True).start()
    run_polling_supervisor()


if __name__ == "__main__":
    main()
