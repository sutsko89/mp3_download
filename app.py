import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus, urljoin

import requests
import streamlit as st
from bs4 import BeautifulSoup

st.set_page_config(
    page_title="MP3 Downloader",
    page_icon="🎧",
    layout="wide",
)

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
    def query(self) -> str:
        return f"{self.artist} {self.title}".strip()

    @property
    def filename(self) -> str:
        name = f"{self.artist} - {self.title}".strip()
        name = re.sub(r'[<>:"/\\|?*]', "_", name)
        name = re.sub(r"\s+", " ", name).strip()
        return f"{name}.mp3"


def init_state():
    if "running" not in st.session_state:
        st.session_state.running = False

    if "stop" not in st.session_state:
        st.session_state.stop = False

    if "results" not in st.session_state:
        st.session_state.results = []

    if "log_lines" not in st.session_state:
        st.session_state.log_lines = []

    if "progress" not in st.session_state:
        st.session_state.progress = 0

    if "total" not in st.session_state:
        st.session_state.total = 0

    if "downloaded_count" not in st.session_state:
        st.session_state.downloaded_count = 0

    if "skipped_count" not in st.session_state:
        st.session_state.skipped_count = 0

    if "not_found_count" not in st.session_state:
        st.session_state.not_found_count = 0

    if "error_count" not in st.session_state:
        st.session_state.error_count = 0


def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def parse_tracks(text: str):
    tracks = []
    seen = set()

    for line in text.splitlines():
        line = line.strip()

        if not line or line.startswith("#"):
            continue

        if line in seen:
            continue

        seen.add(line)

        if " — " in line:
            artist, title = line.split(" — ", 1)
        elif " - " in line:
            artist, title = line.split(" - ", 1)
        else:
            artist, title = "", line

        artist = artist.strip()
        title = title.strip()

        if not artist or not title:
            continue

        if title.lower() in (
            "deep house remix",
            "remix",
            "house remix",
        ):
            continue

        tracks.append(
            Track(
                artist=artist,
                title=title,
                raw=line,
            )
        )

    return tracks


