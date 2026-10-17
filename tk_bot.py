import os
import glob
import urllib.request
import telebot
from telebot import types
from PIL import Image, ImageDraw, ImageFont

TOKEN = '8797743243:AAEasJGWiZpQuG4ztLJj8uCqdzkiXPP7tTs'
bot = telebot.TeleBot(TOKEN)

CARDS_CACHE = {}

FONT_PATH = "DejaVuSans.ttf"
if not os.path.exists(FONT_PATH):
    try:
        url = "https://github.com/dejavu-fonts/dejavu-fonts/raw/master/ttf/DejaVuSans.ttf"
        urllib.request.urlretrieve(url, FONT_PATH)
    except Exception as e:
        print(f"Не удалось скачать шрифт: {e}")

def get_font(size):
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except:
        return ImageFont.load_default()

CLASSIC_NAMES = [
    "дайкири", "маргарита", "секс на пляже", "манхэттен", 
    "космополитен", "негрони", "черный русский", "сайдкар", "беллини"
]

AUTHOR_NAMES = [
    "жидкий опиум", "жаба", "алтайский хайбол", "сибирский экспресс", "клубничный взрыв",
    "хиросима", "облака", "грейпфрутовый сауэр", "ягодный колинз", "зеленый мексиканец",
    "унесенные ветром", "горящий ламборгини", "зеленая фурия", "старый кубинец", "синий кит", "краски"
]

CLASSIC_RECIPES = {
    "Дайкири": {
        "num": "201", "ml": "110 мл",
        "ing": ["• Белый ром — 60 мл", "• Сок лайма — 30 мл", "• Сахарный сироп — 20 мл"],
        "steps": ["1. Налить все ингредиенты в шейкер со льдом.", "2. Интенсивно взбить.", "3. Отцедить в охлажденный бокал купе."]
    },
    "Маргарита": {
        "num": "202", "ml": "120 мл",
        "ing": ["• Текила серебряная — 50 мл", "• Куантро (Трипл Сек) — 25 мл", "• Сок лайма — 25 мл"],
        "steps": ["1. Сделать соляную кромку на бокале.", "2. Взбить ингредиенты со льдом в шейкере.", "3. Перелить в бокал маргарита."]
    },
    "Секс на пляже": {
        "num": "203", "ml": "140 мл",
        "ing": [
            "• Водка — 40 мл", 
            "• Персиковый ликер — 20 мл", 
            "• Клюквенный морс — 40 мл", 
            "• Апельсиновый сок — 40 мл"
        ],
        "steps": [
            "1. Положить лёд в шейкер.", 
            "2. Налить все компоненты (водку, персиковый ликер, клюквенный морс и апельсиновый сок).", 
            "3. Интенсивно взбить и отцедить в бокал хайболл со льдом."
        ]
    },
    "Манхэттен": {
        "num": "204", "ml": "90 мл",
        "ing": ["• Бурбон (или ржаной виски) — 50 мл", "• Красный вермут — 25 мл", "• Биттер Ангостура — 2 мл (2 дэш)"],
        "steps": ["1. Смешать все ингредиенты в смесительном стакане со льдом.", "2. Процедить в охлажденный бокал мартини/купе.", "3. Украсить коктейльной вишней."]
    },
    "Космополитен": {
        "num": "205", "ml": "120 мл",
        "ing": ["• Цитрусовая водка — 40 мл", "• Куантро — 20 мл", "• Сок лайма — 15 мл", "• Клюквенный морс — 30 мл"],
        "steps": ["1. Взбить все ингредиенты со льдом в шейкере.", "2. Отцедить в охлажденный бокал для мартини."]
    },
    "Негрони": {
        "num": "206", "ml": "90 мл",
        "ing": ["• Джин — 30 мл", "• Красный вермут — 30 мл", "• Кампари — 30 мл"],
        "steps": ["1. Налить компоненты в рокс со льдом.", "2. Перемешать барной ложкой.", "3. Украсить цедрой апельсина."]
    },
    "Черный русский": {
        "num": "207", "ml": "100 мл",
        "ing": ["• Водка — 50 мл", "• Кофейный ликер (Калуа) — 25 мл"],
        "steps": ["1. Наполнить стакан рокс кубиками льда.", "2. Налить водку и кофейный ликер.", "3. Перемешать."]
    },
    "Сайдкар": {
        "num": "208", "ml": "100 мл",
        "ing": ["• Коньяк — 50 мл", "• Куантро — 25 мл", "• Сок лимона — 25 мл"],
        "steps": ["1. Сделать сахарную каемку на бокале.", "2. Взбить ингредиенты со льдом в шейкере.", "3. Отцедить в бокал."]
    },
    "Беллини": {
        "num": "209", "ml": "150 мл",
        "ing": ["• Персиковое пюре — 50 мл", "• Игристое вино (Просекко) — 100 мл"],
        "steps": ["1. Положить персиковое пюре в охлажденный бокал флейт.", "2. Долить игристое вино.", "3. Аккуратно перемешать."]
    }
}

