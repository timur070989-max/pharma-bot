#!/usr/bin/env python3
"""CLI для генерации видеорекламы препарата прямо из чата, без Telegram.

Переиспользует пайплайн pharma-bot (video_pipeline.py): Gemini придумывает
стили и сценарий, EdgeTTS озвучивает сцены, Pexels подбирает видеоряд,
FFmpeg монтирует итоговый вертикальный (9:16) ролик.

Пример:
    python generate_ad_video.py \
        --brand "Ринзасип" --inn "парацетамол+фенирамин+аскорбиновая кислота" \
        --form "Порошок для приготовления раствора" \
        --audience "Взрослые (18+)" --group "Средство от простуды и гриппа" \
        --indications "Снимает симптомы простуды и гриппа: температуру, боль, заложенность носа" \
        --style 0

Печатает в stdout один JSON-объект:
    {"video_path": "...", "style": {...}, "script": {...}}
"""
import argparse
import json
import os
import sys
from pathlib import Path

# Репозиторий: <repo_root>/.claude/skills/pharma-video-generation/scripts/generate_ad_video.py
REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

from video_pipeline import generate_styles, generate_script, assemble_video_sync  # noqa: E402


def parse_args():
    p = argparse.ArgumentParser(description="Генерация видеорекламы препарата из чата")
    p.add_argument("--brand", required=True, help="Торговое название препарата")
    p.add_argument("--inn", required=True, help="МНН (действующее вещество)")
    p.add_argument("--form", required=True, help="Лекарственная форма, например: Таблетки")
    p.add_argument("--audience", required=True, help="Целевая аудитория, например: Взрослые (18+)")
    p.add_argument("--group", required=True, help="Фармакологическая группа")
    p.add_argument("--indications", required=True, help="Показания и преимущества (2-3 предложения)")
    p.add_argument(
        "--style", type=int, default=None,
        help="Индекс стиля из предложенных Gemini (0-3). Если не задан, берётся стиль с наивысшим score.",
    )
    p.add_argument("--photo", default=None, help="Путь к фото упаковки препарата для финальной карточки (опционально)")
    p.add_argument("--output", default=None, help="Куда сохранить итоговый mp4 (по умолчанию — временный файл)")
    return p.parse_args()


def pick_style(styles, style_index):
    if style_index is not None:
        if not (0 <= style_index < len(styles)):
            raise ValueError(f"--style должен быть в диапазоне 0-{len(styles) - 1}")
        return styles[style_index]
    return max(styles, key=lambda s: s.get("score", 0))


def main():
    args = parse_args()

    missing = [name for name, val in (("GEMINI_API_KEY", os.environ.get("GEMINI_API_KEY")),
                                       ("PEXELS_API_KEY", os.environ.get("PEXELS_API_KEY"))) if not val]
    if missing:
        print(f"Не заданы переменные окружения: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    data = {
        "brand": args.brand,
        "inn": args.inn,
        "form": args.form,
        "audience": args.audience,
        "group": args.group,
        "indications": args.indications,
    }

    print("Подбираю стили видеорекламы...", file=sys.stderr)
    styles = generate_styles(data)
    style = pick_style(styles, args.style)
    data["selected_style"] = style
    print(f"Стиль: {style['name']} (score {style.get('score')})", file=sys.stderr)

    print("Генерирую сценарий...", file=sys.stderr)
    script = generate_script(data)

    print("Собираю видео (Pexels + EdgeTTS + FFmpeg, обычно 1-3 минуты)...", file=sys.stderr)
    video_path = assemble_video_sync(data, script, photo_path=args.photo)

    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)) or ".", exist_ok=True)
        os.replace(video_path, args.output)
        video_path = args.output

    print(json.dumps({"video_path": video_path, "style": style, "script": script}, ensure_ascii=False))


if __name__ == "__main__":
    main()
