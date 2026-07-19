# -*- coding: utf-8 -*-
"""
yandex_disk.py — вход в Яндекс.Диск по аккаунту (OAuth) и работа с папками
через официальный REST API. Только стандартная библиотека Python (без pip-зависимостей).

Что умеет:
  - oauth_login()      — вход в аккаунт: открывает браузер, ловит ответ на localhost,
                         меняет код на токен, сохраняет его в yandex_config.json;
  - account_login()    — логин пользователя (для подписи «Вошли: …»);
  - list_images(src)   — список картинок во ВХОДНОЙ папке (публичная ссылка ИЛИ путь /Папка);
  - download(item, dst)— скачать один файл из списка;
  - ensure_folder(p)   — создать выходную папку на Диске (с родителями);
  - upload(local, p)   — залить файл в выходную папку;
  - publish(p)         — опубликовать папку и вернуть публичную ссылку.

Регистрация приложения (один раз, ~2 мин): https://oauth.yandex.ru/client/new
  • Платформа: «Веб-сервисы», Redirect URI:  http://localhost:8123
  • Доступы (Яндекс.Диск REST API): чтение + запись + инфо; плюс «Доступ к логину».
  • Скопировать ClientID и Client secret в программу.
"""

import os
import json
import time
import base64
import urllib.parse
import urllib.request
import urllib.error
import webbrowser
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

API = "https://cloud-api.yandex.net/v1/disk"
OAUTH_AUTH = "https://oauth.yandex.ru/authorize"
OAUTH_TOKEN = "https://oauth.yandex.ru/token"
LOGIN_INFO = "https://login.yandex.ru/info?format=json"
REDIRECT_PORT = 8123
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}"
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yandex_config.json")
IMG_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp")


class YaError(Exception):
    """Понятная человеку ошибка работы с Яндекс.Диском."""


# ---------------- конфиг (client_id/secret/token) ----------------
def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def is_logged_in(cfg=None):
    cfg = cfg or load_config()
    return bool(cfg.get("access_token"))


# ---------------- низкоуровневые HTTP-запросы ----------------
def _request(method, url, headers=None, data=None, timeout=60):
    req = urllib.request.Request(url, method=method, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise YaError(f"HTTP {e.code} {method} {url.split('?')[0]} — {body[:300]}")
    except urllib.error.URLError as e:
        raise YaError(f"Нет связи с Яндексом: {e.reason}")


def _auth_headers(token):
    return {"Authorization": f"OAuth {token}", "Accept": "application/json"}


def _get_json(url, headers=None):
    _, body = _request("GET", url, headers=headers)
    return json.loads(body.decode("utf-8"))


# ---------------- OAuth: вход в аккаунт ----------------
class _CodeHandler(BaseHTTPRequestHandler):
    code = None
    error = None

    def do_GET(self):
        q = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(q)
        _CodeHandler.code = (params.get("code") or [None])[0]
        _CodeHandler.error = (params.get("error") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = ("Готово! Можно вернуться в программу GreenKey."
               if _CodeHandler.code else "Ошибка входа. Вернитесь в программу.")
        self.wfile.write(f"<html><body style='font:20px sans-serif;text-align:center;"
                         f"padding:60px'>{msg}</body></html>".encode("utf-8"))

    def log_message(self, *a):
        pass   # не спамить в консоль


def oauth_login(client_id, client_secret, timeout=180):
    """Полный вход: браузер -> код -> токен. Сохраняет токен в конфиг. Возвращает cfg."""
    client_id = (client_id or "").strip()
    client_secret = (client_secret or "").strip()
    if not client_id or not client_secret:
        raise YaError("Укажите ClientID и Client secret приложения Яндекса.")

    _CodeHandler.code = _CodeHandler.error = None
    try:
        server = HTTPServer(("localhost", REDIRECT_PORT), _CodeHandler)
    except OSError as e:
        raise YaError(f"Не открыть порт {REDIRECT_PORT} для входа: {e}")
    server.timeout = timeout

    url = (f"{OAUTH_AUTH}?response_type=code&client_id={urllib.parse.quote(client_id)}"
           f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}&force_confirm=yes")
    webbrowser.open(url)

    # ждём один редирект с кодом (в отдельном потоке — с общим таймаутом)
    deadline = time.time() + timeout
    while _CodeHandler.code is None and _CodeHandler.error is None and time.time() < deadline:
        server.handle_request()
    server.server_close()

    if _CodeHandler.error:
        raise YaError(f"Вход отклонён: {_CodeHandler.error}")
    if not _CodeHandler.code:
        raise YaError("Вход не завершён (истекло время ожидания).")

    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": _CodeHandler.code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT_URI,
    }).encode("utf-8")
    _, body = _request("POST", OAUTH_TOKEN, data=data,
                       headers={"Content-Type": "application/x-www-form-urlencoded"})
    tok = json.loads(body.decode("utf-8"))
    if "access_token" not in tok:
        raise YaError(f"Не получен токен: {tok}")

    cfg = load_config()
    cfg.update({
        "client_id": client_id,
        "client_secret": client_secret,
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token", ""),
        "expires_at": time.time() + int(tok.get("expires_in", 0)),
    })
    save_config(cfg)
    return cfg


