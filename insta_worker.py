import os
import re
import io
import json
import time
import html
import shutil
import random
import base64
import hashlib
import threading
import subprocess
import urllib.parse
from datetime import datetime, timedelta
import telebot
from telebot import apihelper
from telebot.types import (
    InputMediaPhoto,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from instagrapi import Client

# جلوگیری قطعی از قفل شدن سوکت تلگرام
apihelper.CONNECT_TIMEOUT = 10
apihelper.READ_TIMEOUT = 12

try:
    from instagrapi.types import StoryPoll, StoryTallie
    HAS_STORY_POLL = True
except Exception:
    HAS_STORY_POLL = False

# ==========================================
# ۱. تنظیمات پایه و مشخصات شبیه‌ساز گوشی اندروید
# ==========================================
TOKEN = os.environ.get("BOT_TOKEN", "").strip()
BACKUP_BOT_TOKEN = os.environ.get("BACKUP_BOT_TOKEN", "").strip()

ADMIN_ID = os.environ.get("ADMIN_ID", "30956754").strip()
INSTA_USER = os.environ.get("INSTA_USER", "shirazlastnews").strip() or "shirazlastnews"
INSTA_PASS = os.environ.get("INSTA_PASS", "").strip()
CHALLENGE_CODE_ENV = os.environ.get("CHALLENGE_CODE", "").strip()

IS_MANUAL_RUN = os.environ.get("GITHUB_EVENT_NAME", "") == "workflow_dispatch"
DUMP_NOW_INPUT = os.environ.get("DUMP_QUEUE_NOW", "no").strip().lower()
CLEAR_CMD_INPUT = os.environ.get("CLEAR_QUEUE", "no").strip().lower()

SESSION_FILE = "insta_session.json"
UUIDS_FILE = "device_uuids.json"
PUBLISHED_FILE = "published_tasks.json"
STATE_FILE = "worker_state.json"
QUEUE_FILE = "public_data/drafts/insta_queue.json"
PUBLIC_REPO_URL = "https://github.com/arpparnian-arch/shirazlastnewsbot.git"

MAX_ITEMS_PER_RUN = 2
# چرخه ۶۱ دقیقه‌ای برای جلوگیری کامل از حساسیت اینستاگرام
CYCLE_MINUTES = 61
# اجرای پیوسته ۲۴ دقیقه‌ای هماهنگ با کرون ۲۵ دقیقه‌ای گیت‌هاب
LOOP_DURATION_SECONDS = 24 * 60

STATE_LOCK = threading.Lock()
GIT_LOCK = threading.Lock()
KEEP_RUNNING = True
TELEGRAM_CHALLENGE_CODE = None
FORCE_RELOGIN_REQUESTED = False

# ربات اول (@shirazlastnews_bot): فقط برای گزارش‌های انتشار اینستاگرام
bot = telebot.TeleBot(TOKEN, threaded=False) if TOKEN else None
# دو کلاینت مجزا برای ربات دوم (@newswriting1_bot) جهت پاسخگویی آنی
backup_bot = telebot.TeleBot(BACKUP_BOT_TOKEN, threaded=False) if BACKUP_BOT_TOKEN else bot
listener_bot = telebot.TeleBot(BACKUP_BOT_TOKEN, threaded=False) if BACKUP_BOT_TOKEN else None


def to_persian_num(s):
    return str(s).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def norm_fa(s):
    if not s:
        return ""
    s = str(s).replace("ي", "ی").replace("ك", "ک").replace("\u200c", " ")
    return re.sub(r"\s+", " ", s).strip()


def load_worker_state():
    defaults = {
        "auto_upload_enabled": True,
        "sleep_start_hour": 0,
        "sleep_end_hour": 7,
        "backup_chat_id": ADMIN_ID,
        "backup_last_update_id": 0,
        "last_insta_publish_ts": 0,
        "daily_stats": {"date": "", "reels": 0, "stories": 0, "posts": 0}
    }
    with STATE_LOCK:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    defaults.update(data)
                    return defaults
            except Exception:
                pass
    return defaults


def save_worker_state(st):
    with STATE_LOCK:
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def load_published_list():
    with STATE_LOCK:
        if os.path.exists(PUBLISHED_FILE):
            try:
                with open(PUBLISHED_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
    return []


def save_published_list(pub_list):
    with STATE_LOCK:
        try:
            with open(PUBLISHED_FILE, "w", encoding="utf-8") as f:
                json.dump(pub_list[-1000:], f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def refresh_public_data():
    with GIT_LOCK:
        try:
            if not os.path.exists("public_data/.git"):
                shutil.rmtree("public_data", ignore_errors=True)
                subprocess.run(
                    ["git", "clone", "--depth", "1", PUBLIC_REPO_URL, "public_data"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25
                )
            else:
                res = subprocess.run(
                    ["git", "-C", "public_data", "pull", "--ff-only"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15
                )
                if res.returncode != 0:
                    shutil.rmtree("public_data", ignore_errors=True)
                    subprocess.run(
                        ["git", "clone", "--depth", "1", PUBLIC_REPO_URL, "public_data"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25
                    )
        except Exception as e:
            print("Refresh public_data warning:", e)


def get_backup_kb(auto_enabled=True):
    mk = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    mk.add(
        KeyboardButton("📥 دریافت همه موارد باقی‌مانده"),
        KeyboardButton("📊 وضعیت صف اینستاگرام")
    )
    mk.add(
        KeyboardButton("🎬 فقط ریلزهای مانده"),
        KeyboardButton("📱 فقط استوری‌های مانده")
    )
    mk.add(
        KeyboardButton("🖼 فقط پست‌های مانده"),
        KeyboardButton("📋 مشاهده و مدیریت ۵ مورد اول صف")
    )
    toggle_txt = "⏸ توقف آپلود خودکار پیج" if auto_enabled else "▶️ فعال‌سازی آپلود خودکار پیج"
    mk.add(
        KeyboardButton(toggle_txt),
        KeyboardButton("📈 کارنامه و آمار امروز")
    )
    mk.add(
        KeyboardButton("⚙️ تنظیم ساعات خاموشی"),
        KeyboardButton("🔐 ورود مجدد گوشی اندروید")
    )
    return mk


def notify_admin(msg):
    """ارسال گزارش‌های انتشار صرفاً به ربات اول (@shirazlastnews_bot)"""
    print(msg)
    if not bot or not ADMIN_ID:
        return
    try:
        safe_msg = msg[:4000] if len(msg) > 4000 else msg
        bot.send_message(ADMIN_ID, safe_msg, parse_mode="HTML")
    except Exception:
        try:
            plain = re.sub(r"<[^>]+>", "", msg)[:4000]
            bot.send_message(ADMIN_ID, plain)
        except Exception:
            pass


def notify_backup_bot(msg, with_kb=True):
    """ارسال پیام صرفاً به ربات دوم (@newswriting1_bot)"""
    print("[BackupBot]", msg)
    st = load_worker_state()
    target_cid = str(st.get("backup_chat_id") or ADMIN_ID)
    if not backup_bot or not target_cid:
        return
    kb = get_backup_kb(st.get("auto_upload_enabled", True)) if with_kb else None
    try:
        safe_msg = msg[:4000] if len(msg) > 4000 else msg
        backup_bot.send_message(target_cid, safe_msg, parse_mode="HTML", reply_markup=kb)
    except Exception:
        try:
            plain_msg = re.sub(r"<[^>]+>", "", msg)[:4000]
            backup_bot.send_message(target_cid, plain_msg, reply_markup=kb)
        except Exception as e:
            print("Backup bot send error:", e)


# ==========================================
# ۲. شبیه‌ساز واقعی گوشی اندروید (رفع کامل خطای Out of Date)
# ==========================================
def is_in_safety_cooldown():
    st = load_worker_state()
    until_ts = st.get("cooldown_until", 0)
    if time.time() < until_ts:
        rem_mins = int((until_ts - time.time()) // 60)
        print(f"Safety cooldown active for {rem_mins} more minutes.")
        return True
    return False


def activate_safety_cooldown(hours=3, reason=""):
    st = load_worker_state()
    st["cooldown_until"] = time.time() + (hours * 3600)
    st["reason"] = reason
    save_worker_state(st)


def clear_safety_cooldown():
    st = load_worker_state()
    if "cooldown_until" in st:
        st.pop("cooldown_until", None)
        st.pop("reason", None)
        save_worker_state(st)


# دریافت کلید رمزنگاری نسخه ۴ (#PWD_INSTAGRAM:4) با هدرهای رسمی اندروید
_orig_password_encrypt = getattr(Client, "password_encrypt", None)


def _safe_password_encrypt(self, password):
    try:
        hdrs = getattr(self, "base_headers", None) or {
            "User-Agent": getattr(
                self,
                "user_agent",
                "Instagram 317.0.0.34.109 Android (34/14; 480dpi; 1080x2400; Samsung; SM-S918B; dm3q; qcom; en_US; 562021275)"
            ),
            "X-IG-App-ID": "567067343352427",
            "X-IG-Capabilities": "3brTv10=",
            "Accept-Language": "en-US",
        }
        # ۱. بررسی هدرهای آخرین پاسخ دریافتی از سرور موبایل
        last_resp = getattr(self, "last_response", None)
        if last_resp is not None and hasattr(last_resp, "headers"):
            kid = last_resp.headers.get("ig-set-password-encryption-key-id")
            pub = last_resp.headers.get("ig-set-password-encryption-pub-key")
            if kid and pub and str(kid).strip().isdigit():
                self.password_publickeys = lambda k=int(str(kid).strip()), p=pub.strip(): (k, p)
                if _orig_password_encrypt:
                    return _orig_password_encrypt(self, password)

        # ۲. دریافت کلید رمزنگاری RSA با ارسال هدرهای واقعی اپلیکیشن اندروید
        uid_val = getattr(self, "uuid", None) or getattr(self, "phone_id", None) or ""
        for method, ep, payload in [
            ("POST", "https://i.instagram.com/api/v1/qe/sync/", {"id": uid_val, "experiments": ""}),
            ("POST", "https://i.instagram.com/api/v1/launcher/sync/", {"id": uid_val}),
            ("GET", "https://i.instagram.com/api/v1/zr/token/result/", None),
            ("GET", "https://i.instagram.com/api/v1/si/fetch_headers/?challenge_type=signup", None),
        ]:
            try:
                if method == "POST":
                    r = self.private.post(ep, data=payload, headers=hdrs, timeout=10)
                else:
                    r = self.private.get(ep, headers=hdrs, timeout=10)
                kid = r.headers.get("ig-set-password-encryption-key-id")
                pub = r.headers.get("ig-set-password-encryption-pub-key")
                if kid is not None and pub is not None and str(kid).strip().isdigit():
                    self.password_publickeys = lambda k=int(str(kid).strip()), p=pub.strip(): (k, p)
                    if _orig_password_encrypt:
                        return _orig_password_encrypt(self, password)
            except Exception:
                pass

        if _orig_password_encrypt:
            return _orig_password_encrypt(self, password)
    except Exception as e:
        print("Password encrypt notice:", e)
    return f"#PWD_INSTAGRAM:0:{int(time.time())}:{password}"


Client.password_encrypt = _safe_password_encrypt

# محافظت از توکن موبایلی اندروید در برابر پاک شدن در حین آپلود اسلایدهای آلبوم
_orig_send_private_request = getattr(Client, "_send_private_request", None)


def _locked_send_private_request(self, endpoint, *args, **kwargs):
    saved_auth = getattr(self, "_native_android_auth", None)
    saved_bearer = getattr(self, "_native_android_bearer", None)

    if saved_auth and saved_bearer:
        if not getattr(self, "authorization_data", None):
            self.authorization_data = dict(saved_auth)
        if not self.private.headers.get("Authorization"):
            self.private.headers["Authorization"] = saved_bearer

    try:
        return _orig_send_private_request(self, endpoint, *args, **kwargs)
    finally:
        cur_auth = getattr(self, "authorization_data", None) or {}
        if cur_auth.get("ds_user_id") and cur_auth.get("sessionid"):
            self._native_android_auth = dict(cur_auth)
            if self.private.headers.get("Authorization"):
                self._native_android_bearer = self.private.headers["Authorization"]
        elif saved_auth and saved_bearer:
            self.authorization_data = dict(saved_auth)
            self.private.headers["Authorization"] = saved_bearer


if _orig_send_private_request:
    Client._send_private_request = _locked_send_private_request


def configure_android_phone_identity(cl):
    """تثبیت هویت سخت‌افزاری گوشی اندروید با حفظ کامل نسخه جدید اپلیکیشن"""
    cl.delay_range = [3, 7]
    cl.country = "IR"
    cl.country_code = 98
    cl.locale = "en_US"
    cl.timezone_offset = 12600  # +03:30 Tehran

    if os.path.exists(UUIDS_FILE):
        try:
            uuids = json.load(open(UUIDS_FILE, "r", encoding="utf-8"))
            if isinstance(uuids, dict) and uuids.get("uuid"):
                cl.set_uuids(uuids)
                return
        except Exception:
            pass
    try:
        with open(UUIDS_FILE, "w", encoding="utf-8") as f:
            json.dump(cl.uuids, f, indent=2)
    except Exception:
        pass


def has_active_mobile_auth(cl):
    """تشخیص فوری دریافت شدن توکن سشن بلافاصله پس از تایید کد ۶ رقمی"""
    auth = getattr(cl, "authorization_data", None) or {}
    if auth.get("ds_user_id") and auth.get("sessionid"):
        return True
    try:
        sid_cookie = cl.private.cookies.get("sessionid")
        uid_cookie = cl.private.cookies.get("ds_user_id")
        if sid_cookie and ":" in str(sid_cookie):
            clean_sid = urllib.parse.unquote(str(sid_cookie).strip())
            uid_str = str(uid_cookie or clean_sid.split(":")[0]).strip()
            cl.authorization_data = {
                "ds_user_id": uid_str,
                "sessionid": clean_sid,
                "should_use_header_over_cookies": True,
            }
            return True
    except Exception:
        pass
    last_resp = getattr(cl, "last_response", None)
    if last_resp is not None and hasattr(last_resp, "headers"):
        ig_auth = last_resp.headers.get("ig-set-authorization", "")
        if ig_auth.startswith("Bearer IGT:2:"):
            b64_part = ig_auth.split("Bearer IGT:2:", 1)[1].strip()
            try:
                decoded = json.loads(base64.b64decode(b64_part).decode("utf-8"))
                if decoded.get("sessionid"):
                    cl.authorization_data = decoded
                    return True
            except Exception:
                pass
    return False


def lock_native_session_in_memory(cl):
    """قفل کردن توکن واقعی اندروید در حافظه و ذخیره تضمینی در insta_session.json"""
    try:
        has_active_mobile_auth(cl)
        settings = cl.get_settings()
        auth = cl.authorization_data or settings.get("authorization_data", {}) or {}
        if auth.get("ds_user_id") and auth.get("sessionid"):
            cl._native_android_auth = dict(auth)
            b64_payload = base64.b64encode(
                json.dumps(auth, separators=(",", ":")).encode("utf-8")
            ).decode("utf-8")
            bearer_str = cl.private.headers.get("Authorization") if hasattr(cl, "private") else None
            if not bearer_str or not str(bearer_str).startswith("Bearer IGT:2:"):
                bearer_str = f"Bearer IGT:2:{b64_payload}"
            cl._native_android_bearer = bearer_str
            cl.private.headers["Authorization"] = bearer_str

            settings["authorization_data"] = dict(auth)
            settings["uuids"] = cl.uuids
            with open(SESSION_FILE, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2)
            with open(UUIDS_FILE, "w", encoding="utf-8") as f:
                json.dump(cl.uuids, f, indent=2)
            print(f"Native Android session saved for user_id={auth.get('ds_user_id')}")
            return True
    except Exception as e:
        print("Save native session warning:", e)
    return False


def force_android_phone_login():
    """
    ورود مستقیم شبیه‌ساز گوشی اندروید با نسخه جدید (بدون فراخوانی درگاه قدیمی login_legacy)
    """
    global TELEGRAM_CHALLENGE_CODE
    if not INSTA_PASS:
        notify_backup_bot(
            "⚠️ <b>رمز عبور اینستاگرام (INSTA_PASS) در بخش Secrets گیت‌هاب تنظیم نشده است!</b>"
        )
        return None

    cl = Client()
    configure_android_phone_identity(cl)
    cl.init()

    def interactive_telegram_code_handler(username, choice=None):
        global TELEGRAM_CHALLENGE_CODE
        TELEGRAM_CHALLENGE_CODE = None
        notify_backup_bot(
            f"📲 <b>شبیه‌ساز گوشی اندروید در حال ورود به پیج <code>@{INSTA_USER}</code> است!</b>\n\n"
            f"🔐 اینستاگرام یک <b>کد تایید ۶ رقمی ({choice})</b> به ایمیل یا پیامک شما ارسال کرده است.\n"
            f"👈 <b>لطفاً همین الان (ظرف ۳ دقیقه) آن کد ۶ رقمی را در همین چت (@newswriting1_bot) بفرستید</b> تا ورود تکمیل شود:"
        )
        wait_start = time.time()
        while (time.time() - wait_start) < 180:
            if TELEGRAM_CHALLENGE_CODE:
                code_str = str(TELEGRAM_CHALLENGE_CODE).strip()
                TELEGRAM_CHALLENGE_CODE = None
                notify_backup_bot(f"✅ کد <code>{code_str}</code> دریافت شد! در حال ثبت و قفل کردن نشست اندروید...")
                return code_str
            time.sleep(1.2)

        return CHALLENGE_CODE_ENV or "807231"

    cl.challenge_code_handler = interactive_telegram_code_handler

    try:
        print(f"Simulating modern Android login for @{INSTA_USER}...")
        cl.login(INSTA_USER, INSTA_PASS)
        if lock_native_session_in_memory(cl):
            notify_backup_bot(
                "✅ <b>شبیه‌ساز گوشی اندروید با موفقیت وارد اینستاگرام شد!</b>\n"
                "🔒 نشست واقعی موبایل در <code>insta_session.json</code> قفل و ذخیره گردید و دیگر نیازی به لاگین مجدد نیست."
            )
            return cl
    except Exception as e1:
        # اگر کد ۶ رقمی تایید شده و توکن سشن دریافت شده باشد، بلافاصله آن را ذخیره کن
        if has_active_mobile_auth(cl) and lock_native_session_in_memory(cl):
            notify_backup_bot(
                "✅ <b>کد ۶ رقمی تایید شد و نشست شبیه‌ساز گوشی اندروید با موفقیت قفل و ذخیره گردید!</b>"
            )
            return cl

        err1 = str(e1)
        print("First Android login attempt notice:", err1)
        notify_backup_bot(
            f"📲 <b>درخواست ورود گوشی اندروید ارسال شد!</b>\n"
            f"پیام سرور: <code>{html.escape(err1[:220])}</code>\n\n"
            f"👈 <b>اگر در اپلیکیشن اینستاگرام روی گوشی اعلان «It's me / این من بودم» آمده، همین الان روی آن بزنید.</b>\n"
            f"⏳ ربات ۶۰ ثانیه منتظر تایید شما می‌ماند و سپس با همان شناسه گوشی وارد می‌شود..."
        )
        time.sleep(60)

        try:
            cl.login(INSTA_USER, INSTA_PASS)
            if lock_native_session_in_memory(cl):
                notify_backup_bot(
                    "✅ <b>ورود شبیه‌ساز گوشی اندروید پس از تایید شما با موفقیت قفل و ذخیره شد!</b>"
                )
                return cl
        except Exception as e2:
            if has_active_mobile_auth(cl) and lock_native_session_in_memory(cl):
                notify_backup_bot(
                    "✅ <b>نشست شبیه‌ساز گوشی اندروید با موفقیت دریافت و ذخیره شد!</b>"
                )
                return cl
            notify_backup_bot(
                f"⚠️ <b>ورود گوشی اندروید کامل نشد:</b>\n<code>{html.escape(str(e2)[:300])}</code>\n\n"
                f"💡 <i>هر زمان آماده بودید، دکمه «🔐 ورود مجدد گوشی اندروید» را در کیبورد پایین بزنید.</i>"
            )
            return None

    return cl if has_active_mobile_auth(cl) else None


def get_insta_client(force_relogin=False):
    """بارگذاری سشن واقعی اندروید و ورود خودکار با شبیه‌ساز گوشی در صورت نیاز"""
    if not force_relogin and os.path.exists(SESSION_FILE):
        try:
            saved_data = json.load(open(SESSION_FILE, "r", encoding="utf-8"))
            saved_auth = saved_data.get("authorization_data", {}) or {}
            if saved_auth.get("ds_user_id") and saved_auth.get("sessionid"):
                cl = Client()
                configure_android_phone_identity(cl)
                try:
                    cl.load_settings(SESSION_FILE, override_app_version=True)
                except TypeError:
                    cl.load_settings(SESSION_FILE)
                configure_android_phone_identity(cl)
                cl.init()
                lock_native_session_in_memory(cl)
                print(f"Loaded existing Native Android session for user {saved_auth.get('ds_user_id')}!")
                return cl
        except Exception as e:
            print("Saved session load notice:", e)

    return force_android_phone_login()


# ==========================================
# ۳. توابع آپلود مدیا و خواندن صف
# ==========================================
def get_task_priority(task_type):
    t = str(task_type).lower().strip()
    if t in ("reel", "video"):
        return 1
    elif t in ("story_photo", "story_video") or "story" in t:
        return 2
    elif t in ("album", "photo"):
        return 3
    return 4


def make_clean_cover_thumb(video_path):
    thumb_path = video_path + "_cover.jpg"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", video_path, "-ss", "00:00:02.0", "-vframes", "1", thumb_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15
        )
        if os.path.exists(thumb_path):
            return thumb_path
    except Exception:
        pass
    return None


def execute_upload_task(cl, t_type, real_files, caption):
    if t_type == "photo":
        cl.photo_upload(real_files[0], caption=caption)
    elif t_type == "album":
        if len(real_files) >= 2:
            cl.album_upload(real_files, caption=caption)
        else:
            cl.photo_upload(real_files[0], caption=caption)
    elif t_type == "story_photo":
        if HAS_STORY_POLL:
            try:
                poll = StoryPoll(
                    x=0.5, y=0.80, width=0.66, height=0.14,
                    question="نظر شما چیست؟",
                    tallies=[StoryTallie(text="عالیه 👍", count=0), StoryTallie(text="انتقاد دارم 👎", count=0)]
                )
                cl.photo_upload_to_story(real_files[0], polls=[poll])
                return
            except Exception:
                pass
        cl.photo_upload_to_story(real_files[0])
    elif t_type == "story_video":
        th = make_clean_cover_thumb(real_files[0])
        cl.video_upload_to_story(real_files[0], thumbnail=th)
    elif t_type in ("reel", "video"):
        th = make_clean_cover_thumb(real_files[0])
        try:
            cl.clip_upload(real_files[0], caption=caption, thumbnail=th)
        except Exception as e_clip:
            print("clip_upload notice, falling back to video_upload:", e_clip)
            cl.video_upload(real_files[0], caption=caption, thumbnail=th)


def get_current_pending_tasks(published):
    if not os.path.exists(QUEUE_FILE):
        return []
    try:
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            queue = json.load(f)
    except Exception:
        return []

    now_ts = int(time.time())
    MAX_AGE_SECONDS = 18 * 3600
    pending_tasks = []
    modified_pub = False

    for task in queue:
        raw_id = f"{task.get('type')}_{task.get('time')}_{','.join(task.get('files', []))}"
        task_id = hashlib.md5(raw_id.encode("utf-8")).hexdigest()
        if task_id in published:
            continue

        task_time = int(task.get("time") or 0)
        if not task.get("vip") and task_time > 0 and (now_ts - task_time) > MAX_AGE_SECONDS:
            published.append(task_id)
            modified_pub = True
            continue

        pending_tasks.append((task_id, task))

    if modified_pub:
        save_published_list(published)

    pending_tasks.sort(key=lambda item: get_task_priority(item[1].get("type", "")))
    return pending_tasks


# ==========================================
# ۴. ارسال فایل‌ها به تلگرام (@newswriting1_bot) با حذف گروهی پیام‌ها
# ==========================================
def deliver_tasks_to_telegram(tasks_to_send, published_list, title_header="تخلیه صف"):
    st = load_worker_state()
    target_cid = str(st.get("backup_chat_id") or ADMIN_ID)
    if not backup_bot or not target_cid:
        return 0

    iran_now = datetime.utcnow() + timedelta(hours=3, minutes=30)
    time_str = to_persian_num(iran_now.strftime("%H:%M"))

    if not tasks_to_send:
        notify_backup_bot(
            f"✅ <b>گزارش ({title_header} | ساعت {time_str}):</b>\n"
            f"در حال حاضر هیچ فایل منتشرنشده‌ای در صف نیست."
        )
        return 0

    header = (
        f"📦 <b>{title_header} | ساعت {time_str}</b>\n"
        f"تعداد موارد ارسالی: <b>{to_persian_num(len(tasks_to_send))}</b>\n"
        f"────────────────────\n"
        f"👇 <i>فایل‌ها به همراه کاور، فایل کپشن (.txt) و دکمه حذف یکجای پیام ارسال می‌شوند:</i>"
    )
    notify_backup_bot(header)
    time.sleep(1)

    sent_count = 0
    for idx, (task_id, task) in enumerate(tasks_to_send, 1):
        t_type = str(task.get("type", "")).lower().strip()
        rel_files = task.get("files", [])
        caption = (task.get("caption") or "").strip()

        real_files = []
        for rf in rel_files:
            p1 = os.path.join("public_data", rf)
            if os.path.exists(p1):
                real_files.append(p1)
            elif os.path.exists(rf):
                real_files.append(rf)

        if not real_files:
            if task_id not in published_list:
                published_list.append(task_id)
            continue

        num_fa = to_persian_num(idx)
        sent_msg_ids = []

        try:
            if t_type == "story_photo":
                del_kb = InlineKeyboardMarkup().add(
                    InlineKeyboardButton("✅ آپلود کردم (حذف پیام)", callback_data="delmsg|self")
                )
                with open(real_files[0], "rb") as f:
                    backup_bot.send_photo(
                        target_cid, f,
                        caption=f"📱 <b>مورد {num_fa} | استوری تصویری</b>\n✅ آماده قرار دادن در استوری",
                        parse_mode="HTML", reply_markup=del_kb
                    )
            elif t_type == "story_video":
                del_kb = InlineKeyboardMarkup().add(
                    InlineKeyboardButton("✅ آپلود کردم (حذف پیام)", callback_data="delmsg|self")
                )
                with open(real_files[0], "rb") as f:
                    backup_bot.send_video(
                        target_cid, f,
                        caption=f"📱 <b>مورد {num_fa} | استوری ویدیویی</b>",
                        parse_mode="HTML", supports_streaming=True, reply_markup=del_kb
                    )

            elif t_type in ("reel", "video"):
                with open(real_files[0], "rb") as f:
                    m1 = backup_bot.send_video(
                        target_cid, f,
                        caption=f"🎬 <b>مورد {num_fa} | ویدیوی ریلز</b>",
                        parse_mode="HTML", supports_streaming=True
                    )
                    if m1:
                        sent_msg_ids.append(str(m1.message_id))
                c_thumb = make_clean_cover_thumb(real_files[0])
                if c_thumb and os.path.exists(c_thumb):
                    with open(c_thumb, "rb") as c_f:
                        m2 = backup_bot.send_photo(
                            target_cid, c_f,
                            caption=f"🖼 <b>کاور پیشنهادی ریلز شماره {num_fa}</b>",
                            parse_mode="HTML"
                        )
                        if m2:
                            sent_msg_ids.append(str(m2.message_id))
                if caption:
                    cap_bytes = io.BytesIO(caption.encode("utf-8"))
                    cap_bytes.name = f"caption_reel_{idx}.txt"
                    m3 = backup_bot.send_document(target_cid, cap_bytes, caption=f"📝 فایل کپشن ریلز {num_fa}")
                    if m3:
                        sent_msg_ids.append(str(m3.message_id))

                ids_str = ",".join(sent_msg_ids[-4:])
                del_kb = InlineKeyboardMarkup().add(
                    InlineKeyboardButton("✅ آپلود کردم (حذف این مورد)", callback_data=f"delmsg|{ids_str}")
                )
                backup_bot.send_message(
                    target_cid,
                    f"📝 <b>کپشن ریلز {num_fa} (تک‌لمس برای کپی):</b>\n\n<code>{html.escape(caption or 'بدون کپشن')}</code>",
                    parse_mode="HTML", reply_markup=del_kb
                )

            elif t_type in ("photo", "album"):
                if len(real_files) >= 2:
                    try:
                        mg = []
                        for s_i, sp in enumerate(real_files):
                            with open(sp, "rb") as sf:
                                b_data = sf.read()
                            if s_i == 0:
                                mg.append(InputMediaPhoto(b_data, caption=f"🖼 <b>مورد {num_fa} | پست آلبوم ({to_persian_num(len(real_files))} اسلاید)</b>", parse_mode="HTML"))
                            else:
                                mg.append(InputMediaPhoto(b_data))
                        msgs = backup_bot.send_media_group(target_cid, mg)
                        if msgs:
                            for m_item in msgs:
                                sent_msg_ids.append(str(m_item.message_id))
                    except Exception:
                        for s_i, sp in enumerate(real_files, 1):
                            with open(sp, "rb") as sf:
                                m_s = backup_bot.send_photo(target_cid, sf, caption=f"اسلاید {to_persian_num(s_i)}")
                                if m_s:
                                    sent_msg_ids.append(str(m_s.message_id))
                else:
                    with open(real_files[0], "rb") as f:
                        m1 = backup_bot.send_photo(
                            target_cid, f,
                            caption=f"🖼 <b>مورد {num_fa} | پست تصویری</b>",
                            parse_mode="HTML"
                        )
                        if m1:
                            sent_msg_ids.append(str(m1.message_id))
                if caption:
                    cap_bytes = io.BytesIO(caption.encode("utf-8"))
                    cap_bytes.name = f"caption_post_{idx}.txt"
                    m2 = backup_bot.send_document(target_cid, cap_bytes, caption=f"📝 فایل کپشن پست {num_fa}")
                    if m2:
                        sent_msg_ids.append(str(m2.message_id))

                ids_str = ",".join(sent_msg_ids[-5:])
                del_kb = InlineKeyboardMarkup().add(
                    InlineKeyboardButton("✅ آپلود کردم (حذف این مورد)", callback_data=f"delmsg|{ids_str}")
                )
                backup_bot.send_message(
                    target_cid,
                    f"📝 <b>کپشن پست {num_fa} (تک‌لمس برای کپی):</b>\n\n<code>{html.escape(caption or 'بدون کپشن')}</code>",
                    parse_mode="HTML", reply_markup=del_kb
                )

            if task_id not in published_list:
                published_list.append(task_id)
            sent_count += 1
            time.sleep(1.5)
        except Exception as e:
            print(f"Error delivering task {idx}:", e)

    save_published_list(published_list)
    notify_backup_bot(f"✅ <b>ارسال کامل شد! مجموعاً {to_persian_num(sent_count)} مورد تحویل گردید.</b>")
    return sent_count


def execute_manual_dump_action(user_action, published):
    refresh_public_data()
    pending_tasks = get_current_pending_tasks(published)
    if user_action == "dump_reels":
        to_send = [it for it in pending_tasks if str(it[1].get("type", "")).lower() in ("reel", "video")]
        tag = "تحویل ریلزهای باقی‌مانده"
    elif user_action == "dump_stories":
        to_send = [it for it in pending_tasks if "story" in str(it[1].get("type", "")).lower()]
        tag = "تحویل استوری‌های باقی‌مانده"
    elif user_action == "dump_posts":
        to_send = [it for it in pending_tasks if str(it[1].get("type", "")).lower() in ("photo", "album")]
        tag = "تحویل پست‌های باقی‌مانده"
    else:
        to_send = pending_tasks
        tag = "تحویل کامل تمام فایل‌های صف"

    deliver_tasks_to_telegram(to_send, published, title_header=tag)


# ==========================================
# ۵. نخ اختصاصی ربات دوم تلگرام (@newswriting1_bot)
# ==========================================
def process_single_telegram_update(u, published):
    global TELEGRAM_CHALLENGE_CODE, FORCE_RELOGIN_REQUESTED
    if not listener_bot:
        return
    st = load_worker_state()

    if u.callback_query:
        cq = u.callback_query
        if not cq.message:
            return
        cid = cq.message.chat.id
        mid = cq.message.message_id
        data = cq.data or ""

        if data.startswith("delmsg|"):
            try:
                listener_bot.answer_callback_query(cq.id, "✅ پیام حذف شد.")
            except Exception:
                pass
            extra_ids = data.split("|", 1)[1] if "|" in data else ""
            if extra_ids and extra_ids != "self":
                for m_str in extra_ids.split(","):
                    if m_str.strip().isdigit():
                        try:
                            listener_bot.delete_message(cid, int(m_str.strip()))
                        except Exception:
                            pass
            try:
                listener_bot.delete_message(cid, mid)
            except Exception:
                pass
            return

        pending_tasks = get_current_pending_tasks(published)

        if data.startswith("q_del|"):
            target_hash = data.split("|")[1]
            if target_hash not in published:
                published.append(target_hash)
                save_published_list(published)
            try:
                listener_bot.answer_callback_query(cq.id, "🗑 آیتم از صف حذف گردید.")
                listener_bot.edit_message_text("✅ این آیتم از صف انتشار اینستاگرام حذف شد.", cid, mid)
            except Exception:
                pass

        elif data.startswith("q_vip|"):
            target_hash = data.split("|")[1]
            try:
                listener_bot.answer_callback_query(cq.id, "⚡️ در حال ارسال فایل...")
            except Exception:
                pass
            for tid, t_obj in pending_tasks:
                if tid == target_hash:
                    deliver_tasks_to_telegram([(tid, t_obj)], published, title_header="تحویل فوری آیتم انتخابی")
                    break

    elif u.message and u.message.chat:
        cid = str(u.message.chat.id)
        raw_txt = (u.message.text or "").strip()
        txt = norm_fa(raw_txt)

        if st.get("backup_chat_id") != cid:
            st["backup_chat_id"] = cid
            save_worker_state(st)

        # تشخیص فوری کد ۶ رقمی ایمیل/پیامک اینستاگرام
        digits_only = raw_txt.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")).strip()
        if re.fullmatch(r"\d{6}", digits_only):
            TELEGRAM_CHALLENGE_CODE = digits_only
            listener_bot.send_message(cid, f"🔐 کد ۶ رقمی <code>{digits_only}</code> دریافت و به شبیه‌ساز اندروید تحویل داده شد!", parse_mode="HTML")
            return

        if "ورود مجدد" in txt or txt in ("/login", "لاگین"):
            FORCE_RELOGIN_REQUESTED = True
            listener_bot.send_message(
                cid,
                "📱 <b>فرمان ورود با شبیه‌ساز گوشی اندروید صادر شد!</b>\n"
                "تا چند ثانیه دیگر فرآیند ورود آغاز می‌شود؛ اگر کد ۶ رقمی به ایمیلتان آمد، همان عدد را در همین چت بفرستید.",
                parse_mode="HTML"
            )
            return

        if txt.startswith("تنظیم ساعت"):
            tx_en = txt.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
            nums = re.findall(r"\d+", tx_en)
            if len(nums) >= 2:
                st["sleep_start_hour"] = int(nums[0]) % 24
                st["sleep_end_hour"] = int(nums[1]) % 24
                save_worker_state(st)
                listener_bot.send_message(
                    cid,
                    f"✅ ساعت خاموشی شبانه روی <b>{to_persian_num(st['sleep_start_hour'])} تا {to_persian_num(st['sleep_end_hour'])} صبح</b> تنظیم شد.",
                    parse_mode="HTML",
                    reply_markup=get_backup_kb(st.get("auto_upload_enabled", True))
                )
            return

        if txt in ("/start", "/menu", "منو", "شروع"):
            listener_bot.send_message(
                cid,
                "👋 <b>ربات مدیریت و شبیه‌ساز اندروید اینستاگرام فعال است! (پاسخگویی آنی ۲۴ ساعته ⚡️)</b>\n\n"
                "از دکمه‌های زیر می‌توانید در هر لحظه برای مدیریت صف، ورود اندروید و تحویل فایل‌ها استفاده کنید:",
                parse_mode="HTML",
                reply_markup=get_backup_kb(st.get("auto_upload_enabled", True))
            )

        elif "توقف آپلود" in txt:
            st["auto_upload_enabled"] = False
            save_worker_state(st)
            listener_bot.send_message(
                cid,
                "⏸ <b>آپلود خودکار پیج متوقف شد.</b>\nتا زمانی که مجدداً آن را فعال نکنید، پستی در اینستاگرام آپلود نمی‌شود.",
                parse_mode="HTML",
                reply_markup=get_backup_kb(False)
            )

        elif "فعال سازی آپلود" in txt:
            st["auto_upload_enabled"] = True
            save_worker_state(st)
            listener_bot.send_message(
                cid,
                f"▶️ <b>آپلود خودکار فعال شد</b> و طبق روال هر {to_persian_num(CYCLE_MINUTES)} دقیقه ادامه می‌یابد.",
                parse_mode="HTML",
                reply_markup=get_backup_kb(True)
            )

        elif "کارنامه" in txt or txt == "/stats":
            pending_tasks = get_current_pending_tasks(published)
            iran_now = datetime.utcnow() + timedelta(hours=3, minutes=30)
            now_str = to_persian_num(iran_now.strftime("%Y/%m/%d"))
            d_stats = st.get("daily_stats", {})
            if d_stats.get("date") != now_str:
                d_stats = {"date": now_str, "reels": 0, "stories": 0, "posts": 0}

            rem_reels = sum(1 for _, t in pending_tasks if str(t.get("type", "")).lower() in ("reel", "video"))
            rem_stories = sum(1 for _, t in pending_tasks if "story" in str(t.get("type", "")).lower())
            rem_posts = len(pending_tasks) - rem_reels - rem_stories

            stat_msg = (
                f"📈 <b>کارنامه و گزارش عملکرد امروز ({now_str}):</b>\n\n"
                f"✅ ریلزهای منتشرشده امروز: <b>{to_persian_num(d_stats.get('reels', 0))}</b>\n"
                f"✅ استوری‌های منتشرشده امروز: <b>{to_persian_num(d_stats.get('stories', 0))}</b>\n"
                f"✅ پست‌های منتشرشده امروز: <b>{to_persian_num(d_stats.get('posts', 0))}</b>\n"
                f"────────────────────\n"
                f"📦 <b>موارد باقیمانده فعلی در صف:</b>\n"
                f"• ریلز: {to_persian_num(rem_reels)} | استوری: {to_persian_num(rem_stories)} | پست: {to_persian_num(rem_posts)}"
            )
            listener_bot.send_message(cid, stat_msg, parse_mode="HTML", reply_markup=get_backup_kb(st.get("auto_upload_enabled", True)))

        elif "تنظیم ساعات خاموشی" in txt:
            listener_bot.send_message(
                cid,
                f"🕰 ساعت خاموشی فعلی: <b>{to_persian_num(st.get('sleep_start_hour', 0))} تا {to_persian_num(st.get('sleep_end_hour', 7))} صبح</b>\n\n"
                "برای تغییر، عبارتی مانند نمونه زیر بفرستید:\n"
                "<code>تنظیم ساعت 0 تا 7</code>",
                parse_mode="HTML",
                reply_markup=get_backup_kb(st.get("auto_upload_enabled", True))
            )

        elif "دریافت همه" in txt or "دریافت تمام" in txt or txt in ("/dump", "/all"):
            execute_manual_dump_action("dump_all", published)
        elif "فقط ریلز" in txt:
            execute_manual_dump_action("dump_reels", published)
        elif "فقط استوری" in txt:
            execute_manual_dump_action("dump_stories", published)
        elif "فقط پست" in txt:
            execute_manual_dump_action("dump_posts", published)

        elif "مشاهده و مدیریت" in txt:
            pending_tasks = get_current_pending_tasks(published)
            if not pending_tasks:
                listener_bot.send_message(
                    cid,
                    "✅ صف اینستاگرام در حال حاضر خالی است (۰ مورد در انتظار).",
                    reply_markup=get_backup_kb(st.get("auto_upload_enabled", True))
                )
                return

            listener_bot.send_message(cid, "📋 <b>۵ مورد اول صف انتظار اینستاگرام:</b>", parse_mode="HTML")
            for p_i, (t_id, task) in enumerate(pending_tasks[:5], 1):
                cap_short = (task.get("caption") or "بدون کپشن (استوری)").split("\n")[0][:45]
                t_t = task.get("type", "")
                item_txt = (
                    f"🔢 <b>ردیف اولویت: #{to_persian_num(p_i)}</b>\n"
                    f"📂 نوع: <code>{t_t}</code>\n"
                    f"📝 عنوان: {html.escape(cap_short)}..."
                )
                ikb = InlineKeyboardMarkup(row_width=2)
                ikb.add(
                    InlineKeyboardButton("📥 دریافت فوری همین مورد", callback_data=f"q_vip|{t_id}"),
                    InlineKeyboardButton("🗑 حذف از صف", callback_data=f"q_del|{t_id}")
                )
                listener_bot.send_message(cid, item_txt, parse_mode="HTML", reply_markup=ikb)

        elif "وضعیت" in txt or txt == "/status":
            pending_tasks = get_current_pending_tasks(published)
            r_cnt = sum(1 for _, t in pending_tasks if str(t.get("type", "")).lower() in ("reel", "video"))
            s_cnt = sum(1 for _, t in pending_tasks if "story" in str(t.get("type", "")).lower())
            p_cnt = len(pending_tasks) - r_cnt - s_cnt

            last_pub_ts = float(st.get("last_insta_publish_ts", 0))
            rem_wait = max(0, int(((CYCLE_MINUTES * 60) - (time.time() - last_pub_ts)) // 60))
            iran_now = datetime.utcnow() + timedelta(hours=3, minutes=30)
            if not st.get("auto_upload_enabled", True):
                next_str = "⏸ متوقف شده توسط شما"
            elif st.get("sleep_start_hour", 0) <= iran_now.hour < st.get("sleep_end_hour", 7):
                next_str = f"💤 استراحت شبانه تا {to_persian_num(st.get('sleep_end_hour', 7))} صبح"
            elif rem_wait == 0:
                next_str = "⚡️ در نوبت انتشار فوری"
            else:
                next_str = f"⏳ حدود {to_persian_num(rem_wait)} دقیقه دیگر"

            has_sess = "✅ متصل (گوشی اندروید)" if os.path.exists(SESSION_FILE) else "⚠️ نیازمند ورود"
            listener_bot.send_message(
                cid,
                f"📊 <b>وضعیت لحظه‌ای صف اینستاگرام:</b>\n\n"
                f"📱 وضعیت سشن: <b>{has_sess}</b>\n"
                f"🎬 ریلز: <b>{to_persian_num(r_cnt)}</b> | 📱 استوری: <b>{to_persian_num(s_cnt)}</b> | 🖼 پست: <b>{to_persian_num(p_cnt)}</b>\n"
                f"📦 مجموع کل در صف: <b>{to_persian_num(len(pending_tasks))} مورد</b>\n"
                f"🕒 نوبت بعدی آپلود خودکار: <b>{next_str}</b>",
                parse_mode="HTML",
                reply_markup=get_backup_kb(st.get("auto_upload_enabled", True))
            )


def telegram_listener_thread_func(published):
    if not listener_bot:
        return
    try:
        listener_bot.remove_webhook()
    except Exception:
        pass

    print("Telegram Instant Listener Thread started for @newswriting1_bot!")
    while KEEP_RUNNING:
        try:
            st = load_worker_state()
            last_up_id = int(st.get("backup_last_update_id", 0))
            updates = listener_bot.get_updates(
                offset=last_up_id + 1,
                timeout=3,
                allowed_updates=["message", "callback_query"]
            )
            if updates:
                for u in updates:
                    last_up_id = u.update_id
                    st = load_worker_state()
                    st["backup_last_update_id"] = last_up_id
                    save_worker_state(st)
                    try:
                        process_single_telegram_update(u, published)
                    except Exception as e_inner:
                        print("Update process warning:", e_inner)
        except Exception as e:
            print("Telegram polling warning:", e)
            time.sleep(2)
        time.sleep(0.8)


# ==========================================
# ۶. ساخت پیام گزارش و چرخه آپلود اینستاگرام
# ==========================================
def extract_safe_label(task):
    cap = (task.get("caption") or "").strip()
    if cap:
        for line in cap.split("\n"):
            clean_l = re.sub(r"[⭕️✅🛑📍🚗👈👇🔴🎬📸🖼]", "", line).strip()
            if clean_l:
                short_l = clean_l[:35] + ("..." if len(clean_l) > 35 else "")
                return html.escape(short_l)
    files = task.get("files", [])
    if files:
        fname = os.path.basename(files[0])
        if "match" in fname or "sport" in fname:
            return "استوری ورزشی / فوتبال"
        if "tourism" in fname:
            return "پست/استوری گردشگری"
        return html.escape(fname[:20])
    return "محتوای آماده"


def build_two_part_message(published_type, all_sorted_pending, published_list, current_run_ids):
    iran_now = datetime.utcnow() + timedelta(hours=3, minutes=30)
    now_clock = to_persian_num(iran_now.strftime("%H:%M"))

    part1 = (
        f"🚀 <b>اینستاگرام:</b> آیتم جدید (<code>{html.escape(str(published_type))}</code>) "
        f"با موفقیت در پیج <code>@shirazlastnews</code> منتشر شد!\n"
        f"🕰 <b>ساعت انتشار:</b> {now_clock}"
    )

    remaining = [(tid, t) for (tid, t) in all_sorted_pending if tid not in published_list]
    if not remaining:
        part2 = (
            "\n\n📊 <b>وضعیت صف انتشار اینستاگرام:</b>\n"
            "✅ <i>تمامی محتواهای موجود در صف منتشر شدند (۰ مورد در صف انتظار).</i>"
        )
        return part1 + part2

    reels_items, stories_items, posts_items = [], [], []
    future_idx = 0
    for tid, task in remaining:
        t_type = str(task.get("type", "")).lower().strip()
        label = extract_safe_label(task)

        if tid in current_run_ids:
            eta_str = "⚡️ در همین نوبت (تا ۱ دقیقه دیگر)"
        else:
            cycles_ahead = (future_idx // MAX_ITEMS_PER_RUN) + 1
            wait_mins = cycles_ahead * CYCLE_MINUTES
            target_dt = iran_now + timedelta(minutes=wait_mins)
            t_clock = to_persian_num(target_dt.strftime("%H:%M"))
            eta_str = f"⏳ حدود ساعت {t_clock} ({to_persian_num(wait_mins)} دقیقه دیگر)"
            future_idx += 1

        entry_line = f"  ▫️ {label}\n     └ {eta_str}"
        if t_type in ("reel", "video"):
            reels_items.append(entry_line)
        elif "story" in t_type:
            stories_items.append(entry_line)
        else:
            posts_items.append(entry_line)

    def format_group(items_list):
        if not items_list:
            return "  <i>موردی در صف نیست</i>"
        shown = items_list[:5]
        extra = len(items_list) - 5
        txt = "\n".join(shown)
        if extra > 0:
            txt += f"\n  <i>... و {to_persian_num(extra)} مورد دیگر در نوبت‌های بعدی</i>"
        return txt

    total_rem = len(remaining)
    part2 = (
        f"\n\n📋 <b>وضعیت صف انتشار ({to_persian_num(total_rem)} محتوا در نوبت):</b>\n"
        f"────────────────────\n"
        f"🥇 <b>اولویت ۱ | ریلز ({to_persian_num(len(reels_items))} عدد):</b>\n"
        f"{format_group(reels_items)}\n\n"
        f"🥈 <b>اولویت ۲ | استوری ({to_persian_num(len(stories_items))} عدد):</b>\n"
        f"{format_group(stories_items)}\n\n"
        f"🥉 <b>اولویت ۳ | پست ({to_persian_num(len(posts_items))} عدد):</b>\n"
        f"{format_group(posts_items)}"
    )
    return part1 + part2


def run_insta_publish_cycle(published):
    refresh_public_data()
    pending_tasks = get_current_pending_tasks(published)

    if not os.path.exists(SESSION_FILE):
        cl = get_insta_client(force_relogin=True)
        if not cl or not pending_tasks:
            return
    else:
        if not pending_tasks:
            print("Queue is empty. Skipping Instagram connection.")
            return
        cl = get_insta_client(force_relogin=False)
        if not cl:
            return

    if not IS_MANUAL_RUN:
        jitter_sec = random.randint(10, 35)
        time.sleep(jitter_sec)

    batch = pending_tasks[:MAX_ITEMS_PER_RUN]
    current_batch_ids = {tid for (tid, _) in batch}
    iran_now = datetime.utcnow() + timedelta(hours=3, minutes=30)

    for idx, (t_id, task) in enumerate(batch):
        if t_id in published:
            continue
        t_type = task.get("type", "")
        rel_files = task.get("files", [])
        caption = task.get("caption", "")

        real_files = []
        for rf in rel_files:
            p1 = os.path.join("public_data", rf)
            if os.path.exists(p1):
                real_files.append(p1)
            elif os.path.exists(rf):
                real_files.append(rf)

        if not real_files:
            published.append(t_id)
            current_batch_ids.discard(t_id)
            save_published_list(published)
            continue

        try:
            print(f"Publishing {t_type} -> {real_files[0]} ...")
            try:
                execute_upload_task(cl, t_type, real_files, caption)
            except Exception as first_err:
                if "login_required" in str(first_err).lower():
                    print("Session expired on server! Triggering fresh Native Android login...")
                    if os.path.exists(SESSION_FILE):
                        try:
                            os.remove(SESSION_FILE)
                        except Exception:
                            pass
                    cl = get_insta_client(force_relogin=True)
                    if not cl:
                        raise first_err
                    execute_upload_task(cl, t_type, real_files, caption)
                else:
                    raise first_err

            published.append(t_id)
            current_batch_ids.discard(t_id)
            lock_native_session_in_memory(cl)
            save_published_list(published)
            clear_safety_cooldown()

            st = load_worker_state()
            st["last_insta_publish_ts"] = time.time()
            today_str_fa = to_persian_num(iran_now.strftime("%Y/%m/%d"))
            d_st = st.get("daily_stats", {})
            if d_st.get("date") != today_str_fa:
                d_st = {"date": today_str_fa, "reels": 0, "stories": 0, "posts": 0}
            if t_type in ("reel", "video"):
                d_st["reels"] = d_st.get("reels", 0) + 1
            elif "story" in t_type:
                d_st["stories"] = d_st.get("stories", 0) + 1
            else:
                d_st["posts"] = d_st.get("posts", 0) + 1
            st["daily_stats"] = d_st
            save_worker_state(st)

            full_msg = build_two_part_message(t_type, pending_tasks, published, current_batch_ids)
            notify_admin(full_msg)

            if idx < len(batch) - 1 and current_batch_ids:
                human_pause = random.randint(60, 100)
                time.sleep(human_pause)

        except Exception as e:
            err_u = str(e)
            print(f"Upload error on {t_type}:", err_u)
            err_low = err_u.lower()
            if any(k in err_low for k in ["inactive", "challenge", "feedback_required", "spam", "checkpoint", "wait a few minutes"]):
                activate_safety_cooldown(hours=3, reason=err_u)
                notify_admin(
                    f"🛡 <b>محافظ هوشمند پیج فعال شد!</b>\n"
                    f"هنگام آپلود <code>{html.escape(str(t_type))}</code> پیام زیر دریافت شد:\n"
                    f"<code>{html.escape(err_u)}</code>\n\n"
                    f"⏳ ربات برای حفظ امنیت پیج <b>۳ ساعت</b> استراحت می‌کند."
                )
            else:
                notify_admin(f"⚠️ <b>خطا در آپلود {html.escape(str(t_type))}:</b>\n<code>{html.escape(err_u)}</code>")
            break


def main():
    global KEEP_RUNNING, FORCE_RELOGIN_REQUESTED
    refresh_public_data()
    published = load_published_list()

    tg_thread = threading.Thread(target=telegram_listener_thread_func, args=(published,), daemon=True)
    tg_thread.start()

    if CLEAR_CMD_INPUT in ("yes", "true", "1", "بله"):
        pending_tasks = get_current_pending_tasks(published)
        for tid, _ in pending_tasks:
            if tid not in published:
                published.append(tid)
        save_published_list(published)
        clear_safety_cooldown()
        notify_backup_bot("🧹 <b>پاکسازی کامل صف با موفقیت انجام شد!</b>")

    if DUMP_NOW_INPUT in ("yes", "true", "1", "بله"):
        execute_manual_dump_action("dump_all", published)

    if IS_MANUAL_RUN and backup_bot:
        pending_now = get_current_pending_tasks(published)
        notify_backup_bot(
            f"✅ <b>ربات @newswriting1_bot و شبیه‌ساز گوشی اندروید آماده است!</b>\n"
            f"📦 تعداد فایل‌های فعلی در صف: <b>{to_persian_num(len(pending_now))} مورد</b>"
        )

    if not os.path.exists(SESSION_FILE):
        force_android_phone_login()

    print("Starting 24-Minute Active Loop...")
    start_loop_ts = time.time()
    last_git_sync_ts = time.time()
    force_first_manual = IS_MANUAL_RUN

    while (time.time() - start_loop_ts) < LOOP_DURATION_SECONDS:
        if FORCE_RELOGIN_REQUESTED:
            FORCE_RELOGIN_REQUESTED = False
            if os.path.exists(SESSION_FILE):
                try:
                    os.remove(SESSION_FILE)
                except Exception:
                    pass
            force_android_phone_login()

        if (time.time() - last_git_sync_ts) >= 180:
            refresh_public_data()
            last_git_sync_ts = time.time()

        iran_now = datetime.utcnow() + timedelta(hours=3, minutes=30)
        today_date_key = iran_now.strftime("%Y-%m-%d")
        st = load_worker_state()

        sleep_h_start = int(st.get("sleep_start_hour", 0))
        sleep_h_end = int(st.get("sleep_end_hour", 7))
        is_sleep_hours = (sleep_h_start <= iran_now.hour < sleep_h_end)

        if is_sleep_hours:
            if st.get("last_midnight_dump_date") != today_date_key:
                refresh_public_data()
                pending_tasks = get_current_pending_tasks(published)
                deliver_tasks_to_telegram(
                    pending_tasks, published, title_header="تخلیه خودکار شبانه (ساعت خاموشی)"
                )
                st = load_worker_state()
                st["last_midnight_dump_date"] = today_date_key
                save_worker_state(st)
        else:
            if st.get("auto_upload_enabled", True):
                last_pub_ts = float(st.get("last_insta_publish_ts", 0))
                elapsed = time.time() - last_pub_ts
                if force_first_manual or (elapsed >= (CYCLE_MINUTES * 60)):
                    force_first_manual = False
                    if not is_in_safety_cooldown() or IS_MANUAL_RUN:
                        st["last_insta_publish_ts"] = time.time() - ((CYCLE_MINUTES - 5) * 60)
                        save_worker_state(st)
                        run_insta_publish_cycle(published)

        time.sleep(4)

    KEEP_RUNNING = False
    time.sleep(2)
    print("24-minute cycle finished cleanly.")


if __name__ == "__main__":
    main()
