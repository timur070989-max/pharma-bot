import os
import logging
import json
import re
import google.generativeai as genai
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler, CallbackQueryHandler
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

(BRAND, INN, FORM, AUDIENCE, GROUP, INDICATIONS, CHOOSE_STYLE, DONE) = range(8)

DOSAGE_FORMS = ["Таблетки", "Капсулы", "Суспензия/Сироп", "Инъекции", "Гель/Крем", "Капли", "Спрей"]
AUDIENCES = ["Взрослые (18+)", "Дети и родители", "Пожилые (55+)", "Все возрасты", "Врачи/Фармацевты"]

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

def get_gemini():
    genai.configure(api_key=GEMINI_API_KEY)
    return genai.GenerativeModel("gemini-2.0-flash")

def call_gemini(prompt):
    model = get_gemini()
    response = model.generate_content(prompt)
    text = response.text.strip()
    text = re.sub(r'```json|```', '', text).strip()
    return text

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "🎬 *PharmaAd AI — Генератор видеорекламы*\n\n"
        "Привет! Я помогу создать профессиональный сценарий видеорекламы для вашего препарата.\n\n"
        "📋 Нам понадобится:\n"
        "• Торговое название препарата\n"
        "• МНН (действующее вещество)\n"
        "• Лекарственная форма\n"
        "• Целевая аудитория\n"
        "• Показания и преимущества\n\n"
        "Начнём? Введите *торговое название* препарата:",
        parse_mode="Markdown"
    )
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

def generate_styles(data):
    prompt = f"""Ты эксперт по фармацевтической видеорекламе.

Препарат:
- Название: {data['brand']}
- МНН: {data['inn']}
- Форма: {data['form']}
- Аудитория: {data['audience']}
- Группа: {data['group']}
- Показания: {data['indications']}

Предложи 4 стиля видеорекламы. Верни ТОЛЬКО JSON массив без markdown:
[
  {{
    "name": "Название стиля",
    "score": 4.8,
    "audience": "Аудитория",
    "conversion": "Высокий",
    "why": "Почему подходит (1 предложение)"
  }}
]"""
    text = call_gemini(prompt)
    start = text.find('[')
    end = text.rfind(']')
    return json.loads(text[start:end+1])

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
        await send_script(query, script, context.user_data)
    except Exception as e:
        await query.message.reply_text(f"❌ Ошибка: {str(e)}\n\nПопробуйте: /start")
    return DONE

def generate_script(data):
    style = data['selected_style']
    prompt = f"""Создай профессиональный сценарий видеорекламы.

ПРЕПАРАТ: {data['brand']} ({data['inn']})
ФОРМА: {data['form']}
АУДИТОРИЯ: {data['audience']}
ПОКАЗАНИЯ: {data['indications']}
СТИЛЬ: {style['name']}

Верни ТОЛЬКО JSON без markdown:
{{
  "logline": "Главный слоган",
  "scenes": [
    {{
      "num": "Сцена 1",
      "duration": "0-5 сек",
      "visual": "Видеоряд",
      "voiceover": "Текст озвучки",
      "onscreen": "Текст на экране"
    }}
  ],
  "voiceover_full": "Полный текст озвучки",
  "cta": "Призыв к действию",
  "version_30": "Описание 30-сек версии",
  "version_15": "Описание 15-сек версии"
}}"""
    text = call_gemini(prompt)
    start = text.find('{')
    end = text.rfind('}')
    return json.loads(text[start:end+1])

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
        "✅ *Готово! Для нового препарата: /start*",
        parse_mode="Markdown"
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отменено. /start — начать заново.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            BRAND: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_brand)],
            INN: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_inn)],
            FORM: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_form)],
            AUDIENCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_audience)],
            GROUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_group)],
            INDICATIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_indications)],
            CHOOSE_STYLE: [CallbackQueryHandler(choose_style, pattern="^style_")],
            DONE: [CommandHandler("start", start)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
