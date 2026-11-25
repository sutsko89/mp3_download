import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, quote_plus

import requests
from bs4 import BeautifulSoup

SITES = [
    "https://rus.hitmos.fm",
    "https://ru.drivemusic.me",
    "https://music.hitmo.net",
    "https://ru.monfons.com",
    "https://lmusic.kz",
    "https://muz.zvukofon.com",
    "https://web.ligaudio.ru/",
    "https://ruo.morsmusic.org",
    "https://zaycev.net",
    "https://musify.club",
    "https://mp3cc",
    "https://mp3juices",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

MP3_EXTENSIONS = (".mp3",)
AUDIO_MIME_TYPES = (
    "audio/mpeg",
    "audio/mp3",
    "application/octet-stream",
)

@dataclass
class Track:
    artist: str
    title: str
    raw: str

    @property
    def query(self):
        return f"{self.artist} {self.title}".strip()

    @property
    def filename(self):
        name = f"{self.artist} - {self.title}".strip()
        name = re.sub(r'[<>:"/\\|?*]', "_", name)
        name = re.sub(r"\s+", " ", name).strip()
        return f"{name}.mp3"

class Logger:
    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.path = self.log_dir / f"download_{stamp}.log"
        self.counters = {
            "total": 0,
            "downloaded": 0,
            "skipped": 0,
            "not_found": 0,
            "registration_required": 0,
            "blocked": 0,
            "errors": 0,
        }

    def write(self, status, site, track, message=""):
        line = (
            f"[{datetime.now():%Y-%m-%d %H:%M:%S}] "
            f"[{status}] [{site}] [{track}] {message}"
        )
        print(line)

        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

        if status == "DOWNLOADED":
            self.counters["downloaded"] += 1
        elif status == "SKIPPED_ALREADY_DOWNLOADED":
            self.counters["skipped"] += 1
        elif status == "NOT_FOUND":
            self.counters["not_found"] += 1
        elif status == "REGISTRATION_REQUIRED":
            self.counters["registration_required"] += 1
        elif status == "BLOCKED":
            self.counters["blocked"] += 1
        elif status == "ERROR":
            self.counters["errors"] += 1

    def summary(self):
        print("\n=== ИТОГИ ===")
        for key, value in self.counters.items():
            print(f"{key}: {value}")

def load_tracks(path: Path, limit: int):
    tracks = []

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()

            if not line or line.startswith("#"):
                continue

            if " — " in line:
                artist, title = line.split(" — ", 1)
            elif " - " in line:
                artist, title = line.split(" - ", 1)
            else:
                artist, title = "", line

            tracks.append(
                Track(
                    artist=artist.strip(),
                    title=title.strip(),
                    raw=line,
                )
            )

            if len(tracks) >= limit:
                break

    return tracks

def load_cache(path: Path):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    return {}

def save_cache(path: Path, cache):
    path.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

def is_probable_mp3_url(url: str):
    url = url.lower()
    return any(url.endswith(ext) for ext in MP3_EXTENSIONS)

def is_mp3_response(response):
    content_type = response.headers.get("Content-Type", "").lower()

    if any(mime in content_type for mime in AUDIO_MIME_TYPES):
        return True

    disposition = response.headers.get("Content-Disposition", "").lower()

    if ".mp3" in disposition:
        return True

    return False

def safe_filename(name: str):
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name

def extract_mp3_links(html: str, base_url: str):
    soup = BeautifulSoup(html, "html.parser")
    links = set()

    # 1. Прямые ссылки на .mp3
    for a in soup.find_all("a", href=True):
        href = a["href"]
        full_url = urljoin(base_url, href)

        if is_probable_mp3_url(full_url):
            links.add(full_url)

    # 2. Ссылки в data-атрибутах
    for tag in soup.find_all(True):
        for attr, value in tag.attrs.items():
            if not isinstance(value, str):
                continue

            if "mp3" in value.lower() or "audio" in value.lower():
                if value.startswith("http"):
                    links.add(value)
                elif value.startswith("/"):
                    links.add(urljoin(base_url, value))

    # 3. Поиск в HTML через регулярку
    pattern = r'https?://[^\s"\']+\.(?:mp3)(?:\?[^\s"\']*)?'
    for match in re.findall(pattern, html, flags=re.IGNORECASE):
        links.add(match)

    # 4. Поиск относительных ссылок
    pattern_rel = r'["\'](/[^"\']+\.mp3(?:\?[^"\']*)?)["\']'
    for match in re.findall(pattern_rel, html, flags=re.IGNORECASE):
        links.add(urljoin(base_url, match))

    return list(links)

def find_search_url(base_url: str, query: str):
    # TODO: проверить реальный URL поиска для каждого сайта
    candidates = [
        f"{base_url.rstrip('/')}/search?q={quote_plus(query)}",
        f"{base_url.rstrip('/')}/search/{quote_plus(query)}",
        f"{base_url.rstrip('/')}/?s={quote_plus(query)}",
        f"{base_url.rstrip('/')}/search?query={quote_plus(query)}",
    ]

    return candidates

def get_html(session, url):
    response = session.get(url, timeout=25, headers=HEADERS, allow_redirects=True)
    response.raise_for_status()
    return response.text, response.url

def try_download_mp3(session, url, output_path: Path):
    response = session.get(
        url,
        stream=True,
        timeout=40,
        headers=HEADERS,
        allow_redirects=True,
    )
    response.raise_for_status()

    if not is_mp3_response(response) and not is_probable_mp3_url(response.url):
        raise ValueError("Ответ не похож на MP3")

    temp_path = output_path.with_suffix(".part")

    with temp_path.open("wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)

    if temp_path.stat().st_size < 10_000:
        temp_path.unlink()
        raise ValueError("Файл слишком маленький или повреждён")

    temp_path.rename(output_path)

def try_site(session, base_url, track: Track, logger: Logger, output_dir: Path):
    try:
        search_urls = find_search_url(base_url, track.query)
        mp3_links = []

        for search_url in search_urls:
            try:
                html, final_url = get_html(session, search_url)
                links = extract_mp3_links(html, final_url)

                if links:
                    mp3_links.extend(links)
                    break

            except requests.exceptions.HTTPError as e:
                if e.response.status_code in (401, 403):
                    logger.write(
                        "BLOCKED",
                        base_url,
                        track.raw,
                        f"HTTP {e.response.status_code}",
                    )
                    return False

                continue

            except Exception:
                continue

        if not mp3_links:
            logger.write(
                "NOT_FOUND",
                base_url,
                track.raw,
                "MP3-ссылки не найдены",
            )
            return False

        logger.write(
            "FOUND",
            base_url,
            track.raw,
            f"Найдено MP3-ссылок: {len(mp3_links)}",
        )

        output_path = output_dir / track.filename

        if output_path.exists():
            logger.write(
                "SKIPPED_ALREADY_DOWNLOADED",
                base_url,
                track.raw,
                "Файл уже существует",
            )
            return True

        for mp3_url in mp3_links:
            try:
                try_download_mp3(session, mp3_url, output_path)
                logger.write(
                    "DOWNLOADED",
                    base_url,
                    track.raw,
                    output_path.name,
                )
                return True

            except Exception as e:
                continue

        logger.write(
            "NOT_FOUND",
            base_url,
            track.raw,
            "Не удалось скачать ни одну MP3-ссылку",
        )
        return False

    except Exception as e:
        logger.write("ERROR", base_url, track.raw, str(e))
        return False

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="tracks.txt")
    parser.add_argument("--output", default="downloads")
    parser.add_argument("--log", default="logs")
    parser.add_argument("--max-tracks", type=int, default=500)
    parser.add_argument("--delay", type=float, default=2.0)
    args = parser.parse_args()

    input_path = Path(args.input)
    output_dir = Path(args.output)
    log_dir = Path(args.log)
    cache_path = Path("downloaded.json")

    output_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    tracks = load_tracks(input_path, args.max_tracks)
    cache = load_cache(cache_path)
    logger = Logger(log_dir)
    logger.counters["total"] = len(tracks)

    session = requests.Session()
    session.headers.update(HEADERS)

    for track in tracks:
        if track.raw in cache:
            logger.write(
                "SKIPPED_ALREADY_DOWNLOADED",
                "cache",
                track.raw,
                "Уже в кэше",
            )
            continue

        downloaded = False

        for site in SITES:
            if try_site(session, site, track, logger, output_dir):
                downloaded = True

                cache[track.raw] = {
                    "file": track.filename,
                    "downloaded_at": datetime.now().isoformat(),
                }

                save_cache(cache_path, cache)
                break

            time.sleep(args.delay)

        if not downloaded:
            logger.write(
                "NOT_FOUND",
                "all",
                track.raw,
                "Ни один сайт не дал результата",
            )

        time.sleep(args.delay)

    logger.summary()

if __name__ == "__main__":
    main()