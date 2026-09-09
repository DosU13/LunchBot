import functools
import json
import logging
import os
from collections import defaultdict

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    CallbackQueryHandler, MessageHandler, ConversationHandler, filters,
)

from menu_parser import parse_menu, DEFAULT_PRICE

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

CHOOSE_ITEM = 0
WAIT_CHECK = 1

CHECK_TIMEOUT_SECONDS = 15 * 60
GROUP_TYPES = ('group', 'supergroup')

# menu: [{'name': str, 'price': int}, ...]
menu: list[dict] = []
# orders: {user_id: {"username": str, "items": [{"name": str, "price": int}, ...]}}
orders: dict[int, dict] = {}
ordering_open = True


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
    for item in menu:
        if item['name'] == name:
            return item['price']
    return None


def normalize_items(raw_items) -> list[dict]:
    """Старый формат (список названий) приводим к {'name', 'price'}."""
    items = []
    for item in raw_items:
        if isinstance(item, str):
            items.append({'name': item, 'price': price_for(item)})
        else:
            items.append({'name': item.get('name', '?'), 'price': item.get('price')})
    return items


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


def menu_line(item: dict) -> str:
    return f"{item['name']} — {item['price']} сом"


# --- корзина -------------------------------------------------------------

def get_cart(context: ContextTypes.DEFAULT_TYPE) -> dict[int, int]:
    return context.user_data.setdefault('cart', {})


def cart_items(cart: dict[int, int]) -> list[dict]:
    """Разворачивает корзину в плоский список позиций (по одной на штуку)."""
    items = []
    for index, qty in cart.items():
        if index < len(menu):
            items.extend([dict(menu[index]) for _ in range(qty)])
    return items


def cart_total(cart: dict[int, int]) -> int:
    return sum(menu[i]['price'] * qty for i, qty in cart.items() if i < len(menu))


def cart_text(cart: dict[int, int]) -> str:
    if not cart:
        return 'Выберите блюда. Нажимайте на блюдо несколько раз, чтобы добавить его повторно.'
    lines = ['Ваш заказ:']
    for index, qty in cart.items():
        item = menu[index]
        lines.append(f"• {item['name']} ×{qty} — {item['price'] * qty} сом")
    lines.append(f'\nИтого: {cart_total(cart)} сом')
    lines.append('\nМожно добавить ещё или нажать «Готово».')
    return '\n'.join(lines)


def cart_keyboard(cart: dict[int, int]) -> InlineKeyboardMarkup:
    keyboard = []
    for index, item in enumerate(menu):
        qty = cart.get(index, 0)
        label = menu_line(item) + (f'  ×{qty}' if qty else '')
        row = []
        if qty:
            row.append(InlineKeyboardButton('➖', callback_data=f'del:{index}'))
        row.append(InlineKeyboardButton(label, callback_data=f'add:{index}'))
        keyboard.append(row)

    if cart:
        keyboard.append([
            InlineKeyboardButton('🗑 Очистить', callback_data='clear'),
            InlineKeyboardButton(f'✅ Готово · {cart_total(cart)} сом', callback_data='done'),
        ])
    return InlineKeyboardMarkup(keyboard)


def order_summary(items: list[dict]) -> str:
    grouped: dict[str, list[int]] = defaultdict(list)
    for item in items:
        grouped[item['name']].append(item['price'] or 0)
    lines = []
    for name, prices in grouped.items():
        lines.append(f'• {name} ×{len(prices)} — {sum(prices)} сом')
    lines.append(f'Итого: {sum(item["price"] or 0 for item in items)} сом')
    return '\n'.join(lines)


# --- команды администратора ---------------------------------------------

@safe_handler
@private_or_admin
@owner_only
async def open_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not menu:
        await update.message.reply_text('Меню ещё не задано. Сначала используйте /set_menu.')
        return

    global orders, ordering_open
    orders = {}
    ordering_open = True
    save_orders()

    menu_text = '\n'.join(f'• {menu_line(item)}' for item in menu)
    text = f'Меню: \n{menu_text}\nМожете заказать через бот — @{BOT_USERNAME}'
    await context.bot.send_message(chat_id=MAIN_GROUP_ID, text=text)
    await update.message.reply_text('Заказы сброшены. Сообщение отправлено в группу.')


@safe_handler
@private_or_admin
@owner_only
async def close_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ordering_open
    ordering_open = False

    await context.bot.send_message(chat_id=MAIN_GROUP_ID, text='Время заказа закончилось.')
    await update.message.reply_text('Приём заказов остановлен.')


