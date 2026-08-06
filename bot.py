import os
import logging
import asyncio
import tempfile
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler, CallbackQueryHandler
)
from video_pipeline import generate_styles, generate_script, assemble_video_sync

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

(BRAND, INN, FORM, AUDIENCE, GROUP, INDICATIONS, CHOOSE_STYLE, WAIT_PHOTO, DONE) = range(9)

DOSAGE_FORMS = ["Таблетки", "Капсулы", "Суспензия/Сироп", "Инъекции", "Гель/Крем", "Капли", "Спрей"]
AUDIENCES = ["Взрослые (18+)", "Дети и родители", "Пожилые (55+)", "Все возрасты", "Врачи/Фармацевты"]

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")


WELCOME_TEXT = (
    "🎬 *PharmaAd AI — Генератор видеорекламы*\n\n"
    "Привет! Я помогу создать профессиональный сценарий видеорекламы для вашего препарата.\n\n"
    "📋 Нам понадобится:\n"
    "• Торговое название препарата\n"
    "• МНН (действующее вещество)\n"
    "• Лекарственная форма\n"
    "• Целевая аудитория\n"
    "• Показания и преимущества\n\n"
    "Начнём? Введите *торговое название* препарата:"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(WELCOME_TEXT, parse_mode="Markdown")
    return BRAND


async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.message.reply_text(WELCOME_TEXT, parse_mode="Markdown")
    return BRAND


async def get_brand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['brand'] = update.message.text.strip()
    await update.message.reply_text(
        f"✅ Название: *{context.user_data['brand']}*\n\n"
        "Теперь введите *МНН* (действующее вещество):",
        parse_mode="Markdown"
    )
    return INN


async def get_inn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['inn'] = update.message.text.strip()
    keyboard = [[f] for f in DOSAGE_FORMS]
    await update.message.reply_text(
        f"✅ МНН: *{context.user_data['inn']}*\n\nВыберите *лекарственную форму*:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    return FORM


async def get_form(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['form'] = update.message.text.strip()
    keyboard = [[a] for a in AUDIENCES]
    await update.message.reply_text(
        f"✅ Форма: *{context.user_data['form']}*\n\nВыберите *целевую аудиторию*:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    return AUDIENCE


async def get_audience(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['audience'] = update.message.text.strip()
    await update.message.reply_text(
        f"✅ Аудитория: *{context.user_data['audience']}*\n\n"
        "Введите *фармакологическую группу*\n_(например: кишечный антисептик, витамин)_:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    return GROUP


async def get_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['group'] = update.message.text.strip()
    await update.message.reply_text(
        f"✅ Группа: *{context.user_data['group']}*\n\n"
        "Последний шаг! Опишите *показания и преимущества* препарата\n_(2-3 предложения)_:",
        parse_mode="Markdown"
    )
    return INDICATIONS


async def get_indications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['indications'] = update.message.text.strip()
    await update.message.reply_text(
        "🔍 *Анализирую препарат и подбираю стили рекламы...*\n\n⏳ 15–20 секунд",
        parse_mode="Markdown"
    )
    try:
        styles = generate_styles(context.user_data)
        context.user_data['styles'] = styles
        text = "🎨 *Готово! Выберите стиль видеорекламы:*\n\n"
        keyboard_buttons = []
        for i, s in enumerate(styles, 1):
            text += (
                f"*{i}. {s['name']}* — ⭐ {s['score']}/5\n"
                f"👥 {s['audience']}\n"
                f"📈 Конверсия: {s['conversion']}\n"
                f"💡 {s['why']}\n\n"
            )
            keyboard_buttons.append([InlineKeyboardButton(
                f"{i}. {s['name']} ⭐{s['score']}",
                callback_data=f"style_{i-1}"
            )])
        await update.message.reply_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard_buttons)
        )
        return CHOOSE_STYLE
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}\n\nПопробуйте: /start")
        return ConversationHandler.END


async def choose_style(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split('_')[1])
    style = context.user_data['styles'][idx]
    context.user_data['selected_style'] = style
    await query.edit_message_text(
        f"✅ Выбран стиль: *{style['name']}*\n\n🎬 *Генерирую сценарий...*\n⏳ 20–30 секунд",
        parse_mode="Markdown"
    )
    try:
        script = generate_script(context.user_data)
        context.user_data['script'] = script
        await send_script(query, script, context.user_data)
    except Exception as e:
        await query.message.reply_text(f"❌ Ошибка: {str(e)}\n\nПопробуйте: /start")
    return DONE


async def send_script(query, script, data):
    brand = data['brand']
    style = data['selected_style']['name']
    await query.message.reply_text(
        f"🎬 *Сценарий готов: {brand}*\n"
        f"🎨 Стиль: {style}\n\n"
        f"✨ *Слоган:*\n_{script.get('logline', '')}_\n\n"
        f"📢 *CTA:* {script.get('cta', '')}",
        parse_mode="Markdown"
    )
    scenes = script.get('scenes', [])
    if scenes:
        scenes_text = "🎥 *Сторибоард:*\n\n"
        for s in scenes:
            scenes_text += (
                f"*{s.get('num', '')}* ({s.get('duration', '')})\n"
                f"📹 {s.get('visual', '')}\n"
                f"🎙️ _{s.get('voiceover', '')}_\n"
                f"📝 {s.get('onscreen', '')}\n\n"
            )
        await query.message.reply_text(scenes_text, parse_mode="Markdown")
    vo = script.get('voiceover_full', '')
    if vo:
        await query.message.reply_text(
            f"🎙️ *Полный текст озвучки:*\n\n{vo}", parse_mode="Markdown"
        )
    await query.message.reply_text(
        f"⏱️ *Версия 30 сек:*\n{script.get('version_30', '')}\n\n"
        f"⚡ *Версия 15 сек:*\n{script.get('version_15', '')}\n\n"
        "✅ *Сценарий готов. Для нового препарата: /start*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🎬 Сгенерировать видео", callback_data="gen_video")
        ]])
    )


