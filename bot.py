"""
Telegram-бот расписания школы №85 + рассылка от админа.
Полностью готов к деплою на Render (токен берется из Environment Variables).
"""
import asyncio, hashlib, io, json, logging, os, re, shutil, time
from datetime import date, timedelta
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import aiohttp
import pdfplumber
import pypdfium2 as pdfium
from aiohttp import web
from aiogram import Bot, BaseMiddleware, Dispatcher, F, Router
from aiogram.types import TelegramObject
from aiogram.filters import Command, CommandStart
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup)
from PIL import Image

# ============== настройки (из переменных окружения) ==============
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))

PAGE_URL = os.environ.get("PAGE_URL", "https://школа85.екатеринбург.рф/?section_id=13")
EXTRA_PDF_URLS = os.environ.get("EXTRA_PDF_URLS", "").split()
CHANNEL = os.environ.get("CHANNEL", "@kirafear")
CHANNEL_URL = os.environ.get("CHANNEL_URL", "https://t.me/kirafear")

PREFS_FILE = "prefs.json"
CHECK_EVERY = 1800  # проверка сайта раз в 30 минут
RES = 200  # качество картинки (dpi)
CACHE_DIR = "cache"
CACHE_TTL = 24 * 3600

logging.basicConfig(level=logging.INFO)
router = Router()

# ======================= настройки пользователей =======================
try:
    prefs = json.load(open(PREFS_FILE, encoding="utf-8"))
    for u in prefs.values():
        u.setdefault("welcomed", True)
except Exception:
    prefs = {}


def save_prefs():
    try:
        json.dump(prefs, open(PREFS_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception as e:
        logging.warning("save prefs failed: %s", e)


def user(uid):
    return prefs.setdefault(str(uid), {"cls": None, "notify": True, "welcomed": False})


# ======================= проверка подписки =======================
_sub_cache: dict[int, tuple[float, bool]] = {}


async def is_subscribed(bot: Bot, uid: int) -> bool:
    now = time.time()
    if uid in _sub_cache and now - _sub_cache[uid][0] < 60:
        return _sub_cache[uid][1]
    try:
        m = await bot.get_chat_member(chat_id=CHANNEL, user_id=uid)
        ok = m.status in ("member", "administrator", "creator")
    except Exception as e:
        logging.warning("subscription check failed for %s: %s", uid, e)
        ok = False
    _sub_cache[uid] = (now, ok)
    return ok


SUB_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="📣 Подписаться на канал", url=CHANNEL_URL)],
    [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub")],
])


async def require_sub(msg: Message, bot: Bot) -> bool:
    if await is_subscribed(bot, msg.from_user.id):
        return True
    await msg.answer(
        f"Чтобы пользоваться ботом, подпишись на канал {CHANNEL_URL}\n"
        f"После подписки нажми «✅ Я подписался».",
        reply_markup=SUB_KB,
    )
    return False


class SubscriptionMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        if isinstance(event, Message) and event.text and event.text.startswith("/start"):
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and event.data == "check_sub":
            return await handler(event, data)

        uid = None
        if isinstance(event, (Message, CallbackQuery)) and event.from_user:
            uid = event.from_user.id

        if uid and uid == ADMIN_ID:
            return await handler(event, data)

        bot: Bot = data["bot"]
        if uid and not await is_subscribed(bot, uid):
            if isinstance(event, Message):
                await event.answer(f"Сначала подпишись на канал {CHANNEL_URL}",
                                   reply_markup=SUB_KB)
            elif isinstance(event, CallbackQuery):
                await event.answer("Сначала подпишись на канал!", show_alert=True)
            return
        return await handler(event, data)


# ======================= разбор PDF =======================
HEAD_RE = re.compile(r"^\d{1,2}[а-яёА-ЯЁ]")
LABEL_RE = re.compile(r"^(\d{1,2})([а-яё])\(?([^)]*)\)?(.*)$")
MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
          "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}
WD = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WD_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
TITLE_RE = re.compile(r"на\s+([а-яё]+),?\s+(\d{1,2})\s+([а-яё]+)", re.I)