@safe_handler
@private_or_admin
@owner_only
async def set_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global menu
    text = update.message.text.replace('/set_menu', '', 1)
    menu = parse_menu(text, FLAT_PRICE)
    if menu:
        items = '\n'.join(f'• {menu_line(item)}' for item in menu)
        await update.message.reply_text(f'Меню обновлено ({len(menu)} позиций):\n{items}')
    else:
        await update.message.reply_text('Меню пустое. Укажите блюда после команды.')


@safe_handler
@private_or_admin
async def list_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not orders:
        await update.message.reply_text('Заказов пока нет.')
        return

    grouped: dict[str, list[str]] = defaultdict(list)
    for data in orders.values():
        for item in data['items']:
            grouped[item['name']].append(data['username'])

    lines = []
    total = 0
    for item, names in grouped.items():
        count = len(names)
        total += count
        lines.append(f'{item} {count}')
        lines.extend(names)
        lines.append('')

    lines.append(f'Всего {total}')
    await update.message.reply_text('\n'.join(lines))


@safe_handler
@private_or_admin
@owner_only
async def total_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not orders:
        await update.message.reply_text('Заказов пока нет.')
        return

    grouped: dict[str, list[int | None]] = defaultdict(list)
    for data in orders.values():
        for item in data['items']:
            grouped[item['name']].append(item.get('price'))

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
    await update.message.reply_text('\n'.join(lines))


# --- заказ ---------------------------------------------------------------

@safe_handler
@private_or_admin
async def order_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ordering_open:
        await update.message.reply_text('Время заказа закончилось.')
        return ConversationHandler.END

    if not menu:
        await update.message.reply_text('Меню ещё не задано. Дождитесь пока организатор установит меню.')
        return ConversationHandler.END

    context.user_data['cart'] = {}
    cart = get_cart(context)
    await update.message.reply_text(cart_text(cart), reply_markup=cart_keyboard(cart))
    return CHOOSE_ITEM


@safe_handler
async def item_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    cart = get_cart(context)
    data = query.data

    if data == 'done':
        if not cart:
            await query.answer('Корзина пуста — выберите хотя бы одно блюдо.', show_alert=True)
            return CHOOSE_ITEM

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

        await query.message.reply_photo(photo=open('QR.jpg', 'rb'), caption=check_caption(total))
        return WAIT_CHECK

    if data == 'clear':
        if not cart:
            await query.answer('Корзина и так пуста.')
            return CHOOSE_ITEM
        cart.clear()
        await query.answer('Корзина очищена.')
    elif data.startswith('add:') or data.startswith('del:'):
        action, _, raw_index = data.partition(':')
        index = int(raw_index)
        if index >= len(menu):
            await query.answer()
            await query.edit_message_text('Меню изменилось. Начните заново: /start')
            return ConversationHandler.END

        if action == 'add':
            cart[index] = cart.get(index, 0) + 1
            await query.answer(f"Добавлено: {menu[index]['name']}")
        else:
            cart[index] = cart.get(index, 0) - 1
            if cart[index] <= 0:
                cart.pop(index, None)
            await query.answer(f"Убрано: {menu[index]['name']}")
    else:
        await query.answer()
        return CHOOSE_ITEM

    await query.edit_message_text(cart_text(cart), reply_markup=cart_keyboard(cart))
    return CHOOSE_ITEM


@safe_handler
async def remind_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = context.user_data.get('pending_items', [])
    total = sum(item['price'] or 0 for item in items)
    await update.message.reply_photo(photo=open('QR.jpg', 'rb'), caption=check_caption(total))
    return WAIT_CHECK


@safe_handler
async def check_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    items = context.user_data.get('pending_items', [])
    username = user.full_name or user.username or str(user.id)

    if not items:
        await update.message.reply_text('Заказ не найден. Начните заново: /start')
        return ConversationHandler.END

    record_order(user, items)
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
    context.user_data.clear()
    await update.message.reply_text('Заказ отменён.')
    return ConversationHandler.END


async def order_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
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


def main():
    load_orders()

    app = Application.builder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, log_update), group=-1)

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
    app.add_handler(CommandHandler('set_menu', set_menu))
    app.add_handler(CommandHandler('list', list_orders))
    app.add_handler(CommandHandler('total', total_orders))
    app.add_handler(order_conv)

    app.run_polling()


if __name__ == '__main__':
    main()
