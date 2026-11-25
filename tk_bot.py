import copy
import html
import json
import os
import re
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

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в переменных окружения")

ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW.isdigit() else 0
SUPABASE_ENABLED = bool(SUPABASE_URL and SUPABASE_KEY)

DATA_DIR = os.getenv("DATA_DIR", "data")
MEDIA_DIR = os.path.join(DATA_DIR, "user_media")
LOCAL_DB_FILE = os.path.join(DATA_DIR, "tk_bot_state.json")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(MEDIA_DIR, exist_ok=True)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# =========================================================
# КНОПКИ И СПИСКИ
# =========================================================
BTN_RECIPES = "рецепты"
BTN_DISHWARE = "посуда"
BTN_HELP = "помощь"
BTN_USERS = "пользователи"
BTN_SUPPORT = "поддержка"

BTN_REC_AUTHOR = "авторские"
BTN_REC_CLASSIC = "классические"
BTN_REC_INFUSIONS = "наливки"
BTN_REC_MENU = "коктейли меню"

BTN_BULK_MEDIA = "📥 Массовая загрузка"
BTN_DISH_SUMMARY = "📊 Подсчёт остатков"
BTN_BACK = "⬅️ Назад"
BTN_DONE = "✅ Готово"
BTN_CANCEL = "❌ Отмена"

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


def exact_title_from_message(msg: types.Message) -> str | None:
    if msg.content_type == "document" and msg.document and msg.document.file_name:
        stem, _ = os.path.splitext(msg.document.file_name)
        if stem != "":
            return stem
    cap = (msg.caption or "").strip()
    if cap:
        return cap
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
    if is_admin(user_id):
        kb.row(types.KeyboardButton(BTN_BULK_MEDIA))
    kb.row(types.KeyboardButton(BTN_BACK))
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


def dishware_item_keyboard(item_name: str) -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(types.KeyboardButton("+1"), types.KeyboardButton("-1"))
    kb.row(types.KeyboardButton("+5"), types.KeyboardButton("-5"))
    kb.row(types.KeyboardButton(BTN_DISH_SUMMARY), types.KeyboardButton(BTN_BACK))
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
            "first_name": user.first_name or "",
            "last_name": user.last_name or "",
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
# КАРТОЧКИ ФАЙЛОВ / PNG (tk_media_cards)
# =========================================================
def persist_media_card(card: dict) -> None:
    sync_local_from_memory()
    sb_upsert(
        "tk_media_cards",
        {
            "id": str(card["id"]),
            "title": str(card.get("title") or ""),
            "photo_file_id": card.get("photo_file_id"),
            "photo_path": card.get("photo_path"),
            "created_at": str(card.get("created_at") or now_str()),
        },
        "id",
    )


def save_incoming_media_card(msg: types.Message) -> tuple[bool, dict | str]:
    exact_title = exact_title_from_message(msg)
    if not exact_title:
        return False, "Не удалось определить имя файла — отправьте PNG как файл/документ (или добавьте подпись)."

    file_id = None
    ext = ".png"
    original_name = exact_title

    if msg.content_type == "document" and msg.document:
        file_id = msg.document.file_id
        original_name = msg.document.file_name or exact_title
        _, raw_ext = os.path.splitext(original_name)
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
        "photo_file_id": file_id,
        "photo_path": local_path,
        "created_at": now_str(),
    }

    with DATA_LOCK:
        cards = DB.setdefault("media_cards", [])
        existing = next((c for c in cards if str(c.get("title") or "") == exact_title), None)
        if existing:
            existing["photo_file_id"] = card["photo_file_id"]
            existing["photo_path"] = card["photo_path"]
            existing["created_at"] = card["created_at"]
            card = existing
        else:
            cards.append(card)
        persist_media_card(card)

    return True, card


