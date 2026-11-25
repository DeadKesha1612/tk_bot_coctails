import copy
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
DONATIONALERTS_URL = (
    os.getenv("DONATIONALERTS_URL") or "https://www.donationalerts.com/r/your_link"
).strip()
MAX_BULK_FILES = int((os.getenv("MAX_BULK_FILES") or "30").strip())

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в переменных окружения")

ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW.isdigit() else 0
SUPABASE_ENABLED = bool(SUPABASE_URL and SUPABASE_KEY)

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

BTN_REC_AUTHOR = "✨ Авторские"
BTN_REC_CLASSIC = "🥃 Классические"
BTN_REC_INFUSIONS = "🫙 Наливки"
BTN_REC_MENU = "🍹 Коктейли меню"
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
BTN_TTK_NEXT = "➡️ Дальше"
BTN_TTK_PREV = "⬅️ Пред."

CB_CARD_PREFIX = "ttk_open:"
CB_PAGE_PREFIX = "ttk_page:"

TTK_CATEGORIES = [
    "авторские",
    "классические",
    "наливки",
    "коктейли меню",
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

DB: dict = {
    "recipes": [],
    "dishware": [],
    "dishware_logs": [],
    "users": [],
    "support": {"id": "main", "text": DONATIONALERTS_URL},
    "activity_logs": [],
    "media_cards": [],
}


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
    """Оставляет только название коктейля и обычные скобки (), убирая служебные символы и хвосты."""
    s = (raw_title or "").strip()
    if not s:
        return ""
    s = re.sub(r"^(?:page[\s_]+)?(?:tk|тк)[\s_]+(?:tk|тк)?[\s_]*(?:коктейль|напиток)?[\s_]*", "", s, flags=re.I)
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
def main_menu_keyboard(user_id: int) -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(types.KeyboardButton(BTN_RECIPES), types.KeyboardButton(BTN_DISHWARE))
    kb.row(types.KeyboardButton(BTN_HELP), types.KeyboardButton(BTN_SUPPORT))
    if is_admin(user_id):
        kb.row(types.KeyboardButton(BTN_USERS))
    return kb


def recipes_menu_keyboard(user_id: int) -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(types.KeyboardButton(BTN_REC_AUTHOR), types.KeyboardButton(BTN_REC_CLASSIC))
    kb.row(types.KeyboardButton(BTN_REC_INFUSIONS), types.KeyboardButton(BTN_REC_MENU))
    kb.row(types.KeyboardButton(BTN_MEDIA_LIST), types.KeyboardButton(BTN_ADD_TTK_TEXT))
    if is_admin(user_id):
        kb.row(types.KeyboardButton(BTN_MANAGE_TTK), types.KeyboardButton(BTN_BULK_MEDIA))
        kb.row(types.KeyboardButton(BTN_CLEAN_TITLES))
    kb.row(types.KeyboardButton(BTN_BACK))
    return kb


def ttk_popup_keyboard(page_cards: list[dict], page: int, total_pages: int, cat_key: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    for card in page_cards:
        cid = str(card.get("id") or "")
        title = str(card.get("title") or "Без названия")[:64]
        kb.add(types.InlineKeyboardButton(text=title, callback_data=f"{CB_CARD_PREFIX}{cid}"))

    nav_buttons = []
    if total_pages > 1:
        prev_page = page - 1 if page > 0 else total_pages - 1
        next_page = page + 1 if page + 1 < total_pages else 0
        nav_buttons.append(
            types.InlineKeyboardButton(text="⬅️ Назад", callback_data=f"{CB_PAGE_PREFIX}{prev_page}:{cat_key}")
        )
        nav_buttons.append(
            types.InlineKeyboardButton(text="➡️ Вперёд", callback_data=f"{CB_PAGE_PREFIX}{next_page}:{cat_key}")
        )
        kb.row(*nav_buttons)
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
    kb.row(types.KeyboardButton("Без категории"), types.KeyboardButton(BTN_BACK))
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
# SUPABASE REST API + ЛОКАЛЬНЫЙ СНИМОК
# =========================================================
def sb_request(method: str, table: str, query: str = "", payload=None, prefer: str = ""):
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
        with urllib.request.urlopen(req, timeout=15) as resp:
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
    with DATA_LOCK:
        data = load_local_fallback()
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

        users = sb_select("tk_users", "order=last_seen.desc")
        if users is not None:
            data["users"] = users

        support_rows = sb_select("tk_support", "id=eq.main&limit=1")
        if support_rows:
            data["support"] = support_rows[0]

        activity_logs = sb_select("tk_activity_logs", "order=id.desc&limit=800")
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
                    "created_at": str(m.get("created_at") or now_str()),
                }
                for m in media_rows
            ]
        data.setdefault("media_cards", [])

        DB.clear()
        DB.update(data)
        sync_local_from_memory()


