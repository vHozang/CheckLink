import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from time import sleep
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import (
    ConnectionError,
    ProxyError,
    RequestException,
    SSLError,
    Timeout,
)
from urllib3.util.retry import Retry

# ================== CONFIGURATION ==================
USE_PROXY = True  # Set to False when no proxy is required
PROXY_HOSTPORT = "127.0.0.1:60000"  # socks5 proxy, supports user:pass@host:port
TIMEOUT = 20  # seconds per request
# ===================================================

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36")

RE_PASSWORD = re.compile(
    r"(opening\s+soon|enter\s+using\s+password|template-password|name=\"password\")",
    re.IGNORECASE
)
RE_UNAVAILABLE = re.compile(
    r"(this\s+store\s+is\s+unavailable).*?(something\s+went\s+wrong|return\s+to\s+the\s+previous\s+page|request\s+id)",
    re.IGNORECASE | re.DOTALL
)


def normalize_url(url: str) -> str:
    """Ensure the URL is trimmed and uses https:// if missing a scheme."""
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    return url


def make_session(
    use_proxy: Optional[bool] = None,
    proxy_hostport: Optional[str] = None,
) -> requests.Session:
    """Create a configured requests session with retry and optional SOCKS5 proxy."""
    s = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
    )
    s.mount("http://", HTTPAdapter(max_retries=retries))
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})

    use_proxy = USE_PROXY if use_proxy is None else use_proxy
    proxy_hostport = PROXY_HOSTPORT if proxy_hostport is None else proxy_hostport

    if use_proxy and proxy_hostport:
        proxy_url = f"socks5h://{proxy_hostport}"  # socks5h resolves DNS via proxy
        s.proxies.update({
            "http": proxy_url,
            "https": proxy_url,
        })
    return s


_thread_local = threading.local()


def _thread_session(
    use_proxy: bool,
    proxy_hostport: Optional[str],
) -> requests.Session:
    """Reuse one requests.Session per worker thread to avoid reconnect overhead."""
    key: Tuple[bool, Optional[str]] = (use_proxy, proxy_hostport)
    sess = getattr(_thread_local, "session", None)
    sess_key = getattr(_thread_local, "session_key", None)
    if sess is None or sess_key != key:
        sess = make_session(use_proxy=use_proxy, proxy_hostport=proxy_hostport)
        _thread_local.session = sess
        _thread_local.session_key = key
    return sess


def is_password_page(final_url: str, html: str) -> bool:
    path = urlparse(final_url).path.rstrip("/")
    return path.endswith("/password") or bool(RE_PASSWORD.search(html or ""))


def is_unavailable_page(html: str) -> bool:
    return bool(RE_UNAVAILABLE.search(html or ""))


def classify(final_url: str, html: str, status: int) -> str:
    if status == 401:
        return "BLOCKED(401)"
    if status in (403, 429):
        return "BLOCKED"
    if status in (404, 410):
        return "DEAD"
    if status in (500, 502, 503, 504):
        return "RETRY"
    if is_password_page(final_url, html):
        return "PASSWORD"
    if is_unavailable_page(html):
        return "DEAD"
    if status == 200:
        return "LIVE"
    return f"UNKNOWN({status})"


def check_link_with_details(
    url: str,
    session: Optional[requests.Session] = None,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    s = session or make_session()
    timeout = TIMEOUT if timeout is None else timeout
    normalized_url = normalize_url(url)
    result: Dict[str, Any] = {
        "input_url": url,
        "normalized_url": normalized_url,
        "final_url": None,
        "http_status": None,
        "classification": None,
        "error": None,
    }

    try:
        resp = s.get(normalized_url, timeout=timeout, allow_redirects=True)
        classification = classify(resp.url, resp.text, resp.status_code)
        result.update(
            {
                "final_url": resp.url,
                "http_status": resp.status_code,
                "classification": classification,
                "error": None,
            }
        )
        return result
    except ProxyError as e:
        result.update({"classification": "PROXY_FAIL", "error": str(e)})
        return result
    except Timeout as e:
        result.update({"classification": "TIMEOUT", "error": str(e)})
        return result
    except SSLError as e:
        result.update({"classification": "SSL_ERROR", "error": str(e)})
        return result
    except ConnectionError as e:
        result.update({"classification": "BLOCKED_OR_DNS", "error": str(e)})
        return result
    except RequestException as e:
        result.update(
            {
                "classification": "UNREACHABLE",
                "error": f"{e.__class__.__name__}: {e}",
            }
        )
        return result


def check_link_status(
    url: str,
    session: Optional[requests.Session] = None,
    timeout: Optional[float] = None,
) -> str:
    result = check_link_with_details(url, session=session, timeout=timeout)
    return result["classification"]


def _resolve_per_link_delay() -> float:
    """Return enforced delay (seconds) between link checks; defaults to 10s."""
    raw = os.environ.get("CHECKLINK_PER_LINK_DELAY", "10")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 10.0
    return max(0.0, value)


def check_links(
    urls: list[str],
    use_proxy: Optional[bool] = None,
    proxy_hostport: Optional[str] = None,
    timeout: Optional[float] = None,
    max_workers: Optional[int] = None,
) -> list[Dict[str, Any]]:
    if not urls:
        return []

    resolved_timeout = TIMEOUT if timeout is None else timeout
    resolved_use_proxy = USE_PROXY if use_proxy is None else use_proxy
    resolved_proxy = PROXY_HOSTPORT if proxy_hostport is None else proxy_hostport
    per_link_delay = _resolve_per_link_delay()

    if max_workers is None:
        env_workers = os.environ.get("CHECKLINK_WORKERS") or os.environ.get("CHECKLINK_MAX_WORKERS")
        if env_workers:
            try:
                max_workers = int(env_workers)
            except (TypeError, ValueError):
                max_workers = None
    if max_workers is None:
        cpu_count = os.cpu_count() or 4
        max_workers = min(32, max(4, cpu_count * 5))
    max_workers = max(1, min(max_workers, len(urls)))
    if per_link_delay > 0:
        max_workers = 1

    if max_workers == 1:
        session = make_session(use_proxy=resolved_use_proxy, proxy_hostport=resolved_proxy)
        results: list[Dict[str, Any]] = []
        for idx, url in enumerate(urls):
            results.append(check_link_with_details(url, session=session, timeout=resolved_timeout))
            if per_link_delay > 0 and idx < len(urls) - 1:
                sleep(per_link_delay)
        return results

    def worker(idx: int, url: str) -> Tuple[int, Dict[str, Any]]:
        session = _thread_session(resolved_use_proxy, resolved_proxy)
        return idx, check_link_with_details(url, session=session, timeout=resolved_timeout)

    results: list[Optional[Dict[str, Any]]] = [None] * len(urls)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(worker, idx, url)
            for idx, url in enumerate(urls)
        ]
        for future in as_completed(futures):
            idx, data = future.result()
            results[idx] = data

    return [r for r in results if r is not None]


def main():
    base_dir = os.path.dirname(__file__)
    input_file = os.path.join(base_dir, "links.txt")
    output_file = os.path.join(base_dir, "checked_results.txt")

    session = make_session()

    # đọc link và chuẩn hóa
    with open(input_file, "r", encoding="utf-8") as f:
        links = [normalize_url(ln) for ln in f if ln.strip()]

    with open(output_file, "w", encoding="utf-8") as out:
        for link in links:
            status = check_link_status(link, session=session)
            line = f"{link} => {status}"
            print(line)
            out.write(line + "\n")


if __name__ == "__main__":
    main()