def cluster(vals, tol=1.5):
    vals = sorted(vals)
    out, grp = [], []
    for v in vals:
        if grp and v - grp[-1] > tol:
            out.append(sum(grp) / len(grp))
            grp = []
        grp.append(v)
    if grp:
        out.append(sum(grp) / len(grp))
    return out


def build_layout(pdf_bytes: bytes):
    pages, title = [], ""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for pi, page in enumerate(pdf.pages):
            if pi == 0:
                title = (page.extract_text() or "")[:400]
            E = cluster([e["x0"] for e in page.edges if e["orientation"] == "v"])
            H = cluster([e["top"] for e in page.edges if e["orientation"] == "h"])
            words = page.extract_words(extra_attrs=["fontname"])
            heads = [w for w in words if "bold" in w["fontname"].lower() and HEAD_RE.match(w["text"])]
            rows = []
            for w in sorted(heads, key=lambda w: w["top"]):
                if rows and abs(rows[-1][0]["top"] - w["top"]) < 3:
                    rows[-1].append(w)
                else:
                    rows.append([w])
            out_rows = []
            for r in rows:
                cells = []
                for w in sorted(r, key=lambda w: w["x0"]):
                    lefts = [x for x in E if x <= w["x0"] + 1]
                    left = lefts[-1] if lefts else w["x0"] - 5
                    right = left + 105
                    if left in E:
                        i = E.index(left)
                        if i + 2 < len(E) and 70 < E[i + 2] - left < 140:
                            right = E[i + 2]
                    same_row = [x for x in words if abs(x["top"] - w["top"]) < 3
                                and x["x0"] >= w["x0"] - 1 and x["x1"] <= right + 1]
                    label = re.sub(r"\s+", "", "".join(x["text"] for x in sorted(same_row, key=lambda x: x["x0"])))
                    m = LABEL_RE.match(label.lower())
                    if not m:
                        continue
                    key, profile = m[1] + m[2], m[4]
                    cells.append(dict(key=key, full=key + profile, label=label,
                                      left=left, right=right))
                if cells:
                    tops = [y for y in H if y <= r[0]["top"] + 0.5]
                    out_rows.append(dict(top=max(tops) if tops else r[0]["top"] - 4, cells=cells))
            pages.append(dict(E=E, H=H, rows=out_rows,
                              top=min(H) if H else 0, bottom=(max(H) if H else page.height) + 0.5))
    classes = sorted({c["key"] for p in pages for r in p["rows"] for c in r["cells"]},
                     key=lambda k: (int(re.match(r"\d+", k)[0]), k))
    return dict(pages=pages, classes=classes, title=title)


def parse_title(title: str):
    m = TITLE_RE.search(title.lower())
    if not m or m[3] not in MONTHS:
        return None
    today = date.today()
    best = None
    for y in (today.year - 1, today.year, today.year + 1):
        try:
            d = date(y, MONTHS[m[3]], int(m[2]))
        except ValueError:
            continue
        if best is None or abs((d - today).days) < abs((best - today).days):
            best = d
    return best


def _crop(pg, bbox, res):
    W, H = pg.get_size()
    x0, t, x1, b = bbox
    x0, t, x1, b = max(0, x0), max(0, t), min(W, x1), min(H, b)
    bm = pg.render(scale=res / 72, crop=(x0, H - b, W - x1, t))
    return bm.to_pil().convert("RGB")


def norm_cls(s: str) -> str:
    s = s.lower().translate(str.maketrans("acekmhopxty", "асекмнорхту"))
    return re.sub(r"[\s\-–—.()]", "", s)