AUTHOR_RECIPES = {
    "Жидкий опиум": {
        "num": "108", "ml": "225 мл",
        "ing": [
            "• Абсент — 25 г", 
            "• Самбука — 25 г", 
            "• Настойка Легенды Алтая — 25 г", 
            "• Спрайт — 130 г", 
            "• Сироп Гренадин — 20 г"
        ],
        "steps": [
            "1. В Олд Фэшн налить сироп, настойку и спрайт.", 
            "2. В снифтер налить самбуку и абсент, поджечь и прогреть.", 
            "3. Накрыть хостером, дать подышать парами.", 
            "4. Снова поджечь, перелить в Олд Фэшн, накрыв снифтером.", 
            "5. Отставить снифтер на блюдце, Олд Фэшн накрыть салфеткой/хостером,", 
            "ударьте о стойку (вспенить) и пить залпом, затем дать подышать парами."
        ]
    },
    "Жаба": {
        "num": "109", "ml": "225 мл",
        "ing": [
            "• Абсент — 25 г", 
            "• Самбука — 25 г", 
            "• Водка — 25 г", 
            "• Игристое вино — 130 г", 
            "• Сироп Мята — 20 г"
        ],
        "steps": [
            "1. В Олд Фэшн налить сироп, водку и игристое вино.", 
            "2. В снифтер налить самбуку и абсент, поджечь и прогреть.", 
            "3. Накрыть хостером, дать подышать парами.", 
            "4. Снова поджечь, перелить в Олд Фэшн, накрыв снифтером.", 
            "5. Отставить снифтер на блюдце, Олд Фэшн накрыть салфеткой/хостером,", 
            "ударьте о стойку (вспенить) и пить залпом, затем дать подышать парами."
        ]
    },
    "Алтайский хайбол": {
        "num": "110", "ml": "250 мл",
        "ing": [
            "• Алтайский бальзам — 40 мл",
            "• Яблочный сок — 120 мл",
            "• Имбирный эль — 80 мл",
            "• Сок лайма — 10 мл"
        ],
        "steps": [
            "1. Наполнить бокал хайболл кубиками льда.",
            "2. Налить алкоголь и соки, долить имбирный эль.",
            "3. Аккуратно перемешать барной ложкой и украсить."
        ]
    },
    "Сибирский экспресс": {
        "num": "111", "ml": "160 мл",
        "ing": [
            "• Клюквенный морс — 80 мл",
            "• Водка — 40 мл",
            "• Настойка Хаски клюква / Амг персик — 20 мл",
            "• Сахарный сироп — 10 мл",
            "• Сок лимона — 10 мл"
        ],
        "steps": [
            "1. Налить все ингредиенты в шейкер со льдом.",
            "2. Интенсивно взбить.",
            "3. Отцедить в бокал со льдом."
        ]
    },
    "Клубничный взрыв": {
        "num": "112", "ml": "230 мл",
        "ing": [
            "• Джин — 40 мл",
            "• Настойка АМГ Персик — 20 мл",
            "• Сироп Клубника — 20 мл",
            "• Сок лимона — 20 мл",
            "• Тоник — 130 мл"
        ],
        "steps": [
            "1. Наполнить бокал хайболл кубиками льда доверху.",
            "2. Налить джин, настойку персика, клубничный сироп и сок лимона.",
            "3. Долить тоник и аккуратно перемешать барной ложкой."
        ]
    },
    "Хиросима": {
        "num": "113", "ml": "50 мл",
        "ing": [
            "• Самбука — 15 мл",
            "• Ликер Бейлис (Baileys) — 15 мл",
            "• Абсент — 15 мл",
            "• Гренадён (сироп) — 3-5 капель"
        ],
        "steps": [
            "1. Налить самбуку в стопку.",
            "2. Аккуратно по барной ложке слоями уложить Бейлис.",
            "3. Сверху аккуратно влить абсент.",
            "4. Капнуть гренадин в центр (он создаст эффект «гриба»).",
            "5. Поджечь верхний слой по желанию и пить через трубочку."
        ]
    },
    "Облака": {
        "num": "114", "ml": "210 мл",
        "ing": [
            "• Самбука — 20 мл",
            "• Текила серебряная — 20 мл",
            "• Абсент — 15 мл",
            "• Блю Кюрасао (ликер) — 10 мл",
            "• Спрайт — 100 мл",
            "• Ликер Бейлис — 15 мл"
        ],
        "steps": [
            "1. В шейкере со льдом смешать крепкий алкоголь со спрайтом и ликерами.",
            "2. Перецедить в хайболл со льдом.",
            "3. Аккуратно влить сверху Бейлис, чтобы получился эффект облаков."
        ]
    },
    "Грейпфрутовый сауэр": {
        "num": "115", "ml": "160 мл",
        "ing": [
            "• Водка (или джин) — 50 мл",
            "• Свежевыжатый грейпфрутовый сок — 60 мл",
            "• Сахарный сироп — 20 мл",
            "• Сок лимона — 15 мл"
        ],
        "steps": [
            "1. Добавить все ингредиенты в шейкер со льдом.",
            "2. Энергично взбить.",
            "3. Отцедить в бокал рокс на свежий лед."
        ]
    },
    "Ягодный Колинз": {
        "num": "116", "ml": "240 мл",
        "ing": [
            "• Джин — 50 мл",
            "• Ягодное пюре (смородина/малина) — 30 мл",
            "• Сок лимона — 20 мл",
            "• Сахарный сироп — 15 мл",
            "• Содовая (или спрайт) — 100 мл"
        ],
        "steps": [
            "1. Положить ягодное пюре, сок лимона и сироп в шейкер.",
            "2. Добавить джин и лед, взбить.",
            "3. Перелить в хайболл со льдом и долить содовой."
        ]
    },
    "Зеленый мексиканц": {
        "num": "117", "ml": "50 мл",
        "ing": [
            "• Ликер Пибанг (Банан) — 20 мл",
            "• Ликер Бейлис — 15 мл",
            "• Сок лайма — 15 мл"
        ],
        "steps": [
            "1. Налить банановый ликер в стопку.",
            "2. Аккуратно по ложке уложить Бейлис.",
            "3. Сверху добавить сок лайма (кислота свернет сливки, создавая текстуру)."
        ]
    },
    "Унесенные ветром": {
        "num": "118", "ml": "120 мл",
        "ing": [
            "• Бурбон (или виски) — 40 мл",
            "• Персиковый ликер — 20 мл",
            "• Апельсиновый сок — 40 мл",
            "• Гренадин — 10 мл"
        ],
        "steps": [
            "1. Взбить все компоненты со льдом в шейкере.",
            "2. Отцедить в охлажденный бокал коктейльную рюмку или купе."
        ]
    },
    "Горящий Ламборгини": {
        "num": "119", "ml": "150 мл",
        "ing": [
            "• Самбука — 25 мл",
            "• Калуа (кофейный ликер) — 25 мл",
            "• Бейлис — 25 мл",
            "• Блю Кюрасао — 25 мл",
            "• Абсент — 25 мл"
        ],
        "steps": [
            "1. Налить Самбуку и Калуа в бокал для маргариты.",
            "2. В два бокала шота налить Бейлис и Блю Кюрасао.",
            "3. Поджечь основной бокал и залить сверху абсент.",
            "4. Пить через трубочку из шотов, пока горит."
        ]
    },
    "Зеленая фурия": {
        "num": "120", "ml": "180 мл",
        "ing": [
            "• Джин — 40 мл",
            "• Яблочный сок — 100 мл",
            "• Яблочный сироп — 5 мл",
            "• Сок лимона — 12 мл",
            "• Киви — 1 кусочек"
        ],
        "steps": [
            "1. Положить кусочек киви в шейкер и тщательно раздавить мадлером.",
            "2. Добавить джин, яблочный сок, яблочный сироп и сок лимона.",
            "3. Добавить лёд, энергично взбить в шейкере.",
            "4. Перецедить в бокал аллегра или красный винник."
        ]
    },
    "Старый кубинец": {
        "num": "121", "ml": "200 мл",
        "ing": [
            "• Пряный ром — 50 мл",
            "• Сахарный сироп — 15 мл",
            "• Сок лимона — 15 мл",
            "• Мята — 2 веточки",
            "• Игристое вино (сухое/полусладкое) — до верха"
        ],
        "steps": [
            "1. Положить мяту в шейкер, слегка примять.",
            "2. Добавить прямой ром, сахарный сироп, сок лимона и лёд.",
            "3. Интенсивно взбить в шейкере.",
            "4. Перелить в бокал олд фэшн.",
            "5. Долить до верха сухим или полусладким игристым вином."
        ]
    },
    "Синий кит": {
        "num": "122", "ml": "200 мл",
        "ing": [
            "• Водка — 50 мл",
            "• Ликер Блю Кюрасао — 25 мл",
            "• Самбука — 25 мл",
            "• Трипл Сек — 25 мл",
            "• Тоник — 100 мл"
        ],
        "steps": [
            "1. В Олд Фэшн налить водку, ликер Блю Кюрасао, Трипл Сек и тоник.",
            "2. В снифтер налить самбуку, поджечь и прогреть.",
            "3. Накрыть хостером, дать подышать парами.",
            "4. Снова поджечь, перелить в Олд Фэшн, накрыв снифтером.",
            "5. Отставить снифтер на блюдце, Олд Фэшн накрыть салфеткой/хостером,",
            "ударьте о стойку (вспенить) и пить залпом, затем дать подышать парами."
        ]
    },
    "Краски (на 1 сет)": {
        "num": "845", "ml": "180 мл",
        "ing": [
            "• Сок яблочный — 90 г",
            "• Водка «Пять Озер» — 40 г",
            "• Лимоны свежие — 33,3 г",
            "• Сироп «Гренадин» — 5 г",
            "• Сироп «Кюрасао Блю» — 5 г",
            "• Сироп «Зеленое яблоко» — 5 г",
            "• Сироп «Лимон» — 5 г",
            "• Мармелад радуга — 20 г"
        ],
        "steps": [
            "1. Лимоны перебирают, промывают проточной водой, обсушивают, нарезают дольками.",
            "2. Из дольки лимона выжимают сок с помощью сквизера в шейкер, затем наливают водку и сок.",
            "3. В 4 шота наливают сиропы (в каждый шот разный шот), затем аккуратно поверх сиропа наливают смесь из шейкера.",
            "4. Каждый шот украшают мармеладом.",
            "5. Коктейли готовят непосредственно перед подачей."
        ]
    },
    "Краски (на большую бутылку)": {
        "num": "124", "ml": "700 мл",
        "ing": [
            "• Водка — 200 мл",
            "• Сок яблочный — 450 мл",
            "• Сок лимона — 50 мл"
        ],
        "steps": [
            "1. Тщательно смешать ингредиенты в большой емкости (бутылке).",
            "2. Использовать для порционной выдачи."
        ]
    }
}

