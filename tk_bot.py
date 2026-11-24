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
ALBUM_LOCK = threading.RLock()
FILE_LOCK = threading.RLock()

USER_STATES = {}
ALBUM_BUFFER = {}

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
            print(f"[WEB] Listening on port {PORT}")
            server.serve_forever()
        except Exception as e:
            print(f"[WARN] health server error: {e}")

    threading.Thread(target=_serve, daemon=True).start()


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def is_admin(user_id: int) -> bool:
    return int(user_id) == ADMIN_ID


def esc(val) -> str:
    return html.escape("" if val is None else str(val))


def clean_title_from_filename(filename: str | None, fallback: str) -> str:
    if not filename:
        return fallback
    stem = os.path.splitext(os.path.basename(filename))[0]
    stem = re.sub(r"[_\-]+", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    return stem if stem else fallback


def sb_enabled() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY)


def sb_headers(prefer: str | None = None) -> dict:
    h = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def sb_request(method: str, path: str, payload=None, prefer: str | None = None):
    if not sb_enabled():
        return None
    url = f"{SUPABASE_URL}/rest/v1/{path.lstrip('/')}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, headers=sb_headers(prefer=prefer), method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            if not raw:
                return []
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        err_txt = ""
        try:
            err_txt = e.read().decode("utf-8")[:300]
        except Exception:
            pass
        print(f"[WARN] Supabase {method} {path} HTTP {e.code}: {err_txt}")
        return None
    except Exception as e:
        print(f"[WARN] Supabase {method} {path} error: {e}")
        return None


def sb_select(table: str, query_params: str = "") -> list[dict] | None:
    q = f"?{query_params.lstrip('&')}" if query_params else ""
    res = sb_request("GET", f"{table}{q}")
    return res if isinstance(res, list) else None


def sb_upsert(table: str, rows: list[dict] | dict, on_conflict: str | None = None) -> None:
    if not sb_enabled():
        return
    prefer = "resolution=merge-duplicates,return=minimal"
    path = f"{table}?on_conflict={urllib.parse.quote(on_conflict)}" if on_conflict else table
    sb_request("POST", path, payload=rows, prefer=prefer)


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
            print(f"[WARN] save_local_snapshot error: {e}")


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
        print(f"[WARN] load_local_snapshot error: {e}")
    return data


def load_data() -> dict:
    with DATA_LOCK:
        data = load_local_snapshot()
        if not sb_enabled():
            print("[INFO] Работаем в локальном режиме (bot_data.json)")
            return data

        recipe_rows = sb_select("tk_recipes", "order=created_at.asc")
        if recipe_rows is not None and len(recipe_rows) > 0:
            data["recipes"] = {k: [] for k in RECIPE_SECTIONS}
            for r in recipe_rows:
                sec = r.get("section")
                if sec in data["recipes"]:
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
        if dish_rows is not None and len(dish_rows) > 0:
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
        if dlog_rows is not None and len(dlog_rows) > 0:
            data["dishware_logs"] = list(reversed(dlog_rows))

        sup_rows = sb_select("tk_support", "id=eq.main&limit=1")
        if sup_rows and isinstance(sup_rows[0].get("text"), str):
            data["support"]["text"] = sup_rows[0]["text"]

        user_rows = sb_select("tk_users", "order=last_seen.desc")
        if user_rows is not None and len(user_rows) > 0:
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
        if act_rows is not None and len(act_rows) > 0:
            data["activity_logs"] = list(reversed(act_rows))

        save_local_snapshot(data)
        return data


DB = load_data()


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
    threading.Thread(target=sb_upsert, args=("tk_recipes", row, "id"), daemon=True).start()


def delete_recipe_remote(item_id: str) -> None:
    sync_local_from_memory()
    threading.Thread(
        target=sb_delete,
        args=("tk_recipes", f"id=eq.{urllib.parse.quote(str(item_id))}"),
        daemon=True,
    ).start()


def persist_dishware(item: dict, log_entry: dict | None = None) -> None:
    sync_local_from_memory()
    row = {
        "id": str(item["id"]),
        "name": str(item.get("name") or ""),
        "qty": int(item.get("qty") or 0),
        "unit": str(item.get("unit") or "шт."),
        "note": str(item.get("note") or ""),
    }
    threading.Thread(target=sb_upsert, args=("tk_dishware", row, "id"), daemon=True).start()
    if log_entry:
        lrow = {
            "ts": str(log_entry.get("ts") or now_str()),
            "name": str(log_entry.get("name") or ""),
            "delta": int(log_entry.get("delta") or 0),
            "after": int(log_entry.get("after") or 0),
            "reason": str(log_entry.get("reason") or ""),
        }
        threading.Thread(target=sb_insert, args=("tk_dishware_logs", lrow), daemon=True).start()


def delete_dishware_remote(item_id: str) -> None:
    sync_local_from_memory()
    threading.Thread(
        target=sb_delete,
        args=("tk_dishware", f"id=eq.{urllib.parse.quote(str(item_id))}"),
        daemon=True,
    ).start()


def persist_support(text_val: str) -> None:
    sync_local_from_memory()
    row = {"id": "main", "text": str(text_val)}
    threading.Thread(target=sb_upsert, args=("tk_support", row, "id"), daemon=True).start()


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

    urow = {
        "uid": uid,
        "id": int(user.id),
        "username": username,
        "name": full_name,
        "first_seen": u["first_seen"],
        "last_seen": ts,
        "actions_count": int(u["actions_count"]),
    }
    threading.Thread(target=sb_upsert, args=("tk_users", urow, "uid"), daemon=True).start()
    threading.Thread(target=sb_insert, args=("tk_activity_logs", ev), daemon=True).start()


