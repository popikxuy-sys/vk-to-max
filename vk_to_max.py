# -*- coding: utf-8 -*-
"""
Перенос новых записей со стены ВК в канал MAX.
Источник: https://vk.ru/molsovet_kurkino
Приёмник: https://max.ru/channel_molsovet_kurkino

Работает в двух режимах:
  python vk_to_max.py            — непрерывно (на своём ПК)
  python vk_to_max.py --once     — один заход: собрать события и выйти
                                   (для запуска по расписанию в GitHub Actions)

Токены берутся из переменных окружения:
  VK_TOKEN  — ключ доступа сообщества molsovet_kurkino
  MAX_TOKEN — токен бота MAX
Локально их можно положить в файл secrets_local.py (он в .gitignore):
  VK_TOKEN = "vk1.a...."
  MAX_TOKEN = "f9LHodD0c...."
"""
import argparse
import json
import os
import sys
import time
from collections import deque

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

VK_TOKEN = os.environ.get("VK_TOKEN", "").strip()
MAX_TOKEN = os.environ.get("MAX_TOKEN", "").strip()
if not VK_TOKEN or not MAX_TOKEN:
    try:
        import secrets_local

        VK_TOKEN = VK_TOKEN or getattr(secrets_local, "VK_TOKEN", "").strip()
        MAX_TOKEN = MAX_TOKEN or getattr(secrets_local, "MAX_TOKEN", "").strip()
    except ImportError:
        pass

VK_DOMAIN = "molsovet_kurkino"
VK_API_VERSION = "5.131"
MAX_CHAT_ID = -81162970548366
MAX_API_BASE = "https://platform-api2.max.ru"
CHANNEL_NAME_MARKER = "молодежный совет"

STATE_FILE = os.path.join(".state", "state.json")
POLL_INTERVAL_SEC = 60

sent_posts = deque(maxlen=200)


