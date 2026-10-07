import os
import re
import db  # database layer: hosted Postgres (Supabase) or local SQLite file
import datetime
import json
import base64
import random
import time
import threading
import smtplib
import hmac
import hashlib
from secrets import randbelow as _secure_randbelow, token_urlsafe as _token_urlsafe
import streamlit.components.v1 as components
from email.mime.text import MIMEText
import requests
import streamlit as st
from google import genai
from google.genai import types
from streamlit_autorefresh import st_autorefresh

from zoneinfo import ZoneInfo


st.set_page_config(page_title="StudySpace", layout="wide")

# Reminders are typed in by students/teachers as their own local wall-clock time
# (e.g. "1:02 PM"). Streamlit Community Cloud runs its containers in UTC, so
# comparing against a plain datetime.datetime.now() on the deployed app silently
# compares against the wrong clock. Pin "now" to the school's real timezone instead,
# so the due-soon banner fires at the same wall-clock time locally and once deployed.
APP_TIMEZONE = ZoneInfo("Europe/Tallinn")


# Config / Constants
ONESIGNAL_APP_ID = st.secrets.get("ONESIGNAL_APP_ID", "")
ONESIGNAL_REST_KEY = st.secrets.get("ONESIGNAL_REST_KEY", "")
EMAIL_ADDRESS = st.secrets.get("EMAIL_ADDRESS", "")
EMAIL_PASSWORD = st.secrets.get("EMAIL_PASSWORD", "")


TIER_LIMITS = {
   "freemium": 22,
   "pro": 100,
   "pro_plus": 200,
}


TIER_CONFIG = {
   "freemium": {"limit": 22, "sections": 4},
   "pro": {"limit": 100, "sections": 8},
   "pro_plus": {"limit": 200, "sections": 12},
}




# =========================================================
# DATABASE SETUP & USER PROFILES
# =========================================================
def init_db():
   conn = db.connect()
   cursor = conn.cursor()


   cursor.execute("""
       CREATE TABLE IF NOT EXISTS user_profile (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           name TEXT,
           email TEXT UNIQUE,
           purpose TEXT,
           interests TEXT,
           schedule TEXT,
           tier TEXT DEFAULT 'freemium',
           is_onboarded INTEGER DEFAULT 1,
           default_to_ai INTEGER DEFAULT 0,
           save_topic_memory INTEGER DEFAULT 1,
           role TEXT DEFAULT 'student',
           grade TEXT DEFAULT ''
       )
   """)


   cursor.execute("""
       CREATE TABLE IF NOT EXISTS study_logs (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           email TEXT,
           topic TEXT,
           timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
       )
   """)


   cursor.execute("""
       CREATE TABLE IF NOT EXISTS quiz_results (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           email TEXT,
           subject TEXT,
           score INTEGER,
           total INTEGER,
           timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
       )
   """)


   cursor.execute("""
       CREATE TABLE IF NOT EXISTS daily_rpd_usage (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           email TEXT,
           date_str TEXT,
           count INTEGER DEFAULT 0,
           UNIQUE(email, date_str)
       )
   """)


   cursor.execute("""
       CREATE TABLE IF NOT EXISTS class_tests (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           class_id INTEGER,
           test_title TEXT,
           subject TEXT,
           topic TEXT,
           test_date TEXT,
           notification_sent INTEGER DEFAULT 0
       )
   """)


   cursor.execute("""
       CREATE TABLE IF NOT EXISTS class_enrollment (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           class_id INTEGER,
           student_email TEXT,
           student_name TEXT DEFAULT '',
           status TEXT DEFAULT 'pending'
       )
   """)


   cursor.execute("""
       CREATE TABLE IF NOT EXISTS classes (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           teacher_email TEXT,
           class_name TEXT,
           join_code TEXT UNIQUE,
           created_at DATETIME DEFAULT CURRENT_TIMESTAMP
       )
   """)


   cursor.execute("""
       CREATE TABLE IF NOT EXISTS assignments (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           class_id INTEGER,
           title TEXT,
           topic TEXT,
           due_date TEXT,
           created_at DATETIME DEFAULT CURRENT_TIMESTAMP
       )
   """)


   cursor.execute("""
       CREATE TABLE IF NOT EXISTS reminders (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           user_email TEXT,
           title TEXT,
           due_at TEXT,
           notified_1h INTEGER DEFAULT 0,
           notified_10m INTEGER DEFAULT 0,
           created_at DATETIME DEFAULT CURRENT_TIMESTAMP
       )
   """)


   cursor.execute("""
       CREATE TABLE IF NOT EXISTS knowledge_items (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           email TEXT,
           subject TEXT,
           topic TEXT,
           specific_area TEXT,
           raw_text TEXT,
           timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
       )
   """)


   cursor.execute("""
       CREATE TABLE IF NOT EXISTS chat_sessions (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           email TEXT,
           title TEXT,
           created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
           updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
       )
   """)


   cursor.execute("""
       CREATE TABLE IF NOT EXISTS chat_messages (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           session_id INTEGER,
           role TEXT,
           content TEXT,
           image_data BLOB,
           mime_type TEXT,
           help_stage INTEGER,
           original_prompt TEXT,
           created_at DATETIME DEFAULT CURRENT_TIMESTAMP
       )
   """)


   # Lets a student dismiss a single Test Alert / Homework banner from their
   # home page (e.g. once they've prepared for the test or done the homework)
   # without deleting the underlying test/assignment for anyone else.
   cursor.execute("""
       CREATE TABLE IF NOT EXISTS login_tokens (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           token_hash TEXT UNIQUE,
           email TEXT,
           created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
           expires_at TEXT
       )
   """)

   cursor.execute("""
       CREATE TABLE IF NOT EXISTS notification_dismissals (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           student_email TEXT,
           notif_type TEXT,
           ref_id INTEGER,
           dismissed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
           UNIQUE(student_email, notif_type, ref_id)
       )
   """)


   def add_column_if_missing(table, column, col_type):
       cursor.execute(f"PRAGMA table_info({table})")
       existing_cols = [c[1] for c in cursor.fetchall()]
       if column not in existing_cols:
           cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")

   add_column_if_missing("class_enrollment", "student_name", "TEXT DEFAULT ''")
   add_column_if_missing("class_enrollment", "status", "TEXT DEFAULT 'pending'")
   add_column_if_missing("user_profile", "consent_at", "TEXT DEFAULT ''")

   # Older local databases were created before "email TEXT UNIQUE" was added to
   # the CREATE TABLE statement below — CREATE TABLE IF NOT EXISTS never retrofits
   # an existing table, so those DBs are missing the unique index that
   # "ON CONFLICT(email)" in save_user_profile() depends on. Add it if absent.
   cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_user_profile_email ON user_profile(email)")

   conn.commit()
   conn.close()




def store_knowledge_item(email, subject, topic, specific_area, raw_text):
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute(
       """
       INSERT INTO knowledge_items (email, subject, topic, specific_area, raw_text)
       VALUES (?, ?, ?, ?, ?)
   """,
       (email.lower().strip(), subject, topic, specific_area, raw_text),
   )
   conn.commit()
   conn.close()




def fetch_stored_topics(email, subject=None):
   conn = db.connect()
   cursor = conn.cursor()
   if subject:
       cursor.execute(
           """
           SELECT subject, topic, specific_area, raw_text, timestamp
           FROM knowledge_items
           WHERE LOWER(email) = ? AND LOWER(subject) LIKE ?
           ORDER BY timestamp DESC
       """,
           (email.lower().strip(), f"%{subject.lower().strip()}%"),
       )
   else:
       cursor.execute(
           """
           SELECT subject, topic, specific_area, raw_text, timestamp
           FROM knowledge_items
           WHERE LOWER(email) = ?
           ORDER BY timestamp DESC
       """,
           (email.lower().strip(),),
       )
   rows = cursor.fetchall()
   conn.close()
   return rows




def add_class_test(class_id, test_title, subject, topic, test_date):
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute(
       """
       INSERT INTO class_tests (class_id, test_title, subject, topic, test_date)
       VALUES (?, ?, ?, ?, ?)
   """,
       (class_id, test_title, subject, topic, test_date),
   )
   conn.commit()
   conn.close()




def get_current_rpd_count(email):
   conn = db.connect()
   cursor = conn.cursor()
   today_str = datetime.date.today().isoformat()
   cursor.execute(
       "SELECT count FROM daily_rpd_usage WHERE LOWER(email) = ? AND date_str = ?",
       (email.lower().strip(), today_str),
   )
   row = cursor.fetchone()
   conn.close()
   return row[0] if row else 0




def check_and_increment_rpd(email, user_tier):
   conn = db.connect()
   cursor = conn.cursor()
   today_str = datetime.date.today().isoformat()


   cursor.execute(
       "SELECT count FROM daily_rpd_usage WHERE LOWER(email) = ? AND date_str = ?",
       (email.lower().strip(), today_str),
   )
   row = cursor.fetchone()
   current_count = row[0] if row else 0


   max_limit = TIER_LIMITS.get(user_tier.lower(), 22)


   if current_count >= max_limit:
       conn.close()
       return False


   if row:
       cursor.execute(
           "UPDATE daily_rpd_usage SET count = count + 1 WHERE LOWER(email) = ? AND date_str = ?",
           (email.lower().strip(), today_str),
       )
   else:
       cursor.execute(
           "INSERT INTO daily_rpd_usage (email, date_str, count) VALUES (?, ?, 1)",
           (email.lower().strip(), today_str),
       )


   conn.commit()
   conn.close()
   return True




def generate_otp():
   return str(100000 + _secure_randbelow(900000))


OTP_MAX_ATTEMPTS = 5
OTP_TTL_SECONDS = 600


def otp_start(prefix):
   """Call whenever a fresh code is issued: starts the expiry clock and resets attempts."""
   st.session_state[f"{prefix}_otp_issued_at"] = time.time()
   st.session_state[f"{prefix}_otp_attempts"] = 0


def otp_check(prefix, user_code, expected):
   """Returns (ok, error_message). Codes expire after 10 minutes and allow 5 wrong tries."""
   if not expected:
       return False, "Request a new code first."
   issued = st.session_state.get(f"{prefix}_otp_issued_at", 0)
   if time.time() - issued > OTP_TTL_SECONDS:
       return False, "That code has expired. Please request a new one."
   attempts = st.session_state.get(f"{prefix}_otp_attempts", 0)
   if attempts >= OTP_MAX_ATTEMPTS:
       return False, "Too many wrong attempts. Please request a new code."
   st.session_state[f"{prefix}_otp_attempts"] = attempts + 1
   if hmac.compare_digest(user_code.strip(), str(expected)):
       return True, ""
   return False, "Invalid code. Please try again."


# =========================================================
# "STAY LOGGED IN" (remember-me cookie)
# =========================================================
# After a successful OTP login we give the browser a random token in a cookie and keep
# only its SHA-256 hash in the database. On the next visit the cookie is read, hashed and
# looked up; if it matches an unexpired row, the user is logged in without another OTP.
REMEMBER_COOKIE = "studyspace_token"
REMEMBER_DAYS = 30


def _hash_token(token):
   return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utc_now_naive():
   return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def create_login_token(email):
   token = _token_urlsafe(32)
   expires = _utc_now_naive() + datetime.timedelta(days=REMEMBER_DAYS)
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute(
       "INSERT INTO login_tokens (token_hash, email, expires_at) VALUES (?, ?, ?)",
       (_hash_token(token), email.lower().strip(), expires.isoformat()),
   )
   conn.commit()
   conn.close()
   return token


def lookup_login_token(token):
   """Returns the email this token belongs to, or None if unknown/expired."""
   if not token:
       return None
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute(
       "SELECT email, expires_at FROM login_tokens WHERE token_hash = ?",
       (_hash_token(token),),
   )
   row = cursor.fetchone()
   if row:
       try:
           expired = datetime.datetime.fromisoformat(row[1]) < _utc_now_naive()
       except (TypeError, ValueError):
           expired = True
       if expired:
           cursor.execute("DELETE FROM login_tokens WHERE token_hash = ?", (_hash_token(token),))
           conn.commit()
           row = None
   conn.close()
   return row[0] if row else None


def delete_login_token(token):
   if not token:
       return
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute("DELETE FROM login_tokens WHERE token_hash = ?", (_hash_token(token),))
   conn.commit()
   conn.close()


def count_login_tokens():
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute("SELECT COUNT(*) FROM login_tokens")
   n = cursor.fetchone()[0]
   conn.close()
   return n


def read_remember_cookie():
   try:
       value = st.context.cookies.get(REMEMBER_COOKIE)
   except Exception:
       return None
   return value if isinstance(value, str) and value else None


# Streamlit Community Cloud does not forward our cookie to st.context.cookies, so we also
# read it in the browser through a tiny component (cookie_bridge/index.html).
try:
   _cookie_bridge = components.declare_component(
       "studyspace_cookie_bridge",
       path=os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookie_bridge"),
   )
except Exception:
   _cookie_bridge = None


def read_bridge_token():
   if _cookie_bridge is None:
       return None
   try:
       value = _cookie_bridge(key="cookie_bridge", default=None)
   except Exception:
       return None
   token = value.get("token") if isinstance(value, dict) else None
   return token if isinstance(token, str) and token else None


def start_remembered_session(email):
   """Call right after a successful login: queues a cookie to be written to the browser."""
   st.session_state["_remember_token"] = create_login_token(email)


def emit_cookie_script():
   """Writes or clears the remember-me cookie in the browser (one-shot, invisible)."""
   # The cookie is re-written on every run while logged in (idempotent). Writing it only
   # once was fragile: if another rerun fired before the invisible iframe loaded, the
   # cookie was never set.
   token = st.session_state.get("_remember_token") if st.session_state.get("active_email") else None
   clear = st.session_state.pop("_clear_cookie", False)
   if token:
       max_age = REMEMBER_DAYS * 24 * 3600
       js = (
           f"var s = location.protocol === 'https:' ? '; Secure' : '';"
           f"document.cookie = '{REMEMBER_COOKIE}={token}; path=/; max-age={max_age}; SameSite=Lax' + s;"
       )
   elif clear:
       js = (
           f"var s = location.protocol === 'https:' ? '; Secure' : '';"
           f"document.cookie = '{REMEMBER_COOKIE}=; path=/; max-age=0; SameSite=Lax' + s;"
       )
   else:
       return
   components.html(f"<script>{js}</script>", height=0)




def send_otp_email(to_email, otp_code):
   """Sends a real OTP email via Gmail SMTP. Returns True only if the send actually succeeded."""
   if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
       st.session_state["last_otp_error"] = (
           "EMAIL_ADDRESS / EMAIL_PASSWORD are not set in secrets.toml — no email can be sent."
       )
       return False

   try:
       msg = MIMEText(
           f"Your StudySpace verification code is: {otp_code}\n\n"
           f"If you didn't request this, you can ignore this email."
       )
       msg["Subject"] = "Your StudySpace verification code"
       msg["From"] = EMAIL_ADDRESS
       msg["To"] = to_email

       with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
           server.starttls()
           server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
           server.sendmail(EMAIL_ADDRESS, to_email, msg.as_string())
       return True
   except Exception as e:
       st.session_state["last_otp_error"] = f"Couldn't send the OTP email: {e}"
       return False




