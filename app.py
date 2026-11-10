"""
Сбор сведений от клиентов ИП «Престиж мебель» для подготовки
коллективного заявления в полицию и исков о возврате денег.

Одна зависимость: Flask. Данные: SQLite + папка с файлами в DATA_DIR.
"""
import csv
import hmac
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import time
import uuid
import zipfile
from collections import defaultdict, deque
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, send_file, session, url_for)

# ---------------------------------------------------------------- настройки
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "claims.sqlite3"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ACCESS_CODE = os.environ.get("ACCESS_CODE", "")          # код доступа к форме (необязательно)


def norm_phone(p):
    """+7 (701) 234-56-78, 87012345678, 7012345678 -> 77012345678"""
    d = re.sub(r"\D", "", p or "")
    if len(d) == 11 and d[0] == "8":
        d = "7" + d[1:]
    elif len(d) == 10:
        d = "7" + d
    return d


# Белый список телефонов, которым разрешено подавать анкеты (через запятую или точку с запятой).
# Пусто = ограничения по телефону нет.
ALLOWED_PHONES = {norm_phone(x) for x in re.split(r"[,;\n]+", os.environ.get("ALLOWED_PHONES", "")) if norm_phone(x)}
GATED = bool(ACCESS_CODE or ALLOWED_PHONES)
ORGANIZER = os.environ.get("ORGANIZER_NAME", "[ФИО организатора]")
ORGANIZER_CONTACT = os.environ.get("ORGANIZER_CONTACT", "[телефон / e-mail организатора]")
DEADLINE = os.environ.get("DEADLINE", "")                 # например 30.11.2026
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"
# Уведомления на почту о новых анкетах (без персональных данных и файлов)
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "50"))
MAX_TOTAL_MB = int(os.environ.get("MAX_TOTAL_MB", "95"))   # предел одного HTTP-запроса (Cloudflare режет >100 МБ)
# Загрузка частями (JS): каждый кусок — отдельный небольшой запрос, поэтому лимит Cloudflare не мешает
BIG_FILE_MB = int(os.environ.get("BIG_FILE_MB", "1024"))    # максимум на один файл
CLAIM_MAX_MB = int(os.environ.get("CLAIM_MAX_MB", "3072"))  # максимум файлов на одну анкету
CHUNK_MB = 8
TMP_DIR = DATA_DIR / "tmp"

ALLOWED_EXT = {
    "pdf", "jpg", "jpeg", "png", "heic", "heif", "webp",
    "doc", "docx", "xls", "xlsx", "txt", "rtf",
    "mp3", "ogg", "opus", "m4a", "wav", "aac", "mp4", "mov",
    "zip",
}
FILE_CATEGORIES = [
    ("id", "Удостоверение личности (копия, обе стороны)", False),
    ("contract", "Договор, заказ, счёт, эскиз с ценой и сроком", False),
    ("payment", "Доказательства оплаты: чеки, выписка Kaspi/банка, расписки (если есть)", False),
    ("chat", "Переписка: скриншоты, экспорт чата WhatsApp, голосовые", False),
    ("claim", "Претензия и ответ на неё (если были)", False),
    ("other", "Прочее (фото, акты, свидетели)", False),
]
CAT_LABEL = {k: v for k, v, _ in FILE_CATEGORIES}

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
    MAX_CONTENT_LENGTH=MAX_TOTAL_MB * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=COOKIE_SECURE,
    PERMANENT_SESSION_LIFETIME=60 * 60 * 4,
)