def log_activity(user_id: int, username: str, action: str, details: str = "") -> None:
    entry = {
        "id": f"act_{uuid.uuid4().hex[:12]}",
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
    sb_upsert("tk_activity_logs", entry, "id")


def touch_user(msg: types.Message) -> None:
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
        sb_upsert("tk_users", row, "user_id")


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
# ТТК / КАРТОЧКИ + ОФОРМЛЕНИЕ ТТК В PNG (tk_media_cards)
# =========================================================
def _download_font_if_needed(target_path: str, urls: list[str]) -> str | None:
    if os.path.isfile(target_path) and os.path.getsize(target_path) > 10000:
        return target_path
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
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
                "eyebrow": ImageFont.truetype(bold_path, 16),
                "title": ImageFont.truetype(bold_path, 34),
                "sec_head": ImageFont.truetype(bold_path, 19),
                "col_head": ImageFont.truetype(bold_path, 15),
                "item": ImageFont.truetype(reg_path, 22),
                "item_bold": ImageFont.truetype(bold_path, 22),
                "prep": ImageFont.truetype(reg_path, 21),
            }
        except Exception as e:
            print(f"[WARN] ImageFont.truetype failed: {e}", flush=True)
    fallback = ImageFont.load_default()
    return {
        "eyebrow": fallback,
        "title": fallback,
        "sec_head": fallback,
        "col_head": fallback,
        "item": fallback,
        "item_bold": fallback,
        "prep": fallback,
    }


def _parse_ttk_sections(title: str, body_text: str) -> tuple[list[tuple[str, str]], list[str]]:
    ingredients: list[tuple[str, str]] = []
    prep_lines: list[str] = []
    in_prep = False

    raw_lines = [ln.strip() for ln in (body_text or "").splitlines() if ln.strip()]
    for ln in raw_lines:
        low = norm_text(ln)
        if re.match(r"^(приготовление|способ приготовления|метод)[:：]?$", low):
            in_prep = True
            continue
        if re.match(r"^(состав|ингредиенты)[:：]?$", low):
            in_prep = False
            continue

        if in_prep:
            prep_lines.append(ln)
            continue

        m_sep = re.match(
            r"^(.+?)\s*(?:\t|\s{2,}|[:—–-])\s*(\d+(?:[.,]\d+)?\s*(?:мл|г|гр|кг|л|шт|дэш|кап)?)$",
            ln,
            flags=re.I,
        )
        if not m_sep:
            m_sep = re.match(r"^(.+?)\s+(\d+(?:[.,]\d+)?\s*(?:мл|г|гр|кг|л|шт|дэш|кап))$", ln, flags=re.I)

        if m_sep:
            ing_name = m_sep.group(1).strip(" •-–—")
            ing_qty = m_sep.group(2).strip()
            ingredients.append((ing_name, ing_qty))
        elif len(ln) <= 75 and not re.search(r"[.!?]{2,}", ln) and not ln.endswith("."):
            ingredients.append((ln.strip(" •-–—"), ""))
        else:
            in_prep = True
            prep_lines.append(ln)

    if not ingredients and not prep_lines:
        prep_lines.append(title)

    return ingredients, prep_lines


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return int(bbox[2] - bbox[0])