def save_user_profile(
   name,
   email,
   purpose,
   interests,
   schedule,
   role="student",
   grade="",
   tier="freemium",
):
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute(
       """
       INSERT INTO user_profile (name, email, purpose, interests, schedule, role, grade, tier, is_onboarded)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
       ON CONFLICT(email) DO UPDATE SET
           name=excluded.name,
           purpose=excluded.purpose,
           interests=excluded.interests,
           schedule=excluded.schedule,
           role=excluded.role,
           grade=excluded.grade,
           tier=excluded.tier,
           is_onboarded=1
   """,
       (
           name,
           email.lower().strip(),
           purpose,
           interests,
           schedule,
           role,
           grade,
           tier,
       ),
   )
   conn.commit()
   conn.close()




def update_default_to_ai(email, val):
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute(
       "UPDATE user_profile SET default_to_ai = ? WHERE LOWER(email) = ?",
       (1 if val else 0, email.lower().strip()),
   )
   conn.commit()
   conn.close()




def update_save_topic_memory(email, val):
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute(
       "UPDATE user_profile SET save_topic_memory = ? WHERE LOWER(email) = ?",
       (1 if val else 0, email.lower().strip()),
   )
   conn.commit()
   conn.close()




def fetch_all_profiles():
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute(
       "SELECT name, email, purpose, interests, schedule, tier, is_onboarded, default_to_ai, save_topic_memory, role, grade FROM user_profile"
   )
   rows = cursor.fetchall()
   conn.close()
   profiles = []
   for row in rows:
       profiles.append(
           {
               "name": row[0],
               "email": row[1],
               "purpose": row[2],
               "interests": row[3],
               "schedule": row[4],
               "tier": row[5] or "freemium",
               "is_onboarded": bool(row[6]),
               "default_to_ai": bool(row[7]),
               "save_topic_memory": (
                   bool(row[8]) if row[8] is not None else True
               ),
               "role": row[9] or "student",
               "grade": row[10] or "",
           }
       )
   return profiles




def fetch_user_profile(email=None):
   if not email:
       return None
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute(
       "SELECT name, email, purpose, interests, schedule, tier, is_onboarded, default_to_ai, save_topic_memory, role, grade FROM user_profile WHERE LOWER(email) = ? LIMIT 1",
       (email.lower().strip(),),
   )
   row = cursor.fetchone()
   conn.close()


   if row:
       return {
           "name": row[0],
           "email": row[1],
           "purpose": row[2],
           "interests": row[3],
           "schedule": row[4],
           "tier": row[5] or "freemium",
           "is_onboarded": bool(row[6]),
           "default_to_ai": bool(row[7]),
           "save_topic_memory": bool(row[8]) if row[8] is not None else True,
           "role": row[9] or "student",
           "grade": row[10] or "",
       }
   return None




def log_session(topic, email):
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute(
       "INSERT INTO study_logs (email, topic) VALUES (?, ?)", (email, topic)
   )
   conn.commit()
   conn.close()




def fetch_logs(email=None):
   conn = db.connect()
   cursor = conn.cursor()
   if email:
       cursor.execute(
           "SELECT topic, timestamp FROM study_logs WHERE LOWER(email) = ? ORDER BY timestamp DESC",
           (email.lower().strip(),),
       )
   else:
       cursor.execute(
           "SELECT topic, timestamp FROM study_logs ORDER BY timestamp DESC"
       )
   rows = cursor.fetchall()
   conn.close()
   return rows




# =========================================================
# PERSISTED CHAT HISTORY ("RECENT CHATS")
# =========================================================
def create_chat_session(email, title):
   conn = db.connect()
   cursor = conn.cursor()
   clean_title = (title or "New chat").strip()[:80] or "New chat"
   cursor.execute(
       "INSERT INTO chat_sessions (email, title) VALUES (?, ?)",
       (email.lower().strip(), clean_title),
   )
   session_id = cursor.lastrowid
   conn.commit()
   conn.close()
   return session_id


def touch_chat_session(session_id):
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute(
       "UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
       (session_id,),
   )
   conn.commit()
   conn.close()


def save_chat_message(session_id, role, content, image_bytes=None, mime_type=None,
                       help_stage=None, original_prompt=None):
   if not session_id:
       return
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute(
       """
       INSERT INTO chat_messages (session_id, role, content, image_data, mime_type, help_stage, original_prompt)
       VALUES (?, ?, ?, ?, ?, ?, ?)
       """,
       (session_id, role, content, image_bytes, mime_type, help_stage, original_prompt),
   )
   conn.commit()
   conn.close()
   touch_chat_session(session_id)


def get_recent_chat_sessions(email, limit=10):
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute(
       "SELECT id, title, updated_at FROM chat_sessions WHERE LOWER(email) = ? ORDER BY updated_at DESC LIMIT ?",
       (email.lower().strip(), limit),
   )
   rows = cursor.fetchall()
   conn.close()
   return rows


def get_chat_session_messages(session_id):
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute(
       """
       SELECT role, content, image_data, mime_type, help_stage, original_prompt
       FROM chat_messages WHERE session_id = ? ORDER BY id ASC
       """,
       (session_id,),
   )
   rows = cursor.fetchall()
   conn.close()

   messages = []
   for role, content, image_data, mime_type, help_stage, original_prompt in rows:
       msg = {"role": role, "content": content}
       if image_data:
           msg["image_bytes"] = image_data
       if mime_type:
           msg["mime_type"] = mime_type
       if help_stage is not None:
           msg["help_stage"] = help_stage
       if original_prompt:
           msg["original_prompt"] = original_prompt
       messages.append(msg)
   return messages


def delete_chat_session(session_id):
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
   cursor.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
   conn.commit()
   conn.close()


def start_new_chat_session(email, seed_messages, title=None):
   """Creates a persisted chat session from a freshly-built messages list (already about
   to be assigned to st.session_state['messages']), saves every message in it, marks the
   new session as the active one, and returns its id."""
   if not title:
       for m in seed_messages:
           if m.get("role") == "user" and m.get("content"):
               title = m["content"]
               break
   if not title:
       title = seed_messages[0]["content"] if seed_messages else "New chat"

   session_id = create_chat_session(email, title)
   for m in seed_messages:
       save_chat_message(
           session_id,
           m.get("role", "user"),
           m.get("content", ""),
           image_bytes=m.get("image_bytes"),
           mime_type=m.get("mime_type"),
           help_stage=m.get("help_stage"),
           original_prompt=m.get("original_prompt"),
       )
   st.session_state["current_chat_session_id"] = session_id
   return session_id


def resume_chat_session(session_id):
   st.session_state["messages"] = get_chat_session_messages(session_id)
   st.session_state["current_chat_session_id"] = session_id
   st.session_state["page"] = "AI Tutor"
   st.rerun()


def fetch_quiz_results(email=None):
   conn = db.connect()
   cursor = conn.cursor()
   if email:
       cursor.execute(
           "SELECT subject, score, total, timestamp FROM quiz_results WHERE LOWER(email) = ? ORDER BY timestamp DESC",
           (email.lower().strip(),),
       )
   else:
       cursor.execute(
           "SELECT subject, score, total, timestamp FROM quiz_results ORDER BY timestamp DESC"
       )
   rows = cursor.fetchall()
   conn.close()
   return rows




def get_logo_path():
   user_home = os.path.expanduser("~")
   script_dir = os.path.dirname(os.path.abspath(__file__))


   possible_paths = [
       os.path.join(script_dir, "logo.png"),
       os.path.join(user_home, "studyspace", "logo.png"),
       os.path.join(os.getcwd(), "logo.png"),
   ]


   for path in possible_paths:
       if os.path.exists(path):
           return path
   return None




def get_base64_image(image_path):
   if not image_path or not os.path.exists(image_path):
       return ""
   with open(image_path, "rb") as img_file:
       return base64.b64encode(img_file.read()).decode()




# =========================================================
# ONESIGNAL PUSH NOTIFICATIONS & TEST CHECKER
# =========================================================
def send_onesignal_notification(email, title, message):
   if not ONESIGNAL_REST_KEY:
       return
   headers = {
       "Content-Type": "application/json; charset=utf-8",
       "Authorization": f"Basic {ONESIGNAL_REST_KEY}",
   }
   payload = {
       "app_id": ONESIGNAL_APP_ID,
       "filters": [{"field": "tag", "key": "email", "relation": "=", "value": email.lower().strip()}],
       "headings": {"en": title},
       "contents": {"en": message},
   }
   try:
       requests.post("https://onesignal.com/api/v1/notifications", headers=headers, json=payload)
   except Exception:
       pass




def check_and_notify_upcoming_tests(student_email):
   """Checks database for upcoming tests within 3 days and prompts notification."""
   conn = db.connect()
   cursor = conn.cursor()
   today_str = datetime.date.today().isoformat()
   three_days_later = (datetime.date.today() + datetime.timedelta(days=3)).isoformat()


   cursor.execute(
       """
       SELECT t.id, t.test_title, t.subject, t.topic, t.test_date
       FROM class_tests t
       JOIN class_enrollment e ON t.class_id = e.class_id
       WHERE LOWER(e.student_email) = ? AND e.status = 'approved'
       AND t.test_date >= ? AND t.test_date <= ? AND t.notification_sent = 0
   """,
       (student_email.lower().strip(), today_str, three_days_later),
   )
   upcoming = cursor.fetchall()


   for test_id, title, subject, topic, test_date in upcoming:
       msg = f"Hey! You've got a test on {subject} ({topic}) coming up on {test_date}. Would you like me to generate a mock test or study guide from your saved notes?"
       send_onesignal_notification(student_email, f"Upcoming Test: {title}", msg)
       cursor.execute("UPDATE class_tests SET notification_sent = 1 WHERE id = ?", (test_id,))


   conn.commit()
   conn.close()
   return upcoming


def get_upcoming_tests_for_display(student_email):
   """Every not-yet-passed test for this student's classes, minus any the
   student has dismissed from their home page. Unlike check_and_notify_upcoming_tests
   (which is one-shot, for firing the push notification), this is what actually
   drives the home page banner, so it stays visible until dismissed."""
   conn = db.connect()
   cursor = conn.cursor()
   today_str = datetime.date.today().isoformat()
   cursor.execute(
       """
       SELECT t.id, t.test_title, t.subject, t.topic, t.test_date
       FROM class_tests t
       JOIN class_enrollment e ON t.class_id = e.class_id
       WHERE LOWER(e.student_email) = ? AND e.status = 'approved'
       AND t.test_date >= ?
       AND t.id NOT IN (
           SELECT ref_id FROM notification_dismissals
           WHERE LOWER(student_email) = ? AND notif_type = 'test'
       )
       ORDER BY t.test_date ASC
       """,
       (student_email.lower().strip(), today_str, student_email.lower().strip()),
   )
   rows = cursor.fetchall()
   conn.close()
   return rows


def dismiss_notification(student_email, notif_type, ref_id):
   """Marks a single test/homework banner as dismissed for this student only."""
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute(
       "INSERT OR IGNORE INTO notification_dismissals (student_email, notif_type, ref_id) VALUES (?, ?, ?)",
       (student_email.lower().strip(), notif_type, ref_id),
   )
   conn.commit()
   conn.close()




# =========================================================
# CLASS CREATION / JOIN-CODE / APPROVAL / HOMEWORK ENGINE
# =========================================================
def generate_join_code():
   return str(random.randint(100000, 999999))


def create_class(teacher_email, class_name):
   """Creates a class for this teacher and returns a fresh unique 6-digit join code.
   Returns (ok, result) — result is the join code on success, or an error message on failure."""
   conn = db.connect()
   cursor = conn.cursor()

   cursor.execute(
       "SELECT id FROM classes WHERE LOWER(teacher_email) = ? AND LOWER(class_name) = ?",
       (teacher_email.lower().strip(), class_name.strip().lower()),
   )
   if cursor.fetchone():
       conn.close()
       return False, "A class with this name already exists."

   for _ in range(25):
       code = generate_join_code()
       try:
           cursor.execute(
               "INSERT INTO classes (teacher_email, class_name, join_code) VALUES (?, ?, ?)",
               (teacher_email.lower().strip(), class_name.strip(), code),
           )
           conn.commit()
           conn.close()
           return True, code
       except db.IntegrityError:
           continue
   conn.close()
   return False, "Couldn't generate a join code, try again."


def get_teacher_classes(teacher_email):
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute(
       "SELECT id, class_name, join_code FROM classes WHERE LOWER(teacher_email) = ? ORDER BY created_at DESC",
       (teacher_email.lower().strip(),),
   )
   rows = cursor.fetchall()
   conn.close()
   return rows


def get_class_by_code(join_code):
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute(
       "SELECT id, teacher_email, class_name FROM classes WHERE join_code = ?",
       (join_code.strip(),),
   )
   row = cursor.fetchone()
   conn.close()
   return row


def request_join_class(student_email, student_name, join_code):
   """Creates a pending enrollment request and notifies the teacher. Returns (ok, message)."""
   class_row = get_class_by_code(join_code)
   if not class_row:
       return False, "That code doesn't match any class. Double check it with your teacher."

   class_id, teacher_email, class_name = class_row

   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute(
       "SELECT status FROM class_enrollment WHERE class_id = ? AND LOWER(student_email) = ?",
       (class_id, student_email.lower().strip()),
   )
   existing = cursor.fetchone()

   if existing and existing[0] == "approved":
       conn.close()
       return False, f"You're already part of '{class_name}'."
   if existing and existing[0] == "pending":
       conn.close()
       return False, f"Your request to join '{class_name}' is already waiting on your teacher's approval."

   cursor.execute(
       "INSERT INTO class_enrollment (class_id, student_email, student_name, status) VALUES (?, ?, ?, 'pending')",
       (class_id, student_email.lower().strip(), student_name or student_email),
   )
   conn.commit()
   conn.close()

   send_onesignal_notification(
       teacher_email,
       "New Join Request",
       f"'{student_name or student_email}' wants to join '{class_name}' class.",
   )
   return True, f"Request sent! You'll be added to '{class_name}' once your teacher approves it."


def get_pending_join_requests(teacher_email):
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute(
       """
       SELECT e.id, e.student_email, e.student_name, c.class_name
       FROM class_enrollment e
       JOIN classes c ON e.class_id = c.id
       WHERE LOWER(c.teacher_email) = ? AND e.status = 'pending'
   """,
       (teacher_email.lower().strip(),),
   )
   rows = cursor.fetchall()
   conn.close()
   return rows


def update_join_request_status(enrollment_id, status):
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute("UPDATE class_enrollment SET status = ? WHERE id = ?", (status, enrollment_id))
   conn.commit()
   conn.close()


def get_approved_students_for_class(class_id):
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute(
       "SELECT student_email FROM class_enrollment WHERE class_id = ? AND status = 'approved'",
       (class_id,),
   )
   rows = [r[0] for r in cursor.fetchall()]
   conn.close()
   return rows


def get_students_for_class(class_id):
   """Returns (student_name, student_email) for every approved student in a class."""
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute(
       "SELECT student_name, student_email FROM class_enrollment WHERE class_id = ? AND status = 'approved' ORDER BY student_name",
       (class_id,),
   )
   rows = cursor.fetchall()
   conn.close()
   return rows


def get_student_classes(student_email):
   """Returns (class_id, class_name, teacher_email, join_code) for every class this student is approved into."""
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute(
       """
       SELECT c.id, c.class_name, c.teacher_email, c.join_code
       FROM class_enrollment e
       JOIN classes c ON e.class_id = c.id
       WHERE LOWER(e.student_email) = ? AND e.status = 'approved'
       ORDER BY c.class_name
       """,
       (student_email.lower().strip(),),
   )
   rows = cursor.fetchall()
   conn.close()
   return rows