# ---------------------------------------------------------------- база
SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ref TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL,
    ip TEXT,
    full_name TEXT NOT NULL,
    iin TEXT NOT NULL,
    birth_date TEXT,
    phone TEXT NOT NULL,
    email TEXT,
    address TEXT NOT NULL,
    order_date TEXT,
    item TEXT NOT NULL,
    contract_price REAL,
    paid_amount REAL NOT NULL,
    payment_dates TEXT,
    payment_method TEXT,
    promised_date TEXT,
    received_value REAL DEFAULT 0,
    received_desc TEXT,
    damage REAL,
    contact_channel TEXT,
    last_contact TEXT,
    claim_sent TEXT,
    police_filed TEXT,
    story TEXT NOT NULL,
    consent_pd INTEGER NOT NULL,
    consent_share INTEGER NOT NULL,
    consent_text TEXT NOT NULL,
    status TEXT DEFAULT 'новая',
    admin_note TEXT
);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    original_name TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    size INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    ip TEXT,
    action TEXT NOT NULL
);
"""


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_storage():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_tmp()
    try:
        os.chmod(DATA_DIR, 0o700)
    except OSError:
        pass
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.close()


def cleanup_tmp(max_age_h=48):
    cutoff = time.time() - max_age_h * 3600
    for p in TMP_DIR.glob("*"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


def now_iso():
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def client_ip():
    # за обратным прокси (Caddy/nginx) берём первый адрес из X-Forwarded-For
    if os.environ.get("BEHIND_PROXY") == "1":
        cf = request.headers.get("CF-Connecting-IP", "")  # Cloudflare Tunnel
        if cf:
            return cf.strip()
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
    return request.remote_addr or ""


def audit(action):
    db().execute("INSERT INTO audit(at, ip, action) VALUES (?,?,?)", (now_iso(), client_ip(), action))
    db().commit()


# ---------------------------------------------------------------- защита
_hits = defaultdict(deque)


def rate_limited(key, limit, window):
    q = _hits[key]
    t = time.time()
    while q and q[0] < t - window:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(t)
    return False


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


def check_csrf():
    sent = request.form.get("_csrf", "")
    if not sent or not hmac.compare_digest(sent, session.get("_csrf", "")):
        abort(400, "Сессия устарела. Обновите страницу и отправьте форму ещё раз.")


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; frame-ancestors 'none'; form-action 'self'"
    )
    if request.path.startswith("/admin"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/robots.txt")
def robots():
    return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("admin"):
            return redirect(url_for("admin_login", next=request.path))
        return fn(*a, **kw)
    return wrapper


# ---------------------------------------------------------------- проверки
def valid_iin(iin: str) -> bool:
    """Контрольная сумма ИИН РК (12 цифр, 12-я — контрольная)."""
    if not re.fullmatch(r"\d{12}", iin):
        return False
    d = [int(c) for c in iin]
    w1 = list(range(1, 12))
    w2 = [3, 4, 5, 6, 7, 8, 9, 10, 11, 1, 2]
    c = sum(a * b for a, b in zip(d[:11], w1)) % 11
    if c == 10:
        c = sum(a * b for a, b in zip(d[:11], w2)) % 11
        if c == 10:
            return False
    return c == d[11]


def money(v):
    v = (v or "").replace(" ", "").replace(" ", "").replace(",", ".")
    if not v:
        return None
    try:
        x = float(v)
    except ValueError:
        return "bad"
    return x if 0 <= x < 1e11 else "bad"


CONSENT_TEXT = (
    "Я даю согласие {org} на сбор, обработку и хранение моих персональных данных "
    "(ФИО, ИИН, дата рождения, контакты, адрес, сведения о платежах, копии документов и переписки) "
    "в соответствии с Законом РК «О персональных данных и их защите» с целью подготовки и подачи "
    "заявления в правоохранительные органы, исков в суд, обращений в госорганы по спору с "
    "ИП «Престиж мебель», а также на передачу этих данных в полицию, прокуратуру, суд и адвокату, "
    "представляющему пострадавших. Согласие действует до завершения разбирательства и может быть "
    "отозвано мной письменно."
)


def notify_new_claim(ref, claim_id, damage, nfiles):
    """Короткое письмо организатору. ФИО, ИИН и документы в письмо НЕ кладём."""
    if not (SMTP_HOST and NOTIFY_EMAIL):
        return

    def send():
        import smtplib
        import ssl
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["Subject"] = f"Новая анкета {ref}"
        msg["From"] = SMTP_USER or NOTIFY_EMAIL
        msg["To"] = NOTIFY_EMAIL
        link = f"{PUBLIC_URL}/admin/claim/{claim_id}" if PUBLIC_URL else f"/admin/claim/{claim_id}"
        msg.set_content(
            f"Поступила новая анкета {ref}.\n"
            f"Сумма ущерба: {tg(damage)}\nФайлов: {nfiles}\n\n"
            f"Открыть в админке: {link}\n\n"
            "Персональные данные и документы в письмо не включаются, они хранятся только на сервере."
        )
        try:
            if SMTP_PORT == 465:
                with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context(), timeout=20) as srv:
                    if SMTP_USER:
                        srv.login(SMTP_USER, SMTP_PASSWORD)
                    srv.send_message(msg)
            else:
                with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as srv:
                    srv.starttls(context=ssl.create_default_context())
                    if SMTP_USER:
                        srv.login(SMTP_USER, SMTP_PASSWORD)
                    srv.send_message(msg)
        except Exception as exc:  # письмо не должно ломать приём анкеты
            app.logger.error("Не удалось отправить уведомление %s: %s", ref, exc)

    import threading
    threading.Thread(target=send, daemon=True).start()


# ---------------------------------------------------------------- публичная часть
@app.route("/", methods=["GET", "POST"])
def form():
    if GATED and not session.get("access_ok"):
        return redirect(url_for("access"))

    errors, data = [], {}
    if request.method == "POST":
        check_csrf()
        if rate_limited(("form", client_ip()), 30, 3600):
            audit("отправка анкеты отклонена: слишком часто")
            abort(429, "Слишком много отправок. Попробуйте позже.")
        if request.form.get("website"):  # ловушка для ботов
            return redirect(url_for("thanks", ref="OK"))

        f = {k: (request.form.get(k) or "").strip() for k in request.form.keys()}
        if ALLOWED_PHONES:
            f["phone"] = session.get("phone", "")  # телефон фиксируется на входе
        data = f
        req = {
            "full_name": "ФИО", "iin": "ИИН", "phone": "Телефон", "address": "Адрес",
            "item": "Что заказывали", "paid_amount": "Сколько оплачено", "story": "Описание",
        }
        for k, label in req.items():
            if not f.get(k):
                errors.append(f"Заполните поле «{label}».")
        if f.get("iin") and not valid_iin(f["iin"]):
            errors.append("ИИН должен состоять из 12 цифр и быть корректным. Проверьте его по удостоверению.")
        price, paid, recv = money(f.get("contract_price")), money(f.get("paid_amount")), money(f.get("received_value"))
        for val, label in ((price, "Стоимость по договору"), (paid, "Сколько оплачено"), (recv, "Стоимость полученного")):
            if val == "bad":
                errors.append(f"Поле «{label}» должно быть числом в тенге.")
        if not request.form.get("consent_pd"):
            errors.append("Без согласия на обработку персональных данных мы не можем принять сведения.")
        if not request.form.get("consent_share"):
            errors.append("Нужно согласие на передачу данных в полицию, суд и адвокату.")
        if not request.form.get("confirm_truth"):
            errors.append("Подтвердите, что сведения достоверны.")

        uploads = []
        for cat, label, _req in FILE_CATEGORIES:
            for fs in request.files.getlist(f"files_{cat}"):
                if not fs or not fs.filename:
                    continue
                name = fs.filename
                ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                if ext not in ALLOWED_EXT:
                    errors.append(f"Файл «{name}»: формат .{ext or '?'} не принимается.")
                    continue
                uploads.append((cat, name, ext, fs))

        chunked = session_uploads()

        if errors:
            audit(f"анкета не принята ({session.get('phone') or '-'}): " + " | ".join(errors)[:500])
        if not errors:
            ref = "PM-" + datetime.now().strftime("%m%d") + "-" + secrets.token_hex(3).upper()
            paid_v = paid or 0
            recv_v = recv if isinstance(recv, float) else 0
            damage = max(paid_v - recv_v, 0)
            consent = CONSENT_TEXT.format(org=ORGANIZER)
            cur = db().execute(
                """INSERT INTO claims(ref, created_at, ip, full_name, iin, birth_date, phone, email, address,
                order_date, item, contract_price, paid_amount, payment_dates, payment_method, promised_date,
                received_value, received_desc, damage, contact_channel, last_contact, claim_sent, police_filed,
                story, consent_pd, consent_share, consent_text)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ref, now_iso(), client_ip(), f["full_name"], f["iin"], f.get("birth_date"), f["phone"],
                 f.get("email"), f["address"], f.get("order_date"), f["item"],
                 price if isinstance(price, float) else None, paid_v, f.get("payment_dates"),
                 f.get("payment_method"), f.get("promised_date"), recv_v, f.get("received_desc"), damage,
                 f.get("contact_channel"), f.get("last_contact"), f.get("claim_sent"), f.get("police_filed"),
                 f["story"], 1, 1, consent),
            )
            claim_id = cur.lastrowid
            cdir = UPLOAD_DIR / str(claim_id)
            cdir.mkdir(parents=True, exist_ok=True)
            for token, m in chunked:
                stored = f"{m['cat']}_{uuid.uuid4().hex}.{m['ext']}"
                os.replace(tmp_part(token), cdir / stored)
                (TMP_DIR / f"{token}.json").unlink(missing_ok=True)
                db().execute(
                    "INSERT INTO files(claim_id, category, original_name, stored_name, size) VALUES (?,?,?,?,?)",
                    (claim_id, m["cat"], m["name"], stored, m["size"]),
                )
            session["uploads"] = []
            for cat, name, ext, fs in uploads:
                stored = f"{cat}_{uuid.uuid4().hex}.{ext}"
                path = cdir / stored
                fs.save(path)
                size = path.stat().st_size
                if size > MAX_FILE_MB * 1024 * 1024:
                    path.unlink()
                    continue
                db().execute(
                    "INSERT INTO files(claim_id, category, original_name, stored_name, size) VALUES (?,?,?,?,?)",
                    (claim_id, cat, name[:200], stored, size),
                )
            db().commit()
            nfiles = db().execute("SELECT COUNT(*) FROM files WHERE claim_id=?", (claim_id,)).fetchone()[0]
            audit(f"анкета принята {ref} ({session.get('phone') or '-'}), файлов {nfiles}")
            notify_new_claim(ref, claim_id, damage, nfiles)
            return redirect(url_for("thanks", ref=ref))

    return render_template(
        "form.html", errors=errors, data=data, cats=FILE_CATEGORIES,
        organizer=ORGANIZER, contact=ORGANIZER_CONTACT, deadline=DEADLINE,
        locked_phone=session.get("phone") if ALLOWED_PHONES else None,
        consent=CONSENT_TEXT.format(org=ORGANIZER), max_mb=MAX_FILE_MB, total_mb=MAX_TOTAL_MB,
        big_mb=BIG_FILE_MB, claim_mb=CLAIM_MAX_MB, pending=session_uploads(),
        exts=", ".join(sorted(ALLOWED_EXT)),
    )


