import os
import html
import hashlib
import logging
import json
import re
import asyncio
import tempfile
import subprocess
import requests
import edge_tts
from google import genai
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler, CallbackQueryHandler
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

(BRAND, INN, FORM, AUDIENCE, GROUP, INDICATIONS, CHOOSE_STYLE, WAIT_PHOTO, DONE) = range(9)

DOSAGE_FORMS = ["Таблетки", "Капсулы", "Суспензия/Сироп", "Инъекции", "Гель/Крем", "Капли", "Спрей"]
AUDIENCES = ["Взрослые (18+)", "Дети и родители", "Пожилые (55+)", "Все возрасты", "Врачи/Фармацевты"]

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")
# Comma-separated Telegram user IDs allowed to use the bot. Every request spends
# the Gemini/Pexels quota and minutes of CPU on ffmpeg, so leaving the bot open
# to anyone who finds it is costly. Empty = open to everyone (with a warning).
ALLOWED_USER_IDS = {
    int(x) for x in re.split(r"[,\s]+", os.environ.get("ALLOWED_USER_IDS", "")) if x.strip().isdigit()
}
# Video assembly is CPU/RAM heavy; the free Render instance can't run several at once.
MAX_PARALLEL_VIDEOS = 1

TEXT_MODEL = "gemini-3.5-flash-lite"
TTS_VOICE = "ru-RU-SvetlanaNeural"
VIDEO_W, VIDEO_H = 480, 854
VIDEO_FPS = 25
FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "DejaVuSans-Bold.ttf")
BG_MUSIC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "bg_music.mp3")

client = genai.Client(api_key=GEMINI_API_KEY)


def call_gemini(prompt):
    response = client.models.generate_content(model=TEXT_MODEL, contents=prompt)
    text = response.text.strip()
    text = re.sub(r'```json|```', '', text).strip()
    return text


def run_ffmpeg(args):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


def escape_drawtext(text):
    return (text or "").replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace("%", "\\%")


def wrap_lines(text, font_size, max_width=VIDEO_W):
    chars_per_line = max(1, int(max_width * 0.82 / (font_size * 0.62)))
    words = (text or "").split()
    lines, current = [], ""
    for w in words:
        candidate = f"{current} {w}".strip()
        if len(candidate) > chars_per_line and current:
            lines.append(current)
            current = w
        else:
            current = candidate
    if current:
        lines.append(current)
    return "\n".join(lines) if lines else " "


