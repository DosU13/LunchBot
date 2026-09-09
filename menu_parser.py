"""Разбор меню в двух форматах.

1. Меню с ценами: цена указана в конце строки ("350 сом", "150сом", "150сомов")
   или на следующей строке (см. "Ташкентский плов").
2. Меню без цен: каждая строка — блюдо по цене DEFAULT_PRICE.
"""
import re

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