def render_ttk_png_bytes(title: str, body_text: str, category: str = "") -> bytes | None:
    if not PIL_AVAILABLE:
        return None

    fonts = load_ttk_card_fonts()
    ingredients, prep_lines = _parse_ttk_sections(title, body_text)

    canvas_w = 1280
    outer_pad = 36
    card_x0 = outer_pad
    card_y0 = outer_pad
    card_x1 = canvas_w - outer_pad

    inner_x0 = card_x0 + 68
    inner_x1 = card_x1 - 68

    title_lines = textwrap.wrap(title.upper(), width=38) or [title.upper()]
    wrapped_ing: list[tuple[str, str]] = []
    for name, qty in ingredients:
        w_lines = textwrap.wrap(name, width=54) or [name]
        for idx, w_line in enumerate(w_lines):
            wrapped_ing.append((w_line, qty if idx == 0 else ""))

    wrapped_prep: list[str] = []
    for p_line in prep_lines:
        wrapped_prep.extend(textwrap.wrap(p_line, width=78) or [p_line])

    est_h = 110 + len(title_lines) * 44 + 40
    if wrapped_ing:
        est_h += 95 + len(wrapped_ing) * 46 + 24
    if wrapped_prep:
        est_h += 75 + len(wrapped_prep) * 34 + 30
    est_h += 55

    canvas_h = max(620, outer_pad * 2 + est_h)
    card_y1 = canvas_h - outer_pad

    img = Image.new("RGB", (canvas_w, canvas_h), color=(28, 30, 36))
    draw = ImageDraw.Draw(img)

    draw.rounded_rectangle([card_x0, card_y0, card_x1, card_y1], radius=24, fill=(246, 247, 249))

    y = card_y0 + 54
    draw.text((inner_x0, y), "ТЕХНОЛОГИЧЕСКАЯ КАРТА", fill=(115, 122, 134), font=fonts["eyebrow"])
    y += 32

    for t_line in title_lines:
        draw.text((inner_x0, y), t_line, fill=(17, 20, 26), font=fonts["title"])
        y += 43

    y += 14
    draw.line([(inner_x0, y), (inner_x1, y)], fill=(214, 218, 224), width=2)
    y += 26

    if wrapped_ing:
        draw.text((inner_x0, y), "СОСТАВ", fill=(17, 20, 26), font=fonts["sec_head"])
        y += 32

        draw.text((inner_x0, y), "ИНГРЕДИЕНТ / ПОЗИЦИЯ", fill=(122, 128, 138), font=fonts["col_head"])
        qty_hdr = "КОЛИЧЕСТВО"
        draw.text(
            (inner_x1 - _text_width(draw, qty_hdr, fonts["col_head"]), y),
            qty_hdr,
            fill=(122, 128, 138),
            font=fonts["col_head"],
        )
        y += 28

        for idx, (ing_name, ing_qty) in enumerate(wrapped_ing):
            is_vykhod = norm_text(ing_name).startswith("выход")
            row_font = fonts["item_bold"] if is_vykhod else fonts["item"]
            qty_font = fonts["item_bold"] if (is_vykhod or ing_qty) else fonts["item"]

            draw.text((inner_x0, y), ing_name, fill=(24, 28, 34), font=row_font)
            if ing_qty:
                qw = _text_width(draw, ing_qty, qty_font)
                draw.text((inner_x1 - qw, y), ing_qty, fill=(24, 28, 34), font=qty_font)

            y += 36
            if idx < len(wrapped_ing) - 1:
                draw.line([(inner_x0, y), (inner_x1, y)], fill=(230, 233, 238), width=1)
            y += 10

        y += 14

    if wrapped_prep:
        draw.text((inner_x0, y), "ПРИГОТОВЛЕНИЕ", fill=(17, 20, 26), font=fonts["sec_head"])
        y += 34
        for p_line in wrapped_prep:
            draw.text((inner_x0, y), p_line, fill=(36, 40, 48), font=fonts["prep"])
            y += 33

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def create_ttk_from_text(raw_text: str, category: str, uid: int, username: str) -> tuple[bool, dict | str]:
    lines = [ln.strip() for ln in (raw_text or "").splitlines() if ln.strip()]
    if not lines:
        return False, "Текст пуст — напиши название и описание/состав ТТК."

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
    local_path = os.path.join(MEDIA_DIR, f"{card_id}.png")
    png_bytes = render_ttk_png_bytes(title, body, category=category)
    if png_bytes:
        try:
            with open(local_path, "wb") as out:
                out.write(png_bytes)
        except Exception as e:
            print(f"[WARN] write rendered PNG failed: {e}", flush=True)
            local_path = None
    else:
        local_path = None

    card = {
        "id": card_id,
        "title": title,
        "category": category,
        "photo_file_id": None,
        "photo_path": local_path,
        "created_at": now_str(),
    }

    with DATA_LOCK:
        cards = DB.setdefault("media_cards", [])
        existing = next((c for c in cards if norm_text(c.get("title")) == norm_text(title)), None)
        if existing:
            existing["photo_path"] = local_path
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


def persist_media_card(card: dict) -> None:
    sync_local_from_memory()
    row = {
        "id": str(card["id"]),
        "title": str(card.get("title") or ""),
        "category": str(card.get("category") or ""),
        "photo_file_id": card.get("photo_file_id"),
        "photo_path": card.get("photo_path"),
        "created_at": str(card.get("created_at") or now_str()),
    }
    res = sb_upsert("tk_media_cards", row, "id")
    if res is None and "category" in row:
        row_no_cat = {k: v for k, v in row.items() if k != "category"}
        sb_upsert("tk_media_cards", row_no_cat, "id")