def class_segments(layout: dict, query: str):
    q = norm_cls(query)
    segs = []
    pages = layout["pages"]
    for pi, pg in enumerate(pages):
        for ri, row in enumerate(pg["rows"]):
            cells = [c for c in row["cells"] if c["key"] == q] or \
                    [c for c in row["cells"] if c["full"] == q]
            if not cells:
                continue
            strip_left = pg["E"][0] if pg["E"] else 0
            strip_right = min(c["left"] for c in row["cells"])
            xs = [(strip_left, strip_right)] + [(c["left"], c["right"]) for c in cells]
            nxt = pg["rows"][ri + 1]["top"] if ri + 1 < len(pg["rows"]) else pg["bottom"]
            segs.append((pi, row["top"], nxt, xs))
            if ri == len(pg["rows"]) - 1 and pi + 1 < len(pages):
                np_ = pages[pi + 1]
                end = np_["rows"][0]["top"] if np_["rows"] else np_["bottom"]
                if end - np_["top"] > 10:
                    segs.append((pi + 1, np_["top"], end, xs))
    return segs


def class_signature(page_words: list, layout: dict, query: str):
    parts = []
    for pi, top, bottom, xs in class_segments(layout, query):
        for x0, x1 in xs[1:]:
            ws = [w for w in page_words[pi] if w["x0"] >= x0 - 1 and w["x1"] <= x1 + 1
                  and w["top"] >= top - 1 and w["bottom"] <= bottom + 1]
            ws.sort(key=lambda w: (round(w["top"]), w["x0"]))
            parts.append(" ".join(w["text"] for w in ws))
    return hashlib.sha1("|".join(parts).encode()).hexdigest() if parts else None


def render_class(pdf_bytes: bytes, layout: dict, query: str):
    segs = class_segments(layout, query)
    if not segs:
        return None
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        parts = []
        for pi, top, bottom, xs in segs:
            page = doc[pi]
            crops = [_crop(page, (x0, top, x1, bottom), RES) for x0, x1 in xs]
            w = sum(c.width for c in crops)
            h = max(c.height for c in crops)
            img = Image.new("RGB", (w, h), "white")
            x = 0
            for c in crops:
                img.paste(c, (x, 0))
                x += c.width
            parts.append(img)
    finally:
        doc.close()
    W = max(p.width for p in parts)
    H = sum(p.height for p in parts)
    out = Image.new("RGB", (W, H), "white")
    y = 0
    for p in parts:
        out.paste(p, (0, y))
        y += p.height
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# ======================= кэш картинок =======================
def _build_sync(sha: str, pdf_bytes: bytes, layout: dict):
    final = os.path.join(CACHE_DIR, sha)
    tmp = final + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    names = set(layout["classes"]) | {c["full"] for p in layout["pages"]
                                      for r in p["rows"] for c in r["cells"]}
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page_words = [pg.extract_words() for pg in pdf.pages]
    n = 0
    for q in sorted(names):
        png = render_class(pdf_bytes, layout, q)
        if png:
            open(os.path.join(tmp, q + ".png"), "wb").write(png)
            sig = class_signature(page_words, layout, q)
            if sig:
                open(os.path.join(tmp, q + ".sig"), "w").write(sig)
            n += 1
    open(os.path.join(tmp, ".ok"), "w").write(str(time.time()))
    shutil.rmtree(final, ignore_errors=True)
    os.replace(tmp, final)
    logging.info("cache built: %s, classes: %d", sha[:8], n)


_build_lock = asyncio.Lock()


async def build_cache(sha: str, data: bytes, layout: dict):
    async with _build_lock:
        if os.path.exists(os.path.join(CACHE_DIR, sha, ".ok")):
            return
        await asyncio.to_thread(_build_sync, sha, data, layout)


async def cache_png(d: dict, cls: str):
    folder = os.path.join(CACHE_DIR, d["sha"])
    if not os.path.exists(os.path.join(folder, ".ok")):
        await build_cache(d["sha"], d["data"], d["layout"])
    path = os.path.join(folder, norm_cls(cls) + ".png")
    return open(path, "rb").read() if os.path.exists(path) else None


async def cache_sig(d: dict, cls: str):
    folder = os.path.join(CACHE_DIR, d["sha"])
    if not os.path.exists(os.path.join(folder, ".ok")):
        await build_cache(d["sha"], d["data"], d["layout"])
    path = os.path.join(folder, norm_cls(cls) + ".sig")
    return open(path).read() if os.path.exists(path) else None