def refresh_if_needed(cfg):
    """Обновить access_token по refresh_token, если срок близок. Возвращает cfg."""
    if not cfg.get("refresh_token"):
        return cfg
    if time.time() < cfg.get("expires_at", 0) - 120:
        return cfg
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": cfg["refresh_token"],
        "client_id": cfg.get("client_id", ""),
        "client_secret": cfg.get("client_secret", ""),
    }).encode("utf-8")
    try:
        _, body = _request("POST", OAUTH_TOKEN, data=data,
                           headers={"Content-Type": "application/x-www-form-urlencoded"})
        tok = json.loads(body.decode("utf-8"))
        if "access_token" in tok:
            cfg["access_token"] = tok["access_token"]
            cfg["refresh_token"] = tok.get("refresh_token", cfg["refresh_token"])
            cfg["expires_at"] = time.time() + int(tok.get("expires_in", 0))
            save_config(cfg)
    except YaError:
        pass   # если не вышло — попробуем работать со старым токеном
    return cfg


def account_login(cfg=None):
    """Логин пользователя для подписи. Пусто, если не удалось."""
    cfg = cfg or load_config()
    if not cfg.get("access_token"):
        return ""
    try:
        info = _get_json(LOGIN_INFO, headers=_auth_headers(cfg["access_token"]))
        return info.get("display_name") or info.get("login") or ""
    except YaError:
        return ""


# ---------------- разбор «вход»: ссылка или путь ----------------
def parse_source(text):
    """Вернуть ('public', url) для публичной ссылки или ('path', '/Папка') для пути на Диске."""
    s = (text or "").strip()
    if not s:
        raise YaError("Пустая папка-источник.")
    if s.startswith("http://") or s.startswith("https://"):
        return ("public", s)
    # путь на своём Диске: нормализуем к виду disk:/...
    s = s.replace("\\", "/")
    if s.startswith("disk:/"):
        return ("path", s)
    if not s.startswith("/"):
        s = "/" + s
    return ("path", "disk:" + s)


def norm_dest(text):
    """Нормализовать путь выходной папки к disk:/... (принимает и ссылку-владельца как путь нельзя)."""
    s = (text or "").strip().replace("\\", "/")
    if not s:
        raise YaError("Пустая папка-выход.")
    if s.startswith("http://") or s.startswith("https://"):
        raise YaError("Для ВЫХОДА укажите путь на Диске (напр. /Готово), а не ссылку — "
                      "в публичную ссылку записывать нельзя.")
    if s.startswith("disk:/"):
        return s
    if not s.startswith("/"):
        s = "/" + s
    return "disk:" + s


# ---------------- листинг входной папки ----------------
def _is_img(name):
    return name.lower().endswith(IMG_EXT)