def get_class_homework(class_id):
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute(
       "SELECT title, topic, due_date FROM assignments WHERE class_id = ? ORDER BY due_date DESC",
       (class_id,),
   )
   rows = cursor.fetchall()
   conn.close()
   return rows


def get_class_tests_for_class(class_id):
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute(
       "SELECT test_title, subject, topic, test_date FROM class_tests WHERE class_id = ? ORDER BY test_date",
       (class_id,),
   )
   rows = cursor.fetchall()
   conn.close()
   return rows


def get_student_upcoming_homework(student_email):
   """Returns (id, title, topic, due_date, class_name) for homework due today or later,
   across every class this student is approved into, minus any the student has
   dismissed (e.g. because they already did it) from their home page."""
   conn = db.connect()
   cursor = conn.cursor()
   today_str = datetime.date.today().isoformat()
   cursor.execute(
       """
       SELECT a.id, a.title, a.topic, a.due_date, c.class_name
       FROM assignments a
       JOIN classes c ON a.class_id = c.id
       JOIN class_enrollment e ON e.class_id = c.id
       WHERE LOWER(e.student_email) = ? AND e.status = 'approved' AND a.due_date >= ?
       AND a.id NOT IN (
           SELECT ref_id FROM notification_dismissals
           WHERE LOWER(student_email) = ? AND notif_type = 'homework'
       )
       ORDER BY a.due_date ASC
       """,
       (student_email.lower().strip(), today_str, student_email.lower().strip()),
   )
   rows = cursor.fetchall()
   conn.close()
   return rows


def notify_class_students(class_id, title, message):
   for student_email in get_approved_students_for_class(class_id):
       send_onesignal_notification(student_email, title, message)


def create_assignment(class_id, class_name, title, topic, due_date):
   """Posts a homework item for a class and immediately pings enrolled students."""
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute(
       "INSERT INTO assignments (class_id, title, topic, due_date) VALUES (?, ?, ?, ?)",
       (class_id, title, topic, due_date),
   )
   conn.commit()
   conn.close()

   notify_class_students(
       class_id,
       f"New Homework: {title}",
       f"oh u've got this homework for {due_date} ({class_name}) — would u like to work on it now?",
   )


def get_class_assignment_count(teacher_email):
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute(
       """
       SELECT COUNT(*) FROM assignments a
       JOIN classes c ON a.class_id = c.id
       WHERE LOWER(c.teacher_email) = ?
   """,
       (teacher_email.lower().strip(),),
   )
   count = cursor.fetchone()[0]
   conn.close()
   return count




# =========================================================
# PERSONAL SCHEDULED REMINDERS
# =========================================================
def create_reminder(user_email, title, due_at_iso):
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute(
       "INSERT INTO reminders (user_email, title, due_at) VALUES (?, ?, ?)",
       (user_email.lower().strip(), title.strip(), due_at_iso),
   )
   conn.commit()
   conn.close()


def get_upcoming_reminders(user_email):
   """Returns this user's reminders that haven't fully fired yet, soonest first."""
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute(
       """
       SELECT id, title, due_at FROM reminders
       WHERE LOWER(user_email) = ? AND notified_10m = 0
       ORDER BY due_at ASC
   """,
       (user_email.lower().strip(),),
   )
   rows = cursor.fetchall()
   conn.close()
   return rows


def get_due_soon_reminders(user_email):
   """Reminders due within the next 10 minutes (or up to 15 minutes overdue),
   for the in-app alert banner on the home page. Deliberately NOT push-based:
   this is a live query, re-run every time the page loads, mirroring the same
   dismiss-table pattern get_upcoming_tests_for_display already uses for test
   and homework alerts. Replaces the old OneSignal-push reminder path, which
   depended on a browser permission prompt that was never confirmed to work."""
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute(
       """
       SELECT id, title, due_at FROM reminders
       WHERE LOWER(user_email) = ?
       AND id NOT IN (
           SELECT ref_id FROM notification_dismissals
           WHERE LOWER(student_email) = ? AND notif_type = 'reminder'
       )
       """,
       (user_email.lower().strip(), user_email.lower().strip()),
   )
   rows = cursor.fetchall()
   conn.close()

   now = datetime.datetime.now(APP_TIMEZONE).replace(tzinfo=None)
   due_soon = []
   for r_id, title, due_at in rows:
       try:
           due_dt = datetime.datetime.fromisoformat(due_at)
       except ValueError:
           continue
       minutes_left = (due_dt - now).total_seconds() / 60
       if -15 <= minutes_left <= 10:
           due_soon.append((r_id, title, due_at, minutes_left))
   return due_soon


def delete_reminder(reminder_id):
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
   conn.commit()
   conn.close()


def check_and_notify_reminders(user_email):
   """Pull-based check, run on page load: pings 1h and 10m before a reminder is due."""
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute(
       "SELECT id, title, due_at, notified_1h, notified_10m FROM reminders WHERE LOWER(user_email) = ?",
       (user_email.lower().strip(),),
   )
   rows = cursor.fetchall()

   now = datetime.datetime.now(APP_TIMEZONE).replace(tzinfo=None)
   for r_id, title, due_at, notified_1h, notified_10m in rows:
       try:
           due_dt = datetime.datetime.fromisoformat(due_at)
       except ValueError:
           continue

       minutes_left = (due_dt - now).total_seconds() / 60

       if minutes_left < -60:
           # Very overdue (over an hour past due) — stop chasing it, mark done silently.
           cursor.execute(
               "UPDATE reminders SET notified_1h = 1, notified_10m = 1 WHERE id = ?", (r_id,)
           )
           continue

       if not notified_1h and minutes_left <= 60:
           send_onesignal_notification(
               user_email, "Reminder", f"'{title}' is due in about an hour — want to start now?"
           )
           cursor.execute("UPDATE reminders SET notified_1h = 1 WHERE id = ?", (r_id,))

       if not notified_10m and minutes_left <= 10:
           send_onesignal_notification(
               user_email, "Reminder", f"'{title}' is due in about 10 minutes!"
           )
           cursor.execute("UPDATE reminders SET notified_10m = 1 WHERE id = ?", (r_id,))

   conn.commit()
   conn.close()


def check_and_notify_all_reminders():
   """Same as check_and_notify_reminders, but for every user with a pending reminder.
   Meant to be run on a timer (see start_reminder_scheduler), not tied to any one
   browser session, so reminders still fire even if nobody has the app open."""
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute(
       "SELECT DISTINCT user_email FROM reminders WHERE notified_10m = 0"
   )
   emails = [row[0] for row in cursor.fetchall()]
   conn.close()

   for email in emails:
       check_and_notify_reminders(email)


@st.cache_resource
def start_reminder_scheduler():
   """Starts a single background thread, once per running app process (not per
   browser session — st.cache_resource shares this across every visitor), that
   checks reminders every 60 seconds regardless of whether anyone has a tab open."""

   def _loop():
       while True:
           try:
               check_and_notify_all_reminders()
           except Exception:
               pass
           time.sleep(60)

   thread = threading.Thread(target=_loop, daemon=True)
   thread.start()
   return thread


# =========================================================
# AI RESPONSE & TOPIC EXTRACTION ENGINE (WITH RETRY LOGIC)
# =========================================================
def extract_and_store_topic_details(prompt, image_bytes, mime_type, user_email):
   if not user_email:
       return


   profile = fetch_user_profile(user_email)
   if profile and not profile.get("save_topic_memory", True):
       return


   api_key = None
   try:
       if "GEMINI_API_KEY" in st.secrets:
           api_key = st.secrets["GEMINI_API_KEY"]
   except Exception:
       pass
   if not api_key:
       api_key = os.environ.get("GEMINI_API_KEY")


   if not api_key:
       return


   client = genai.Client(api_key=api_key)
   extraction_prompt = (
       "Analyze this prompt/image and extract the academic metadata as JSON with keys: "
       "'subject', 'topic', 'specific_area', 'content_summary'. "
       "'content_summary' must capture the ACTUAL problems/questions visible in the image or prompt "
       "(list out the specific expressions, numbers, or question text as written), not a generic "
       "description — this is what a future practice test will be modeled on. "
       "Example JSON: {\"subject\": \"Physics\", \"topic\": \"Kinematics\", \"specific_area\": \"Projectile Motion Formulae\", "
       "\"content_summary\": \"Worksheet has 3 problems: (1) find range for v=20m/s at 30deg, "
       "(2) time of flight for h=50m drop, (3) max height for v=15m/s at 45deg.\"}. "
       "Return ONLY raw JSON, nothing else."
   )
   contents = (
       [
           types.Part.from_bytes(data=image_bytes, mime_type=mime_type or "image/png"),
           extraction_prompt,
       ]
       if image_bytes
       else extraction_prompt + f"\nContent: {prompt}"
   )


   # Added Retry Loop for 503 errors
   max_retries = 4
   for attempt in range(max_retries):
       try:
           response = client.models.generate_content(
               model="gemini-3.8-flash", contents=contents
           )
           clean_text = response.text.strip().strip("```json").strip("```").strip()
           data = json.loads(clean_text)


           store_knowledge_item(
               email=user_email,
               subject=data.get("subject", "General"),
               topic=data.get("topic", "Homework"),
               specific_area=data.get("specific_area", prompt[:50]),
               raw_text=data.get("content_summary") or prompt,
           )
           break
       except Exception as e:
           err = str(e)
           if ("503" in err or "UNAVAILABLE" in err) and attempt < max_retries - 1:
               time.sleep(2 ** attempt)
               continue
           store_knowledge_item(
               email=user_email,
               subject="General",
               topic="Study Session",
               specific_area=prompt[:50],
               raw_text=prompt,
           )
           break




CONFUSION_PHRASES = [
   "dont get it", "don't get it", "not get it",
   "dont understand", "don't understand",
   "im confused", "i'm confused", "confused",
   "no idea", "dont know", "don't know",
   "still lost", "im lost", "i'm lost",
   "explain again", "explain differently", "explain it differently",
   "makes no sense", "doesnt make sense", "doesn't make sense",
   "can you simplify", "simpler please", "im stuck", "i'm stuck",
]


def is_confused_message(prompt):
   """Loosely detects 'I don't understand'-style replies so the tutor can step down
   to a simpler explanation instead of repeating the same method-only answer."""
   p = prompt.lower().strip()
   return any(phrase in p for phrase in CONFUSION_PHRASES)


def generate_ai_response(
       prompt,
       image_bytes=None,
       mime_type=None,
       mode="method",
       user_tier="freemium",
       grade="",
       user_email="",
       history=None,
):
   essay_keywords = [
       "write an essay",
       "write my essay",
       "write an assignment",
       "write my assignment",
       "write a paper",
   ]
   if any(keyword in prompt.lower() for keyword in essay_keywords):
       return (
           "It is against my policy to write the entire essay for you. "
           "However, if you provide the essay topic and requirements, "
           "I can provide sources, articles, and help you research them."
       )


   if user_email:
       if not check_and_increment_rpd(user_email, user_tier):
           max_limit = TIER_LIMITS.get(user_tier.lower(), 22)
           return (
               f"Daily RPD Limit Reached\n\n"
               f"You have used all {max_limit} requests available for today on the {user_tier.upper()} plan. "
               "Please upgrade your plan or wait until tomorrow to continue."
           )


   api_key = None
   try:
       if "GEMINI_API_KEY" in st.secrets:
           api_key = st.secrets["GEMINI_API_KEY"]
   except Exception:
       pass


   if not api_key:
       api_key = os.environ.get("GEMINI_API_KEY")


   if not api_key:
       return "Server Proxy Error: GEMINI_API_KEY is not configured in .streamlit/secrets.toml."


   selected_model = "gemini-3.8-flash"


   # extract_and_store_topic_details() used to run here on EVERY message (text
   # included), making its own separate Gemini call and doubling API usage
   # against the same rate-limited quota as the actual tutor response below —
   # so it was disabled outright. That silently broke "study memory" entirely:
   # nothing was ever saved to knowledge_items, so generate_mock_test_from_memory
   # always came back empty ("No study history found"), even right after a
   # student uploaded a homework worksheet photo.
   # Only re-run it when THIS turn actually has an image: that's the case that
   # matters (capturing the worksheet's real content for later mock tests) and
   # image uploads are far rarer than ordinary text follow-ups, so this adds
   # roughly one extra call per worksheet upload rather than doubling every turn.
   if image_bytes:
       extract_and_store_topic_details(prompt, image_bytes, mime_type, user_email)


   client = genai.Client(api_key=api_key)
   grade_context = f" Target academic level: {grade}." if grade else ""
   academic_guardrail = (
       " ACADEMIC INTEGRITY RULE: If the user asks you to write an entire essay, assignment, "
       "or complete paper for them, respond: 'It is against my policy to write the entire essay for you. "
       "However, if you provide the essay topic and requirements, I can provide sources, articles, and help you research them.' "
       "Never generate complete essays or finished homework assignments."
   )


   if mode == "method":
       system_instruction = (
               f"You are an interactive AI tutor.{grade_context} Provide ONLY the core method, concepts, "
               "or strategy needed to solve the user's problem. DO NOT provide hints with worked "
               "examples, and DO NOT give the final calculation or direct answer yet."
               + academic_guardrail
       )
   elif mode == "hint":
       system_instruction = (
               f"You are an interactive AI tutor.{grade_context} Provide a helpful hint along with a simplified worked "
               "example using a SIMILAR question. DO NOT reveal the answer to the user's actual original question."
               + academic_guardrail
       )
   else:
       system_instruction = (
               f"You are a helpful AI tutor.{grade_context}"
               + academic_guardrail
       )


   config = types.GenerateContentConfig(
       system_instruction=system_instruction
   )

   # Build the full conversation as multi-turn `contents` when a message history is
   # given, so the model actually sees earlier turns (including earlier uploaded
   # images) instead of only ever seeing this one isolated message. Without this,
   # every call was a fresh, context-free request — the model had no memory of
   # anything said or shown earlier in the same chat.
   if history:
       contents = []
       for msg in history:
           role = "model" if msg.get("role") == "assistant" else "user"
           parts = []
           if msg.get("image_bytes"):
               parts.append(
                   types.Part.from_bytes(
                       data=msg["image_bytes"],
                       mime_type=msg.get("mime_type") or "image/png",
                   )
               )
           if msg.get("content"):
               parts.append(types.Part.from_text(text=msg["content"]))
           if parts:
               contents.append(types.Content(role=role, parts=parts))

       current_parts = []
       if image_bytes:
           current_parts.append(
               types.Part.from_bytes(data=image_bytes, mime_type=mime_type or "image/png")
           )
       current_parts.append(types.Part.from_text(text=prompt))
       contents.append(types.Content(role="user", parts=current_parts))
   else:
       contents = (
           [
               types.Part.from_bytes(data=image_bytes, mime_type=mime_type or "image/png"),
               prompt,
           ]
           if image_bytes
           else prompt
       )


   # Added Exponential Backoff Retry Loop to resolve 503 UNAVAILABLE errors
   max_retries = 4
   for attempt in range(max_retries):
       try:
           response = client.models.generate_content(
               model=selected_model, contents=contents, config=config
           )
           return response.text
       except Exception as e:
           error_msg = str(e)
           if ("503" in error_msg or "UNAVAILABLE" in error_msg) and attempt < max_retries - 1:
               time.sleep(2 * (attempt + 1))
               continue
           if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
               retry_match = re.search(r"retryDelay['\"]?:\s*['\"]?(\d+)", error_msg)
               wait_s = retry_match.group(1) if retry_match else "a few"
               return (
                   f"The tutor is getting a lot of requests right now — please wait "
                   f"{wait_s} seconds and try again."
               )
           if "503" in error_msg or "UNAVAILABLE" in error_msg:
               return "Please try again in about 20 seconds — the tutor is briefly unavailable."
           return f"Error communicating with AI: {error_msg}"