def write_textfile(tmpdir, name, text):
    path = os.path.join(tmpdir, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return escape_drawtext(path)


async def synthesize_tts(text, out_path):
    text = (text or "").strip()
    if not text:
        run_ffmpeg(["-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "2", "-c:a", "libmp3lame", out_path])
        return
    last_err = None
    for attempt in range(3):
        try:
            await edge_tts.Communicate(text, TTS_VOICE).save(out_path)
            return
        except Exception as e:
            last_err = e
            await asyncio.sleep(1.5)
    raise last_err


def fetch_pexels_video(query, out_path):
    if not PEXELS_API_KEY:
        raise RuntimeError("PEXELS_API_KEY не задан")
    resp = requests.get(
        "https://api.pexels.com/videos/search",
        headers={"Authorization": PEXELS_API_KEY},
        params={"query": query, "per_page": 5, "orientation": "portrait"},
        timeout=20,
    )
    resp.raise_for_status()
    results = resp.json().get("videos", [])
    if not results:
        raise RuntimeError(f"Pexels: нет видео по запросу «{query}»")
    files = sorted(results[0]["video_files"], key=lambda f: f.get("width", 0))
    target = next((f for f in files if f.get("width", 0) >= VIDEO_W), files[-1])
    with requests.get(target["link"], stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                f.write(chunk)


def build_scene_clip(tmpdir, idx, scene):
    raw_video = os.path.join(tmpdir, f"raw_{idx}.mp4")
    voice_path = os.path.join(tmpdir, f"voice_{idx}.mp3")
    clip_path = os.path.join(tmpdir, f"scene_{idx}.mp4")

    query = (scene.get("keywords") or "").strip() or "pharmacy healthcare medicine"
    fetch_pexels_video(query, raw_video)
    asyncio.run(synthesize_tts(scene.get("voiceover", ""), voice_path))

    onscreen = scene.get("onscreen", "").strip()
    vf = f"scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=increase,crop={VIDEO_W}:{VIDEO_H}"
    if onscreen:
        textfile = write_textfile(tmpdir, f"onscreen_{idx}.txt", wrap_lines(onscreen, 40))
        vf += (
            f",drawtext=fontfile='{FONT_PATH}':textfile='{textfile}':"
            "fontcolor=white:fontsize=40:line_spacing=6:box=1:boxcolor=black@0.5:boxborderw=16:"
            "x=(w-text_w)/2:y=h-text_h-60"
        )

    run_ffmpeg([
        "-stream_loop", "-1", "-i", raw_video,
        "-i", voice_path,
        "-vf", vf,
        "-r", str(VIDEO_FPS),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
        "-c:a", "aac", "-ar", "44100", "-ac", "1",
        "-shortest",
        clip_path,
    ])
    os.remove(raw_video)
    os.remove(voice_path)
    return clip_path


def build_outro_clip(tmpdir, data, script, photo_path=None):
    outro_path = os.path.join(tmpdir, "outro.mp4")

    if photo_path:
        photo_h = int(VIDEO_H * 0.55)
        y_brand = photo_h + 40
        y_cta = y_brand + 100
        brand_file = write_textfile(tmpdir, "outro_brand.txt", wrap_lines(data['brand'], 48))
        cta_file = write_textfile(tmpdir, "outro_cta.txt", wrap_lines(script.get('cta', ''), 30))
        args = [
            "-f", "lavfi", "-i", f"color=c=0x1a1a2e:s={VIDEO_W}x{VIDEO_H}",
            "-loop", "1", "-i", photo_path,
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-filter_complex",
            f"[1:v]scale={VIDEO_W}:{photo_h}:force_original_aspect_ratio=decrease[pic];"
            f"[0:v][pic]overlay=(W-w)/2:({photo_h}-h)/2[bg];"
            f"[bg]drawtext=fontfile='{FONT_PATH}':textfile='{brand_file}':fontcolor=white:fontsize=48:line_spacing=6:"
            f"x=(w-text_w)/2:y={y_brand},"
            f"drawtext=fontfile='{FONT_PATH}':textfile='{cta_file}':fontcolor=white:fontsize=30:line_spacing=6:"
            f"x=(w-text_w)/2:y={y_cta}[outv]",
            "-map", "[outv]", "-map", "2:a:0",
        ]
    else:
        brand_file = write_textfile(tmpdir, "outro_brand.txt", wrap_lines(data['brand'], 56))
        cta_file = write_textfile(tmpdir, "outro_cta.txt", wrap_lines(script.get('cta', ''), 34))
        args = [
            "-f", "lavfi", "-i", f"color=c=0x1a1a2e:s={VIDEO_W}x{VIDEO_H}",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-vf",
            f"drawtext=fontfile='{FONT_PATH}':textfile='{brand_file}':fontcolor=white:fontsize=56:line_spacing=6:"
            f"x=(w-text_w)/2:y=h*0.30,"
            f"drawtext=fontfile='{FONT_PATH}':textfile='{cta_file}':fontcolor=white:fontsize=34:line_spacing=6:"
            f"x=(w-text_w)/2:y=h*0.48",
        ]

    run_ffmpeg([
        *args,
        "-r", str(VIDEO_FPS),
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
        "-c:a", "aac", "-ar", "44100", "-ac", "1",
        "-t", "2.5",
        outro_path,
    ])
    return outro_path


def concat_clips(tmpdir, clip_paths, output_path):
    list_path = os.path.join(tmpdir, "concat_list.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for p in clip_paths:
            f.write(f"file '{p}'\n")
    run_ffmpeg(["-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", output_path])


def add_background_music(dry_path, output_path):
    run_ffmpeg([
        "-i", dry_path,
        "-stream_loop", "-1", "-i", BG_MUSIC_PATH,
        "-filter_complex",
        "[0:a]volume=1.0[voice];[1:a]volume=0.15[bgm];"
        "[voice][bgm]amix=inputs=2:duration=first:dropout_transition=0[aout]",
        "-map", "0:v:0", "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac", "-ar", "44100", "-ac", "1",
        output_path,
    ])


def assemble_video_sync(data, script, photo_path=None):
    with tempfile.TemporaryDirectory() as tmpdir:
        clips = [build_scene_clip(tmpdir, i, s) for i, s in enumerate(script.get("scenes", []))]
        clips.append(build_outro_clip(tmpdir, data, script, photo_path))
        dry_path = os.path.join(tmpdir, "dry.mp4")
        concat_clips(tmpdir, clips, dry_path)
        fd, out_path = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        add_background_music(dry_path, out_path)
        return out_path


WELCOME_TEXT = (
    "🎬 <b>PharmaAd AI — Генератор видеорекламы</b>\n\n"
    "Привет! Я помогу создать профессиональный сценарий видеорекламы для вашего препарата.\n\n"
    "📋 Нам понадобится:\n"
    "• Торговое название препарата\n"
    "• МНН (действующее вещество)\n"
    "• Лекарственная форма\n"
    "• Целевая аудитория\n"
    "• Показания и преимущества\n\n"
    "Начнём? Введите <b>торговое название</b> препарата:"
)

GENERIC_ERROR = "❌ Что-то пошло не так. Попробуйте ещё раз: /start"

video_slots = asyncio.Semaphore(MAX_PARALLEL_VIDEOS)


def h(value):
    """Escape any user- or Gemini-provided text for parse_mode=HTML.

    Messages used to be sent with parse_mode=Markdown, so a single "_" or "*"
    in a drug name or in Gemini's output made Telegram reject the message and
    the dialog got stuck.
    """
    return html.escape(str(value if value is not None else ""), quote=False)


def is_allowed(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USER_IDS)


async def deny(update: Update):
    user_id = update.effective_user.id if update.effective_user else "?"
    logger.info("Access denied for user %s", user_id)
    text = f"⛔ Доступ к боту ограничен. Ваш ID: <code>{h(user_id)}</code> — передайте его администратору."
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(text, parse_mode="HTML")
    elif update.message:
        await update.message.reply_text(text, parse_mode="HTML")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await deny(update)
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text(WELCOME_TEXT, parse_mode="HTML")
    return BRAND


async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await deny(update)
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.message.reply_text(WELCOME_TEXT, parse_mode="HTML")
    return BRAND


async def get_brand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['brand'] = update.message.text.strip()
    await update.message.reply_text(
        f"✅ Название: <b>{h(context.user_data['brand'])}</b>\n\n"
        "Теперь введите <b>МНН</b> (действующее вещество):",
        parse_mode="HTML"
    )
    return INN


async def get_inn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['inn'] = update.message.text.strip()
    keyboard = [[f] for f in DOSAGE_FORMS]
    await update.message.reply_text(
        f"✅ МНН: <b>{h(context.user_data['inn'])}</b>\n\nВыберите <b>лекарственную форму</b>:",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    return FORM


async def get_form(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['form'] = update.message.text.strip()
    keyboard = [[a] for a in AUDIENCES]
    await update.message.reply_text(
        f"✅ Форма: <b>{h(context.user_data['form'])}</b>\n\nВыберите <b>целевую аудиторию</b>:",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    return AUDIENCE


async def get_audience(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['audience'] = update.message.text.strip()
    await update.message.reply_text(
        f"✅ Аудитория: <b>{h(context.user_data['audience'])}</b>\n\n"
        "Введите <b>фармакологическую группу</b>\n<i>(например: кишечный антисептик, витамин)</i>:",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove()
    )
    return GROUP


async def get_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['group'] = update.message.text.strip()
    await update.message.reply_text(
        f"✅ Группа: <b>{h(context.user_data['group'])}</b>\n\n"
        "Последний шаг! Опишите <b>показания и преимущества</b> препарата\n<i>(2-3 предложения)</i>:",
        parse_mode="HTML"
    )
    return INDICATIONS


async def get_indications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['indications'] = update.message.text.strip()
    await update.message.reply_text(
        "🔍 <b>Анализирую препарат и подбираю стили рекламы...</b>\n\n⏳ 15–20 секунд",
        parse_mode="HTML"
    )
    try:
        # Gemini calls are blocking; running them on the event loop froze the
        # bot for every user for 15–30 seconds.
        styles = await asyncio.to_thread(generate_styles, context.user_data)
        if not styles:
            raise ValueError("Gemini returned no styles")
        context.user_data['styles'] = styles
        text = "🎨 <b>Готово! Выберите стиль видеорекламы:</b>\n\n"
        keyboard_buttons = []
        for i, s in enumerate(styles, 1):
            text += (
                f"<b>{i}. {h(s.get('name'))}</b> — ⭐ {h(s.get('score'))}/5\n"
                f"👥 {h(s.get('audience'))}\n"
                f"📈 Конверсия: {h(s.get('conversion'))}\n"
                f"💡 {h(s.get('why'))}\n\n"
            )
            keyboard_buttons.append([InlineKeyboardButton(
                f"{i}. {s.get('name', 'Стиль')} ⭐{s.get('score', '')}"[:64],
                callback_data=f"style_{i-1}"
            )])
        await update.message.reply_text(
            text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard_buttons)
        )
        return CHOOSE_STYLE
    except Exception:
        logger.exception("Style generation failed")
        await update.message.reply_text(GENERIC_ERROR)
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
    styles = json.loads(text[start:end+1])
    return [s for s in styles if isinstance(s, dict)][:6]


async def choose_style(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    styles = context.user_data.get('styles') or []
    try:
        idx = int(query.data.split('_')[1])
        style = styles[idx]
    except (ValueError, IndexError):
        # Stale button from an older dialog (e.g. after a bot restart).
        await query.message.reply_text("Эта кнопка устарела. Начните заново: /start")
        return ConversationHandler.END
    context.user_data['selected_style'] = style
    await query.edit_message_text(
        f"✅ Выбран стиль: <b>{h(style.get('name'))}</b>\n\n🎬 <b>Генерирую сценарий...</b>\n⏳ 20–30 секунд",
        parse_mode="HTML"
    )
    try:
        script = await asyncio.to_thread(generate_script, context.user_data)
        context.user_data['script'] = script
        await send_script(query, script, context.user_data)
    except Exception:
        logger.exception("Script generation failed")
        await query.message.reply_text(GENERIC_ERROR)
    return DONE


def generate_script(data):
    style = data['selected_style']
    prompt = f"""Создай профессиональный сценарий видеорекламы.

ПРЕПАРАТ: {data['brand']} ({data['inn']})
ФОРМА: {data['form']}
АУДИТОРИЯ: {data['audience']}
ПОКАЗАНИЯ: {data['indications']}
СТИЛЬ: {style.get('name', '')}

Верни ТОЛЬКО JSON без markdown:
{{
"logline": "Главный слоган",
"scenes": [
{{
"num": "Сцена 1",
"duration": "0-5 сек",
"visual": "Видеоряд",
"keywords": "2-4 английских слова для поиска стокового видео, например: pharmacist smiling patient",
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
    script = json.loads(text[start:end+1])
    script['scenes'] = [s for s in script.get('scenes') or [] if isinstance(s, dict)]
    return script


async def send_long(message, text, **kwargs):
    """Telegram rejects messages over 4096 chars; split on blank lines."""
    limit = 4000
    chunks, current = [], ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > limit and current:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    for i, chunk in enumerate(chunks):
        extra = kwargs if i == len(chunks) - 1 else {k: v for k, v in kwargs.items() if k != "reply_markup"}
        await message.reply_text(chunk[:4096], **extra)


async def send_script(query, script, data):
    brand = data['brand']
    style = data['selected_style'].get('name', '')
    await query.message.reply_text(
        f"🎬 <b>Сценарий готов: {h(brand)}</b>\n"
        f"🎨 Стиль: {h(style)}\n\n"
        f"✨ <b>Слоган:</b>\n<i>{h(script.get('logline', ''))}</i>\n\n"
        f"📢 <b>CTA:</b> {h(script.get('cta', ''))}",
        parse_mode="HTML"
    )
    scenes = script.get('scenes', [])
    if scenes:
        scenes_text = "🎥 <b>Сторибоард:</b>\n\n"
        for s in scenes:
            scenes_text += (
                f"<b>{h(s.get('num', ''))}</b> ({h(s.get('duration', ''))})\n"
                f"📹 {h(s.get('visual', ''))}\n"
                f"🎙️ <i>{h(s.get('voiceover', ''))}</i>\n"
                f"📝 {h(s.get('onscreen', ''))}\n\n"
            )
        await send_long(query.message, scenes_text, parse_mode="HTML")
    vo = script.get('voiceover_full', '')
    if vo:
        await send_long(query.message, f"🎙️ <b>Полный текст озвучки:</b>\n\n{h(vo)}", parse_mode="HTML")
    await query.message.reply_text(
        f"⏱️ <b>Версия 30 сек:</b>\n{h(script.get('version_30', ''))}\n\n"
        f"⚡ <b>Версия 15 сек:</b>\n{h(script.get('version_15', ''))}\n\n"
        "✅ <b>Сценарий готов. Для нового препарата: /start</b>",
        parse_mode="HTML",
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
    restart_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Новый препарат", callback_data="restart")]])
    if 'script' not in data or 'selected_style' not in data:
        await message.reply_text("❌ Сценарий не найден (возможно, бот перезапускался). Начните заново: /start")
        return
    if video_slots.locked():
        await message.reply_text("⏳ Сейчас собирается другое видео — ваше в очереди, начну сразу после него.")
    out_path = None
    status_msg = None
    try:
        async with video_slots:
            status_msg = await message.reply_text(
                "🎬 <b>Собираю видео из сториборда...</b>\n⏳ Обычно занимает 1–3 минуты",
                parse_mode="HTML"
            )
            out_path = await asyncio.to_thread(assemble_video_sync, data, data['script'], photo_path)
        with open(out_path, "rb") as f:
            await message.reply_video(
                f, caption=f"🎬 {data['brand']} — {data['selected_style'].get('name', '')}\n"
                           "⚠️ Черновик на стоковых кадрах для внутренней оценки. "
                           "Требует юридической проверки перед публикацией.",
                reply_markup=restart_kb
            )
        await status_msg.delete()
    except Exception:
        logger.exception("Video generation failed")
        error_text = "❌ Не удалось собрать видео. Попробуйте ещё раз чуть позже."
        if status_msg:
            await status_msg.edit_text(error_text)
        else:
            await message.reply_text(error_text)
        await message.reply_text("Попробовать другой препарат:", reply_markup=restart_kb)
    finally:
        if out_path and os.path.exists(out_path):
            os.remove(out_path)
        if photo_path and os.path.exists(photo_path):
            os.remove(photo_path)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отменено. /start — начать заново.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled error while processing an update", exc_info=context.error)


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан")
    if not ALLOWED_USER_IDS:
        logger.warning(
            "ALLOWED_USER_IDS не задан — ботом может пользоваться любой человек в Telegram "
            "и тратить квоты Gemini/Pexels. Укажите через запятую разрешённые Telegram user ID."
        )

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
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start)],
    )
    app.add_handler(conv)
    app.add_error_handler(on_error)

    # --- Webhook-режим для бесплатного Render Web Service ---
    PORT = int(os.environ.get("PORT", 10000))
    RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")

    if not RENDER_URL:
        logger.warning("RENDER_EXTERNAL_URL не задан — запускаюсь в режиме polling (локальный запуск).")
        app.run_polling()
        return

    # The webhook path used to be the bot token itself, which put the token
    # into every access-log line. Use a value derived from it instead, and have
    # Telegram send it back as a header so forged updates are rejected.
    webhook_secret = hashlib.sha256(f"pharma-bot-webhook:{BOT_TOKEN}".encode()).hexdigest()[:48]
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=f"tg/{webhook_secret}",
        webhook_url=f"{RENDER_URL}/tg/{webhook_secret}",
        secret_token=webhook_secret,
    )


if __name__ == "__main__":
    main()