def list_images(source, cfg=None):
    """Список картинок во входной папке. Возвращает список dict: {name, download}.
    source: ('public', url) или ('path', 'disk:/...')."""
    cfg = cfg or load_config()
    kind, val = source
    items = []
    limit, offset = 200, 0
    while True:
        if kind == "public":
            url = (f"{API}/public/resources?public_key={urllib.parse.quote(val, safe='')}"
                   f"&limit={limit}&offset={offset}"
                   f"&fields=_embedded.items.name,_embedded.items.type,_embedded.items.file,"
                   f"_embedded.items.path,_embedded.total")
            data = _get_json(url)
        else:
            if not cfg.get("access_token"):
                raise YaError("Для пути на Диске нужен вход в аккаунт.")
            url = (f"{API}/resources?path={urllib.parse.quote(val, safe='')}"
                   f"&limit={limit}&offset={offset}"
                   f"&fields=_embedded.items.name,_embedded.items.type,_embedded.items.file,"
                   f"_embedded.items.path,_embedded.total")
            data = _get_json(url, headers=_auth_headers(cfg["access_token"]))
        emb = data.get("_embedded") or {}
        batch = emb.get("items") or []
        for it in batch:
            if it.get("type") == "file" and _is_img(it.get("name", "")):
                items.append({"name": it["name"],
                              "file": it.get("file"),
                              "path": it.get("path"),
                              "public_key": val if kind == "public" else None})
        total = emb.get("total", len(batch))
        offset += limit
        if offset >= total or not batch:
            break
    return items


def download(item, dest_path, cfg=None):
    """Скачать один файл (item из list_images) в dest_path."""
    cfg = cfg or load_config()
    href = item.get("file")
    headers = None
    if not href:
        # запросить прямую ссылку на скачивание
        if item.get("public_key"):
            url = (f"{API}/public/resources/download?public_key="
                   f"{urllib.parse.quote(item['public_key'], safe='')}"
                   f"&path={urllib.parse.quote(item.get('path', ''), safe='')}")
            href = _get_json(url).get("href")
        else:
            url = (f"{API}/resources/download?path="
                   f"{urllib.parse.quote(item.get('path', ''), safe='')}")
            href = _get_json(url, headers=_auth_headers(cfg["access_token"])).get("href")
    # сам файл-хост обычно без авторизации, но для private download href добавим токен-заголовок безопасно
    _, body = _request("GET", href, headers=headers)
    with open(dest_path, "wb") as f:
        f.write(body)


# ---------------- выходная папка: создать / залить / опубликовать ----------------
def ensure_folder(disk_path, cfg=None):
    """Создать папку disk:/a/b/c (с родителями). Игнорирует «уже существует»."""
    cfg = cfg or load_config()
    token = cfg["access_token"]
    # disk:/A/B/C -> создаём последовательно /A, /A/B, /A/B/C
    rel = disk_path.split("disk:", 1)[-1]
    parts = [p for p in rel.split("/") if p]
    cur = "disk:"
    for p in parts:
        cur = cur + "/" + p
        url = f"{API}/resources?path={urllib.parse.quote(cur, safe='')}"
        try:
            _request("PUT", url, headers=_auth_headers(token))
        except YaError as e:
            if "HTTP 409" not in str(e):   # 409 = уже существует, это ок
                raise


def upload(local_path, disk_path, cfg=None, overwrite=True):
    """Залить локальный файл в disk_path (полный путь с именем файла)."""
    cfg = cfg or load_config()
    token = cfg["access_token"]
    url = (f"{API}/resources/upload?path={urllib.parse.quote(disk_path, safe='')}"
           f"&overwrite={'true' if overwrite else 'false'}")
    href = _get_json(url, headers=_auth_headers(token)).get("href")
    if not href:
        raise YaError("Не получен адрес загрузки.")
    with open(local_path, "rb") as f:
        _request("PUT", href, data=f.read())


def publish(disk_path, cfg=None):
    """Опубликовать папку и вернуть публичную ссылку (или '')."""
    cfg = cfg or load_config()
    token = cfg["access_token"]
    try:
        _request("PUT", f"{API}/resources/publish?path={urllib.parse.quote(disk_path, safe='')}",
                 headers=_auth_headers(token))
    except YaError:
        pass
    try:
        info = _get_json(f"{API}/resources?path={urllib.parse.quote(disk_path, safe='')}"
                         f"&fields=public_url", headers=_auth_headers(token))
        return info.get("public_url", "")
    except YaError:
        return ""


def list_dir_names(disk_path, cfg=None):
    """Имена подпапок в disk_path (для нумерации датированной папки). [] если папки нет."""
    cfg = cfg or load_config()
    try:
        url = (f"{API}/resources?path={urllib.parse.quote(disk_path, safe='')}"
               f"&limit=1000&fields=_embedded.items.name,_embedded.items.type")
        data = _get_json(url, headers=_auth_headers(cfg["access_token"]))
        emb = data.get("_embedded") or {}
        return [it["name"] for it in emb.get("items", []) if it.get("type") == "dir"]
    except YaError:
        return []
