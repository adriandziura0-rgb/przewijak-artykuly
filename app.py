#!/usr/bin/env python3
# Przewijak — ARTYKUŁY / Windows + Android Termux — LOW-IMPACT
# Cel: niezawodne pobieranie i archiwizacja artykułów. Bez analizy treści.

import csv
import hashlib
import html
import json
import os
import platform
import re
import shutil
import subprocess
import sqlite3
import threading
import time
import unicodedata
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

PORT = int(os.environ.get("PRZEWIJAK_PORT", "8765"))
HOST = "127.0.0.1"
APP_VERSION = "15.3-pc-phone-native-clean"


def runtime_platform():
    """Rozpoznaje środowisko bez uzależniania programu od GUI systemowego."""
    prefix = (os.environ.get("PREFIX") or "").lower()
    if os.environ.get("PRZEWIJAK_ANDROID_NATIVE") == "1":
        return "android-native"
    if os.environ.get("TERMUX_VERSION") or "com.termux" in prefix:
        return "android-termux"
    if os.name == "nt":
        return "windows"
    return (platform.system() or "other").lower()


def preferred_downloads_dir():
    """Najbardziej użyteczny katalog Pobrane dla aktualnej platformy."""
    if runtime_platform() == "android-native":
        return Path(os.environ.get("HOME") or str(Path.home())) / "Downloads"
    if runtime_platform() == "android-termux":
        shared = [Path.home() / "storage" / "downloads", Path("/storage/emulated/0/Download")]
        for p in shared:
            try:
                if p.exists() and os.access(p, os.W_OK):
                    return p
            except Exception:
                pass
        # Nie tworzymy sztucznego ~/storage, bo Termux używa tam własnych linków
        # tworzonych przez termux-setup-storage. Bez uprawnień bezpiecznie zapisujemy w HOME.
        return Path.home() / "Downloads"
    candidates = [Path.home() / "Downloads", Path.home() / "Pobrane"]
    for p in candidates:
        try:
            if p.exists():
                return p
        except Exception:
            pass
    return candidates[0]


TRACKING_KEYS = {
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "ref", "ref_src",
    "cmpid", "campaign", "campaign_id", "source", "src",
}
BAD_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".pdf", ".zip", ".rar",
    ".7z", ".mp3", ".mp4", ".avi", ".mov", ".m3u8", ".css", ".js", ".xml",
)
BAD_LINK_WORDS = {
    "autor", "author", "tag", "tags", "kategoria", "category", "kontakt", "contact",
    "login", "logowanie", "konto", "account", "newsletter", "reklama", "advertising",
    "privacy", "polityka-prywatnosci", "cookies", "regulamin", "terms", "video", "wideo",
    "galeria", "gallery", "podcast", "programy", "program", "tematy", "topic", "topics",
}
ARTICLE_HINTS = (
    "/artykul/", "/artykuly/", "/article/", "/articles/", "/news/", "/wiadomosci/",
    "/polska/", "/swiat/", "/biznes/", "/sport/", "/kultura/", "/technologie/",
)
NOISE_RX = re.compile(
    r"(?:^|[-_])(nav|menu|header|footer|sidebar|aside|cookie|consent|advert|ads?|promo|banner|"
    r"recommend(?:ed|ation|ations)?|related|newsletter|social|share|comments?|login|paywall|popular|latest|most-read)"
    r"(?:$|[-_])",
    re.I,
)
BLOCKED_RX = re.compile(
    r"access denied|forbidden|captcha|verify you are human|checking your browser|"
    r"odmowa dostępu|robot verification|cloudflare|security check|challenge",
    re.I,
)

# Słabe frazy (np. "enable javascript") bardzo często występują w poprawnym HTML
# jako fallback w <noscript> lub skryptach. Nie mogą samodzielnie oznaczać CAPTCHA.
BLOCKED_WEAK_RX = re.compile(
    r"enable javascript|włącz javascript|javascript is required|javascript required",
    re.I,
)


class OperationCancelled(RuntimeError):
    """Normalne przerwanie operacji przez użytkownika (STOP / zamknięcie panelu)."""


def _is_client_disconnect(exc):
    """Rozłączenie lokalnej przeglądarki nie jest błędem programu."""
    if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
        return True
    if isinstance(exc, OSError) and getattr(exc, "winerror", None) in {10053, 10054, 10038}:
        return True
    return False



def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def safe_slug(text, fallback="artykul", max_len=88):
    text = unquote(text or "").strip()
    text = re.sub(r"[^0-9A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż._ -]+", " ", text)
    text = re.sub(r"\s+", "_", text).strip("._-")
    return (text[:max_len] or fallback)


def canonicalize_url(url):
    try:
        s = urlsplit((url or "").strip())
        if s.scheme not in ("http", "https") or not s.netloc:
            return ""
        scheme = s.scheme.lower()
        netloc = s.netloc.lower()
        if netloc.endswith(":80") and scheme == "http":
            netloc = netloc[:-3]
        if netloc.endswith(":443") and scheme == "https":
            netloc = netloc[:-4]
        path = re.sub(r"/{2,}", "/", s.path or "/")
        if path != "/":
            path = path.rstrip("/")
        clean_q = []
        for k, v in parse_qsl(s.query, keep_blank_values=True):
            lk = k.lower()
            if lk.startswith("utm_") or lk in TRACKING_KEYS:
                continue
            clean_q.append((k, v))
        return urlunsplit((scheme, netloc, path, urlencode(clean_q, doseq=True), ""))
    except Exception:
        return ""


def same_site(a, b):
    try:
        ha = (urlsplit(a).hostname or "").lower()
        hb = (urlsplit(b).hostname or "").lower()
        return ha == hb or ha.endswith("." + hb) or hb.endswith("." + ha)
    except Exception:
        return False


