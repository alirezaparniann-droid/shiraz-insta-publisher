import os
import re
import io
import json
import time
import html
import random
import base64
import hashlib
import subprocess
import urllib.parse
from datetime import datetime, timedelta
import telebot
from telebot.types import (
    InputMediaPhoto,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from instagrapi import Client

try:
    from instagrapi.types import StoryPoll, StoryTallie
    HAS_STORY_POLL = True
except Exception:
    HAS_STORY_POLL = False

# ==========================================
# ۱. تنظیمات پایه و توکن‌ها (نسخه نهایی ۶۱ دقیقه‌ای با قفل دائمی سشن)
# ==========================================
TOKEN = os.environ.get("BOT_TOKEN", "").strip()
BACKUP_BOT_TOKEN = os.environ.get("BACKUP_BOT_TOKEN", "").strip()

ADMIN_ID = os.environ.get("ADMIN_ID", "30956754").strip()
INSTA_USER = os.environ.get("INSTA_USER", "shirazlastnews").strip()
INSTA_PASS = os.environ.get("INSTA_PASS", "").strip()
CHALLENGE_CODE = os.environ.get("CHALLENGE_CODE", "").strip() or "807231"

RAW_SID = os.environ.get("IG_SESSIONID", "").strip()
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
# فاصله ۶۱ دقیقه‌ای بین هر نوبت آپلود برای جلوگیری کامل از حساسیت اینستاگرام
CYCLE_MINUTES = 61
# مدت زمان روشن ماندن پیوسته در هر نوبت گیت‌هاب (۵۰ دقیقه)
LOOP_DURATION_SECONDS = 50 * 60

# ربات اول (@shirazlastnews_bot): مخصوص گزارش‌های انتشار و وضعیت صف
bot = telebot.TeleBot(TOKEN) if TOKEN else None
# ربات دوم (@newswriting1_bot): صرفاً مخصوص دکمه‌های مدیریتی و تحویل فایل‌های باقی‌مانده
backup_bot = telebot.TeleBot(BACKUP_BOT_TOKEN) if BACKUP_BOT_TOKEN else bot


def to_persian_num(s):
    return str(s).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


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
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def load_published_list():
    if os.path.exists(PUBLISHED_FILE):
        try:
            with open(PUBLISHED_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_published_list(pub_list):
    try:
        with open(PUBLISHED_FILE, "w", encoding="utf-8") as f:
            json.dump(pub_list[-1000:], f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def refresh_public_data():
    """به‌روزرسانی لحظه‌ای صف از مخزن اول"""
    try:
        if not os.path.exists("public_data/.git"):
            subprocess.run(["rm", "-rf", "public_data"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(
                ["git", "clone", "--depth", "1", PUBLIC_REPO_URL, "public_data"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45
            )
        else:
            subprocess.run(
                ["git", "-C", "public_data", "fetch", "origin", "main"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30
            )
            subprocess.run(
                ["git", "-C", "public_data", "reset", "--hard", "origin/main"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15
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
    mk.add(KeyboardButton("⚙️ تنظیم ساعات خاموشی"))
    return mk


def notify_admin(msg):
    """ارسال گزارش‌های عادی آپلود و صف صرفاً به ربات اول (@shirazlastnews_bot)"""
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
# ۲. سیستم محافظ پیج (Cooldown) و قفل دائمی سشن (ضد خطای login_required)
# ==========================================
def is_in_safety_cooldown():
    st = load_worker_state()
    until_ts = st.get("cooldown_until", 0)
    if time.time() < until_ts:
        rem_mins = int((until_ts - time.time()) // 60)
        print(f"Safety cooldown active for {rem_mins} more minutes to protect account.")
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


_orig_password_encrypt = getattr(Client, "password_encrypt", None)


def _safe_password_encrypt(self, password):
    try:
        for ep in [
            "https://i.instagram.com/api/v1/si/fetch_headers/?challenge_type=signup",
            "https://i.instagram.com/api/v1/qe/sync/",
        ]:
            try:
                r = self.private.get(ep, timeout=10)
                kid = r.headers.get("ig-set-password-encryption-key-id")
                pub = r.headers.get("ig-set-password-encryption-pub-key")
                if kid is not None and pub is not None and str(kid).strip().isdigit():
                    self.password_publickeys = lambda: (int(str(kid).strip()), pub.strip())
                    if _orig_password_encrypt:
                        return _orig_password_encrypt(self, password)
            except Exception:
                pass
    except Exception:
        pass
    return f"#PWD_INSTAGRAM:0:{int(time.time())}:{password}"


Client.password_encrypt = _safe_password_encrypt

# پچ حیاتی برای جلوگیری از پاک شدن هدر Authorization بین اسلایدهای آلبوم و ریلز
_orig_send_private_request = getattr(Client, "_send_private_request", None)


def _locked_send_private_request(self, endpoint, *args, **kwargs):
    locked_bearer = getattr(self, "_locked_bearer_str", None)
    locked_auth = getattr(self, "_locked_auth_dict", None)
    locked_sid = getattr(self, "_locked_sid_str", None)
    locked_uid = getattr(self, "_locked_uid_str", None)

    if locked_bearer and locked_auth:
        self.authorization_data = dict(locked_auth)
        self.private.headers["Authorization"] = locked_bearer
        if locked_sid and locked_uid:
            self.private.cookies.set("sessionid", locked_sid, domain=".instagram.com")
            self.private.cookies.set("ds_user_id", locked_uid, domain=".instagram.com")

    try:
        return _orig_send_private_request(self, endpoint, *args, **kwargs)
    finally:
        # اگر اینستاگرام هدر خالی ig-set-authorization فرستاد، بلافاصله توکن قفل‌شده را برگردان
        if locked_bearer and locked_auth:
            cur_auth = getattr(self, "authorization_data", None) or {}
            if not cur_auth.get("sessionid"):
                self.authorization_data = dict(locked_auth)
                self.private.headers["Authorization"] = locked_bearer


if _orig_send_private_request:
    Client._send_private_request = _locked_send_private_request


def attach_persistent_uuids(cl):
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


def build_igt_bearer_header(uid_str, raw_colon_sid):
    auth_dict = {
        "ds_user_id": str(uid_str),
        "sessionid": raw_colon_sid,
        "should_use_header_over_cookies": True,
    }
    b64_payload = base64.b64encode(
        json.dumps(auth_dict, separators=(",", ":")).encode("utf-8")
    ).decode("utf-8")
    return auth_dict, f"Bearer IGT:2:{b64_payload}"


def save_valid_session(cl, fallback_sid=""):
    """ذخیره امن سشن بدون پاک شدن sessionid پس از آپلود"""
    try:
        settings = cl.get_settings()
        locked_auth = getattr(cl, "_locked_auth_dict", None) or {}
        auth = settings.get("authorization_data", {}) or {}

        raw_sid = (
            fallback_sid
            or getattr(cl, "_locked_sid_str", "")
            or locked_auth.get("sessionid", "")
            or auth.get("sessionid", "")
            or RAW_SID
        )
        clean_sid = urllib.parse.unquote(str(raw_sid).strip()).strip('"').strip("'").strip()

        ds_uid = (
            getattr(cl, "_locked_uid_str", "")
            or locked_auth.get("ds_user_id", "")
            or auth.get("ds_user_id", "")
            or "56014548029"
        )
        if ":" in clean_sid:
            ds_uid = clean_sid.split(":")[0].strip() or ds_uid
            auth["sessionid"] = clean_sid

        auth["ds_user_id"] = str(ds_uid)
        auth["should_use_header_over_cookies"] = True
        settings["authorization_data"] = auth

        if clean_sid and ":" in clean_sid:
            cookies = settings.get("cookies", {}) or {}
            cookies["sessionid"] = clean_sid
            cookies["ds_user_id"] = str(ds_uid)
            settings["cookies"] = cookies

        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
        print(f"Session saved and locked successfully (user_id={ds_uid}).")
        return True
    except Exception as e:
        print("Save session warning:", e)
    return False


def inject_mobile_sessionid(cl, sid_input):
    raw_colon_sid = urllib.parse.unquote(str(sid_input).strip()).strip('"').strip("'").strip()
    if not raw_colon_sid or ":" not in raw_colon_sid:
        return False
    uid_str = raw_colon_sid.split(":")[0].strip()
    if not uid_str.isdigit():
        return False

    attach_persistent_uuids(cl)
    auth_dict, bearer_str = build_igt_bearer_header(uid_str, raw_colon_sid)

    settings = cl.get_settings()
    settings["authorization_data"] = dict(auth_dict)
    settings["cookies"] = {
        "sessionid": raw_colon_sid,
        "ds_user_id": uid_str,
    }
    cl.set_settings(settings)
    cl.init()

    # قفل کردن دائمی توکن و کوکی روی کلاینت بدون دستکاری ویژگی‌های Read-Only
    cl._locked_sid_str = raw_colon_sid
    cl._locked_uid_str = uid_str
    cl._locked_auth_dict = dict(auth_dict)
    cl._locked_bearer_str = bearer_str

    cl.authorization_data = dict(auth_dict)
    cl.private.headers.update({"Authorization": bearer_str})
    cl.private.cookies.set("sessionid", raw_colon_sid, domain=".instagram.com")
    cl.private.cookies.set("ds_user_id", uid_str, domain=".instagram.com")
    return True


def get_insta_client(force_env_sid=False):
    last_err = ""
    clean_env_sid = urllib.parse.unquote(RAW_SID.strip()).strip('"').strip("'").strip() if RAW_SID else ""

    # ۱. بررسی فایل سشن ذخیره‌شده و تزریق کامل کوکی و هدر قفل‌شده
    if not force_env_sid and os.path.exists(SESSION_FILE):
        try:
            saved_data = json.load(open(SESSION_FILE, "r", encoding="utf-8"))
            saved_auth = saved_data.get("authorization_data", {}) or {}
            saved_cookies = saved_data.get("cookies", {}) or {}
            saved_sid = (
                saved_auth.get("sessionid")
                or saved_cookies.get("sessionid")
                or clean_env_sid
            )
            saved_sid = urllib.parse.unquote(str(saved_sid).strip()).strip('"').strip("'").strip()

            if saved_sid and ":" in saved_sid:
                cl = Client()
                cl.delay_range = [4, 9]
                attach_persistent_uuids(cl)
                try:
                    cl.load_settings(SESSION_FILE, override_app_version=True)
                except TypeError:
                    cl.load_settings(SESSION_FILE)
                if inject_mobile_sessionid(cl, saved_sid):
                    save_valid_session(cl, fallback_sid=saved_sid)
                    print(f"Loaded & locked existing session for user {cl._locked_uid_str}!")
                    return cl
        except Exception as e:
            last_err = f"Session file load notice: {e}"
            print(last_err)

    # ۲. ساخت و قفل سشن از متغیر IG_SESSIONID
    if clean_env_sid:
        try:
            cl = Client()
            cl.delay_range = [4, 9]
            if inject_mobile_sessionid(cl, clean_env_sid):
                save_valid_session(cl, fallback_sid=clean_env_sid)
                print("Injected and locked mobile session via IG_SESSIONID!")
                return cl
            else:
                last_err = "فرمت IG_SESSIONID نامعتبر است."
        except Exception as e:
            last_err = f"Session injection error: {e}"
            print(last_err)

    err_details = f"\n<code>{html.escape(last_err)}</code>" if last_err else ""
    notify_admin(f"⚠️ <b>سشن اینستاگرام در دسترس نیست یا منقضی شده است.</b>{err_details}")
    return None


# ==========================================
# ۳. توابع آپلود مدیا
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
# ۴. مدیریت تعاملی و لحظه‌ای ربات دوم (@newswriting1_bot)
# ==========================================
def handle_backup_bot_interactions(published):
    if not BACKUP_BOT_TOKEN or not backup_bot:
        return None

    st = load_worker_state()
    last_up_id = int(st.get("backup_last_update_id", 0))
    action_result = None

    try:
        updates = backup_bot.get_updates(offset=last_up_id + 1, timeout=2)
        if not updates:
            return None

        refresh_public_data()
        pending_tasks = get_current_pending_tasks(published)

        for u in updates:
            last_up_id = u.update_id

            if u.callback_query:
                cq = u.callback_query
                cid = cq.message.chat.id
                mid = cq.message.message_id
                data = cq.data or ""

                if data.startswith("delmsg|"):
                    try:
                        backup_bot.delete_message(cid, mid)
                        backup_bot.answer_callback_query(cq.id, "✅ پیام حذف شد.")
                    except Exception:
                        backup_bot.answer_callback_query(cq.id, "خطا در حذف پیام.")

                elif data.startswith("q_del|"):
                    target_hash = data.split("|")[1]
                    if target_hash not in published:
                        published.append(target_hash)
                        save_published_list(published)
                    backup_bot.answer_callback_query(cq.id, "🗑 آیتم از صف حذف گردید.")
                    backup_bot.send_message(cid, "✅ آیتم انتخابی از صف انتشار اینستاگرام حذف شد.")

                elif data.startswith("q_vip|"):
                    target_hash = data.split("|")[1]
                    for tid, t_obj in pending_tasks:
                        if tid == target_hash:
                            deliver_tasks_to_telegram([(tid, t_obj)], published, title_header="تحویل فوری آیتم انتخابی")
                            save_published_list(published)
                            break
                    backup_bot.answer_callback_query(cq.id, "⚡️ آیتم تحویل داده شد!")

            elif u.message and u.message.chat:
                cid = str(u.message.chat.id)
                txt = (u.message.text or "").strip()
                st["backup_chat_id"] = cid

                if txt.startswith("سشن:") or txt.startswith("/session"):
                    raw_sid = txt.replace("سشن:", "").replace("/session", "").strip()
                    if raw_sid:
                        cl_test = Client()
                        if inject_mobile_sessionid(cl_test, raw_sid):
                            save_valid_session(cl_test, fallback_sid=raw_sid)
                            backup_bot.send_message(cid, "✅ سشن جدید اینستاگرام با موفقیت ثبت، قفل و ذخیره شد!")
                        else:
                            backup_bot.send_message(cid, "⚠️ فرمت کد سشن اشتباه است.")
                    continue

                if txt.startswith("تنظیم ساعت"):
                    tx_en = txt.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
                    nums = re.findall(r"\d+", tx_en)
                    if len(nums) >= 2:
                        st["sleep_start_hour"] = int(nums[0]) % 24
                        st["sleep_end_hour"] = int(nums[1]) % 24
                        save_worker_state(st)
                        backup_bot.send_message(
                            cid,
                            f"✅ ساعت خاموشی شبانه روی <b>{to_persian_num(st['sleep_start_hour'])} تا {to_persian_num(st['sleep_end_hour'])} صبح</b> تنظیم شد.",
                            parse_mode="HTML"
                        )
                        continue

                if txt in ("/start", "/menu", "منو"):
                    backup_bot.send_message(
                        cid,
                        "👋 <b>ربات مدیریت و تحویل فایل‌های اینستاگرام فعال است! (پاسخگویی آنی ۲۴ ساعته ⚡️)</b>\n\n"
                        "از دکمه‌های زیر می‌توانید برای مدیریت صف و تحویل فایل‌ها استفاده کنید:",
                        parse_mode="HTML",
                        reply_markup=get_backup_kb(st.get("auto_upload_enabled", True))
                    )

                elif "توقف آپلود خودکار" in txt:
                    st["auto_upload_enabled"] = False
                    save_worker_state(st)
                    backup_bot.send_message(
                        cid,
                        "⏸ <b>آپلود خودکار پیج متوقف شد.</b>\nتا زمانی که مجدداً آن را فعال نکنید، پستی در اینستاگرام آپلود نمی‌شود.",
                        parse_mode="HTML",
                        reply_markup=get_backup_kb(False)
                    )

                elif "فعال‌سازی آپلود خودکار" in txt:
                    st["auto_upload_enabled"] = True
                    save_worker_state(st)
                    backup_bot.send_message(
                        cid,
                        f"▶️ <b>آپلود خودکار فعال شد</b> و طبق روال هر {to_persian_num(CYCLE_MINUTES)} دقیقه ادامه می‌یابد.",
                        parse_mode="HTML",
                        reply_markup=get_backup_kb(True)
                    )

                elif "کارنامه و آمار" in txt or txt == "/stats":
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
                    backup_bot.send_message(cid, stat_msg, parse_mode="HTML", reply_markup=get_backup_kb(st.get("auto_upload_enabled", True)))

                elif "تنظیم ساعات خاموشی" in txt:
                    backup_bot.send_message(
                        cid,
                        f"🕰 ساعت خاموشی فعلی: <b>{to_persian_num(st.get('sleep_start_hour', 0))} تا {to_persian_num(st.get('sleep_end_hour', 7))} صبح</b>\n\n"
                        "برای تغییر، عبارتی مانند نمونه زیر بفرستید:\n"
                        "<code>تنظیم ساعت 0 تا 7</code>",
                        parse_mode="HTML"
                    )

                elif "دریافت همه" in txt or "دریافت تمام" in txt:
                    action_result = "dump_all"
                elif "فقط ریلز" in txt:
                    action_result = "dump_reels"
                elif "فقط استوری" in txt:
                    action_result = "dump_stories"
                elif "فقط پست" in txt:
                    action_result = "dump_posts"

                elif "مشاهده و مدیریت" in txt:
                    if not pending_tasks:
                        backup_bot.send_message(cid, "✅ صف اینستاگرام در حال حاضر خالی است.")
                        continue

                    backup_bot.send_message(cid, "📋 <b>۵ مورد اول صف انتظار اینستاگرام:</b>", parse_mode="HTML")
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
                        backup_bot.send_message(cid, item_txt, parse_mode="HTML", reply_markup=ikb)

                elif "وضعیت" in txt or txt == "/status":
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

                    backup_bot.send_message(
                        cid,
                        f"📊 <b>وضعیت لحظه‌ای صف اینستاگرام:</b>\n\n"
                        f"🎬 ریلز: <b>{to_persian_num(r_cnt)}</b> | 📱 استوری: <b>{to_persian_num(s_cnt)}</b> | 🖼 پست: <b>{to_persian_num(p_cnt)}</b>\n"
                        f"📦 مجموع کل در صف: <b>{to_persian_num(len(pending_tasks))} مورد</b>\n"
                        f"🕒 نوبت بعدی آپلود خودکار: <b>{next_str}</b>",
                        parse_mode="HTML",
                        reply_markup=get_backup_kb(st.get("auto_upload_enabled", True))
                    )

        st["backup_last_update_id"] = last_up_id
        save_worker_state(st)
    except Exception as e:
        print("Backup bot error:", e)

    return action_result


# ==========================================
# ۵. ارسال فایل‌ها به تلگرام (@newswriting1_bot)
# ==========================================
def deliver_tasks_to_telegram(tasks_to_send, published_list, title_header="تخلیه صف"):
    st = load_worker_state()
    target_cid = str(st.get("backup_chat_id") or ADMIN_ID)
    if not backup_bot or not target_cid:
        return 0

    iran_now = datetime.utcnow() + timedelta(hours=3, minutes=30)
    time_str = to_persian_num(iran_now.strftime("%H:%M"))

    if not tasks_to_send:
        notify_backup_bot(f"✅ <b>{title_header} ({time_str}):</b>\nهیچ فایل منتشرنشده‌ای در این بخش وجود ندارد.")
        return 0

    header = (
        f"📦 <b>{title_header} | ساعت {time_str}</b>\n"
        f"تعداد موارد ارسالی: <b>{to_persian_num(len(tasks_to_send))}</b>\n"
        f"────────────────────\n"
        f"👇 <i>فایل‌ها به همراه کاور، فایل کپشن (.txt) و کپشن تک‌لمسی ارسال می‌شوند:</i>"
    )
    notify_backup_bot(header)
    time.sleep(2)

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
            published_list.append(task_id)
            continue

        num_fa = to_persian_num(idx)
        del_kb = InlineKeyboardMarkup().add(InlineKeyboardButton("✅ آپلود کردم (حذف پیام)", callback_data="delmsg|1"))

        try:
            if t_type == "story_photo":
                with open(real_files[0], "rb") as f:
                    backup_bot.send_photo(
                        target_cid, f,
                        caption=f"📱 <b>مورد {num_fa} | استوری تصویری</b>\n✅ آماده قرار دادن در استوری",
                        parse_mode="HTML", reply_markup=del_kb
                    )
            elif t_type == "story_video":
                with open(real_files[0], "rb") as f:
                    backup_bot.send_video(
                        target_cid, f,
                        caption=f"📱 <b>مورد {num_fa} | استوری ویدیویی</b>",
                        parse_mode="HTML", supports_streaming=True, reply_markup=del_kb
                    )

            elif t_type in ("reel", "video"):
                with open(real_files[0], "rb") as f:
                    backup_bot.send_video(
                        target_cid, f,
                        caption=f"🎬 <b>مورد {num_fa} | ویدیوی ریلز</b>",
                        parse_mode="HTML", supports_streaming=True
                    )
                c_thumb = make_clean_cover_thumb(real_files[0])
                if c_thumb and os.path.exists(c_thumb):
                    with open(c_thumb, "rb") as c_f:
                        backup_bot.send_photo(
                            target_cid, c_f,
                            caption=f"🖼 <b>کاور پیشنهادی ریلز شماره {num_fa}</b>"
                        )
                if caption:
                    cap_bytes = io.BytesIO(caption.encode("utf-8"))
                    cap_bytes.name = f"caption_reel_{idx}.txt"
                    backup_bot.send_document(target_cid, cap_bytes, caption=f"📝 فایل کپشن ریلز {num_fa}")
                    backup_bot.send_message(
                        target_cid,
                        f"📝 <b>کپشن ریلز {num_fa} (تک‌لمس برای کپی):</b>\n\n<code>{html.escape(caption)}</code>",
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
                        backup_bot.send_media_group(target_cid, mg)
                    except Exception:
                        for s_i, sp in enumerate(real_files, 1):
                            with open(sp, "rb") as sf:
                                backup_bot.send_photo(target_cid, sf, caption=f"اسلاید {to_persian_num(s_i)}")
                else:
                    with open(real_files[0], "rb") as f:
                        backup_bot.send_photo(
                            target_cid, f,
                            caption=f"🖼 <b>مورد {num_fa} | پست تصویری</b>",
                            parse_mode="HTML"
                        )
                if caption:
                    cap_bytes = io.BytesIO(caption.encode("utf-8"))
                    cap_bytes.name = f"caption_post_{idx}.txt"
                    backup_bot.send_document(target_cid, cap_bytes, caption=f"📝 فایل کپشن پست {num_fa}")
                    backup_bot.send_message(
                        target_cid,
                        f"📝 <b>کپشن پست {num_fa} (تک‌لمس برای کپی):</b>\n\n<code>{html.escape(caption)}</code>",
                        parse_mode="HTML", reply_markup=del_kb
                    )

            published_list.append(task_id)
            sent_count += 1
            time.sleep(2)
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


# ==========================================
# ۶. چرخه آپلود اینستاگرام و حلقه اصلی ۲۴ ساعته
# ==========================================
def run_insta_publish_cycle(published):
    refresh_public_data()
    pending_tasks = get_current_pending_tasks(published)
    if not pending_tasks:
        print("Queue is empty. Skipping Instagram connection.")
        return

    cl = get_insta_client()
    if not cl:
        return

    if not IS_MANUAL_RUN:
        jitter_sec = random.randint(10, 40)
        time.sleep(jitter_sec)

    batch = pending_tasks[:MAX_ITEMS_PER_RUN]
    current_batch_ids = {tid for (tid, _) in batch}
    iran_now = datetime.utcnow() + timedelta(hours=3, minutes=30)

    for idx, (t_id, task) in enumerate(batch):
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
                if "login_required" in str(first_err).lower() and RAW_SID:
                    print("Re-injecting fresh locked session from IG_SESSIONID and retrying upload...")
                    cl = get_insta_client(force_env_sid=True)
                    if not cl:
                        raise first_err
                    execute_upload_task(cl, t_type, real_files, caption)
                else:
                    raise first_err

            published.append(t_id)
            current_batch_ids.discard(t_id)
            save_valid_session(cl)
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

            # ارسال گزارش انتشار و وضعیت صف صرفاً به ربات اول (@shirazlastnews_bot)
            full_msg = build_two_part_message(t_type, pending_tasks, published, current_batch_ids)
            notify_admin(full_msg)

            if idx < len(batch) - 1 and current_batch_ids:
                human_pause = random.randint(60, 105)
                end_pause = time.time() + human_pause
                while time.time() < end_pause:
                    act = handle_backup_bot_interactions(published)
                    if act:
                        execute_manual_dump_action(act, published)
                        return
                    time.sleep(3)

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
    refresh_public_data()
    published = load_published_list()

    if CLEAR_CMD_INPUT in ("yes", "true", "1", "بله"):
        pending_tasks = get_current_pending_tasks(published)
        for tid, _ in pending_tasks:
            if tid not in published:
                published.append(tid)
        save_published_list(published)
        clear_safety_cooldown()
        notify_backup_bot("🧹 <b>پاکسازی کامل صف با موفقیت انجام شد!</b>")
        return

    if DUMP_NOW_INPUT in ("yes", "true", "1", "بله"):
        execute_manual_dump_action("dump_all", published)
        return

    print("Starting Always-On 50-Minute Loop (61-Min Instagram Cycle)...")
    start_loop_ts = time.time()
    force_first_manual = IS_MANUAL_RUN

    while (time.time() - start_loop_ts) < LOOP_DURATION_SECONDS:
        # ۱. بررسی لحظه‌ای دکمه‌های ربات دوم (@newswriting1_bot) در هر ۳ ثانیه
        user_action = handle_backup_bot_interactions(published)
        if user_action:
            execute_manual_dump_action(user_action, published)

        iran_now = datetime.utcnow() + timedelta(hours=3, minutes=30)
        today_date_key = iran_now.strftime("%Y-%m-%d")
        st = load_worker_state()

        sleep_h_start = int(st.get("sleep_start_hour", 0))
        sleep_h_end = int(st.get("sleep_end_hour", 7))
        is_sleep_hours = (sleep_h_start <= iran_now.hour < sleep_h_end)

        # ۲. تخلیه خودکار شبانه به ربات دوم در شروع ساعت خاموشی
        if is_sleep_hours:
            if st.get("last_midnight_dump_date") != today_date_key:
                refresh_public_data()
                pending_tasks = get_current_pending_tasks(published)
                deliver_tasks_to_telegram(
                    pending_tasks, published, title_header="تخلیه خودکار شبانه (شروع ساعت خاموشی)"
                )
                st["last_midnight_dump_date"] = today_date_key
                save_worker_state(st)
        else:
            # ۳. آپلود خودکار اینستاگرام طبق چرخه ۶۱ دقیقه‌ای
            if st.get("auto_upload_enabled", True):
                last_pub_ts = float(st.get("last_insta_publish_ts", 0))
                elapsed = time.time() - last_pub_ts
                if force_first_manual or (elapsed >= (CYCLE_MINUTES * 60)):
                    force_first_manual = False
                    if not is_in_safety_cooldown() or IS_MANUAL_RUN:
                        st["last_insta_publish_ts"] = time.time() - ((CYCLE_MINUTES - 5) * 60)
                        save_worker_state(st)
                        run_insta_publish_cycle(published)

        time.sleep(3)

    print("50-minute cycle finished cleanly.")


if __name__ == "__main__":
    main()