def load_state():
    """Возвращает {"last_post_id": int|None, "ts": str|None}."""
    if not os.path.exists(STATE_FILE):
        return {"last_post_id": None, "ts": None}
    with open(STATE_FILE, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    last_id = data.get("last_post_id")
    ts = data.get("ts")
    return {"last_post_id": int(last_id) if last_id is not None else None,
            "ts": str(ts) if ts is not None else None}


def save_state(last_post_id, ts):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    data = {"last_post_id": last_post_id, "ts": ts}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


def load_last_post_id():
    return load_state()["last_post_id"]


def save_last_post_id(post_id):
    st = load_state()
    save_state(post_id, st["ts"])


def check_chat_info(chat_id):
    try:
        res = requests.get(f"{MAX_API_BASE}/chats/{chat_id}",
                           headers={"Authorization": MAX_TOKEN}, timeout=15, verify=False)
        if res.status_code == 200:
            return res.json()
    except Exception:
        pass
    return None


def check_bot_role(chat_id):
    """Возвращает (is_admin, is_owner) или (None, None), если проверить не удалось."""
    try:
        r = requests.get(f"{MAX_API_BASE}/chats/{chat_id}/members/me",
                         headers={"Authorization": MAX_TOKEN}, timeout=15, verify=False)
        if r.status_code == 200:
            j = r.json()
            return bool(j.get("is_admin")), bool(j.get("is_owner"))
    except Exception:
        pass
    return None, None


def get_max_channel_id():
    info = check_chat_info(MAX_CHAT_ID)
    if not info:
        print(f"[Ошибка MAX] Канал {MAX_CHAT_ID} недоступен: проверьте, что бот — администратор канала.")
        return None
    title = info.get("title") or ""
    if CHANNEL_NAME_MARKER not in title.lower():
        print(f"[Остановка] Канал {MAX_CHAT_ID} называется «{title}» — это не целевой канал.")
        return None
    is_admin, is_owner = check_bot_role(MAX_CHAT_ID)
    if is_admin is False and is_owner is False:
        print(f"[Остановка] Бот не администратор канала «{title}» (ID: {MAX_CHAT_ID}).")
        print("Добавьте бота в администраторы канала MAX с правом публикации сообщений, иначе посты теряются.")
        return None
    print(f"MAX: канал «{title}» (ID: {MAX_CHAT_ID}), бот — администратор")
    return MAX_CHAT_ID


def vk_request(method, params):
    params.update({"access_token": VK_TOKEN, "v": VK_API_VERSION})
    res = requests.get(f"https://api.vk.com/method/{method}", params=params,
                       timeout=25, verify=False).json()
    if "error" in res:
        err = res["error"]
        raise RuntimeError(f"VK #{err.get('error_code')}: {err.get('error_msg')}")
    return res.get("response")


def get_group_id():
    group = vk_request("groups.getById", {"group_id": VK_DOMAIN})[0]
    print(f"ВК: группа «{group.get('name')}» (ID: {group['id']})")
    return group["id"]


def get_long_poll_server(group_id):
    try:
        vk_request("groups.setLongPollSettings", {
            "group_id": group_id, "enabled": 1,
            "api_version": VK_API_VERSION, "wall_post_new": 1})
        return vk_request("groups.getLongPollServer", {"group_id": group_id})
    except RuntimeError as e:
        print(f"Long Poll недоступен: {e}")
        return None


def extract_photo_url(photo_obj):
    sizes = photo_obj.get("sizes", [])
    if sizes:
        best = max(sizes, key=lambda x: x.get("width", 0) * x.get("height", 0))
        return best.get("url") or best.get("src")
    for key in ("photo_2560", "photo_1280", "photo_807", "photo_604", "photo_130",
                "src_xxbig", "src_xbig", "src_big"):
        if photo_obj.get(key):
            return photo_obj.get(key)
    return None


def upload_image_to_max(photo_url):
    headers = {"Authorization": MAX_TOKEN}
    try:
        img = requests.get(photo_url, timeout=25, verify=False)
        if img.status_code != 200:
            return {"type": "image", "payload": {"url": photo_url}}
        init = requests.post(f"{MAX_API_BASE}/uploads", params={"type": "image"},
                             headers=headers, timeout=20, verify=False)
        if init.status_code == 200:
            upload_url = (init.json() or {}).get("url")
            if upload_url:
                up = requests.post(upload_url,
                                   files={"data": ("photo.jpg", img.content, "image/jpeg")},
                                   timeout=40, verify=False)
                if up.status_code == 200:
                    j = up.json()
                    if "token" in j:
                        return {"type": "image", "payload": {"token": j["token"]}}
                    if isinstance(j.get("photos"), dict):
                        for _, pd in j["photos"].items():
                            if isinstance(pd, dict) and "token" in pd:
                                return {"type": "image", "payload": {"token": pd["token"]}}
    except Exception as e:
        print(f"Предупреждение при загрузке фото ({e}); использую прямую ссылку.")
    return {"type": "image", "payload": {"url": photo_url}}


def parse_post_content(post):
    parts = []
    if post.get("text"):
        parts.append(post["text"].strip())
    attachments = list(post.get("attachments", []))
    for copy_item in post.get("copy_history", []):
        if copy_item.get("text"):
            parts.append(copy_item["text"].strip())
        attachments.extend(copy_item.get("attachments", []))

    max_atts, extra = [], []
    for att in attachments:
        at = att.get("type")
        if at == "photo":
            url = extract_photo_url(att.get("photo", {}))
            if url and len(max_atts) < 10:
                print(" - фото: загружаю на сервер MAX...")
                max_atts.append(upload_image_to_max(url))
        elif at == "poll":
            poll = att.get("poll", {})
            extra.append(f"\n\n📊 Опрос: {poll.get('question', 'Опрос')}")
            for ans in poll.get("answers", []):
                extra.append(f"\n• {ans.get('text')}")
        elif at == "video":
            v = att.get("video", {})
            if v.get("owner_id") and v.get("id"):
                extra.append(f"\n\n🎬 {v.get('title', 'Видеозапись')}: "
                             f"https://vk.com/video{v['owner_id']}_{v['id']}")
            if not max_atts:
                thumb = extract_photo_url(v)
                if thumb:
                    max_atts.append(upload_image_to_max(thumb))
        elif at == "link":
            link = att.get("link", {})
            if link.get("url"):
                extra.append(f"\n\n🔗 {link.get('title', 'Ссылка')}: {link['url']}")
        elif at == "doc":
            d = att.get("doc", {})
            if d.get("url"):
                extra.append(f"\n\n📎 {d.get('title', 'Документ')}: {d['url']}")

    base_text = "\n\n".join([p for p in parts if p])
    return (base_text + "".join(extra)).strip()[:4000], max_atts


def send_to_max(chat_id, text, max_attachments):
    headers = {"Authorization": MAX_TOKEN, "Content-Type": "application/json"}
    payload = {}
    if text.strip():
        payload["text"] = text
    if max_attachments:
        payload["attachments"] = max_attachments
    if not payload:
        payload["text"] = "Новая запись в сообществе ВКонтакте"
    try:
        if max_attachments:
            time.sleep(1.5)
        resp = requests.post(f"{MAX_API_BASE}/messages", params={"chat_id": chat_id},
                             json=payload, headers=headers, timeout=30, verify=False)
        if resp.status_code == 200:
            print("-> Опубликовано в MAX.")
            return True
        print(f"-> Ошибка отправки в MAX ({resp.status_code}): {resp.text}")
    except Exception as e:
        print(f"-> Ошибка запроса в MAX: {e}")
    return False


def publish_post(post, chat_id, last_id):
    pid = post.get("id")
    ptype = post.get("post_type")
    if ptype != "post" or pid is None:
        print(f"[Пропуск] пост id={pid}, post_type={ptype!r} — не обычная запись")
        return False
    if last_id is not None and pid <= last_id:
        print(f"[Пропуск] пост id={pid} уже публиковался (last_id={last_id})")
        return False
    if pid in sent_posts:
        return False
    print(f"\n[ВК] Новый пост ID {pid}")
    text, atts = parse_post_content(post)
    print(f" - текст: {len(text)} симв., вложений: {len(atts)}")
    if send_to_max(chat_id, text, atts):
        sent_posts.append(pid)
        save_last_post_id(pid)
        return True
    return False


def process_events(events, chat_id, skip):
    """Обрабатывает пачку событий. skip=True — пачку только запоминаем (первый запуск)."""
    print(f"[LP] событий в пачке: {len(events)} | типы: {[ev.get('type') for ev in events]}")
    posts = [ev.get("object", {}) for ev in events if ev.get("type") == "wall_post_new"]
    if not posts:
        return 0
    if skip:
        ids = [p.get("id") for p in posts if p.get("id") is not None]
        if ids:
            save_last_post_id(max(ids))
            print(f"Первый запуск: старые записи ({len(ids)}) отмечены как опубликованные.")
        return 0
    published = 0
    for post in sorted(posts, key=lambda p: p.get("id", 0)):
        if publish_post(post, chat_id, load_last_post_id()):
            published += 1
    return published


def run_once(minutes_budget):
    """Один заход: подключиться к Long Poll, забрать накопившиеся события, выйти."""
    chat_id = get_max_channel_id()
    if not chat_id:
        return 1
    group_id = get_group_id()
    lp = get_long_poll_server(group_id)
    if not lp:
        return 1

    try:
        state = load_state()
    except Exception as e:
        print(f"[Остановка] Файл состояния {STATE_FILE} повреждён: {e}")
        print("Удалите его или исправьте JSON, иначе можно повторно опубликовать старые посты.")
        return 1
    saved_ts = state["ts"]
    saved_id = state["last_post_id"]
    server, key = lp["server"], lp["key"]
    # Если сохранена позиция — продолжаем с неё, иначе берём текущую.
    ts = saved_ts if saved_ts else lp["ts"]
    first_run = saved_id is None and not saved_ts
    deadline = time.time() + minutes_budget * 60
    total = 0
    print(f"Long Poll запущен, окно {minutes_budget} мин (стартовый ts={ts}).")

    while time.time() < deadline:
        try:
            res = requests.get(f"{server}?act=a_check&key={key}&ts={ts}&wait=25",
                               timeout=40, verify=False).json()
        except Exception as e:
            print(f"Сетевая ошибка a_check: {e}")
            time.sleep(5)
            continue
        if "failed" in res:
            code = res.get("failed")
            print(f"Long Poll: failed={code}, переподключаюсь.")
            lp = get_long_poll_server(group_id)
            if not lp:
                break
            server, key = lp["server"], lp["key"]
            if code in (1, 3):
                # очередь устарела: продолжаем с текущей позиции
                print("Позиция очереди устарела, продолжаю с текущей (часть событий могла не дойти).")
                ts = lp["ts"]
                save_state(load_last_post_id(), ts)
            continue
        ts = str(res.get("ts", ts))
        events = res.get("updates", [])
        if events:
            total += process_events(events, chat_id, skip=first_run)
            first_run = False
        elif first_run:
            first_run = False
            if load_last_post_id() is None:
                save_state(0, ts)
        save_state(load_last_post_id(), ts)

    print(f"Готово: опубликовано новых постов — {total}.")
    return 0


def run_forever():
    chat_id = get_max_channel_id()
    if not chat_id:
        return 1
    group_id = get_group_id()
    lp = get_long_poll_server(group_id)
    if not lp:
        return 1
    try:
        state = load_state()
    except Exception as e:
        print(f"[Остановка] Файл состояния {STATE_FILE} повреждён: {e}")
        return 1
    server, key = lp["server"], lp["key"]
    ts = state["ts"] if state["ts"] else lp["ts"]
    first_run = state["last_post_id"] is None and not state["ts"]
    print("Ожидание новых записей на стене (Ctrl+C для остановки)...")
    while True:
        try:
            res = requests.get(f"{server}?act=a_check&key={key}&ts={ts}&wait=25",
                               timeout=40, verify=False).json()
            if "failed" in res:
                lp = get_long_poll_server(group_id)
                if not lp:
                    time.sleep(20)
                    continue
                server, key = lp["server"], lp["key"]
                if res.get("failed") in (1, 3):
                    ts = lp["ts"]
                    save_state(load_last_post_id(), ts)
                continue
            ts = str(res.get("ts", ts))
            events = res.get("updates", [])
            if events:
                process_events(events, chat_id, skip=first_run)
                first_run = False
            save_state(load_last_post_id(), ts)
        except KeyboardInterrupt:
            print("\nОстановлено пользователем.")
            return 0
        except Exception as e:
            print(f"Переподключение ({e})...")
            time.sleep(5)


def main():
    if not VK_TOKEN or not MAX_TOKEN:
        print("[Ошибка] Не заданы токены.")
        print("В GitHub они берутся из секретов репозитория VK_TOKEN и MAX_TOKEN.")
        print("Локально положите их в файл secrets_local.py рядом со скриптом.")
        return 1

    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true",
                        help="один заход и выход (для запуска по расписанию)")
    parser.add_argument("--wait", type=int, default=5,
                        help="сколько минут слушать события в режиме --once")
    args = parser.parse_args()

    if args.once:
        return run_once(args.wait)
    return run_forever()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
        sys.exit(0)
    except Exception as e:
        print(f"Критическая ошибка: {e}")
        sys.exit(1)