# ---------------------------------------------------------------- загрузка частями
TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


def upload_guard():
    if GATED and not session.get("access_ok"):
        abort(403, "Сначала войдите в форму.")
    sent = request.headers.get("X-CSRF-Token", "")
    if not sent or not hmac.compare_digest(sent, session.get("_csrf", "")):
        abort(400, "Сессия устарела. Обновите страницу.")


def tmp_meta(token):
    if not TOKEN_RE.match(token or "") or token not in session.get("uploads", []):
        return None
    mp = TMP_DIR / f"{token}.json"
    if not mp.is_file():
        return None
    return json.loads(mp.read_text())


def tmp_part(token):
    return TMP_DIR / f"{token}.part"


def session_uploads():
    """Полностью загруженные файлы текущей сессии: [(token, meta)]."""
    out = []
    for t in session.get("uploads", []):
        m = tmp_meta(t)
        if m and tmp_part(t).is_file() and tmp_part(t).stat().st_size == m["size"]:
            out.append((t, m))
    return out


@app.route("/upload/start", methods=["POST"])
def upload_start():
    upload_guard()
    d = request.get_json(silent=True) or {}
    cat, name = d.get("cat", ""), str(d.get("name", ""))[:200]
    try:
        size = int(d.get("size", -1))
    except (TypeError, ValueError):
        size = -1
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if cat not in CAT_LABEL:
        return {"error": "Неизвестный раздел."}, 400
    if ext not in ALLOWED_EXT:
        return {"error": f"Формат .{ext or '?'} не принимается."}, 400
    if size <= 0 or size > BIG_FILE_MB * 1024 * 1024:
        return {"error": f"Файл больше {BIG_FILE_MB} МБ. Сделайте экспорт без медиа или разделите файл."}, 400
    used = sum(m["size"] for _, m in session_uploads())
    if used + size > CLAIM_MAX_MB * 1024 * 1024:
        return {"error": f"На одну анкету можно загрузить до {CLAIM_MAX_MB} МБ."}, 400
    if shutil.disk_usage(DATA_DIR).free < size + 2 * 1024 ** 3:
        app.logger.error("Мало места на диске для загрузки %s байт", size)
        return {"error": "На сервере закончилось место. Напишите организатору."}, 507
    if len(session.get("uploads", [])) >= 200:
        return {"error": "Слишком много файлов в одной анкете."}, 400
    token = uuid.uuid4().hex
    (TMP_DIR / f"{token}.json").write_text(json.dumps({"cat": cat, "name": name, "ext": ext, "size": size}))
    tmp_part(token).touch()
    session["uploads"] = session.get("uploads", []) + [token]
    return {"token": token, "chunk": CHUNK_MB * 1024 * 1024}