def clean_existing_media_titles(uid: int, username: str) -> list[tuple[str, str]]:
    """Очищает названия всех существующих карточек в базе по правилам clean_uploaded_title."""
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

    sb_delete("tk_media_cards", f"id=eq.{urllib.parse.quote(str(card_id))}")
    log_activity(uid, username, "TTK_DELETE", str(removed.get("title") or card_id))
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

    local_path = os.path.join(MEDIA_DIR, f"{card_id}{ext}")
    try:
        file_info = bot.get_file(file_id)
        downloaded = bot.download_file(file_info.file_path)
        with open(local_path, "wb") as out:
            out.write(downloaded)
    except Exception as e:
        print(f"[WARN] replace download_file failed: {e}", flush=True)
        local_path = None

    with DATA_LOCK:
        cards = DB.get("media_cards", [])
        card = next((c for c in cards if str(c.get("id")) == str(card_id)), None)
        if not card:
            return False, "Карточка не найдена."
        card["photo_file_id"] = None
        card["photo_path"] = local_path
        persist_media_card(card)

    log_activity(uid, username, "TTK_REPLACE_FILE", str(card.get("title") or card_id))
    return True, card


def save_incoming_media_card(msg: types.Message, active_category: str = "") -> tuple[bool, dict | str]:
    exact_title = exact_title_from_message(msg)
    if not exact_title:
        return False, "Не удалось определить название коктейля — отправьте файл с названием или добавьте подпись."

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
    safe_disk_name = f"{card_id}{ext}"
    local_path = os.path.join(MEDIA_DIR, safe_disk_name)

    try:
        file_info = bot.get_file(file_id)
        downloaded = bot.download_file(file_info.file_path)
        with open(local_path, "wb") as out:
            out.write(downloaded)
    except Exception as e:
        print(f"[WARN] download_file failed ({exact_title}): {e}", flush=True)
        local_path = None

    card = {
        "id": card_id,
        "title": exact_title,
        "category": active_category,
        "photo_file_id": None if msg.content_type == "document" else file_id,
        "photo_path": local_path,
        "created_at": now_str(),
    }

    with DATA_LOCK:
        cards = DB.setdefault("media_cards", [])
        existing = next((c for c in cards if norm_text(c.get("title")) == norm_text(exact_title)), None)
        if existing:
            existing["title"] = exact_title
            existing["photo_file_id"] = card["photo_file_id"]
            existing["photo_path"] = card["photo_path"]
            existing["created_at"] = card["created_at"]
            if active_category:
                existing["category"] = active_category
            card = existing
        else:
            cards.append(card)
        persist_media_card(card)

    return True, card


def _build_photo_stream(photo_path: str | None, file_id: str | None):
    if photo_path and os.path.isfile(photo_path):
        if PIL_AVAILABLE:
            try:
                with Image.open(photo_path) as im:
                    rgb = im.convert("RGB")
                    buf = io.BytesIO()
                    buf.name = "card.jpg"
                    rgb.save(buf, format="JPEG", quality=92)
                    buf.seek(0)
                    return buf
            except Exception as e:
                print(f"[WARN] Pillow convert to JPEG failed ({photo_path}): {e}", flush=True)
        try:
            with open(photo_path, "rb") as f:
                data = f.read()
            buf = io.BytesIO(data)
            buf.name = os.path.basename(photo_path) or "card.png"
            buf.seek(0)
            return buf
        except Exception as e:
            print(f"[WARN] read photo_path failed ({photo_path}): {e}", flush=True)

    if file_id:
        return file_id

    return None