# ======================= сайт =======================
def fix_url(u: str) -> str:
    p = urlsplit(u)
    try:
        host = (p.hostname or "").encode("idna").decode()
        netloc = host + (f":{p.port}" if p.port else "")
        return urlunsplit((p.scheme, netloc, p.path, p.query, p.fragment))
    except Exception:
        return u


HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)
SRC_RE = re.compile(r"""src\s*=\s*["']([^"']+)["']""", re.I)
ANCHOR_RE = re.compile(r"""<a\b[^>]*?href\s*=\s*["']([^"']+)["'][^>]*>(.*?)</a>""", re.I | re.S)
WD_STEMS = ["понедельник", "вторник", "сред", "четверг", "пятниц", "суббот", "воскресень"]
docs: dict = {}
_last = {"t": 0}
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; school-schedule-bot)"}


def anchor_weekday(inner_html: str):
    t = re.sub(r"<[^>]+>", " ", inner_html).lower()
    if "расписание" not in t:
        return None
    for i, stem in enumerate(WD_STEMS):
        if stem in t:
            return i
    return None


def weekday_date(wd: int) -> date:
    today = date.today()
    start = today - timedelta(days=today.weekday())
    if today.weekday() >= 5:
        start += timedelta(days=7)
    return start + timedelta(days=wd)


def find_links(html: str, base: str) -> dict:
    found = {}
    for href, inner in ANCHOR_RE.findall(html):
        if href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        full = urljoin(base, href.strip())
        wd = anchor_weekday(inner)
        if wd is not None or ".pdf" in unquote(full).lower():
            found.setdefault(full, wd)
    return found


def google_direct(url: str) -> str:
    m = re.search(r"drive\.google\.com/file/d/([\w-]+)", url) or \
        re.search(r"drive\.google\.com/(?:open|uc)\?(?:[^#]*&)?id=([\w-]+)", url)
    if m:
        return f"https://drive.google.com/uc?export=download&id={m[1]}"
    m = re.search(r"docs\.google\.com/spreadsheets/d/([\w-]+)", url)
    if m and "/export" not in url:
        return f"https://docs.google.com/spreadsheets/d/{m[1]}/export?format=pdf&portrait=true&fitw=true"
    return url


async def fetch(session, url, as_text=False):
    async with session.get(fix_url(url), timeout=aiohttp.ClientTimeout(total=60), headers=HEADERS) as r:
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}")
        return await (r.text() if as_text else r.read())


async def get_pdf_bytes(session, url):
    data = await fetch(session, google_direct(url))
    if data[:4] == b"%PDF":
        return data
    html = data.decode("utf-8", "ignore")
    inner = []
    for h in HREF_RE.findall(html) + SRC_RE.findall(html):
        full = urljoin(url, h.strip())
        if (".pdf" in unquote(full).lower() or "drive.google.com" in full
            or "docs.google.com/spreadsheets" in full) and full not in inner and full != url:
            inner.append(full)
    for u in inner[:3]:
        try:
            d = await fetch(session, google_direct(u))
        except Exception:
            continue
        if d[:4] == b"%PDF":
            return d
    return None


async def refresh(force=False):
    if not force and time.time() - _last["t"] < 300 and docs:
        return []
    changed = []
    try:
        async with aiohttp.ClientSession() as s:
            links = {u: None for u in EXTRA_PDF_URLS}
            try:
                html = await fetch(s, PAGE_URL, as_text=True)
                links.update(find_links(html, PAGE_URL))
            except Exception as e:
                logging.warning("page fetch failed: %s", e)
            for url, wd in list(links.items())[:20]:
                try:
                    data = await get_pdf_bytes(s, url)
                except Exception as e:
                    logging.warning("pdf %s failed: %s", url, e)
                    continue
                if not data:
                    logging.warning("no pdf behind %s", url)
                    continue
                sha = hashlib.sha1(data).hexdigest()
                if url in docs and docs[url]["sha"] == sha:
                    continue
                try:
                    lay = await asyncio.to_thread(build_layout, data)
                except Exception as e:
                    logging.warning("layout %s failed: %s", url, e)
                    continue
                d = parse_title(lay["title"]) or (weekday_date(wd) if wd is not None else None)
                name = WD[wd].capitalize() if wd is not None else \
                    unquote(url.rsplit("/", 1)[-1]).rsplit(".", 1)[0]
                docs[url] = dict(data=data, sha=sha, layout=lay, date=d, name=name)
                await build_cache(sha, data, lay)
                changed.append(url)
            for url in [u for u in docs if u not in links]:
                del docs[url]
        _last["t"] = time.time()
    except Exception as e:
        logging.warning("refresh failed: %s", e)
    return changed