@app.route("/upload/chunk/<token>", methods=["POST"])
def upload_chunk(token):
    upload_guard()
    m = tmp_meta(token)
    if not m:
        return {"error": "Загрузка не найдена, выберите файл ещё раз."}, 404
    part = tmp_part(token)
    have = part.stat().st_size
    try:
        offset = int(request.args.get("offset", "-1"))
    except ValueError:
        offset = -1
    if offset != have:
        return {"received": have}, 409
    data = request.get_data(cache=False)
    if len(data) > CHUNK_MB * 1024 * 1024 or have + len(data) > m["size"]:
        return {"error": "Неверный кусок файла."}, 400
    with open(part, "ab") as fh:
        fh.write(data)
    return {"received": have + len(data), "done": have + len(data) == m["size"]}


@app.route("/upload/delete/<token>", methods=["POST"])
def upload_delete(token):
    upload_guard()
    if tmp_meta(token):
        for p in (tmp_part(token), TMP_DIR / f"{token}.json"):
            p.unlink(missing_ok=True)
        session["uploads"] = [t for t in session.get("uploads", []) if t != token]
    return {"ok": True}


@app.route("/access", methods=["GET", "POST"])
def access():
    if not GATED:
        return redirect(url_for("form"))
    err = None
    if request.method == "POST":
        check_csrf()
        if rate_limited(("access", client_ip()), 30, 600):
            audit("вход в форму заблокирован: слишком много попыток")
            abort(429, "Слишком много попыток. Подождите 10 минут.")
        code_ok = not ACCESS_CODE or hmac.compare_digest(request.form.get("code", "").strip(), ACCESS_CODE)
        phone = norm_phone(request.form.get("phone", ""))
        phone_ok = not ALLOWED_PHONES or phone in ALLOWED_PHONES
        if code_ok and phone_ok:
            audit(f"вход в форму: телефон {phone or '-'}")
            session["access_ok"] = True
            if ALLOWED_PHONES:
                session["phone"] = "+" + phone
            return redirect(url_for("form"))
        audit(f"отказ во входе в форму: телефон {phone or '-'}")
        err = ("Этого номера нет в списке участников. Введите номер, на который вам пришла ссылка, "
               "или напишите организатору." if not phone_ok else "Неверный код. Уточните его у организатора.")
    return render_template("access.html", err=err, contact=ORGANIZER_CONTACT,
                           need_code=bool(ACCESS_CODE), need_phone=bool(ALLOWED_PHONES))