def generate_mock_test_from_memory(user_email, target_subject=None):
   stored_items = fetch_stored_topics(user_email, subject=target_subject)
   if not stored_items:
       return f"No study history found{' for ' + target_subject if target_subject else ''} yet. Upload a screenshot or ask a question to start building practice tests!"


   memory_summary = "\n".join(
       [f"- Subject: {s}, Topic: {t}, Details: {a}\n  Actual content seen: {r}" for s, t, a, r, ts in stored_items[:10]]
   )


   prompt = (
       f"Based on the student's study topics and past uploaded assignments:\n{memory_summary}\n\n"
       f"Generate a targeted 5-question Mock Practice Test with step-by-step solutions and key summary review points "
       f"specifically formatted to help them study and excel in their test. "
       f"Wherever an entry above has 'Actual content seen', base your questions on those specific problems "
       f"(same numbers/expressions/style where reasonable, or close variations of them) rather than inventing "
       f"generic textbook questions on the topic."
   )
   return generate_ai_response(prompt, mode="full", user_email=user_email)




# INITIALIZE APPLICATION COMPONENTS
@st.cache_resource
def _init_db_once():
   # Creating the tables on every rerun is slow on a hosted database, so do it
   # once per app start.
   init_db()
   return True


_init_db_once()
# Superseded by the in-app due-soon banner (get_due_soon_reminders, used in
# render_home_page): that's a live query on page load, so this background
# push-based thread no longer needs to run. Left defined, not deleted, in
# case OneSignal push is revisited later.
# start_reminder_scheduler()


if "active_email" not in st.session_state:
   # Nobody is logged in until they prove they own an email (OTP). Never default to
   # an existing profile: that would hand one user's account to every visitor.
   st.session_state["active_email"] = None


if "show_search" not in st.session_state:
   st.session_state["show_search"] = False


if "show_test_prep" not in st.session_state:
   st.session_state["show_test_prep"] = False


# STYLING
st.markdown(
   """
<style>
  @import url('https://fonts.googleapis.com/css2?family=Nunito:wght@400;500;600;700&display=swap');


  html, body, [class*="css"], .stApp, p, h1, h2, h3, h4, h5, h6, label, input, button {
      font-family: 'Nunito', sans-serif !important;
      font-weight: 500 !important;
  }


  .stApp {
      background: #f8fafc !important;
      color: #0f172a;
  }


  header[data-testid="stHeader"] {
      background: transparent !important;
  }


  div[data-baseweb="input"],
  div[data-baseweb="base-input"],
  div[data-baseweb="select"] {
      background-color: #ffffff !important;
      border-radius: 12px !important;
      border: 1px solid #cbd5e1 !important;
  }


  .stTextInput input,
  .stTextArea textarea,
  div[data-baseweb="input"] input,
  div[data-baseweb="base-input"] input {
      background-color: #ffffff !important;
      color: #0f172a !important;
      -webkit-text-fill-color: #0f172a !important;
      border-radius: 12px !important;
      border: none !important;
      font-weight: 500 !important;
  }


  div.stButton > button, div.stLinkButton > a {
      background-color: #f1f5f9 !important;
      color: #0f172a !important;
      border-radius: 12px !important;
      border: 1px solid #cbd5e1 !important;
      padding: 8px 16px !important;
      font-size: 0.9rem !important;
      font-weight: 600 !important;
      text-decoration: none !important;
      display: flex !important;
      justify-content: center !important;
      align-items: center !important;
      box-shadow: none !important;
  }


  div.stButton > button:hover, div.stLinkButton > a:hover {
      background-color: #e2e8f0 !important;
      color: #0f172a !important;
      border-color: #94a3b8 !important;
  }


  .sidebar-logo-button {
      display: flex !important;
      flex-direction: row !important;
      align-items: center !important;
      justify-content: flex-start !important;
      gap: 10px !important;
      width: 100% !important;
      background-color: #f1f5f9 !important;
      border: 1px solid #cbd5e1 !important;
      border-radius: 12px !important;
      padding: 8px 14px !important;
      color: #0f172a !important;
      font-weight: 700 !important;
      font-size: 1rem !important;
      text-decoration: none !important;
      box-sizing: border-box !important;
  }


  .sidebar-logo-button * {
      text-decoration: none !important;
  }


  .sidebar-logo-img {
      height: 26px !important;
      width: auto !important;
      object-fit: contain !important;
  }


  section[data-testid="stSidebar"] {
      background-color: #ffffff !important;
      border-right: 1px solid #e2e8f0;
  }


  /* SEGMENTED PROGRESS BAR STYLES */
  .progress-bar-container {
      display: flex;
      gap: 3px;
      width: 100%;
      height: 8px;
      margin: 8px 0;
  }
  .progress-segment {
      flex: 1;
      height: 100%;
      background-color: #e2e8f0;
      border-radius: 2px;
      transition: background-color 0.3s ease;
  }
  .progress-segment.active {
      background-color: #ef4444;
  }


  .dash-header h1 {
      font-size: 2.2rem !important;
      font-weight: 700 !important;
      color: #0f172a !important;
  }


  .metric-card {
      background: #ffffff;
      border-radius: 12px;
      padding: 16px;
      border: 1px solid #e2e8f0;
  }
  .metric-card h3 {
      font-size: 1.4rem !important;
      font-weight: 700 !important;
      color: #2563eb !important;
      margin: 0 !important;
  }
  .metric-card p {
      color: #64748b !important;
      font-size: 0.8rem !important;
      font-weight: 600 !important;
      margin: 0 !important;
  }


  .profile-card {
      background: #ffffff;
      border-radius: 12px;
      padding: 18px;
      border: 1px solid #e2e8f0;
      margin-top: 20px;
  }


  .profile-badge {
      background: #e0f2fe;
      color: #0284c7;
      font-weight: 600;
      font-size: 0.75rem;
      padding: 3px 8px;
      border-radius: 12px;
      display: inline-block;
      margin-bottom: 8px;
  }


  .account-avatar {
      width: 32px;
      height: 32px;
      border-radius: 50%;
      background-color: #2563eb;
      color: white;
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 600;
      font-size: 0.9rem;
  }


  .account-row {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 8px 12px;
      border-radius: 10px;
      background: #f8fafc;
      border: 1px solid #e2e8f0;
  }


  .test-alert-card {
      background: #fef2f2;
      border: 1px solid #fca5a5;
      border-radius: 12px;
      padding: 16px;
      margin-bottom: 20px;
  }


  .homework-alert-card {
      background: #eff6ff;
      border: 1px solid #93c5fd;
      border-radius: 12px;
      padding: 16px;
      margin-bottom: 20px;
  }


  .reminder-alert-card {
      background: #fffbeb;
      border: 1px solid #fcd34d;
      border-radius: 12px;
      padding: 16px;
      margin-bottom: 20px;
  }


  .tutor-gemini-landing {
      display: flex !important;
      flex-direction: column !important;
      align-items: center !important;
      justify-content: center !important;
      text-align: center !important;
      margin-top: 10vh !important;
      width: 100% !important;
  }


  .tutor-gemini-landing img {
      height: 70px !important;
      width: auto !important;
      margin-bottom: 16px !important;
  }


  .tutor-gemini-landing h1 {
      font-size: 2rem !important;
      font-weight: 700 !important;
      color: #0f172a !important;
      margin: 0 !important;
      text-align: center !important;
  }


  .learn-more-box {
      background-color: #f1f5f9;
      border: 1px dashed #cbd5e1;
      border-radius: 10px;
      padding: 12px 16px;
      font-size: 0.85rem;
      color: #475569;
      margin-top: 8px;
  }


  div[data-testid="stDialog"] > div,
  div[role="dialog"] {
      width: 80vw !important;
      max-width: 1000px !important;
  }
</style>
""",
   unsafe_allow_html=True,
)




# =========================================================
# ACCOUNT SWITCHER DIALOG
# =========================================================
# =========================================================
# UPGRADE PLAN DIALOG WITH STRIPE LINKS
# =========================================================
@st.dialog("Upgrade Your Subscription", width="large")
def render_upgrade_dialog(profile):
   user_email = profile.get("email", "") if profile else ""
   st.write(
       "Unlock higher daily request limits and high-speed processing for your study sessions."
   )
   col_free, col_pro, col_plus = st.columns(3, gap="large")


   pro_stripe_url = (
       f"https://buy.stripe.com/your_pro_link?prefilled_email={user_email}"
   )
   pro_plus_stripe_url = f"https://buy.stripe.com/your_pro_plus_link?prefilled_email={user_email}"


   with col_free:
       st.markdown(
           """
          <div style="border: 1px solid #cbd5e1; border-radius: 12px; padding: 20px; text-align: center; background: #f8fafc; min-height: 260px;">
              <h4 style="color: #64748b; margin-bottom: 4px;">Freemium</h4>
              <h3 style="margin: 0; color: #0f172a;">EUR 0 <span style="font-size: 0.85rem; color: #64748b;">/ mo</span></h3>
              <hr style="margin: 14px 0;">
              <ul style="text-align: left; font-size: 0.85rem; padding-left: 20px; color: #334155; line-height: 1.6;">
                  <li><b>22 RPD</b> (Requests Per Day)</li>
                  <li>Powered by Gemini 3.8 Flash</li>
              </ul>
          </div>
      """,
           unsafe_allow_html=True,
       )
       st.button(
           "Current Plan",
           use_container_width=True,
           disabled=True,
           key="btn_current_free",
       )


   with col_pro:
       st.markdown(
           """
          <div style="border: 1px solid #2563eb; border-radius: 12px; padding: 20px; text-align: center; background: #ffffff; min-height: 260px;">
              <h4 style="color: #2563eb; margin-bottom: 4px;">Pro</h4>
              <h3 style="margin: 0; color: #0f172a;">EUR 10 <span style="font-size: 0.85rem; color: #64748b;">/ mo</span></h3>
              <hr style="margin: 14px 0;">
              <ul style="text-align: left; font-size: 0.85rem; padding-left: 20px; color: #334155; line-height: 1.6;">
                  <li><b>100 RPD</b> (Requests Per Day)</li>
                  <li>Powered by Gemini 3.8 Flash</li>
              </ul>
          </div>
      """,
           unsafe_allow_html=True,
       )
       st.link_button(
           "Select Pro (EUR 10)", pro_stripe_url, use_container_width=True
       )


   with col_plus:
       st.markdown(
           """
          <div style="border: 1px solid #7c3aed; border-radius: 12px; padding: 20px; text-align: center; background: #ffffff; min-height: 260px;">
              <h4 style="color: #7c3aed; margin-bottom: 4px;">Pro Plus</h4>
              <h3 style="margin: 0; color: #0f172a;">EUR 15 <span style="font-size: 0.85rem; color: #64748b;">/ mo</span></h3>
              <hr style="margin: 14px 0;">
              <ul style="text-align: left; font-size: 0.85rem; padding-left: 20px; color: #334155; line-height: 1.6;">
                  <li><b>200 RPD</b> (Requests Per Day)</li>
              </ul>
          </div>
      """,
           unsafe_allow_html=True,
       )
       st.link_button(
           "Select Pro Plus (EUR 15)",
           pro_plus_stripe_url,
           use_container_width=True,
       )




# =========================================================
# EDIT PROFILE (study times, topic focus, goal, grade)
# =========================================================
EDIT_SUBJECT_OPTIONS = [
   "Mathematics", "Physics", "Chemistry", "Biology", "Computer Science",
   "English", "History", "Geography", "Civics", "Economics",
   "Environmental Science", "Foreign Language",
]
EDIT_GRADE_OPTIONS = [
   "6th Grade", "7th Grade", "8th Grade", "9th Grade", "10th Grade",
   "11th Grade", "12th Grade", "College / University", "Other",
]
EDIT_DAY_OPTIONS = [
   "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
]


def update_user_profile_details(email, name, purpose, interests, schedule, grade):
   conn = db.connect()
   cursor = conn.cursor()
   cursor.execute(
       """
       UPDATE user_profile
       SET name = ?, purpose = ?, interests = ?, schedule = ?, grade = ?
       WHERE LOWER(email) = ?
   """,
       (name, purpose, interests, schedule, grade, email.lower().strip()),
   )
   conn.commit()
   conn.close()


def parse_schedule_string(schedule):
   """Turns 'Monday: 04:00 PM - 06:00 PM | Friday: ...' back into
   {day: (start_time, end_time)} so the edit form can pre-fill it."""
   parsed = {}
   if not schedule:
       return parsed
   for part in schedule.split(" | "):
       try:
           day, times = part.split(": ", 1)
           start_s, end_s = times.split(" - ")
           parsed[day.strip()] = (
               datetime.datetime.strptime(start_s.strip(), "%I:%M %p").time(),
               datetime.datetime.strptime(end_s.strip(), "%I:%M %p").time(),
           )
       except ValueError:
           continue
   return parsed


@st.dialog("Edit Your Profile", width="large")
def render_edit_profile_dialog(profile):
   user_email = profile["email"]
   is_teacher = profile.get("role", "student") == "teacher"
   st.caption(f"{user_email} · {profile.get('role', 'student').capitalize()} (email and role can't be changed)")

   new_name = st.text_input("Name", value=profile.get("name", ""), key="edit_name")

   if is_teacher:
       new_grade = "Teacher"
       goals = [
           "Managing my classes & sending assignments",
           "Generating AI mock tests",
           "Tracking progress",
       ]
   else:
       current_grade = profile.get("grade", "")
       new_grade = st.selectbox(
           "Grade",
           EDIT_GRADE_OPTIONS,
           index=EDIT_GRADE_OPTIONS.index(current_grade) if current_grade in EDIT_GRADE_OPTIONS else 3,
           key="edit_grade",
       )
       goals = ["Preparing for exams", "Building study habits", "General tutoring"]

   current_purpose = profile.get("purpose", "")
   new_purpose = st.radio(
       "Main goal",
       goals,
       index=goals.index(current_purpose) if current_purpose in goals else 0,
       key="edit_purpose",
   )

   current_subjects = [
       s.strip() for s in (profile.get("interests") or "").split(",")
       if s.strip() in EDIT_SUBJECT_OPTIONS
   ]
   selected_subjects = st.multiselect(
       "Topic focus (subjects)",
       EDIT_SUBJECT_OPTIONS,
       default=current_subjects,
       key="edit_subjects",
   )

   new_schedule = profile.get("schedule", "") or ""
   if not is_teacher:
       st.markdown("##### Study times")
       existing = parse_schedule_string(profile.get("schedule", ""))
       selected_days = st.multiselect(
           "Study days",
           EDIT_DAY_OPTIONS,
           default=[d for d in EDIT_DAY_OPTIONS if d in existing] or ["Monday", "Wednesday", "Friday"],
           key="edit_days",
       )
       schedule_details = []
       for day in selected_days:
           default_start, default_end = existing.get(day, (datetime.time(16, 0), datetime.time(18, 0)))
           c1, c2 = st.columns(2)
           with c1:
               s_time = st.time_input(f"{day} start", default_start, key=f"edit_start_{day}")
           with c2:
               e_time = st.time_input(f"{day} end", default_end, key=f"edit_end_{day}")
           schedule_details.append(
               f"{day}: {s_time.strftime('%I:%M %p')} - {e_time.strftime('%I:%M %p')}"
           )
       new_schedule = " | ".join(schedule_details) if schedule_details else "No schedule set"

   if st.button("Save changes", use_container_width=True, key="btn_save_profile"):
       if not new_name.strip():
           st.warning("Name can't be empty.")
       else:
           update_user_profile_details(
               user_email,
               new_name.strip(),
               new_purpose,
               ", ".join(selected_subjects) if selected_subjects else "General",
               new_schedule,
               new_grade,
           )
           st.rerun()


