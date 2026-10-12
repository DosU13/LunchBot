"""Разбор меню поставщиков.

Айзада (parse_menu), два формата:
1. Меню с ценами: цена указана в конце строки ("350 сом", "150сом", "150сомов")
   или на следующей строке (см. "Ташкентский плов").
2. Меню без цен: каждая строка — блюдо по цене DEFAULT_PRICE.

Азиза (parse_aziza_menu): меню разбито на разделы ("Завтрак", "Гарниры:").
Цена указана у блюда ("100с", "0,5- 50с", "35/40/60с") или одна на весь
раздел ("Блюда по *230с*"). Блюда получают поле 'category' — название раздела.
Блюда с выбором получают 'options' ([{'label', 'name'}]) и 'options_kind':
- 'garnish' — "(гарнир по желанию)", варианты берутся из раздела "Гарниры";
- 'variant' — одна цена на несколько вариантов: "Кофе 3в1, капучино,латте 30с";
- 'flavour' — вкусы в скобках у напитков: "Нитро 0,450-80с (лесные ягоды, мохито, яблоко)".
"""
import re
import unicodedata

DEFAULT_PRICE = 260

# "350 сом", "150сом", "220сомов", "200 som"
PRICE_RE = re.compile(r'(\d+)\s*(?:сом\w*|som\w*)', re.IGNORECASE)
# строка, состоящая только из цены: " 250сом"
PRICE_ONLY_RE = re.compile(r'^\W*\d+\s*(?:сом\w*|som\w*)\W*$', re.IGNORECASE)
# приветствия/заголовки/подписи, которые не являются блюдами
NOISE_RE = re.compile(
    r'^(доброе утро|добрый день|добрый вечер|здравствуйте|здравствуй|привет|'
    r'салам|ассалам|меню|заказы|приятного аппетита|спасибо|итого|всего)\b',
    re.IGNORECASE,
)
HAS_LETTER_RE = re.compile(r'[^\W\d_]')


def _strip_decorations(text: str) -> str:
    """Убирает ведущие маркеры (✅, •, -) и хвостовую пунктуацию."""
    text = re.sub(r'^[\W_]+', '', text)
    text = re.sub(r'[\s,;:.\-–—]+$', '', text)
    return text.strip()


def _split_price(line: str) -> tuple[str, int | None]:
    """Возвращает (название, цена) для строки с ценой в конце."""
    matches = list(PRICE_RE.finditer(line))
    if not matches:
        return _strip_decorations(line), None
    last = matches[-1]
    name = _strip_decorations(line[:last.start()] + line[last.end():])
    return name, int(last.group(1))


def _is_noise(name: str) -> bool:
    return not HAS_LETTER_RE.search(name) or bool(NOISE_RE.match(name))


