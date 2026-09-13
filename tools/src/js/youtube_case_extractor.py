"""
Извлекает субтитры/транскрипт, описание и ссылки из YouTube-видео и
сохраняет их в company_cases/<company>/ для последующего анализа
архитектурного кейса компании.

Использование:
    python tools/src/youtube_case_extractor.py <youtube_url> [--company "Company Name"]

Результат:
    company_cases/<company_slug>/source.md      — метаданные видео, описание, ссылки
    company_cases/<company_slug>/transcript.txt — очищенный текст субтитров
"""

import argparse
import re
import sys
import tempfile
import time
from pathlib import Path

import yt_dlp

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

PROJECT_ROOT = Path(__file__).resolve().parents[2]
URL_RE = re.compile(r"https?://[^\s)\]>]+")
PREFERRED_LANGS = ["en", "ru"]
SUBTITLE_RETRIES = 4
SUBTITLE_RETRY_DELAY = 10


def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9а-яё]+", "_", text, flags=re.IGNORECASE)
    return text.strip("_") or "unknown_company"


def extract_info(url: str) -> dict:
    opts = {"quiet": True, "noprogress": True, "skip_download": True, "no_warnings": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def pick_subtitle_lang(info: dict) -> tuple[str, bool] | None:
    """Возвращает (lang, is_automatic) для лучшего доступного варианта субтитров."""
    subs = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    for lang in PREFERRED_LANGS:
        if lang in subs:
            return lang, False
    for lang in PREFERRED_LANGS:
        if lang in auto:
            return lang, True
    if subs:
        return next(iter(subs)), False
    if auto:
        return next(iter(auto)), True
    return None


def download_subtitle_vtt(url: str, lang: str, is_automatic: bool, dest_dir: Path) -> Path | None:
    outtmpl = str(dest_dir / "%(id)s")
    opts = {
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "skip_download": True,
        "writesubtitles": not is_automatic,
        "writeautomaticsub": is_automatic,
        "subtitleslangs": [lang],
        "subtitlesformat": "vtt",
        "outtmpl": outtmpl,
    }
    last_error: Exception | None = None
    for attempt in range(1, SUBTITLE_RETRIES + 1):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            matches = list(dest_dir.glob(f"*.{lang}.vtt"))
            return matches[0] if matches else None
        except yt_dlp.utils.DownloadError as exc:
            last_error = exc
            if attempt < SUBTITLE_RETRIES:
                print(
                    f"Субтитры недоступны с попытки {attempt} ({exc}); "
                    f"повтор через {SUBTITLE_RETRY_DELAY}с...",
                    file=sys.stderr,
                )
                time.sleep(SUBTITLE_RETRY_DELAY)
    print(f"Не удалось скачать субтитры после {SUBTITLE_RETRIES} попыток: {last_error}", file=sys.stderr)
    return None


def vtt_to_text(vtt_path: Path) -> str:
    lines = vtt_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    tag_re = re.compile(r"<[^>]+>")
    timestamp_line_re = re.compile(r"^\d\d:\d\d:\d\d\.\d\d\d\s*-->")
    cleaned: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line or line == "WEBVTT" or line.isdigit():
            continue
        if timestamp_line_re.match(line) or "-->" in line:
            continue
        if line.startswith(("Kind:", "Language:", "NOTE")):
            continue
        line = tag_re.sub("", line).strip()
        if not line:
            continue
        if cleaned and cleaned[-1] == line:
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


def build_source_md(info: dict, links: list[str], lang: str | None, is_automatic: bool | None) -> str:
    title = info.get("title", "")
    uploader = info.get("uploader", "")
    upload_date = info.get("upload_date", "")
    if upload_date and len(upload_date) == 8:
        upload_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
    video_id = info.get("id", "")
    webpage_url = info.get("webpage_url", "")
    description = info.get("description", "") or ""

    if lang is None:
        subtitle_note = "субтитры недоступны"
    else:
        subtitle_note = f"{lang} ({'автоматические' if is_automatic else 'ручные'})"

    links_block = "\n".join(f"- {link}" for link in links) if links else "(ссылок в описании не найдено)"

    return f"""# {title}

- **Канал:** {uploader}
- **URL:** {webpage_url}
- **ID видео:** {video_id}
- **Дата публикации:** {upload_date}
- **Субтитры:** {subtitle_note}

## Описание (как на YouTube)

{description}

## Ссылки из описания

{links_block}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Ссылка на YouTube-видео")
    parser.add_argument("--company", help="Название компании (иначе — из заголовка видео)")
    parser.add_argument(
        "--out-dir",
        default=str(PROJECT_ROOT / "company_cases"),
        help="Корневая папка для кейсов компаний (по умолчанию company_cases/)",
    )
    args = parser.parse_args()

    print(f"Извлекаю метаданные: {args.url}")
    info = extract_info(args.url)

    company_slug = slugify(args.company) if args.company else slugify(info.get("title", ""))
    out_dir = Path(args.out_dir) / company_slug
    out_dir.mkdir(parents=True, exist_ok=True)

    description = info.get("description", "") or ""
    links = URL_RE.findall(description)

    picked = pick_subtitle_lang(info)
    lang = is_automatic = None
    if picked:
        lang, is_automatic = picked
        print(f"Скачиваю субтитры ({lang}, {'auto' if is_automatic else 'manual'})...")
        with tempfile.TemporaryDirectory() as tmp:
            vtt_path = download_subtitle_vtt(args.url, lang, is_automatic, Path(tmp))
            if vtt_path:
                transcript = vtt_to_text(vtt_path)
                (out_dir / "transcript.txt").write_text(transcript, encoding="utf-8")
            else:
                print("Не удалось скачать файл субтитров.", file=sys.stderr)
                lang = None
    else:
        print("Субтитры для этого видео недоступны.", file=sys.stderr)

    source_md = build_source_md(info, links, lang, is_automatic)
    (out_dir / "source.md").write_text(source_md, encoding="utf-8")

    print(f"Готово: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