async def ask_for_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = context.user_data
    if 'script' not in data or 'selected_style' not in data:
        await query.message.reply_text("❌ Сначала сгенерируйте сценарий: /start")
        return DONE
    await query.message.reply_text(
        "📦 Пришлите фото упаковки препарата — вставлю его в финальную карточку видео "
        "вместо однотонного фона.\n\nЕсли фото нет — нажмите «Пропустить».",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Пропустить", callback_data="skip_photo")
        ]])
    )
    return WAIT_PHOTO


async def receive_package_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]
    file = await photo.get_file()
    fd, path = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    await file.download_to_drive(path)
    context.user_data['package_photo'] = path
    await run_video_generation(update.message, context)
    return DONE


async def skip_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['package_photo'] = None
    await run_video_generation(query.message, context)
    return DONE


async def run_video_generation(message, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data
    photo_path = data.get('package_photo')
    status_msg = await message.reply_text(
        "🎬 *Собираю видео из сториборда...*\n⏳ Обычно занимает 1–3 минуты",
        parse_mode="Markdown"
    )
    restart_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Новый препарат", callback_data="restart")]])
    out_path = None
    try:
        out_path = await asyncio.to_thread(assemble_video_sync, data, data['script'], photo_path)
        with open(out_path, "rb") as f:
            await message.reply_video(
                f, caption=f"🎬 {data['brand']} — {data['selected_style']['name']}\n"
                           "⚠️ Черновик на стоковых кадрах для внутренней оценки. "
                           "Требует юридической проверки перед публикацией.",
                reply_markup=restart_kb
            )
        await status_msg.delete()
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка генерации видео: {str(e)}")
        await message.reply_text("Попробовать другой препарат:", reply_markup=restart_kb)
    finally:
        if out_path and os.path.exists(out_path):
            os.remove(out_path)
        if photo_path and os.path.exists(photo_path):
            os.remove(photo_path)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отменено. /start — начать заново.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(restart, pattern="^restart$"),
        ],
        states={
            BRAND: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_brand)],
            INN: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_inn)],
            FORM: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_form)],
            AUDIENCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_audience)],
            GROUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_group)],
            INDICATIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_indications)],
            CHOOSE_STYLE: [CallbackQueryHandler(choose_style, pattern="^style_")],
            WAIT_PHOTO: [
                MessageHandler(filters.PHOTO, receive_package_photo),
                CallbackQueryHandler(skip_photo, pattern="^skip_photo$"),
            ],
            DONE: [
                CallbackQueryHandler(ask_for_photo, pattern="^gen_video$"),
                CallbackQueryHandler(restart, pattern="^restart$"),
                CommandHandler("start", start),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv)

    # --- Webhook-режим для бесплатного Render Web Service ---
    PORT = int(os.environ.get("PORT", 10000))
    RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")

    if not RENDER_URL:
        logger.warning(
            "RENDER_EXTERNAL_URL не задан — если запускаешь локально, "
            "используй run_polling() вместо run_webhook()."
        )

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=BOT_TOKEN,
        webhook_url=f"{RENDER_URL}/{BOT_TOKEN}"
    )


if __name__ == "__main__":
    main()
