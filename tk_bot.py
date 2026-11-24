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
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import telebot
from telebot import types

DEFAULT_BOT_TOKEN = "8797743243:AAHw8tDYRr-i9VPlXmjAThnVOmvcJUOx3vA"
BOT_TOKEN = os.environ.get("BOT_TOKEN", DEFAULT_BOT_TOKEN).strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", "5393257255"))
PORT = int(os.environ.get("PORT", "10000"))

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
if SUPABASE_URL.endswith("/rest/v1"):
    SUPABASE_URL = SUPABASE_URL[: -len("/rest/v1")]
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MEDIA_DIR = os.path.join(BASE_DIR, "user_media")
DATA_FILE = os.path.join(BASE_DIR, "bot_data.json")

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN")

os.makedirs(MEDIA_DIR, exist_ok=True)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
DATA_LOCK = threading.RLock()
STATE_LOCK = threading.RLock()
FILE_LOCK = threading.RLock()

USER_STATES = {}
LAST_SYNC_TS = 0.0

RECIPE_SECTIONS = {
    "author": "Авторские",
    "classic": "Классические",
    "menu_cocktails": "Коктейли меню",
    "tinctures": "Наливки",
}

ALL_SECTION_TITLES = {
    **RECIPE_SECTIONS,
    "dishware": "Посуда",
    "users": "Пользователи",
    "support": "Поддержать проект",
}

BTN_AUTHOR = "🍸 Авторские"
BTN_CLASSIC = "🥃 Классические"
BTN_MENU_COCKTAILS = "📋 Коктейли меню"
BTN_TINCTURES = "🫙 Наливки"
BTN_DISHWARE = "🥂 Посуда"
BTN_USERS = "👥 Пользователи"
BTN_SUPPORT = "💛 Поддержать проект"

BTN_TO_SECTION = {
    BTN_AUTHOR: "author",
    BTN_CLASSIC: "classic",
    BTN_MENU_COCKTAILS: "menu_cocktails",
    BTN_TINCTURES: "tinctures",
    BTN_DISHWARE: "dishware",
    BTN_USERS: "users",
    BTN_SUPPORT: "support",
}


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return


def start_health_server() -> None:
    def _serve():
        try:
            server = ThreadingHTTPServer(("0.0.0.0", PORT), _HealthHandler)
            print(f"[WEB] Listening on port {PORT}", flush=True)
            server.serve_forever()
        except Exception as e:
            print(f"[WARN] health server error: {e}", flush=True)

    threading.Thread(target=_serve, daemon=True).start()


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def is_admin(user_id: int) -> bool:
    return int(user_id) == ADMIN_ID


def esc(val) -> str:
    return html.escape("" if val is None else str(val))


def sb_enabled() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY)


def sb_request(method: str, path: str, payload=None, prefer: str | None = None):
    if not sb_enabled():
        return None
    url = f"{SUPABASE_URL}/rest/v1/{path.lstrip('/')}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else []
    except urllib.error.HTTPError as e:
        err_txt = ""
        try:
            err_txt = e.read().decode("utf-8")[:250]
        except Exception:
            pass
        print(f"[WARN] Supabase {method} {path} HTTP {e.code}: {err_txt}", flush=True)
        return None
    except Exception as e:
        print(f"[WARN] Supabase {method} {path} error: {e}", flush=True)
        return None


def sb_select(table: str, query_params: str = "") -> list[dict] | None:
    q = f"?{query_params.lstrip('&')}" if query_params else ""
    res = sb_request("GET", f"{table}{q}")
    return res if isinstance(res, list) else None


def sb_upsert(table: str, rows: list[dict] | dict, on_conflict: str | None = None) -> None:
    if not sb_enabled():
        return
    path = f"{table}?on_conflict={urllib.parse.quote(on_conflict)}" if on_conflict else table
    sb_request("POST", path, payload=rows, prefer="resolution=merge-duplicates,return=minimal")


def sb_insert(table: str, rows: list[dict] | dict) -> None:
    if not sb_enabled():
        return
    sb_request("POST", table, payload=rows, prefer="return=minimal")


def sb_delete(table: str, filter_query: str) -> None:
    if not sb_enabled():
        return
    sb_request("DELETE", f"{table}?{filter_query.lstrip('&')}", prefer="return=minimal")


def default_data() -> dict:
    return {
        "recipes": {
            "author": [],
            "classic": [],
            "menu_cocktails": [],
            "tinctures": [],
        },
        "dishware": [],
        "dishware_logs": [],
        "support": {
            "text": "Поддержать проект можно по реквизитам, которые администратор укажет в разделе редактирования."
        },
        "users": {},
        "activity_logs": [],
    }


def save_local_snapshot(snapshot: dict) -> None:
    with FILE_LOCK:
        try:
            tmp = f"{DATA_FILE}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
            os.replace(tmp, DATA_FILE)
        except Exception as e:
            print(f"[WARN] save_local_snapshot error: {e}", flush=True)


def sync_local_from_memory() -> None:
    with DATA_LOCK:
        snap = copy.deepcopy(DB)
    save_local_snapshot(snap)


