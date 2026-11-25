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
# КНОПКИ И ТЕКСТЫ
# =========================================================
BTN_RECIPES = "🍸 Рецепты"
BTN_DISHWARE = "🥃 Посуда"
BTN_BULK_MEDIA = "📥 Массовая загрузка"
BTN_MEDIA_LIST = "🖼 Карточки"
BTN_LOGS = "📜 Логи"
BTN_USERS = "👥 Пользователи"
BTN_SUPPORT = "💬 Поддержка"
BTN_HELP = "❓ Помощь"
BTN_BACK = "⬅️ Назад"
BTN_DONE = "✅ Готово"
BTN_CANCEL = "❌ Отмена"

DATA_LOCK = threading.RLock()
USER_STATES: dict[int, dict] = {}
LAST_SEEN_SYNC: dict[int, float] = {}

DB: dict = {
    "recipes": [],
    "dishware": [],
    "dishware_logs": [],
    "users": [],
    "support": {"id": "main", "text": "Обратитесь к администратору."},
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
    """Возвращает точное имя файла без расширения (или подпись), без изменений."""
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


def safe_send_text(chat_id: int, text: str, reply_markup=None):
    try:
        return bot.send_message(chat_id, text, reply_markup=reply_markup)
    except Exception as e:
        print(f"[WARN] send_message failed ({chat_id}): {e}", flush=True)
        return None


def safe_edit_text(chat_id: int, message_id: int, text: str, reply_markup=None):
    try:
        return bot.edit_message_text(
            text,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
        )
    except Exception as e:
        print(f"[WARN] edit_message_text failed ({chat_id}/{message_id}): {e}", flush=True)
        return None


def main_menu_keyboard(user_id: int) -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(types.KeyboardButton(BTN_RECIPES), types.KeyboardButton(BTN_DISHWARE))
    kb.row(types.KeyboardButton(BTN_BULK_MEDIA), types.KeyboardButton(BTN_MEDIA_LIST))
    if is_admin(user_id):
        kb.row(types.KeyboardButton(BTN_LOGS), types.KeyboardButton(BTN_USERS))
    kb.row(types.KeyboardButton(BTN_SUPPORT), types.KeyboardButton(BTN_HELP))
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


def load_local_fallback() -> dict:
    fallback = {
        "recipes": [],
        "dishware": [],
        "dishware_logs": [],
        "users": [],
        "support": {"id": "main", "text": "Обратитесь к администратору."},
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

        dishware = sb_select("tk_dishware", "order=name.asc")
        if dishware is not None:
            data["dishware"] = dishware

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
    """Сохраняет файл/PNG с точным именем файла (без расширения) в DB и Supabase."""
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
        # Если карточка с точно таким же title уже есть — обновляем её файл/file_id
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
# ПОИСК И КОМАНДЫ БОТА
# =========================================================
def start_bulk_mode(msg: types.Message) -> None:
    uid = msg.from_user.id
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


@bot.message_handler(commands=["start", "help"])
def cmd_start(msg: types.Message):
    touch_user(msg)
    USER_STATES.pop(msg.from_user.id, None)
    safe_send_text(
        msg.chat.id,
        "Привет! Бот готов к работе.\n"
        "• Напиши любое название — найду в карточках/рецептах/посуде.\n"
        "• Нажми <b>📥 Массовая загрузка</b> или отправь <code>/bulk</code> для загрузки пачки PNG/файлов.",
        reply_markup=main_menu_keyboard(msg.from_user.id),
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
    text = (msg.text or "").strip()
    st = USER_STATES.get(uid) or {}

    if st.get("mode") == "media_bulk_add":
        if text == BTN_DONE:
            finish_bulk_mode(msg)
            return
        if text == BTN_CANCEL:
            USER_STATES.pop(uid, None)
            safe_send_text(msg.chat.id, "❌ Режим массовой загрузки отменён.", reply_markup=main_menu_keyboard(uid))
            return
        safe_send_text(
            msg.chat.id,
            "Сейчас активен режим массовой загрузки. Кидай файлы/PNG или нажми <b>✅ Готово</b>.",
            reply_markup=bulk_done_keyboard(),
        )
        return

    if text == BTN_BULK_MEDIA:
        start_bulk_mode(msg)
        return

    if text == BTN_MEDIA_LIST:
        with DATA_LOCK:
            cards = list(DB.get("media_cards", []))
        if not cards:
            safe_send_text(msg.chat.id, "Сохранённых карточек пока нет.", reply_markup=main_menu_keyboard(uid))
            return
        lines = [f"• {esc(c.get('title') or '')}" for c in cards[:50]]
        safe_send_text(
            msg.chat.id,
            f"🖼 <b>Карточки ({len(cards)}):</b>\n\n" + "\n".join(lines),
            reply_markup=main_menu_keyboard(uid),
        )
        return

    if text == BTN_HELP:
        safe_send_text(
            msg.chat.id,
            "• Нажми <b>📥 Массовая загрузка</b> и отправь файлы PNG — бот запишет точное имя файла без расширения.\n"
            "• Напиши название текстом — бот отправит название и прикреплённый файл/PNG.",
            reply_markup=main_menu_keyboard(uid),
        )
        return

    if text == BTN_SUPPORT:
        with DATA_LOCK:
            sup_text = str((DB.get("support") or {}).get("text") or "Обратитесь к администратору.")
        safe_send_text(msg.chat.id, f"💬 <b>Поддержка:</b>\n{esc(sup_text)}", reply_markup=main_menu_keyboard(uid))
        return

    if text == BTN_RECIPES:
        with DATA_LOCK:
            recipes = list(DB.get("recipes", []))
        if not recipes:
            safe_send_text(msg.chat.id, "Рецептов пока нет.", reply_markup=main_menu_keyboard(uid))
            return
        names = "\n".join(f"• {esc(r.get('name') or r.get('title') or 'Без названия')}" for r in recipes[:50])
        safe_send_text(msg.chat.id, f"🍸 <b>Рецепты:</b>\n\n{names}", reply_markup=main_menu_keyboard(uid))
        return

    if text == BTN_DISHWARE:
        with DATA_LOCK:
            dishware = list(DB.get("dishware", []))
        if not dishware:
            safe_send_text(msg.chat.id, "Посуды пока нет.", reply_markup=main_menu_keyboard(uid))
            return
        lines = [f"• {esc(d.get('name') or '')}: {esc(d.get('qty') or 0)}" for d in dishware[:50]]
        safe_send_text(msg.chat.id, f"🥃 <b>Посуда:</b>\n\n{lines}", reply_markup=main_menu_keyboard(uid))
        return

    # Текстовый поиск по сохранённым карточкам
    print(f"[TEXT_SEARCH] uid={uid} query={text!r}", flush=True)
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
    print(f"[NEW BUILD v10.4] Запуск бота (supabase: {SUPABASE_ENABLED})...", flush=True)
    load_data()
    threading.Thread(target=start_web_server, daemon=True).start()
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=25)


if __name__ == "__main__":
    main()