def render_login_form():
   st.subheader("Log in to your account")
   login_email = st.text_input("Email", key="login_email_input")
   email_norm = login_email.strip().lower()

   def send_login_code():
       if not email_norm:
           st.warning("Enter your email first.")
           return
       if not fetch_user_profile(email_norm):
           st.error("No account found with that email. Use 'New here? Create an account' below to sign up.")
           return
       otp = generate_otp()
       if send_otp_email(email_norm, otp):
           st.session_state["login_generated_otp"] = otp
           st.session_state["login_otp_email"] = email_norm
           st.session_state["login_otp_sent"] = True
           otp_start("login")
           st.rerun()
       else:
           st.error(st.session_state.get("last_otp_error", "Couldn't send the code."))

   code_sent_for_this_email = (
       st.session_state.get("login_otp_sent")
       and st.session_state.get("login_otp_email") == email_norm
   )
   if not code_sent_for_this_email:
       if st.button("Send login code", use_container_width=True, key="btn_login_send"):
           send_login_code()
   else:
       st.success(f"Code sent to {email_norm}.")
       code = st.text_input("Enter 6-digit login code:", key="login_code_input")
       st.caption("Didn't get an email? Check your spam/junk folder — it can take a minute to arrive.")
       col_in, col_re = st.columns(2)
       with col_in:
           if st.button("Log in", use_container_width=True, key="btn_login_verify"):
               ok, msg = otp_check("login", code, st.session_state.get("login_generated_otp"))
               if ok:
                   st.session_state["active_email"] = email_norm
                   st.session_state["is_logged_in"] = True
                   start_remembered_session(email_norm)
                   for k in ("login_generated_otp", "login_otp_email", "login_otp_sent"):
                       st.session_state.pop(k, None)
                   st.rerun()
               else:
                   st.error(msg)
       with col_re:
           if st.button("Resend code", use_container_width=True, key="btn_login_resend"):
               send_login_code()

   if st.button(
           "New here? Create an account",
           use_container_width=True,
           type="tertiary",
           key="btn_goto_signup",
   ):
       st.session_state["auth_mode"] = "signup"
       st.rerun()


# =========================================================
# ONBOARDING WIZARD
# =========================================================
def render_onboarding_wizard():
   if "wizard_step" not in st.session_state:
       st.session_state["wizard_step"] = 1
   if "otp_sent" not in st.session_state:
       st.session_state["otp_sent"] = False
   if "generated_otp" not in st.session_state:
       st.session_state["generated_otp"] = None
   if "email_verified" not in st.session_state:
       st.session_state["email_verified"] = False


   if "form_data" not in st.session_state:
       st.session_state["form_data"] = {
           "name": "",
           "email": "",
           "role": "student",
           "grade": "9th Grade",
           "purpose": "Preparing for upcoming exams & tests",
           "interests": "",
           "schedule": "",
       }


   is_teacher = st.session_state["form_data"].get("role") == "teacher"
   total_steps = 3 if is_teacher else 4
   step = st.session_state["wizard_step"]


   st.markdown(
       """
      <h1 style="text-align:center; font-size:2.4rem; font-weight:700; color:#0f172a; margin-top:2vh;">
          Welcome to StudySpace
      </h1>
  """,
       unsafe_allow_html=True,
   )


   _, card_col, _ = st.columns([1, 2, 1])


   with card_col:
       if st.session_state.pop("_deleted_notice", False):
           st.success("Your account and data have been deleted.")
       if st.session_state.get("auth_mode") == "login":
           render_login_form()
           with st.expander("Privacy notice"):
               render_privacy_notice()
           return

       st.progress(min(step / total_steps, 1.0))
       st.caption(f"Step {step} of {total_steps}")


       if step == 1:
           st.subheader("Please fill in your details to create an account")
           st.session_state["form_data"]["name"] = st.text_input(
               "Name", value=st.session_state["form_data"]["name"]
           )
           email_val = st.text_input(
               "Email", value=st.session_state["form_data"]["email"]
           )
           st.session_state["form_data"]["email"] = email_val
           if st.session_state.get("email_verified") and st.session_state.get("verified_email") != email_val.strip().lower():
               # Email was changed after verifying: the old verification no longer applies.
               st.session_state["email_verified"] = False
               st.session_state["otp_sent"] = False
               st.session_state["generated_otp"] = None
               st.session_state["otp_email_confirmed"] = False


           role_choice = st.radio(
               "I am a...", ["Student", "Teacher"], horizontal=True, key="role_radio"
           )
           st.session_state["form_data"]["role"] = role_choice.lower()


           if role_choice == "Student":
               grade_options = [
                   "6th Grade",
                   "7th Grade",
                   "8th Grade",
                   "9th Grade",
                   "10th Grade",
                   "11th Grade",
                   "12th Grade",
                   "College / University",
                   "Other",
               ]
               st.session_state["form_data"]["grade"] = st.selectbox(
                   "What grade are you in?", grade_options, index=3
               )
           else:
               st.session_state["form_data"]["grade"] = "Teacher"


           if not st.session_state["email_verified"]:
               if not st.session_state["otp_sent"]:
                   if st.button(
                           "Send Verification OTP", use_container_width=True, key="btn_send_otp"
                   ):
                       if email_val.strip() and fetch_user_profile(email_val.strip().lower()):
                           st.error("This email is already in use. Use 'Already have an account? Log in' below to access your account.")
                       elif email_val.strip():
                           otp = generate_otp()
                           st.session_state["generated_otp"] = otp
                           otp_start("signup")
                           if send_otp_email(email_val.strip(), otp):
                               st.session_state["otp_sent"] = True
                               st.session_state["otp_email_confirmed"] = True
                               st.success(f"Code sent to {email_val}!")
                               st.rerun()
                           else:
                               st.session_state["otp_email_confirmed"] = False
                               st.error(st.session_state.get("last_otp_error", "Couldn't send the OTP email."))
               else:
                   code_col, tick_col = st.columns([5, 1], vertical_alignment="bottom")
                   with code_col:
                       user_code = st.text_input("Enter 6-digit OTP code:")
                   with tick_col:
                       if st.session_state.get("otp_email_confirmed"):
                           st.markdown(
                               "<div style='text-align:center; color:#16a34a; font-size:1.5rem; padding-bottom:0.5rem;'>&#10003;</div>",
                               unsafe_allow_html=True,
                           )
                   st.caption("Didn't get an email? Check your spam/junk folder — it can take a minute to arrive.")
                   col_v, col_r = st.columns([1, 1])
                   with col_v:
                       if st.button("Verify OTP", use_container_width=True, key="btn_verify_otp"):
                           otp_ok, otp_msg = otp_check("signup", user_code, st.session_state["generated_otp"])
                           if otp_ok:
                               if fetch_user_profile(email_val.strip().lower()):
                                   st.session_state["otp_sent"] = False
                                   st.session_state["generated_otp"] = None
                                   st.error("This email is already in use. Use 'Already have an account? Log in' below to access your account.")
                               else:
                                   st.session_state["email_verified"] = True
                                   st.session_state["verified_email"] = email_val.strip().lower()
                                   st.success("Email Verified successfully!")
                                   st.rerun()
                           else:
                               st.error(otp_msg)
                   with col_r:
                       resend_clicked = st.button(
                           "Resend OTP", use_container_width=True, key="btn_resend_otp"
                       )
                       if st.session_state.get("otp_email_confirmed"):
                           st.caption(":green[✓ Sent]")
                       if resend_clicked:
                           if email_val.strip():
                               new_otp = generate_otp()
                               st.session_state["generated_otp"] = new_otp
                               otp_start("signup")
                               if send_otp_email(email_val.strip(), new_otp):
                                   st.session_state["otp_email_confirmed"] = True
                                   st.success(
                                       f"A new code was sent to {email_val}!"
                                   )
                                   st.rerun()
                               else:
                                   st.session_state["otp_email_confirmed"] = False
                                   st.error(st.session_state.get("last_otp_error", "Couldn't send the OTP email."))
           else:
               st.success("Email Verified")


       elif step == 2:
           st.subheader("What is your main goal?")
           goals = (
               [
                   "Managing my classes & sending assignments",
                   "Generating AI mock tests",
                   "Tracking progress",
               ]
               if is_teacher
               else [
                   "Preparing for exams",
                   "Building study habits",
                   "General tutoring",
               ]
           )
           selected_goal = st.radio("Primary Goal:", goals)
           st.session_state["form_data"]["purpose"] = selected_goal


       elif step == 3:
           st.subheader("Which subjects are you focused on?")
           subject_options = [
               "Mathematics",
               "Physics",
               "Chemistry",
               "Biology",
               "Computer Science",
               "English",
               "History",
               "Geography",
               "Civics",
               "Economics",
               "Environmental Science",
               "Foreign Language",
           ]
           selected_subjects = st.multiselect(
               "Select Subjects:", options=subject_options
           )
           st.session_state["form_data"]["interests"] = (
               ", ".join(selected_subjects) if selected_subjects else "General"
           )


       elif step == 4 and not is_teacher:
           st.subheader("Preferred Study Schedule?")
           selected_days = st.multiselect(
               "Select your study days:",
               [
                   "Monday",
                   "Tuesday",
                   "Wednesday",
                   "Thursday",
                   "Friday",
                   "Saturday",
                   "Sunday",
               ],
               default=["Monday", "Wednesday", "Friday"],
           )


           schedule_details = []
           if selected_days:
               st.caption("Set study hours for selected days:")
               for day in selected_days:
                   col1, col2 = st.columns(2)
                   with col1:
                       start_time = st.time_input(
                           f"{day} Start Time",
                           datetime.time(16, 0),
                           key=f"start_{day}",
                       )
                   with col2:
                       end_time = st.time_input(
                           f"{day} End Time",
                           datetime.time(18, 0),
                           key=f"end_{day}",
                       )
                   schedule_details.append(
                       f"{day}: {start_time.strftime('%I:%M %p')} - {end_time.strftime('%I:%M %p')}"
                   )


           st.session_state["form_data"]["schedule"] = (
               " | ".join(schedule_details)
               if schedule_details
               else "No schedule set"
           )


       if step == 1:
           with st.expander("Privacy notice: what StudySpace stores"):
               render_privacy_notice()

       if step == 1 and not st.session_state["email_verified"]:
           if st.button(
                   "Already have an account? Log in",
                   use_container_width=True,
                   type="tertiary",
                   key="btn_goto_login",
           ):
               st.session_state["auth_mode"] = "login"
               st.rerun()

       if step == total_steps:
           with st.expander("Read the Privacy Notice"):
               render_privacy_notice()
           st.checkbox(
               "I have read the Privacy Notice and I agree to it.",
               key="consent_privacy",
           )

       btn_col1, btn_col2 = st.columns([1, 1])
       with btn_col1:
           if step > 1 and st.button("Back", use_container_width=True, key="btn_wizard_back"):
               st.session_state["wizard_step"] -= 1
               st.rerun()
       with btn_col2:
           if step < total_steps:
               if step == 1 and not st.session_state["email_verified"]:
                   pass  # No Next button until the OTP has been verified.
               elif st.button("Next", use_container_width=True, key="btn_wizard_next"):
                   st.session_state["wizard_step"] += 1
                   st.rerun()
           else:
               if st.button(
                       "Complete Setup",
                       use_container_width=True,
                       key="btn_wizard_complete",
                       disabled=not st.session_state.get("consent_privacy", False),
               ):
                   fd = st.session_state["form_data"]
                   user_e = fd["email"].strip().lower()
                   if fetch_user_profile(user_e):
                       # Never overwrite an existing account (any role) via the signup form.
                       st.error("This email is already in use. Use 'Already have an account? Log in' below to access your account.")
                       st.stop()
                   save_user_profile(
                       fd["name"],
                       user_e,
                       fd["purpose"],
                       fd["interests"],
                       fd.get("schedule", "N/A"),
                       fd.get("role", "student"),
                       fd.get("grade", ""),
                       "freemium",
                   )
                   record_consent(user_e)
                   st.session_state["active_email"] = user_e
                   st.session_state["is_logged_in"] = True
                   start_remembered_session(user_e)
                   st.rerun()




# =========================================================
# TEACHER HOME PAGE (WITH TEST CREATION)
# =========================================================
@st.dialog("Create New Class")
def render_create_class_dialog(teacher_email):
   st.write("Create a class, then share the join code with your students.")
   with st.form("create_class_form"):
       new_class_name = st.text_input("Class name (e.g. Grade 9 Physics)")
       if st.form_submit_button("Create Class"):
           if new_class_name.strip():
               ok, result = create_class(teacher_email, new_class_name.strip())
               if ok:
                   st.success(f"Class '{new_class_name.strip()}' created! Join code: {result}")
               else:
                   st.error(result)
           else:
               st.warning("Enter a class name first.")

   existing_classes = get_teacher_classes(teacher_email)
   if existing_classes:
       st.write("---")
       st.caption("YOUR CLASSES")
       for _, c_name, c_code in existing_classes:
           st.markdown(f"**{c_name}** — join code `{c_code}`")


@st.dialog("Join a Class")
def render_join_class_dialog(student_email, student_name):
   st.write("Enter the 6-digit code your teacher shared with you.")
   with st.form("join_class_form"):
       code_input = st.text_input("Join code")
       if st.form_submit_button("Request to Join"):
           if code_input.strip():
               ok, msg = request_join_class(student_email, student_name, code_input.strip())
               if ok:
                   st.success(msg)
               else:
                   st.error(msg)
           else:
               st.warning("Enter a join code first.")


@st.dialog("Post Homework")
def render_post_homework_dialog(teacher_email):
   teacher_classes = get_teacher_classes(teacher_email)
   class_options = {f"{c_name} ({c_code})": c_id for c_id, c_name, c_code in teacher_classes}

   if not class_options:
       st.info("Create a class first (use the '+ Create New Class' widget) before posting homework.")
       return

   with st.form("post_homework_form"):
       hw_class_label = st.selectbox("Class", options=list(class_options.keys()), key="hw_class_select")
       hw_title = st.text_input("Homework Title (e.g. Worksheet 4)")
       hw_topic = st.text_input("Topic (e.g. Chapter 3 Practice Problems)")
       hw_due = st.date_input("Due Date", datetime.date.today() + datetime.timedelta(days=1), key="hw_due_date")

       if st.form_submit_button("Post Homework & Notify Students"):
           sel_class_id = class_options[hw_class_label]
           sel_class_name = hw_class_label.split(" (")[0]
           create_assignment(sel_class_id, sel_class_name, hw_title, hw_topic, hw_due.isoformat())
           st.success(f"Homework '{hw_title}' posted! AI notified enrolled students.")