def create_single_card_image(name, data):
    filename = f"{name}.png"
    try:
        img = Image.new('RGB', (900, 750), color=(255, 255, 255))
        d = ImageDraw.Draw(img)
        
        font_title = get_font(18)
        font_text = get_font(15)
        
        d.text((50, 20), "Предприятия: ООО «__________________»", fill=(0, 0, 0), font=font_text)
        d.text((50, 50), "ШашлыкоFF (Коктейли)", fill=(0, 0, 0), font=font_text)
        d.text((50, 80), f"ТЕХНОЛОГИЧЕСКАЯ КАРТА № {data['num']}", fill=(0, 0, 0), font=font_title)
        d.text((50, 110), f"Наименование блюда: {name}", fill=(0, 0, 0), font=font_title)
        d.text((550, 110), f"Выход: {data['ml']}", fill=(0, 0, 0), font=font_title)
        
        d.line([(50, 145), (850, 145)], fill=(0, 0, 0), width=2)
        d.text((50, 155), "РЕЦЕПТУРА И ИНГРЕДИЕНТЫ:", fill=(0, 0, 0), font=font_title)
        
        y = 190
        for ing in data['ing']:
            d.text((70, y), ing, fill=(40, 40, 40), font=font_text)
            y += 26
            
        d.line([(50, y + 5), (850, y + 5)], fill=(0, 0, 0), width=2)
        
        y += 20
        d.text((50, y), "ТЕХНОЛОГИЯ ПРИГОТОВЛЕНИЯ:", fill=(0, 0, 0), font=font_title)
        y += 30
        for step in data['steps']:
            d.text((70, y), step, fill=(40, 40, 40), font=font_text)
            y += 26
            
        img.save(filename)
    except Exception as e:
        print(f"Ошибка генерации {name}: {e}")