def safe_send_text(chat_id: int, text: str, reply_markup=None) -> None:
    try:
        bot.send_message(chat_id, text, reply_markup=reply_markup, disable_web_page_preview=True)
    except Exception:
        plain = html.unescape(text)
        try:
            bot.send_message(chat_id, plain, reply_markup=reply_markup, parse_mode=None, disable_web_page_preview=True)
        except Exception as e:
            print(f"[ERROR] safe_send_text: {e}")


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

    parts = [f"<b>{title}</b>"]
    if item.get("ingredients"):
        parts.append(f"\n<b>Состав:</b>\n{ingredients}")
    if item.get("method"):
        parts.append(f"\n<b>Приготовление:</b>\n{method}")
    caption = "\n".join(parts)

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
            print(f"[WARN] send_photo by file_id failed: {e}")

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
            print(f"[WARN] send_photo failed for {photo_path}: {e}")

    safe_send_text(chat_id, caption, reply_markup=reply_markup)


def remove_file_if_exists(path: str | None) -> None:
    if path and path.startswith(MEDIA_DIR) and os.path.isfile(path):
        try:
            os.remove(path)
        except Exception:
            pass


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


def bulk_done_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(types.KeyboardButton("✅ Готово"), types.KeyboardButton("❌ Отмена"))
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
        kb.add(
            types.InlineKeyboardButton(
                text=f"📄 {title[:60]}",
                callback_data=f"r_view:{section}:{item_id}",
            )
        )

    if is_admin(user_id):
        kb.row(
            types.InlineKeyboardButton("➕ Добавить", callback_data=f"r_add:{section}"),
            types.InlineKeyboardButton("📥 Массово", callback_data=f"r_bulk:{section}"),
        )
        kb.row(
            types.InlineKeyboardButton("✏️ Редактировать", callback_data=f"r_edit_pick:{section}"),
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
        kb.add(
            types.InlineKeyboardButton(
                text=f"🥂 {d.get('name', 'Посуда')} — {qty} {unit}",
                callback_data=f"d_view:{d['id']}",
            )
        )

    if is_admin(user_id):
        kb.row(
            types.InlineKeyboardButton("➕ Добавить посуду", callback_data="d_add"),
            types.InlineKeyboardButton("📜 История остатков", callback_data="d_logs"),
        )
    return kb


def dishware_item_inline(item_id: str, user_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    if is_admin(user_id):
        kb.row(
            types.InlineKeyboardButton("➖ 1", callback_data=f"d_delta:{item_id}:-1"),
            types.InlineKeyboardButton("➕ 1", callback_data=f"d_delta:{item_id}:1"),
        )
        kb.row(
            types.InlineKeyboardButton("➖ N", callback_data=f"d_custom:{item_id}:sub"),
            types.InlineKeyboardButton("➕ N", callback_data=f"d_custom:{item_id}:add"),
        )
        kb.row(
            types.InlineKeyboardButton("✏️ Задать число", callback_data=f"d_set:{item_id}"),
            types.InlineKeyboardButton("🗑 Удалить", callback_data=f"d_del_ask:{item_id}"),
        )
    kb.add(types.InlineKeyboardButton("⬅️ К списку посуды", callback_data="sec_open:dishware"))
    return kb


def users_main_inline() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("📋 Список пользователей", callback_data="u_list"),
        types.InlineKeyboardButton("🕒 Лента последних действий", callback_data="u_logs"),
    )
    return kb


def users_list_inline() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    with DATA_LOCK:
        users = list(DB["users"].values())
    users.sort(key=lambda x: x.get("last_seen", ""), reverse=True)

    for u in users[:30]:
        label = f"{u.get('name', 'Без имени')} ({u.get('username', '—')}) • {u.get('actions_count', 0)}"
        kb.add(types.InlineKeyboardButton(text=label[:64], callback_data=f"u_one:{u['id']}"))
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="sec_open:users"))
    return kb


def support_inline(user_id: int) -> types.InlineKeyboardMarkup | None:
    if not is_admin(user_id):
        return None
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✏️ Редактировать текст", callback_data="sup_edit"))
    return kb


def render_recipe_section(chat_id: int, user_id: int, section: str, edit_msg_id: int | None = None) -> None:
    try:
        title = RECIPE_SECTIONS[section]
        with DATA_LOCK:
            count = len(DB["recipes"].get(section, []))
        text = (
            f"<b>{esc(title)}</b>\n"
            f"Позиций в разделе: <b>{count}</b>\n"
            + ("<i>Выберите карточку или воспользуйтесь кнопками управления ниже.</i>" if is_admin(user_id) else "<i>Выберите позицию для просмотра.</i>")
        )
        kb = recipe_section_inline(section, user_id)
        if edit_msg_id:
            safe_edit_text(chat_id, edit_msg_id, text, reply_markup=kb)
        else:
            safe_send_text(chat_id, text, reply_markup=kb)
    except Exception as e:
        print(f"[ERROR] render_recipe_section({section}): {e}")
        safe_send_text(chat_id, f"Ошибка при открытии раздела «{esc(RECIPE_SECTIONS.get(section, section))}».", reply_markup=main_menu_keyboard(user_id))