def load_local_snapshot() -> dict:
    data = default_data()
    if not os.path.isfile(DATA_FILE):
        return data
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return data
        for sec in RECIPE_SECTIONS:
            items = raw.get("recipes", {}).get(sec, [])
            if isinstance(items, list):
                data["recipes"][sec] = items
        if isinstance(raw.get("dishware"), list):
            data["dishware"] = raw["dishware"]
        if isinstance(raw.get("dishware_logs"), list):
            data["dishware_logs"] = raw["dishware_logs"]
        if isinstance(raw.get("support"), dict) and isinstance(raw["support"].get("text"), str):
            data["support"]["text"] = raw["support"]["text"]
        if isinstance(raw.get("users"), dict):
            data["users"] = raw["users"]
        if isinstance(raw.get("activity_logs"), list):
            data["activity_logs"] = raw["activity_logs"]
    except Exception as e:
        print(f"[WARN] load_local_snapshot error: {e}", flush=True)
    return data


def load_data() -> dict:
    global LAST_SYNC_TS
    with DATA_LOCK:
        data = load_local_snapshot()
        if not sb_enabled():
            return data

        recipe_rows = sb_select("tk_recipes", "order=created_at.asc")
        if recipe_rows is not None:
            data["recipes"] = {k: [] for k in RECIPE_SECTIONS}
            for r in recipe_rows:
                sec = r.get("section")
                if sec not in data["recipes"]:
                    # Если раздел написан по-русски (например "Коктейли" при ручном INSERT),
                    # кладём в classic, чтобы рецепт всё равно отображался и находился
                    sec_lower = str(sec or "").strip().lower()
                    sec_map = {
                        "авторские": "author",
                        "классические": "classic",
                        "коктейли": "classic",
                        "коктейли меню": "menu_cocktails",
                        "наливки": "tinctures",
                    }
                    sec = sec_map.get(sec_lower, "classic")
                data["recipes"][sec].append({
                    "id": str(r.get("id")),
                    "title": str(r.get("title") or "Без названия"),
                    "ingredients": str(r.get("ingredients") or ""),
                    "method": str(r.get("method") or ""),
                    "photo_path": r.get("photo_path"),
                    "photo_file_id": r.get("photo_file_id"),
                    "created_at": str(r.get("created_at") or now_str()),
                })

        dish_rows = sb_select("tk_dishware", "order=name.asc")
        if dish_rows is not None:
            data["dishware"] = [
                {
                    "id": str(d.get("id")),
                    "name": str(d.get("name") or "Посуда"),
                    "qty": int(d.get("qty") or 0),
                    "unit": str(d.get("unit") or "шт."),
                    "note": str(d.get("note") or ""),
                }
                for d in dish_rows
            ]

        dlog_rows = sb_select("tk_dishware_logs", "order=id.desc&limit=300")
        if dlog_rows is not None:
            data["dishware_logs"] = list(reversed(dlog_rows))

        sup_rows = sb_select("tk_support", "id=eq.main&limit=1")
        if sup_rows and isinstance(sup_rows[0].get("text"), str):
            data["support"]["text"] = sup_rows[0]["text"]

        user_rows = sb_select("tk_users", "order=last_seen.desc")
        if user_rows is not None:
            for u in user_rows:
                uid_str = str(u.get("uid") or u.get("id"))
                data["users"][uid_str] = {
                    "id": int(u.get("id") or 0),
                    "username": str(u.get("username") or "—"),
                    "name": str(u.get("name") or "Без имени"),
                    "first_seen": str(u.get("first_seen") or ""),
                    "last_seen": str(u.get("last_seen") or ""),
                    "actions_count": int(u.get("actions_count") or 0),
                }

        act_rows = sb_select("tk_activity_logs", "order=id.desc&limit=800")
        if act_rows is not None:
            data["activity_logs"] = list(reversed(act_rows))

        LAST_SYNC_TS = time.time()
        save_local_snapshot(data)
        return data


DB = load_data()


def refresh_from_supabase_if_needed(force: bool = False) -> None:
    global DB
    if not sb_enabled():
        return
    if force or (time.time() - LAST_SYNC_TS > 15):
        fresh = load_data()
        with DATA_LOCK:
            DB = fresh


def set_state(user_id: int, state: dict | None) -> None:
    with STATE_LOCK:
        if state is None:
            USER_STATES.pop(user_id, None)
        else:
            USER_STATES[user_id] = state


def get_state(user_id: int) -> dict | None:
    with STATE_LOCK:
        st = USER_STATES.get(user_id)
        return copy.deepcopy(st) if st is not None else None


def persist_recipe(sec: str, item: dict) -> None:
    sync_local_from_memory()
    row = {
        "id": str(item["id"]),
        "section": sec,
        "title": str(item.get("title") or ""),
        "ingredients": str(item.get("ingredients") or ""),
        "method": str(item.get("method") or ""),
        "photo_path": item.get("photo_path"),
        "photo_file_id": item.get("photo_file_id"),
        "created_at": str(item.get("created_at") or now_str()),
    }
    sb_upsert("tk_recipes", row, "id")