def sorted_docs():
    return sorted(docs.items(), key=lambda kv: (kv[1]["date"] or date.min, kv[1]["name"]))


def doc_label(d: dict) -> str:
    if d["date"]:
        return f"{WD_SHORT[d['date'].weekday()]} {d['date']:%d.%m}"
    return d["name"][:14]


def pick_doc(on: date | None = None):
    items = sorted_docs()
    if not items:
        return None
    if on:
        for u, d in items:
            if d["date"] == on:
                return u
        return None
    today = date.today()
    future = [u for u, d in items if d["date"] and d["date"] >= today]
    return future[0] if future else items[-1][0]


def all_classes():
    return sorted({c for d in docs.values() for c in d["layout"]["classes"]},
                  key=lambda k: (int(re.match(r"\d+", k)[0]), k))


# ======================= интерфейс =======================
MENU = ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
    [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="📆 Завтра")],
    [KeyboardButton(text="🗓 Другой день"), KeyboardButton(text="🏫 Сменить класс")],
    [KeyboardButton(text="📄 Весь файл"), KeyboardButton(text="🔔 Уведомления")],
])


def days_kb(prefix="d"):
    rows, cur = [], []
    for url, d in sorted_docs()[-8:]:
        cur.append(InlineKeyboardButton(text=doc_label(d),
                                        callback_data=f"{prefix}:{hashlib.md5(url.encode()).hexdigest()[:8]}"))
        if len(cur) == 4:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def url_by_id(h):
    for u in docs:
        if hashlib.md5(u.encode()).hexdigest()[:8] == h:
            return u