def render_dishware_section(chat_id: int, user_id: int, edit_msg_id: int | None = None) -> None:
    with DATA_LOCK:
        items = list(DB["dishware"])
    if not items:
        text = "<b>🥂 Посуда</b>\n\nСписок посуды пока пуст."
    else:
        lines = ["<b>🥂 Посуда — текущие остатки:</b>\n"]
        for d in items:
            qty = int(d.get("qty", 0))
            unit = esc(d.get("unit") or "шт.")
            lines.append(f"• <b>{esc(d.get('name', 'Посуда'))}</b>: <b>{qty}</b> {unit}")
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
        safe_send_text(chat_id, "Позиция посуды не найдена.", reply_markup=main_menu_keyboard(user_id))
        return

    qty = int(item.get("qty", 0))
    unit = esc(item.get("unit") or "шт.")
    note = esc(item.get("note") or "")
    text = f"<b>🥂 {esc(item.get('name', 'Посуда'))}</b>\nОстаток: <b>{qty}</b> {unit}"
    if note:
        text += f"\nПримечание: {note}"

    kb = dishware_item_inline(item_id, user_id)
    if edit_msg_id:
        safe_edit_text(chat_id, edit_msg_id, text, reply_markup=kb)
    else:
        safe_send_text(chat_id, text, reply_markup=kb)


def render_support_section(chat_id: int, user_id: int, edit_msg_id: int | None = None) -> None:
    with DATA_LOCK:
        raw_text = DB["support"].get("text", "")
    text = f"<b>💛 Поддержать проект</b>\n\n{esc(raw_text)}"
    kb = support_inline(user_id)
    if edit_msg_id:
        safe_edit_text(chat_id, edit_msg_id, text, reply_markup=kb)
    else:
        safe_send_text(chat_id, text, reply_markup=kb)


def render_users_section(chat_id: int, user_id: int, edit_msg_id: int | None = None) -> None:
    if not is_admin(user_id):
        return
    with DATA_LOCK:
        total_users = len(DB["users"])
        total_logs = len(DB["activity_logs"])
    text = (
        "<b>👥 Пользователи и активность</b>\n\n"
        f"Всего пользователей: <b>{total_users}</b>\n"
        f"Записей в журнале действий: <b>{total_logs}</b>"
    )
    kb = users_main_inline()
    if edit_msg_id:
        safe_edit_text(chat_id, edit_msg_id, text, reply_markup=kb)
    else:
        safe_send_text(chat_id, text, reply_markup=kb)


def open_section_by_key(chat_id: int, user_id: int, section: str, edit_msg_id: int | None = None) -> None:
    if section in RECIPE_SECTIONS:
        render_recipe_section(chat_id, user_id, section, edit_msg_id=edit_msg_id)
    elif section == "dishware":
        render_dishware_section(chat_id, user_id, edit_msg_id=edit_msg_id)
    elif section == "support":
        render_support_section(chat_id, user_id, edit_msg_id=edit_msg_id)
    elif section == "users":
        if is_admin(user_id):
            render_users_section(chat_id, user_id, edit_msg_id=edit_msg_id)


def extract_media_payload(msg: types.Message) -> tuple[bytes | None, str | None, str | None, str | None]:
    if msg.content_type == "photo" and msg.photo:
        best = msg.photo[-1]
        file_info = bot.get_file(best.file_id)
        raw = bot.download_file(file_info.file_path)
        return raw, ".jpg", None, best.file_id

    if msg.content_type == "document" and msg.document:
        doc = msg.document
        mime = (doc.mime_type or "").lower()
        orig_name = doc.file_name or ""
        ext = os.path.splitext(orig_name)[1].lower()
        if not ext and mime.startswith("image/"):
            ext = "." + mime.split("/", 1)[1].replace("jpeg", "jpg")
        if ext in (".png", ".jpg", ".jpeg", ".webp") or mime.startswith("image/"):
            file_info = bot.get_file(doc.file_id)
            raw = bot.download_file(file_info.file_path)
            return raw, ext or ".png", orig_name, doc.file_id

    return None, None, None, None


def commit_bulk_messages(uid: int, chat_id: int, sec: str, msgs: list[types.Message]) -> None:
    st = get_state(uid)
    if not st or st.get("mode") != "recipe_bulk":
        return

    added_titles = []
    skipped_count = 0

    with DATA_LOCK:
        current_count = len(DB["recipes"].get(sec, []))
        for idx, m in enumerate(msgs, start=1):
            raw_bytes, ext, orig_name, file_id = extract_media_payload(m)
            if not raw_bytes:
                skipped_count += 1
                continue

            item_id = uuid.uuid4().hex[:8]
            safe_ext = ext if ext in (".png", ".jpg", ".jpeg", ".webp") else ".jpg"
            photo_path = os.path.join(MEDIA_DIR, f"{sec}_{item_id}{safe_ext}")
            with open(photo_path, "wb") as f:
                f.write(raw_bytes)

            caption = (m.caption or "").strip()
            fallback_title = f"Позиция {current_count + len(added_titles) + 1}"
            if orig_name:
                title = clean_title_from_filename(orig_name, fallback_title)
            elif caption:
                title = caption if len(msgs) == 1 else f"{caption} ({idx})"
            else:
                title = fallback_title

            new_item = {
                "id": item_id,
                "title": title,
                "ingredients": "",
                "method": "",
                "photo_path": photo_path,
                "photo_file_id": file_id,
                "created_at": now_str(),
            }
            DB["recipes"].setdefault(sec, []).append(new_item)
            persist_recipe(sec, new_item)
            added_titles.append(title)

    if added_titles:
        track_user_and_action(
            msgs[0].from_user,
            f"Массово загрузил ({len(added_titles)}) в «{RECIPE_SECTIONS[sec]}»: {', '.join(added_titles[:5])}",
        )
        preview = "\n".join(f"• <b>{esc(t)}</b>" for t in added_titles[:25])
        extra = f"\n…и ещё {len(added_titles) - 25}" if len(added_titles) > 25 else ""
        skip_note = f"\n<i>Пропущено неподходящих файлов: {skipped_count}</i>" if skipped_count else ""
        safe_send_text(
            chat_id,
            f"✅ Добавлено позиций: <b>{len(added_titles)}</b>\n\n{preview}{extra}{skip_note}\n\n"
            f"Можете отправлять ещё файлы или нажать <b>«✅ Готово»</b>.",
            reply_markup=bulk_done_keyboard(),
        )
    elif skipped_count:
        safe_send_text(
            chat_id,
            "⚠️ Не удалось распознать как картинку (поддерживаются PNG, JPG, WEBP и фото). Попробуйте ещё раз.",
            reply_markup=bulk_done_keyboard(),
        )