def delete_recipe_remote(item_id: str) -> None:
    sync_local_from_memory()
    sb_delete("tk_recipes", f"id=eq.{urllib.parse.quote(str(item_id))}")


def persist_dishware(item: dict, log_entry: dict | None = None) -> None:
    sync_local_from_memory()
    row = {
        "id": str(item["id"]),
        "name": str(item.get("name") or ""),
        "qty": int(item.get("qty") or 0),
        "unit": str(item.get("unit") or "шт."),
        "note": str(item.get("note") or ""),
    }
    sb_upsert("tk_dishware", row, "id")
    if log_entry:
        sb_insert("tk_dishware_logs", {
            "ts": str(log_entry.get("ts") or now_str()),
            "name": str(log_entry.get("name") or ""),
            "delta": int(log_entry.get("delta") or 0),
            "after": int(log_entry.get("after") or 0),
            "reason": str(log_entry.get("reason") or ""),
        })


def delete_dishware_remote(item_id: str) -> None:
    sync_local_from_memory()
    sb_delete("tk_dishware", f"id=eq.{urllib.parse.quote(str(item_id))}")


def persist_support(text_val: str) -> None:
    sync_local_from_memory()
    sb_upsert("tk_support", {"id": "main", "text": str(text_val)}, "id")


def track_user_and_action(user, action_text: str) -> None:
    if not user:
        return
    uid = str(user.id)
    username = f"@{user.username}" if getattr(user, "username", None) else "—"
    first_name = (getattr(user, "first_name", "") or "").strip()
    last_name = (getattr(user, "last_name", "") or "").strip()
    full_name = f"{first_name} {last_name}".strip() or "Без имени"
    ts = now_str()

    with DATA_LOCK:
        u = DB["users"].get(uid)
        if not u:
            u = {
                "id": user.id,
                "username": username,
                "name": full_name,
                "first_seen": ts,
                "last_seen": ts,
                "actions_count": 0,
            }
        u["username"] = username
        u["name"] = full_name
        u["last_seen"] = ts
        u["actions_count"] = int(u.get("actions_count", 0)) + 1
        DB["users"][uid] = copy.deepcopy(u)

        ev = {
            "ts": ts,
            "user_id": user.id,
            "username": username,
            "name": full_name,
            "action": action_text,
        }
        DB["activity_logs"].append(ev)
        if len(DB["activity_logs"]) > 800:
            DB["activity_logs"] = DB["activity_logs"][-800:]

    sync_local_from_memory()


def search_entries(raw_query: str) -> list[dict]:
    refresh_from_supabase_if_needed()
    cleaned = re.sub(r"\s+", " ", (raw_query or "").strip().lower())
    without_word = re.sub(r"^(коктейль|коктейли|рецепт|наливка|наливки)\s+", "", cleaned).strip()
    target = without_word if without_word else cleaned
    if not target:
        return []

    parts = [p for p in target.split(" ") if p]
    results = []

    with DATA_LOCK:
        for sec, sec_title in RECIPE_SECTIONS.items():
            for item in DB["recipes"].get(sec, []):
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or "Без названия")
                ingredients = str(item.get("ingredients") or "")
                method = str(item.get("method") or "")
                hay = f"{title} {ingredients} {method}".lower()
                if target in hay or all(p in hay for p in parts):
                    results.append({
                        "kind": "recipe",
                        "section": sec,
                        "section_title": sec_title,
                        "id": str(item.get("id")),
                        "title": title,
                    })

        for d in DB.get("dishware", []):
            if not isinstance(d, dict):
                continue
            name = str(d.get("name") or "Посуда")
            note = str(d.get("note") or "")
            qty = int(d.get("qty") or 0)
            unit = str(d.get("unit") or "шт.")
            hay = f"{name} {note}".lower()
            if target in hay or all(p in hay for p in parts):
                results.append({
                    "kind": "dishware",
                    "section": "dishware",
                    "section_title": "Посуда",
                    "id": str(d.get("id")),
                    "title": f"{name} ({qty} {unit})",
                })

    return results[:15]


def safe_send_text(chat_id: int, text: str, reply_markup=None) -> None:
    try:
        bot.send_message(chat_id, text, reply_markup=reply_markup, disable_web_page_preview=True)
    except Exception:
        plain = html.unescape(text)
        try:
            bot.send_message(chat_id, plain, reply_markup=reply_markup, parse_mode=None, disable_web_page_preview=True)
        except Exception as e:
            print(f"[ERROR] safe_send_text: {e}", flush=True)