def load_cache(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    return {}


def save_cache(path: Path, cache: dict):
    path.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def is_probable_mp3_url(url: str) -> bool:
    url = url.lower()
    return any(url.endswith(ext) for ext in MP3_EXTENSIONS)


def is_mp3_response(response) -> bool:
    content_type = response.headers.get("Content-Type", "").lower()

    if any(mime in content_type for mime in AUDIO_MIME_TYPES):
        return True

    disposition = response.headers.get("Content-Disposition", "").lower()

    if ".mp3" in disposition:
        return True

    return False


def extract_mp3_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        full_url = urljoin(base_url, href)

        if is_probable_mp3_url(full_url):
            links.add(full_url)

    for tag in soup.find_all(True):
        for attr, value in tag.attrs.items():
            if not isinstance(value, str):
                continue

            if "mp3" in value.lower() or "audio" in value.lower():
                if value.startswith("http"):
                    links.add(value)
                elif value.startswith("/"):
                    links.add(urljoin(base_url, value))

    pattern = r'https?://[^\s"\']+\.(?:mp3)(?:\?[^\s"\']*)?'
    for match in re.findall(pattern, html, flags=re.IGNORECASE):
        links.add(match)

    pattern_rel = r'["\'](/[^"\']+\.mp3(?:\?[^"\']*)?)["\']'
    for match in re.findall(pattern_rel, html, flags=re.IGNORECASE):
        links.add(urljoin(base_url, match))

    return list(links)


def find_search_urls(base_url: str, query: str) -> list[str]:
    base = base_url.rstrip("/")

    return [
        f"{base}/search?q={quote_plus(query)}",
        f"{base}/search/{quote_plus(query)}",
        f"{base}/?s={quote_plus(query)}",
        f"{base}/search?query={quote_plus(query)}",
    ]


def get_html(session, url: str):
    response = session.get(
        url,
        timeout=25,
        headers=HEADERS,
        allow_redirects=True,
    )
    response.raise_for_status()
    return response.text, response.url


def download_mp3(session, url: str, output_path: Path):
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


def try_site(
    session,
    base_url: str,
    track: Track,
    output_dir: Path,
    delay: float,
):
    try:
        search_urls = find_search_urls(base_url, track.query)
        mp3_links = []

        for search_url in search_urls:
            if st.session_state.stop:
                return "STOPPED", ""

            try:
                html, final_url = get_html(session, search_url)
                links = extract_mp3_links(html, final_url)

                if links:
                    mp3_links.extend(links)
                    break

            except requests.exceptions.HTTPError as e:
                if e.response.status_code in (401, 403):
                    return "BLOCKED", f"HTTP {e.response.status_code}"

                continue

            except Exception:
                continue

            time.sleep(delay)

        if not mp3_links:
            return "NOT_FOUND", "MP3-ссылки не найдены"

        output_path = output_dir / track.filename

        if output_path.exists():
            return "SKIPPED_ALREADY_DOWNLOADED", "Файл уже существует"

        for mp3_url in mp3_links:
            if st.session_state.stop:
                return "STOPPED", ""

            try:
                download_mp3(session, mp3_url, output_path)
                return "DOWNLOADED", output_path.name

            except Exception:
                continue

            time.sleep(delay)

        return "NOT_FOUND", "Не удалось скачать ни одну MP3-ссылку"

    except Exception as e:
        return "ERROR", str(e)


def run_download(
    tracks,
    output_dir: Path,
    cache_path: Path,
    delay: float,
):
    cache = load_cache(cache_path)
    session = requests.Session()
    session.headers.update(HEADERS)

    st.session_state.total = len(tracks)
    st.session_state.progress = 0
    st.session_state.downloaded_count = 0
    st.session_state.skipped_count = 0
    st.session_state.not_found_count = 0
    st.session_state.error_count = 0
    st.session_state.results = []
    st.session_state.log_lines = []

    progress_bar = st.progress(0)
    status_area = st.empty()
    log_area = st.empty()

    for index, track in enumerate(tracks, start=1):
        if st.session_state.stop:
            status_area.warning("Загрузка остановлена пользователем")
            break

        if track.raw in cache:
            status = "SKIPPED_ALREADY_DOWNLOADED"
            message = "Уже в кэше"
            site = "cache"
        else:
            site = ""
            status = "NOT_FOUND"
            message = "Ни один сайт не дал результата"

            for base_url in SITES:
                if st.session_state.stop:
                    status = "STOPPED"
                    message = ""
                    break

                status, message = try_site(
                    session=session,
                    base_url=base_url,
                    track=track,
                    output_dir=output_dir,
                    delay=delay,
                )

                if status in (
                    "DOWNLOADED",
                    "SKIPPED_ALREADY_DOWNLOADED",
                    "BLOCKED",
                    "REGISTRATION_REQUIRED",
                    "STOPPED",
                ):
                    site = base_url
                    break

                time.sleep(delay)

        if status == "DOWNLOADED":
            cache[track.raw] = {
                "file": track.filename,
                "downloaded_at": datetime.now().isoformat(),
            }
            save_cache(cache_path, cache)
            st.session_state.downloaded_count += 1

        elif status == "SKIPPED_ALREADY_DOWNLOADED":
            st.session_state.skipped_count += 1

        elif status == "NOT_FOUND":
            st.session_state.not_found_count += 1

        elif status == "ERROR":
            st.session_state.error_count += 1

        log_line = (
            f"[{datetime.now():%Y-%m-%d %H:%M:%S}] "
            f"[{status}] [{site or 'all'}] [{track.raw}] {message}"
        )

        st.session_state.log_lines.append(log_line)
        st.session_state.results.append(
            {
                "Трек": track.raw,
                "Статус": status,
                "Сайт": site or "all",
                "Файл": track.filename if status == "DOWNLOADED" else "",
                "Сообщение": message,
            }
        )

        st.session_state.progress = index
        progress_bar.progress(index / len(tracks))

        status_area.info(
            f"Обработано: {index} / {len(tracks)}\n\n"
            f"Скачано: {st.session_state.downloaded_count}\n\n"
            f"Пропущено: {st.session_state.skipped_count}\n\n"
            f"Не найдено: {st.session_state.not_found_count}\n\n"
            f"Ошибок: {st.session_state.error_count}"
        )

        log_area.code("\n".join(st.session_state.log_lines[-30:]))

        time.sleep(delay)

    progress_bar.progress(1.0)
    status_area.success("Готово")


init_state()

st.title("🎧 MP3 Downloader")
st.caption(
    "Загрузка треков из tracks.txt через chill house / deep house / remix-сайты"
)

with st.sidebar:
    st.header("Настройки")

    uploaded_file = st.file_uploader(
        "Загрузите tracks.txt",
        type=["txt"],
    )

    output_dir_text = st.text_input(
        "Папка для MP3",
        value="downloads",
    )

    cache_path_text = st.text_input(
        "Файл кэша",
        value="downloaded.json",
    )

    delay = st.slider(
        "Пауза между запросами, сек",
        min_value=0.5,
        max_value=10.0,
        value=2.0,
        step=0.5,
    )

    max_tracks = st.number_input(
        "Максимум треков",
        min_value=1,
        max_value=5000,
        value=500,
    )

    st.divider()

    start_button = st.button(
        "▶️ Начать загрузку",
        use_container_width=True,
        disabled=st.session_state.running,
    )

    stop_button = st.button(
        "⏹️ Остановить",
        use_container_width=True,
        disabled=not st.session_state.running,
    )

if stop_button:
    st.session_state.stop = True
    st.warning("Остановка запрошена")

if start_button and uploaded_file is not None:
    text = uploaded_file.getvalue().decode("utf-8")
    tracks = parse_tracks(text)

    if not tracks:
        st.error("В файле нет корректных треков")
    else:
        tracks = tracks[: int(max_tracks)]

        output_dir = Path(output_dir_text)
        cache_path = Path(cache_path_text)

        output_dir.mkdir(parents=True, exist_ok=True)
        cache_path.parent.mkdir(parents=True, exist_ok=True)

        st.session_state.running = True
        st.session_state.stop = False

        run_download(
            tracks=tracks,
            output_dir=output_dir,
            cache_path=cache_path,
            delay=delay,
        )

        st.session_state.running = False

elif start_button and uploaded_file is None:
    st.warning("Сначала загрузите tracks.txt")

st.divider()

col1, col2, col3, col4 = st.columns(4)

col1.metric("Всего", st.session_state.total)
col2.metric("Скачано", st.session_state.downloaded_count)
col3.metric("Пропущено", st.session_state.skipped_count)
col4.metric("Не найдено", st.session_state.not_found_count)

st.subheader("Результаты")

if st.session_state.results:
    st.dataframe(
        st.session_state.results,
        use_container_width=True,
        hide_index=True,
    )
else:
    st.info("Результатов пока нет")

st.subheader("Лог")

if st.session_state.log_lines:
    st.code("\n".join(st.session_state.log_lines[-100:]))
else:
    st.info("Лог пока пуст")