@st.dialog("Add a Reminder to the Class")
def render_add_reminder_dialog(teacher_email):
   teacher_classes = get_teacher_classes(teacher_email)
   class_options = {f"{c_name} ({c_code})": c_id for c_id, c_name, c_code in teacher_classes}

   if not class_options:
       st.info("Create a class first (use the '+ Create New Class' widget) before adding a reminder.")
       return

   with st.form("schedule_test_form"):
       t_title = st.text_input("Test Title (e.g. Midterm Chapter 3)")
       t_subject = st.text_input("Subject (e.g. Physics)")
       t_topic = st.text_input("Topic (e.g. Kinematics)")
       t_date = st.date_input("Test Date", datetime.date.today() + datetime.timedelta(days=2))
       t_class_label = st.selectbox("Class", options=list(class_options.keys()), key="test_class_select")

       if st.form_submit_button("Schedule Test & Notify Students"):
           t_class_id = class_options[t_class_label]
           add_class_test(t_class_id, t_title, t_subject, t_topic, t_date.isoformat())
           st.success(f"Test '{t_title}' scheduled! AI will notify enrolled students 3 days prior.")


@st.dialog("Schedule a Reminder")
def render_schedule_reminder_dialog(user_email):
   st.caption("You'll get pinged 1 hour before, and again 10 minutes before, it's due.")
   with st.form("schedule_reminder_form"):
       r_title = st.text_input("What's it for? (e.g. Finish Chapter 4 worksheet)")
       rc1, rc2 = st.columns(2)
       with rc1:
           r_date = st.date_input("Due date", datetime.date.today())
       with rc2:
           r_time = st.time_input("Due time", datetime.time(18, 0))

       if st.form_submit_button("Schedule Reminder"):
           if not r_title.strip():
               st.warning("Give the reminder a title first.")
           else:
               due_dt = datetime.datetime.combine(r_date, r_time)
               create_reminder(user_email, r_title, due_dt.isoformat())
               st.success(f"Reminder set for {due_dt.strftime('%b %d, %I:%M %p')}.")


@st.dialog("Your Classes")
def render_classes_dialog(teacher_email):
   teacher_classes = get_teacher_classes(teacher_email)

   if not teacher_classes:
       st.info("You haven't created any classes yet. Use '+ Create New Class' first.")
       return

   selected_id = st.session_state.get("classes_dlg_selected_id")
   selected_class = next((c for c in teacher_classes if c[0] == selected_id), None)

   if not selected_class:
       st.caption("Click a class to manage it.")
       for c_id, c_name, c_code in teacher_classes:
           if st.button(f"{c_name}  —  join code {c_code}", use_container_width=True, key=f"cls_pick_{c_id}"):
               st.session_state["classes_dlg_selected_id"] = c_id
               st.session_state["classes_dlg_action"] = None
               st.rerun()
       return

   c_id, c_name, c_code = selected_class
   st.markdown(f"**{c_name}** — join code `{c_code}`")
   if st.button("< Back to all classes", key="cls_back_btn"):
       st.session_state["classes_dlg_selected_id"] = None
       st.session_state["classes_dlg_action"] = None
       st.rerun()

   st.write("---")

   action = st.session_state.get("classes_dlg_action")

   ac1, ac2, ac3 = st.columns(3, gap="small")
   with ac1:
       if st.button("Post Homework", use_container_width=True, key=f"cls_act_hw_{c_id}"):
           st.session_state["classes_dlg_action"] = "hw"
           st.rerun()
   with ac2:
       if st.button("Add a reminder", use_container_width=True, key=f"cls_act_rem_{c_id}"):
           st.session_state["classes_dlg_action"] = "rem"
           st.rerun()
   with ac3:
       if st.button("Show students", use_container_width=True, key=f"cls_act_stu_{c_id}"):
           st.session_state["classes_dlg_action"] = "stu"
           st.rerun()

   st.write("")

   if action == "hw":
       with st.form(f"cls_hw_form_{c_id}"):
           hw_title = st.text_input("Homework Title (e.g. Worksheet 4)")
           hw_topic = st.text_input("Topic (e.g. Chapter 3 Practice Problems)")
           hw_due = st.date_input(
               "Due Date", datetime.date.today() + datetime.timedelta(days=1), key=f"cls_hw_due_{c_id}"
           )
           if st.form_submit_button("Post Homework & Notify Students"):
               if hw_title.strip():
                   create_assignment(c_id, c_name, hw_title, hw_topic, hw_due.isoformat())
                   st.success(f"Homework '{hw_title}' posted! AI notified enrolled students.")
               else:
                   st.warning("Give the homework a title first.")

   elif action == "rem":
       with st.form(f"cls_rem_form_{c_id}"):
           t_title = st.text_input("Test Title (e.g. Midterm Chapter 3)")
           t_subject = st.text_input("Subject (e.g. Physics)")
           t_topic = st.text_input("Topic (e.g. Kinematics)")
           t_date = st.date_input(
               "Test Date", datetime.date.today() + datetime.timedelta(days=2), key=f"cls_rem_date_{c_id}"
           )
           if st.form_submit_button("Schedule Test & Notify Students"):
               if t_title.strip():
                   add_class_test(c_id, t_title, t_subject, t_topic, t_date.isoformat())
                   st.success(f"Test '{t_title}' scheduled! AI will notify enrolled students 3 days prior.")
               else:
                   st.warning("Give the test a title first.")

   elif action == "stu":
       students = get_students_for_class(c_id)
       if not students:
           st.caption("No students have been approved into this class yet.")
       else:
           st.caption(f"{len(students)} student(s) enrolled:")
           for s_name, s_email in students:
               st.markdown(f"- **{s_name or s_email}** ({s_email})")


@st.dialog("Join Requests")
def render_join_requests_dialog(teacher_email):
   pending = get_pending_join_requests(teacher_email)
   if not pending:
       st.info("No pending join requests right now.")
       return

   for req_id, req_email, req_name, req_class_name in pending:
       with st.container(border=True):
           st.markdown(f"**{req_name or req_email}** wants to join **'{req_class_name}'**")
           jc1, jc2 = st.columns(2)
           with jc1:
               if st.button("Allow", key=f"jr_allow_{req_id}", use_container_width=True):
                   update_join_request_status(req_id, "approved")
                   st.rerun()
           with jc2:
               if st.button("Decline", key=f"jr_decline_{req_id}", use_container_width=True):
                   update_join_request_status(req_id, "declined")
                   st.rerun()


def render_teacher_home_page(profile):
   user_name = profile["name"] if profile else "Teacher"
   teacher_email = profile["email"] if profile else ""


   col_title, col_upg = st.columns([4, 1])
   with col_title:
       st.markdown(
           f"""
          <div class="dash-header">
              <h1>Welcome back, {user_name}!</h1>
              <p style="color: #64748b; font-size: 1.05rem;">Role: <b>Teacher</b></p>
          </div>
      """,
           unsafe_allow_html=True,
       )
   with col_upg:
       st.write("")
       if st.button(
               "Upgrade", use_container_width=True, key="teacher_header_upgrade"
       ):
           render_upgrade_dialog(profile)


   pending_requests = get_pending_join_requests(teacher_email)
   if pending_requests:
       st.write("")
       for req_id, req_email, req_name, req_class_name in pending_requests:
           with st.container(border=True):
               rc1, rc2, rc3 = st.columns([3, 1, 1])
               with rc1:
                   st.markdown(f"**{req_name or req_email}** wants to join **'{req_class_name}'**")
               with rc2:
                   if st.button("Allow", key=f"allow_{req_id}", use_container_width=True):
                       update_join_request_status(req_id, "approved")
                       st.rerun()
               with rc3:
                   if st.button("Decline", key=f"decline_{req_id}", use_container_width=True):
                       update_join_request_status(req_id, "declined")
                       st.rerun()


   # Same in-app due-soon banner as the student home page — teachers create
   # reminders too (render_add_reminder_dialog), through the same reminders
   # table, so they need the same live alert.
   teacher_due_soon = get_due_soon_reminders(teacher_email)
   if teacher_due_soon:
       for rem_id, rem_title, rem_due, minutes_left in teacher_due_soon:
           try:
               rem_due_display = datetime.datetime.fromisoformat(rem_due).strftime("%I:%M %p")
           except ValueError:
               rem_due_display = rem_due
           when_text = "Starting now" if minutes_left <= 0 else f"Starting in about {int(round(minutes_left))} minutes"
           t_rem_card_col, t_rem_btn_col = st.columns([5, 1.3], vertical_alignment="center")
           with t_rem_card_col:
               st.markdown(
                   f"""
                   <div class="reminder-alert-card">
                       <h4 style="margin:0; color: #92400e;">Reminder: {rem_title}</h4>
                       <p style="margin:4px 0 0 0; color: #b45309;"><b>{when_text}</b> — {rem_due_display}</p>
                   </div>
                   """,
                   unsafe_allow_html=True,
               )
           with t_rem_btn_col:
               if st.button("✓ Got it, dismiss", use_container_width=True, key=f"dismiss_rem_teacher_{rem_id}"):
                   dismiss_notification(teacher_email, "reminder", rem_id)
                   st.rerun()


   m1, m2, m3 = st.columns(3)
   with m1:
       st.markdown(
           f'<div class="metric-card"><p>TOTAL SESSIONS</p><h3>{len(fetch_logs(teacher_email))}</h3></div>',
           unsafe_allow_html=True,
       )
   with m2:
       st.markdown(
           f'<div class="metric-card"><p>HOMEWORKS ASSIGNED</p><h3>{get_class_assignment_count(teacher_email)}</h3></div>',
           unsafe_allow_html=True,
       )
   with m3:
       st.markdown(
           f'<div class="metric-card"><p>TARGET SUBJECTS</p><h3 style="font-size:1.1rem; overflow:hidden; text-overflow:ellipsis;">{profile["interests"] if profile else "General"}</h3></div>',
           unsafe_allow_html=True,
       )


   w1, w2, w3 = st.columns(3)
   with w2:
       if st.button("+ Create New Class", use_container_width=True, key="btn_create_class_widget"):
           render_create_class_dialog(teacher_email)


   teacher_classes = get_teacher_classes(teacher_email)

   st.write("---")

   if not teacher_classes:
       st.info("Create a class first (use the '+ Create New Class' widget above) to schedule tests or post homework.")
   else:
       bubble_col1, bubble_col2 = st.columns(2, gap="small")
       with bubble_col1:
           if st.button("Post Homework", use_container_width=True, key="btn_post_homework_widget"):
               render_post_homework_dialog(teacher_email)
       with bubble_col2:
           if st.button("Add a reminder to the class", use_container_width=True, key="btn_add_reminder_widget"):
               render_add_reminder_dialog(teacher_email)



# =========================================================
# STUDENT HOME PAGE (WITH TEST NOTIFICATION BANNERS)
# =========================================================
@st.dialog("Your Classes")
def render_student_classes_dialog(student_email):
   student_classes = get_student_classes(student_email)

   if not student_classes:
       st.info("You're not enrolled in any classes yet. Use '+ Join Class' to request to join one.")
       return

   selected_id = st.session_state.get("student_classes_dlg_selected_id")
   selected_class = next((c for c in student_classes if c[0] == selected_id), None)

   if not selected_class:
       st.caption("Click a class to see its homework and tests.")
       for c_id, c_name, c_teacher_email, c_code in student_classes:
           if st.button(f"{c_name}  —  {c_teacher_email}", use_container_width=True, key=f"stu_cls_pick_{c_id}"):
               st.session_state["student_classes_dlg_selected_id"] = c_id
               st.rerun()
       return

   c_id, c_name, c_teacher_email, c_code = selected_class
   st.markdown(f"**{c_name}**")
   st.caption(f"Teacher: {c_teacher_email}")
   if st.button("< Back to all classes", key="stu_cls_back_btn"):
       st.session_state["student_classes_dlg_selected_id"] = None
       st.rerun()

   st.write("---")

   st.markdown("##### Homework")
   homework = get_class_homework(c_id)
   if not homework:
       st.caption("No homework posted yet.")
   else:
       for hw_title, hw_topic, hw_due in homework:
           st.markdown(f"- **{hw_title}** — {hw_topic} (due {hw_due})")

   st.write("")
   st.markdown("##### Upcoming Tests")
   tests = get_class_tests_for_class(c_id)
   if not tests:
       st.caption("No tests scheduled yet.")
   else:
       for t_title, t_subject, t_topic, t_date in tests:
           st.markdown(f"- **{t_title}** — {t_subject} / {t_topic} ({t_date})")