def parse_menu(text: str, default_price: int = DEFAULT_PRICE) -> list[dict]:
    """Разбирает текст меню в список {'name': str, 'price': int}."""
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]

    parsed: list[tuple[str, int | None]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if PRICE_ONLY_RE.match(line):
            # цена без блюда перед ней — прицепляем к предыдущему, если у того цены нет
            if parsed and parsed[-1][1] is None:
                price = int(PRICE_RE.search(line).group(1))
                parsed[-1] = (parsed[-1][0], price)
            i += 1
            continue

        name, price = _split_price(line)
        if price is None and i + 1 < len(lines) and PRICE_ONLY_RE.match(lines[i + 1]):
            price = int(PRICE_RE.search(lines[i + 1]).group(1))
            i += 1
        parsed.append((name, price))
        i += 1

    priced_menu = any(price is not None for _, price in parsed)

    menu = []
    for name, price in parsed:
        if not name or _is_noise(name):
            continue
        if price is None:
            if priced_menu:
                # в меню с ценами строка без цены — это заголовок/приветствие
                continue
            price = default_price
        menu.append({'name': name, 'price': price})
    return menu


# --- Азиза ----------------------------------------------------------------

# "100с", "100сом", "35/40/60с" (варианты через слэш), "65 с (зеро)"; "0,5" не цена.
# "70ч" — опечатка («ч» рядом с «с» на клавиатуре)
AZ_PRICE_RE = re.compile(
    r'(?<![\d,.])(\d+(?:/\d+)*)(?:\s?сом\w*|с|\sс(?=\s*(?:\(|$))|ч)(?!\w)',
    re.IGNORECASE,
)
# цена на весь раздел: "Блюда по *230с*", "На 1- ое *180* с хлебом", "Салаты по 100с"
AZ_SECTION_PRICE_RE = re.compile(
    r'\*\s*(\d+)\s?(?:сом\w*|с)?\s*\*|\bпо\s+(\d+)\s?(?:сом\w*|с)(?!\w)',
    re.IGNORECASE,
)
# пункт списка: "1)Рис", "2. Гречка", "•Свежий"
AZ_BULLET_RE = re.compile(r'^(?:\d+\s*[).]|[•·▪●◦‣\-–—])\s*')
AZ_WEEKDAY_RE = re.compile(
    r'^(понедельник|вторник|среда|четверг|пятница|суббота|воскресенье)\b',
    re.IGNORECASE,
)
# "С курицей" в разделе "Пиццы:" → "Пиццы с курицей"
AZ_PREPOSITION_RE = re.compile(r'^(с|со|без)\s', re.IGNORECASE)
AZ_DEFAULT_CATEGORY = 'Другое'
# "Бифштекс с гарниром (гарнир по желанию)" — гарнир выбирают в подменю
AZ_GARNISH_CHOICE_RE = re.compile(r'гарнир\w*\s*\)?\s*\(?\s*(?:по\s+желанию|на\s+выбор)', re.IGNORECASE)
AZ_GARNISH_NOTE_RE = re.compile(r'\s*\(\s*гарнир\w*\s+(?:по\s+желанию|на\s+выбор)\s*\)|\s+(?:по\s+желанию|на\s+выбор)\b', re.IGNORECASE)
AZ_GARNISH_SECTION_RE = re.compile(r'^гарнир', re.IGNORECASE)
# запятые вне скобок: "Кофе 3в1, капучино,латте", но не "Салат (огурец, помидор)" и не "0,5"
AZ_COMMA_RE = re.compile(r'(?:(?<!\d),|,(?!\d))(?![^(]*\))')
AZ_LIST_SEPARATOR_RE = re.compile(r'(?:(?<!\d),|,(?!\d))(?![^(]*\))|;|\s+(?:и|или)\s+', re.IGNORECASE)
# список в скобках: "(персик,манго ананас)". Вкусы — только у напитков:
# у блюд в скобках обычно состав ("Салат (огурцы, помидоры)"), его не выбирают
AZ_PAREN_LIST_RE = re.compile(r'\(([^()]*,[^()]*)\)')
AZ_DRINKS_SECTION_RE = re.compile(r'напит', re.IGNORECASE)


def _drop_emoji(text: str) -> str:
    text = ''.join(
        ch for ch in text
        if unicodedata.category(ch) not in ('So', 'Sk', 'Cf', 'Co', 'Cs')
        and ch not in '︎️⃣'
    )
    return re.sub(r'\s+', ' ', text).strip()


def _nz_name(text: str, category: str, section_price: int | None) -> str:
    name = _strip_decorations(re.sub(r'\s+', ' ', text))
    if name.count('(') > name.count(')'):
        name += ')'
    if section_price is None and category != AZ_DEFAULT_CATEGORY and AZ_PREPOSITION_RE.match(name):
        name = f'{category} {name[0].lower()}{name[1:]}'
    return name


def _comma_parts(name: str) -> list[str]:
    return [part.strip() for part in AZ_COMMA_RE.split(name) if part.strip()]


def _expand_variants(parts: list[str]) -> list[str]:
    """["Булочки пустышка", "со сгущёнкой"] → ["Булочки пустышка", "Булочки со сгущёнкой"]."""
    base = parts[0].split()[0]
    return [parts[0]] + [part if part[0].isupper() else f'{base} {part}' for part in parts[1:]]


def _split_variants(name: str, prices: list[int]) -> list[tuple[str, int]]:
    """"Булочки пустышка, со сгущёнкой, с творогом 35/40/60с" → три позиции."""
    if len(prices) == 1:
        return [(name, prices[0])]
    parts = _comma_parts(name)
    if len(parts) != len(prices):
        return [(f'{name} ({price}с)', price) for price in prices]
    return list(zip(_expand_variants(parts), prices))


def _comma_options(name: str) -> list[tuple[str, str]]:
    """"Кофе 3в1, капучино,латте" (одна цена на всё) → "Кофе 3в1", "Кофе капучино", "Кофе латте".

    Короткие части (до двух слов), чтобы не резать обычные названия с запятой.
    Возвращает [(подпись кнопки, название позиции)].
    """
    parts = _comma_parts(name)
    if len(parts) < 2 or any(len(part.split()) > 2 for part in parts):
        return []
    return [(variant, variant) for variant in _expand_variants(parts)]


def _flavour_options(name: str) -> list[tuple[str, str]]:
    """"Фьюз 0,5- (персик,манго ананас)" → ("Персик", "Фьюз 0,5 (персик)"), ("Манго ананас", ...)."""
    match = AZ_PAREN_LIST_RE.search(name)
    if not match:
        return []
    flavours = _comma_parts(match.group(1))
    if len(flavours) < 2 or any(len(flavour.split()) > 3 for flavour in flavours):
        return []
    base = _strip_decorations(re.sub(r'\s+', ' ', f'{name[:match.start()]} {name[match.end():]}'))
    return [(flavour[0].upper() + flavour[1:], f'{base} ({flavour})') for flavour in flavours]


def _list_items(text: str) -> list[str]:
    """"рис, гречка и пюре" → ["Рис", "Гречка", "Пюре"]."""
    items = []
    for part in AZ_LIST_SEPARATOR_RE.split(text):
        part = _strip_decorations(part or '')
        if part and HAS_LETTER_RE.search(part):
            items.append(part[0].upper() + part[1:])
    return items


def wants_garnish(name: str) -> bool:
    return bool(AZ_GARNISH_CHOICE_RE.search(name))


def _menu_item(name: str, price: int, category: str, split_options: bool = True) -> dict:
    item = {'name': name, 'price': price, 'category': category}
    if not split_options or AZ_GARNISH_SECTION_RE.match(category):
        return item
    options, kind = _comma_options(name), 'variant'
    if not options and AZ_DRINKS_SECTION_RE.search(category):
        options, kind = _flavour_options(name), 'flavour'
    if options:
        item['options'] = [{'label': label, 'name': option_name} for label, option_name in options]
        item['options_kind'] = kind
    return item


def _add_garnish_options(menu: list[dict], garnishes: list[str]):
    """Блюдам "(гарнир по желанию)" — подменю с гарнирами из раздела "Гарниры"."""
    garnishes = garnishes + [
        garnish
        for item in menu if AZ_GARNISH_SECTION_RE.match(item['category'])
        for garnish in _list_items(item['name'])
    ]
    unique = {}
    for garnish in garnishes:
        unique.setdefault(garnish.lower(), garnish)
    if not unique:
        return
    for item in menu:
        if wants_garnish(item['name']):
            base = AZ_GARNISH_NOTE_RE.sub('', item['name']).strip()
            item['options'] = [{'label': garnish, 'name': f'{base} ({key})'} for key, garnish in unique.items()]
            item['options_kind'] = 'garnish'


def parse_aziza_menu(text: str) -> list[dict]:
    """Разбирает меню Азизы в список {'name', 'price', 'category'} (+ 'options', 'options_kind')."""
    menu: list[dict] = []
    # гарниры, перечисленные без цены, — только варианты для блюд "по желанию"
    garnishes: list[str] = []
    category = AZ_DEFAULT_CATEGORY
    section_price: int | None = None
    last_was_item = False

    for raw in text.splitlines():
        raw = raw.strip()
        bulleted = bool(AZ_BULLET_RE.match(raw))
        line = _drop_emoji(AZ_BULLET_RE.sub('', raw, count=1))
        if not line:
            continue

        # "(лаза+кетчуп)" отдельной строкой — примечание к предыдущему блюду
        if line.startswith('(') and line.endswith(')') and not AZ_PRICE_RE.search(line):
            if last_was_item:
                menu[-1]['name'] += f' {line}'
            continue

        section = AZ_SECTION_PRICE_RE.search(line)
        if section:
            category = _strip_decorations(line.replace('*', ''))
            section_price = int(section.group(1) or section.group(2))
            last_was_item = False
            continue

        line = line.replace('*', '')
        matches = list(AZ_PRICE_RE.finditer(line))
        if matches:
            if section_price is not None and not bulleted:
                # в разделе с общей ценой блюда идут списком — значит, раздел закончился
                category = AZ_DEFAULT_CATEGORY
                section_price = None
            last = matches[-1]
            before = line[:last.start()].rstrip(' -–—')
            name = _nz_name(f'{before} {line[last.end():]}', category, section_price)
            prices = [int(price) for price in last.group(1).split('/')]
            for variant, price in _split_variants(name, prices):
                menu.append(_menu_item(variant, price, category, split_options=len(prices) == 1))
            last_was_item = True
        elif bulleted and section_price is None and AZ_GARNISH_SECTION_RE.match(category):
            garnishes.extend(_list_items(line))
            last_was_item = False
        elif bulleted:
            # пункт без своей цены берёт цену раздела, иначе его не заказать
            last_was_item = section_price is not None
            if last_was_item:
                name = _nz_name(line, category, section_price)
                menu.append(_menu_item(name, section_price, category))
        else:
            # строка без цены и без маркера — заголовок раздела
            last_was_item = False
            name = _strip_decorations(line)
            if not name or _is_noise(name) or AZ_WEEKDAY_RE.match(name):
                continue
            head, _, rest = name.partition(':')
            if AZ_GARNISH_SECTION_RE.match(head) and rest.strip():
                # "Гарниры: рис, гречка, пюре" — список в одну строку
                garnishes.extend(_list_items(rest))
                name = _strip_decorations(head)
            category = name
            section_price = None

    menu = [item for item in menu if item['name']]
    _add_garnish_options(menu, garnishes)
    return menu
