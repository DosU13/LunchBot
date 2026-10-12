import functools
import json
import logging
import os
import time
from collections import defaultdict

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    CallbackQueryHandler, MessageHandler, ConversationHandler, filters,
)

from menu_parser import parse_menu, parse_aziza_menu, wants_garnish, DEFAULT_PRICE

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv('TOKEN')
CHECK_COLLECTION_GROUP_ID = int(os.getenv('CHECK_COLLECTION_GROUP_ID'))
MAIN_GROUP_ID = int(os.getenv('MAIN_GROUP_ID'))
BOT_USERNAME = os.getenv('BOT_USERNAME')
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', '').lstrip('@').lower()
FLAT_PRICE = int(os.getenv('DEFAULT_PRICE', DEFAULT_PRICE))

DATA_FILE = 'orders.json'
PENDING_FILE = 'pending.json'

# заказ (/start)
CHOOSE_ITEM = 0
WAIT_CHECK = 1
# установка меню (/set_menu)
CHOOSE_MENU_SUPPLIER = 2
WAIT_MENU_TEXT = 3

CHECK_TIMEOUT_SECONDS = 15 * 60
MENU_TEXT_TIMEOUT_SECONDS = 10 * 60
# Как часто напоминать про чек, пока заказ не оплачен.
CHECK_REMINDER_SECONDS = int(os.getenv('CHECK_REMINDER_SECONDS', 60))
GROUP_TYPES = ('group', 'supergroup')
# Telegram не принимает сообщения длиннее 4096 символов.
MAX_MESSAGE_LEN = 4000

SUPPLIERS = {
    'aizada': 'Айзада',
    'aziza': 'Азиза',
}
# заказы, сохранённые до появления второго поставщика, — это Айзада
DEFAULT_SUPPLIER = 'aizada'
# после выбора поставщика эти действия доступны только админу (/list и /pending — всем в личке)
ADMIN_ACTIONS = ('open', 'close', 'total')

# menus: {поставщик: [{'name': str, 'price': int, 'category': str (только у Азизы),
#                      'options': [{'label': str, 'name': str}], 'options_kind': 'garnish' | 'variant' | 'flavour'}, ...]}
# options — подменю: гарнир, вариант ("Кофе 3в1, капучино,латте") или вкус напитка; цена у всех вариантов одна
menus: dict[str, list[dict]] = {key: [] for key in SUPPLIERS}
# orders: {user_id: {"username": str, "items": [{"name": str, "price": int, "supplier": str}, ...]}}
orders: dict[int, dict] = {}
# pending: заказы, которые ждут чек
# {user_id: {"username": str, "chat_id": int, "items": [...], "started_at": float}}
pending: dict[int, dict] = {}
# приём заказов открывается и закрывается отдельно у каждого поставщика
ordering_open: dict[str, bool] = {key: True for key in SUPPLIERS}


def check_caption(total: int) -> str:
    return (
        f'К оплате: {total} сом\n'
        'Пожалуйста, отправьте чек (фото или PDF) для подтверждения заказа.\n'
        'У вас есть 15 минут, иначе заказ будет отменён.\n'
        'Чтобы отменить заказ самостоятельно, используйте /cancel.'
    )


def is_admin(user) -> bool:
    if user is None or not user.username or not ADMIN_USERNAME:
        return False
    return user.username.lower() == ADMIN_USERNAME


def owner_only(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update.effective_user):
            if update.effective_message:
                await update.effective_message.reply_text('У вас нет доступа к этой команде.')
            return ConversationHandler.END
        return await func(update, context)
    return wrapper


def private_or_admin(func):
    """В группах команды доступны только админу, остальных отправляем в личку бота."""
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        if chat and chat.type in GROUP_TYPES and not is_admin(update.effective_user):
            if update.effective_message:
                await update.effective_message.reply_text(
                    f'Здесь команды доступны только организатору.\n'
                    f'Напишите боту в личные сообщения — @{BOT_USERNAME}'
                )
            return ConversationHandler.END
        return await func(update, context)
    return wrapper