def generate_cards_images():
    all_dict = {**CLASSIC_RECIPES, **AUTHOR_RECIPES}
    for name, data in all_dict.items():
        create_single_card_image(name, data)

def clean_name(raw_name):
    words = raw_name.split()
    filtered_words = [w for w in words if w.lower() not in ["page", "tk", "тк"]]
    cleaned = " ".join(filtered_words).strip()
    return cleaned if cleaned else raw_name

def import_and_get_cards():
    global CARDS_CACHE
    CARDS_CACHE.clear()
    generate_cards_images()
    
    cards = {}
    files = glob.glob("*.png") + glob.glob("*.PNG")
    idx = 0
    for file_path in files:
        base_name = os.path.basename(file_path)
        if base_name.lower() == "ttk_temp.png":
            continue
        raw_name = os.path.splitext(base_name)[0].replace("_", " ").strip()
        name = clean_name(raw_name)
        lower_name = name.lower()
        
        if any(c in lower_name for c in CLASSIC_NAMES):
            category = "classic"
        elif any(a in lower_name for a in AUTHOR_NAMES):
            category = "author"
        else:
            category = "other"
            
        cards[lower_name] = (name, file_path, category)
        CARDS_CACHE[str(idx)] = (name, file_path)
        idx += 1
        
    return cards