def render_home_page(profile):
   # Re-runs this page every 30s so the due-soon reminder banner can appear
   # on its own, without the student/teacher needing to click anything first.
   # Scoped to the home page only (not the whole app) so it never interrupts
   # someone mid-chat on the AI Tutor page.
   st_autorefresh(interval=30_000, key="home_due_soon_autorefresh")

   if profile and profile.get("role") == "teacher":
       render_teacher_home_page(profile)
       return


   user_name = profile["name"] if profile else "Student"
   user_email = profile["email"] if profile else ""
   user_role = (
       profile.get("role", "student").capitalize() if profile else "Student"
   )
   user_grade = profile.get("grade", "") if profile else ""


   col_title, col_upg = st.columns([4, 1])
   with col_title:
       st.markdown(
           f"""
          <div class="dash-header">
              <h1>Welcome back, {user_name}!</h1>
              <p style="color: #64748b; font-size: 1.05rem;">Role: <b>{user_role}</b> {f'({user_grade})' if user_grade else ''}</p>
          </div>
      """,
           unsafe_allow_html=True,
       )
   with col_upg:
       st.write("")
       if st.button("Upgrade", use_container_width=True, key="header_upgrade_btn"):
           render_upgrade_dialog(profile)


   check_and_notify_upcoming_tests(user_email)  # fires the one-time push notification only
   upcoming_tests = get_upcoming_tests_for_display(user_email)
   if upcoming_tests:
       for t_id, title, subj, top, t_date in upcoming_tests:
           test_card_col, test_btn_col = st.columns([5, 1.3], vertical_alignment="center")
           with test_card_col:
               st.markdown(
                   f"""
                   <div class="test-alert-card">
                       <h4 style="margin:0; color: #991b1b;">Upcoming Test Alert: {title}</h4>
                       <p style="margin:4px 0 0 0; color: #7f1d1d;"><b>Subject:</b> {subj} | <b>Topic:</b> {top} | <b>Date:</b> {t_date}</p>
                   </div>
                   """,
                   unsafe_allow_html=True,
               )
           with test_btn_col:
               if st.button(f"Generate Mock Test & Notes for {top}", use_container_width=True, key=f"gen_mock_{t_id}"):
                   with st.spinner("Analyzing stored topic history and generating custom mock test..."):
                       mock_res = generate_mock_test_from_memory(user_email, target_subject=top)
                       st.session_state["messages"] = [
                           {"role": "user", "content": f"Generate mock test for upcoming {subj} test on {top}."},
                           {"role": "assistant", "content": mock_res, "help_stage": 3}
                       ]
                       start_new_chat_session(user_email, st.session_state["messages"])
                       st.session_state["page"] = "AI Tutor"
                       st.rerun()
               if st.button("✓ Already prepared, dismiss", use_container_width=True, key=f"dismiss_test_{t_id}"):
                   dismiss_notification(user_email, "test", t_id)
                   st.rerun()


   upcoming_homework = get_student_upcoming_homework(user_email)
   if upcoming_homework:
       for hw_idx, (hw_id, hw_title, hw_topic, hw_due, hw_class_name) in enumerate(upcoming_homework):
           hw_card_col, hw_btn_col = st.columns([5, 1.3], vertical_alignment="center")
           with hw_card_col:
               st.markdown(
                   f"""
                   <div class="homework-alert-card">
                       <h4 style="margin:0; color: #1e3a8a;">Homework: {hw_title}</h4>
                       <p style="margin:4px 0 0 0; color: #1e40af;"><b>Class:</b> {hw_class_name} | <b>Topic:</b> {hw_topic} | <b>Due:</b> {hw_due}</p>
                   </div>
                   """,
                   unsafe_allow_html=True,
               )
           with hw_btn_col:
               if st.button("Want help with this?", use_container_width=True, key=f"hw_help_btn_{hw_idx}"):
                   st.session_state["messages"] = [
                       {
                           "role": "assistant",
                           "content": (
                               f"Sure! Upload a photo of the worksheet or problem for **'{hw_title}'** "
                               f"({hw_class_name} — {hw_topic}) using the box below, and I'll walk you "
                               f"through it step by step."
                           ),
                       }
                   ]
                   start_new_chat_session(
                       user_email, st.session_state["messages"], title=f"Help with: {hw_title}"
                   )
                   st.session_state["page"] = "AI Tutor"
                   st.rerun()
               if st.button("✓ Done, dismiss", use_container_width=True, key=f"dismiss_hw_{hw_id}"):
                   dismiss_notification(user_email, "homework", hw_id)
                   st.rerun()


   # In-app "due soon" banner for personal reminders (10 minutes out, re-shown
   # until 5 minutes out, then gone after a 15-minute grace window) — replaces
   # the old OneSignal push path with something that doesn't depend on a browser
   # permission prompt. Works for both student and teacher reminders, since both
   # go through the same create_reminder()/reminders table.
   due_soon_reminders = get_due_soon_reminders(user_email)
   if due_soon_reminders:
       for rem_id, rem_title, rem_due, minutes_left in due_soon_reminders:
           try:
               rem_due_display = datetime.datetime.fromisoformat(rem_due).strftime("%I:%M %p")
           except ValueError:
               rem_due_display = rem_due
           if minutes_left <= 0:
               when_text = "Starting now"
           else:
               when_text = f"Starting in about {int(round(minutes_left))} minutes"
           rem_card_col, rem_btn_col = st.columns([5, 1.3], vertical_alignment="center")
           with rem_card_col:
               st.markdown(
                   f"""
                   <div class="reminder-alert-card">
                       <h4 style="margin:0; color: #92400e;">Reminder: {rem_title}</h4>
                       <p style="margin:4px 0 0 0; color: #b45309;"><b>{when_text}</b> — {rem_due_display}</p>
                   </div>
                   """,
                   unsafe_allow_html=True,
               )
           with rem_btn_col:
               if st.button("✓ Got it, dismiss", use_container_width=True, key=f"dismiss_rem_{rem_id}"):
                   dismiss_notification(user_email, "reminder", rem_id)
                   st.rerun()


   logs = fetch_logs(user_email)
   quizzes = fetch_quiz_results(user_email)


   m1, m2, m3 = st.columns(3)
   with m1:
       st.markdown(
           f'<div class="metric-card"><p>TOTAL SESSIONS</p><h3>{len(logs)}</h3></div>',
           unsafe_allow_html=True,
       )
   with m2:
       st.markdown(
           f'<div class="metric-card"><p>QUIZZES COMPLETED</p><h3>{len(quizzes)}</h3></div>',
           unsafe_allow_html=True,
       )
   with m3:
       st.markdown(
           f'<div class="metric-card"><p>TARGET SUBJECTS</p><h3 style="font-size:1.1rem; overflow:hidden; text-overflow:ellipsis;">{profile["interests"] if profile else "General"}</h3></div>',
           unsafe_allow_html=True,
       )


   sw1, sw2, sw3 = st.columns(3)
   with sw2:
       if st.button("+ Join Class", use_container_width=True, key="btn_join_class_widget"):
           render_join_class_dialog(user_email, user_name)



# =========================================================
# AI TUTOR PAGE (CLEAN CHAT LANDING & INPUT)
# =========================================================
def render_tutor(profile):
   user_name = profile["name"] if profile else "Student"
   user_email = profile["email"] if profile else ""
   user_tier = profile.get("tier", "freemium") if profile else "freemium"
   user_grade = profile.get("grade", "") if profile else ""


   if "messages" not in st.session_state:
       st.session_state.messages = []


   if len(st.session_state.messages) == 0:
       logo_file = get_logo_path()
       b64_logo = get_base64_image(logo_file) if logo_file else ""
       logo_html = (
           f'<img src="data:image/png;base64,{b64_logo}" alt="Logo"/>'
           if b64_logo
           else ""
       )


       st.markdown(
           f"""
          <div class="tutor-gemini-landing">
              {logo_html}
              <h1>Hi {user_name}, let's get into it</h1>
          </div>
      """,
           unsafe_allow_html=True,
       )


   for msg in st.session_state.messages:
       with st.chat_message(msg["role"]):
           if msg.get("image_bytes"):
               st.image(msg["image_bytes"], caption="Uploaded Screenshot", width=300)
           st.markdown(msg["content"])


   chat_input_data = st.chat_input(
       "Ask a question or drop a screenshot here...",
       accept_file=True,
       file_type=["png", "jpg", "jpeg", "webp"],
   )


   if chat_input_data:
       prompt = chat_input_data["text"] or "Please analyze this image."
       uploaded_files = chat_input_data["files"]


       image_bytes = None
       mime_type = None


       if uploaded_files:
           file = uploaded_files[0]
           image_bytes = file.getvalue()
           mime_type = file.type


       log_session(prompt, user_email)


       st.session_state.messages.append(
           {
               "role": "user",
               "content": prompt,
               "image_bytes": image_bytes,
               "mime_type": mime_type,
           }
       )


       if not st.session_state.get("current_chat_session_id"):
           st.session_state["current_chat_session_id"] = create_chat_session(user_email, prompt)
       save_chat_message(
           st.session_state["current_chat_session_id"],
           "user", prompt, image_bytes=image_bytes, mime_type=mime_type,
       )


       with st.chat_message("user"):
           if image_bytes:
               st.image(image_bytes, caption="Uploaded Screenshot", width=300)
           st.markdown(prompt)


       # 3-step tutor rule: step 1 = method only, step 2 = a hint with a similar
       # worked example (triggered when the student says they're confused), step 3 =
       # the full walkthrough (triggered if they're still confused after the hint).
       last_help_stage = 1
       for prior_msg in reversed(st.session_state.messages[:-1]):
           if prior_msg.get("role") == "assistant" and "help_stage" in prior_msg:
               last_help_stage = prior_msg["help_stage"]
               break

       if is_confused_message(prompt):
           next_help_stage = min(last_help_stage + 1, 3)
       else:
           next_help_stage = 1

       stage_to_mode = {1: "method", 2: "hint", 3: "full"}
       chosen_mode = stage_to_mode[next_help_stage]

       with st.chat_message("assistant"):
           with st.spinner("Analyzing screenshot and thinking..."):
               reply = generate_ai_response(
                   prompt,
                   image_bytes=image_bytes,
                   mime_type=mime_type,
                   mode=chosen_mode,
                   user_tier=user_tier,
                   grade=user_grade,
                   user_email=user_email,
                   history=st.session_state.messages[:-1],
               )


       st.session_state.messages.append(
           {
               "role": "assistant",
               "content": reply,
               "help_stage": next_help_stage,
               "original_prompt": prompt,
           }
       )
       save_chat_message(
           st.session_state["current_chat_session_id"],
           "assistant", reply, help_stage=next_help_stage, original_prompt=prompt,
       )
       st.rerun()




# =========================================================
# MAIN APP ENTRY POINT WITH DIALOG HANDLER
# =========================================================
def render_onesignal_web_push_snippet(user_email):
   """Loads the OneSignal Web SDK on the real page (not a sandboxed iframe) and tags
   this browser with the logged-in user's email, so server-side send_onesignal_notification
   calls (which filter by that same 'email' tag) can actually reach this device.

   Requires ONESIGNAL_APP_ID to have a Web Push platform configured in the OneSignal
   dashboard, with its Site URL set to wherever this app is actually deployed — that
   part has to be done by hand in the OneSignal dashboard, it isn't something code can do.
   """
   if not ONESIGNAL_APP_ID:
       return
   # Only inject the SDK <script> once per browser session, not on every Streamlit rerun.
   if st.session_state.get("_onesignal_sdk_loaded"):
       return
   st.session_state["_onesignal_sdk_loaded"] = True

   safe_email = user_email.lower().strip().replace('"', "")
   st.html(
       f"""
       <script src="https://cdn.onesignal.com/sdks/web/v16/OneSignalSDK.page.js" defer></script>
       <script>
         window.OneSignalDeferred = window.OneSignalDeferred || [];
         OneSignalDeferred.push(async function(OneSignal) {{
           await OneSignal.init({{ appId: "{ONESIGNAL_APP_ID}" }});
           await OneSignal.User.addTag("email", "{safe_email}");
         }});
       </script>
       """,
       unsafe_allow_javascript=True,
   )


# =========================================================
# PRIVACY, CONSENT, DATA DOWNLOAD AND ACCOUNT DELETION
# =========================================================
# Who people can contact about their data. Change this text if it changes.
PRIVACY_CONTACT = "the creator of StudySpace (Daivya Chaudhary, TIK) or your teacher or supervisor at school"

PRIVACY_NOTICE_MD = f"""
**StudySpace is a school project** (an MYP Personal Project at TIK). It is a prototype made by a student, not a company.

**What StudySpace stores about you**
- **Account:** your name, email, whether you are a student or teacher, your grade, your main goal, your subjects and your study schedule.
- **What you do in the app:** your chats with the AI tutor and any homework photos you upload, topics saved from your questions (you can switch this off in the sidebar), study logs, quiz results, reminders and a daily usage counter.
- **Classes:** the classes you create or join, join requests, homework and test dates. A teacher can see the name and email of students in their class.
- **Staying logged in:** if you stay logged in, only a scrambled (hashed) token is stored, for up to 30 days. Login codes stop working after 10 minutes.

**Why:** to give you the tutor, homework planner, reminders and classes. You agree to this when you create your account.

**Who else handles your data**
- **Google (Gemini):** your messages and any photos you upload are sent to Google's Gemini AI to write the answers. On free plans, Google's terms may let it use this content to improve its products, so do not upload anything private.
- **Supabase:** the database that stores your data. The servers are in the EU (Ireland).
- **Streamlit Community Cloud:** hosts the app.
- **Gmail (Google):** sends your login code by email.
- **OneSignal:** if notifications are on, your email is attached to your browser so reminders can reach you.

**How long:** until you delete your account. You can do that in this window, under "Delete my account".

**Your choices**
- **See or download your data:** use the "Download my data" tab.
- **Correct your details:** use "Edit My Profile" in the sidebar.
- **Delete everything:** use the "Delete my account" tab. This removes your data from StudySpace's database. Data already held by OneSignal or sent to Google is handled under their own rules.
- **Questions or requests:** ask {PRIVACY_CONTACT}.
- **Complaints:** in Estonia you can contact the Data Protection Inspectorate (Andmekaitse Inspektsioon).

**Age:** if you are younger than the age at which you may agree to online services in your country, ask a parent or guardian before signing up.
"""


def render_privacy_notice():
    st.markdown(PRIVACY_NOTICE_MD)


def record_consent(email):
    """Stores when the person ticked the privacy box at sign-up."""
    now_txt = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE user_profile SET consent_at = ? WHERE LOWER(email) = ?",
        (now_txt, email.lower().strip()),
    )
    conn.commit()
    conn.close()


def export_user_data(email):
    """Everything StudySpace holds about this person, as a plain dict (for JSON download)."""
    e = email.lower().strip()
    conn = db.connect()
    cursor = conn.cursor()

    def grab(sql, params, cols):
        cursor.execute(sql, params)
        return [dict(zip(cols, row)) for row in cursor.fetchall()]

    out = {"exported_at_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
           "email": e}
    out["profile"] = grab(
        "SELECT name, email, purpose, interests, schedule, tier, role, grade, consent_at "
        "FROM user_profile WHERE LOWER(email) = ?", (e,),
        ["name", "email", "purpose", "interests", "schedule", "tier", "role", "grade", "consent_at"])
    out["study_logs"] = grab("SELECT topic, timestamp FROM study_logs WHERE LOWER(email) = ?", (e,),
                             ["topic", "timestamp"])
    out["quiz_results"] = grab("SELECT subject, score, total, timestamp FROM quiz_results WHERE LOWER(email) = ?",
                               (e,), ["subject", "score", "total", "timestamp"])
    out["daily_usage"] = grab("SELECT date_str, count FROM daily_rpd_usage WHERE LOWER(email) = ?", (e,),
                              ["date", "count"])
    out["reminders"] = grab("SELECT title, due_at, created_at FROM reminders WHERE LOWER(user_email) = ?", (e,),
                            ["title", "due_at", "created_at"])
    out["saved_topics"] = grab(
        "SELECT subject, topic, specific_area, raw_text, timestamp FROM knowledge_items WHERE LOWER(email) = ?",
        (e,), ["subject", "topic", "specific_area", "raw_text", "timestamp"])
    out["class_memberships"] = grab(
        "SELECT class_id, student_name, status FROM class_enrollment WHERE LOWER(student_email) = ?", (e,),
        ["class_id", "student_name", "status"])
    out["dismissed_notifications"] = grab(
        "SELECT notif_type, ref_id, dismissed_at FROM notification_dismissals WHERE LOWER(student_email) = ?",
        (e,), ["type", "ref_id", "dismissed_at"])

    chats = grab("SELECT id, title, created_at, updated_at FROM chat_sessions WHERE LOWER(email) = ?", (e,),
                 ["id", "title", "created_at", "updated_at"])
    for c in chats:
        cursor.execute(
            "SELECT role, content, image_data, mime_type, created_at FROM chat_messages "
            "WHERE session_id = ? ORDER BY id", (c["id"],))
        msgs = []
        for role, content, image_data, mime_type, created_at in cursor.fetchall():
            m = {"role": role, "content": content, "created_at": created_at}
            if image_data:
                m["image_mime_type"] = mime_type
                m["image_base64"] = base64.b64encode(bytes(image_data)).decode("ascii")
            msgs.append(m)
        c["messages"] = msgs
    out["chats"] = chats

    classes = grab("SELECT id, class_name, join_code, created_at FROM classes WHERE LOWER(teacher_email) = ?",
                   (e,), ["id", "class_name", "join_code", "created_at"])
    for c in classes:
        cursor.execute("SELECT title, topic, due_date FROM assignments WHERE class_id = ?", (c["id"],))
        c["homework"] = [dict(zip(["title", "topic", "due_date"], r)) for r in cursor.fetchall()]
        cursor.execute("SELECT test_title, subject, topic, test_date FROM class_tests WHERE class_id = ?", (c["id"],))
        c["tests"] = [dict(zip(["title", "subject", "topic", "date"], r)) for r in cursor.fetchall()]
    out["classes_i_teach"] = classes
    conn.close()
    return out