def host_of(url):
    try:
        return (urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


def likely_bad_link(url, text=""):
    try:
        s = urlsplit(url)
        path = (s.path or "").lower()
        if path in ("", "/"):
            return True
        if any(path.endswith(ext) for ext in BAD_EXTENSIONS):
            return True
        bits = [x for x in re.split(r"[/_.-]+", path) if x]
        if any(w in BAD_LINK_WORDS for w in bits):
            return True
        t = re.sub(r"\s+", " ", text or "").strip().lower()
        if t in {"więcej", "more", "dalej", "next", "następna", "następny", ">", "›", "»"}:
            return True
        return False
    except Exception:
        return True


def site_profile(url):
    host = host_of(url)
    path = (urlsplit(url).path or "").lower()
    query = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
    if host == "tvn24.pl" or host.endswith(".tvn24.pl"):
        return {"name": "TVN24", "selector": "a[href*='-st']", "strict": True, "dynamic": False, "filter": ""}
    if (host == "tvp.info" or host.endswith(".tvp.info")) and path.rstrip("/") == "/tag" and query.get("tag"):
        # TVP Info renderuje listę tagu przez JavaScript. Po wyrenderowaniu
        # artykuły mają ścieżkę /<numeryczne_id>/<slug>.
        return {"name": "TVP INFO TAG", "selector": "a[href]", "strict": True, "dynamic": True, "filter": "tvp_numeric_article"}
    return {"name": "AUTO", "selector": "", "strict": False, "dynamic": False, "filter": ""}


# Pakiet startowy redakcji. Źródła są dodawane tylko raz jako migracja V15.2;
# późniejsze ręczne zmiany użytkownika nie są nadpisywane ani odtwarzane po usunięciu.
# Dla serwisów bez dedykowanego profilu używamy jednej strony głównej/listy i
# trybu nie-strict, aby mechanizm scoringu wybrał artykuły bez zgadywania paginacji.
DEFAULT_SOURCE_PRESETS = [
    {"name": "TVN24 — Najnowsze", "list_url": "https://tvn24.pl/najnowsze", "strict_selector": True, "list_mode": "auto", "max_articles": 40, "max_list_pages": 5, "enabled": True},
    {"name": "TVP Info — Najnowsze", "list_url": "https://www.tvp.info/", "strict_selector": False, "list_mode": "single", "max_articles": 40, "max_list_pages": 1, "enabled": True},
    {"name": "TV Republika — Najnowsze", "list_url": "https://tvrepublika.pl/", "strict_selector": False, "list_mode": "single", "max_articles": 40, "max_list_pages": 1, "enabled": True},
    {"name": "Polsat News — Najnowsze", "list_url": "https://www.polsatnews.pl/", "strict_selector": False, "list_mode": "single", "max_articles": 40, "max_list_pages": 1, "enabled": True},
    {"name": "RMF24 — Fakty", "list_url": "https://www.rmf24.pl/fakty", "strict_selector": False, "list_mode": "single", "max_articles": 40, "max_list_pages": 1, "enabled": True},
    {"name": "Radio ZET — Wiadomości", "list_url": "https://wiadomosci.radiozet.pl/", "strict_selector": False, "list_mode": "single", "max_articles": 40, "max_list_pages": 1, "enabled": True},
    {"name": "Onet — Wiadomości", "list_url": "https://wiadomosci.onet.pl/", "strict_selector": False, "list_mode": "single", "max_articles": 40, "max_list_pages": 1, "enabled": True},
    {"name": "Interia — Wydarzenia", "list_url": "https://wydarzenia.interia.pl/", "strict_selector": False, "list_mode": "single", "max_articles": 40, "max_list_pages": 1, "enabled": True},
    {"name": "WP — Wiadomości", "list_url": "https://wiadomosci.wp.pl/", "strict_selector": False, "list_mode": "single", "max_articles": 40, "max_list_pages": 1, "enabled": True},
    {"name": "Rzeczpospolita", "list_url": "https://www.rp.pl/", "strict_selector": False, "list_mode": "single", "max_articles": 40, "max_list_pages": 1, "enabled": True},
    {"name": "Money.pl — Gospodarka", "list_url": "https://www.money.pl/gospodarka/", "strict_selector": False, "list_mode": "single", "max_articles": 40, "max_list_pages": 1, "enabled": True},
    {"name": "Business Insider — Wiadomości", "list_url": "https://businessinsider.com.pl/wiadomosci", "strict_selector": False, "list_mode": "single", "max_articles": 40, "max_list_pages": 1, "enabled": True},
    {"name": "Forsal", "list_url": "https://forsal.pl/", "strict_selector": False, "list_mode": "single", "max_articles": 40, "max_list_pages": 1, "enabled": True},
    {"name": "Dziennik.pl", "list_url": "https://www.dziennik.pl/", "strict_selector": False, "list_mode": "single", "max_articles": 40, "max_list_pages": 1, "enabled": True},
    {"name": "Bankier — Wiadomości", "list_url": "https://www.bankier.pl/wiadomosc/", "strict_selector": False, "list_mode": "single", "max_articles": 40, "max_list_pages": 1, "enabled": True},
    {"name": "Puls Biznesu", "list_url": "https://www.pb.pl/", "strict_selector": False, "list_mode": "single", "max_articles": 40, "max_list_pages": 1, "enabled": True},
    {"name": "Niezależna", "list_url": "https://niezalezna.pl/", "strict_selector": False, "list_mode": "single", "max_articles": 40, "max_list_pages": 1, "enabled": True},
    {"name": "wPolityce", "list_url": "https://wpolityce.pl/", "strict_selector": False, "list_mode": "single", "max_articles": 40, "max_list_pages": 1, "enabled": True},
    {"name": "OKO.press", "list_url": "https://oko.press/", "strict_selector": False, "list_mode": "single", "max_articles": 40, "max_list_pages": 1, "enabled": True},
    {"name": "Do Rzeczy", "list_url": "https://dorzeczy.pl/", "strict_selector": False, "list_mode": "single", "max_articles": 40, "max_list_pages": 1, "enabled": True},
    {"name": "Tygodnik Solidarność", "list_url": "https://tysol.pl/", "strict_selector": False, "list_mode": "single", "max_articles": 40, "max_list_pages": 1, "enabled": True},
    # Dodatkowe źródła zapisujemy, ale domyślnie wyłączamy: częściej stosują
    # blokady 403/antybot lub cięższy frontend. Można je testować pojedynczo.
    {"name": "PAP — Aktualności [TEST]", "list_url": "https://www.pap.pl/aktualnosci", "strict_selector": False, "list_mode": "single", "max_articles": 30, "max_list_pages": 1, "enabled": False},
    {"name": "Gazeta Prawna [TEST]", "list_url": "https://www.gazetaprawna.pl/", "strict_selector": False, "list_mode": "single", "max_articles": 30, "max_list_pages": 1, "enabled": False},
    {"name": "WNP [TEST]", "list_url": "https://www.wnp.pl/", "strict_selector": False, "list_mode": "single", "max_articles": 30, "max_list_pages": 1, "enabled": False},
    {"name": "Portal Samorządowy [TEST]", "list_url": "https://www.portalsamorzadowy.pl/", "strict_selector": False, "list_mode": "single", "max_articles": 30, "max_list_pages": 1, "enabled": False},
]


def normalize_text(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _cjk_count(text):
    return sum(1 for ch in str(text or "") if ("\u3400" <= ch <= "\u4dbf") or ("\u4e00" <= ch <= "\u9fff") or ("\uf900" <= ch <= "\ufaff"))


def repair_polish_mojibake(text):
    """Naprawia typowy przypadek UTF-8 błędnie zinterpretowanego jako GBK/GB18030.
    Działa zachowawczo: zmiana jest przyjmowana tylko, gdy znika większość znaków CJK.
    """
    src = str(text or "")
    cjk_before = _cjk_count(src)
    if cjk_before < 1:
        return src
    # Jeżeli tekst jest głównie łaciński, a pojedyncze CJK występują wewnątrz polskich słów,
    # to jest charakterystyczny ślad UTF-8 -> GBK.
    latinish = sum(1 for ch in src if ch.isascii() and (ch.isalpha() or ch.isspace()))
    if latinish < max(20, len(src) // 8):
        return src
    best = src
    best_cjk = cjk_before
    for enc in ("gb18030", "gbk"):
        try:
            cand = src.encode(enc, errors="strict").decode("utf-8", errors="strict")
        except Exception:
            continue
        cjk_after = _cjk_count(cand)
        if cjk_after < best_cjk and cjk_after <= max(0, cjk_before // 5):
            best = cand
            best_cjk = cjk_after
    return best




def htmlish_to_text(value):
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value:
        return ""
    if "<" in value and ">" in value:
        soup = BeautifulSoup(value, "html.parser")
        blocks = []
        for el in soup.find_all(["p", "h2", "h3", "blockquote", "li"]):
            t = normalize_text(el.get_text(" ", strip=True))
            if t:
                blocks.append(t)
        if blocks:
            return "\n\n".join(blocks)
        return normalize_text(soup.get_text(" ", strip=True))
    return normalize_text(value)


def dedupe_blocks(blocks):
    out = []
    seen = set()
    for block in blocks:
        t = normalize_text(block)
        if not t:
            continue
        key = re.sub(r"\W+", "", t.lower())
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


class ArticleExtractor:
    DOM_SELECTORS = (
        "article",
        "[itemprop='articleBody']",
        "[data-testid*='article']",
        "[data-testid*='content']",
        "[class*='article-body']",
        "[class*='articleBody']",
        "[class*='article__body']",
        "[class*='article-content']",
        "[class*='articleContent']",
        "[class*='story-body']",
        "[class*='storyBody']",
        "main",
    )
    FLOW_STOP_RX = re.compile(
        r"^(?:zobacz także|najnowsze informacje|czytaj także|polecamy|więcej na temat|"
        r"udostępnij|tagi|komentarze|przeczytaj również|sprawdź także|oglądaj na żywo|reklama)\s*:?$",
        re.I,
    )
    UI_TEXT_RX = re.compile(
        r"^(?:posłuchaj artykułu|czyta lektor ai|wykup subskrypcję|masz subskrypcję|"
        r"dowiedz się więcej|dodaj nas do listy|kopiuj link|facebook|źródło zdj\.|źródło wideo)\b",
        re.I,
    )

    @staticmethod
    def _page_title(soup):
        for sel, attr in [
            ("meta[property='og:title']", "content"),
            ("meta[name='twitter:title']", "content"),
        ]:
            n = soup.select_one(sel)
            if n and n.get(attr):
                t = normalize_text(n.get(attr))
                if t:
                    return t
        h1 = soup.find("h1")
        if h1:
            t = normalize_text(h1.get_text(" ", strip=True))
            if t:
                return t
        if soup.title:
            return normalize_text(soup.title.get_text(" ", strip=True))
        return ""

    @staticmethod
    def _canonical(soup, url):
        n = soup.select_one("link[rel='canonical']")
        if n and n.get("href"):
            u = canonicalize_url(urljoin(url, n.get("href")))
            if u:
                return u
        n = soup.select_one("meta[property='og:url']")
        if n and n.get("content"):
            u = canonicalize_url(urljoin(url, n.get("content")))
            if u:
                return u
        return canonicalize_url(url)

    @staticmethod
    def _strip_noise(node):
        for tag in node.find_all(["script", "style", "noscript", "svg", "canvas", "form", "nav", "footer", "aside"]):
            tag.decompose()
        for el in list(node.find_all(True)):
            attrs = " ".join([
                " ".join(el.get("class", [])) if isinstance(el.get("class"), list) else str(el.get("class", "")),
                str(el.get("id", "")), str(el.get("role", "")), str(el.get("data-testid", "")),
            ]).strip()
            if attrs and NOISE_RX.search(attrs):
                el.decompose()

    @classmethod
    def _is_noise_node(cls, node):
        if node is None:
            return True
        for p in [node] + list(node.parents):
            name = getattr(p, "name", None)
            if name in {"nav", "footer", "aside", "form", "noscript", "script", "style"}:
                return True
            if not getattr(p, "attrs", None):
                continue
            attrs = " ".join([
                " ".join(p.get("class", [])) if isinstance(p.get("class"), list) else str(p.get("class", "")),
                str(p.get("id", "")), str(p.get("role", "")), str(p.get("data-testid", "")),
            ]).strip()
            if attrs and NOISE_RX.search(attrs):
                return True
        return False

    @staticmethod
    def _node_text(node):
        blocks = []
        for el in node.find_all(["p", "h2", "h3", "blockquote", "li"]):
            t = normalize_text(el.get_text(" ", strip=True))
            if not t:
                continue
            if el.name == "li" and len(t) < 30:
                continue
            blocks.append(t)
        blocks = dedupe_blocks(blocks)
        if not blocks:
            raw = normalize_text(node.get_text(" ", strip=True))
            if raw:
                blocks = [raw]
        return "\n\n".join(blocks), len(blocks)

    @classmethod
    def _dom_candidates(cls, soup):
        candidates = []
        seen = set()
        for selector in cls.DOM_SELECTORS:
            try:
                for node in soup.select(selector):
                    ident = id(node)
                    if ident in seen:
                        continue
                    seen.add(ident)
                    frag = BeautifulSoup(str(node), "html.parser")
                    cls._strip_noise(frag)
                    text, blocks = cls._node_text(frag)
                    if not text:
                        continue
                    links_chars = sum(len(normalize_text(a.get_text(" ", strip=True))) for a in frag.find_all("a"))
                    link_density = links_chars / max(1, len(text))
                    source_bonus = 0
                    sl = selector.lower()
                    if selector == "article" or "articlebody" in sl or "article-body" in sl or "article__body" in sl:
                        source_bonus = 1800
                    elif "article" in sl or "story" in sl:
                        source_bonus = 1200
                    elif selector == "main":
                        source_bonus = -300
                    score = len(text) + blocks * 140 + source_bonus - int(len(text) * min(0.9, link_density) * 1.6)
                    candidates.append({
                        "method": "dom:" + selector,
                        "text": text,
                        "blocks": blocks,
                        "score": score,
                    })
            except Exception:
                continue
        return candidates

    @classmethod
    def _h1_flow_candidate(cls, soup):
        """Fallback dla serwisów, które nie mają stabilnego <article>.
        Zbiera długie bloki po nagłówku H1 i zatrzymuje się na sekcjach typu
        „Zobacz także”, „Najnowsze informacje”, tagi itd.
        """
        h1 = soup.find("h1")
        if not h1:
            return []
        blocks = []
        started = False
        scanned = 0
        for el in h1.find_all_next(["p", "h2", "h3", "blockquote"]):
            scanned += 1
            if scanned > 260:
                break
            if cls._is_noise_node(el):
                continue
            t = normalize_text(el.get_text(" ", strip=True))
            if not t:
                continue
            if cls.FLOW_STOP_RX.match(t):
                if started:
                    break
                continue
            if cls.UI_TEXT_RX.match(t):
                continue
            link_chars = sum(len(normalize_text(a.get_text(" ", strip=True))) for a in el.find_all("a"))
            if link_chars / max(1, len(t)) > 0.55:
                continue
            if el.name in {"h2", "h3"}:
                if started and len(t) >= 8:
                    blocks.append(t)
                continue
            is_source = bool(re.match(r"^źródło\s*:", t, re.I))
            if len(t) < 55 and not is_source:
                continue
            letters = sum(ch.isalpha() for ch in t)
            if not is_source and letters < max(25, int(len(t) * 0.45)):
                continue
            started = True
            blocks.append(t)
        blocks = dedupe_blocks(blocks)
        text = "\n\n".join(blocks)
        if len(text) < 180 or len(blocks) < 2:
            return []
        return [{
            "method": "dom:h1-flow",
            "text": text,
            "blocks": len(blocks),
            "score": len(text) + len(blocks) * 170 + 2600,
        }]

    @staticmethod
    def _json_walk(obj, path="root"):
        found = []
        if isinstance(obj, dict):
            typ = normalize_text(obj.get("@type", "")).lower()
            for key, value in obj.items():
                lk = str(key).lower()
                p = f"{path}.{key}"
                if lk == "articlebody" and isinstance(value, str):
                    txt = htmlish_to_text(value)
                    if txt:
                        found.append((txt, 6500, "json:articleBody", p))
                elif lk in {"body", "content", "text"} and isinstance(value, str):
                    if any(x in typ for x in ("article", "news", "report", "story")):
                        txt = htmlish_to_text(value)
                        if len(txt) >= 180:
                            found.append((txt, 2200, f"json:{key}", p))
                elif lk == "description" and isinstance(value, str):
                    # Description jest zwykle tylko leadem/snippetem. Nie może wygrać
                    # z pełnym korpusem artykułu tylko dzięki wysokiemu bonusowi.
                    if any(x in typ for x in ("article", "news", "report", "story")):
                        txt = htmlish_to_text(value)
                        if len(txt) >= 100:
                            found.append((txt, 100, "json:description", p))
                elif lk in {"paragraphs", "blocks"} and isinstance(value, list):
                    bits = []
                    for x in value:
                        if isinstance(x, str):
                            t = htmlish_to_text(x)
                            if t:
                                bits.append(t)
                        elif isinstance(x, dict):
                            for kk in ("text", "content", "body", "value"):
                                if isinstance(x.get(kk), str):
                                    t = htmlish_to_text(x.get(kk))
                                    if t:
                                        bits.append(t)
                    txt = "\n\n".join(dedupe_blocks(bits))
                    if len(txt) >= 180:
                        found.append((txt, 3000, f"json:{key}", p))
                found.extend(ArticleExtractor._json_walk(value, p))
        elif isinstance(obj, list):
            for i, value in enumerate(obj):
                found.extend(ArticleExtractor._json_walk(value, f"{path}[{i}]"))
        return found

    @classmethod
    def _json_candidates(cls, soup):
        out = []
        scripts = []
        scripts.extend(soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}))
        next_data = soup.find("script", id="__NEXT_DATA__")
        if next_data:
            scripts.append(next_data)
        for s in soup.find_all("script", attrs={"type": re.compile(r"application/json", re.I)}):
            if s not in scripts:
                scripts.append(s)
        for idx, script in enumerate(scripts):
            raw = script.string or script.get_text("", strip=True)
            raw = (raw or "").strip()
            if not raw or len(raw) > 15_000_000:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            for text, bonus, method, path in cls._json_walk(data, f"script[{idx}]"):
                blocks = max(1, text.count("\n\n") + 1)
                out.append({
                    "method": method,
                    "text": text,
                    "blocks": blocks,
                    "score": len(text) + blocks * 130 + bonus,
                    "json_path": path,
                })
        return out

    @staticmethod
    def _decode_json_string_token(token):
        try:
            return json.loads(token)
        except Exception:
            return ""

    @classmethod
    def _script_stream_candidates(cls, soup):
        """Odzyskuje treść z osadzonych strumieni JS/Next.js.
        Część nowoczesnych serwisów trzyma pełne akapity w skryptach, mimo że
        klasyczny DOM widziany przez requests zawiera tylko shell i lead.
        """
        text_bits = []
        for script in soup.find_all("script"):
            raw = script.string or script.get_text("", strip=True)
            raw = (raw or "").strip()
            if len(raw) < 200 or len(raw) > 12_000_000:
                continue

            payloads = [raw]
            # Next.js Flight: self.__next_f.push([1,"..."])
            for m in re.finditer(r"self\.__next_f\.push\(\[\s*\d+\s*,\s*(\"(?:\\.|[^\"\\])*\")\s*\]\)", raw, re.S):
                dec = cls._decode_json_string_token(m.group(1))
                if dec:
                    payloads.append(dec)

            for payload in payloads:
                # Jeżeli po rozkodowaniu mamy JSON, wykorzystaj normalny rekursywny parser.
                p = payload.strip()
                if p[:1] in "[{":
                    try:
                        data = json.loads(p)
                    except Exception:
                        data = None
                    if data is not None:
                        for txt, _bonus, _method, _path in cls._json_walk(data, "embedded"):
                            if len(txt) >= 80:
                                text_bits.append(txt)

                # Długie teksty zakodowane jako literały JS/JSON. Menu, identyfikatory,
                # URL-e i etykiety są krótkie, więc filtr długości/słów je odrzuca.
                for m in re.finditer(r'\"(?:\\.|[^\"\\])*\"', payload, re.S):
                    token = m.group(0)
                    if len(token) < 80 or len(token) > 80_000:
                        continue
                    val = cls._decode_json_string_token(token)
                    val = htmlish_to_text(val)
                    val = normalize_text(val)
                    if len(val) < 65 or val.startswith(("http://", "https://")):
                        continue
                    if cls.UI_TEXT_RX.match(val) or cls.FLOW_STOP_RX.match(val):
                        continue
                    words = val.split()
                    letters = sum(ch.isalpha() for ch in val)
                    if len(words) < 9 or letters < int(len(val) * 0.45):
                        continue
                    text_bits.append(val)

        bits = dedupe_blocks(text_bits)
        # Bardzo krótkie zbiory są częściej leadem/rekomendacją niż artykułem.
        text = "\n\n".join(bits)
        if len(text) < 450 or len(bits) < 3:
            return []
        return [{
            "method": "script:embedded-text",
            "text": text,
            "blocks": len(bits),
            "score": len(text) + len(bits) * 120 + 1800,
        }]

    @staticmethod
    def _json_metadata_walk(obj):
        authors = []
        tags = []

        def add_author(value):
            if isinstance(value, str):
                t = normalize_text(value)
                if 2 <= len(t) <= 120:
                    authors.append(t)
            elif isinstance(value, dict):
                if isinstance(value.get("name"), str):
                    add_author(value.get("name"))
                for k in ("givenName", "familyName"):
                    if isinstance(value.get(k), str):
                        pass
                gn = normalize_text(value.get("givenName", "")) if isinstance(value.get("givenName"), str) else ""
                fn = normalize_text(value.get("familyName", "")) if isinstance(value.get("familyName"), str) else ""
                if gn or fn:
                    add_author((gn + " " + fn).strip())
            elif isinstance(value, list):
                for x in value:
                    add_author(x)

        def add_tags(value):
            vals = []
            if isinstance(value, str):
                vals = re.split(r"[,;|]", value)
            elif isinstance(value, list):
                vals = value
            for x in vals:
                if isinstance(x, dict):
                    x = x.get("name", "")
                if not isinstance(x, str):
                    continue
                t = normalize_text(x)
                if 2 <= len(t) <= 90:
                    tags.append(t)

        def walk(value):
            if isinstance(value, dict):
                typ = normalize_text(value.get("@type", "")).lower()
                if any(x in typ for x in ("article", "news", "report", "story")):
                    if "author" in value:
                        add_author(value.get("author"))
                    if "creator" in value:
                        add_author(value.get("creator"))
                    if "keywords" in value:
                        add_tags(value.get("keywords"))
                for v in value.values():
                    walk(v)
            elif isinstance(value, list):
                for v in value:
                    walk(v)

        walk(obj)
        return authors, tags

    @classmethod
    def _article_metadata(cls, soup):
        authors = []
        tags = []

        def add_author(v):
            t = normalize_text(v or "")
            t = re.sub(r"^(?:oprac\.|opracowanie|autor(?:ka)?|tekst)\s*[:.-]?\s*", "", t, flags=re.I)
            if (2 <= len(t) <= 120 and not t.lower().startswith(("http://", "https://"))
                    and t.lower() not in {"tvn24", "redakcja", "autor"}):
                authors.append(t)

        def add_tag(v):
            t = normalize_text(v or "")
            if 2 <= len(t) <= 90 and not re.match(r"^(?:tagi?|więcej|zobacz|udostępnij)$", t, re.I):
                tags.append(t)

        # JSON-LD / JSON osadzony w stronie.
        for script in soup.find_all("script", attrs={"type": re.compile(r"application/(?:ld\+)?json", re.I)}):
            raw = script.string or script.get_text("", strip=True)
            raw = (raw or "").strip()
            if not raw or len(raw) > 15_000_000:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            aa, tt = cls._json_metadata_walk(data)
            for x in aa:
                add_author(x)
            for x in tt:
                add_tag(x)

        # Meta-tagowe źródła autora i tagów.
        for sel in [
            "meta[name='author']", "meta[property='author']", "meta[property='article:author']",
            "meta[name='byl']", "meta[name='parsely-author']",
        ]:
            for n in soup.select(sel):
                add_author(n.get("content", ""))
        for sel in [
            "meta[property='article:tag']", "meta[name='news_keywords']", "meta[name='keywords']",
            "meta[name='parsely-tags']",
        ]:
            for n in soup.select(sel):
                raw = n.get("content", "")
                for x in re.split(r"[,;|]", raw):
                    add_tag(x)

        # Górna linia byline, np. TVN24: "Oprac. Wiktor Knowski | 24.09...".
        # Robimy to przed szerokim szukaniem klas "author", żeby nie zebrać autorów
        # z kafelków polecanych materiałów.
        raw_lines = soup.get_text("\n", strip=True).splitlines()
        for line in raw_lines[:180]:
            line = normalize_text(line)
            m = re.match(r"^(?:Oprac\.|Opracowanie|Autor(?:ka)?|Tekst)\s*[:.-]?\s*(.+?)(?:\s*\|\s*.*)?$", line, re.I)
            if m:
                add_author(m.group(1))

        # Widoczny autor / byline – tylko gdy wcześniejsze, pewniejsze źródła nic nie dały.
        if not authors:
            for sel in [
                "[itemprop='author']", "[rel='author']", "[data-testid*='author']", "[data-testid*='byline']",
                "[class*='author']", "[class*='Author']", "[class*='byline']", "[class*='Byline']",
            ]:
                try:
                    for n in soup.select(sel)[:20]:
                        t = normalize_text(n.get_text(" ", strip=True))
                        if not t or len(t) > 180:
                            continue
                        parts = []
                        for c in n.find_all(["a", "span", "strong"], recursive=True):
                            ct = normalize_text(c.get_text(" ", strip=True))
                            if 2 <= len(ct) <= 90:
                                parts.append(ct)
                        if parts:
                            for ct in parts[:4]:
                                if not re.search(r"dziennikarz|redakcj|działu|tvn24\.pl|wydawca", ct, re.I):
                                    add_author(ct)
                        else:
                            add_author(t)
                except Exception:
                    pass

        # Widoczna sekcja "TAGI:" – zbieramy tylko lokalne linki/krótkie elementy obok etykiety,
        # nie wielką chmurę tagów z dołu strony.
        labels = soup.find_all(string=re.compile(r"^\s*tagi\s*:?\s*$", re.I))
        for label in labels[:5]:
            node = getattr(label, "parent", None)
            if node is None:
                continue
            candidates = []
            parent = node.parent
            if parent is not None:
                candidates.append(parent)
                if parent.parent is not None and len(normalize_text(parent.parent.get_text(" ", strip=True))) < 800:
                    candidates.append(parent.parent)
            for container in candidates:
                for a in container.find_all("a")[:30]:
                    add_tag(a.get_text(" ", strip=True))
                # Gdy tagi nie są linkami, spróbuj krótkich spanów po etykiecie.
                if not tags:
                    for sp in container.find_all(["span", "li"])[1:30]:
                        add_tag(sp.get_text(" ", strip=True))
            if tags:
                break

        def uniq(seq, limit):
            out = []
            seen = set()
            for x in seq:
                k = x.casefold()
                if k in seen:
                    continue
                seen.add(k)
                out.append(x)
                if len(out) >= limit:
                    break
            return out

        return uniq(authors, 5), uniq(tags, 30)

    @staticmethod
    def _meta_datetime_parts(value):
        """Zwraca (pełna_wartość, data, godzina) bez zgadywania strefy/czasu.

        Serwisy podają daty w kilku formatach. Zachowujemy oryginalną, jawną
        wartość, a datę/godzinę wyciągamy tylko wtedy, gdy są w niej czytelne.
        """
        raw = normalize_text(value or "")
        if not raw:
            return "", "", ""

        date_part = ""
        time_part = ""

        # ISO: 2026-09-25T14:32:00+02:00 / 2026-09-25 14:32
        m = re.search(r"\b(\d{4}-\d{2}-\d{2})[T\s]+(\d{1,2}:\d{2})(?::\d{2})?", raw)
        if m:
            date_part, time_part = m.group(1), m.group(2)
            return raw, date_part, time_part

        # Polska forma: 25.09.2026, 14:32 / 25-09-2026 14:32
        m = re.search(r"\b(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{4})(?:\s*(?:r\.)?\s*[,|]\s*|\s+)(\d{1,2}:\d{2})", raw)
        if m:
            date_part, time_part = m.group(1), m.group(2)
            return raw, date_part, time_part

        m = re.search(r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}[.\-/]\d{1,2}[.\-/]\d{4})\b", raw)
        if m:
            date_part = m.group(1)
        m = re.search(r"\b([01]?\d|2[0-3]):[0-5]\d\b", raw)
        if m:
            time_part = m.group(0)
        return raw, date_part, time_part

    @classmethod
    def _extended_metadata(cls, soup, url="", canonical="", article_text=""):
        """Wyciąga tylko jawne, stałe metadane artykułu.

        Priorytet: dane Article/NewsArticle w JSON-LD -> meta-tag -> widoczny tekst.
        Brak wartości pozostaje brakiem; funkcja nie zgaduje autora, redakcji,
        kategorii ani daty na podstawie nazwy domeny.
        """
        result = {
            "published_at": "",
            "published_date": "",
            "published_time": "",
            "modified_at": "",
            "modified_date": "",
            "modified_time": "",
            "publisher": "",
            "site_name": "",
            "section": "",
            "source_name": "",
            "article_id": "",
        }

        def clean(v, limit=300):
            if isinstance(v, (int, float)):
                v = str(v)
            if not isinstance(v, str):
                return ""
            t = repair_polish_mojibake(normalize_text(v))
            if not t or len(t) > limit:
                return ""
            return t

        def entity_name(v):
            if isinstance(v, str):
                return clean(v, 180)
            if isinstance(v, dict):
                return clean(v.get("name") or v.get("legalName") or "", 180)
            if isinstance(v, list):
                for x in v:
                    n = entity_name(x)
                    if n:
                        return n
            return ""

        def set_once(key, value, limit=300):
            if result.get(key):
                return
            v = clean(value, limit)
            if v:
                result[key] = v

        def article_json_walk(value):
            if isinstance(value, dict):
                typ_raw = value.get("@type", "")
                if isinstance(typ_raw, list):
                    typ = " ".join(str(x) for x in typ_raw).lower()
                else:
                    typ = normalize_text(str(typ_raw)).lower()
                is_article = any(x in typ for x in ("article", "news", "report", "story"))
                if is_article:
                    set_once("published_at", value.get("datePublished") or value.get("dateCreated"), 120)
                    set_once("modified_at", value.get("dateModified"), 120)
                    section = value.get("articleSection")
                    if isinstance(section, list):
                        section = " / ".join(clean(x, 100) for x in section if clean(x, 100))
                    set_once("section", section, 250)
                    if not result["publisher"]:
                        result["publisher"] = entity_name(value.get("publisher"))
                    if not result["source_name"]:
                        result["source_name"] = entity_name(value.get("sourceOrganization") or value.get("provider"))
                    ident = value.get("identifier")
                    if isinstance(ident, dict):
                        ident = ident.get("value") or ident.get("@id") or ident.get("name")
                    elif isinstance(ident, list):
                        ident = next((x for x in ident if isinstance(x, (str, int, float))), "")
                    set_once("article_id", ident, 180)
                for v in value.values():
                    article_json_walk(v)
            elif isinstance(value, list):
                for v in value:
                    article_json_walk(v)

        # JSON-LD i jawne JSON-y zawierające obiekt Article/NewsArticle.
        for script in soup.find_all("script", attrs={"type": re.compile(r"application/(?:ld\+)?json", re.I)}):
            raw = script.string or script.get_text("", strip=True)
            raw = (raw or "").strip()
            if not raw or len(raw) > 15_000_000:
                continue
            try:
                article_json_walk(json.loads(raw))
            except Exception:
                continue

        def meta_first(selectors):
            for sel in selectors:
                for n in soup.select(sel):
                    v = clean(n.get("content", ""), 300)
                    if v:
                        return v
            return ""

        if not result["published_at"]:
            result["published_at"] = meta_first([
                "meta[property='article:published_time']", "meta[name='datePublished']",
                "meta[name='date']", "meta[name='pubdate']", "meta[name='publish-date']",
                "meta[name='parsely-pub-date']", "meta[name='citation_publication_date']",
                "meta[name='dc.date']", "meta[name='dcterms.date']",
            ])
        if not result["modified_at"]:
            result["modified_at"] = meta_first([
                "meta[property='article:modified_time']", "meta[property='og:updated_time']",
                "meta[name='dateModified']", "meta[name='last-modified']",
                "meta[name='parsely-modified']",
            ])
        if not result["section"]:
            result["section"] = meta_first([
                "meta[property='article:section']", "meta[name='section']",
                "meta[name='articleSection']", "meta[name='parsely-section']",
            ])
        if not result["publisher"]:
            result["publisher"] = meta_first([
                "meta[name='publisher']", "meta[name='citation_publisher']",
            ])
        result["site_name"] = meta_first([
            "meta[property='og:site_name']", "meta[name='application-name']",
            "meta[name='citation_journal_title']",
        ])
        if not result["source_name"]:
            result["source_name"] = meta_first([
                "meta[name='citation_source']", "meta[name='source']",
            ])

        # Jawna linia „Źródło: ...” z właściwego korpusu artykułu.
        if not result["source_name"] and article_text:
            for line in article_text.splitlines():
                t = normalize_text(line)
                m = re.match(r"^źródło\s*:\s*(.{2,180})$", t, re.I)
                if m:
                    result["source_name"] = clean(m.group(1), 180)
                    break
            if not result["source_name"]:
                # Część ekstraktorów/JSON-LD spłaszcza akapity do jednej linii.
                # Gdy „Źródło:” jest końcowym polem artykułu, nadal je odzyskujemy.
                m = re.search(r"\bźródło\s*:\s*([^\n\r]{2,180}?)\s*$", article_text, re.I)
                if m:
                    result["source_name"] = clean(m.group(1), 180)

        # Fallback daty tylko z widocznego początku strony/byline. Nie zgadujemy
        # dat z innych elementów na dole strony.
        if not result["published_at"]:
            for line in soup.get_text("\n", strip=True).splitlines()[:180]:
                t = normalize_text(line)
                if len(t) > 220:
                    continue
                m = re.search(
                    r"\b\d{1,2}[.\-/]\d{1,2}[.\-/]\d{4}\b"
                    r"(?:\s*r\.)?\s*(?:[,|]\s*)?"
                    r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b",
                    t,
                )
                if m:
                    result["published_at"] = clean(m.group(0), 120)
                    break

        # ID: najpierw jawny identifier. Dla TVN24 numer po „-st” jest częścią
        # kanonicznego identyfikatora artykułu, więc można go bezpiecznie odczytać.
        if not result["article_id"]:
            id_url = canonical or url or ""
            m = re.search(r"-st(\d{5,})(?:$|[/?#])", id_url, re.I)
            if m:
                result["article_id"] = m.group(1)

        pub_raw, pub_date, pub_time = cls._meta_datetime_parts(result["published_at"])
        mod_raw, mod_date, mod_time = cls._meta_datetime_parts(result["modified_at"])
        result["published_at"] = pub_raw
        result["published_date"] = pub_date
        result["published_time"] = pub_time
        result["modified_at"] = mod_raw
        result["modified_date"] = mod_date
        result["modified_time"] = mod_time
        return result

    @classmethod
    def _clean_candidate_text(cls, text, page_title="", authors=None):
        """Czyści właściwy korpus artykułu bez ucinania metadanych.

        Sekcje polecanych materiałów są pomijane, ale autor i tagi są pobierani
        osobno z całej strony, więc nie trzeba trzymać w korpusie reklamowych kart.
        """
        authors = authors or []
        raw_blocks = [normalize_text(x) for x in re.split(r"\n{2,}", text or "")]
        raw_blocks = [x for x in raw_blocks if x]
        out = []
        skip_related = 0
        title_key = normalize_text(page_title).casefold()
        author_keys = {normalize_text(a).casefold() for a in authors if a}

        hard_end = re.compile(
            r"^(?:zobacz także|najnowsze informacje|oglądaj na żywo|więcej z tvn24|"
            r"przeczytaj również|sprawdź także|polecamy)\s*:?$", re.I
        )
        soft_end = re.compile(r"^(?:udostępnij|tagi|komentarze)\s*:?$", re.I)
        inline_related = re.compile(r"^(?:dowiedz się więcej|czytaj także|więcej na temat)\s*:?$", re.I)

        for block in raw_blocks:
            b = normalize_text(block)
            if not b:
                continue
            bk = b.casefold()

            if title_key and bk == title_key:
                continue
            if cls.UI_TEXT_RX.match(b):
                continue

            if inline_related.match(b):
                # W TVN24 po tej etykiecie występują zwykle: tytuł polecanego materiału + autor.
                skip_related = 2
                continue
            if skip_related:
                skip_related -= 1
                continue

            body_chars = sum(len(x) for x in out)
            if hard_end.match(b):
                if body_chars >= 250:
                    break
                continue
            if soft_end.match(b):
                if body_chars >= 500:
                    break
                continue

            # Nie duplikuj autora w korpusie – trafi na koniec pliku jako metadane.
            if bk in author_keys:
                continue
            if any(ak and ak in bk and len(b) < 180 for ak in author_keys):
                if re.search(r"dziennikarz|redakcj|działu|tvn24\.pl|oprac\.", b, re.I):
                    continue

            # Typowe etykiety/śmieci interfejsu, ale bez wycinania źródła artykułu.
            if re.match(r"^(?:facebook|kopiuj link|link skopiowany|dodaj do preferowanych źródeł|reklama)$", b, re.I):
                continue
            out.append(b)

        return "\n\n".join(dedupe_blocks(out)).strip()

    @staticmethod
    def _method_bonus(method):
        m = (method or "").lower()
        if "articlebody" in m:
            return 5200
        if m == "dom:article" or "article-body" in m or "article__body" in m:
            return 2600
        if "h1-flow" in m:
            return 2200
        if m.startswith("json:"):
            return 1800
        if "article" in m or "story" in m:
            return 1100
        if "script:" in m:
            return 700
        if m == "dom:main":
            return -700
        return 0

    @classmethod
    def extract(cls, html_text, url=""):
        soup = BeautifulSoup(html_text or "", "html.parser")
        page_title = cls._page_title(soup)
        canonical = cls._canonical(soup, url)
        authors, tags = cls._article_metadata(soup)
        candidates = (
            cls._json_candidates(soup)
            + cls._dom_candidates(soup)
            + cls._h1_flow_candidate(soup)
            + cls._script_stream_candidates(soup)
        )
        cleaned = []
        for c in candidates:
            txt = cls._clean_candidate_text(c.get("text", ""), page_title, authors)
            if not txt:
                continue
            cc = dict(c)
            cc["text"] = txt
            cc["blocks"] = max(1, txt.count("\n\n") + 1)
            cc["score"] = len(txt) + cc["blocks"] * 140 + cls._method_bonus(cc.get("method", ""))
            cleaned.append(cc)
        candidates = cleaned
        if not candidates:
            extra_meta = cls._extended_metadata(soup, url=url, canonical=canonical, article_text="")
            return {
                "title": page_title, "page_title": page_title, "canonical": canonical,
                "text": "", "text_chars": 0, "blocks": 0, "method": "none", "score": -1,
                "authors": authors, "tags": tags, "candidates": [], "extended_metadata": extra_meta,
            }
        candidates.sort(key=lambda x: (x.get("score", 0), len(x.get("text", ""))), reverse=True)
        best = candidates[0]
        text = unicodedata.normalize("NFC", best.get("text", "").strip())
        diagnostics = [
            {
                "method": c.get("method", ""),
                "chars": len(c.get("text", "")),
                "blocks": int(c.get("blocks", 0) or 0),
                "score": int(c.get("score", 0) or 0),
            }
            for c in candidates[:6]
        ]
        extra_meta = cls._extended_metadata(soup, url=url, canonical=canonical, article_text=text)
        return {
            "title": unicodedata.normalize("NFC", page_title or "artykul"),
            "page_title": page_title,
            "canonical": canonical,
            "text": text,
            "text_chars": len(text),
            "blocks": int(best.get("blocks", 0) or 0),
            "method": best.get("method", "unknown"),
            "score": best.get("score", 0),
            "json_path": best.get("json_path", ""),
            "authors": authors,
            "tags": tags,
            "extended_metadata": extra_meta,
            "candidates": diagnostics,
        }

    @staticmethod
    def _visible_page_text(html_text):
        """Tekst widoczny dla użytkownika; bez JS/CSS/noscript.

        Detektor blokady nie może analizować surowego HTML, bo poprawne serwisy
        często zawierają w skryptach/noscript frazy typu "enable javascript".
        """
        try:
            soup = BeautifulSoup(html_text or "", "html.parser")
            for node in soup(["script", "style", "template", "noscript", "svg"]):
                node.decompose()
            return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
        except Exception:
            return ""

    @classmethod
    def _looks_like_block_page(cls, extracted, html_text):
        title = re.sub(r"\s+", " ", extracted.get("title") or "").strip()
        article = re.sub(r"\s+", " ", extracted.get("text") or "").strip()
        visible = cls._visible_page_text(html_text)

        # Tytuł blokady jest sygnałem bardzo mocnym.
        if BLOCKED_RX.search(title):
            return True

        # Analizujemy wyłącznie widoczny początek strony, nigdy surowy HTML.
        head = re.sub(r"\s+", " ", (visible or article)[:2200]).strip()
        strong_hits = len(BLOCKED_RX.findall(head))
        weak_hits = len(BLOCKED_WEAK_RX.findall(head))

        # Prawdziwa strona challenge zwykle jest krótka i zawiera mocne frazy.
        if strong_hits >= 2 and len(visible) < 7000:
            return True
        if strong_hits >= 1 and weak_hits >= 1 and len(visible) < 3500:
            return True

        # Nie odrzucamy pełnego artykułu tylko dlatego, że tekst wspomina np.
        # o CAPTCHA/Cloudflare lub strona ma fallback JavaScript.
        return False

    @classmethod
    def validate(cls, extracted, html_text, min_chars):
        text = (extracted.get("text") or "").strip()
        chars = len(text)
        if cls._looks_like_block_page(extracted, html_text):
            raise ValueError("strona blokady/CAPTCHA zamiast artykułu")
        if chars < min_chars:
            diag = ", ".join(
                f"{x.get('method')}={x.get('chars')}"
                for x in (extracted.get("candidates") or [])[:4]
            )
            extra = f"; kandydaci: {diag}" if diag else ""
            raise ValueError(f"za mało treści artykułu: {chars} < {min_chars} znaków; metoda={extracted.get('method','none')}{extra}")
        letters = sum(ch.isalpha() for ch in text)
        if letters < max(80, int(chars * 0.35)):
            raise ValueError("treść nie wygląda jak artykuł (za mało tekstu właściwego)")
        if extracted.get("method") == "dom:main" and extracted.get("blocks", 0) < 2:
            raise ValueError("znaleziono tylko ogólny kontener strony, brak pewnego korpusu artykułu")
        return True


@dataclass
class Settings:
    list_url: str
    selector: str = ""
    strict_selector: bool = True
    max_articles: int = 100
    max_list_pages: int = 100
    list_mode: str = "auto"  # auto | pages | scroll | single
    page_template: str = ""
    retry: int = 1
    min_chars: int = 300
    pause: float = 8.0
    skip_processed: bool = True
    output_dir: str = ""
    source_id: int = 0
    source_name: str = ""


@dataclass
class FetchResult:
    final_url: str
    text: str
    status: int
    content_type: str
    bytes_len: int
    profile: str


class Downloader:
    def __init__(self):
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.status = "Gotowe"
        self.running = False
        self.progress = {
            "done": 0, "failed": 0, "found": 0, "attempted": 0,
            "skipped": 0, "list_page": 0, "current": "",
        }
        self.last_test_links = []
        self.recent_failures = []
        self.session = requests.Session()
        # LOW-IMPACT: jeden zewnętrzny GET naraz, cache i stały odstęp.
        self.net_lock = threading.Lock()
        self.cache_lock = threading.RLock()
        self.response_cache = {}
        self.json_cache = {}
        self.cache_ttl = 300.0
        self.last_external_request = 0.0
        self.request_interval = 8.0
        self.allowed_host = ""
        self.fatal_stop_reason = ""
        self.active_source_id = 0
        self.active_source_name = ""
        self.batch_running = False
        self.batch_index = 0
        self.batch_total = 0
        self.batch_done_sources = 0
        self.network_stats = {"requests": 0, "cache_hits": 0, "redirects": 0, "browser_requests": 0, "browser_blocked": 0, "browser_pages": 0, "browser_scroll_rounds": 0}
        self.runtime_platform = runtime_platform()
        self.storage_mode = "filesystem"
        self.saf_root_uri = ""
        self.saf_label = ""
        self.db_path = self._global_db_path()
        self.saf_state_path = self.db_path.parent / "saf_target.json"
        self.output_root = self._default_output_root()
        self._bind_output_paths(self.output_root)
        self._init_db()
        self._seed_default_sources_once()
        if self.runtime_platform == "android-termux":
            self._restore_saf_target()
        self.cleanup_stats = self._cleanup_disposable_files(self.output_root)
        self._sync_archive_to_db(self.output_root)
        if self.runtime_platform == "android-termux":
            shared_ok = False
            for p in (Path.home() / "storage" / "downloads", Path("/storage/emulated/0/Download")):
                try:
                    shared_ok = shared_ok or (p.exists() and os.access(p, os.W_OK))
                except Exception:
                    pass
            if not shared_ok and self.storage_mode != "saf":
                self.status = "Gotowe · Android: brak dostępu do wspólnego Pobrane. W Termux wykonaj raz: termux-setup-storage"

    def _global_db_path(self):
        """Stała baza historii niezależna od folderu zapisu i wersji programu."""
        if self.runtime_platform == "android-native":
            root = Path(os.environ.get("HOME") or str(Path.home())) / "PrzewijakArtykuly"
            root.mkdir(parents=True, exist_ok=True)
            return root / "baza_artykulow.sqlite3"
        if self.runtime_platform == "android-termux":
            legacy_db = Path.home() / "AppData" / "Local" / "PrzewijakArtykuly" / "baza_artykulow.sqlite3"
            if legacy_db.exists():
                # Zachowaj dokładnie tę samą historię deduplikacji z wcześniejszej wersji.
                return legacy_db
            root = Path.home() / ".local" / "share" / "PrzewijakArtykuly"
            root.mkdir(parents=True, exist_ok=True)
            return root / "baza_artykulow.sqlite3"
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        root = Path(base) / "PrzewijakArtykuly"
        root.mkdir(parents=True, exist_ok=True)
        return root / "baza_artykulow.sqlite3"

    def _db_connect(self):
        con = sqlite3.connect(str(self.db_path), timeout=15)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        return con

    def _init_db(self):
        with self._db_connect() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS articles (
                    canonical TEXT PRIMARY KEY,
                    url TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL DEFAULT '',
                    saved_at TEXT NOT NULL DEFAULT '',
                    output_folder TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'saved',
                    source_id INTEGER NOT NULL DEFAULT 0,
                    source_name TEXT NOT NULL DEFAULT ''
                )
            """)
            cols = {row[1] for row in con.execute("PRAGMA table_info(articles)")}
            if "source_id" not in cols:
                con.execute("ALTER TABLE articles ADD COLUMN source_id INTEGER NOT NULL DEFAULT 0")
            if "source_name" not in cols:
                con.execute("ALTER TABLE articles ADD COLUMN source_name TEXT NOT NULL DEFAULT ''")
            con.execute("CREATE INDEX IF NOT EXISTS idx_articles_hash ON articles(content_hash)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_articles_url ON articles(url)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_articles_source_id ON articles(source_id)")
            con.execute("""
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    list_url TEXT NOT NULL,
                    selector TEXT NOT NULL DEFAULT '',
                    strict_selector INTEGER NOT NULL DEFAULT 1,
                    max_articles INTEGER NOT NULL DEFAULT 100,
                    max_list_pages INTEGER NOT NULL DEFAULT 100,
                    list_mode TEXT NOT NULL DEFAULT 'auto',
                    page_template TEXT NOT NULL DEFAULT '',
                    retry INTEGER NOT NULL DEFAULT 1,
                    min_chars INTEGER NOT NULL DEFAULT 300,
                    pause REAL NOT NULL DEFAULT 8.0,
                    skip_processed INTEGER NOT NULL DEFAULT 1,
                    output_dir TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT '',
                    last_run_at TEXT NOT NULL DEFAULT '',
                    last_status TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT ''
                )
            """)
            con.execute("CREATE INDEX IF NOT EXISTS idx_sources_enabled ON sources(enabled)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_sources_url ON sources(list_url)")
            con.execute("""
                CREATE TABLE IF NOT EXISTS article_sources (
                    canonical TEXT NOT NULL,
                    source_id INTEGER NOT NULL,
                    source_name TEXT NOT NULL DEFAULT '',
                    seen_url TEXT NOT NULL DEFAULT '',
                    first_seen_at TEXT NOT NULL DEFAULT '',
                    last_seen_at TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(canonical, source_id)
                )
            """)
            con.execute("CREATE INDEX IF NOT EXISTS idx_article_sources_source ON article_sources(source_id)")
            con.execute("""
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                )
            """)

    def save_form_config(self, data):
        cfg = settings_from_json(data or {})
        payload = {
            "source_id": int(getattr(cfg, "source_id", 0) or 0),
            "source_name": str(getattr(cfg, "source_name", "") or ""),
            "list_url": cfg.list_url, "selector": cfg.selector,
            "strict_selector": cfg.strict_selector, "max_articles": cfg.max_articles,
            "max_list_pages": cfg.max_list_pages, "list_mode": cfg.list_mode,
            "page_template": cfg.page_template, "retry": cfg.retry,
            "min_chars": cfg.min_chars, "pause": cfg.pause,
            "skip_processed": cfg.skip_processed, "output_dir": cfg.output_dir,
        }
        raw = json.dumps(payload, ensure_ascii=False)
        with self._db_connect() as con:
            con.execute("""
                INSERT INTO app_settings(key,value,updated_at) VALUES('last_form',?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """, (raw, now_iso()))
        return payload

    def load_form_config(self):
        try:
            with self._db_connect() as con:
                row = con.execute("SELECT value FROM app_settings WHERE key='last_form'").fetchone()
            if row and row[0]:
                data = json.loads(row[0])
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    @staticmethod
    def _source_row_to_dict(row):
        keys = [
            "id","name","list_url","selector","strict_selector","max_articles","max_list_pages",
            "list_mode","page_template","retry","min_chars","pause","skip_processed","output_dir",
            "enabled","created_at","updated_at","last_run_at","last_status","last_error","article_count"
        ]
        d = dict(zip(keys, row))
        for k in ("strict_selector","skip_processed","enabled"):
            d[k] = bool(d.get(k))
        return d

    def list_sources(self):
        with self._db_connect() as con:
            rows = con.execute("""
                SELECT s.id,s.name,s.list_url,s.selector,s.strict_selector,s.max_articles,s.max_list_pages,
                       s.list_mode,s.page_template,s.retry,s.min_chars,s.pause,s.skip_processed,s.output_dir,
                       s.enabled,s.created_at,s.updated_at,s.last_run_at,s.last_status,s.last_error,
                       (SELECT COUNT(DISTINCT z.canonical) FROM (
                            SELECT canonical, source_id FROM article_sources
                            UNION ALL
                            SELECT canonical, source_id FROM articles WHERE source_id<>0
                        ) z WHERE z.source_id=s.id) AS article_count
                FROM sources s ORDER BY s.enabled DESC, lower(s.name), s.id
            """).fetchall()
        return [self._source_row_to_dict(r) for r in rows]

    def _seed_default_sources_once(self):
        """Jednorazowo uzupełnia bazę o pakiet redakcji V15.2, bez nadpisywania użytkownika."""
        migration_key = "source_pack_v15_2"
        now = now_iso()
        added = 0
        with self._db_connect() as con:
            done = con.execute("SELECT value FROM app_settings WHERE key=?", (migration_key,)).fetchone()
            if done:
                return 0
            existing = {
                canonicalize_url(r[0])
                for r in con.execute("SELECT list_url FROM sources").fetchall()
                if canonicalize_url(r[0])
            }
            for preset in DEFAULT_SOURCE_PRESETS:
                url = canonicalize_url(preset.get("list_url", ""))
                if not url or url in existing:
                    continue
                con.execute("""
                    INSERT INTO sources(name,list_url,selector,strict_selector,max_articles,max_list_pages,list_mode,page_template,
                        retry,min_chars,pause,skip_processed,output_dir,enabled,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    str(preset.get("name") or host_of(url) or "Źródło")[:160],
                    url,
                    str(preset.get("selector") or ""),
                    int(bool(preset.get("strict_selector", False))),
                    max(1, int(preset.get("max_articles", 40) or 40)),
                    max(1, int(preset.get("max_list_pages", 1) or 1)),
                    str(preset.get("list_mode") or "single"),
                    str(preset.get("page_template") or ""),
                    1,
                    300,
                    8.0,
                    1,
                    "",
                    int(bool(preset.get("enabled", True))),
                    now,
                    now,
                ))
                existing.add(url)
                added += 1
            con.execute("""
                INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """, (migration_key, json.dumps({"added": added, "at": now}, ensure_ascii=False), now))
        return added

    def get_source(self, source_id):
        sid = int(source_id or 0)
        for src in self.list_sources():
            if src["id"] == sid:
                return src
        return None

    def save_source(self, data):
        cfg = settings_from_json(data or {})
        canon = canonicalize_url(cfg.list_url)
        if not canon:
            raise ValueError("Podaj poprawny URL listy źródła")
        name = normalize_text((data or {}).get("source_name") or (data or {}).get("name") or "")
        if not name:
            name = host_of(canon) or "Źródło"
        name = name[:160]
        sid = int((data or {}).get("source_id") or (data or {}).get("id") or 0)
        enabled = 1 if bool((data or {}).get("enabled", True)) else 0
        now = now_iso()
        values = (
            name, canon, cfg.selector, int(cfg.strict_selector), cfg.max_articles, cfg.max_list_pages,
            cfg.list_mode, cfg.page_template, cfg.retry, cfg.min_chars, cfg.pause, int(cfg.skip_processed),
            cfg.output_dir, enabled, now
        )
        with self._db_connect() as con:
            if sid:
                exists = con.execute("SELECT 1 FROM sources WHERE id=?", (sid,)).fetchone()
                if not exists:
                    raise ValueError("Nie ma takiego źródła")
                con.execute("""
                    UPDATE sources SET name=?,list_url=?,selector=?,strict_selector=?,max_articles=?,max_list_pages=?,
                        list_mode=?,page_template=?,retry=?,min_chars=?,pause=?,skip_processed=?,output_dir=?,enabled=?,updated_at=?
                    WHERE id=?
                """, values + (sid,))
            else:
                cur = con.execute("""
                    INSERT INTO sources(name,list_url,selector,strict_selector,max_articles,max_list_pages,list_mode,page_template,
                        retry,min_chars,pause,skip_processed,output_dir,enabled,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, values[:-1] + (now, now))
                sid = int(cur.lastrowid)
        src = self.get_source(sid)
        if src:
            self.save_form_config({**src, "source_id": sid, "source_name": src["name"]})
        return src

    def delete_source(self, source_id):
        sid = int(source_id or 0)
        with self.lock:
            if self.running and self.active_source_id == sid:
                raise RuntimeError("Nie można usunąć źródła, które właśnie pracuje")
        with self._db_connect() as con:
            cur = con.execute("DELETE FROM sources WHERE id=?", (sid,))
        return bool(cur.rowcount)

    def toggle_source(self, source_id, enabled=None):
        sid = int(source_id or 0)
        with self._db_connect() as con:
            row = con.execute("SELECT enabled FROM sources WHERE id=?", (sid,)).fetchone()
            if not row:
                raise ValueError("Nie ma takiego źródła")
            newval = (not bool(row[0])) if enabled is None else bool(enabled)
            con.execute("UPDATE sources SET enabled=?, updated_at=? WHERE id=?", (int(newval), now_iso(), sid))
        return self.get_source(sid)

    def _mark_source_state(self, source_id, status, error="", ran=False):
        sid = int(source_id or 0)
        if not sid:
            return
        with self._db_connect() as con:
            if ran:
                con.execute("UPDATE sources SET last_run_at=?,last_status=?,last_error=?,updated_at=? WHERE id=?",
                            (now_iso(), str(status or "")[:1200], str(error or "")[:1200], now_iso(), sid))
            else:
                con.execute("UPDATE sources SET last_status=?,last_error=?,updated_at=? WHERE id=?",
                            (str(status or "")[:1200], str(error or "")[:1200], now_iso(), sid))

    @staticmethod
    def _content_hash(text):
        # Hash po znormalizowanym tekście: wykrywa ten sam artykuł pod innym URL-em.
        norm = normalize_text(text or "").lower()
        norm = re.sub(r"\s+", " ", norm).strip()
        return hashlib.sha256(norm.encode("utf-8")).hexdigest() if norm else ""

    def _db_has_url(self, url):
        canon = canonicalize_url(url)
        if not canon:
            return False
        with self._db_connect() as con:
            row = con.execute("SELECT 1 FROM articles WHERE canonical=? OR url=? LIMIT 1", (canon, canon)).fetchone()
        return bool(row)

    def _db_has_hash(self, content_hash):
        if not content_hash:
            return False
        with self._db_connect() as con:
            row = con.execute("SELECT 1 FROM articles WHERE content_hash=? LIMIT 1", (content_hash,)).fetchone()
        return bool(row)

    def _db_add(self, canonical, url="", title="", content_hash="", saved_at="", output_folder="", source="saved", source_id=0, source_name=""):
        canon = canonicalize_url(canonical or url)
        if not canon:
            return
        urlc = canonicalize_url(url) or canon
        sid = int(source_id or 0)
        sname = str(source_name or "")[:160]
        with self._db_connect() as con:
            con.execute("""
                INSERT INTO articles(canonical,url,title,content_hash,saved_at,output_folder,source,source_id,source_name)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(canonical) DO UPDATE SET
                    url=CASE WHEN excluded.url<>'' THEN excluded.url ELSE articles.url END,
                    title=CASE WHEN excluded.title<>'' THEN excluded.title ELSE articles.title END,
                    content_hash=CASE WHEN excluded.content_hash<>'' THEN excluded.content_hash ELSE articles.content_hash END,
                    saved_at=CASE WHEN excluded.saved_at<>'' THEN excluded.saved_at ELSE articles.saved_at END,
                    output_folder=CASE WHEN excluded.output_folder<>'' THEN excluded.output_folder ELSE articles.output_folder END,
                    source=CASE WHEN excluded.source<>'' THEN excluded.source ELSE articles.source END,
                    source_id=CASE WHEN excluded.source_id<>0 THEN excluded.source_id ELSE articles.source_id END,
                    source_name=CASE WHEN excluded.source_name<>'' THEN excluded.source_name ELSE articles.source_name END
            """, (canon, urlc, title or "", content_hash or "", saved_at or "", output_folder or "", source or "saved", sid, sname))

    def _db_link_source(self, canonical, source_id=0, source_name="", seen_url=""):
        sid = int(source_id or 0)
        canon = canonicalize_url(canonical or seen_url)
        if not sid or not canon:
            return
        now = now_iso()
        with self._db_connect() as con:
            con.execute("""
                INSERT INTO article_sources(canonical,source_id,source_name,seen_url,first_seen_at,last_seen_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(canonical,source_id) DO UPDATE SET
                    source_name=CASE WHEN excluded.source_name<>'' THEN excluded.source_name ELSE article_sources.source_name END,
                    seen_url=CASE WHEN excluded.seen_url<>'' THEN excluded.seen_url ELSE article_sources.seen_url END,
                    last_seen_at=excluded.last_seen_at
            """, (canon, sid, str(source_name or "")[:160], canonicalize_url(seen_url) or canon, now, now))

    def _db_count(self):
        try:
            with self._db_connect() as con:
                return int(con.execute("SELECT COUNT(*) FROM articles").fetchone()[0])
        except Exception:
            return 0

    def _sync_archive_to_db(self, root):
        """Importuje starsze archiwum V6/V7 do bazy bez ponownego pobierania."""
        root = Path(root)
        imported = 0
        pp = root / "processed_urls.json"
        try:
            data = json.loads(pp.read_text(encoding="utf-8"))
            urls = data if isinstance(data, list) else list(data.keys()) if isinstance(data, dict) else []
            for u in urls:
                c = canonicalize_url(u)
                if c:
                    self._db_add(c, c, source="legacy_processed")
                    imported += 1
        except Exception:
            pass
        try:
            for meta in root.glob("*/metadata.json"):
                try:
                    d = json.loads(meta.read_text(encoding="utf-8"))
                    canon = canonicalize_url(d.get("canonical") or d.get("url") or "")
                    if not canon:
                        continue
                    txt_path = meta.parent / "artykul.txt"
                    ch = ""
                    if txt_path.exists():
                        ch = self._content_hash(txt_path.read_text(encoding="utf-8", errors="replace"))
                    self._db_add(canon, d.get("url") or canon, d.get("title") or d.get("page_title") or "", ch, d.get("saved_at") or "", str(meta.parent), source="archive_sync")
                    imported += 1
                except Exception:
                    continue
        except Exception:
            pass
        return imported

    def _default_output_root(self):
        base_downloads = preferred_downloads_dir()
        candidates = [
            base_downloads / "Przewijak_Artykuly",
            Path.cwd() / "pobrane_artykuly",
        ]
        for p in candidates:
            try:
                p.mkdir(parents=True, exist_ok=True)
                t = p / ".write_test"
                t.write_text("ok", encoding="utf-8")
                t.unlink(missing_ok=True)
                return p
            except Exception:
                continue
        return Path.cwd() / "pobrane_artykuly"

    def _cleanup_disposable_files(self, root):
        """Usuwa wyłącznie pozostałości techniczne po przerwanym działaniu.

        Nie dotyka artykułów, index.csv, processed_urls.json ani bazy historii.
        """
        root = Path(root)
        removed = 0
        errors = 0
        try:
            for p in root.glob(".tmp_*"):
                try:
                    if p.is_dir():
                        shutil.rmtree(p, ignore_errors=False)
                    elif p.is_file():
                        p.unlink(missing_ok=True)
                    removed += 1
                except Exception:
                    errors += 1
            for name in (".write_test", ".przewijak_write_test", "processed_urls.tmp"):
                p = root / name
                try:
                    if p.exists() and p.is_file():
                        p.unlink(missing_ok=True)
                        removed += 1
                except Exception:
                    errors += 1
        except Exception:
            errors += 1
        return {"removed": removed, "errors": errors}

    def _saf_commands_available(self):
        required = ("termux-saf-managedir", "termux-saf-ls", "termux-saf-stat", "termux-saf-mkdir", "termux-saf-create", "termux-saf-write", "termux-saf-rm")
        return all(shutil.which(x) for x in required)

    def _saf_api_ready(self):
        """Sprawdza także aplikację Termux:API, nie tylko obecność skryptów w shellu."""
        if not self._saf_commands_available() or not shutil.which("termux-saf-dirs"):
            return False, "Brak pakietu termux-api w Termuxie"
        try:
            self._run_saf(["termux-saf-dirs"], timeout=12)
            return True, ""
        except Exception as e:
            return False, str(e)

    def _run_saf(self, args, input_bytes=None, timeout=90):
        """Uruchamia oficjalne polecenia SAF Termux:API i zwraca stdout.

        SAF jest potrzebny do pełnego, systemowego wyboru katalogu na karcie SD.
        Zwykły dostęp plikowy Termuxa do external-1 obejmuje tylko prywatny katalog
        aplikacji na karcie i nie jest odpowiednikiem systemowego wyboru folderu.
        """
        if self.runtime_platform != "android-termux":
            raise RuntimeError("SAF jest dostępny tylko w Android/Termux")
        cmd = [str(x) for x in args]
        try:
            cp = subprocess.run(cmd, input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=timeout, check=False)
        except FileNotFoundError:
            raise RuntimeError("Brak poleceń Termux:API. Uruchom INSTALUJ_ANDROID.sh i zainstaluj aplikację Termux:API z tego samego źródła co Termux.")
        except subprocess.TimeoutExpired:
            raise RuntimeError("Systemowy wybór folderu nie odpowiedział na czas")
        if cp.returncode != 0:
            err = cp.stderr.decode("utf-8", errors="replace").strip() or cp.stdout.decode("utf-8", errors="replace").strip()
            raise RuntimeError("Termux:API/SAF: " + (err or f"kod {cp.returncode}"))
        return cp.stdout.decode("utf-8", errors="replace").strip()

    def _saf_list(self, uri):
        raw = self._run_saf(["termux-saf-ls", uri], timeout=45)
        try:
            data = json.loads(raw or "[]")
        except Exception as e:
            raise RuntimeError(f"Nieprawidłowa odpowiedź SAF listy: {e}")
        return data if isinstance(data, list) else []

    def _saf_stat(self, uri):
        raw = self._run_saf(["termux-saf-stat", uri], timeout=45)
        try:
            data = json.loads(raw or "{}")
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _saf_is_dir(entry):
        return str((entry or {}).get("type", "")) == "vnd.android.document/directory"

    def _saf_find_child(self, parent_uri, name, want_dir=None):
        for e in self._saf_list(parent_uri):
            if str(e.get("name", "")) != str(name):
                continue
            if want_dir is None or self._saf_is_dir(e) == bool(want_dir):
                return str(e.get("uri", "") or "")
        return ""

    def _saf_ensure_dir(self, parent_uri, name):
        found = self._saf_find_child(parent_uri, name, True)
        if found:
            return found
        out = self._run_saf(["termux-saf-mkdir", parent_uri, name], timeout=45).strip()
        if not out:
            found = self._saf_find_child(parent_uri, name, True)
            if found:
                return found
            raise RuntimeError(f"SAF nie utworzył folderu: {name}")
        return out.splitlines()[-1].strip()

    @staticmethod
    def _mime_for_path(path):
        ext = Path(path).suffix.lower()
        return {
            ".html": "text/html", ".htm": "text/html", ".txt": "text/plain",
            ".json": "application/json", ".csv": "text/csv",
        }.get(ext, "application/octet-stream")

    def _saf_write_local_file(self, parent_uri, local_path):
        local_path = Path(local_path)
        name = local_path.name
        file_uri = self._saf_find_child(parent_uri, name, False)
        if not file_uri:
            out = self._run_saf(["termux-saf-create", "-t", self._mime_for_path(local_path), parent_uri, name], timeout=45).strip()
            file_uri = out.splitlines()[-1].strip() if out else ""
        if not file_uri:
            raise RuntimeError(f"SAF nie utworzył pliku: {name}")
        self._run_saf(["termux-saf-write", file_uri], input_bytes=local_path.read_bytes(), timeout=90)
        return file_uri

    def _saf_probe_write(self, root_uri):
        """Sprawdza REALNY zapis, a nie tylko możliwość wyświetlenia katalogu."""
        probe_name = f"Przewijak_write_test_{int(time.time())}.txt"
        out = self._run_saf(["termux-saf-create", "-t", "text/plain", root_uri, probe_name], timeout=45).strip()
        uri = out.splitlines()[-1].strip() if out else ""
        if not uri:
            raise RuntimeError("SAF: nie udało się utworzyć pliku testowego")
        try:
            self._run_saf(["termux-saf-write", uri], input_bytes=b"ok", timeout=45)
        finally:
            try:
                self._run_saf(["termux-saf-rm", uri], timeout=45)
            except Exception:
                pass

    def _saf_cache_dir(self, uri):
        key = hashlib.sha1(uri.encode("utf-8")).hexdigest()[:16]
        p = self.db_path.parent / "saf_cache" / key
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _save_saf_state(self):
        if self.storage_mode != "saf" or not self.saf_root_uri:
            try:
                self.saf_state_path.unlink(missing_ok=True)
            except Exception:
                pass
            return
        data = {"uri": self.saf_root_uri, "label": self.saf_label, "version": 1}
        tmp = self.saf_state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.saf_state_path)

    def _restore_saf_target(self):
        try:
            data = json.loads(self.saf_state_path.read_text(encoding="utf-8"))
            uri = str(data.get("uri", "") or "").strip()
            if not uri or not self._saf_commands_available():
                return False
            # Tylko krótki odczyt uprawnień; nie otwieramy żadnego okna przy starcie.
            self._saf_list(uri)
            self.storage_mode = "saf"
            self.saf_root_uri = uri
            self.saf_label = str(data.get("label", "") or "Wybrany folder Android")
            self._bind_output_paths(self._saf_cache_dir(uri))
            return True
        except Exception:
            return False

    def set_saf_output_root(self, uri):
        uri = str(uri or "").strip()
        if not uri.startswith("content://"):
            raise ValueError("Android nie zwrócił poprawnego folderu SAF")
        # Najpierw sprawdź odczyt i zapis. Jeśli to się nie powiedzie, nie zmieniaj aktywnego folderu.
        self._saf_list(uri)
        self._saf_probe_write(uri)
        st = self._saf_stat(uri)
        label = str(st.get("name", "") or "Wybrany folder")
        cache = self._saf_cache_dir(uri)
        with self.lock:
            self.storage_mode = "saf"
            self.saf_root_uri = uri
            self.saf_label = label
            self._bind_output_paths(cache)
        self._save_saf_state()
        self.cleanup_stats = self._cleanup_disposable_files(cache)
        self._sync_archive_to_db(cache)
        with self.lock:
            self.status = f"Folder zapisu ustawiony przez Android: {label} · SAF · baza: {self._db_count()} artykułów"
        return self.output_display()

    def output_display(self):
        if self.storage_mode in {"saf", "android-saf"} and (self.saf_root_uri or self.saf_label):
            return f"Android/SAF: {self.saf_label or 'wybrany folder'}"
        return str(self.output_root)

    def _saf_sync_root_file(self, local_path):
        if self.storage_mode == "saf" and self.saf_root_uri and Path(local_path).exists():
            self._saf_write_local_file(self.saf_root_uri, local_path)

    def _saf_sync_article_folder(self, local_folder):
        if self.storage_mode != "saf" or not self.saf_root_uri:
            return
        local_folder = Path(local_folder)
        remote_dir = self._saf_ensure_dir(self.saf_root_uri, local_folder.name)
        for f in sorted(local_folder.iterdir(), key=lambda x: x.name.casefold()):
            if f.is_file():
                self._saf_write_local_file(remote_dir, f)

    def _saf_sync_all_root_metadata(self):
        if self.storage_mode != "saf":
            return
        for p in (self.processed_path, self.index_csv, self.index_html, self.failed_csv):
            if Path(p).exists():
                self._saf_sync_root_file(p)

    def _android_path_no_resolve(self, path):
        """Absolutna ścieżka bez rozwijania symlinków Termuxa (ważne dla external-1/media-1)."""
        p = Path(path).expanduser()
        return Path(os.path.abspath(os.path.normpath(str(p))))

    def _bind_output_paths(self, root):
        # Na Androidzie zachowujemy ścieżkę przez ~/storage/external-1 lub media-1.
        # Path.resolve() zamieniał ją na /storage/XXXX-XXXX/... i na części urządzeń
        # powodował problemy z dalszą nawigacją / zapisem przez scoped storage.
        if self.runtime_platform == "android-termux":
            root = self._android_path_no_resolve(root)
        else:
            root = Path(root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        self.output_root = root
        self.processed_path = root / "processed_urls.json"
        self.index_csv = root / "index.csv"
        self.index_html = root / "index.html"
        self.failed_csv = root / "failed.csv"

    @staticmethod
    def _looks_like_css_selector(raw):
        x = str(raw or "").strip().lower()
        return bool(x) and (
            "href" in x or x.startswith(("a[", "article[", "div[", "main["))
            or ("[" in x and "]" in x and "/" not in x and "\\" not in x)
        )

    def resolve_output_dir(self, value):
        raw = str(value or "").strip()
        if not raw:
            return self.output_root
        if self._looks_like_css_selector(raw):
            raise ValueError(
                "W polu FOLDER ZAPISU wpisano selektor CSS. "
                "Selektor (np. a[href*='-st']) wpisz w polu SELEKTOR CSS."
            )
        low = raw.lower().replace("\\", "/")
        downloads = preferred_downloads_dir()
        documents = (Path.home() / "Documents") if self.runtime_platform != "android-termux" else (Path.home() / "storage" / "shared" / "Documents")
        if low in {"pobrane", "download", "downloads"}:
            p = downloads / "Przewijak_Artykuly"
        elif low in {"dokumenty", "documents"}:
            p = documents / "Przewijak_Artykuly"
        elif low.startswith("downloads/") or low.startswith("pobrane/"):
            p = downloads / raw.replace("\\", "/").split("/", 1)[1]
        elif low.startswith("documents/") or low.startswith("dokumenty/"):
            p = documents / raw.replace("\\", "/").split("/", 1)[1]
        elif raw.startswith("~/"):
            p = Path(raw).expanduser()
        elif Path(raw).is_absolute():
            p = Path(raw)
        else:
            p = downloads / raw
        if self.runtime_platform == "android-termux":
            return self._android_path_no_resolve(p)
        return p.expanduser().resolve()

    def pick_output_dir(self):
        """Windows: natywne tkinter. Android: systemowy Storage Access Framework (SAF).

        To jest właściwa droga do wyboru DOWOLNEGO folderu na karcie SD na nowych
        Androidach. Nie mylimy już private external-1 z pełną kartą SD.
        """
        with self.lock:
            if self.running:
                raise RuntimeError("Nie można zmienić folderu podczas pobierania")
        if self.runtime_platform == "android-native":
            return {"picker": "android-native", "output_root": self.output_display(), "storage_mode": "android-saf"}
        if self.runtime_platform == "android-termux":
            # 1) Najpierw zachowujemy starszy, prosty mechanizm /storage/UUID, ale tylko
            #    gdy ten pełny nośnik jest DZISIAJ faktycznie czytelny przez Termuxa.
            direct_sd = self._discover_direct_sd_roots()
            if direct_sd:
                return {"picker": "internal", "initial_path": str(direct_sd[0]),
                        "output_root": self.output_display(), "storage_mode": "filesystem"}
            # 2) Gdy Android blokuje bezpośredni root SD, używamy systemowego SAF.
            if not self._saf_commands_available():
                return {"picker": "internal", "output_root": self.output_display(),
                        "warning": "Android nie daje bezpośredniego wejścia do pełnej karty SD i brakuje pakietu termux-api. Awaryjny picker pokaże tylko katalogi dostępne zwykłą ścieżką."}
            ready, why = self._saf_api_ready()
            if not ready:
                raise RuntimeError(
                    "Polecenia termux-api są zainstalowane, ale aplikacja Termux:API nie odpowiada. "
                    "Zainstaluj/uruchom Termux:API z tego samego źródła co Termux. Szczegół: " + why
                )
            # To polecenie otwiera SYSTEMOWY wybór katalogu Androida i zwraca trwały URI.
            out = self._run_saf(["termux-saf-managedir"], timeout=300).strip()
            uri = out.splitlines()[-1].strip() if out else ""
            if not uri:
                return {"picker": "cancelled", "output_root": self.output_display()}
            display = self.set_saf_output_root(uri)
            return {"picker": "saf", "output_root": display, "storage_mode": "saf"}
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            try:
                root.attributes("-topmost", True)
            except Exception:
                pass
            initial = str(self.output_root if self.output_root.exists() else Path.home())
            chosen = filedialog.askdirectory(title="Wybierz folder zapisu artykułów", initialdir=initial, mustexist=True, parent=root)
            root.destroy()
        except Exception as e:
            raise RuntimeError(f"Nie udało się otworzyć wyboru folderu Windows: {e}")
        if not chosen:
            return {"picker": "cancelled", "output_root": self.output_display()}
        return {"picker": "filesystem", "output_root": self.set_output_root(chosen), "storage_mode": "filesystem"}

    def _folder_allowed_android(self, path):
        """Ogranicza mobilny picker do pamięci użytkownika i katalogów Termuxa, bez dereferencji symlinków."""
        try:
            rp = self._android_path_no_resolve(path)
        except Exception:
            return False
        allowed = [self._android_path_no_resolve(Path.home()), Path("/storage")]
        for root in allowed:
            if rp == root or root in rp.parents:
                return True
        return False

    @staticmethod
    def _can_browse_dir(path):
        try:
            p = Path(path).expanduser()
            if not p.is_dir():
                return False
            # Faktyczna próba wejścia jest pewniejsza niż samo exists()/is_dir().
            with os.scandir(str(p)) as it:
                next(it, None)
            return True
        except Exception:
            return False

    def _discover_direct_sd_roots(self):
        """Znajduje pełne korzenie wymiennych nośników, ale tylko gdy da się je realnie otworzyć.

        Najpierw skanuje /storage, a dodatkowo wyciąga UUID z celu symlinków
        ~/storage/external-*; to odtwarza starszy działający wariant /storage/UUID,
        gdy Android nadal przyznaje zwykły dostęp plikowy.
        """
        found = []
        seen = set()

        def add(p):
            try:
                p = Path(p)
                name = p.name
                if not re.fullmatch(r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}", name):
                    return
                key = str(p)
                if key in seen or not self._can_browse_dir(p):
                    return
                seen.add(key)
                found.append(p)
            except Exception:
                pass

        try:
            storage = Path("/storage")
            if storage.is_dir():
                for child in storage.iterdir():
                    add(child)
        except Exception:
            pass

        ts = Path.home() / "storage"
        try:
            for link in ts.glob("external-*"):
                if not link.is_symlink():
                    continue
                target = Path(os.path.realpath(str(link)))
                parts = target.parts
                # typowy cel: /storage/XXXX-XXXX/Android/data/com.termux/files
                if len(parts) >= 3 and parts[1] == "storage":
                    add(Path("/") / parts[1] / parts[2])
        except Exception:
            pass
        return sorted(found, key=lambda x: x.name.casefold())

    def _android_picker_aliases(self):
        ts = Path.home() / "storage"
        return {
            "@downloads": preferred_downloads_dir(),
            "@phone": Path("/storage/emulated/0"),
            "@termux": Path.home(),
            "@shared": ts / "shared",
            "@sd": ts / "external-1",
            "@sdmedia": ts / "media-1",
        }

    def list_output_dirs(self, value=""):
        """Zwraca katalogi dla mobilnego wyboru folderu bez tkinter.

        Ważne: na Androidzie NIE rozwijamy symlinków external-1/media-1.
        Dzięki temu kliknięcie „Karta SD” porusza się po ścieżce Termuxa, która
        faktycznie ma przyznany dostęp, zamiast przełączać się na surowe /storage/UUID.
        """
        if self.runtime_platform != "android-termux":
            return {"platform": self.runtime_platform, "path": str(self.output_root), "parent": None, "dirs": [], "roots": [], "storage_info": ""}

        aliases = self._android_picker_aliases()
        raw = str(value or "").strip()
        if raw in aliases:
            raw = str(aliases[raw])

        candidates = []
        if raw:
            try:
                candidates.append(Path(raw).expanduser())
            except Exception:
                pass
        candidates.extend([
            self.output_root,
            aliases["@downloads"],
            Path("/storage/emulated/0/Download"),
            Path("/storage/emulated/0"),
            aliases["@shared"],
            aliases["@termux"],
        ])

        current = None
        last_error = ""
        for c in candidates:
            try:
                cc = self._android_path_no_resolve(c)
                if cc.is_dir() and self._folder_allowed_android(cc):
                    # Przy wejściu do wskazanego katalogu sprawdzamy realny odczyt.
                    try:
                        with os.scandir(str(cc)) as it:
                            next(it, None)
                        current = cc
                        break
                    except Exception as e:
                        last_error = f"{type(e).__name__}: {e}"
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                continue
        if current is None:
            current = self._android_path_no_resolve(Path.home())

        dirs = []
        try:
            with os.scandir(str(current)) as it:
                for entry in it:
                    try:
                        if entry.name.startswith("."):
                            continue
                        if entry.is_dir(follow_symlinks=True):
                            child = self._android_path_no_resolve(current / entry.name)
                            dirs.append({"name": entry.name, "path": str(child)})
                    except Exception:
                        continue
        except Exception as e:
            raise RuntimeError(f"Nie można wejść do folderu: {current}: {type(e).__name__}: {e}")
        dirs.sort(key=lambda x: x["name"].casefold())

        parent = None
        try:
            par = self._android_path_no_resolve(current.parent)
            if par != current and self._folder_allowed_android(par):
                parent = str(par)
        except Exception:
            pass

        roots = []
        seen = set()

        def add_root(label, path_or_token, real_path=None):
            real = aliases.get(path_or_token, real_path if real_path is not None else path_or_token)
            try:
                rr = Path(real).expanduser()
                # Dla symlinków SD sprawdzamy czy naprawdę da się katalog otworzyć.
                if not self._can_browse_dir(rr):
                    return
                key = str(self._android_path_no_resolve(rr))
                if key in seen or not self._folder_allowed_android(rr):
                    return
                roots.append({"name": label, "path": str(path_or_token)})
                seen.add(key)
            except Exception:
                return

        add_root("📥 Pobrane telefonu", "@downloads")
        add_root("📱 Pamięć telefonu", "@phone")

        # Najpierw stabilne aliasy Termuxa. Nie dereferencjujemy ich do /storage/UUID.
        sd_ok = self._can_browse_dir(aliases["@sd"])
        sdm_ok = self._can_browse_dir(aliases["@sdmedia"])
        if sd_ok:
            add_root("💾 Karta SD — katalog Termux", "@sd")
        if sdm_ok:
            add_root("💾 Karta SD — katalog media Termux", "@sdmedia")

        # Pełny root woluminu pokazujemy tylko gdy Android naprawdę pozwala go odczytać.
        direct_sd = []
        for child in self._discover_direct_sd_roots():
            add_root(f"💾 Karta SD — cały nośnik ({child.name})", str(child))
            direct_sd.append(child.name)

        add_root("📂 Pamięć współdzielona", "@shared")
        add_root("🏠 Termux", "@termux")

        info_bits = []
        if sd_ok:
            info_bits.append("external-1: OK")
        elif (Path.home() / "storage" / "external-1").is_symlink():
            info_bits.append("external-1: wykryty, ale brak wejścia")
        else:
            info_bits.append("external-1: brak")
        if sdm_ok:
            info_bits.append("media-1: OK")
        if direct_sd:
            info_bits.append("root SD: " + ", ".join(direct_sd))
        if last_error:
            info_bits.append("ostatni błąd: " + last_error)

        return {
            "platform": self.runtime_platform,
            "path": str(current),
            "parent": parent,
            "dirs": dirs,
            "roots": roots,
            "storage_info": " · ".join(info_bits),
        }

    def set_default_output_root(self):
        if self.running:
            raise RuntimeError("Nie można zmienić folderu podczas pobierania")
        return self.set_output_root(str(preferred_downloads_dir() / "Przewijak_Artykuly"))

    def set_output_root(self, value):
        with self.lock:
            if self.running:
                raise RuntimeError("Nie można zmienić folderu podczas pobierania")
        p = self.resolve_output_dir(value)
        p.mkdir(parents=True, exist_ok=True)
        test = p / ".przewijak_write_test"
        try:
            test.write_text("ok", encoding="utf-8")
            test.unlink(missing_ok=True)
        except Exception as e:
            raise ValueError(f"Brak zapisu do folderu: {p}: {e}")
        with self.lock:
            self.storage_mode = "filesystem"
            self.saf_root_uri = ""
            self.saf_label = ""
            self._bind_output_paths(p)
        self._save_saf_state()
        self.cleanup_stats = self._cleanup_disposable_files(p)
        imported = self._sync_archive_to_db(p)
        with self.lock:
            self.status = f"Folder zapisu ustawiony: {p} · baza: {self._db_count()} artykułów"
        return str(p)

    def _set_status(self, s):
        with self.lock:
            self.status = s

    def snapshot(self):
        with self.lock:
            return {
                "version": APP_VERSION,
                "running": self.running,
                "status": self.status,
                "progress": dict(self.progress),
                "output_root": self.output_display(),
                "output_fs_root": str(self.output_root),
                "storage_mode": self.storage_mode,
                "saf_label": self.saf_label,
                "database_path": str(self.db_path),
                "database_count": self._db_count(),
                "sources_count": len(self.list_sources()),
                "sources_enabled": sum(1 for x in self.list_sources() if x.get("enabled")),
                "active_source_id": self.active_source_id,
                "active_source_name": self.active_source_name,
                "batch_running": self.batch_running,
                "batch_index": self.batch_index,
                "batch_total": self.batch_total,
                "batch_done_sources": self.batch_done_sources,
                "last_test_links": list(self.last_test_links[-100:]),
                "recent_failures": list(self.recent_failures[-8:]),
                "network": dict(self.network_stats),
                "request_interval": self.request_interval,
                "allowed_host": self.allowed_host,
                "fatal_stop_reason": self.fatal_stop_reason,
                "platform": self.runtime_platform,
                "cleanup": dict(self.cleanup_stats),
            }

    def load_processed(self):
        try:
            data = json.loads(self.processed_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return {canonicalize_url(x) for x in data if canonicalize_url(x)}
            if isinstance(data, dict):
                return {canonicalize_url(x) for x in data.keys() if canonicalize_url(x)}
        except Exception:
            pass
        return set()

    def save_processed(self, items):
        tmp = self.processed_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(items), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.processed_path)
        self._saf_sync_root_file(self.processed_path)

    @staticmethod
    def _request_headers(profile="low-impact", referer=""):
        # Stały, nierotowany identyfikator. Bez udawania wielu urządzeń/użytkowników.
        h = {
            "User-Agent": f"PrzewijakArtykuly/{APP_VERSION} ({runtime_platform()}; read-only; low-impact)",
            "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.5",
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        }
        if referer:
            h["Referer"] = referer
        return h

    @staticmethod
    def _decode_response_html(r):
        raw = bytes(r.content or b"")
        if not raw:
            return ""
        if raw.startswith(b"\xef\xbb\xbf"):
            return raw.decode("utf-8-sig", errors="replace")
        # UTF-8 ma pierwszeństwo. apparent_encoding potrafiło zamieniać polskie litery na CJK.
        try:
            return raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            pass
        declared = []
        ctype = r.headers.get("content-type") or ""
        m = re.search(r"charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", ctype, re.I)
        if m:
            declared.append(m.group(1))
        head = raw[:20000].decode("latin-1", errors="ignore")
        for rx in (r"<meta[^>]+charset\s*=\s*['\"]?([^'\"\s/>]+)", r"charset\s*=\s*([^'\"\s;/>]+)"):
            mm = re.search(rx, head, re.I)
            if mm:
                declared.append(mm.group(1))
        declared += [getattr(r, "apparent_encoding", None), "windows-1250", "iso-8859-2", "latin-1"]
        seen = set()
        for enc in declared:
            if not enc:
                continue
            enc = str(enc).strip().lower()
            if enc in seen:
                continue
            seen.add(enc)
            try:
                return raw.decode(enc, errors="strict")
            except Exception:
                continue
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _host_key(url):
        try:
            h = (urlsplit(url).hostname or "").lower().strip(".")
        except Exception:
            return ""
        if h.startswith("www."):
            h = h[4:]
        return h

    def _set_allowed_source(self, url):
        h = self._host_key(url)
        if not h:
            raise ValueError("Nieprawidłowa domena źródłowa")
        self.allowed_host = h

    def _is_allowed_url(self, url):
        return bool(self.allowed_host and self._host_key(url) == self.allowed_host)

    def _configure_low_impact(self, cfg):
        # Minimum 5 s jest wymuszane po stronie programu.
        self.request_interval = max(5.0, min(120.0, float(getattr(cfg, "pause", 8.0) or 8.0)))
        self._set_allowed_source(cfg.list_url)

    def _cache_get(self, url):
        key = canonicalize_url(url)
        if not key:
            return None
        now = time.monotonic()
        with self.cache_lock:
            item = self.response_cache.get(key)
            if not item:
                return None
            ts, result = item
            if now - ts > self.cache_ttl:
                self.response_cache.pop(key, None)
                return None
            self.network_stats["cache_hits"] += 1
            return result

    def _cache_put(self, url, result):
        key = canonicalize_url(url)
        if not key:
            return
        with self.cache_lock:
            self.response_cache[key] = (time.monotonic(), result)
            # Mały cache procesowy, bez rozrostu pamięci.
            if len(self.response_cache) > 300:
                oldest = sorted(self.response_cache.items(), key=lambda kv: kv[1][0])[:50]
                for k, _ in oldest:
                    self.response_cache.pop(k, None)

    def _json_cache_get(self, url):
        key = canonicalize_url(url)
        if not key:
            return None
        now = time.monotonic()
        with self.cache_lock:
            item = self.json_cache.get(key)
            if not item:
                return None
            ts, value = item
            if now - ts > self.cache_ttl:
                self.json_cache.pop(key, None)
                return None
            self.network_stats["cache_hits"] += 1
            return value

    def _json_cache_put(self, url, value):
        key = canonicalize_url(url)
        if not key:
            return
        with self.cache_lock:
            self.json_cache[key] = (time.monotonic(), value)
            if len(self.json_cache) > 300:
                oldest = sorted(self.json_cache.items(), key=lambda kv: kv[1][0])[:50]
                for k, _ in oldest:
                    self.json_cache.pop(k, None)

    def _wait_request_slot(self):
        # Jeden zewnętrzny GET naraz + stały minimalny odstęp między GET-ami.
        now = time.monotonic()
        wait = self.request_interval - (now - self.last_external_request)
        if wait > 0:
            self.stop_event.wait(wait)
        if self.stop_event.is_set():
            raise OperationCancelled("zatrzymano")

    def _single_get(self, url, timeout, referer="", accept_json=False):
        if not self._is_allowed_url(url):
            raise RuntimeError(f"READ-ONLY: zablokowano wyjście poza domenę źródłową: {url}")
        with self.net_lock:
            self._wait_request_slot()
            try:
                headers = self._request_headers("low-impact", referer)
                if accept_json:
                    headers["Accept"] = "application/json,text/plain;q=0.9,*/*;q=0.1"
                r = self.session.get(
                    url, timeout=timeout, allow_redirects=False, headers=headers,
                )
            finally:
                self.last_external_request = time.monotonic()
                with self.lock:
                    self.network_stats["requests"] += 1
        return r

    def fetch(self, url, timeout=30, referer=""):
        current = canonicalize_url(url)
        if not current:
            raise RuntimeError("nieprawidłowy URL")
        if not self._is_allowed_url(current):
            raise RuntimeError(f"READ-ONLY: URL spoza domeny źródłowej: {current}")
        cached = self._cache_get(current)
        if cached is not None:
            return cached

        redirects = 0
        while True:
            try:
                r = self._single_get(current, timeout, referer=referer)
            except requests.RequestException as e:
                raise RuntimeError(f"GET: {type(e).__name__}: {e}")

            status = int(r.status_code)
            if status in (403, 429):
                retry_after = (r.headers.get("Retry-After") or "").strip()
                extra = f" · Retry-After={retry_after}" if retry_after else ""
                self.fatal_stop_reason = f"LOW-IMPACT STOP: HTTP {status}{extra} · {current}"
                self.stop_event.set()
                raise RuntimeError(self.fatal_stop_reason)

            if status in (301, 302, 303, 307, 308):
                loc = (r.headers.get("Location") or "").strip()
                if not loc:
                    raise RuntimeError(f"HTTP {status} bez Location")
                nxt = canonicalize_url(urljoin(current, loc))
                if not nxt or not self._is_allowed_url(nxt):
                    raise RuntimeError(f"READ-ONLY: zablokowano przekierowanie poza domenę: {loc}")
                redirects += 1
                with self.lock:
                    self.network_stats["redirects"] += 1
                if redirects > 5:
                    raise RuntimeError("za dużo przekierowań")
                referer = current
                current = nxt
                cached = self._cache_get(current)
                if cached is not None:
                    return cached
                continue

            ctype = (r.headers.get("content-type") or "").lower()
            size = len(r.content or b"")
            if status < 200 or status >= 300:
                raise RuntimeError(f"HTTP {status}")
            if ctype and "html" not in ctype and "xhtml" not in ctype:
                raise RuntimeError(f"nie-HTML ({ctype})")
            text = self._decode_response_html(r)
            if len(text) < 120:
                raise RuntimeError(f"odpowiedź HTML ma tylko {len(text)} znaków")
            result = FetchResult(current, text, status, ctype, size, "low-impact")
            self._cache_put(url, result)
            self._cache_put(current, result)
            return result

    def fetch_json(self, url, timeout=30, referer=""):
        """Lekki GET JSON z tym samym limiterem, polityką domeny i STOP 403/429."""
        current = canonicalize_url(url)
        if not current:
            raise RuntimeError("nieprawidłowy URL JSON")
        if not self._is_allowed_url(current):
            raise RuntimeError(f"READ-ONLY: URL JSON spoza domeny źródłowej: {current}")
        cached = self._json_cache_get(current)
        if cached is not None:
            return cached
        redirects = 0
        while True:
            try:
                r = self._single_get(current, timeout, referer=referer, accept_json=True)
            except requests.RequestException as e:
                raise RuntimeError(f"GET JSON: {type(e).__name__}: {e}")
            status = int(r.status_code)
            if status in (403, 429):
                retry_after = (r.headers.get("Retry-After") or "").strip()
                extra = f" · Retry-After={retry_after}" if retry_after else ""
                self.fatal_stop_reason = f"LOW-IMPACT STOP: HTTP {status}{extra} · {current}"
                self.stop_event.set()
                raise RuntimeError(self.fatal_stop_reason)
            if status in (301, 302, 303, 307, 308):
                loc = (r.headers.get("Location") or "").strip()
                if not loc:
                    raise RuntimeError(f"HTTP {status} bez Location")
                nxt = canonicalize_url(urljoin(current, loc))
                if not nxt or not self._is_allowed_url(nxt):
                    raise RuntimeError(f"READ-ONLY: zablokowano przekierowanie JSON poza domenę: {loc}")
                redirects += 1
                with self.lock:
                    self.network_stats["redirects"] += 1
                if redirects > 5:
                    raise RuntimeError("za dużo przekierowań JSON")
                referer = current
                current = nxt
                continue
            if status < 200 or status >= 300:
                raise RuntimeError(f"HTTP {status}")
            raw = self._decode_response_html(r)
            try:
                value = (json.loads(raw), current, len(r.content or b""))
                self._json_cache_put(url, value)
                self._json_cache_put(current, value)
                return value
            except Exception as e:
                raise RuntimeError(f"odpowiedź API nie jest JSON: {type(e).__name__}: {e}")

    @staticmethod
    def _tvp_tag_value(url):
        try:
            q = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
            return normalize_text(q.get("tag", ""))
        except Exception:
            return ""

    def _tvp_api_extract_links(self, data):
        """Tolerancyjny parser odpowiedzi TVP list-by-tags.

        Nie zakłada jednego sztywnego schematu JSON: wyłapuje adresy artykułów
        i numeryczne ID w zagnieżdżonych obiektach, dzięki czemu drobne zmiany
        nazw pól API nie psują całego kolektora.
        """
        found = {}

        def add(url, text="", score=30):
            if not isinstance(url, str):
                return
            raw = url.strip()
            if not raw:
                return
            abs_url = canonicalize_url(urljoin("https://www.tvp.info/", raw))
            if not abs_url:
                return
            path = urlsplit(abs_url).path or ""
            if not re.match(r"^/\d{5,}(?:/[^/]+.*)?$", path):
                return
            if self._host_key(abs_url) != "tvp.info":
                return
            item = {"url": abs_url, "text": normalize_text(text), "score": score}
            old = found.get(abs_url)
            if old is None or len(item["text"]) > len(old.get("text", "")):
                found[abs_url] = item

        def walk(value, parent_title=""):
            if isinstance(value, dict):
                title = parent_title
                for k in ("title", "headline", "name", "lead", "description"):
                    v = value.get(k)
                    if isinstance(v, str) and len(normalize_text(v)) >= 3:
                        title = normalize_text(v)
                        break
                for k, v in value.items():
                    lk = str(k).lower()
                    if isinstance(v, str) and lk in {
                        "url", "href", "link", "canonical", "canonicalurl", "canonical_url",
                        "weburl", "web_url", "uri", "path"
                    }:
                        add(v, title, 40)
                # Fallback po ID, jeśli API nie zwraca gotowego URL-a.
                for k in ("id", "newsId", "news_id", "articleId", "article_id", "objectId", "object_id"):
                    v = value.get(k)
                    if isinstance(v, int) or (isinstance(v, str) and v.isdigit()):
                        digits = str(v)
                        if len(digits) >= 5:
                            add(f"/{digits}", title, 25)
                            break
                for v in value.values():
                    walk(v, title)
            elif isinstance(value, list):
                for v in value:
                    walk(v, parent_title)
            elif isinstance(value, str):
                # Czasem URL może być elementem prostej listy.
                if re.search(r"(?:https?://(?:www\.)?tvp\.info)?/\d{5,}(?:/|$)", value):
                    add(value, parent_title, 30)

        walk(data)
        # Jeśli dla tego samego ID mamy pełny URL ze slugiem i awaryjny /ID,
        # zachowaj pełniejszy wariant, żeby nie próbować tego samego artykułu dwa razy.
        by_id = {}
        for item in found.values():
            path = urlsplit(item["url"]).path or ""
            m = re.match(r"^/(\d{5,})(?:/|$)", path)
            key = m.group(1) if m else item["url"]
            old = by_id.get(key)
            if old is None:
                by_id[key] = item
                continue
            old_path = urlsplit(old["url"]).path or ""
            new_has_slug = path.count("/") >= 2 and path.rstrip("/").count("/") >= 2
            old_has_slug = old_path.count("/") >= 2 and old_path.rstrip("/").count("/") >= 2
            if (new_has_slug and not old_has_slug) or (new_has_slug == old_has_slug and item.get("score", 0) > old.get("score", 0)):
                by_id[key] = item
        return sorted(by_id.values(), key=lambda x: (-x.get("score", 0), x["url"]))

    def _tvp_fetch_tag_api(self, cfg, test_mode=False):
        tag = self._tvp_tag_value(cfg.list_url)
        if not tag:
            raise RuntimeError("TVP Info: brak wartości parametru tag")
        max_pages = max(1, min(int(getattr(cfg, "max_list_pages", 100) or 100), 1000))
        if (getattr(cfg, "list_mode", "auto") or "auto").lower() == "single":
            max_pages = 1
        if test_mode:
            max_pages = 1
        limit = 20
        all_links = {}
        total_bytes = 0
        last_api_url = cfg.list_url
        stale_pages = 0
        for page_no in range(1, max_pages + 1):
            if self.stop_event.is_set():
                raise OperationCancelled("zatrzymano")
            q = urlencode({"device": "www", "tags[]": tag, "page": page_no, "limit": limit})
            api_url = f"https://www.tvp.info/api/info/list-by-tags?{q}"
            self._set_status(f"TVP INFO TAG · API strona {page_no}/{max_pages} · {len(all_links)} linków")
            data, final_api, size = self.fetch_json(api_url, referer=cfg.list_url)
            total_bytes += size
            last_api_url = final_api
            page_links = self._tvp_api_extract_links(data)
            before = len(all_links)
            for item in page_links:
                all_links[item["url"]] = item
            after = len(all_links)
            with self.lock:
                self.progress["found"] = after
                self.progress["list_page"] = page_no
            if not page_links or after == before:
                stale_pages += 1
            else:
                stale_pages = 0
            # Dwie puste/powtarzające się strony z rzędu oznaczają koniec danych.
            if stale_pages >= 2:
                break
            # Gdy API zwraca mniej niż limit elementów, zwykle jesteśmy na końcu.
            # Nie polegamy na tym bezwzględnie, bo parser może odrzucić elementy bez URL/ID.
            if page_no > 1 and len(page_links) == 0:
                break
        links = sorted(all_links.values(), key=lambda x: (-x.get("score", 0), x["url"]))
        pseudo_html = "\n".join(f'<a href="{html.escape(x["url"], quote=True)}">{html.escape(x.get("text", ""))}</a>' for x in links)
        return FetchResult(
            final_url=canonicalize_url(cfg.list_url),
            text=pseudo_html,
            status=200,
            content_type="application/json; tvp-list-by-tags",
            bytes_len=total_bytes,
            profile="tvp-api",
        ), links

    def _profile_filter_links(self, list_url, links):
        """Dodatkowy filtr URL dla profili, których nie da się opisać samym CSS."""
        profile = site_profile(list_url)
        kind = profile.get("filter", "")
        if kind == "tvp_numeric_article":
            out = []
            seen = set()
            for item in links or []:
                u = canonicalize_url(item.get("url", ""))
                if not u or u in seen:
                    continue
                path = urlsplit(u).path or ""
                # Artykuły TVP Info: /<numeryczne_id>/<slug>. Odrzuca /tag, menu,
                # galerie/kategorie bez ID i inne elementy nawigacyjne.
                if not re.match(r"^/\d{5,}(?:/[^/]+.*)?$", path):
                    continue
                seen.add(u)
                out.append(item)
            return out
        return list(links or [])

    def _collect_list_links(self, cfg, list_url, html_text, selector, strict):
        """Zbiera linki i stosuje profil serwisu. Dla TVP ma bezpieczny fallback
        na wbudowany filtr, gdy ręcznie wpisany selektor nie pasuje do DOM po JS.
        """
        links = self._filter_allowed_links(self.collect_links(list_url, html_text, selector, strict))
        links = self._profile_filter_links(list_url, links)
        profile = site_profile(list_url)
        if profile.get("filter") == "tvp_numeric_article" and not links and (cfg.selector or "").strip():
            # Użytkownik może mieć stary/ręczny selektor. Nie wymuszamy jego kasowania:
            # jeśli daje 0, ponawiamy lokalne parsowanie już wyrenderowanego HTML-a.
            fallback = self.collect_links(list_url, html_text, profile.get("selector", "a[href]"), True)
            links = self._profile_filter_links(list_url, self._filter_allowed_links(fallback))
        return links

    @staticmethod
    def _browser_host_allowed(source_url, request_url):
        """Zasoby potrzebne do wyrenderowania listy. TVP Info korzysta także z tvp.pl."""
        sh = host_of(source_url)
        rh = host_of(request_url)
        if not rh:
            return False
        if rh == sh or rh.endswith("." + sh):
            return True
        if sh == "tvp.info" or sh.endswith(".tvp.info"):
            return rh == "tvp.pl" or rh.endswith(".tvp.pl") or rh == "tvp.info" or rh.endswith(".tvp.info")
        return False

    def _render_dynamic_list(self, cfg, url, selector, strict, test_mode=False):
        """Renderuje wyłącznie listę wymagającą JavaScriptu.

        READ-ONLY: przeglądarka przepuszcza tylko GET/HEAD, blokuje obrazki, media
        i fonty. Dla TVP dopuszczone są tylko hosty tvp.info/tvp.pl. Artykuły są
        później nadal pobierane zwykłym limitowanym GET-em.
        """
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
        except Exception as e:
            if self.runtime_platform == "android-termux":
                raise RuntimeError(
                    "TVP Info: awaryjny fallback JavaScript nie jest dostępny w tej lekkiej wersji Termux. "
                    f"API i statyczny GET nie zwróciły listy. Szczegóły: {type(e).__name__}: {e}"
                )
            raise RuntimeError(
                "TVP Info: brak opcjonalnego Playwright. Na Windows uruchom INSTALUJ_WINDOWS.bat. "
                f"Szczegóły: {type(e).__name__}: {e}"
            )

        target = canonicalize_url(url)
        if not target or not self._is_allowed_url(target):
            raise RuntimeError("Nieprawidłowy URL dynamicznej listy")
        max_rounds = max(1, min(int(getattr(cfg, "max_list_pages", 100) or 100), 120))
        if (getattr(cfg, "list_mode", "auto") or "auto").lower() == "single":
            max_rounds = 1
        if test_mode:
            max_rounds = min(max_rounds, 10)
        scroll_wait_ms = 1800 if test_mode else 2200
        last_links = []
        final_url = target
        final_html = ""
        stable_rounds = 0
        previous_count = -1
        previous_height = -1

        def launch_browser(pw):
            errors = []
            for kwargs in (
                {"channel": "msedge", "headless": True},
                {"channel": "chrome", "headless": True},
            ):
                try:
                    return pw.chromium.launch(**kwargs)
                except Exception as exc:
                    errors.append(f"{kwargs.get('channel','browser')}: {exc}")
            # Dodatkowy fallback: przeglądarka wskazana zmienną środowiskową
            # albo dostępna w PATH (przydaje się także na komputerach bez Edge).
            candidates = []
            explicit = (os.environ.get("PRZEWIJAK_BROWSER") or "").strip()
            if explicit:
                candidates.append(explicit)
            for name in ("msedge", "chrome", "google-chrome", "chromium", "chromium-browser"):
                found = shutil.which(name)
                if found and found not in candidates:
                    candidates.append(found)
            for exe in candidates:
                try:
                    return pw.chromium.launch(executable_path=exe, headless=True)
                except Exception as exc:
                    errors.append(f"{exe}: {exc}")
            try:
                return pw.chromium.launch(headless=True)
            except Exception as exc:
                errors.append(f"playwright-chromium: {exc}")
            raise RuntimeError(
                "Nie znaleziono działającej przeglądarki dla Playwright. "
                "Na Windows uruchom INSTALUJ_WINDOWS.bat (instaluje Chromium). " + " | ".join(errors[-2:])
            )

        try:
            with sync_playwright() as pw:
                browser = launch_browser(pw)
                try:
                    context = browser.new_context(
                        locale="pl-PL",
                        viewport={"width": 1280, "height": 900},
                        service_workers="block",
                    )
                    page = context.new_page()

                    def route_handler(route):
                        req = route.request
                        method = (req.method or "GET").upper()
                        rtype = req.resource_type
                        allowed_method = method in {"GET", "HEAD"}
                        allowed_host = self._browser_host_allowed(target, req.url)
                        lightweight = rtype not in {"image", "media", "font"}
                        if not (allowed_method and allowed_host and lightweight):
                            with self.lock:
                                self.network_stats["browser_blocked"] += 1
                            try:
                                route.abort()
                            except Exception:
                                pass
                            return
                        with self.lock:
                            self.network_stats["browser_requests"] += 1
                        route.continue_()

                    page.route("**/*", route_handler)
                    self._set_status("TVP INFO: renderuję listę tagu w trybie JavaScript (READ-ONLY)…")
                    # Nie równoleglimy nawigacji z klasycznymi GET-ami programu.
                    with self.net_lock:
                        self._wait_request_slot()
                        try:
                            response = page.goto(target, wait_until="domcontentloaded", timeout=45000)
                        finally:
                            self.last_external_request = time.monotonic()
                    if response is not None and response.status in (403, 429):
                        self.fatal_stop_reason = f"LOW-IMPACT STOP: HTTP {response.status} · {target}"
                        self.stop_event.set()
                        raise RuntimeError(self.fatal_stop_reason)
                    page.wait_for_timeout(1800)
                    try:
                        page.evaluate("""() => {
                            document.documentElement.style.scrollBehavior='auto';
                            if (document.body) document.body.style.overflow='auto';
                        }""")
                    except Exception:
                        pass
                    with self.lock:
                        self.network_stats["browser_pages"] += 1

                    for round_no in range(1, max_rounds + 1):
                        if self.stop_event.is_set():
                            raise OperationCancelled("zatrzymano")
                        final_url = canonicalize_url(page.url) or target
                        final_html = page.content()
                        last_links = self._collect_list_links(cfg, final_url, final_html, selector, strict)
                        try:
                            height = int(page.evaluate("Math.max(document.body ? document.body.scrollHeight : 0, document.documentElement.scrollHeight)"))
                        except Exception:
                            height = -1
                        count = len(last_links)
                        with self.lock:
                            self.network_stats["browser_scroll_rounds"] += 1
                            self.progress["found"] = count
                        self._set_status(f"TVP INFO · JS porcja {round_no}/{max_rounds} · znaleziono {count} linków")

                        if count == previous_count and height == previous_height:
                            stable_rounds += 1
                        else:
                            stable_rounds = 0
                        previous_count, previous_height = count, height
                        if stable_rounds >= 2:
                            break
                        # TEST ma potwierdzić działanie, ale robi kilka przewinięć aby
                        # użytkownik zobaczył realną porcję linków, a nie tylko pierwszy ekran.
                        try:
                            page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
                        except Exception:
                            break
                        page.wait_for_timeout(scroll_wait_ms)

                    final_url = canonicalize_url(page.url) or target
                    final_html = page.content()
                    last_links = self._collect_list_links(cfg, final_url, final_html, selector, strict)
                    return FetchResult(
                        final_url=final_url,
                        text=final_html,
                        status=200,
                        content_type="text/html; rendered=playwright",
                        bytes_len=len(final_html.encode("utf-8", errors="ignore")),
                        profile="browser-js",
                    ), last_links
                finally:
                    try:
                        browser.close()
                    except Exception:
                        pass
        except OperationCancelled:
            raise
        except PlaywrightTimeoutError as e:
            raise RuntimeError(f"TVP Info: przekroczony czas renderowania listy: {e}")

    def _fetch_initial_list(self, cfg, selector, strict, test_mode=False):
        profile = site_profile(cfg.list_url)
        if profile.get("filter") == "tvp_numeric_article" and self._tvp_tag_value(cfg.list_url):
            errors = []
            try:
                result, links = self._tvp_fetch_tag_api(cfg, test_mode=test_mode)
                if links:
                    return result, links
                errors.append("API tagów: 0 linków")
            except OperationCancelled:
                raise
            except Exception as e:
                if self.fatal_stop_reason or self.stop_event.is_set():
                    raise
                errors.append(f"API tagów: {type(e).__name__}: {e}")

            # Drugi, nadal lekki fallback: zwykły HTML. Czasem serwer zwraca listę
            # bez potrzeby JavaScriptu (zależy od wariantu strony / wdrożenia).
            try:
                static_result = self.fetch(cfg.list_url)
                static_links = self._collect_list_links(cfg, static_result.final_url, static_result.text, selector, strict)
                if static_links:
                    static_result.profile = "tvp-static"
                    return static_result, static_links
                errors.append("statyczny GET: 0 linków")
            except OperationCancelled:
                raise
            except Exception as e:
                if self.fatal_stop_reason or self.stop_event.is_set():
                    raise
                errors.append(f"statyczny GET: {type(e).__name__}: {e}")

            if self.runtime_platform != "android-termux":
                try:
                    return self._render_dynamic_list(cfg, cfg.list_url, selector, strict, test_mode=test_mode)
                except OperationCancelled:
                    raise
                except Exception as e:
                    errors.append(f"JS fallback: {type(e).__name__}: {e}")
            else:
                errors.append("JS fallback: pominięty na Android/Termux")
            raise RuntimeError("TVP Info: nie udało się pobrać listy tagu. " + " | ".join(errors[-3:]))

        if profile.get("dynamic"):
            return self._render_dynamic_list(cfg, cfg.list_url, selector, strict, test_mode=test_mode)
        result = self.fetch(cfg.list_url)
        links = self._collect_list_links(cfg, result.final_url, result.text, selector, strict)
        return result, links

    def _effective_selector(self, cfg):
        selector = (cfg.selector or "").strip()
        strict = bool(cfg.strict_selector)
        profile = site_profile(cfg.list_url)
        if not selector and profile["selector"]:
            selector = profile["selector"]
            strict = bool(profile["strict"])
        return selector, strict, profile["name"]

    def collect_links(self, list_url, html_text, selector="", strict=False):
        soup = BeautifulSoup(html_text or "", "html.parser")
        selected = []
        if selector:
            try:
                for n in soup.select(selector):
                    if n.name == "a" and n.get("href"):
                        selected.append(n)
                    else:
                        selected.extend(n.find_all("a", href=True))
            except Exception as e:
                raise ValueError(f"Błędny selektor CSS: {e}")

        selected_ids = {id(n) for n in selected}
        nodes = list(selected)
        if not strict:
            nodes.extend(soup.find_all("a", href=True))

        scored = {}
        seen_node_keys = set()
        for a in nodes:
            href = a.get("href")
            text = normalize_text(a.get_text(" ", strip=True))
            node_key = (href, text)
            if node_key in seen_node_keys:
                continue
            seen_node_keys.add(node_key)
            if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
                continue
            abs_url = canonicalize_url(urljoin(list_url, href))
            if not abs_url or not same_site(list_url, abs_url) or abs_url == canonicalize_url(list_url):
                continue
            if likely_bad_link(abs_url, text):
                continue
            path = (urlsplit(abs_url).path or "").lower()
            is_selected = id(a) in selected_ids or (selector and strict)
            score = 0
            if is_selected:
                score += 8
            if any(h in path for h in ARTICLE_HINTS):
                score += 4
            if re.search(r"-st\d{5,}(?:$|[/?#])", abs_url, re.I):
                score += 8
            if len(text) >= 25:
                score += 2
            if len(text) >= 60:
                score += 1
            if a.find_parent("article") is not None:
                score += 3
            if a.find_parent(["h1", "h2", "h3"]) is not None:
                score += 2
            if path.count("/") >= 2:
                score += 1
            if strict:
                score += 10
            if score < (8 if strict else 4):
                continue
            item = {"url": abs_url, "text": text, "score": score}
            old = scored.get(abs_url)
            if old is None or score > old["score"] or (score == old["score"] and len(text) > len(old.get("text", ""))):
                scored[abs_url] = item
        return sorted(scored.values(), key=lambda x: (-x["score"], x["url"]))

    def find_next(self, current_url, html_text):
        soup = BeautifulSoup(html_text or "", "html.parser")
        current = canonicalize_url(current_url)
        rel = soup.find("a", rel=lambda v: v and "next" in (v if isinstance(v, list) else [v]), href=True)
        if rel:
            u = canonicalize_url(urljoin(current, rel.get("href")))
            if u and same_site(current, u) and u != current:
                return u

        wanted = re.compile(r"^(następna|następny|dalej|next|more|więcej|›|»|>)$", re.I)
        for a in soup.find_all("a", href=True):
            txt = normalize_text(a.get_text(" ", strip=True))
            aria = normalize_text(a.get("aria-label"))
            title = normalize_text(a.get("title"))
            if wanted.match(txt) or wanted.search(aria) or wanted.search(title):
                u = canonicalize_url(urljoin(current, a.get("href")))
                if u and same_site(current, u) and u != current:
                    return u

        cur_path = urlsplit(current).path.rstrip("/")
        m = re.match(r"^(.*?)/s-(\d+)$", cur_path)
        if m:
            base = m.group(1)
            cur_n = int(m.group(2))
        else:
            base = cur_path
            cur_n = 1
        wanted_n = cur_n + 1
        exact = re.compile(rf"^{re.escape(base)}/s-{wanted_n}$")
        for a in soup.find_all("a", href=True):
            u = canonicalize_url(urljoin(current, a.get("href")))
            if not u or not same_site(current, u):
                continue
            if exact.match(urlsplit(u).path.rstrip("/")):
                return u
        return ""

    def _numeric_next_from_links(self, current_url, html_text):
        soup = BeautifulSoup(html_text or "", "html.parser")
        current = canonicalize_url(current_url)
        candidates = []
        for a in soup.find_all("a", href=True):
            txt = normalize_text(a.get_text(" ", strip=True))
            if not re.fullmatch(r"\d{1,6}", txt):
                continue
            n = int(txt)
            u = canonicalize_url(urljoin(current, a.get("href")))
            if not u or not same_site(current, u) or u == current:
                continue
            candidates.append((n, u))
        if not candidates:
            return ""
        cur = 1
        ss = urlsplit(current)
        q = dict(parse_qsl(ss.query, keep_blank_values=True))
        for k in ("page", "p", "strona"):
            if str(q.get(k, "")).isdigit():
                cur = int(q[k]); break
        m = re.search(r"/(?:s-|page/|strona/)(\d+)(?:/)?$", ss.path, re.I)
        if m:
            cur = int(m.group(1))
        greater = sorted((n, u) for n, u in candidates if n > cur)
        return greater[0][1] if greater else ""

    def _template_page_url(self, template, page_no):
        template = str(template or "").strip()
        if not template:
            return ""
        try:
            return canonicalize_url(template.format(page=page_no, n=page_no))
        except Exception:
            return ""

    def _common_page_candidates(self, current_url, next_page_no):
        ss = urlsplit(canonicalize_url(current_url))
        out = []
        q = dict(parse_qsl(ss.query, keep_blank_values=True))
        for key in ("page", "p", "strona"):
            q2 = dict(q); q2[key] = str(next_page_no)
            out.append(urlunsplit((ss.scheme, ss.netloc, ss.path, urlencode(q2), "")))
        path = ss.path.rstrip("/")
        base = re.sub(r"/(?:s-\d+|page/\d+|strona/\d+)$", "", path, flags=re.I)
        out += [
            urlunsplit((ss.scheme, ss.netloc, f"{base}/s-{next_page_no}", ss.query, "")),
            urlunsplit((ss.scheme, ss.netloc, f"{base}/page/{next_page_no}", ss.query, "")),
            urlunsplit((ss.scheme, ss.netloc, f"{base}/strona/{next_page_no}", ss.query, "")),
        ]
        uniq = []
        for u in out:
            c = canonicalize_url(u)
            if c and c != canonicalize_url(current_url) and c not in uniq:
                uniq.append(c)
        return uniq

    def _filter_allowed_links(self, links):
        return [x for x in (links or []) if self._is_allowed_url(x.get("url", ""))]

    def _discover_next_batch(self, cfg, list_result, page_no, selector, strict, found_unique, visited_list_urls):
        mode = (cfg.list_mode or "auto").lower()
        if mode == "single" or site_profile(list_result.final_url).get("dynamic"):
            # Dynamiczna lista (TVP Info tag) została już przewinięta i zebrana
            # w jednej kontrolowanej sesji Playwright. Nie wykonujemy dodatkowych
            # zgadywanych żądań paginacji.
            return None, None
        candidates = []
        # LOW-IMPACT: nie sondąujemy wielu zgadywanych adresów.
        # Priorytet: wzór użytkownika -> jawny rel/"Następna" -> jawny numer strony -> znany wzór TVN24.
        templ = self._template_page_url(cfg.page_template, page_no + 1)
        explicit = self.find_next(list_result.final_url, list_result.text)
        numeric = self._numeric_next_from_links(list_result.final_url, list_result.text)
        if templ:
            candidates.append(templ)
        elif explicit:
            candidates.append(explicit)
        elif numeric:
            candidates.append(numeric)
        else:
            h = self._host_key(list_result.final_url)
            ss = urlsplit(canonicalize_url(list_result.final_url))
            if h == "tvn24.pl":
                base = re.sub(r"/s-\d+$", "", ss.path.rstrip("/"), flags=re.I)
                candidates.append(urlunsplit((ss.scheme, ss.netloc, f"{base}/s-{page_no + 1}", ss.query, "")))
        uniq = []
        for u in candidates:
            c = canonicalize_url(u)
            if c and c not in visited_list_urls and c not in uniq:
                uniq.append(c)
        for cand in uniq:
            if not self._is_allowed_url(cand):
                continue
            try:
                rr = self.fetch(cand, referer=list_result.final_url)
                cc = canonicalize_url(rr.final_url)
                if cc in visited_list_urls:
                    continue
                links = self._collect_list_links(cfg, rr.final_url, rr.text, selector, strict)
                new = {canonicalize_url(x["url"]) for x in links if canonicalize_url(x["url"])} - set(found_unique)
                if new:
                    return rr, links
            except Exception:
                continue
        return None, None

    def test_list(self, cfg):
        if self.running:
            raise RuntimeError("Najpierw zatrzymaj bieżące pobieranie przyciskiem STOP")
        self.save_form_config({
            "source_id": cfg.source_id, "source_name": cfg.source_name, "list_url": cfg.list_url,
            "selector": cfg.selector, "strict_selector": cfg.strict_selector, "max_articles": cfg.max_articles,
            "max_list_pages": cfg.max_list_pages, "list_mode": cfg.list_mode, "page_template": cfg.page_template,
            "retry": cfg.retry, "min_chars": cfg.min_chars, "pause": cfg.pause,
            "skip_processed": cfg.skip_processed, "output_dir": cfg.output_dir,
        })
        self.stop_event.clear()
        self.fatal_stop_reason = ""
        if cfg.output_dir and not (self.storage_mode == "saf" and cfg.output_dir == self.output_display()):
            self.set_output_root(cfg.output_dir)
        self._configure_low_impact(cfg)
        selector, strict, profile = self._effective_selector(cfg)
        result, links = self._fetch_initial_list(cfg, selector, strict, test_mode=True)
        with self.lock:
            self.last_test_links = links[:100]
            self.progress["found"] = len(links)
        sel_info = f" | profil {profile}" if profile != "AUTO" else ""
        render_info = " · TVP-API" if result.profile == "tvp-api" else (" · TVP-HTML" if result.profile == "tvp-static" else (" · JS-render" if result.profile == "browser-js" else ""))
        self._set_status(f"TEST LISTY: {len(links)} linków artykułów{sel_info}{render_info} · odstęp artykułów {self.request_interval:.0f}s · domena {self.allowed_host}")
        self._mark_source_state(cfg.source_id, self.status, ran=False)
        return links

    def start(self, cfg):
        if cfg.output_dir and not (self.storage_mode == "saf" and cfg.output_dir == self.output_display()):
            self.set_output_root(cfg.output_dir)
        self.save_form_config({
            "source_id": cfg.source_id, "source_name": cfg.source_name, "list_url": cfg.list_url,
            "selector": cfg.selector, "strict_selector": cfg.strict_selector, "max_articles": cfg.max_articles,
            "max_list_pages": cfg.max_list_pages, "list_mode": cfg.list_mode, "page_template": cfg.page_template,
            "retry": cfg.retry, "min_chars": cfg.min_chars, "pause": cfg.pause,
            "skip_processed": cfg.skip_processed, "output_dir": cfg.output_dir,
        })
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.stop_event.clear()
            self.fatal_stop_reason = ""
            self.progress = {
                "done": 0, "failed": 0, "found": 0, "attempted": 0,
                "skipped": 0, "list_page": 0, "current": "",
            }
            self.recent_failures = []
            self.active_source_id = int(cfg.source_id or 0)
            self.active_source_name = str(cfg.source_name or "")
            self.thread = threading.Thread(target=self._run, args=(cfg,), daemon=True)
            self.thread.start()
            return True

    def start_all(self):
        sources = [x for x in self.list_sources() if x.get("enabled")]
        if not sources:
            raise ValueError("Brak włączonych źródeł")
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.batch_running = True
            self.batch_index = 0
            self.batch_total = len(sources)
            self.batch_done_sources = 0
            self.stop_event.clear()
            self.fatal_stop_reason = ""
            self.progress = {
                "done": 0, "failed": 0, "found": 0, "attempted": 0,
                "skipped": 0, "list_page": 0, "current": "",
            }
            self.recent_failures = []
            self.thread = threading.Thread(target=self._run_all_sources, args=(sources,), daemon=True)
            self.thread.start()
            return True

    def _run_all_sources(self, sources):
        total = len(sources)
        completed = 0
        try:
            for idx, src in enumerate(sources, 1):
                if self.stop_event.is_set():
                    break
                cfg = settings_from_json({**src, "source_id": src.get("id", 0), "source_name": src.get("name", "")})
                with self.lock:
                    self.batch_index = idx
                    self.active_source_id = int(src.get("id") or 0)
                    self.active_source_name = str(src.get("name") or "")
                    self.progress = {
                        "done": 0, "failed": 0, "found": 0, "attempted": 0,
                        "skipped": 0, "list_page": 0, "current": "",
                    }
                self._set_status(f"PAKIET ŹRÓDEŁ {idx}/{total} · {self.active_source_name}")
                self._run(cfg)
                completed += 1
                with self.lock:
                    self.batch_done_sources = completed
                if self.fatal_stop_reason:
                    break
        finally:
            stopped = self.stop_event.is_set()
            fatal = self.fatal_stop_reason
            with self.lock:
                self.batch_running = False
                self.running = False
                self.active_source_id = 0
                self.active_source_name = ""
                self.progress["current"] = ""
            if fatal:
                self._set_status(f"PAKIET PRZERWANY: {fatal} · źródła ukończone {completed}/{total}")
            elif stopped:
                self._set_status(f"Pakiet zatrzymany · źródła ukończone {completed}/{total}")
            else:
                self._set_status(f"Pakiet gotowy · źródła ukończone {completed}/{total}")

    def stop(self):
        self.stop_event.set()
        self._set_status("Zatrzymywanie po bieżącej operacji…")

    @staticmethod
    def _append_csv(path, row, fields):
        exists = path.exists()
        with path.open("a", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            if not exists:
                w.writeheader()
            w.writerow({k: row.get(k, "") for k in fields})

    def _save_article(self, url, html_text, extracted, fetch_result, list_page, number):
        title = extracted.get("title") or extracted.get("page_title") or "artykul"
        canonical = extracted.get("canonical") or canonicalize_url(url)
        key = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:10]
        folder = self.output_root / f"{number:04d}_{safe_slug(title)}_{key}"
        temp = self.output_root / f".tmp_{number:04d}_{key}_{int(time.time()*1000)}"
        if temp.exists():
            shutil.rmtree(temp, ignore_errors=True)
        temp.mkdir(parents=True, exist_ok=False)
        try:
            text = repair_polish_mojibake((extracted.get("text") or "").strip())
            authors = [repair_polish_mojibake(normalize_text(x)) for x in (extracted.get("authors") or []) if normalize_text(x)]
            tags = [repair_polish_mojibake(normalize_text(x)) for x in (extracted.get("tags") or []) if normalize_text(x)]
            title_txt = repair_polish_mojibake(normalize_text(title))
            extra = dict(extracted.get("extended_metadata") or {})

            def meta_value(key, fallback="BRAK"):
                v = repair_polish_mojibake(normalize_text(extra.get(key, "")))
                return v or fallback

            saved_at = now_iso()
            redakcja = meta_value("publisher", "") or meta_value("site_name", "") or "BRAK"
            autor = "; ".join(authors) if authors else "BRAK"
            tagi = ", ".join(tags) if tags else "BRAK"
            header_rows = [
                ("TYTUŁ", title_txt or "BRAK"),
                ("DATA PUBLIKACJI", meta_value("published_date")),
                ("GODZINA PUBLIKACJI", meta_value("published_time")),
                ("DATA/GODZINA PUBLIKACJI — ORYGINAŁ", meta_value("published_at")),
                ("AKTUALIZACJA", meta_value("modified_at")),
                ("AUTOR", autor),
                ("REDAKCJA/WYDAWCA", redakcja),
                ("SERWIS", meta_value("site_name")),
                ("DZIAŁ/KATEGORIA", meta_value("section")),
                ("ŹRÓDŁO", meta_value("source_name")),
                ("TAGI", tagi),
                ("ID ARTYKUŁU", meta_value("article_id")),
                ("URL", url or "BRAK"),
                ("CANONICAL URL", canonical or "BRAK"),
                ("DATA POBRANIA", saved_at),
            ]
            header_text = "\n".join(f"{k}: {v}" for k, v in header_rows)
            text_file = header_text + "\n\n" + ("-" * 72) + "\n\n" + text
            (temp / "raw.html").write_text(html_text, encoding="utf-8")
            (temp / "artykul.txt").write_text(text_file + "\n", encoding="utf-8-sig")
            readable = """<!doctype html><html lang='pl'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><style>body{font-family:system-ui,sans-serif;max-width:820px;margin:24px auto;padding:0 16px;line-height:1.65;font-size:18px}h1{line-height:1.2}pre{white-space:pre-wrap;font:inherit}.meta{padding:12px 0;border-top:1px solid #ddd;margin-top:24px}</style></head><body>"""
            readable += f"<h1>{html.escape(title)}</h1>"
            readable += "<div class='meta'>" + "<br>".join(
                f"<strong>{html.escape(k)}:</strong> {html.escape(v)}" for k, v in header_rows[1:]
            ) + "</div>"
            readable += f"<pre>{html.escape(text)}</pre>"
            readable += "</body></html>"
            (temp / "artykul.html").write_text(readable, encoding="utf-8")
            content_hash = self._content_hash(text)
            metadata = {
                "app_version": APP_VERSION,
                "saved_at": saved_at,
                "url": url,
                "canonical": canonical,
                "title": title,
                "page_title": extracted.get("page_title", ""),
                "published_at": extra.get("published_at", ""),
                "published_date": extra.get("published_date", ""),
                "published_time": extra.get("published_time", ""),
                "modified_at": extra.get("modified_at", ""),
                "modified_date": extra.get("modified_date", ""),
                "modified_time": extra.get("modified_time", ""),
                "publisher": extra.get("publisher", ""),
                "site_name": extra.get("site_name", ""),
                "section": extra.get("section", ""),
                "source_name": extra.get("source_name", ""),
                "article_id": extra.get("article_id", ""),
                "text_chars": len(text),
                "blocks": extracted.get("blocks", 0),
                "extract_method": extracted.get("method", ""),
                "json_path": extracted.get("json_path", ""),
                "candidate_diagnostics": extracted.get("candidates", []),
                "authors": authors,
                "tags": tags,
                "content_hash": content_hash,
                "list_page": list_page,
                "article_no": number,
                "http_status": fetch_result.status,
                "fetch_profile": fetch_result.profile,
                "response_bytes": fetch_result.bytes_len,
                "files": ["raw.html", "artykul.txt", "artykul.html", "metadata.json"],
            }
            (temp / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            if folder.exists():
                shutil.rmtree(folder)
            temp.replace(folder)
        except Exception:
            shutil.rmtree(temp, ignore_errors=True)
            raise

        # W trybie Android/SAF artykuł jest najpierw bezpiecznie złożony lokalnie,
        # a następnie kopiowany do folderu wybranego przez system Android.
        self._saf_sync_article_folder(folder)

        row = {
            "saved_at": now_iso(), "title": title, "url": url, "canonical": canonical,
            "text_chars": extracted.get("text_chars", 0), "extract_method": extracted.get("method", ""),
            "content_hash": content_hash, "list_page": list_page, "article_no": number, "folder": folder.name,
        }
        self._append_csv(
            self.index_csv, row,
            ["saved_at", "title", "url", "canonical", "text_chars", "extract_method", "content_hash", "list_page", "article_no", "folder"],
        )
        self.rebuild_index_html()
        self._saf_sync_root_file(self.index_csv)
        self._saf_sync_root_file(self.index_html)
        return folder

    def rebuild_index_html(self):
        rows = []
        if self.index_csv.exists():
            with self.index_csv.open("r", encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
        out = [
            "<!doctype html><html lang='pl'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
            "<title>Przewijak — artykuły</title>",
            "<style>body{font-family:system-ui,sans-serif;max-width:900px;margin:auto;padding:18px;background:#101418;color:#f5f7fa}a{color:#74b8ff}.card{background:#182028;border:1px solid #303a45;border-radius:14px;padding:14px;margin:10px 0}.muted{color:#aeb8c2;font-size:13px}</style></head><body>",
            "<h1>Archiwum artykułów</h1>",
        ]
        for r in reversed(rows[-1000:]):
            folder = html.escape(r.get("folder", ""))
            title = html.escape(r.get("title", ""))
            url = html.escape(r.get("url", ""))
            chars = html.escape(str(r.get("text_chars", "")))
            method = html.escape(r.get("extract_method", ""))
            out.append(f"<div class='card'><b>{title}</b><div class='muted'>{chars} znaków · {method}</div><p><a href='{quote(folder)}/artykul.html'>czytaj lokalnie</a> · <a href='{url}'>źródło</a></p></div>")
        out.append("</body></html>")
        self.index_html.write_text("\n".join(out), encoding="utf-8")

    def _record_failed(self, url, reason, list_page, attempts):
        reason = normalize_text(reason)[:1200]
        row = {"time": now_iso(), "url": url, "reason": reason, "list_page": list_page, "attempts": attempts}
        self._append_csv(self.failed_csv, row, ["time", "url", "reason", "list_page", "attempts"])
        self._saf_sync_root_file(self.failed_csv)
        with self.lock:
            self.recent_failures.append({"url": url, "reason": reason})
            self.recent_failures = self.recent_failures[-8:]

    def _run(self, cfg):
        self._configure_low_impact(cfg)
        if cfg.skip_processed:
            self._sync_archive_to_db(self.output_root)
        processed = self.load_processed() if cfg.skip_processed else set()
        seen_this_run = set()
        found_unique = set()
        visited_list_urls = set()
        saved_no = 0
        selector, strict, _profile = self._effective_selector(cfg)
        try:
            list_url = canonicalize_url(cfg.list_url)
            if not list_url:
                raise ValueError("Nieprawidłowy URL listy")
            list_result, prefetched_links = self._fetch_initial_list(cfg, selector, strict, test_mode=False)
            page_no = 1
            max_rounds = max(1, cfg.max_list_pages)
            while page_no <= max_rounds and not self.stop_event.is_set() and saved_no < cfg.max_articles:
                list_canon = canonicalize_url(list_result.final_url)
                if list_canon in visited_list_urls:
                    self._set_status("Koniec listy: ta sama porcja została już odwiedzona.")
                    break
                visited_list_urls.add(list_canon)
                with self.lock:
                    self.progress["list_page"] = page_no
                self._set_status(f"Lista/porcja {page_no}: {list_result.final_url}")
                links = prefetched_links if prefetched_links is not None else self._collect_list_links(cfg, list_result.final_url, list_result.text, selector, strict)
                prefetched_links = None
                for item in links:
                    c = canonicalize_url(item["url"])
                    if c:
                        found_unique.add(c)
                with self.lock:
                    self.progress["found"] = len(found_unique)

                for item in links:
                    if self.stop_event.is_set() or saved_no >= cfg.max_articles:
                        break
                    url = item["url"]
                    if not self._is_allowed_url(url):
                        continue
                    canon = canonicalize_url(url)
                    if not canon or canon in seen_this_run:
                        continue
                    seen_this_run.add(canon)
                    if cfg.skip_processed and (canon in processed or self._db_has_url(canon)):
                        self._db_link_source(canon, cfg.source_id, cfg.source_name, url)
                        with self.lock:
                            self.progress["skipped"] += 1
                        continue
                    with self.lock:
                        self.progress["current"] = url
                        self.progress["attempted"] += 1
                    success = False
                    last_error = ""
                    attempts_total = max(1, min(2, cfg.retry + 1))
                    for attempt in range(1, attempts_total + 1):
                        if self.stop_event.is_set():
                            break
                        self._set_status(f"Artykuł {saved_no + 1}/{cfg.max_articles} · próba {attempt}/{attempts_total}\n{url}")
                        try:
                            result = self.fetch(url, referer=list_result.final_url)
                            extracted = ArticleExtractor.extract(result.text, result.final_url)
                            ArticleExtractor.validate(extracted, result.text, cfg.min_chars)
                            final_canon = canonicalize_url(result.final_url)
                            content_canon = canonicalize_url(extracted.get("canonical", ""))
                            content_hash = self._content_hash(extracted.get("text", ""))
                            if cfg.skip_processed and (
                                self._db_has_url(final_canon) or
                                (content_canon and self._db_has_url(content_canon)) or
                                self._db_has_hash(content_hash)
                            ):
                                for u in (canon, final_canon, content_canon):
                                    if u:
                                        processed.add(u)
                                self.save_processed(processed)
                                self._db_link_source(content_canon or final_canon or canon, cfg.source_id, cfg.source_name, result.final_url or url)
                                with self.lock:
                                    self.progress["skipped"] += 1
                                self._set_status(f"Pominięto — już jest w bazie: {extracted.get('title','')[:110]}")
                                success = True
                                break
                            next_no = saved_no + 1
                            folder = self._save_article(result.final_url, result.text, extracted, result, page_no, next_no)
                            for u in (canon, final_canon, content_canon):
                                if u:
                                    processed.add(u)
                            self.save_processed(processed)
                            self._db_add(
                                content_canon or final_canon or canon,
                                final_canon or canon,
                                extracted.get("title") or extracted.get("page_title") or "",
                                content_hash,
                                now_iso(),
                                str(folder),
                                source="saved",
                                source_id=cfg.source_id,
                                source_name=cfg.source_name,
                            )
                            self._db_link_source(content_canon or final_canon or canon, cfg.source_id, cfg.source_name, final_canon or canon)
                            # Alias wejściowego URL też trafia do bazy, jeśli różni się od canonical.
                            if canon and canon != (content_canon or final_canon):
                                self._db_add(canon, canon, extracted.get("title") or "", content_hash, now_iso(), str(folder), source="alias", source_id=cfg.source_id, source_name=cfg.source_name)
                            saved_no = next_no
                            with self.lock:
                                self.progress["done"] = saved_no
                            success = True
                            self._set_status(f"Zapisano {saved_no}/{cfg.max_articles}: {extracted.get('title','')[:110]}\n{extracted.get('text_chars',0)} znaków · {extracted.get('method','')}")
                            break
                        except Exception as e:
                            last_error = f"{type(e).__name__}: {e}"
                            if self.stop_event.is_set():
                                break
                            # Bez szybkich ponowień; kolejny GET i tak przejdzie przez globalny limiter.
                    if not success and not self.stop_event.is_set():
                        with self.lock:
                            self.progress["failed"] += 1
                        self._record_failed(url, last_error or "nieznany błąd", page_no, attempts_total)
                        self._set_status(f"BŁĄD: {last_error}\n{url}")
                    # Pauza jest wymuszana globalnie przed każdym zewnętrznym GET-em.

                if saved_no >= cfg.max_articles or self.stop_event.is_set():
                    break
                next_result, next_links = self._discover_next_batch(cfg, list_result, page_no, selector, strict, found_unique, visited_list_urls)
                if not next_result:
                    self._set_status(f"Koniec listy: brak kolejnej strony/porcji. Zapisane {saved_no}/{cfg.max_articles}.")
                    break
                list_result = next_result
                prefetched_links = next_links
                page_no += 1

            if self.stop_event.is_set():
                if self.fatal_stop_reason:
                    self._set_status(f"{self.fatal_stop_reason}\nZapisane: {self.progress['done']} · błędy: {self.progress['failed']} · pominięte: {self.progress['skipped']}")
                else:
                    self._set_status(f"Zatrzymano. Zapisane: {self.progress['done']} · błędy: {self.progress['failed']} · pominięte: {self.progress['skipped']}")
            elif saved_no >= cfg.max_articles:
                self._set_status(f"Gotowe: osiągnięto limit {saved_no} nowych artykułów.")
            else:
                self._set_status(f"Gotowe. Zapisane: {self.progress['done']} · błędy: {self.progress['failed']} · pominięte: {self.progress['skipped']}")
        except Exception as e:
            self._set_status(f"Błąd główny: {type(e).__name__}: {e}")
            self._mark_source_state(cfg.source_id, self.status, error=f"{type(e).__name__}: {e}", ran=True)
        else:
            self._mark_source_state(cfg.source_id, self.status, error="", ran=True)
        finally:
            with self.lock:
                if not self.batch_running:
                    self.running = False
                    self.active_source_id = 0
                    self.active_source_name = ""
                self.progress["current"] = ""


DL = Downloader()

INDEX_HTML = r'''<!doctype html>
<html lang="pl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Przewijak — MultiSource</title>
<style>
:root{--bg:#101418;--card:#182028;--line:#303a45;--txt:#f5f7fa;--muted:#aeb8c2;--ok:#16833c;--bad:#a92d38;--blue:#176bb8;--amber:#9b6b11}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font-family:system-ui,-apple-system,sans-serif;padding:14px}.wrap{max-width:820px;margin:auto}.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:14px;margin-bottom:12px}h1{font-size:23px}h2{font-size:17px;margin:4px 0 10px}label{display:block;color:var(--muted);font-size:13px;margin:10px 0 5px}input,select{width:100%;min-height:50px;background:#0f1419;color:#fff;border:1px solid #3a4652;border-radius:12px;padding:0 12px;font-size:16px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.buttons{display:grid;grid-template-columns:1fr 1fr;gap:9px}button{min-height:52px;border:0;border-radius:13px;color:#fff;font-weight:800;font-size:15px;background:#28323d;padding:8px}button.ok{background:var(--ok)}button.bad{background:var(--bad)}button.blue{background:var(--blue)}button.amber{background:var(--amber)}.status{background:#0f1419;border-radius:12px;padding:12px;white-space:pre-wrap;word-break:break-word}.tiny{font-size:12px;color:var(--muted)}.links a{display:block;color:#74b8ff;padding:8px 0;border-bottom:1px solid #26313b;text-decoration:none}.checks{display:grid;grid-template-columns:1fr 1fr;gap:8px}.check{display:flex;align-items:center;gap:8px;background:#121920;padding:10px;border-radius:10px}.check input{width:auto;min-height:auto}.fail{padding:7px 0;border-bottom:1px solid #26313b;color:#ffb2b7;word-break:break-word}.folderbuttons{margin-top:8px}.folderbuttons button{min-height:48px}.source{background:#11181f;border:1px solid #2b3641;border-radius:13px;padding:11px;margin:8px 0}.source.off{opacity:.58}.sourcehead{display:flex;gap:8px;align-items:center;justify-content:space-between}.sourceurl{word-break:break-all;color:#90bde8;font-size:12px;margin:5px 0}.sourceactions{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-top:8px}.sourceactions button{min-height:42px;font-size:12px}.badge{font-size:11px;padding:4px 7px;border-radius:999px;background:#25303a}.badge.on{background:#143c24}.picker{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:50;padding:14px}.picker.show{display:flex;align-items:center;justify-content:center}.pickerbox{width:min(680px,100%);max-height:88vh;overflow:auto;background:var(--card);border:1px solid var(--line);border-radius:18px;padding:14px}.pickerpath{font-size:11px;color:var(--muted);word-break:break-all;margin:6px 0 10px}.pickertip{background:#10261a;border:1px solid #2f8f5b;border-radius:12px;padding:10px;margin:8px 0 12px}.quickgrid{display:grid;grid-template-columns:1fr;gap:6px;margin-bottom:10px}.dirbtn{width:100%;min-height:48px;text-align:left;padding:8px 12px;margin:4px 0;background:#202a34}.rootbtn{background:#24313c}.pickeractions{display:grid;grid-template-columns:1fr 1fr;gap:8px;position:sticky;bottom:-14px;background:var(--card);padding-top:10px;padding-bottom:4px}@media(max-width:560px){.buttons,.grid{grid-template-columns:1fr 1fr}.sourceactions{grid-template-columns:1fr}.pickerbox{max-height:92vh}}
</style></head><body><div class="wrap">
<h1>📰 Przewijak — ARTYKUŁY · MultiSource</h1>
<div class="card" style="border-color:#2f8f5b"><b>V15.2 — PAKIET ŹRÓDEŁ + AUTOMATYCZNE NOWE ARTYKUŁY</b><div class="tiny">Program ma pakiet wielu redakcji zapisany w SQLite. START WSZYSTKICH przechodzi po włączonych źródłach po kolei, z jednym GET naraz, odstępem i pomijaniem artykułów już obecnych w bazie.</div></div>
<div class="card"><h2>Źródła</h2><div class="buttons"><button class="ok" onclick="saveSource()">💾 ZAPISZ / AKTUALIZUJ ŹRÓDŁO</button><button onclick="newSource()">＋ NOWE ŹRÓDŁO</button></div><div id="sources" class="tiny" style="margin-top:10px">Ładowanie…</div></div>
<div class="card"><input id="sourceid" type="hidden" value="0"><label>Nazwa źródła</label><input id="sourcename" placeholder="np. TVN24 — Najnowsze"><label>URL listy / strony z artykułami</label><input id="url" placeholder="https://..."><label>Folder zapisu</label><input id="outdir" placeholder="Folder wybierzesz przyciskiem poniżej"><div class="buttons folderbuttons"><button id="pickbtn" type="button" class="ok" onclick="pickFolder()">📁 WYBIERZ FOLDER</button><button type="button" onclick="setFolder()">✅ USTAW WPISANĄ ŚCIEŻKĘ</button><button type="button" onclick="defaultFolder()">📥 DOMYŚLNE POBRANE</button><button type="button" onclick="openArchive()">📚 ARCHIWUM</button></div><div class="tiny" id="folderhelp">Nie musisz znać ścieżki — naciśnij WYBIERZ FOLDER i wskaż katalog.</div><label>Selektor CSS linków — opcjonalnie</label><input id="selector" placeholder="TVN24 i TVP Info tag: może zostać puste — profil ustawi się sam"><div class="checks"><label class="check"><input id="strict" type="checkbox" checked>Tylko selektor</label><label class="check"><input id="skip" type="checkbox" checked>Pomiń artykuły już w bazie</label></div><label>Sposób przechodzenia listy</label><select id="mode"><option value="auto">AUTO — strony / profil serwisu</option><option value="pages">STRONY — 1,2,3 / Następna</option><option value="scroll">PRZEWIJANIE — profil dynamiczny</option><option value="single">TYLKO BIEŻĄCA STRONA</option></select><label>Wzór kolejnej strony — opcjonalnie</label><input id="template" placeholder="np. https://serwis.pl/news/page/{page}"><div class="grid"><div><label>Max NOWYCH zapisanych</label><input id="maxa" type="number" value="100" min="1"></div><div><label>Max stron / porcji</label><input id="maxp" type="number" value="100" min="1"></div><div><label>Retry po błędzie</label><input id="retry" type="number" value="1" min="0" max="1"></div><div><label>Min. znaków artykułu</label><input id="minc" type="number" value="300" min="50"></div><div><label>Min. odstęp między GET [s]</label><input id="pause" type="number" value="8" min="5" max="120" step="1"></div></div></div>
<div class="card"><div class="buttons"><button class="blue" onclick="testList()">🧪 TEST WYBRANEGO</button><button class="ok" onclick="start()">▶ START WYBRANEGO</button><button class="ok" onclick="startAll()">▶▶ START WSZYSTKICH WŁĄCZONYCH</button><button class="bad" onclick="stop()">■ STOP</button><button onclick="refresh()">↻ ODŚWIEŻ STATUS</button></div></div>
<div class="card"><h2>Status</h2><div id="status" class="status">Gotowe</div><p id="folder" class="tiny"></p></div>
<div class="card"><h2>Ostatnie błędy</h2><div id="failures" class="tiny">—</div></div>
<div class="card"><h2>Linki z TEST LISTY</h2><div id="links" class="links tiny">—</div></div>
</div>
<div id="folderPicker" class="picker" onclick="if(event.target===this)closeFolderPicker()"><div class="pickerbox"><h2>📁 Wybierz folder zapisu</h2><div class="pickertip"><b>Nie wpisujesz żadnej ścieżki.</b><br><span class="tiny">To jest tryb awaryjny bez Termux:API. Pełny wybór karty SD wymaga systemowego SAF; tutaj widoczne są tylko katalogi, do których Termux ma zwykły dostęp plikowy.</span></div><div id="pickerRoots" class="quickgrid"></div><div id="pickerStorageInfo" class="tiny"></div><div id="pickerDirs"></div><div class="pickerpath">Aktualny katalog: <span id="pickerPath">—</span></div><div class="pickeractions"><button class="ok" onclick="choosePickerFolder()">✅ UŻYJ TEGO FOLDERU</button><button onclick="closeFolderPicker()">✕ ANULUJ</button></div></div></div>
<script>
let sourcesCache=[];let configLoaded=false;let saveTimer=null;
function val(id){return document.getElementById(id).value} function chk(id){return document.getElementById(id).checked}
function cfg(){return {source_id:+val('sourceid')||0,source_name:val('sourcename').trim(),list_url:val('url').trim(),output_dir:val('outdir').trim(),selector:val('selector').trim(),strict_selector:chk('strict'),max_articles:+val('maxa')||100,max_list_pages:+val('maxp')||100,list_mode:val('mode')||'auto',page_template:val('template').trim(),retry:+val('retry')||0,min_chars:+val('minc')||300,pause:+val('pause')||8,skip_processed:chk('skip')}}
async function post(path,obj){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(obj||{})});const j=await r.json();if(!r.ok)throw new Error(j.error||('HTTP '+r.status));return j}
function fill(c){c=c||{};document.getElementById('sourceid').value=c.source_id||c.id||0;document.getElementById('sourcename').value=c.source_name||c.name||'';document.getElementById('url').value=c.list_url||'';if(c.output_dir!==undefined)document.getElementById('outdir').value=c.output_dir||'';document.getElementById('selector').value=c.selector||'';document.getElementById('strict').checked=c.strict_selector!==false;document.getElementById('maxa').value=c.max_articles||100;document.getElementById('maxp').value=c.max_list_pages||100;document.getElementById('mode').value=c.list_mode||'auto';document.getElementById('template').value=c.page_template||'';document.getElementById('retry').value=(c.retry===0?0:(c.retry||1));document.getElementById('minc').value=c.min_chars||300;document.getElementById('pause').value=c.pause||8;document.getElementById('skip').checked=c.skip_processed!==false}
function newSource(){const out=val('outdir');fill({output_dir:out,strict_selector:true,skip_processed:true,max_articles:100,max_list_pages:100,list_mode:'auto',retry:1,min_chars:300,pause:8});scheduleConfigSave()}
async function loadSources(){try{const r=await fetch('/api/sources',{cache:'no-store'});const j=await r.json();sourcesCache=j.sources||[];const box=document.getElementById('sources');if(!sourcesCache.length){box.innerHTML='<div class="tiny">Brak zapisanych źródeł. Wypełnij formularz i kliknij ZAPISZ ŹRÓDŁO.</div>';return}box.innerHTML=sourcesCache.map(x=>'<div class="source '+(x.enabled?'':'off')+'"><div class="sourcehead"><b>'+esc(x.name)+'</b><span class="badge '+(x.enabled?'on':'')+'">'+(x.enabled?'WŁ.':'WYŁ.')+'</span></div><div class="sourceurl">'+esc(x.list_url)+'</div><div class="tiny">Artykuły przypisane: '+(x.article_count||0)+(x.last_run_at?' · ostatnie uruchomienie: '+esc(x.last_run_at):'')+'</div>'+(x.last_status?'<div class="tiny">'+esc(x.last_status).slice(0,220)+'</div>':'')+'<div class="sourceactions"><button class="blue" onclick="loadSource('+x.id+')">WCZYTAJ</button><button class="amber" onclick="toggleSource('+x.id+','+(!x.enabled)+')">'+(x.enabled?'WYŁĄCZ':'WŁĄCZ')+'</button><button class="bad" onclick="deleteSource('+x.id+')">USUŃ</button></div></div>').join('')}catch(e){document.getElementById('sources').textContent='Błąd listy źródeł: '+e.message}}
function loadSource(id){const x=sourcesCache.find(v=>v.id===id);if(x){fill({...x,source_id:x.id,source_name:x.name});scheduleConfigSave();window.scrollTo({top:0,behavior:'smooth'})}}
async function saveSource(){try{const c=cfg();if(!c.list_url){alert('Podaj URL źródła.');return}const old=sourcesCache.find(v=>v.id===c.source_id);const j=await post('/api/sources/save',{...c,enabled:old?old.enabled:true});if(j.source){fill({...j.source,source_id:j.source.id,source_name:j.source.name})}await loadSources();await saveConfig()}catch(e){alert(e.message)}}
async function deleteSource(id){if(!confirm('Usunąć to źródło z listy? Artykuły i baza historii NIE zostaną usunięte.'))return;try{await post('/api/sources/delete',{id});if(+val('sourceid')===id)newSource();await loadSources()}catch(e){alert(e.message)}}
async function toggleSource(id,enabled){try{await post('/api/sources/toggle',{id,enabled});await loadSources()}catch(e){alert(e.message)}}
async function saveConfig(){try{await post('/api/config',cfg())}catch(e){}}
function scheduleConfigSave(){clearTimeout(saveTimer);saveTimer=setTimeout(saveConfig,350)}
async function loadConfig(){try{const r=await fetch('/api/config',{cache:'no-store'});const j=await r.json();if(j.config&&Object.keys(j.config).length)fill(j.config);configLoaded=true}catch(e){configLoaded=true}}
async function testList(){try{await saveConfig();const j=await post('/api/test',cfg());if(j.cancelled)return}catch(e){alert(e.message)}refresh();loadSources()}
async function start(){try{await saveConfig();await post('/api/start',cfg())}catch(e){alert(e.message)}refresh()}
async function startAll(){try{await post('/api/start-all',{})}catch(e){alert(e.message)}refresh();loadSources()}
async function stop(){try{await post('/api/stop',{})}catch(e){}refresh()}
let pickerCurrent='';
async function pickFolder(){try{if(window.AndroidBridge&&AndroidBridge.chooseFolder){AndroidBridge.chooseFolder();return}const j=await post('/api/pick-output',{});if(j.warning)alert(j.warning);if(j.picker==='internal'){await openFolderPicker(j.initial_path||'');return}if(j.output_root)document.getElementById('outdir').value=j.output_root;scheduleConfigSave()}catch(e){alert(e.message)}refresh()}
async function setFolder(){try{const raw=val('outdir').trim();if(!raw){alert('Najpierw wpisz ścieżkę albo użyj WYBIERZ FOLDER.');return}if(raw.startsWith('Android/SAF:')){alert('Ten folder został już wybrany przez system Android.');return}const j=await post('/api/output',{output_dir:raw});if(j.output_root)document.getElementById('outdir').value=j.output_root;scheduleConfigSave()}catch(e){alert(e.message)}refresh()}
async function defaultFolder(){try{const j=await post('/api/default-output',{});if(j.output_root)document.getElementById('outdir').value=j.output_root;scheduleConfigSave()}catch(e){alert(e.message)}refresh()}
async function openFolderPicker(path){try{const r=await fetch('/api/folders?path='+encodeURIComponent(path||''),{cache:'no-store'});const j=await r.json();if(!r.ok)throw new Error(j.error||('HTTP '+r.status));pickerCurrent=j.path||'';document.getElementById('pickerPath').textContent=pickerCurrent||'—';document.getElementById('pickerStorageInfo').textContent=j.storage_info?('Dostęp: '+j.storage_info):'';const roots=j.roots||[];document.getElementById('pickerRoots').innerHTML=roots.map(x=>'<button class="dirbtn rootbtn" data-p="'+esc(encodeURIComponent(x.path||''))+'" onclick="openFolderPicker(decodeURIComponent(this.dataset.p))">'+esc(x.name||'Pamięć')+'</button>').join('');let h='';if(j.parent)h+='<button class="dirbtn" data-p="'+esc(encodeURIComponent(j.parent))+'" onclick="openFolderPicker(decodeURIComponent(this.dataset.p))">⬆️ Folder wyżej</button>';for(const x of (j.dirs||[])){h+='<button class="dirbtn" data-p="'+esc(encodeURIComponent(x.path||''))+'" onclick="openFolderPicker(decodeURIComponent(this.dataset.p))">📁 '+esc(x.name)+'</button>'}document.getElementById('pickerDirs').innerHTML=h||'<div class="tiny">W tym miejscu nie ma podfolderów.</div>';document.getElementById('folderPicker').classList.add('show')}catch(e){alert('Nie udało się wejść do folderu: '+e.message)}}
function closeFolderPicker(){document.getElementById('folderPicker').classList.remove('show')}
async function choosePickerFolder(){try{const j=await post('/api/output',{output_dir:pickerCurrent});if(j.output_root)document.getElementById('outdir').value=j.output_root;closeFolderPicker();scheduleConfigSave();refresh()}catch(e){alert(e.message)}}
function openArchive(){window.open('/archive/index.html','_blank')}
function esc(s){return String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function refresh(){try{const r=await fetch('/api/status',{cache:'no-store'});const s=await r.json();const p=s.progress||{};document.getElementById('status').textContent=(s.running?'🟢 PRACUJE'+(s.active_source_name?' · '+s.active_source_name:'')+(s.batch_running?' · źródło '+(s.batch_index||0)+'/'+(s.batch_total||0):'')+'\\n':'⚪ STOP\\n')+(s.status||'')+'\\n\\nZapisane: '+(p.done||0)+' | Błędy: '+(p.failed||0)+' | Próbowane: '+(p.attempted||0)+' | Pominięte: '+(p.skipped||0)+' | znalezione: '+(p.found||0)+' | lista: '+(p.list_page||0)+(p.current?'\\n\\nTeraz: '+p.current:'');const n=s.network||{};const cl=s.cleanup||{};document.getElementById('folder').textContent='Folder: '+(s.output_root||'—')+' · Źródła: '+(s.sources_count||0)+' ('+(s.sources_enabled||0)+' włącz.) · Baza: '+(s.database_count||0)+' artykułów · GET: '+(n.requests||0)+' · cache: '+(n.cache_hits||0)+' · cleanup: '+(cl.removed||0)+' · platforma: '+(s.platform||'—')+' · wersja '+(s.version||'');const od=document.getElementById('outdir');if(!od.value||(s.storage_mode==='saf'&&od.value.startsWith('Android/SAF:'))){od.value=s.output_root||'';}const fh=document.getElementById('folderhelp');if(s.platform==='android-native'){fh.textContent=s.storage_mode==='android-saf'?'Aktywny jest systemowy folder Android/SAF — archiwum jest synchronizowane do wybranego katalogu.':'Naciśnij WYBIERZ FOLDER — Android otworzy systemowy wybór pamięci telefonu lub karty SD.';}else if(s.platform==='android-termux'){fh.textContent=s.storage_mode==='saf'?'Aktywny jest systemowy folder Android/SAF.':'Naciśnij WYBIERZ FOLDER — Android otworzy wybór katalogu.';}else{fh.textContent='Naciśnij WYBIERZ FOLDER — otworzy się normalne okno Windows.';}const links=s.last_test_links||[];document.getElementById('links').innerHTML=links.length?links.map(x=>'<a href="'+esc(x.url)+'" target="_blank">['+x.score+'] '+esc(x.text||x.url)+'</a>').join(''):'—';const fs=s.recent_failures||[];document.getElementById('failures').innerHTML=fs.length?fs.slice().reverse().map(x=>'<div class="fail"><b>'+esc(x.reason)+'</b><br>'+esc(x.url)+'</div>').join(''):'—'}catch(e){document.getElementById('status').textContent='Błąd połączenia z programem'}}
for(const id of ['sourcename','url','outdir','selector','strict','skip','mode','template','maxa','maxp','retry','minc','pause']){document.addEventListener('input',e=>{if(e.target&&e.target.id===id)scheduleConfigSave()});document.addEventListener('change',e=>{if(e.target&&e.target.id===id)scheduleConfigSave()})}
(async()=>{await loadConfig();await loadSources();await refresh()})();setInterval(()=>{refresh();if(!document.hidden)loadSources()},5000);
</script></body></html>'''


def settings_from_json(d):
    return Settings(
        list_url=str(d.get("list_url", "")).strip(),
        selector=str(d.get("selector", "")).strip(),
        strict_selector=bool(d.get("strict_selector", True)),
        max_articles=max(1, min(5000, int(d.get("max_articles", 100) or 100))),
        max_list_pages=max(1, min(1000, int(d.get("max_list_pages", 100) or 100))),
        list_mode=(str(d.get("list_mode", "auto") or "auto").lower() if str(d.get("list_mode", "auto") or "auto").lower() in {"auto","pages","scroll","single"} else "auto"),
        page_template=str(d.get("page_template", "") or "").strip(),
        retry=max(0, min(1, int(d.get("retry", 1) or 0))),
        min_chars=max(50, min(2_000_000, int(d.get("min_chars", 300) or 300))),
        pause=max(5.0, min(120.0, float(d.get("pause", 8.0) or 8.0))),
        skip_processed=bool(d.get("skip_processed", True)),
        output_dir=str(d.get("output_dir", "") or "").strip(),
        source_id=max(0, int(d.get("source_id", d.get("id", 0)) or 0)),
        source_name=str(d.get("source_name", d.get("name", "")) or "").strip()[:160],
    )


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, content, ctype="application/json; charset=utf-8"):
        if isinstance(content, str):
            content = content.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(content)
            return True
        except Exception as e:
            # Chrome/Edge potrafi anulować lokalne /api/status przy odświeżeniu,
            # zmianie karty lub zamknięciu panelu. To normalne i nie ma sensu
            # drukować wielostronicowego tracebacku WinError 10053/10054.
            if _is_client_disconnect(e):
                return False
            raise

    def _json(self, obj, code=200):
        return self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _read_json(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        if n > 1_000_000:
            raise ValueError("Za duże żądanie")
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw.decode("utf-8")) if raw else {}

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            return self._send(200, INDEX_HTML, "text/html; charset=utf-8")
        if path == "/api/status":
            return self._json(DL.snapshot())
        if path == "/api/config":
            return self._json({"ok": True, "config": DL.load_form_config()})
        if path == "/api/sources":
            return self._json({"ok": True, "sources": DL.list_sources()})
        if path == "/api/folders":
            try:
                q = dict(parse_qsl(urlsplit(self.path).query, keep_blank_values=True))
                return self._json({"ok": True, **DL.list_output_dirs(q.get("path", ""))})
            except Exception as e:
                return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        if path.startswith("/archive/"):
            rel = unquote(path[len("/archive/"):]) or "index.html"
            target = (DL.output_root / rel).resolve()
            root = DL.output_root.resolve()
            if root not in target.parents and target != root:
                return self._send(403, "Forbidden", "text/plain; charset=utf-8")
            if not target.exists() or not target.is_file():
                return self._send(404, "Brak pliku", "text/plain; charset=utf-8")
            ext = target.suffix.lower()
            ctype = {
                ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
                ".json": "application/json; charset=utf-8", ".csv": "text/csv; charset=utf-8",
            }.get(ext, "text/plain; charset=utf-8")
            return self._send(200, target.read_bytes(), ctype)
        return self._send(404, "Not found", "text/plain; charset=utf-8")

    def do_POST(self):
        try:
            d = self._read_json()
            if self.path == "/api/config":
                return self._json({"ok": True, "config": DL.save_form_config(d)})
            if self.path == "/api/sources/save":
                return self._json({"ok": True, "source": DL.save_source(d)})
            if self.path == "/api/sources/delete":
                return self._json({"ok": True, "deleted": DL.delete_source(d.get("id"))})
            if self.path == "/api/sources/toggle":
                return self._json({"ok": True, "source": DL.toggle_source(d.get("id"), d.get("enabled"))})
            if self.path == "/api/test":
                cfg = settings_from_json(d)
                if not cfg.list_url:
                    return self._json({"ok": False, "error": "Brak URL"}, 400)
                links = DL.test_list(cfg)
                return self._json({"ok": True, "count": len(links), "links": links[:100]})
            if self.path == "/api/start-all":
                if not DL.start_all():
                    return self._json({"ok": False, "error": "Program już pracuje"}, 409)
                return self._json({"ok": True})
            if self.path == "/api/start":
                cfg = settings_from_json(d)
                if not cfg.list_url:
                    return self._json({"ok": False, "error": "Brak URL"}, 400)
                if not DL.start(cfg):
                    return self._json({"ok": False, "error": "Program już pracuje"}, 409)
                return self._json({"ok": True})
            if self.path == "/api/output":
                path = DL.set_output_root(str(d.get("output_dir", "") or ""))
                return self._json({"ok": True, "output_root": path})
            if self.path == "/api/pick-output":
                result = DL.pick_output_dir()
                return self._json({"ok": True, **result})
            if self.path == "/api/default-output":
                path = DL.set_default_output_root()
                return self._json({"ok": True, "output_root": path})
            if self.path == "/api/stop":
                DL.stop()
                return self._json({"ok": True})
            return self._json({"ok": False, "error": "Nieznana akcja"}, 404)
        except OperationCancelled:
            DL._set_status("Operacja zatrzymana.")
            return self._json({"ok": False, "cancelled": True, "error": "Operacja zatrzymana"}, 200)
        except Exception as e:
            # Jeśli klient zdążył zamknąć połączenie, nie próbujemy wysyłać
            # kolejnej odpowiedzi błędu do nieistniejącego gniazda.
            if _is_client_disconnect(e):
                return None
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)


def create_server_on_free_port(host=HOST, start_port=PORT, attempts=100):
    last_error = None
    for port in range(int(start_port), int(start_port) + max(1, int(attempts))):
        try:
            server = ThreadingHTTPServer((host, port), Handler)
            return server, int(server.server_address[1])
        except OSError as e:
            last_error = e
            continue
    try:
        server = ThreadingHTTPServer((host, 0), Handler)
        return server, int(server.server_address[1])
    except OSError:
        if last_error:
            raise last_error
        raise


def main():
    server, actual_port = create_server_on_free_port(HOST, PORT)
    panel_url = f"http://{HOST}:{actual_port}"
    print("\nPRZEWIJAK — ARTYKUŁY / V15.2 — MULTISOURCE AUTO NEW")
    print("Wersja:", APP_VERSION)
    print("Platforma:", DL.runtime_platform)
    print("Panel:", panel_url)
    print("Folder:", DL.output_display())
    print("Baza duplikatów:", DL.db_path, f"({DL._db_count()} artykułów)")
    print("Cleanup techniczny:", DL.cleanup_stats)
    print("Zatrzymanie programu: Ctrl+C\n")
    try:
        if DL.runtime_platform == "android-termux":
            opener = shutil.which("termux-open-url")
            if opener:
                threading.Timer(0.8, lambda: subprocess.Popen([opener, panel_url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)).start()
            else:
                print("Android: otwórz w Chrome/Firefox:", panel_url)
        else:
            threading.Timer(0.8, lambda: webbrowser.open(panel_url)).start()
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        DL.stop()
        server.server_close()


if __name__ == "__main__":
    main()