ITEMS_PER_PAGE = 6

def get_main_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add("🍸 Авторские", "🥃 Классические")
    markup.add("📋 Список всех ТК")
    return markup

def get_paged_keyboard(items_list, page=0, prefix="cat"):
    markup = types.InlineKeyboardMarkup(row_width=1)
    start_idx = page * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE
    current_page_items = items_list[start_idx:end_idx]
    
    for real_name, file_path, _ in current_page_items:
        card_id = None
        for cid, data in CARDS_CACHE.items():
            if data[1] == file_path:
                card_id = cid
                break
        if card_id is not None:
            markup.add(types.InlineKeyboardButton(text=f"🍸 {real_name}", callback_data=f"id_{card_id}"))
        
    nav_buttons = []
    if page > 0:
        nav_buttons.append(types.InlineKeyboardButton(text="⬅️ Назад", callback_data=f"{prefix}_page_{page - 1}"))
    if end_idx < len(items_list):
        nav_buttons.append(types.InlineKeyboardButton(text="Вперед ➡️", callback_data=f"{prefix}_page_{page + 1}"))
    if nav_buttons:
        markup.row(*nav_buttons)
    return markup

@bot.message_handler(commands=['start'])
def send_welcome(message):
    cards = import_and_get_cards()
    bot.send_message(
        message.chat.id, 
        f"Бот запущен на сервере! Загружено ТК: {len(cards)}.\nВыбери категорию в меню или напиши название коктейля текстом:", 
        reply_markup=get_main_keyboard()
    )

