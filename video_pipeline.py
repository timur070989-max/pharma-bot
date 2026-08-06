"""Переиспользуемый пайплайн генерации видеорекламы препарата.

Не зависит от Telegram — используется и ботом (bot.py), и CLI-скриптом
скилла генерации видео (.claude/skills/pharma-video-generation/scripts/generate_ad_video.py).

Пайплайн: Gemini придумывает стили и сценарий -> EdgeTTS озвучивает сцены ->
Pexels подбирает стоковый видеоряд -> FFmpeg монтирует сцены, титры,
финальную карточку бренда и фоновую музыку в один вертикальный (9:16) ролик.
"""
import os
import re
import json
import asyncio
import tempfile
import subprocess
import requests
import edge_tts
from google import genai

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")

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
    return json.loads(text[start:end + 1])


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
    return json.loads(text[start:end + 1])