def flush_album(media_group_id: str) -> None:
    with ALBUM_LOCK:
        entry = ALBUM_BUFFER.pop(media_group_id, None)
    if not entry:
        return
    msgs = entry["messages"]
    if not msgs:
        return
    first = msgs[0]
    commit_bulk_messages(first.from_user.id, first.chat.id, entry["section"], msgs)


def enqueue_bulk_message(message: types.Message, sec: str) -> None:
    mgid = message.media_group_id
    if not mgid:
        commit_bulk_messages(message.from_user.id, message.chat.id, sec, [message])
        return

    with ALBUM_LOCK:
        entry = ALBUM_BUFFER.get(mgid)
        if entry is None:
            timer = threading.Timer(0.7, flush_album, args=[mgid])
            ALBUM_BUFFER[mgid] = {
                "section": sec,
                "messages": [message],
                "timer": timer,
            }
            timer.start()
        else:
            entry["messages"].append(message)
            if entry.get("timer"):
                entry["timer"].cancel()
            timer = threading.Timer(0.7, flush_album, args=[mgid])
            entry["timer"] = timer
            timer.start()


@bot.message_handler(commands=["start", "menu", "cancel"])
def cmd_start_or_cancel(message: types.Message):
    uid = message.from_user.id
    set_state(uid, None)
    track_user_and_action(message.from_user, f"Команда {message.text or '/start'}")
    safe_send_text(
        message.chat.id,
        "<b>Главное меню</b>\nВыберите нужный раздел:",
        reply_markup=main_menu_keyboard(uid),
    )


@bot.message_handler(func=lambda m: m.text == "✅ Готово")
def btn_bulk_done(message: types.Message):
    uid = message.from_user.id
    st = get_state(uid)
    set_state(uid, None)
    track_user_and_action(message.from_user, "Завершил массовую загрузку")
    safe_send_text(message.chat.id, "✅ Массовая загрузка завершена.", reply_markup=main_menu_keyboard(uid))
    if st and st.get("return_section"):
        open_section_by_key(message.chat.id, uid, st["return_section"])


@bot.message_handler(func=lambda m: m.text == "❌ Отмена")
def btn_cancel(message: types.Message):
    uid = message.from_user.id
    st = get_state(uid)
    set_state(uid, None)
    track_user_and_action(message.from_user, "Отмена ввода")
    safe_send_text(message.chat.id, "Действие отменено.", reply_markup=main_menu_keyboard(uid))
    if st and st.get("return_section"):
        open_section_by_key(message.chat.id, uid, st["return_section"])