def delete_user_account(email):
    """Permanently removes everything StudySpace stores about this person. All-or-nothing."""
    e = email.lower().strip()
    conn = db.connect()
    cursor = conn.cursor()
    steps = [
        ("DELETE FROM chat_messages WHERE session_id IN (SELECT id FROM chat_sessions WHERE LOWER(email) = ?)", (e,)),
        ("DELETE FROM chat_sessions WHERE LOWER(email) = ?", (e,)),
        ("DELETE FROM study_logs WHERE LOWER(email) = ?", (e,)),
        ("DELETE FROM quiz_results WHERE LOWER(email) = ?", (e,)),
        ("DELETE FROM daily_rpd_usage WHERE LOWER(email) = ?", (e,)),
        ("DELETE FROM reminders WHERE LOWER(user_email) = ?", (e,)),
        ("DELETE FROM knowledge_items WHERE LOWER(email) = ?", (e,)),
        ("DELETE FROM login_tokens WHERE LOWER(email) = ?", (e,)),
        ("DELETE FROM notification_dismissals WHERE LOWER(student_email) = ?", (e,)),
        ("DELETE FROM class_enrollment WHERE LOWER(student_email) = ?", (e,)),
        # Classes this person created as a teacher, and everything inside them
        ("DELETE FROM class_tests WHERE class_id IN (SELECT id FROM classes WHERE LOWER(teacher_email) = ?)", (e,)),
        ("DELETE FROM assignments WHERE class_id IN (SELECT id FROM classes WHERE LOWER(teacher_email) = ?)", (e,)),
        ("DELETE FROM class_enrollment WHERE class_id IN (SELECT id FROM classes WHERE LOWER(teacher_email) = ?)", (e,)),
        ("DELETE FROM classes WHERE LOWER(teacher_email) = ?", (e,)),
        ("DELETE FROM user_profile WHERE LOWER(email) = ?", (e,)),
    ]
    try:
        for sql, params in steps:
            cursor.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


@st.dialog("Privacy & My Data", width="large")
def render_privacy_and_data_dialog(profile):
    user_email = profile["email"]
    is_teacher = profile.get("role", "student") == "teacher"
    tab_notice, tab_download, tab_delete = st.tabs(["Privacy notice", "Download my data", "Delete my account"])

    with tab_notice:
        render_privacy_notice()

    with tab_download:
        st.write("Download a copy of the information StudySpace stores about you (including your chats and any "
                 "photos you uploaded) as a JSON file.")
        try:
            payload = json.dumps(export_user_data(user_email), indent=2, default=str)
            st.download_button(
                "Download my data (.json)",
                data=payload,
                file_name="my_studyspace_data.json",
                mime="application/json",
                use_container_width=True,
                key="btn_download_my_data",
            )
        except Exception as ex:
            st.error(f"Couldn't prepare your data: {ex}")

    with tab_delete:
        st.warning(
            "This permanently deletes your account and everything StudySpace stores about you: profile, chats, "
            "photos, reminders, quiz results and class memberships. It cannot be undone."
        )
        if is_teacher:
            st.warning("You are a teacher: your classes, homework and test dates will be deleted too, and your "
                       "students will lose access to them.")
        st.caption("Tip: use the Download tab first if you want a copy.")
        typed = st.text_input("To confirm, type DELETE", key="delete_confirm_text")
        if st.button("Delete my account permanently", type="primary", use_container_width=True,
                     disabled=(typed.strip() != "DELETE"), key="btn_delete_account"):
            try:
                delete_user_account(user_email)
            except Exception as ex:
                st.error(f"Couldn't delete your account: {ex}")
            else:
                token = st.session_state.get("_remember_token") or read_remember_cookie()
                if token:
                    delete_login_token(token)
                st.session_state.clear()
                st.session_state["_clear_cookie"] = True
                st.session_state["_deleted_notice"] = True
                st.rerun()



def main():
   if "account_action" in st.session_state:
       action, target_email = st.session_state.pop("account_action")
       if action == "add":
           st.session_state["active_email"] = None
           st.session_state["is_logged_in"] = False
           st.session_state["wizard_step"] = 1
           st.session_state["email_verified"] = False
           st.session_state["otp_sent"] = False
           st.session_state["otp_email_confirmed"] = False
           st.session_state["form_data"] = {
               "name": "",
               "email": "",
               "role": "student",
               "grade": "9th Grade",
               "purpose": "Preparing for upcoming exams & tests",
               "interests": "",
               "schedule": "",
           }
           st.rerun()
       elif action == "signout":
           delete_login_token(st.session_state.get("_remember_token") or read_remember_cookie())
           st.session_state.clear()
           st.session_state["_clear_cookie"] = True
           st.rerun()

   # Returning visitor with a valid "stay logged in" cookie: log them in without an OTP.
   bridge_token = read_bridge_token()
   if not st.session_state.get("active_email"):
       _cookie_token = read_remember_cookie() or bridge_token
       remembered_email = lookup_login_token(_cookie_token)
       if remembered_email and fetch_user_profile(remembered_email):
           st.session_state["active_email"] = remembered_email
           st.session_state["_remember_token"] = _cookie_token

   emit_cookie_script()

   # Troubleshooting aid: open the site with ?debug=1 while logged out to see why
   # "stay logged in" did or didn't work. Shows no personal data.
   if st.query_params.get("debug") == "1" and not st.session_state.get("active_email"):
       _ck = read_remember_cookie()
       try:
           _server_names = sorted(st.context.cookies.keys())
       except Exception:
           _server_names = []
       st.caption(
           f"debug: cookie received by server = {bool(_ck)} | received via browser bridge = {bool(bridge_token)} | "
           f"matches a saved login = {bool(lookup_login_token(_ck or bridge_token))} | "
           f"saved logins in database = {count_login_tokens()} | cookies the server can see = {_server_names}"
       )
       components.html(
           "<div id='o' style='font:12px sans-serif;color:#555'></div><script>"
           "var n=document.cookie.split(';').map(function(c){return c.trim().split('=')[0]}).filter(Boolean);"
           "document.getElementById('o').textContent='debug (browser side): cookie names the page can see = ['"
           "+n.join(', ')+']  |  has studyspace_token = '+(n.indexOf('studyspace_token')>=0);"
           "</script>",
           height=24,
       )


   current_active = st.session_state.get("active_email")
   active_profile = fetch_user_profile(current_active) if current_active else None


   if not current_active or not active_profile or not active_profile.get("is_onboarded", False):
       render_onboarding_wizard()
       return


   if "page" not in st.session_state:
       st.session_state["page"] = "Home"


   user_name = active_profile["name"]
   user_email = active_profile["email"]
   user_tier_raw = active_profile.get("tier", "freemium").lower()
   user_tier_display = user_tier_raw.capitalize().replace("_", " ")

   render_onesignal_web_push_snippet(user_email)


   # RETRIEVE TIER CONFIGURATION & USAGE
   current_cfg = TIER_CONFIG.get(user_tier_raw, TIER_CONFIG["freemium"])
   rpd_max = current_cfg["limit"]
   num_sections = current_cfg["sections"]
   rpd_used = get_current_rpd_count(user_email)


   with st.sidebar:
       logo_path = get_logo_path()
       if logo_path:
           b64_logo = get_base64_image(logo_path)
           logo_img_tag = f'<img src="data:image/png;base64,{b64_logo}" class="sidebar-logo-img" alt="Logo"/>'
       else:
           logo_img_tag = ""


       st.markdown(
           f'<a href="?nav=home" target="_self" class="sidebar-logo-button">{logo_img_tag}<span>StudySpace</span></a>',
           unsafe_allow_html=True,
       )


       st.write("---")


       # Superseded by the in-app due-soon banner on the home page — see
       # get_due_soon_reminders. This OneSignal push call is no longer used.
       # check_and_notify_reminders(user_email)


       if st.button("New Chat", use_container_width=True):
           st.session_state["messages"] = []
           st.session_state["current_chat_session_id"] = None
           st.session_state["page"] = "AI Tutor"
           st.rerun()


       if st.button("Search Chats", use_container_width=True):
           st.session_state["show_search"] = not st.session_state.get(
               "show_search", False
           )
           st.rerun()


       if st.session_state.get("show_search"):
           search_query = st.text_input(
               "Search keywords:",
               key="search_chats_input",
               placeholder="e.g. Physics",
           )
           if search_query:
               past_logs = fetch_logs(user_email)
               matches = [
                   log
                   for log in past_logs
                   if search_query.lower() in log[0].lower()
               ]
               if matches:
                   st.caption(f"Found {len(matches)} match(es):")
                   for topic, t_stamp in matches[:5]:
                       if st.button(
                               f"{topic[:22]}...", key=f"s_log_{t_stamp}"
                       ):
                           st.session_state["messages"] = [
                               {"role": "user", "content": topic}
                           ]
                           start_new_chat_session(user_email, st.session_state["messages"])
                           st.session_state["page"] = "AI Tutor"
                           st.rerun()
               else:
                   st.caption("No matching chats found.")


       # SIDEBAR "STUDY FOR A TEST" BUTTON & MEMORY SAVE TOGGLE
       col_test_btn, col_test_toggle = st.columns([3, 1], vertical_alignment="center")
       with col_test_btn:
           if st.button("Study for a test", use_container_width=True, key="btn_study_for_test"):
               st.session_state["show_test_prep"] = not st.session_state.get("show_test_prep", False)
               st.rerun()


       with col_test_toggle:
           current_save_mem = active_profile.get("save_topic_memory", True)
           save_mem_toggle = st.toggle(
               "",
               value=current_save_mem,
               key="save_topic_memory_toggle",
               help="Toggle on/off automatically saving key study topics for test prep.",
           )
           if save_mem_toggle != current_save_mem:
               update_save_topic_memory(user_email, save_mem_toggle)
               st.rerun()


       # SEARCH OVERLAY WHEN "STUDY FOR A TEST" IS CLICKED
       if st.session_state.get("show_test_prep"):
           st.markdown("##### Test Prep & Revision")
           subject_query = st.text_input(
               "Search Subject:",
               key="test_prep_subject_input",
               placeholder="e.g. Physics, Math, Biology",
           )


           if subject_query:
               matching_items = fetch_stored_topics(user_email, subject=subject_query)
               if matching_items:
                   st.caption(f"Found {len(matching_items)} topic entry(ies) for '{subject_query}':")
                   for s, t, a, r, ts in matching_items[:3]:
                       st.markdown(f"- **{t}**: *{a}*")


                   if st.button(f"Generate Practice Test for {subject_query}", key="btn_gen_subj_test",
                                use_container_width=True):
                       with st.spinner(f"Generating practice test for {subject_query}..."):
                           mock_res = generate_mock_test_from_memory(user_email, target_subject=subject_query)
                           st.session_state["messages"] = [
                               {"role": "user", "content": f"Study for a test: {subject_query}"},
                               {"role": "assistant", "content": mock_res, "help_stage": 3}
                           ]
                           start_new_chat_session(user_email, st.session_state["messages"])
                           st.session_state["page"] = "AI Tutor"
                           st.session_state["show_test_prep"] = False
                           st.rerun()
               else:
                   st.caption(f"No previous topics saved for '{subject_query}' yet.")


           with st.expander("Learn more", expanded=False):
               st.markdown(
                   """
                   <div class="learn-more-box">
                       <b>How Test Prep Works:</b><br/>
                       When you ask questions or drop homework screenshots, the AI keeps a quick note of the main subject topics (like <i>Calculus</i> or <i>Cell Structure</i>).<br/><br/>
                       This lets you generate custom practice tests and study guides whenever an exam is coming up! You can toggle this on or off anytime using the switch next to <b>Study for a test</b>.
                   </div>
                   """,
                   unsafe_allow_html=True,
               )


       # SIDEBAR "CLASSES" WIDGET (TEACHERS: + REQUESTS. STUDENTS: THEIR OWN CLASSES.)
       if active_profile.get("role", "student") == "teacher":
           pending_request_count = len(get_pending_join_requests(user_email))
           requests_label = (
               f"Requests ({pending_request_count})" if pending_request_count else "Requests"
           )
           if st.button(requests_label, use_container_width=True, key="btn_show_requests"):
               render_join_requests_dialog(user_email)

           if st.button("Classes", use_container_width=True, key="btn_show_classes"):
               render_classes_dialog(user_email)
       else:
           if st.button("Classes", use_container_width=True, key="btn_show_student_classes"):
               render_student_classes_dialog(user_email)

       # SIDEBAR "SCHEDULED" SECTION (PERSONAL REMINDERS)
       if st.button("Scheduled", use_container_width=True, key="btn_show_scheduled"):
           st.session_state["show_scheduled"] = not st.session_state.get("show_scheduled", False)
           st.rerun()

       if st.session_state.get("show_scheduled"):
           if st.button("+ Schedule a Reminder", use_container_width=True, key="btn_schedule_reminder_widget"):
               render_schedule_reminder_dialog(user_email)

           upcoming_reminders = get_upcoming_reminders(user_email)
           if upcoming_reminders:
               for rem_id, rem_title, rem_due in upcoming_reminders:
                   try:
                       rem_due_display = datetime.datetime.fromisoformat(rem_due).strftime("%b %d, %I:%M %p")
                   except ValueError:
                       rem_due_display = rem_due
                   rcol1, rcol2 = st.columns([4, 1], vertical_alignment="center")
                   with rcol1:
                       st.caption(f"**{rem_title}** — {rem_due_display}")
                   with rcol2:
                       if st.button("✕", key=f"del_reminder_{rem_id}", help="Remove reminder"):
                           delete_reminder(rem_id)
                           st.rerun()
           else:
               st.caption("No reminders scheduled yet.")


       st.write("---")


       # =========================================================
       # DYNAMIC SEGMENTED REQUESTS TRACKER
       # =========================================================
       st.caption("REQUESTS USED FOR TODAY")
       st.markdown(f"**Current Tier:** {user_tier_display}")


       usage_ratio = min(rpd_used / rpd_max, 1.0)
       filled_segments = int(usage_ratio * num_sections) if rpd_used > 0 else 0


       segments_html = "".join(
           f'<div class="progress-segment {"active" if i < filled_segments else ""}"></div>'
           for i in range(num_sections)
       )


       st.markdown(
           f'<div class="progress-bar-container">{segments_html}</div>',
           unsafe_allow_html=True,
       )
       st.caption(f"Used today: **{rpd_used} / {rpd_max} RPD**")


       if st.button(
               "Upgrade Plan",
               use_container_width=True,
               key="sidebar_upgrade_btn",
       ):
           render_upgrade_dialog(active_profile)


       st.write("---")


       # =========================================================
       # RECENT CHATS (persisted, resumable conversation history)
       # =========================================================
       st.caption("RECENT CHATS")
       recent_sessions = get_recent_chat_sessions(user_email, limit=8)
       if recent_sessions:
           for sess_id, sess_title, sess_updated in recent_sessions:
               display_title = sess_title if len(sess_title) <= 30 else sess_title[:30] + "..."
               is_active = st.session_state.get("current_chat_session_id") == sess_id
               if st.button(
                       ("• " if is_active else "") + display_title,
                       use_container_width=True,
                       key=f"recent_chat_{sess_id}",
               ):
                   resume_chat_session(sess_id)
       else:
           st.caption("No past chats yet.")


       st.write("---")


       # =========================================================
       # BOTTOM OF SIDEBAR: ACCOUNT
       # =========================================================
       st.caption("ACCOUNT")
       st.write(
           f"**{user_name}** ({active_profile.get('role', 'student').capitalize()})"
       )


       if st.button("Edit My Profile", use_container_width=True, key="btn_edit_profile"):
           render_edit_profile_dialog(active_profile)

       if st.button("Privacy & My Data", use_container_width=True, key="btn_privacy_data"):
           render_privacy_and_data_dialog(active_profile)

       if st.button("Log Out", use_container_width=True, key="btn_logout"):
           st.session_state["account_action"] = ("signout", None)
           st.rerun()


   if st.session_state["page"] == "Home":
       render_home_page(active_profile)
   elif st.session_state["page"] == "AI Tutor":
       render_tutor(active_profile)




if __name__ == "__main__":
   try:
       main()
   except Exception as e:
       st.error(f"An unexpected error occurred: {e}")