def safe_edit_text(chat_id: int, message_id: int, text: str, reply_markup=None) -> None:
    try:
        bot.edit_message_text(
            text,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except Exception:
        safe_send_text(chat_id, text, reply_markup=reply_markup)


def safe_send_recipe(chat_id: int, item: dict, reply_markup=None) -> None:
    title = esc(item.get("title", "Без названия"))
    ingredients = esc(item.get("ingredients", ""))
    method = esc(item.get("method", ""))

    lines = [f"<b>{title}</b>"]
    if item.get("ingredients"):
        lines.append(f"\n<b>Состав:</b>\n{ingredients}")
    if item.get("method"):
        lines.append(f"\n<b>Приготовление:</b>\n{method}")
    caption = "\n".join(lines)

    file_id = item.get("photo_file_id")
    photo_path = item.get("photo_path")

    if file_id:
        try:
            if len(caption) <= 1024:
                bot.send_photo(chat_id, file_id, caption=caption, reply_markup=reply_markup)
            else:
                bot.send_photo(chat_id, file_id)
                safe_send_text(chat_id, caption, reply_markup=reply_markup)
            return
        except Exception as e:
            print(f"[WARN] send_photo by file_id failed: {e}", flush=True)

    if photo_path and os.path.isfile(photo_path):
        try:
            with open(photo_path, "rb") as img:
                if len(caption) <= 1024:
                    sent = bot.send_photo(chat_id, img, caption=caption, reply_markup=reply_markup)
                else:
                    sent = bot.send_photo(chat_id, img)
                    safe_send_text(chat_id, caption, reply_markup=reply_markup)
            if sent and sent.photo:
                item["photo_file_id"] = sent.photo[-1].file_id
                sync_local_from_memory()
            return
        except Exception as e:
            print(f"[WARN] send_photo failed: {e}", flush=True)

    safe_send_text(chat_id, caption, reply_markup=reply_markup)


def main_menu_keyboard(user_id: int) -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(types.KeyboardButton(BTN_AUTHOR), types.KeyboardButton(BTN_CLASSIC))
    kb.row(types.KeyboardButton(BTN_MENU_COCKTAILS), types.KeyboardButton(BTN_TINCTURES))
    kb.row(types.KeyboardButton(BTN_DISHWARE), types.KeyboardButton(BTN_SUPPORT))
    if is_admin(user_id):
        kb.row(types.KeyboardButton(BTN_USERS))
    return kb


def cancel_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(types.KeyboardButton("❌ Отмена"))
    return kb


def skip_or_cancel_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(types.KeyboardButton("⏭ Пропустить"), types.KeyboardButton("❌ Отмена"))
    return kb


def recipe_section_inline(section: str, user_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    with DATA_LOCK:
        items = list(DB["recipes"].get(section, []))
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or f"idx_{idx}")
        title = str(item.get("title") or "Без названия")
        kb.add(types.InlineKeyboardButton(text=f"📄 {title[:60]}", callback_data=f"r_view:{section}:{item_id}"))

    if is_admin(user_id):
        kb.row(
            types.InlineKeyboardButton("➕ Добавить", callback_data=f"r_add:{section}"),
            types.InlineKeyboardButton("🗑 Удалить", callback_data=f"r_del_pick:{section}"),
        )
    return kb


def recipe_item_inline(section: str, item_id: str, user_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    if is_admin(user_id):
        kb.add(
            types.InlineKeyboardButton("✏️ Изменить", callback_data=f"r_edit_menu:{section}:{item_id}"),
            types.InlineKeyboardButton("🗑 Удалить", callback_data=f"r_del_ask:{section}:{item_id}"),
        )
    kb.add(types.InlineKeyboardButton("⬅️ Назад в раздел", callback_data=f"sec_open:{section}"))
    return kb


def recipe_edit_fields_inline(section: str, item_id: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("Название", callback_data=f"r_ef:{section}:{item_id}:title"),
        types.InlineKeyboardButton("Состав", callback_data=f"r_ef:{section}:{item_id}:ingredients"),
        types.InlineKeyboardButton("Приготовление", callback_data=f"r_ef:{section}:{item_id}:method"),
        types.InlineKeyboardButton("Картинка", callback_data=f"r_ef:{section}:{item_id}:photo"),
    )
    kb.add(types.InlineKeyboardButton("⬅️ К карточке", callback_data=f"r_view:{section}:{item_id}"))
    return kb


def dishware_list_inline(user_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    with DATA_LOCK:
        items = list(DB["dishware"])
    for d in items:
        qty = int(d.get("qty", 0))
        unit = d.get("unit") or "шт."
        kb.add(types.InlineKeyboardButton(text=f"🥂 {d.get('name', 'Посуда')} — {qty} {unit}", callback_data=f"d_view:{d['id']}"))
    if is_admin(user_id):
        kb.row(types.InlineKeyboardButton("➕ Добавить посуду", callback_data="d_add"))
    return kb


def dishware_item_inline(item_id: str, user_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    if is_admin(user_id):
        kb.row(
            types.InlineKeyboardButton("➖ 1", callback_data=f"d_delta:{item_id}:-1"),
            types.InlineKeyboardButton("➕ 1", callback_data=f"d_delta:{item_id}:1"),
        )
        kb.row(
            types.InlineKeyboardButton("✏️ Задать число", callback_data=f"d_set:{item_id}"),
            types.InlineKeyboardButton("🗑 Удалить", callback_data=f"d_del_ask:{item_id}"),
        )
    kb.add(types.InlineKeyboardButton("⬅️ К списку посуды", callback_data="sec_open:dishware"))
    return kb


def render_recipe_section(chat_id: int, user_id: int, section: str, edit_msg_id: int | None = None) -> None:
    refresh_from_supabase_if_needed()
    title = RECIPE_SECTIONS[section]
    with DATA_LOCK:
        count = len(DB["recipes"].get(section, []))
    text = f"<b>{esc(title)}</b>\nПозиций в разделе: <b>{count}</b>\nНажмите карточку или кнопку управления:"
    kb = recipe_section_inline(section, user_id)
    if edit_msg_id:
        safe_edit_text(chat_id, edit_msg_id, text, reply_markup=kb)
    else:
        safe_send_text(chat_id, text, reply_markup=kb)


def render_dishware_section(chat_id: int, user_id: int, edit_msg_id: int | None = None) -> None:
    refresh_from_supabase_if_needed()
    with DATA_LOCK:
        items = list(DB["dishware"])
    if not items:
        text = "<b>🥂 Посуда</b>\n\nСписок пока пуст."
    else:
        lines = ["<b>🥂 Посуда:</b>\n"]
        for d in items:
            lines.append(f"• <b>{esc(d.get('name', 'Посуда'))}</b>: <b>{int(d.get('qty', 0))}</b> {esc(d.get('unit') or 'шт.')}")
        text = "\n".join(lines)
    kb = dishware_list_inline(user_id)
    if edit_msg_id:
        safe_edit_text(chat_id, edit_msg_id, text, reply_markup=kb)
    else:
        safe_send_text(chat_id, text, reply_markup=kb)


def render_dishware_item(chat_id: int, user_id: int, item_id: str, edit_msg_id: int | None = None) -> None:
    with DATA_LOCK:
        item = next((x for x in DB["dishware"] if x["id"] == item_id), None)
    if not item:
        safe_send_text(chat_id, "Позиция не найдена.", reply_markup=main_menu_keyboard(user_id))
        return
    text = f"<b>🥂 {esc(item.get('name', 'Посуда'))}</b>\nОстаток: <b>{int(item.get('qty', 0))}</b> {esc(item.get('unit') or 'шт.')}"
    kb = dishware_item_inline(item_id, user_id)
    if edit_msg_id:
        safe_edit_text(chat_id, edit_msg_id, text, reply_markup=kb)
    else:
        safe_send_text(chat_id, text, reply_markup=kb)


def render_support_section(chat_id: int, user_id: int, edit_msg_id: int | None = None) -> None:
    with DATA_LOCK:
        raw_text = DB["support"].get("text", "")
    kb = None
    if is_admin(user_id):
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("✏️ Редактировать текст", callback_data="sup_edit"))
    text = f"<b>💛 Поддержать проект</b>\n\n{esc(raw_text)}"
    if edit_msg_id:
        safe_edit_text(chat_id, edit_msg_id, text, reply_markup=kb)
    else:
        safe_send_text(chat_id, text, reply_markup=kb)


def render_users_section(chat_id: int, user_id: int, edit_msg_id: int | None = None) -> None:
    if not is_admin(user_id):
        return
    with DATA_LOCK:
        users = list(DB["users"].values())
    users.sort(key=lambda x: x.get("last_seen", ""), reverse=True)
    lines = [f"<b>👥 Пользователи ({len(users)})</b>\n"]
    for u in users[:25]:
        lines.append(f"• <b>{esc(u.get('name'))}</b> ({esc(u.get('username'))}) — {int(u.get('actions_count', 0))} действ.")
    text = "\n".join(lines)
    if edit_msg_id:
        safe_edit_text(chat_id, edit_msg_id, text)
    else:
        safe_send_text(chat_id, text)


def open_section_by_key(chat_id: int, user_id: int, section: str, edit_msg_id: int | None = None) -> None:
    if section in RECIPE_SECTIONS:
        render_recipe_section(chat_id, user_id, section, edit_msg_id=edit_msg_id)
    elif section == "dishware":
        render_dishware_section(chat_id, user_id, edit_msg_id=edit_msg_id)
    elif section == "support":
        render_support_section(chat_id, user_id, edit_msg_id=edit_msg_id)
    elif section == "users":
        render_users_section(chat_id, user_id, edit_msg_id=edit_msg_id)


def extract_media(msg: types.Message) -> tuple[bytes | None, str | None, str | None]:
    if msg.content_type == "photo" and msg.photo:
        best = msg.photo[-1]
        info = bot.get_file(best.file_id)
        return bot.download_file(info.file_path), ".jpg", best.file_id
    if msg.content_type == "document" and msg.document:
        doc = msg.document
        ext = os.path.splitext(doc.file_name or "")[1].lower() or ".jpg"
        if ext in (".png", ".jpg", ".jpeg", ".webp") or (doc.mime_type or "").startswith("image/"):
            info = bot.get_file(doc.file_id)
            raw = bot.download_file(info.file_path)
            sent = bot.send_photo(msg.chat.id, raw)
            fid = sent.photo[-1].file_id if sent and sent.photo else None
            return raw, ext, fid
    return None, None, None


@bot.message_handler(commands=["start", "menu", "cancel"])
def cmd_start_or_cancel(message: types.Message):
    uid = message.from_user.id
    set_state(uid, None)
    refresh_from_supabase_if_needed(force=True)
    track_user_and_action(message.from_user, "start/menu")
    safe_send_text(
        message.chat.id,
        "<b>Главное меню</b>\nВыберите раздел или напишите слово для поиска (например: <code>негрони</code>):",
        reply_markup=main_menu_keyboard(uid),
    )


@bot.message_handler(func=lambda m: m.text == "❌ Отмена")
def btn_cancel(message: types.Message):
    uid = message.from_user.id
    st = get_state(uid)
    set_state(uid, None)
    safe_send_text(message.chat.id, "Отменено.", reply_markup=main_menu_keyboard(uid))
    if st and st.get("return_section"):
        open_section_by_key(message.chat.id, uid, st["return_section"])


@bot.message_handler(func=lambda m: m.text in BTN_TO_SECTION)
def handle_main_menu_buttons(message: types.Message):
    uid = message.from_user.id
    sec = BTN_TO_SECTION[message.text]
    if sec == "users" and not is_admin(uid):
        return
    set_state(uid, None)
    track_user_and_action(message.from_user, f"раздел {ALL_SECTION_TITLES.get(sec, sec)}")
    open_section_by_key(message.chat.id, uid, sec)


@bot.message_handler(content_types=["text", "photo", "document"], func=lambda m: get_state(m.from_user.id) is not None)
def handle_wizard_input(message: types.Message):
    uid = message.from_user.id
    if not is_admin(uid):
        set_state(uid, None)
        return
    st = get_state(uid)
    if not st:
        return

    mode = st.get("mode")
    step = st.get("step")
    data = st.get("data", {})
    txt = (message.text or "").strip()

    if mode == "recipe_add":
        sec = data["section"]
        if step == "title":
            if not txt:
                safe_send_text(message.chat.id, "Введите название:")
                return
            data["title"] = txt
            set_state(uid, {"mode": mode, "step": "ingredients", "data": data, "return_section": sec})
            safe_send_text(message.chat.id, "Введите состав (или <code>-</code>):")
            return
        if step == "ingredients":
            data["ingredients"] = "" if txt == "-" else txt
            set_state(uid, {"mode": mode, "step": "method", "data": data, "return_section": sec})
            safe_send_text(message.chat.id, "Введите приготовление (или <code>-</code>):")
            return
        if step == "method":
            data["method"] = "" if txt == "-" else txt
            set_state(uid, {"mode": mode, "step": "photo", "data": data, "return_section": sec})
            safe_send_text(message.chat.id, "Отправьте картинку или нажмите «⏭ Пропустить»:", reply_markup=skip_or_cancel_keyboard())
            return
        if step == "photo":
            photo_path = None
            file_id = None
            item_id = uuid.uuid4().hex[:8]
            raw_bytes, ext, file_id = extract_media(message)
            if raw_bytes:
                safe_ext = ext if ext in (".png", ".jpg", ".jpeg", ".webp") else ".jpg"
                photo_path = os.path.join(MEDIA_DIR, f"{sec}_{item_id}{safe_ext}")
                with open(photo_path, "wb") as f:
                    f.write(raw_bytes)
            elif txt != "⏭ Пропустить":
                safe_send_text(message.chat.id, "Пришлите картинку или нажмите «⏭ Пропустить».", reply_markup=skip_or_cancel_keyboard())
                return

            new_item = {
                "id": item_id,
                "title": data["title"],
                "ingredients": data.get("ingredients", ""),
                "method": data.get("method", ""),
                "photo_path": photo_path,
                "photo_file_id": file_id,
                "created_at": now_str(),
            }
            with DATA_LOCK:
                DB["recipes"].setdefault(sec, []).append(new_item)
                persist_recipe(sec, new_item)
            set_state(uid, None)
            safe_send_text(message.chat.id, f"✅ Добавлено: <b>{esc(new_item['title'])}</b>", reply_markup=main_menu_keyboard(uid))
            render_recipe_section(message.chat.id, uid, sec)
            return

    if mode == "recipe_edit_field":
        sec = data["section"]
        item_id = data["item_id"]
        field = data["field"]
        if field == "photo":
            raw_bytes, ext, file_id = extract_media(message)
            if txt == "⏭ Пропустить":
                new_photo, new_file_id = None, None
            elif raw_bytes:
                safe_ext = ext if ext in (".png", ".jpg", ".jpeg", ".webp") else ".jpg"
                new_photo = os.path.join(MEDIA_DIR, f"{sec}_{item_id}_{uuid.uuid4().hex[:4]}{safe_ext}")
                with open(new_photo, "wb") as f:
                    f.write(raw_bytes)
                new_file_id = file_id
            else:
                safe_send_text(message.chat.id, "Отправьте фото или «⏭ Пропустить»:", reply_markup=skip_or_cancel_keyboard())
                return
            with DATA_LOCK:
                item = next((x for x in DB["recipes"].get(sec, []) if x["id"] == item_id), None)
                if item:
                    item["photo_path"] = new_photo
                    item["photo_file_id"] = new_file_id
                    persist_recipe(sec, item)
            set_state(uid, None)
            safe_send_text(message.chat.id, "✅ Картинка обновлена.", reply_markup=main_menu_keyboard(uid))
            if item:
                safe_send_recipe(message.chat.id, item, reply_markup=recipe_item_inline(sec, item_id, uid))
            return

        if not txt:
            safe_send_text(message.chat.id, "Введите значение:")
            return
        val = "" if txt == "-" else txt
        with DATA_LOCK:
            item = next((x for x in DB["recipes"].get(sec, []) if x["id"] == item_id), None)
            if item:
                item[field] = val
                persist_recipe(sec, item)
        set_state(uid, None)
        safe_send_text(message.chat.id, "✅ Сохранено.", reply_markup=main_menu_keyboard(uid))
        if item:
            safe_send_recipe(message.chat.id, item, reply_markup=recipe_item_inline(sec, item_id, uid))
        return

    if mode == "dishware_add":
        if step == "name":
            if not txt:
                safe_send_text(message.chat.id, "Введите название посуды:")
                return
            data["name"] = txt
            set_state(uid, {"mode": mode, "step": "qty", "data": data, "return_section": "dishware"})
            safe_send_text(message.chat.id, "Введите количество (целое число):")
            return
        if step == "qty":
            try:
                qty = int(txt)
            except ValueError:
                safe_send_text(message.chat.id, "Нужно целое число:")
                return
            new_d = {"id": uuid.uuid4().hex[:8], "name": data["name"], "qty": qty, "unit": "шт.", "note": ""}
            with DATA_LOCK:
                DB["dishware"].append(new_d)
                persist_dishware(new_d)
            set_state(uid, None)
            safe_send_text(message.chat.id, f"✅ Добавлено: <b>{esc(new_d['name'])}</b> ({qty} шт.)", reply_markup=main_menu_keyboard(uid))
            render_dishware_section(message.chat.id, uid)
            return

    if mode == "dishware_adjust":
        try:
            num = int(txt)
        except ValueError:
            safe_send_text(message.chat.id, "Введите целое число:")
            return
        item_id = data["item_id"]
        with DATA_LOCK:
            d = next((x for x in DB["dishware"] if x["id"] == item_id), None)
            if d:
                d["qty"] = num
                persist_dishware(d)
        set_state(uid, None)
        safe_send_text(message.chat.id, "✅ Остаток обновлён.", reply_markup=main_menu_keyboard(uid))
        render_dishware_item(message.chat.id, uid, item_id)
        return

    if mode == "support_edit":
        if not txt:
            safe_send_text(message.chat.id, "Введите текст:")
            return
        with DATA_LOCK:
            DB["support"]["text"] = txt
            persist_support(txt)
        set_state(uid, None)
        safe_send_text(message.chat.id, "✅ Текст обновлён.", reply_markup=main_menu_keyboard(uid))
        render_support_section(message.chat.id, uid)
        return


@bot.message_handler(content_types=["text"], func=lambda m: not (m.text or "").startswith("/"))
def handle_text_search(message: types.Message):
    uid = message.from_user.id
    query = (message.text or "").strip()
    print(f"[TEXT_SEARCH] uid={uid} chat={message.chat.id} state={get_state(uid)} query={query!r}", flush=True)
    if not query:
        return
    if get_state(uid) is not None:
        return

    matches = search_entries(query)
    if not matches:
        safe_send_text(
            message.chat.id,
            f"По запросу <b>«{esc(query)}»</b> ничего не найдено.",
            reply_markup=main_menu_keyboard(uid),
        )
        return

    kb = types.InlineKeyboardMarkup(row_width=1)
    lines = [f"<b>🔎 Найдено по «{esc(query)}»:</b>\n"]
    for idx, m_item in enumerate(matches, start=1):
        lines.append(f"{idx}. <b>{esc(m_item['title'])}</b> — <i>{esc(m_item['section_title'])}</i>")
        if m_item["kind"] == "recipe":
            kb.add(types.InlineKeyboardButton(text=f"📄 {m_item['title'][:55]}", callback_data=f"r_view:{m_item['section']}:{m_item['id']}"))
        else:
            kb.add(types.InlineKeyboardButton(text=f"🥂 {m_item['title'][:55]}", callback_data=f"d_view:{m_item['id']}"))
    safe_send_text(message.chat.id, "\n".join(lines), reply_markup=kb)


@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call: types.CallbackQuery):
    uid = call.from_user.id
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    data = call.data or ""
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    if data.startswith("sec_open:"):
        sec = data.split(":", 1)[1]
        if sec == "users" and not is_admin(uid):
            return
        set_state(uid, None)
        open_section_by_key(chat_id, uid, sec, edit_msg_id=msg_id)
    elif data.startswith("r_view:"):
        _, sec, item_id = data.split(":", 2)
        with DATA_LOCK:
            item = next((x for x in DB["recipes"].get(sec, []) if x["id"] == item_id), None)
        if item:
            safe_send_recipe(chat_id, item, reply_markup=recipe_item_inline(sec, item_id, uid))
    elif data.startswith("r_add:"):
        if not is_admin(uid):
            return
        sec = data.split(":", 1)[1]
        set_state(uid, {"mode": "recipe_add", "step": "title", "data": {"section": sec}, "return_section": sec})
        safe_send_text(chat_id, f"<b>Добавление в «{esc(RECIPE_SECTIONS[sec])}»</b>\n\nВведите название:", reply_markup=cancel_keyboard())
    elif data.startswith("r_edit_menu:"):
        if not is_admin(uid):
            return
        _, sec, item_id = data.split(":", 2)
        safe_send_text(chat_id, "Что меняем?", reply_markup=recipe_edit_fields_inline(sec, item_id))
    elif data.startswith("r_ef:"):
        if not is_admin(uid):
            return
        _, sec, item_id, field = data.split(":", 3)
        set_state(uid, {"mode": "recipe_edit_field", "step": "value", "data": {"section": sec, "item_id": item_id, "field": field}, "return_section": sec})
        safe_send_text(chat_id, "Введите новое значение (или отправьте фото):", reply_markup=skip_or_cancel_keyboard() if field == "photo" else cancel_keyboard())
    elif data.startswith("r_del_pick:"):
        if not is_admin(uid):
            return
        sec = data.split(":", 1)[1]
        with DATA_LOCK:
            items = list(DB["recipes"].get(sec, []))
        kb = types.InlineKeyboardMarkup(row_width=1)
        for it in items:
            kb.add(types.InlineKeyboardButton(f"🗑 {it.get('title', 'Без названия')}", callback_data=f"r_del_ask:{sec}:{it['id']}"))
        kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"sec_open:{sec}"))
        safe_edit_text(chat_id, msg_id, "Выберите карточку для удаления:", reply_markup=kb)
    elif data.startswith("r_del_ask:"):
        if not is_admin(uid):
            return
        _, sec, item_id = data.split(":", 2)
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("✅ Удалить", callback_data=f"r_del_yes:{sec}:{item_id}"),
            types.InlineKeyboardButton("❌ Отмена", callback_data=f"sec_open:{sec}"),
        )
        safe_send_text(chat_id, "Подтвердить удаление?", reply_markup=kb)
    elif data.startswith("r_del_yes:"):
        if not is_admin(uid):
            return
        _, sec, item_id = data.split(":", 2)
        with DATA_LOCK:
            DB["recipes"][sec] = [x for x in DB["recipes"].get(sec, []) if x["id"] != item_id]
            delete_recipe_remote(item_id)
        safe_edit_text(chat_id, msg_id, "🗑 Удалено.", reply_markup=recipe_section_inline(sec, uid))
    elif data.startswith("d_view:"):
        item_id = data.split(":", 1)[1]
        render_dishware_item(chat_id, uid, item_id, edit_msg_id=msg_id)
    elif data == "d_add":
        if not is_admin(uid):
            return
        set_state(uid, {"mode": "dishware_add", "step": "name", "data": {}, "return_section": "dishware"})
        safe_send_text(chat_id, "Введите название посуды:", reply_markup=cancel_keyboard())
    elif data.startswith("d_delta:"):
        if not is_admin(uid):
            return
        _, item_id, delta_str = data.split(":", 2)
        with DATA_LOCK:
            d = next((x for x in DB["dishware"] if x["id"] == item_id), None)
            if d:
                d["qty"] = int(d.get("qty", 0)) + int(delta_str)
                persist_dishware(d)
        render_dishware_item(chat_id, uid, item_id, edit_msg_id=msg_id)
    elif data.startswith("d_set:"):
        if not is_admin(uid):
            return
        item_id = data.split(":", 1)[1]
        set_state(uid, {"mode": "dishware_adjust", "step": "amount", "data": {"item_id": item_id}, "return_section": "dishware"})
        safe_send_text(chat_id, "Введите новое число:", reply_markup=cancel_keyboard())
    elif data.startswith("d_del_ask:"):
        if not is_admin(uid):
            return
        item_id = data.split(":", 1)[1]
        with DATA_LOCK:
            DB["dishware"] = [x for x in DB["dishware"] if x["id"] != item_id]
            delete_dishware_remote(item_id)
        render_dishware_section(chat_id, uid, edit_msg_id=msg_id)
    elif data == "sup_edit":
        if not is_admin(uid):
            return
        set_state(uid, {"mode": "support_edit", "step": "text", "data": {}, "return_section": "support"})
        safe_send_text(chat_id, "Введите новый текст поддержки:", reply_markup=cancel_keyboard())


def run_bot():
    start_health_server()
    print(f"[NEW BUILD v10.3] Запуск бота (supabase: {sb_enabled()})...", flush=True)
    try:
        bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass
    time.sleep(3)
    while True:
        try:
            bot.infinity_polling(
                skip_pending=False,
                timeout=25,
                long_polling_timeout=25,
                allowed_updates=["message", "callback_query"],
            )
        except Exception as e:
            err_s = str(e)
            if "409" in err_s:
                print("[WARN] 409 Conflict, ждём 8 сек...", flush=True)
                time.sleep(8)
            else:
                print(f"[ERROR] polling crashed: {e}", flush=True)
                time.sleep(3)


if __name__ == "__main__":
    run_bot()