@bot.message_handler(func=lambda m: m.text in BTN_TO_SECTION)
def handle_main_menu_buttons(message: types.Message):
    uid = message.from_user.id
    sec = BTN_TO_SECTION[message.text]
    if sec == "users" and not is_admin(uid):
        return
    set_state(uid, None)
    track_user_and_action(message.from_user, f"Открыл раздел «{ALL_SECTION_TITLES.get(sec, sec)}»")
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

    if mode == "recipe_bulk":
        sec = data["section"]
        if message.content_type in ("photo", "document"):
            enqueue_bulk_message(message, sec)
        else:
            safe_send_text(
                message.chat.id,
                "Отправьте файлы/фото (можно сразу пачкой) или нажмите <b>«✅ Готово»</b>.",
                reply_markup=bulk_done_keyboard(),
            )
        return

    if mode == "recipe_add":
        sec = data["section"]
        if step == "title":
            if not txt:
                safe_send_text(message.chat.id, "Название не может быть пустым. Введите название текстом:")
                return
            data["title"] = txt
            set_state(uid, {"mode": mode, "step": "ingredients", "data": data, "return_section": sec})
            safe_send_text(message.chat.id, "Введите <b>состав и пропорции</b> (или отправьте <code>-</code>, если без состава):")
            return

        if step == "ingredients":
            data["ingredients"] = "" if txt == "-" else txt
            set_state(uid, {"mode": mode, "step": "method", "data": data, "return_section": sec})
            safe_send_text(message.chat.id, "Введите <b>способ приготовления</b> (или отправьте <code>-</code>):")
            return

        if step == "method":
            data["method"] = "" if txt == "-" else txt
            set_state(uid, {"mode": mode, "step": "photo", "data": data, "return_section": sec})
            safe_send_text(
                message.chat.id,
                "Отправьте <b>готовое фото / PNG</b> или нажмите <b>«⏭ Пропустить»</b>:",
                reply_markup=skip_or_cancel_keyboard(),
            )
            return

        if step == "photo":
            photo_path = None
            file_id = None
            item_id = uuid.uuid4().hex[:8]
            raw_bytes, ext, _, file_id = extract_media_payload(message)
            if raw_bytes:
                safe_ext = ext if ext in (".png", ".jpg", ".jpeg", ".webp") else ".jpg"
                photo_path = os.path.join(MEDIA_DIR, f"{sec}_{item_id}{safe_ext}")
                with open(photo_path, "wb") as f:
                    f.write(raw_bytes)
            elif txt == "⏭ Пропустить":
                photo_path = None
            else:
                safe_send_text(
                    message.chat.id,
                    "Пришлите фото/картинку или нажмите <b>«⏭ Пропустить»</b>.",
                    reply_markup=skip_or_cancel_keyboard(),
                )
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
            track_user_and_action(message.from_user, f"Добавил в «{RECIPE_SECTIONS[sec]}»: {new_item['title']}")
            safe_send_text(
                message.chat.id,
                f"✅ Позиция <b>{esc(new_item['title'])}</b> добавлена.",
                reply_markup=main_menu_keyboard(uid),
            )
            render_recipe_section(message.chat.id, uid, sec)
            return

    if mode == "recipe_edit_field":
        sec = data["section"]
        item_id = data["item_id"]
        field = data["field"]

        if field == "photo":
            raw_bytes, ext, _, file_id = extract_media_payload(message)
            if txt == "⏭ Пропустить":
                new_photo = None
                new_file_id = None
            elif raw_bytes:
                safe_ext = ext if ext in (".png", ".jpg", ".jpeg", ".webp") else ".jpg"
                new_photo = os.path.join(MEDIA_DIR, f"{sec}_{item_id}_{uuid.uuid4().hex[:4]}{safe_ext}")
                with open(new_photo, "wb") as f:
                    f.write(raw_bytes)
                new_file_id = file_id
            else:
                safe_send_text(
                    message.chat.id,
                    "Отправьте новое фото/файл или нажмите <b>«⏭ Пропустить»</b> чтобы удалить картинку:",
                    reply_markup=skip_or_cancel_keyboard(),
                )
                return

            with DATA_LOCK:
                item = next((x for x in DB["recipes"].get(sec, []) if x["id"] == item_id), None)
                if item:
                    remove_file_if_exists(item.get("photo_path"))
                    item["photo_path"] = new_photo
                    item["photo_file_id"] = new_file_id
                    persist_recipe(sec, item)
            set_state(uid, None)
            track_user_and_action(message.from_user, f"Обновил картинку в «{RECIPE_SECTIONS[sec]}»")
            safe_send_text(message.chat.id, "✅ Картинка обновлена.", reply_markup=main_menu_keyboard(uid))
            if item:
                safe_send_recipe(message.chat.id, item, reply_markup=recipe_item_inline(sec, item_id, uid))
            return

        if not txt:
            safe_send_text(message.chat.id, "Введите новое значение текстом:")
            return

        val = "" if txt == "-" else txt
        if field == "title" and not val:
            safe_send_text(message.chat.id, "Название не может быть пустым. Введите название:")
            return

        with DATA_LOCK:
            item = next((x for x in DB["recipes"].get(sec, []) if x["id"] == item_id), None)
            if item:
                item[field] = val
                persist_recipe(sec, item)

        set_state(uid, None)
        track_user_and_action(message.from_user, f"Отредактировал поле {field} в «{RECIPE_SECTIONS[sec]}»")
        safe_send_text(message.chat.id, "✅ Изменения сохранены.", reply_markup=main_menu_keyboard(uid))
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
            safe_send_text(message.chat.id, "Введите начальный остаток (целое число, например <code>24</code>):")
            return

        if step == "qty":
            try:
                qty = int(txt)
            except ValueError:
                safe_send_text(message.chat.id, "Нужно ввести целое число, например <code>24</code> или <code>0</code>:")
                return
            data["qty"] = qty
            set_state(uid, {"mode": mode, "step": "unit", "data": data, "return_section": "dishware"})
            safe_send_text(
                message.chat.id,
                "Введите единицу измерения (например <code>шт.</code>) или нажмите <b>«⏭ Пропустить»</b>:",
                reply_markup=skip_or_cancel_keyboard(),
            )
            return

        if step == "unit":
            unit = "шт." if txt in ("", "⏭ Пропустить", "-") else txt
            item_id = uuid.uuid4().hex[:8]
            new_d = {
                "id": item_id,
                "name": data["name"],
                "qty": int(data["qty"]),
                "unit": unit,
                "note": "",
            }
            log_ev = {
                "ts": now_str(),
                "name": new_d["name"],
                "delta": new_d["qty"],
                "after": new_d["qty"],
                "reason": "создание позиции",
            }
            with DATA_LOCK:
                DB["dishware"].append(new_d)
                DB["dishware_logs"].append(log_ev)
                persist_dishware(new_d, log_ev)

            set_state(uid, None)
            track_user_and_action(message.from_user, f"Добавил посуду: {new_d['name']} ({new_d['qty']} {unit})")
            safe_send_text(
                message.chat.id,
                f"✅ Позиция <b>{esc(new_d['name'])}</b> ({new_d['qty']} {esc(unit)}) добавлена.",
                reply_markup=main_menu_keyboard(uid),
            )
            render_dishware_section(message.chat.id, uid)
            return

    if mode == "dishware_adjust":
        try:
            num = int(txt)
        except ValueError:
            safe_send_text(message.chat.id, "Введите целое число:")
            return

        op = data["op"]
        item_id = data["item_id"]
        with DATA_LOCK:
            d = next((x for x in DB["dishware"] if x["id"] == item_id), None)
            if not d:
                set_state(uid, None)
                safe_send_text(message.chat.id, "Позиция не найдена.", reply_markup=main_menu_keyboard(uid))
                return
            old_qty = int(d.get("qty", 0))
            if op == "add":
                delta = abs(num)
                new_qty = old_qty + delta
            elif op == "sub":
                delta = -abs(num)
                new_qty = old_qty + delta
            else:
                new_qty = num
                delta = new_qty - old_qty

            d["qty"] = new_qty
            log_ev = {
                "ts": now_str(),
                "name": d["name"],
                "delta": delta,
                "after": new_qty,
                "reason": f"было {old_qty} → стало {new_qty}",
            }
            DB["dishware_logs"].append(log_ev)
            persist_dishware(d, log_ev)

        set_state(uid, None)
        track_user_and_action(message.from_user, f"Изменил остаток «{d['name']}»: {old_qty} → {new_qty}")
        safe_send_text(
            message.chat.id,
            f"✅ Остаток <b>{esc(d['name'])}</b>: <b>{old_qty}</b> → <b>{new_qty}</b>",
            reply_markup=main_menu_keyboard(uid),
        )
        render_dishware_item(message.chat.id, uid, item_id)
        return

    if mode == "support_edit":
        if not txt:
            safe_send_text(message.chat.id, "Текст не может быть пустым. Введите текст:")
            return
        with DATA_LOCK:
            DB["support"]["text"] = txt
            persist_support(txt)
        set_state(uid, None)
        track_user_and_action(message.from_user, "Обновил текст раздела «Поддержать проект»")
        safe_send_text(message.chat.id, "✅ Текст обновлён.", reply_markup=main_menu_keyboard(uid))
        render_support_section(message.chat.id, uid)
        return


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
        track_user_and_action(call.from_user, f"Открыл раздел «{ALL_SECTION_TITLES.get(sec, sec)}»")
        open_section_by_key(chat_id, uid, sec, edit_msg_id=msg_id)
        return

    if data.startswith("r_view:"):
        _, sec, item_id = data.split(":", 2)
        with DATA_LOCK:
            item = next((x for x in DB["recipes"].get(sec, []) if x["id"] == item_id), None)
        if not item:
            safe_send_text(chat_id, "Карточка не найдена.")
            return
        track_user_and_action(call.from_user, f"Открыл «{item.get('title')}» в «{RECIPE_SECTIONS.get(sec, sec)}»")
        safe_send_recipe(chat_id, item, reply_markup=recipe_item_inline(sec, item_id, uid))
        return

    if data.startswith("r_add:"):
        if not is_admin(uid):
            return
        sec = data.split(":", 1)[1]
        set_state(uid, {"mode": "recipe_add", "step": "title", "data": {"section": sec}, "return_section": sec})
        safe_send_text(
            chat_id,
            f"<b>Добавление в «{esc(RECIPE_SECTIONS[sec])}»</b>\n\nВведите <b>название</b>:",
            reply_markup=cancel_keyboard(),
        )
        return

    if data.startswith("r_bulk:"):
        if not is_admin(uid):
            return
        sec = data.split(":", 1)[1]
        set_state(uid, {"mode": "recipe_bulk", "step": "files", "data": {"section": sec}, "return_section": sec})
        safe_send_text(
            chat_id,
            f"<b>📥 Массовая загрузка в «{esc(RECIPE_SECTIONS[sec])}»</b>\n\n"
            "Отправляйте сразу несколько файлов (PNG/JPG/WEBP как <b>файл</b> или <b>фото</b> / альбомом).\n"
            "Название подхватится из имени файла (если файл) или из подписи.\n\n"
            "Когда закончите — нажмите <b>«✅ Готово»</b>.",
            reply_markup=bulk_done_keyboard(),
        )
        return

    if data.startswith("r_edit_pick:"):
        if not is_admin(uid):
            return
        sec = data.split(":", 1)[1]
        with DATA_LOCK:
            items = list(DB["recipes"].get(sec, []))
        if not items:
            safe_send_text(chat_id, "В этом разделе пока нет карточек для редактирования.")
            return
        kb = types.InlineKeyboardMarkup(row_width=1)
        for it in items:
            kb.add(
                types.InlineKeyboardButton(
                    f"✏️ {it.get('title', 'Без названия')}",
                    callback_data=f"r_edit_menu:{sec}:{it['id']}",
                )
            )
        kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"sec_open:{sec}"))
        safe_edit_text(chat_id, msg_id, "Выберите карточку для редактирования:", reply_markup=kb)
        return

    if data.startswith("r_edit_menu:"):
        if not is_admin(uid):
            return
        _, sec, item_id = data.split(":", 2)
        with DATA_LOCK:
            item = next((x for x in DB["recipes"].get(sec, []) if x["id"] == item_id), None)
        if not item:
            safe_send_text(chat_id, "Карточка не найдена.")
            return
        safe_send_text(
            chat_id,
            f"Что редактируем в <b>{esc(item.get('title'))}</b>?",
            reply_markup=recipe_edit_fields_inline(sec, item_id),
        )
        return

    if data.startswith("r_ef:"):
        if not is_admin(uid):
            return
        _, sec, item_id, field = data.split(":", 3)
        set_state(
            uid,
            {
                "mode": "recipe_edit_field",
                "step": "value",
                "data": {"section": sec, "item_id": item_id, "field": field},
                "return_section": sec,
            },
        )
        if field == "photo":
            safe_send_text(
                chat_id,
                "Отправьте новое фото/файл или нажмите <b>«⏭ Пропустить»</b> чтобы удалить картинку:",
                reply_markup=skip_or_cancel_keyboard(),
            )
        else:
            prompts = {
                "title": "Введите новое <b>название</b>:",
                "ingredients": "Введите новый <b>состав</b> (или <code>-</code> для очистки):",
                "method": "Введите новый <b>способ приготовления</b> (или <code>-</code> для очистки):",
            }
            safe_send_text(chat_id, prompts.get(field, "Введите новое значение:"), reply_markup=cancel_keyboard())
        return

    if data.startswith("r_del_pick:"):
        if not is_admin(uid):
            return
        sec = data.split(":", 1)[1]
        with DATA_LOCK:
            items = list(DB["recipes"].get(sec, []))
        if not items:
            safe_send_text(chat_id, "В этом разделе пока нет карточек для удаления.")
            return
        kb = types.InlineKeyboardMarkup(row_width=1)
        for it in items:
            kb.add(
                types.InlineKeyboardButton(
                    f"🗑 {it.get('title', 'Без названия')}",
                    callback_data=f"r_del_ask:{sec}:{it['id']}",
                )
            )
        kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"sec_open:{sec}"))
        safe_edit_text(chat_id, msg_id, "Выберите карточку для удаления:", reply_markup=kb)
        return

    if data.startswith("r_del_ask:"):
        if not is_admin(uid):
            return
        _, sec, item_id = data.split(":", 2)
        with DATA_LOCK:
            item = next((x for x in DB["recipes"].get(sec, []) if x["id"] == item_id), None)
        if not item:
            safe_send_text(chat_id, "Карточка уже удалена.")
            return
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("✅ Да, удалить", callback_data=f"r_del_yes:{sec}:{item_id}"),
            types.InlineKeyboardButton("❌ Отмена", callback_data=f"sec_open:{sec}"),
        )
        safe_send_text(
            chat_id,
            f"Удалить карточку <b>{esc(item.get('title'))}</b>?",
            reply_markup=kb,
        )
        return

    if data.startswith("r_del_yes:"):
        if not is_admin(uid):
            return
        _, sec, item_id = data.split(":", 2)
        deleted_title = ""
        with DATA_LOCK:
            arr = DB["recipes"].get(sec, [])
            kept = []
            for it in arr:
                if it["id"] == item_id:
                    deleted_title = it.get("title", "")
                    remove_file_if_exists(it.get("photo_path"))
                else:
                    kept.append(it)
            DB["recipes"][sec] = kept
            delete_recipe_remote(item_id)
        track_user_and_action(call.from_user, f"Удалил из «{RECIPE_SECTIONS.get(sec, sec)}»: {deleted_title}")
        safe_edit_text(
            chat_id,
            msg_id,
            f"🗑 Удалено: <b>{esc(deleted_title or item_id)}</b>",
            reply_markup=recipe_section_inline(sec, uid),
        )
        return

    if data.startswith("d_view:"):
        item_id = data.split(":", 1)[1]
        with DATA_LOCK:
            d = next((x for x in DB["dishware"] if x["id"] == item_id), None)
        if d:
            track_user_and_action(call.from_user, f"Открыл посуду «{d.get('name')}»")
        render_dishware_item(chat_id, uid, item_id, edit_msg_id=msg_id)
        return

    if data == "d_add":
        if not is_admin(uid):
            return
        set_state(uid, {"mode": "dishware_add", "step": "name", "data": {}, "return_section": "dishware"})
        safe_send_text(chat_id, "<b>Новая позиция посуды</b>\n\nВведите название:", reply_markup=cancel_keyboard())
        return

    if data.startswith("d_delta:"):
        if not is_admin(uid):
            return
        _, item_id, delta_str = data.split(":", 2)
        delta = int(delta_str)
        with DATA_LOCK:
            d = next((x for x in DB["dishware"] if x["id"] == item_id), None)
            if not d:
                return
            old_qty = int(d.get("qty", 0))
            new_qty = old_qty + delta
            d["qty"] = new_qty
            log_ev = {
                "ts": now_str(),
                "name": d["name"],
                "delta": delta,
                "after": new_qty,
                "reason": f"кнопка {delta:+d}",
            }
            DB["dishware_logs"].append(log_ev)
            persist_dishware(d, log_ev)
        track_user_and_action(call.from_user, f"Посуда «{d['name']}» ({delta:+d}): {old_qty} → {new_qty}")
        render_dishware_item(chat_id, uid, item_id, edit_msg_id=msg_id)
        return

    if data.startswith("d_custom:"):
        if not is_admin(uid):
            return
        _, item_id, op = data.split(":", 2)
        set_state(
            uid,
            {
                "mode": "dishware_adjust",
                "step": "amount",
                "data": {"item_id": item_id, "op": op},
                "return_section": "dishware",
            },
        )
        label = "прибавить (приехала посуда)" if op == "add" else "списать (взяли со склада)"
        safe_send_text(chat_id, f"Введите количество, которое нужно <b>{label}</b>:", reply_markup=cancel_keyboard())
        return

    if data.startswith("d_set:"):
        if not is_admin(uid):
            return
        item_id = data.split(":", 1)[1]
        set_state(
            uid,
            {
                "mode": "dishware_adjust",
                "step": "amount",
                "data": {"item_id": item_id, "op": "set"},
                "return_section": "dishware",
            },
        )
        safe_send_text(chat_id, "Введите новый точный остаток (целое число):", reply_markup=cancel_keyboard())
        return

    if data.startswith("d_del_ask:"):
        if not is_admin(uid):
            return
        item_id = data.split(":", 1)[1]
        with DATA_LOCK:
            d = next((x for x in DB["dishware"] if x["id"] == item_id), None)
        if not d:
            return
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("✅ Да, удалить", callback_data=f"d_del_yes:{item_id}"),
            types.InlineKeyboardButton("❌ Отмена", callback_data=f"d_view:{item_id}"),
        )
        safe_send_text(chat_id, f"Удалить позицию посуды <b>{esc(d.get('name'))}</b>?", reply_markup=kb)
        return

    if data.startswith("d_del_yes:"):
        if not is_admin(uid):
            return
        item_id = data.split(":", 1)[1]
        removed_name = ""
        with DATA_LOCK:
            kept = []
            for x in DB["dishware"]:
                if x["id"] == item_id:
                    removed_name = x.get("name", "")
                else:
                    kept.append(x)
            DB["dishware"] = kept
            delete_dishware_remote(item_id)
        track_user_and_action(call.from_user, f"Удалил посуду: {removed_name}")
        safe_send_text(chat_id, f"🗑 Удалено: <b>{esc(removed_name or item_id)}</b>")
        render_dishware_section(chat_id, uid)
        return

    if data == "d_logs":
        if not is_admin(uid):
            return
        with DATA_LOCK:
            logs = list(reversed(DB["dishware_logs"][-25:]))
        if not logs:
            text = "<b>📜 История остатков посуды</b>\n\nЗаписей пока нет."
        else:
            lines = ["<b>📜 Последние движения посуды:</b>\n"]
            for lg in logs:
                lines.append(
                    f"• <code>{esc(lg['ts'])}</code> — <b>{esc(lg['name'])}</b> "
                    f"(<code>{int(lg['delta']):+d}</code>, остаток <b>{int(lg['after'])}</b>) [{esc(lg.get('reason', ''))}]"
                )
            text = "\n".join(lines)
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("⬅️ К списку посуды", callback_data="sec_open:dishware"))
        safe_edit_text(chat_id, msg_id, text, reply_markup=kb)
        return

    if data == "u_list":
        if not is_admin(uid):
            return
        with DATA_LOCK:
            count = len(DB["users"])
        safe_edit_text(
            chat_id,
            msg_id,
            f"<b>📋 Пользователи (всего: {count})</b>\nНажмите на пользователя для подробностей:",
            reply_markup=users_list_inline(),
        )
        return

    if data.startswith("u_one:"):
        if not is_admin(uid):
            return
        target_uid = int(data.split(":", 1)[1])
        with DATA_LOCK:
            u = DB["users"].get(str(target_uid))
            u_logs = [x for x in DB["activity_logs"] if x.get("user_id") == target_uid][-15:]

        if not u:
            safe_send_text(chat_id, "Пользователь не найден.")
            return

        lines = [
            "<b>👤 Карточка пользователя</b>",
            f"Имя: <b>{esc(u.get('name'))}</b>",
            f"Username: {esc(u.get('username'))}",
            f"ID: <code>{u.get('id')}</code>",
            f"Первый вход: <code>{esc(u.get('first_seen'))}</code>",
            f"Последняя активность: <code>{esc(u.get('last_seen'))}</code>",
            f"Всего действий: <b>{int(u.get('actions_count', 0))}</b>",
            "\n<b>Последние действия:</b>",
        ]
        if not u_logs:
            lines.append("— нет записей")
        else:
            for item in reversed(u_logs):
                lines.append(f"• <code>{esc(item['ts'])}</code> — {esc(item['action'])}")

        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("⬅️ К списку пользователей", callback_data="u_list"))
        safe_edit_text(chat_id, msg_id, "\n".join(lines), reply_markup=kb)
        return

    if data == "u_logs":
        if not is_admin(uid):
            return
        with DATA_LOCK:
            recent = list(reversed(DB["activity_logs"][-30:]))
        if not recent:
            text = "<b>🕒 Лента действий</b>\n\nПока пусто."
        else:
            lines = ["<b>🕒 Последние действия пользователей:</b>\n"]
            for ev in recent:
                lines.append(
                    f"• <code>{esc(ev['ts'])}</code> | <b>{esc(ev['name'])}</b> "
                    f"({esc(ev['username'])}, <code>{ev['user_id']}</code>): {esc(ev['action'])}"
                )
            text = "\n".join(lines)
            if len(text) > 3900:
                text = text[:3900] + "\n…"

        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="sec_open:users"))
        safe_edit_text(chat_id, msg_id, text, reply_markup=kb)
        return

    if data == "sup_edit":
        if not is_admin(uid):
            return
        set_state(uid, {"mode": "support_edit", "step": "text", "data": {}, "return_section": "support"})
        safe_send_text(
            chat_id,
            "Введите новый текст для раздела <b>«Поддержать проект»</b>:",
            reply_markup=cancel_keyboard(),
        )
        return


def run_bot():
    start_health_server()
    print(f"[NEW BUILD v9.2] Запуск бота (supabase: {sb_enabled()})...")
    try:
        bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass
    time.sleep(2)
    while True:
        try:
            bot.infinity_polling(
                skip_pending=False,
                timeout=25,
                long_polling_timeout=25,
                allowed_updates=["message", "callback_query"],
            )
        except Exception as e:
            print(f"[ERROR] polling crashed: {e}")
            time.sleep(3)


if __name__ == "__main__":
    run_bot()