def safe_handler(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            return await func(update, context)
        except Exception:
            logger.exception('Error in handler %s', func.__name__)
            if update.effective_message:
                try:
                    await update.effective_message.reply_text('Произошла ошибка. Попробуйте позже.')
                except Exception:
                    pass
            return ConversationHandler.END
    return wrapper


def price_for(name: str) -> int | None:
    for item in menus[DEFAULT_SUPPLIER]:
        if item['name'] == name:
            return item['price']
    return None


def normalize_items(raw_items) -> list[dict]:
    """Старые форматы (список названий, позиции без поставщика) приводим к {'name', 'price', 'supplier'}."""
    items = []
    for item in raw_items:
        if isinstance(item, str):
            item = {'name': item, 'price': price_for(item)}
        supplier = item.get('supplier')
        items.append({
            'name': item.get('name', '?'),
            'price': item.get('price'),
            'supplier': supplier if supplier in SUPPLIERS else DEFAULT_SUPPLIER,
        })
    return items


def items_of(items: list[dict], supplier: str) -> list[dict]:
    return [item for item in items if item.get('supplier', DEFAULT_SUPPLIER) == supplier]


def load_orders():
    global orders
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        orders = {}
        for k, v in raw.items():
            raw_items = v.get('items') or ([v['item']] if 'item' in v else [])
            orders[int(k)] = {'username': v['username'], 'items': normalize_items(raw_items)}


def save_orders():
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(orders, f, ensure_ascii=False, indent=2)


def record_order(user, items: list[dict]):
    username = user.full_name or user.username or str(user.id)
    if user.id not in orders:
        orders[user.id] = {'username': username, 'items': []}
    orders[user.id]['username'] = username
    orders[user.id]['items'].extend(items)
    save_orders()


# --- неоплаченные заказы (ждём чек) --------------------------------------

def load_pending():
    global pending
    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        pending = {int(k): {**v, 'items': normalize_items(v.get('items', []))} for k, v in raw.items()}


def save_pending():
    with open(PENDING_FILE, 'w', encoding='utf-8') as f:
        json.dump(pending, f, ensure_ascii=False, indent=2)


def add_pending(user, chat_id: int, items: list[dict]):
    pending[user.id] = {
        'username': user.full_name or user.username or str(user.id),
        'chat_id': chat_id,
        'items': items,
        'started_at': time.time(),
    }
    save_pending()


def drop_pending(user_id: int):
    if pending.pop(user_id, None) is not None:
        save_pending()


def reminder_job_name(user_id: int) -> str:
    return f'check_reminder:{user_id}'


def cancel_reminder(job_queue, user_id: int):
    if job_queue is None:
        return
    for job in job_queue.get_jobs_by_name(reminder_job_name(user_id)):
        job.schedule_removal()


def schedule_reminder(job_queue, user_id: int, chat_id: int):
    """Напоминаем про чек каждые CHECK_REMINDER_SECONDS, пока заказ не оплачен."""
    if job_queue is None:
        logger.warning('JobQueue недоступна, напоминания о чеке отключены.')
        return
    cancel_reminder(job_queue, user_id)
    job_queue.run_repeating(
        check_reminder_job,
        interval=CHECK_REMINDER_SECONDS,
        first=CHECK_REMINDER_SECONDS,
        name=reminder_job_name(user_id),
        chat_id=chat_id,
        user_id=user_id,
        data={'user_id': user_id},
    )


async def check_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    user_id = context.job.data['user_id']
    entry = pending.get(user_id)
    if not entry:
        context.job.schedule_removal()
        return

    left = CHECK_TIMEOUT_SECONDS - (time.time() - entry['started_at'])
    if left <= 0:
        drop_pending(user_id)
        context.job.schedule_removal()
        return

    total = sum(item.get('price') or 0 for item in entry['items'])
    minutes_left = max(1, int(left // 60))
    try:
        await context.bot.send_message(
            chat_id=entry['chat_id'],
            text=(
                f'Напоминание: заказ на {total} сом ещё не подтверждён.\n'
                'Отправьте, пожалуйста, чек (фото или PDF).\n'
                f'Осталось примерно {minutes_left} мин, потом заказ отменится.\n'
                'Отменить самому — /cancel.'
            ),
        )
    except Exception:
        logger.exception('Не смог отправить напоминание пользователю %s', user_id)


def pending_report(supplier: str) -> str:
    entries = [(entry, items_of(entry['items'], supplier)) for entry in pending.values()]
    entries = [(entry, items) for entry, items in entries if items]
    if not entries:
        return 'Все, кто заказал, отправили чек. 🎉'

    now = time.time()
    lines = ['Ждут чек:', '']
    total_sum = 0
    for entry, items in sorted(entries, key=lambda e: e[0]['started_at']):
        total = sum(item.get('price') or 0 for item in items)
        total_sum += total
        waiting = int((now - entry['started_at']) // 60)
        names = ', '.join(f"{item['name']}" for item in items)
        lines.append(f"• {entry['username']} — {total} сом (ждём {waiting} мин)")
        lines.append(f'  {names}')
        lines.append('')

    lines.append(f'Всего: {len(entries)} чел., {total_sum} сом')
    return '\n'.join(lines)


# --- меню ----------------------------------------------------------------

def menu_line(item: dict) -> str:
    return f"{item['name']} — {item['price']} сом"


def parse_supplier_menu(supplier: str, text: str) -> list[dict]:
    if supplier == 'aziza':
        return parse_aziza_menu(text)
    return parse_menu(text, FLAT_PRICE)


def menu_categories(supplier: str) -> list[str]:
    """Разделы меню по порядку; пусто, если меню без разделов (Айзада)."""
    categories = []
    for item in menus[supplier]:
        category = item.get('category')
        if category and category not in categories:
            categories.append(category)
    return categories


def options_report(supplier: str) -> str:
    """Какие блюда открывают подменю — чтобы организатор проверил разбор меню."""
    grouped: dict[str, list[str]] = defaultdict(list)
    missing = []
    for item in menus[supplier]:
        options = item.get('options')
        if options:
            grouped[', '.join(option['label'] for option in options)].append(item['name'])
        elif wants_garnish(item['name']):
            missing.append(item['name'])

    lines = []
    if grouped:
        lines.append('Выбор в подменю:')
        for labels, names in grouped.items():
            where = names[0] if len(names) == 1 else f'{len(names)} поз.'
            lines.append(f'• {where}: {labels}')
    if missing:
        lines.append('Не нашёл раздел «Гарниры» — эти блюда добавятся без выбора гарнира:')
        lines.extend(f'• {name}' for name in missing)
    return '\n'.join(lines)


def menu_text(supplier: str) -> str:
    lines = []
    category = None
    for item in menus[supplier]:
        if item.get('category') and item['category'] != category:
            category = item['category']
            if lines:
                lines.append('')
            lines.append(f'{category}:')
        lines.append(f'• {menu_line(item)}')
    return '\n'.join(lines)


def available_suppliers() -> list[str]:
    """Поставщики, у которых сейчас можно заказать."""
    return [supplier for supplier in SUPPLIERS if menus[supplier] and ordering_open[supplier]]


def split_text(text: str, limit: int = MAX_MESSAGE_LEN) -> list[str]:
    """Режет длинный текст по строкам на куски, которые пропустит Telegram."""
    chunks = []
    current = ''
    for line in text.split('\n'):
        candidate = f'{current}\n{line}' if current else line
        if len(candidate) > limit and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


async def send_long(bot, chat_id: int, text: str):
    for chunk in split_text(text):
        await bot.send_message(chat_id=chat_id, text=chunk)


# --- корзина -------------------------------------------------------------
# cart: {(поставщик, индекс блюда в его меню, индекс варианта или None): количество}
# view в user_data: None — выбор поставщика,
#   иначе (поставщик, индекс раздела или None, индекс блюда с подменю или None)

CartKey = tuple[str, int, int | None]
OPTION_HINTS = {'garnish': 'Выберите гарнир.', 'flavour': 'Выберите вкус.'}


def get_cart(context: ContextTypes.DEFAULT_TYPE) -> dict[CartKey, int]:
    return context.user_data.setdefault('cart', {})


def item_options(supplier: str, index: int) -> list[dict]:
    if index >= len(menus[supplier]):
        return []
    return menus[supplier][index].get('options') or []


def resolve_item(supplier: str, index: int, option: int | None) -> dict | None:
    """Блюдо из корзины с учётом выбранного варианта; None, если меню изменилось."""
    if supplier not in SUPPLIERS or index >= len(menus[supplier]):
        return None
    item = menus[supplier][index]
    options = item.get('options') or []
    if option is None:
        return None if options else item
    if option >= len(options):
        return None
    return {**item, 'name': options[option]['name']}


def current_view(context: ContextTypes.DEFAULT_TYPE) -> tuple[str, str | None, int | None] | None:
    """Где пользователь в меню: None — список поставщиков, иначе (поставщик, раздел или None, блюдо с подменю или None)."""
    view = context.user_data.get('view')
    if view is None:
        return None
    supplier, category_index, item_index = view
    if supplier not in SUPPLIERS or not menus[supplier]:
        return None
    categories = menu_categories(supplier)
    category = None
    if category_index is not None and category_index < len(categories):
        category = categories[category_index]
    if item_index is not None and not item_options(supplier, item_index):
        item_index = None
    return supplier, category, item_index


def cart_entries(cart: dict[CartKey, int]) -> list[tuple[str, int, dict, int]]:
    """Позиции корзины, которые ещё есть в меню: (поставщик, индекс, блюдо, количество)."""
    entries = []
    for (supplier, index, option), qty in cart.items():
        item = resolve_item(supplier, index, option)
        if item is not None:
            entries.append((supplier, index, item, qty))
    entries.sort(key=lambda entry: list(SUPPLIERS).index(entry[0]))
    return entries


def cart_items(cart: dict[CartKey, int]) -> list[dict]:
    """Разворачивает корзину в плоский список позиций (по одной на штуку)."""
    items = []
    for supplier, _, item, qty in cart_entries(cart):
        items.extend(
            {'name': item['name'], 'price': item['price'], 'supplier': supplier} for _ in range(qty)
        )
    return items


def cart_total(cart: dict[CartKey, int]) -> int:
    return sum(item['price'] * qty for _, _, item, qty in cart_entries(cart))


def with_qty(label: str, qty: int) -> str:
    return label + (f'  ×{qty}' if qty else '')


def cart_text(cart: dict[CartKey, int], view: tuple[str, str | None, int | None] | None = None) -> str:
    if view is None:
        header, hint = '', 'Выберите поставщика.'
    else:
        supplier, category, item_index = view
        path = [SUPPLIERS[supplier]] + ([category] if category else [])
        if item_index is not None:
            item = menus[supplier][item_index]
            path.append(menu_line(item))
            hint = OPTION_HINTS.get(item.get('options_kind'), 'Выберите вариант.')
        elif menu_categories(supplier) and category is None:
            hint = 'Выберите раздел меню.'
        else:
            hint = 'Выберите блюда. Нажимайте на блюдо несколько раз, чтобы добавить его повторно.'
        header = ' › '.join(path) + '\n\n'
        if item_index is not None and cart:
            # в подменю подсказку показываем всегда: кнопки — это гарниры/вкусы, а не блюда
            header += hint + '\n\n'
    if not cart:
        return header + hint

    lines = ['Ваш заказ:']
    shown = None
    for supplier, _, item, qty in cart_entries(cart):
        if supplier != shown:
            shown = supplier
            lines.append(f'{SUPPLIERS[supplier]}:')
        lines.append(f"• {item['name']} ×{qty} — {item['price'] * qty} сом")
    lines.append(f'\nИтого: {cart_total(cart)} сом')
    lines.append('\nМожно добавить ещё или нажать «Готово».')
    return header + '\n'.join(lines)


def cart_keyboard(cart: dict[CartKey, int], view: tuple[str, str | None, int | None] | None = None) -> InlineKeyboardMarkup:
    entries = cart_entries(cart)
    keyboard = []
    if view is None:
        for supplier in available_suppliers():
            qty = sum(q for s, _, _, q in entries if s == supplier)
            keyboard.append([InlineKeyboardButton(with_qty(SUPPLIERS[supplier], qty), callback_data=f'sup:{supplier}')])
        bottom = [InlineKeyboardButton('🗑 Очистить', callback_data='clear')] if cart else []
    else:
        supplier, category, item_index = view
        categories = menu_categories(supplier)
        if item_index is not None:
            # подменю блюда: гарнир или вариант
            for option_index, option in enumerate(item_options(supplier, item_index)):
                qty = cart.get((supplier, item_index, option_index), 0)
                row = []
                if qty:
                    row.append(InlineKeyboardButton('➖', callback_data=f'del:{supplier}:{item_index}:{option_index}'))
                row.append(InlineKeyboardButton(
                    with_qty(option['label'], qty), callback_data=f'add:{supplier}:{item_index}:{option_index}',
                ))
                keyboard.append(row)
        elif categories and category is None:
            # большое меню (Азиза): сначала выбираем раздел
            for category_index, name in enumerate(categories):
                qty = sum(q for s, _, item, q in entries if s == supplier and item.get('category') == name)
                keyboard.append([InlineKeyboardButton(with_qty(name, qty), callback_data=f'cat:{category_index}')])
        else:
            for index, item in enumerate(menus[supplier]):
                if category is not None and item.get('category') != category:
                    continue
                if item.get('options'):
                    qty = sum(q for s, i, _, q in entries if s == supplier and i == index)
                    keyboard.append([InlineKeyboardButton(
                        with_qty(menu_line(item), qty) + ' ›', callback_data=f'opt:{supplier}:{index}',
                    )])
                    continue
                qty = cart.get((supplier, index, None), 0)
                row = []
                if qty:
                    row.append(InlineKeyboardButton('➖', callback_data=f'del:{supplier}:{index}'))
                row.append(InlineKeyboardButton(with_qty(menu_line(item), qty), callback_data=f'add:{supplier}:{index}'))
                keyboard.append(row)
        if item_index is not None:
            back = '⬅️ Назад'
        else:
            back = '⬅️ Разделы' if category is not None else '⬅️ Поставщики'
        bottom = [InlineKeyboardButton(back, callback_data='back')]

    if cart:
        bottom.append(InlineKeyboardButton(f'✅ Готово · {cart_total(cart)} сом', callback_data='done'))
    if bottom:
        keyboard.append(bottom)
    return InlineKeyboardMarkup(keyboard)


def order_summary(items: list[dict]) -> str:
    lines = []
    for supplier, title in SUPPLIERS.items():
        grouped: dict[str, list[int]] = defaultdict(list)
        for item in items_of(items, supplier):
            grouped[item['name']].append(item['price'] or 0)
        if not grouped:
            continue
        lines.append(f'{title}:')
        for name, prices in grouped.items():
            lines.append(f'• {name} ×{len(prices)} — {sum(prices)} сом')
    lines.append(f'Итого: {sum(item["price"] or 0 for item in items)} сом')
    return '\n'.join(lines)


# --- отчёты по поставщику ------------------------------------------------

def price_label(price: int | None) -> str:
    return f'{price} сом' if price is not None else 'цена неизвестна'


def list_report(supplier: str) -> str:
    customers = [(data['username'], items_of(data['items'], supplier)) for data in orders.values()]
    customers = [(username, items) for username, items in customers if items]
    has_pending = any(items_of(entry['items'], supplier) for entry in pending.values())
    if not customers and not has_pending:
        return 'Заказов пока нет.'

    lines = []
    if not customers:
        lines.append('Подтверждённых заказов пока нет.')
    elif supplier == 'aziza':
        # Азиза получает список по людям, каждая порция — отдельной строкой: «×3» легко не заметить
        for username, items in customers:
            names = [item['name'] for item in items]
            items = sorted(items, key=lambda item: names.index(item['name']))  # одинаковые блюда рядом
            lines.append(username)
            lines.extend(f"• {item['name']} — {price_label(item.get('price'))}" for item in items)
            lines.append('')
        all_items = [item for _, items in customers for item in items]
        lines.append(f'Всего {len(all_items)}')
        lines.append(f"Сумма: {sum(item.get('price') or 0 for item in all_items)} сом")
    else:
        grouped: dict[str, list[tuple[str, int | None]]] = defaultdict(list)
        for username, items in customers:
            for item in items:
                grouped[item['name']].append((username, item.get('price')))

        count = 0
        total = 0
        for item, orders_of_item in grouped.items():
            subtotal = sum(price or 0 for _, price in orders_of_item)
            count += len(orders_of_item)
            total += subtotal
            lines.append(f'{item} {len(orders_of_item)} — {subtotal} сом')
            lines.extend(username for username, _ in orders_of_item)
            lines.append('')

        lines.append(f'Всего {count}')
        lines.append(f'Сумма: {total} сом')

    # дополнительно: кто заказал, но ещё не прислал чек
    if has_pending:
        lines.append('')
        lines.append('———')
        lines.append(pending_report(supplier))
    return '\n'.join(lines)


def total_report(supplier: str) -> str:
    grouped: dict[str, list[int | None]] = defaultdict(list)
    for data in orders.values():
        for item in items_of(data['items'], supplier):
            grouped[item['name']].append(item.get('price'))
    if not grouped:
        return 'Заказов пока нет.'

    lines = []
    total = 0
    count = 0
    unknown = 0
    for name, prices in grouped.items():
        subtotal = sum(price for price in prices if price is not None)
        missing = sum(1 for price in prices if price is None)
        unknown += missing
        total += subtotal
        count += len(prices)
        suffix = ' (цена неизвестна)' if missing else ''
        lines.append(f'{name} ×{len(prices)} — {subtotal} сом{suffix}')

    lines.append('')
    lines.append(f'Порций: {count}')
    lines.append(f'Общая сумма: {total} сом')
    if unknown:
        lines.append(f'Внимание: у {unknown} позиций нет цены, они не учтены в сумме.')
    return '\n'.join(lines)


async def open_supplier(context: ContextTypes.DEFAULT_TYPE, supplier: str) -> str:
    title = SUPPLIERS[supplier]
    if not menus[supplier]:
        return f'Меню ({title}) ещё не задано. Сначала используйте /set_menu.'

    ordering_open[supplier] = True
    # сбрасываем заказы только этого поставщика
    for user_id in list(orders):
        orders[user_id]['items'] = [item for item in orders[user_id]['items'] if item['supplier'] != supplier]
        if not orders[user_id]['items']:
            del orders[user_id]
    save_orders()

    for user_id, entry in list(pending.items()):
        if items_of(entry['items'], supplier):
            cancel_reminder(context.job_queue, user_id)
            drop_pending(user_id)

    text = f'Меню ({title}):\n{menu_text(supplier)}\nМожете заказать через бот — @{BOT_USERNAME}'
    await send_long(context.bot, MAIN_GROUP_ID, text)
    return 'Заказы сброшены. Сообщение отправлено в группу.'


async def close_supplier(context: ContextTypes.DEFAULT_TYPE, supplier: str) -> str:
    ordering_open[supplier] = False
    await context.bot.send_message(chat_id=MAIN_GROUP_ID, text=f'{SUPPLIERS[supplier]}: время заказа закончилось.')
    return 'Приём заказов остановлен.'


# --- команды с выбором поставщика ----------------------------------------

def supplier_picker(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(title, callback_data=f'{prefix}:{supplier}') for supplier, title in SUPPLIERS.items()
    ]])


async def ask_supplier(update: Update, action: str):
    await update.message.reply_text('Выберите поставщика:', reply_markup=supplier_picker(f'pick:{action}'))


@safe_handler
@private_or_admin
@owner_only
async def open_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ask_supplier(update, 'open')


@safe_handler
@private_or_admin
@owner_only
async def close_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ask_supplier(update, 'close')


@safe_handler
@private_or_admin
async def list_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ask_supplier(update, 'list')


@safe_handler
@private_or_admin
async def pending_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ask_supplier(update, 'pending')


@safe_handler
@private_or_admin
@owner_only
async def total_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ask_supplier(update, 'total')


@safe_handler
async def supplier_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка поставщика после /open_orders, /close_orders, /list, /pending, /total."""
    query = update.callback_query
    _, action, supplier = query.data.split(':')
    in_group = update.effective_chat.type in GROUP_TYPES
    if (action in ADMIN_ACTIONS or in_group) and not is_admin(update.effective_user):
        await query.answer('Эта кнопка только для организатора.', show_alert=True)
        return
    await query.answer()
    if supplier not in SUPPLIERS:
        return

    await query.edit_message_text(f'Поставщик: {SUPPLIERS[supplier]}')
    if action == 'open':
        text = await open_supplier(context, supplier)
    elif action == 'close':
        text = await close_supplier(context, supplier)
    elif action == 'list':
        text = list_report(supplier)
    elif action == 'pending':
        text = pending_report(supplier)
    elif action == 'total':
        text = total_report(supplier)
    else:
        return
    await send_long(context.bot, update.effective_chat.id, text)


# --- установка меню: /set_menu → поставщик → текст меню ------------------

@safe_handler
@private_or_admin
@owner_only
async def set_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Чьё меню устанавливаем?', reply_markup=supplier_picker('setmenu'))
    return CHOOSE_MENU_SUPPLIER


@safe_handler
async def set_menu_supplier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(update.effective_user):
        await query.answer('Меню может менять только организатор.', show_alert=True)
        return ConversationHandler.END

    await query.answer()
    supplier = query.data.partition(':')[2]
    if supplier not in SUPPLIERS:
        return ConversationHandler.END

    context.user_data['menu_supplier'] = supplier
    await query.edit_message_text(
        f'Поставщик: {SUPPLIERS[supplier]}.\n'
        'Теперь отправьте текст меню одним сообщением. Отмена — /cancel.'
    )
    return WAIT_MENU_TEXT


@safe_handler
async def set_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    supplier = context.user_data.get('menu_supplier')
    if supplier not in SUPPLIERS:
        await update.message.reply_text('Не понял, чьё это меню. Начните заново: /set_menu')
        return ConversationHandler.END

    parsed = parse_supplier_menu(supplier, update.message.text)
    if not parsed:
        await update.message.reply_text('Не нашёл блюд в этом тексте. Отправьте меню ещё раз или /cancel.')
        return WAIT_MENU_TEXT

    menus[supplier] = parsed
    context.user_data.pop('menu_supplier', None)
    await update.message.reply_text(f'Меню обновлено — {SUPPLIERS[supplier]} ({len(parsed)} позиций):')
    await send_long(context.bot, update.effective_chat.id, menu_text(supplier))
    options = options_report(supplier)
    if options:
        await send_long(context.bot, update.effective_chat.id, options)
    return ConversationHandler.END


@safe_handler
async def set_menu_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('menu_supplier', None)
    await update.message.reply_text('Установка меню отменена.')
    return ConversationHandler.END


# --- заказ ---------------------------------------------------------------

@safe_handler
@private_or_admin
async def order_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not available_suppliers():
        if any(menus.values()):
            await update.message.reply_text('Время заказа закончилось.')
        else:
            await update.message.reply_text('Меню ещё не задано. Дождитесь пока организатор установит меню.')
        return ConversationHandler.END

    context.user_data['cart'] = {}
    context.user_data['view'] = None
    cart = get_cart(context)
    await update.message.reply_text(cart_text(cart), reply_markup=cart_keyboard(cart))
    return CHOOSE_ITEM


async def menu_changed(query) -> int:
    await query.answer()
    await query.edit_message_text('Меню изменилось. Начните заново: /start')
    return ConversationHandler.END


@safe_handler
async def item_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    cart = get_cart(context)
    data = query.data
    action, _, arg = data.partition(':')

    if data == 'done':
        closed = [s for s in SUPPLIERS if s not in available_suppliers() and any(key[0] == s for key in cart)]
        if closed:
            # пока человек выбирал, организатор закрыл приём у этого поставщика
            for key in [key for key in cart if key[0] in closed]:
                del cart[key]
            context.user_data['view'] = None
            names = ', '.join(SUPPLIERS[s] for s in closed)
            await query.answer(f'Приём заказов закрыт ({names}). Эти блюда убраны из корзины.', show_alert=True)
        elif not cart:
            await query.answer('Корзина пуста — выберите хотя бы одно блюдо.', show_alert=True)
            return CHOOSE_ITEM
        else:
            await query.answer()
            items = cart_items(cart)
            context.user_data['pending_items'] = items
            total = cart_total(cart)
            await query.edit_message_text(f'Ваш заказ:\n{order_summary(items)}')

            if is_admin(update.effective_user):
                record_order(update.effective_user, items)
                context.user_data.clear()
                await query.message.reply_text('Заказ принят без оплаты (админ).')
                return ConversationHandler.END

            add_pending(update.effective_user, query.message.chat_id, items)
            schedule_reminder(context.job_queue, update.effective_user.id, query.message.chat_id)
            await query.message.reply_photo(photo=open('QR.jpg', 'rb'), caption=check_caption(total))
            return WAIT_CHECK

    elif data == 'clear':
        if not cart:
            await query.answer('Корзина и так пуста.')
            return CHOOSE_ITEM
        cart.clear()
        await query.answer('Корзина очищена.')

    elif data == 'back':
        await query.answer()
        view = context.user_data.get('view')
        if view is None:
            return CHOOSE_ITEM
        supplier, category_index, item_index = view
        if item_index is not None:
            context.user_data['view'] = (supplier, category_index, None)
        elif category_index is not None:
            context.user_data['view'] = (supplier, None, None)
        else:
            context.user_data['view'] = None

    elif action == 'sup':
        if arg not in available_suppliers():
            await query.answer(f'{SUPPLIERS.get(arg, arg)}: приём заказов закрыт.', show_alert=True)
            context.user_data['view'] = None
        else:
            await query.answer()
            if context.user_data.get('view') == (arg, None, None):
                # повторное нажатие — сообщение уже такое, Telegram не даст его «изменить»
                return CHOOSE_ITEM
            context.user_data['view'] = (arg, None, None)

    elif action == 'cat':
        view = context.user_data.get('view')
        category_index = int(arg)
        if view is None:
            await query.answer()
            return CHOOSE_ITEM
        if category_index >= len(menu_categories(view[0])):
            return await menu_changed(query)
        await query.answer()
        if view[1:] == (category_index, None):
            return CHOOSE_ITEM
        context.user_data['view'] = (view[0], category_index, None)

    elif action == 'opt':
        # блюдо с подменю: гарнир на выбор или вариант
        supplier, _, raw_index = arg.partition(':')
        index = int(raw_index)
        if supplier not in SUPPLIERS or not item_options(supplier, index):
            return await menu_changed(query)
        await query.answer()
        view = context.user_data.get('view')
        category_index = view[1] if view is not None and view[0] == supplier else None
        if view == (supplier, category_index, index):
            return CHOOSE_ITEM
        context.user_data['view'] = (supplier, category_index, index)

    elif action in ('add', 'del'):
        # add:поставщик:блюдо или add:поставщик:блюдо:вариант
        supplier, raw_index, *raw_option = arg.split(':')
        index = int(raw_index)
        option = int(raw_option[0]) if raw_option else None
        item = resolve_item(supplier, index, option)
        if item is None:
            return await menu_changed(query)

        key = (supplier, index, option)
        name = item['name']
        if action == 'add' and supplier not in available_suppliers():
            await query.answer(f'{SUPPLIERS[supplier]}: приём заказов закрыт.', show_alert=True)
            context.user_data['view'] = None
        elif action == 'add':
            cart[key] = cart.get(key, 0) + 1
            await query.answer(f'Добавлено: {name}')
            view = context.user_data.get('view')
            if option is not None and view is not None:
                # гарнир выбран — возвращаемся к списку блюд
                context.user_data['view'] = (view[0], view[1], None)
        else:
            cart[key] = cart.get(key, 0) - 1
            if cart[key] <= 0:
                cart.pop(key, None)
            await query.answer(f'Убрано: {name}')

    else:
        await query.answer()
        return CHOOSE_ITEM

    view = current_view(context)
    await query.edit_message_text(cart_text(cart, view), reply_markup=cart_keyboard(cart, view))
    return CHOOSE_ITEM


@safe_handler
async def remind_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    items = context.user_data.get('pending_items') or (pending.get(user_id) or {}).get('items', [])
    total = sum(item.get('price') or 0 for item in items)
    await update.message.reply_photo(photo=open('QR.jpg', 'rb'), caption=check_caption(total))
    return WAIT_CHECK


@safe_handler
async def check_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # после перезапуска бота user_data пустая — берём заказ из pending.json
    items = context.user_data.get('pending_items') or (pending.get(user.id) or {}).get('items', [])
    username = user.full_name or user.username or str(user.id)

    if not items:
        await update.message.reply_text('Заказ не найден. Начните заново: /start')
        return ConversationHandler.END

    record_order(user, items)
    cancel_reminder(context.job_queue, user.id)
    drop_pending(user.id)
    context.user_data.clear()

    await update.message.reply_text('Ваш заказ подтверждён! Спасибо.')

    caption = f'{username}\n{order_summary(items)}'
    if update.message.photo:
        await context.bot.send_photo(
            chat_id=CHECK_COLLECTION_GROUP_ID,
            photo=update.message.photo[-1].file_id,
            caption=caption,
        )
    elif update.message.document:
        await context.bot.send_document(
            chat_id=CHECK_COLLECTION_GROUP_ID,
            document=update.message.document.file_id,
            caption=caption,
        )

    return ConversationHandler.END


@safe_handler
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_reminder(context.job_queue, update.effective_user.id)
    drop_pending(update.effective_user.id)
    context.user_data.clear()
    await update.message.reply_text('Заказ отменён.')
    return ConversationHandler.END


async def order_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.effective_user:
            cancel_reminder(context.job_queue, update.effective_user.id)
            drop_pending(update.effective_user.id)
        context.user_data.clear()
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text='Время на отправку чека истекло. Заказ отменён. Чтобы заказать снова, используйте /start.',
        )
    except Exception:
        logger.exception('Error in order_timeout')
    return ConversationHandler.END


async def log_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info('Incoming update: %s', update)


async def restore_reminders(app: Application):
    """После перезапуска возобновляем напоминания по незакрытым заказам."""
    now = time.time()
    for user_id, entry in list(pending.items()):
        if now - entry['started_at'] >= CHECK_TIMEOUT_SECONDS:
            drop_pending(user_id)
            continue
        schedule_reminder(app.job_queue, user_id, entry['chat_id'])


def main():
    load_orders()
    load_pending()

    app = Application.builder().token(TOKEN).post_init(restore_reminders).build()
    app.add_handler(MessageHandler(filters.ALL, log_update), group=-1)

    set_menu_supplier_handler = CallbackQueryHandler(set_menu_supplier, pattern=r'^setmenu:')
    set_menu_conv = ConversationHandler(
        # кнопка поставщика — тоже точка входа: работает и после таймаута, и при смене поставщика
        entry_points=[CommandHandler('set_menu', set_menu), set_menu_supplier_handler],
        states={
            CHOOSE_MENU_SUPPLIER: [set_menu_supplier_handler],
            WAIT_MENU_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_menu_text)],
        },
        fallbacks=[CommandHandler('cancel', set_menu_cancel)],
        conversation_timeout=MENU_TEXT_TIMEOUT_SECONDS,
        allow_reentry=True,
    )

    order_conv = ConversationHandler(
        entry_points=[CommandHandler('start', order_start)],
        states={
            CHOOSE_ITEM: [CallbackQueryHandler(item_chosen)],
            WAIT_CHECK: [
                MessageHandler(filters.PHOTO | filters.Document.ALL, check_received),
                MessageHandler(filters.ALL & ~filters.COMMAND, remind_check),
            ],
            ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, order_timeout)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        conversation_timeout=CHECK_TIMEOUT_SECONDS,
    )

    app.add_handler(CommandHandler('open_orders', open_orders))
    app.add_handler(CommandHandler('close_orders', close_orders))
    app.add_handler(CommandHandler('list', list_orders))
    app.add_handler(CommandHandler('pending', pending_orders))
    app.add_handler(CommandHandler('total', total_orders))
    # set_menu_conv и выбор поставщика — до order_conv: иначе их кнопки перехватит корзина
    app.add_handler(set_menu_conv)
    app.add_handler(CallbackQueryHandler(supplier_picked, pattern=r'^pick:'))
    app.add_handler(order_conv)

    app.run_polling()


if __name__ == '__main__':
    main()