@app.route("/whatsapp")
def whatsapp_help():
    return render_template("whatsapp.html", max_mb=BIG_FILE_MB, contact=ORGANIZER_CONTACT)


@app.route("/thanks")
def thanks():
    return render_template("thanks.html", ref=request.args.get("ref", ""), contact=ORGANIZER_CONTACT)


@app.errorhandler(413)
def too_large(_e):
    audit(f"анкета не принята: файлы больше {MAX_TOTAL_MB} МБ ({session.get('phone') or '-'})")
    return render_template("message.html", title="Файлы слишком большие",
                           text=f"Общий размер файлов больше {MAX_TOTAL_MB} МБ. Отправьте форму с меньшим "
                                f"количеством файлов, остальное передайте организатору."), 413


@app.errorhandler(400)
@app.errorhandler(429)
def bad_req(e):
    return render_template("message.html", title="Не получилось", text=e.description), e.code


# ---------------------------------------------------------------- админка
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    err = None
    if not ADMIN_PASSWORD or len(ADMIN_PASSWORD) < 12:
        return render_template("message.html", title="Админка отключена",
                               text="Задайте ADMIN_PASSWORD длиной не меньше 12 символов в файле .env и перезапустите."), 503
    if request.method == "POST":
        check_csrf()
        if rate_limited(("login", client_ip()), 5, 900):
            abort(429, "Слишком много попыток входа. Подождите 15 минут.")
        if hmac.compare_digest(request.form.get("password", ""), ADMIN_PASSWORD):
            session.clear()
            session["admin"] = True
            session.permanent = True
            audit("вход в админку")
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/admin") else url_for("admin_list"))
        audit("неудачный вход")
        err = "Неверный пароль."
    return render_template("login.html", err=err)