@bot.message_handler(func=lambda message: message.text == "📋 Список всех ТК")
def show_all_cards(message):
    cards_dict = import_and_get_cards()
    if not cards_dict:
        bot.send_message(message.chat.id, "❌ Картинки не найдены.")
        return
    items = list(cards_dict.values())
    total_pages = int((len(items) - 1) // ITEMS_PER_PAGE + 1)
    bot.send_message(message.chat.id, f"📋 **Все технологические карты** (Стр. 1 из {total_pages}):", reply_markup=get_paged_keyboard(items, 0, prefix="all"), parse_mode="Markdown")

@bot.message_handler(func=lambda message: message.text == "🍸 Авторские")
def show_author_cards(message):
    cards_dict = import_and_get_cards()
    items = [item for item in cards_dict.values() if item[2] == "author"]
    if not items:
        bot.send_message(message.chat.id, "❌ Пока нет авторских коктейлей.")
        return
        
    total_pages = int((len(items) - 1) // ITEMS_PER_PAGE + 1)
    bot.send_message(message.chat.id, f"🍸 **Авторские коктейли** (Стр. 1 из {total_pages}):", reply_markup=get_paged_keyboard(items, 0, prefix="cat_author"), parse_mode="Markdown")

@bot.message_handler(func=lambda message: message.text == "🥃 Классические")
def show_classic_cards(message):
    cards_dict = import_and_get_cards()
    items = [item for item in cards_dict.values() if item[2] == "classic"]
    if not items:
        bot.send_message(message.chat.id, "❌ Пока нет классических картинок.")
        return
        
    total_pages = int((len(items) - 1) // ITEMS_PER_PAGE + 1)
    bot.send_message(message.chat.id, f"🥃 **Классические коктейли** (Стр. 1 из {total_pages}):", reply_markup=get_paged_keyboard(items, 0, prefix="cat_classic"), parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: 'page' in call.data)
def handle_pagination(call):
    bot.answer_callback_query(call.id)
    parts = call.data.split('_')
    page = int(parts[-1])
    cards_dict = import_and_get_cards()
    
    if "all" in call.data:
        prefix = "all"
        cat_name = "Все технологические карты"
        items = list(cards_dict.values())
    elif "author" in call.data:
        prefix = "cat_author"
        cat_name = "Авторские коктейли"
        items = [item for item in cards_dict.values() if item[2] == "author"]
    else:
        prefix = "cat_classic"
        cat_name = "Классические коктейли"
        items = [item for item in cards_dict.values() if item[2] == "classic"]
        
    total_pages = int((len(items) - 1) // ITEMS_PER_PAGE + 1)
    
    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"📂 **{cat_name}** (Стр. {page + 1} из {total_pages}):",
            reply_markup=get_paged_keyboard(items, page, prefix=prefix),
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Ошибка пагинации: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith('id_'))
def handle_card_selection(call):
    card_id = call.data.replace('id_', '', 1)
    if card_id in CARDS_CACHE:
        real_name, file_path = CARDS_CACHE[card_id]
        if not os.path.exists(file_path):
            if real_name in AUTHOR_RECIPES:
                create_single_card_image(real_name, AUTHOR_RECIPES[real_name])
            elif real_name in CLASSIC_RECIPES:
                create_single_card_image(real_name, CLASSIC_RECIPES[real_name])
            
        if os.path.exists(file_path):
            bot.answer_callback_query(call.id, text=f"Отправка: {real_name}")
            with open(file_path, 'rb') as photo:
                bot.send_photo(call.message.chat.id, photo, caption=f"💥 Технологическая карта: **{real_name}**", parse_mode="Markdown")
        else:
            bot.answer_callback_query(call.id, text="Файл не найден на диске!", show_alert=True)
    else:
        bot.answer_callback_query(call.id, text="Устаревшая кнопка, вызовите список заново.", show_alert=True)

@bot.message_handler(func=lambda message: message.text and message.text.strip().lower() == "краски")
def send_paint_options(message):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("ТТК на 1 сет", callback_data="paint_set"),
        types.InlineKeyboardButton("ТТК на большую бутылку", callback_data="paint_bottle")
    )
    bot.send_message(message.chat.id, "🎨 Выберите вариант технологической карты для напитка «Краски»:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data in ["paint_set", "paint_bottle"])
def handle_paint_buttons(call):
    bot.answer_callback_query(call.id)
    if call.data == "paint_set":
        target_name = "Краски (на 1 сет)"
    else:
        target_name = "Краски (на большую бутылку)"
        
    file_path = f"{target_name}.png"
    
    if not os.path.exists(file_path) and target_name in AUTHOR_RECIPES:
        create_single_card_image(target_name, AUTHOR_RECIPES[target_name])
        
    if os.path.exists(file_path):
        with open(file_path, 'rb') as photo:
            bot.send_photo(call.message.chat.id, photo, caption=f"💥 Технологическая карта: **{target_name}**", parse_mode="Markdown")
    else:
        bot.send_message(call.message.chat.id, f"❌ Картинка для «{target_name}» не найдена.")

@bot.message_handler(func=lambda message: True)
def handle_text_search(message):
    query_words = message.text.strip().lower().split()
    cards_dict = import_and_get_cards()
    if not cards_dict:
        bot.send_message(message.chat.id, "❌ База картинок пуста.")
        return

    matched_cards = []
    for key, (real_name, file_path, _) in cards_dict.items():
        if all(word in key for word in query_words):
            matched_cards.append((real_name, file_path))

    if not matched_cards:
        bot.send_message(message.chat.id, f"❌ По запросу «{message.text}» ничего не найдено.")
        return

    if len(matched_cards) > 1:
        markup = types.InlineKeyboardMarkup(row_width=1)
        for real_name, file_path in matched_cards:
            card_id = None
            for cid, data in CARDS_CACHE.items():
                if data[1] == file_path:
                    card_id = cid
                    break
            
            lower_name = real_name.lower()
            if "без градуса" in lower_name or "безалкоголь" in lower_name:
                btn_text = f"🟢 {real_name} (Без градуса)"
            elif "дабл" in lower_name:
                btn_text = f"🥛 {real_name} (Двойной / Дабл)"
            else:
                btn_text = f"🍸 {real_name}"
            
            if card_id is not None:
                markup.add(types.InlineKeyboardButton(text=btn_text, callback_data=f"id_{card_id}"))
        
        bot.send_message(
            message.chat.id, 
            f"🔍 По запросу **«{message.text}»** найдено несколько вариантов. Выберите нужный:", 
            reply_markup=markup, 
            parse_mode="Markdown"
        )
    else:
        real_name, file_path = matched_cards[0]
        if not os.path.exists(file_path):
            if real_name in AUTHOR_RECIPES:
                create_single_card_image(real_name, AUTHOR_RECIPES[real_name])
            elif real_name in CLASSIC_RECIPES:
                create_single_card_image(real_name, CLASSIC_RECIPES[real_name])
            
        if os.path.exists(file_path):
            with open(file_path, 'rb') as photo:
                bot.send_photo(message.chat.id, photo, caption=f"💥 Технологическая карта: **{real_name}**", parse_mode="Markdown")

if __name__ == '__main__':
    print("Бот запущен на сервере...")
    bot.infinity_polling()
