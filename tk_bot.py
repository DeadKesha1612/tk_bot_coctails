def _build_photo_stream(photo_path: str | None, file_id: str | None):
    """Возвращает готовый поток/объект для bot.send_photo."""
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
    title_raw = str(card.get("title") or "")
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