@app.route("/admin/logout", methods=["POST"])
@admin_required
def admin_logout():
    check_csrf()
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin_list():
    rows = db().execute(
        "SELECT c.*, (SELECT COUNT(*) FROM files f WHERE f.claim_id=c.id) AS nfiles, "
        "(SELECT COUNT(*) FROM files f WHERE f.claim_id=c.id AND f.category='payment') AS npay "
        "FROM claims c ORDER BY c.id"
    ).fetchall()
    total_paid = sum(r["paid_amount"] or 0 for r in rows)
    total_damage = sum(r["damage"] or 0 for r in rows)
    dup = defaultdict(int)
    for r in rows:
        dup[r["iin"]] += 1
    return render_template("admin_list.html", rows=rows, total_paid=total_paid,
                           total_damage=total_damage, dup=dup)


@app.route("/admin/claim/<int:cid>", methods=["GET", "POST"])
@admin_required
def admin_claim(cid):
    c = db().execute("SELECT * FROM claims WHERE id=?", (cid,)).fetchone()
    if not c:
        abort(404)
    if request.method == "POST":
        check_csrf()
        db().execute("UPDATE claims SET status=?, admin_note=? WHERE id=?",
                     (request.form.get("status", "новая")[:40], request.form.get("admin_note", "")[:5000], cid))
        db().commit()
        flash("Сохранено.")
        return redirect(url_for("admin_claim", cid=cid))
    files = db().execute("SELECT * FROM files WHERE claim_id=? ORDER BY category, id", (cid,)).fetchall()
    audit(f"просмотр заявки {c['ref']}")
    return render_template("admin_claim.html", c=c, files=files, cat_label=CAT_LABEL,
                           statuses=["новая", "проверена", "нужны документы", "подписал заявление",
                                     "признан потерпевшим", "иск подан", "отозвал согласие"])


@app.route("/admin/file/<int:fid>")
@admin_required
def admin_file(fid):
    f = db().execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
    if not f:
        abort(404)
    path = UPLOAD_DIR / str(f["claim_id"]) / f["stored_name"]
    if not path.is_file():
        abort(404)
    return send_file(path, as_attachment=True, download_name=f["original_name"])


@app.route("/admin/claim/<int:cid>/delete", methods=["POST"])
@admin_required
def admin_delete(cid):
    check_csrf()
    c = db().execute("SELECT ref FROM claims WHERE id=?", (cid,)).fetchone()
    if not c:
        abort(404)
    for f in db().execute("SELECT stored_name FROM files WHERE claim_id=?", (cid,)):
        p = UPLOAD_DIR / str(cid) / f["stored_name"]
        if p.exists():
            p.unlink()
    d = UPLOAD_DIR / str(cid)
    if d.exists() and not any(d.iterdir()):
        d.rmdir()
    db().execute("DELETE FROM files WHERE claim_id=?", (cid,))
    db().execute("DELETE FROM claims WHERE id=?", (cid,))
    db().commit()
    audit(f"удалена заявка {c['ref']}")
    flash(f"Заявка {c['ref']} и её файлы удалены.")
    return redirect(url_for("admin_list"))