def safe_send_media_card(chat_id: int, card: dict, reply_markup=None) -> None:
    title_raw = str(card.get("title") or "")
    caption = f"<b>{esc(title_raw)}</b>"
    file_id = card.get("photo_file_id")
    photo_path = card.get("photo_path")

    if file_id:
        try:
            bot.send_document(chat_id, file_id, caption=caption, reply_markup=reply_markup)
            return
        except Exception:
            try:
                bot.send_photo(chat_id, file_id, caption=caption, reply_markup=reply_markup)
                return
            except Exception as e:
                print(f"[WARN] send media by file_id failed: {e}", flush=True)

    if photo_path and os.path.isfile(photo_path):
        try:
            with open(photo_path, "rb") as f:
                bot.send_document(chat_id, f, caption=caption, reply_markup=reply_markup)
            return
        except Exception as e:
            print(f"[WARN] send media from disk failed: {e}", flush=True)

    safe_send_text(chat_id, caption, reply_markup=reply_markup)


def find_media_matches(query: str) -> list[dict]:
    q = norm_text(query)
    if not q:
        return []
    with DATA_LOCK:
        cards = list(DB.get("media_cards", []))
    exact = [c for c in cards if norm_text(c.get("title")) == q]
    if exact:
        return exact
    return [c for c in cards if q in norm_text(c.get("title"))]


