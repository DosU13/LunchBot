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

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv('TOKEN')
CHECK_COLLECTION_GROUP_ID = int(os.getenv('CHECK_COLLECTION_GROUP_ID'))
MAIN_GROUP_ID = int(os.getenv('MAIN_GROUP_ID'))
BOT_USERNAME = os.getenv('BOT_USERNAME')
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', '').lstrip('@').lower()

DATA_FILE = 'orders.json'

CHOOSE_ITEM = 0
WAIT_CHECK = 1

CHECK_TIMEOUT_SECONDS = 15 * 60
CHECK_CAPTION = (
    'Пожалуйста, отправьте чек (фото или PDF) для подтверждения заказа.\n'
    'У вас есть 15 минут, иначе заказ будет отменён.\n'
    'Чтобы отменить заказ самостоятельно, используйте /cancel.'
)

menu: list[str] = []
# orders: {user_id: {"username": str, "items": [str, ...]}}
orders: dict[int, dict] = {}
ordering_open = True


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
            return
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


def load_orders():
    global orders
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        orders = {}
        for k, v in raw.items():
            if 'item' in v and 'items' not in v:
                v = {'username': v['username'], 'items': [v['item']]}
            orders[int(k)] = v


def save_orders():
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(orders, f, ensure_ascii=False, indent=2)


def record_order(user, item):
    username = user.full_name or user.username or str(user.id)
    if user.id not in orders:
        orders[user.id] = {'username': username, 'items': []}
    orders[user.id]['items'].append(item)
    save_orders()


@safe_handler
@owner_only
async def open_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not menu:
        await update.message.reply_text('Меню ещё не задано. Сначала используйте /set_menu.')
        return

    global orders, ordering_open
    orders = {}
    ordering_open = True
    save_orders()

    menu_text = '\n'.join(f'• {item}' for item in menu)
    text = f'Доброе утро! Сегодня меню: \n{menu_text}\nМожете заказать через бот — @{BOT_USERNAME}'
    await context.bot.send_message(chat_id=MAIN_GROUP_ID, text=text)
    await update.message.reply_text('Заказы сброшены. Сообщение отправлено в группу.')


@safe_handler
@owner_only
async def close_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ordering_open
    ordering_open = False

    await context.bot.send_message(chat_id=MAIN_GROUP_ID, text='Время заказа закончилось.')
    await update.message.reply_text('Приём заказов остановлен.')


@safe_handler
async def set_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global menu
    text = update.message.text.replace('/set_menu', '', 1)
    menu = [x.strip() for x in text.split('\n') if x.strip()]
    if menu:
        items = '\n'.join(f'• {item}' for item in menu)
        await update.message.reply_text(f'Меню обновлено:\n{items}')
    else:
        await update.message.reply_text('Меню пустое. Укажите блюда после команды.')


@safe_handler
async def order_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ordering_open:
        await update.message.reply_text('Время заказа закончилось.')
        return ConversationHandler.END

    if not menu:
        await update.message.reply_text('Меню ещё не задано. Дождитесь пока организатор установит меню.')
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton(item, callback_data=str(i))] for i, item in enumerate(menu)]
    await update.message.reply_text('Выберите блюдо:', reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_ITEM


@safe_handler
async def item_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    choice = int(query.data)
    if choice >= len(menu):
        await query.edit_message_text('Ошибка: блюдо не найдено. Попробуйте снова.')
        return ConversationHandler.END

    item = menu[choice]
    context.user_data['chosen_item'] = item
    await query.edit_message_text(f'Вы выбрали: {item}')

    if is_admin(update.effective_user):
        record_order(update.effective_user, item)
        await query.message.reply_text('Заказ принят без оплаты (админ).')
        return ConversationHandler.END

    await query.message.reply_photo(
        photo=open('QR.jpg', 'rb'),
        caption=CHECK_CAPTION,
    )
    return WAIT_CHECK


@safe_handler
async def remind_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_photo(
        photo=open('QR.jpg', 'rb'),
        caption=CHECK_CAPTION,
    )
    return WAIT_CHECK


@safe_handler
async def check_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    item = context.user_data.get('chosen_item', '?')
    username = user.full_name or user.username or str(user.id)

    record_order(user, item)

    await update.message.reply_text('Ваш заказ подтверждён! Спасибо.')

    caption = f'{username} — {item}'
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
    await update.message.reply_text('Заказ отменён.')
    return ConversationHandler.END


async def order_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text='Время на отправку чека истекло. Заказ отменён. Чтобы заказать снова, используйте /start.',
        )
    except Exception:
        logger.exception('Error in order_timeout')
    return ConversationHandler.END


@safe_handler
async def list_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not orders:
        await update.message.reply_text('Заказов пока нет.')
        return

    grouped: dict[str, list[str]] = defaultdict(list)
    for data in orders.values():
        for item in data['items']:
            grouped[item].append(data['username'])

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
    app.add_handler(order_conv)

    app.run_polling()


if __name__ == '__main__':
    main()