def parallels_kb():
    pars = sorted({int(re.match(r"\d+", c)[0]) for c in all_classes()})
    rows, cur = [], []
    for p in pars:
        cur.append(InlineKeyboardButton(text=f"{p} класс", callback_data=f"p:{p}"))
        if len(cur) == 3:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def letters_kb(p: int):
    rows, cur = [], []
    for c in all_classes():
        if int(re.match(r"\d+", c)[0]) == p:
            cur.append(InlineKeyboardButton(text=c, callback_data=f"c:{c}"))
            if len(cur) == 4:
                rows.append(cur)
                cur = []
    if cur:
        rows.append(cur)
    rows.append([InlineKeyboardButton(text="⬅ Назад", callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def ask_class(msg: Message):
    await refresh()
    if not docs:
        return await msg.answer("Не нашёл файлов расписания на сайте школы. Попробуй позже.")
    await msg.answer("Выбери параллель (или просто напиши класс, например 7б):",
                     reply_markup=parallels_kb())


async def send_schedule(msg: Message, uid, url=None, full=False):
    await refresh()
    u = user(uid)
    url = url or pick_doc()
    if not url or url not in docs:
        return await msg.answer("Расписания на сайте пока нет.")
    d = docs[url]
    title = f"{WD[d['date'].weekday()].capitalize()} {d['date']:%d.%m}" if d["date"] else d["name"]
    if not full and u["cls"]:
        png = await cache_png(d, u["cls"])
        if png:
            _mark_sent(u, d, await cache_sig(d, u["cls"]) or hashlib.sha1(png).hexdigest())
            save_prefs()
            return await msg.answer_photo(BufferedInputFile(png, "schedule.png"),
                                          caption=f"🏫 {u['cls'].upper()} · {title}",
                                          reply_markup=days_kb())
        await msg.answer(f"Класс «{u['cls'].upper()}» в этом файле не найден. Отправляю весь файл.")
    await msg.answer_document(BufferedInputFile(d["data"], f"{d['name']}.pdf"),
                              caption=title, reply_markup=days_kb())


def parse_date(t: str):
    m = re.match(r"^(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?$", t.strip())
    if not m:
        return None
    yr = m[3]
    yr = date.today().year if not yr else (int(yr) + 2000 if len(yr) == 2 else int(yr))
    try:
        return date(yr, int(m[2]), int(m[1]))
    except ValueError:
        return None


async def send_day(msg: Message, uid, d: date):
    await refresh()
    url = pick_doc(d)
    if url:
        return await send_schedule(msg, uid, url)
    have = ", ".join(doc_label(x) for _, x in sorted_docs()) or "ничего нет"
    await msg.answer(f"На {d:%d.%m} расписания на сайте нет.\nСейчас есть: {have}",
                     reply_markup=days_kb())


# ======================= РАССЫЛКА =======================
broadcast_state: dict[int, dict] = {}


@router.message(Command("broadcast"))
async def cmd_broadcast(m: Message):
    if m.from_user.id != ADMIN_ID:
        return await m.answer("⛔ Команда только для админа.")
    await m.answer(
        "📢 <b>Режим рассылки</b>\n\n"
        "Отправь мне сообщение (текст, фото или фото с подписью).\n"
        "Оно уйдёт <b>ВСЕМ</b> пользователям бота.\n\n"
        "Отмена: /cancel",
        parse_mode="HTML",
    )
    broadcast_state[m.from_user.id] = {"awaiting": True}


@router.message(Command("cancel"))
async def cmd_cancel(m: Message):
    if m.from_user.id == ADMIN_ID and m.from_user.id in broadcast_state:
        broadcast_state.pop(m.from_user.id, None)
        return await m.answer("❌ Рассылка отменена.")
    await m.answer("Нечего отменять.")


@router.message(Command("stats"))
async def cmd_stats(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    total = len(prefs)
    with_cls = sum(1 for u in prefs.values() if u.get("cls"))
    notify_on = sum(1 for u in prefs.values() if u.get("notify", True))
    await m.answer(
        f"👥 Всего пользователей: <b>{total}</b>\n"
        f"🎓 Выбрали класс: <b>{with_cls}</b>\n"
        f"🔔 Уведомления вкл.: <b>{notify_on}</b>",
        parse_mode="HTML",
    )


async def do_broadcast(bot: Bot, m: Message):
    ids = [int(uid) for uid in prefs]
    if not ids:
        return await m.answer("Нет пользователей для рассылки.")

    ok, fail = 0, 0
    status = await m.answer(f"📤 Начинаю рассылку на {len(ids)} польз…")

    for i, uid in enumerate(ids, 1):
        try:
            if m.photo:
                file_id = m.photo[-1].file_id
                await bot.send_photo(uid, file_id, caption=m.caption or "")
            elif m.document:
                await bot.send_document(uid, m.document.file_id, caption=m.caption or "")
            elif m.video:
                await bot.send_video(uid, m.video.file_id, caption=m.caption or "")
            else:
                await bot.send_message(uid, m.text or "")
            ok += 1
        except Exception as e:
            fail += 1
            logging.warning("broadcast to %s failed: %s", uid, e)
        await asyncio.sleep(0.05)

        if i % 20 == 0:
            try:
                await status.edit_text(f"📤 Рассылка… {i}/{len(ids)}")
            except Exception:
                pass

    await status.edit_text(
        f"✅ Рассылка завершена.\nУспешно: <b>{ok}</b>\nОшибок: <b>{fail}</b>",
        parse_mode="HTML",
    )


# ======================= обработчики =======================
@router.message(CommandStart())
async def start(m: Message, bot: Bot):
    if not await require_sub(m, bot):
        return
    u = user(m.from_user.id)
    if not u.get("welcomed"):
        u["welcomed"] = True
        save_prefs()
        try:
            await bot.send_message(
                CHANNEL,
                f"👋 Новый пользователь: {m.from_user.full_name} "
                f"(@{m.from_user.username or 'без_username'}) запустил бота.",
            )
        except Exception as e:
            logging.warning("channel notify failed: %s", e)
    await m.answer("Привет! Я присылаю расписание уроков школы №85 для твоего класса "
                   "и сам сообщу, когда на сайте появится новое.", reply_markup=MENU)
    await ask_class(m)


@router.callback_query(F.data == "check_sub")
async def cb_check_sub(c: CallbackQuery, bot: Bot):
    await c.answer()
    if await is_subscribed(bot, c.from_user.id):
        _sub_cache.pop(c.from_user.id, None)
        user(c.from_user.id)
        save_prefs()
        await c.message.edit_text("✅ Спасибо за подписку! Нажми /start, чтобы продолжить.")
    else:
        await c.answer("Ты ещё не подписан на канал!", show_alert=True)


@router.callback_query(F.data.startswith("p:"))
async def cb_parallel(c: CallbackQuery):
    await c.answer()
    await c.message.edit_text(f"Выбери класс ({c.data[2:]} параллель):",
                              reply_markup=letters_kb(int(c.data[2:])))


@router.callback_query(F.data == "back")
async def cb_back(c: CallbackQuery):
    await c.answer()
    await c.message.edit_text("Выбери параллель:", reply_markup=parallels_kb())


@router.callback_query(F.data.startswith("c:"))
async def cb_class(c: CallbackQuery):
    await c.answer()
    user(c.from_user.id)["cls"] = c.data[2:]
    save_prefs()
    await c.message.edit_text(f"✅ Класс: {c.data[2:].upper()}")
    await send_schedule(c.message, c.from_user.id)


@router.callback_query(F.data.startswith("d:"))
async def cb_day(c: CallbackQuery):
    await c.answer()
    if not user(c.from_user.id)["cls"]:
        return await ask_class(c.message)
    url = url_by_id(c.data[2:])
    await send_schedule(c.message, c.from_user.id, url)


@router.message(F.text == "📅 Сегодня")
async def b_today(m: Message):
    if not user(m.from_user.id)["cls"]:
        return await ask_class(m)
    await send_day(m, m.from_user.id, date.today())


@router.message(F.text == "📆 Завтра")
async def b_tomorrow(m: Message):
    if not user(m.from_user.id)["cls"]:
        return await ask_class(m)
    await send_day(m, m.from_user.id, date.today() + timedelta(days=1))


@router.message(F.text == "🗓 Другой день")
async def b_other(m: Message):
    await refresh()
    if not docs:
        return await m.answer("Расписания на сайте пока нет.")
    await m.answer("Выбери день (или напиши дату, например 16.10):", reply_markup=days_kb())


@router.message(F.text == "🏫 Сменить класс")
async def b_class(m: Message):
    await ask_class(m)


@router.message(F.text == "📄 Весь файл")
async def b_full(m: Message):
    await send_schedule(m, m.from_user.id, full=True)


@router.message(F.text.in_({"🔔 Уведомления", "/notify"}))
@router.message(Command("notify"))
async def b_notify(m: Message):
    u = user(m.from_user.id)
    u["notify"] = not u.get("notify", True)
    save_prefs()
    await m.answer("🔔 Уведомления о новом расписании включены." if u["notify"]
                   else "🔕 Уведомления выключены.")


@router.message(F.text)
async def free_text(m: Message, bot: Bot):
    if m.from_user.id == ADMIN_ID and m.from_user.id in broadcast_state:
        broadcast_state.pop(m.from_user.id, None)
        return await do_broadcast(bot, m)

    t = m.text.strip()
    d = parse_date(t)
    if d:
        if not user(m.from_user.id)["cls"]:
            return await ask_class(m)
        return await send_day(m, m.from_user.id, d)
    await refresh()
    q = norm_cls(t)
    known = all_classes()
    fulls = {c["full"] for dd in docs.values() for p in dd["layout"]["pages"]
             for r in p["rows"] for c in r["cells"]}
    if q in known or q in fulls:
        user(m.from_user.id)["cls"] = q
        save_prefs()
        return await send_schedule(m, m.from_user.id)
    await m.answer("Не нашёл такой класс. Выбери из списка или напиши, например: 7б, 10а, 11б",
                   reply_markup=parallels_kb() if known else None)


# ======================= автопроверка сайта =======================
def _key(d: dict) -> str:
    return d["date"].isoformat() if d["date"] else d["name"]


def _same_sent(u: dict, d: dict, sig: str) -> bool:
    return u.setdefault("sent", {}).get(_key(d)) == sig


def _mark_sent(u: dict, d: dict, sig: str):
    sent = u.setdefault("sent", {})
    sent[_key(d)] = sig
    for k in sorted(sent)[:-10]:
        del sent[k]


async def notify(bot: Bot, d: dict, only_mark=False):
    title = f"{WD[d['date'].weekday()].capitalize()} {d['date']:%d.%m}" if d["date"] else d["name"]
    for uid, u in list(prefs.items()):
        if not u.get("cls") or (not u.get("notify", True) and not only_mark):
            continue
        try:
            if not only_mark and not await is_subscribed(bot, int(uid)):
                continue
            png = await cache_png(d, u["cls"])
            if not png:
                continue
            sig = await cache_sig(d, u["cls"]) or hashlib.sha1(png).hexdigest()
            if _same_sent(u, d, sig):
                continue
            if not only_mark:
                await bot.send_photo(int(uid), BufferedInputFile(png, "schedule.png"),
                                     caption=f"🆕 Новое расписание · {u['cls'].upper()} · {title}")
                await asyncio.sleep(0.1)
            _mark_sent(u, d, sig)
        except Exception as e:
            logging.warning("send to %s failed: %s", uid, e)
    save_prefs()


async def watcher(bot: Bot):
    await refresh(force=True)
    for d in list(docs.values()):
        await notify(bot, d, only_mark=True)
    while True:
        await asyncio.sleep(CHECK_EVERY)
        try:
            for url in await refresh(force=True):
                d = docs[url]
                if d["date"] and d["date"] < date.today():
                    continue
                await notify(bot, d)
        except Exception as e:
            logging.warning("watcher error: %s", e)


# ======================= очистка кэша =======================
async def cleanup_loop():
    while True:
        await asyncio.sleep(3600)
        try:
            now = time.time()
            actual = {d["sha"] for d in docs.values()}
            for name in os.listdir(CACHE_DIR):
                folder = os.path.join(CACHE_DIR, name)
                marker = os.path.join(folder, ".ok")
                age = now - (os.path.getmtime(marker) if os.path.exists(marker) else os.path.getmtime(folder))
                if age > CACHE_TTL or (name not in actual and not name.endswith(".tmp")):
                    shutil.rmtree(folder, ignore_errors=True)
                    logging.info("cache removed: %s", name[:8])
            for d in list(docs.values()):
                await build_cache(d["sha"], d["data"], d["layout"])
        except Exception as e:
            logging.warning("cleanup error: %s", e)


# ======================= запуск =======================
async def health(request):
    return web.Response(text="ok")


async def main():
    if not BOT_TOKEN:
        logging.error("❌ Не задан BOT_TOKEN в переменных окружения!")
        return

    shutil.rmtree(CACHE_DIR, ignore_errors=True)
    os.makedirs(CACHE_DIR, exist_ok=True)

    port = int(os.environ.get("PORT", 10000))
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    dp.message.outer_middleware(SubscriptionMiddleware())
    dp.callback_query.outer_middleware(SubscriptionMiddleware())
    await bot.delete_webhook(drop_pending_updates=True)
    tasks = [asyncio.create_task(watcher(bot)), asyncio.create_task(cleanup_loop())]
    try:
        await dp.start_polling(bot)
    finally:
        for t in tasks:
            t.cancel()


if __name__ == "__main__":
    asyncio.run(main())