# =========================================================
# РЕЦЕПТЫ, ПОМОЩЬ, ПОЛЬЗОВАТЕЛИ, ПОДДЕРЖКА
# =========================================================
def show_recipe_category(msg: types.Message, category_key: str) -> None:
    uid = msg.from_user.id
    with DATA_LOCK:
        recipes = [
            r for r in DB.get("recipes", [])
            if norm_text(r.get("category") or r.get("type") or "") == norm_text(category_key)
        ]
    if not recipes:
        safe_send_text(
            msg.chat.id,
            f"Раздел <b>{esc(category_key)}</b> пока пуст.",
            reply_markup=recipes_menu_keyboard(uid),
        )
        return
    lines = [f"• <b>{esc(r.get('name') or r.get('title') or 'Без названия')}</b>" for r in recipes[:40]]
    safe_send_text(
        msg.chat.id,
        f"🍸 <b>{esc(category_key)}</b> ({len(recipes)}):\n\n" + "\n".join(lines),
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
        name = u.get("first_name") or ""
        user_lines.append(
            f"• {esc(name)} ({esc(uname)}, id <code>{esc(u.get('user_id'))}</code>) — {esc(u.get('last_seen') or '')}"
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
        f"💛 <b>Поддержка проекта</b>\nСсылка: {esc(url)}",
        reply_markup=kb,
    )
    safe_send_text(msg.chat.id, "Нажми кнопку выше или выбери раздел:", reply_markup=main_menu_keyboard(uid))


def start_bulk_mode(msg: types.Message) -> None:
    uid = msg.from_user.id
    if not is_admin(uid):
        safe_send_text(msg.chat.id, "Недостаточно прав.", reply_markup=main_menu_keyboard(uid))
        return
    USER_STATES[uid] = {"mode": "media_bulk_add", "count": 0, "titles": []}
    safe_send_text(
        msg.chat.id,
        "📥 <b>Режим массовой загрузки файлов/PNG включён.</b>\n\n"
        "Отправляй файлы подряд (например, 10+ PNG). Название запишется в базу "
        "<b>один в один</b> как имя файла без расширения (например, файл <code>коктейль Алексей.png</code> → "
        "название <code>коктейль Алексей</code>).\n\n"
        "Когда закончишь — нажми <b>✅ Готово</b>.",
        reply_markup=bulk_done_keyboard(),
    )


def finish_bulk_mode(msg: types.Message) -> None:
    uid = msg.from_user.id
    st = USER_STATES.pop(uid, {})
    count = int(st.get("count") or 0)
    titles = st.get("titles") or []
    preview = "\n".join(f"• {esc(t)}" for t in titles[:20])
    extra = f"\n…и ещё {len(titles) - 20}" if len(titles) > 20 else ""
    safe_send_text(
        msg.chat.id,
        f"✅ Загрузка завершена. Сохранено файлов: <b>{count}</b>"
        + (f"\n\n{preview}{extra}" if preview else ""),
        reply_markup=main_menu_keyboard(uid),
    )


# =========================================================
# ОБРАБОТЧИКИ TELEGRAM
# =========================================================
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
    start_bulk_mode(msg)


@bot.message_handler(content_types=["document", "photo"])
def handle_media_upload(msg: types.Message):
    touch_user(msg)
    uid = msg.from_user.id
    st = USER_STATES.get(uid) or {}

    ok, res = save_incoming_media_card(msg)
    if not ok:
        safe_send_text(msg.chat.id, f"⚠️ {esc(res)}")
        return

    card = res
    title = str(card.get("title") or "")
    log_activity(uid, msg.from_user.username or "", "MEDIA_ADD", title)

    if st.get("mode") == "media_bulk_add":
        st["count"] = int(st.get("count") or 0) + 1
        st.setdefault("titles", []).append(title)
        safe_send_text(
            msg.chat.id,
            f"➕ Сохранено (#{st['count']}): <b>{esc(title)}</b>",
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

    if st.get("mode") == "media_bulk_add":
        if text == BTN_DONE:
            finish_bulk_mode(msg)
            return
        if text == BTN_CANCEL:
            USER_STATES.pop(uid, None)
            safe_send_text(msg.chat.id, "❌ Загрузка отменена.", reply_markup=main_menu_keyboard(uid))
            return
        safe_send_text(
            msg.chat.id,
            "Сейчас активен режим массовой загрузки. Кидай файлы/PNG или нажми <b>✅ Готово</b>.",
            reply_markup=bulk_done_keyboard(),
        )
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
            if aid != uid:
                safe_send_text(aid, report)
        if uid in admins:
            safe_send_text(uid, report)
        safe_send_text(
            msg.chat.id,
            "✅ Запрос отправлен администратору. Спасибо!",
            reply_markup=main_menu_keyboard(uid),
        )
        return

    if st.get("mode") == "dishware_item":
        item_id = st.get("item_id")
        if text == BTN_BACK:
            USER_STATES.pop(uid, None)
            safe_send_text(msg.chat.id, "Раздел «посуда»:", reply_markup=dishware_menu_keyboard(uid))
            return
        if text == BTN_DISH_SUMMARY:
            safe_send_text(msg.chat.id, format_dishware_summary(), reply_markup=dishware_item_keyboard(st.get("name") or ""))
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
                st["name"] = updated.get("name") or ""
                safe_send_text(
                    msg.chat.id,
                    f"🥃 <b>{esc(updated.get('name'))}</b>: <b>{int(updated.get('qty') or 0)}шт</b>",
                    reply_markup=dishware_item_keyboard(st["name"]),
                )
            return

    if text == BTN_BACK:
        USER_STATES.pop(uid, None)
        safe_send_text(msg.chat.id, "Главное меню:", reply_markup=main_menu_keyboard(uid))
        return

    if text == BTN_BULK_MEDIA:
        start_bulk_mode(msg)
        return

    if low == BTN_RECIPES:
        log_activity(uid, username, "OPEN_RECIPES")
        safe_send_text(msg.chat.id, "🍸 Выбери раздел рецептов:", reply_markup=recipes_menu_keyboard(uid))
        return

    if low in (BTN_REC_AUTHOR, BTN_REC_CLASSIC, BTN_REC_INFUSIONS, BTN_REC_MENU):
        log_activity(uid, username, "RECIPES_CAT", low)
        show_recipe_category(msg, low)
        return

    if low == BTN_DISHWARE:
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
            reply_markup=dishware_item_keyboard(matched_dish.get("name") or ""),
        )
        return

    if low == BTN_HELP:
        USER_STATES[uid] = {"mode": "help_wait_text"}
        safe_send_text(
            msg.chat.id,
            "🆘 Опиши проблему текстом и отправь сообщение — я передам его администратору.",
            reply_markup=back_only_keyboard(),
        )
        return

    if low == BTN_USERS:
        log_activity(uid, username, "OPEN_USERS")
        show_users_and_actions(msg)
        return

    if low == BTN_SUPPORT:
        log_activity(uid, username, "OPEN_SUPPORT")
        send_support_link(msg)
        return

    # Текстовый поиск по сохранённым карточкам
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
    print(f"[NEW BUILD v10.6] Запуск бота (supabase: {SUPABASE_ENABLED})...", flush=True)
    load_data()
    threading.Thread(target=start_web_server, daemon=True).start()
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=25)


if __name__ == "__main__":
    main()