def safe_send_media_card(chat_id: int, card: dict, reply_markup=None) -> None:
    title_raw = clean_uploaded_title(str(card.get("title") or "")) or str(card.get("title") or "")
    caption = f"<b>{esc(title_raw)}</b>"
    file_id = card.get("photo_file_id")
    photo_path = card.get("photo_path")

    photo_payload = _build_photo_stream(photo_path, file_id)
    if photo_payload is not None:
        try:
            sent = bot.send_photo(chat_id, photo_payload, caption=caption, reply_markup=reply_markup)
            if sent and sent.photo and not file_id:
                new_fid = sent.photo[-1].file_id
                with DATA_LOCK:
                    card["photo_file_id"] = new_fid
                    persist_media_card(card)
            return
        except Exception as e:
            print(f"[WARN] send_photo failed ({title_raw}): {e}", flush=True)

    safe_send_text(chat_id, caption, reply_markup=reply_markup)


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

    all_words_matches: list[tuple[int, dict]] = []
    partial_matches: list[tuple[int, dict]] = []

    for c in cards:
        title_clean = search_clean_text(c.get("title"))
        title_words = title_clean.split()
        matched_count = 0
        for kw in keywords:
            if kw in title_words or any(kw in tw for tw in title_words) or kw in title_clean:
                matched_count += 1

        if matched_count == len(keywords):
            score = matched_count * 10 + (1 if q_clean in title_clean else 0)
            all_words_matches.append((score, c))
        elif matched_count > 0:
            partial_matches.append((matched_count, c))

    if all_words_matches:
        all_words_matches.sort(key=lambda pair: pair[0], reverse=True)
        return [c for _, c in all_words_matches]

    partial_matches.sort(key=lambda pair: pair[0], reverse=True)
    return [c for _, c in partial_matches]


def _get_filtered_cards(category_key: str) -> list[dict]:
    with DATA_LOCK:
        all_cards = list(DB.get("media_cards", []))
    if category_key:
        return [c for c in all_cards if norm_text(c.get("category")) == category_key]
    return all_cards