CSV_COLS = [
    ("ref", "Номер"), ("created_at", "Дата подачи"), ("full_name", "ФИО"), ("iin", "ИИН"),
    ("birth_date", "Дата рождения"), ("phone", "Телефон"), ("email", "E-mail"), ("address", "Адрес"),
    ("order_date", "Дата заказа"), ("item", "Что заказывали"), ("contract_price", "Цена по договору, тг"),
    ("paid_amount", "Оплачено, тг"), ("payment_dates", "Даты оплат"), ("payment_method", "Способ оплаты"),
    ("promised_date", "Обещанный срок"), ("received_value", "Стоимость полученного, тг"),
    ("received_desc", "Что получено"), ("damage", "Ущерб, тг"), ("contact_channel", "Канал связи с ИП"),
    ("last_contact", "Последний контакт"), ("claim_sent", "Претензия"), ("police_filed", "Уже обращался в полицию"),
    ("story", "Описание"), ("status", "Статус"), ("admin_note", "Заметка"), ("nfiles", "Файлов"),
]


def csv_val(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else f"{v:.2f}".replace(".", ",")
    return v


def build_csv(rows):
    buf = io.StringIO()
    buf.write("﻿")  # BOM, чтобы Excel правильно открыл кириллицу
    w = csv.writer(buf, delimiter=";")
    w.writerow([h for _, h in CSV_COLS])
    for r in rows:
        w.writerow([csv_val(r[k]) for k, _ in CSV_COLS])
    w.writerow([])
    w.writerow(["ИТОГО", "", "", "", "", "", "", "", "", "", "",
                csv_val(float(sum(r["paid_amount"] or 0 for r in rows))), "", "", "", "", "",
                csv_val(float(sum(r["damage"] or 0 for r in rows)))])
    return buf.getvalue()


def all_rows():
    return db().execute(
        "SELECT c.*, (SELECT COUNT(*) FROM files f WHERE f.claim_id=c.id) AS nfiles FROM claims c ORDER BY c.id"
    ).fetchall()


@app.route("/admin/export.csv")
@admin_required
def export_csv():
    audit("экспорт CSV")
    data = build_csv(all_rows())
    return Response(data, mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=prestige_{datetime.now():%Y%m%d}.csv"})


def safe_dir_name(s):
    s = re.sub(r"[^\w\-. ]+", "", s, flags=re.UNICODE).strip().replace(" ", "_")
    return s[:60] or "noname"


@app.route("/admin/export.zip")
@admin_required
def export_zip():
    audit("экспорт ZIP")
    rows = all_rows()
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("svodnaya_tablica.csv", build_csv(rows))
        for i, r in enumerate(rows, 1):
            folder = f"{i:02d}_{safe_dir_name(r['full_name'])}_{r['ref']}"
            card = {k: r[k] for k in r.keys() if k not in ("ip",)}
            z.writestr(f"{folder}/anketa.json", json.dumps(card, ensure_ascii=False, indent=2))
            z.writestr(f"{folder}/anketa.txt", "\n".join(
                f"{h}: {'' if r[k] is None else r[k]}" for k, h in CSV_COLS) + f"\n\nТекст согласия:\n{r['consent_text']}\n")
            for f in db().execute("SELECT * FROM files WHERE claim_id=?", (r["id"],)):
                p = UPLOAD_DIR / str(r["id"]) / f["stored_name"]
                if p.is_file():
                    orig = safe_dir_name(f["original_name"]) or f["stored_name"]
                    z.write(p, f"{folder}/{f['category']}_{f['id']}_{orig}")
    mem.seek(0)
    return send_file(mem, mimetype="application/zip", as_attachment=True,
                     download_name=f"prestige_all_{datetime.now():%Y%m%d}.zip")


@app.route("/admin/audit")
@admin_required
def admin_audit():
    rows = db().execute("SELECT * FROM audit ORDER BY id DESC LIMIT 300").fetchall()
    return render_template("admin_audit.html", rows=rows)


@app.template_filter("tg")
def tg(v):
    if v is None or v == "":
        return "—"
    return f"{v:,.0f}".replace(",", " ") + " ₸"


init_storage()

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