def _build_popup_page_payload(cards: list[dict], header_title: str, page: int, category_key: str):
    per_page = 8
    total_pages = max(1, (len(cards) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start_idx = page * per_page
    chunk = cards[start_idx : start_idx + per_page]

    page_note = f" (стр. {page + 1}/{total_pages})" if total_pages > 1 else ""
    text = f"<b>{esc(header_title)}</b>{esc(page_note)} — выбери коктейль из списка:"
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


def show_ttk_manage_page(msg: types.Message, page: int = 0) -> None:
    uid = msg.from_user.id
    if not is_admin(uid):
        safe_send_text(msg.chat.id, "Недостаточно прав.", reply_markup=main_menu_keyboard(uid))
        return
    with DATA_LOCK:
        cards = list(DB.get("media_cards", []))
    if not cards:
        USER_STATES.pop(uid, None)
        safe_send_text(msg.chat.id, "ТТК (карточек) пока нет.", reply_markup=recipes_menu_keyboard(uid))
        return

    per_page = 10
    total_pages = max(1, (len(cards) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start_idx = page * per_page
    chunk = cards[start_idx : start_idx + per_page]

    USER_STATES[uid] = {
        "mode": "ttk_select",
        "page": page,
        "page_map": {str(start_idx + i + 1): c.get("id") for i, c in enumerate(chunk)},
    }

    lines = []
    for i, c in enumerate(chunk, start=start_idx + 1):
        cat = f" <i>[{esc(c.get('category'))}]</i>" if c.get("category") else ""
        lines.append(f"<b>{i}.</b> {esc(c.get('title') or '')}{cat}")

    safe_send_text(
        msg.chat.id,
        f"✏️ <b>Управление ТТК</b> (стр. {page + 1}/{total_pages}, всего {len(cards)}):\n\n"
        + "\n".join(lines)
        + "\n\n• Напиши <b>один номер</b> (например, <code>1</code>) или точное название для редактирования/удаления.\n"
        "• Напиши <b>несколько номеров через запятую</b> (например, <code>1, 3, 5</code> или <code>1-3</code>), чтобы удалить их сразу.",
        reply_markup=ttk_list_keyboard(page, total_pages),
    )


def open_ttk_card_actions(msg: types.Message, card: dict) -> None:
    uid = msg.from_user.id
    USER_STATES[uid] = {"mode": "ttk_card_actions", "card_id": card.get("id")}
    cat_label = card.get("category") or "не задана"
    safe_send_text(
        msg.chat.id,
        f"📄 <b>{esc(card.get('title') or '')}</b>\n"
        f"Категория: <b>{esc(cat_label)}</b>\n\n"
        "Выбери действие:",
        reply_markup=ttk_edit_keyboard(),
    )


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
    uid = msg.from_user.id
    username = msg.from_user.username or ""
    if not is_admin(uid):
        safe_send_text(msg.chat.id, "Недостаточно прав.", reply_markup=main_menu_keyboard(uid))
        return

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
    uid = msg.from_user.id
    if not is_admin(uid):
        safe_send_text(msg.chat.id, "Недостаточно прав.", reply_markup=main_menu_keyboard(uid))
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
    with DATA_LOCK:
        sup_raw = str((DB.get("support") or {}).get("text") or "").strip()
    url = sup_raw if sup_raw.startswith(("http://", "https://")) else DONATIONALERTS_URL
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("💛 Поддержать на DonationAlerts", url=url))
    safe_send_text(
        msg.chat.id,
        f"💛 <b>Поддержка проекта</b>\n{esc(url)}",
        reply_markup=kb,
    )
    safe_send_text(msg.chat.id, "Главное меню:", reply_markup=main_menu_keyboard(uid))


def ask_bulk_category(msg: types.Message) -> None:
    uid = msg.from_user.id
    if not is_admin(uid):
        safe_send_text(msg.chat.id, "Недостаточно прав.", reply_markup=main_menu_keyboard(uid))
        return
    USER_STATES[uid] = {"mode": "bulk_choose_cat"}
    safe_send_text(
        msg.chat.id,
        "📥 В какую вкладку загрузить файлы при массовой загрузке?\n\nВыбери вкладку на клавиатуре ниже:",
        reply_markup=ttk_category_keyboard(),
    )


def start_bulk_mode(msg: types.Message, active_category: str = "") -> None:
    uid = msg.from_user.id
    if not is_admin(uid):
        safe_send_text(msg.chat.id, "Недостаточно прав.", reply_markup=main_menu_keyboard(uid))
        return
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
        reply_markup=main_menu_keyboard(uid),
    )


# =========================================================
# ОБРАБОТЧИКИ TELEGRAM
# =========================================================
@bot.callback_query_handler(func=lambda call: call.data and (call.data.startswith(CB_CARD_PREFIX) or call.data.startswith(CB_PAGE_PREFIX)))
def handle_ttk_callback(call: types.CallbackQuery):
    data = call.data or ""
    uid = call.from_user.id if call.from_user else 0
    username = (call.from_user.username or "") if call.from_user else ""

    if data.startswith(CB_CARD_PREFIX):
        card_id = data[len(CB_CARD_PREFIX) :]
        with DATA_LOCK:
            card = next((c for c in DB.get("media_cards", []) if str(c.get("id")) == card_id), None)
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
        page_str, cat_key = (rest.split(":", 1) + [""])[:2]
        page = int(page_str) if page_str.isdigit() else 0
        cards = _get_filtered_cards(cat_key)
        header_map = {
            "авторские": BTN_REC_AUTHOR,
            "классические": BTN_REC_CLASSIC,
            "наливки": BTN_REC_INFUSIONS,
            "коктейли меню": BTN_REC_MENU,
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
    USER_STATES.pop(uid, None)
    log_activity(uid, msg.from_user.username or "", "START")
    safe_send_text(
        msg.chat.id,
        "Привет! Выбери раздел в меню или напиши название для поиска.",
        reply_markup=main_menu_keyboard(uid),
    )


@bot.message_handler(commands=["bulk"])
def cmd_bulk(msg: types.Message):
    touch_user(msg)
    ask_bulk_category(msg)


@bot.message_handler(commands=["clean_titles"])
def cmd_clean_titles(msg: types.Message):
    touch_user(msg)
    run_clean_titles_report(msg)


@bot.message_handler(content_types=["document", "photo"])
def handle_media_upload(msg: types.Message):
    touch_user(msg)
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
        safe_send_text(
            msg.chat.id,
            f"✅ Сохранено: <b>{esc(title)}</b>",
            reply_markup=main_menu_keyboard(uid),
        )


@bot.message_handler(content_types=["text"])
def handle_text(msg: types.Message):
    touch_user(msg)
    uid = msg.from_user.id
    username = msg.from_user.username or ""
    text = (msg.text or "").strip()
    low = norm_text(text)
    st = USER_STATES.get(uid) or {}

    if st.get("mode") == "bulk_choose_cat":
        if text == BTN_BACK:
            USER_STATES.pop(uid, None)
            safe_send_text(msg.chat.id, "Главное меню:", reply_markup=main_menu_keyboard(uid))
            return
        chosen_cat = "" if low == "без категории" else low
        if chosen_cat and chosen_cat not in TTK_CATEGORIES:
            safe_send_text(msg.chat.id, "Выбери вкладку на клавиатуре ниже:", reply_markup=ttk_category_keyboard())
            return
        start_bulk_mode(msg, active_category=chosen_cat)
        return

    if st.get("mode") == "media_bulk_add":
        if text == BTN_DONE:
            finish_bulk_mode(msg)
            return
        if text == BTN_CANCEL:
            USER_STATES.pop(uid, None)
            safe_send_text(msg.chat.id, "❌ Загрузка отменена.", reply_markup=main_menu_keyboard(uid))
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
            "Напиши ТТК текстом:\n"
            "• 1-я строка — <b>название</b>\n"
            "• Строки с количеством (например, <code>Выход: 120 мл</code>, <code>Джин 30 мл</code>) — уйдут в таблицу <b>СОСТАВ</b>\n"
            "• Остальные строки — в блок <b>ПРИГОТОВЛЕНИЕ</b>",
            reply_markup=back_only_keyboard(),
        )
        return

    if st.get("mode") == "add_ttk_wait_text":
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
        card = res
        if not card.get("photo_path"):
            safe_send_text(
                msg.chat.id,
                f"⚠️ Карточка <b>{esc(card.get('title'))}</b> сохранена, но модуль Pillow не установлен — PNG не сгенерирован.",
                reply_markup=recipes_menu_keyboard(uid),
            )
            return
        safe_send_media_card(msg.chat.id, card, reply_markup=recipes_menu_keyboard(uid))
        return

    if st.get("mode") == "ttk_select":
        page = int(st.get("page") or 0)
        if text == BTN_BACK:
            USER_STATES.pop(uid, None)
            safe_send_text(msg.chat.id, "🍸 Выбери раздел рецептов:", reply_markup=recipes_menu_keyboard(uid))
            return
        if text == BTN_TTK_NEXT:
            show_ttk_manage_page(msg, page + 1)
            return
        if text == BTN_TTK_PREV:
            show_ttk_manage_page(msg, page - 1)
            return

        with DATA_LOCK:
            cards = list(DB.get("media_cards", []))

        if "," in text or "-" in text:
            selected_nums = parse_number_selection(text, len(cards))
            if not selected_nums:
                safe_send_text(
                    msg.chat.id,
                    f"⚠️ Некорректные номера. Укажи числа от 1 до {len(cards)} через запятую (например, <code>1, 2, 4</code>).",
                    reply_markup=ttk_list_keyboard(page, max(1, (len(cards) + 9) // 10)),
                )
                return
            target_ids = [str(cards[n - 1].get("id")) for n in selected_nums]
            removed_items = delete_media_cards_bulk(target_ids, uid, username)
            removed_names = "\n".join(f"• {esc(r.get('title') or '')}" for r in removed_items)
            safe_send_text(
                msg.chat.id,
                f"🗑 Удалено ТТК: <b>{len(removed_items)}</b>\n\n{removed_names}",
            )
            show_ttk_manage_page(msg, page)
            return

        if text.isdigit():
            idx = int(text) - 1
            if 0 <= idx < len(cards):
                open_ttk_card_actions(msg, cards[idx])
                return
            safe_send_text(msg.chat.id, f"⚠️ ТТК с номером {text} нет (всего {len(cards)}).")
            return

        target = next((c for c in cards if norm_text(c.get("title")) == low), None)
        if target:
            open_ttk_card_actions(msg, target)
            return

        safe_send_text(
            msg.chat.id,
            "Введи номер ТТК, несколько номеров через запятую (например, <code>1, 2, 3</code>) или точное название.",
        )
        return

    if st.get("mode") == "ttk_card_actions":
        card_id = st.get("card_id")
        with DATA_LOCK:
            card = next((c for c in DB.get("media_cards", []) if str(c.get("id")) == str(card_id)), None)
        if not card or text == BTN_BACK:
            show_ttk_manage_page(msg, 0)
            return

        if text == BTN_TTK_RENAME:
            USER_STATES[uid] = {"mode": "ttk_wait_rename", "card_id": card_id}
            safe_send_text(
                msg.chat.id,
                f"Текущее название: <b>{esc(card.get('title') or '')}</b>\nВведи новое название ТТК:",
                reply_markup=back_only_keyboard(),
            )
            return

        if text == BTN_TTK_CAT:
            USER_STATES[uid] = {"mode": "ttk_wait_cat", "card_id": card_id}
            safe_send_text(
                msg.chat.id,
                f"Выбери категорию для <b>{esc(card.get('title') or '')}</b>:",
                reply_markup=ttk_category_keyboard(),
            )
            return

        if text == BTN_TTK_REPLACE_FILE:
            USER_STATES[uid] = {"mode": "ttk_wait_replace_file", "card_id": card_id}
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
            show_ttk_manage_page(msg, 0)
            return

    if st.get("mode") == "ttk_wait_rename":
        card_id = st.get("card_id")
        if text == BTN_BACK:
            with DATA_LOCK:
                card = next((c for c in DB.get("media_cards", []) if str(c.get("id")) == str(card_id)), None)
            if card:
                open_ttk_card_actions(msg, card)
            else:
                show_ttk_manage_page(msg, 0)
            return
        if not text:
            safe_send_text(msg.chat.id, "Название не может быть пустым. Введи текст:")
            return
        updated = update_media_card_title(str(card_id), text, uid, username)
        if updated:
            safe_send_text(msg.chat.id, f"✅ Новое название: <b>{esc(updated.get('title') or '')}</b>")
            open_ttk_card_actions(msg, updated)
        else:
            show_ttk_manage_page(msg, 0)
        return

    if st.get("mode") == "ttk_wait_cat":
        card_id = st.get("card_id")
        if text == BTN_BACK:
            with DATA_LOCK:
                card = next((c for c in DB.get("media_cards", []) if str(c.get("id")) == str(card_id)), None)
            if card:
                open_ttk_card_actions(msg, card)
            else:
                show_ttk_manage_page(msg, 0)
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
            open_ttk_card_actions(msg, updated)
        else:
            show_ttk_manage_page(msg, 0)
        return

    if st.get("mode") == "help_wait_text":
        if text == BTN_BACK:
            USER_STATES.pop(uid, None)
            safe_send_text(msg.chat.id, "Главное меню:", reply_markup=main_menu_keyboard(uid))
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
            reply_markup=main_menu_keyboard(uid),
        )
        return

    if st.get("mode") == "dishware_item":
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
        safe_send_text(msg.chat.id, "Главное меню:", reply_markup=main_menu_keyboard(uid))
        return

    if text == BTN_ADD_TTK_TEXT:
        USER_STATES[uid] = {"mode": "add_ttk_choose_cat"}
        safe_send_text(msg.chat.id, "Выбери категорию для новой ТТК:", reply_markup=ttk_category_keyboard())
        return

    if text == BTN_MANAGE_TTK:
        show_ttk_manage_page(msg, 0)
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

    if text in (BTN_REC_AUTHOR, BTN_REC_CLASSIC, BTN_REC_INFUSIONS, BTN_REC_MENU):
        log_activity(uid, username, "RECIPES_CAT", text)
        show_recipe_category(msg, text, page=0)
        return

    if text == BTN_DISHWARE or low == "посуда":
        USER_STATES.pop(uid, None)
        log_activity(uid, username, "OPEN_DISHWARE")
        safe_send_text(msg.chat.id, "🥃 Выбери позицию посуды:", reply_markup=dishware_menu_keyboard(uid))
        return

    if text == BTN_DISH_SUMMARY:
        log_activity(uid, username, "DISHWARE_SUMMARY")
        safe_send_text(msg.chat.id, format_dishware_summary(), reply_markup=dishware_menu_keyboard(uid))
        return

    matched_dish = match_dishware_button(text)
    if matched_dish:
        USER_STATES[uid] = {
            "mode": "dishware_item",
            "item_id": matched_dish.get("id"),
            "name": matched_dish.get("name"),
        }
        safe_send_text(
            msg.chat.id,
            f"🥃 <b>{esc(matched_dish.get('name'))}</b> — сейчас <b>{int(matched_dish.get('qty') or 0)}шт</b>.\n"
            "Нажми <b>+1 / -1 / +5 / -5</b> или отправь число (например, <code>+10</code>, <code>-3</code> или <code>24</code>).",
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
        log_activity(uid, username, "OPEN_USERS")
        show_users_and_actions(msg)
        return

    if text == BTN_SUPPORT or low == "поддержка":
        log_activity(uid, username, "OPEN_SUPPORT")
        send_support_link(msg)
        return

    # Текстовый поиск по ключевым словам
    print(f"[TEXT_SEARCH] uid={uid} query={text!r}", flush=True)
    log_activity(uid, username, "SEARCH", text[:120])
    matches = find_media_matches(text)
    if matches:
        for card in matches[:5]:
            safe_send_media_card(msg.chat.id, card, reply_markup=main_menu_keyboard(uid))
        return

    safe_send_text(msg.chat.id, f"Ничего не найдено по запросу «{esc(text)}».", reply_markup=main_menu_keyboard(uid))


# =========================================================
# HTTP HEALTH-CHECK ДЛЯ RENDER
# =========================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
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
# ТОЧКА ВХОДА
# =========================================================
def main() -> None:
    print(f"[NEW BUILD v10.19] Запуск бота (supabase: {SUPABASE_ENABLED}, pil: {PIL_AVAILABLE})...", flush=True)
    load_data()
    threading.Thread(target=start_web_server, daemon=True).start()
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=25)


if __name__ == "__main__":
    main()
