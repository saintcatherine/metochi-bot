"""
Metochi WhatsApp Bot — Vercel Serverless Function
تدفق المحادثة: Metochi ← اللغة ← السنة ← الشهر ← المحافظة ← العقار ← النتيجة
البيانات: مقروءة من ملف JSON يُرفع على Google Drive أو GitHub
"""

from http.server import BaseHTTPRequestHandler
import json, os, urllib.request, urllib.parse

# ─────────────────────────────────────────────
# إعدادات — ضعها في Vercel Environment Variables
# ─────────────────────────────────────────────
VERIFY_TOKEN   = os.environ.get("VERIFY_TOKEN",   "metochi_secret")
WA_TOKEN       = os.environ.get("WA_TOKEN",       "")          # Temporary Access Token من Meta
PHONE_ID       = os.environ.get("PHONE_ID",       "")          # Phone Number ID من Meta
DATA_URL       = os.environ.get("DATA_URL",       "")          # رابط ملف JSON العام
WHITELIST      = set(os.environ.get("WHITELIST",  "").split(","))  # أرقام مصرح لها مفصولة بفاصلة

# ─────────────────────────────────────────────
# GOV_MAP — خريطة المحافظات والعقارات
# (مطابقة تماماً لـ monastery.html)
# ─────────────────────────────────────────────
GOV_MAP = {
    "alex": {
        "ar": "🏙️ الإسكندرية",
        "en": "🏙️ Alexandria",
        "streets": [
            "11 شارع عباس محمود العقاد (البوستة سابقاً)   المنشية  الأسكندرية",
            "7 شارع سعد زغلول  / المنشية الأسكندرية",
        ]
    },
    "cairo": {
        "ar": "🏛️ القاهرة",
        "en": "🏛️ Cairo",
        "streets": [
            "14 شارع سعيد -الظاهر  القاهرة",
            "12 شارع سعيد - الظاهر   القاهرة",
            "10 شارع سعيد - الظاهر     القاهرة",
            "4 شارع طور سيناء    القاهرة",
            "16 شارع البستان    القاهرة",
        ]
    },
    "suez": {
        "ar": "⚓ السويس",
        "en": "⚓ Suez",
        "streets": [
            "23 شارع سعد زغلول - محافظة السويس",
        ]
    },
}

# اسم مختصر للعقار (أول 35 حرف)
def short_street(s):
    import re
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:35] + ('…' if len(s) > 35 else '')

# ─────────────────────────────────────────────
# ترجمات
# ─────────────────────────────────────────────
TEXTS = {
    "welcome":        {"ar": "أهلاً بك في *Metochi* 🏢\nاختر اللغة:", "en": "Welcome to *Metochi* 🏢\nChoose language:"},
    "choose_year":    {"ar": "📅 اختر السنة:", "en": "📅 Choose year:"},
    "choose_month":   {"ar": "📆 اختر الشهر:", "en": "📆 Choose month:"},
    "choose_gov":     {"ar": "🗺️ اختر المحافظة:", "en": "🗺️ Choose governorate:"},
    "choose_prop":    {"ar": "🏠 اختر العقار:", "en": "🏠 Choose property:"},
    "no_data":        {"ar": "⚠️ لا توجد بيانات لهذا الشهر.", "en": "⚠️ No data for this month."},
    "data_error":     {"ar": "⚠️ خطأ في تحميل البيانات، حاول لاحقاً.", "en": "⚠️ Data error, try again later."},
    "expired":        {"ar": "⏰ انتهت الجلسة. أرسل *Metochi* للبدء من جديد.", "en": "⏰ Session expired. Send *Metochi* to restart."},
    "back_menu":      {"ar": "🔙 القائمة الرئيسية", "en": "🔙 Main Menu"},
    "report_header":  {"ar": "📊 *تقرير العقار*", "en": "📊 *Property Report*"},
    "lbl_prop":       {"ar": "🏠 العقار", "en": "🏠 Property"},
    "lbl_period":     {"ar": "📅 الفترة", "en": "📅 Period"},
    "lbl_total":      {"ar": "💰 إجمالي العقار", "en": "💰 Property Total"},
    "lbl_collected":  {"ar": "✅ إجمالي التحصيل", "en": "✅ Total Collected"},
    "lbl_late":       {"ar": "⚠️ إجمالي المتأخرات", "en": "⚠️ Total Arrears"},
    "lbl_expenses":   {"ar": "💸 إجمالي المصروفات", "en": "💸 Total Expenses"},
    "lbl_net":        {"ar": "🏆 الإجمالي النهائي", "en": "🏆 Final Total"},
    "currency":       {"ar": "جنيه", "en": "EGP"},
}

MONTHS_AR = ["يناير","فبراير","مارس","أبريل","مايو","يونيو",
             "يوليو","أغسطس","سبتمبر","أكتوبر","نوفمبر","ديسمبر"]
MONTHS_EN = ["January","February","March","April","May","June",
             "July","August","September","October","November","December"]

def t(key, lang):
    return TEXTS.get(key, {}).get(lang, key)

def fmt(n):
    return f"{n:,.2f}"

def month_label(month_ar, lang):
    if lang == "en":
        try:
            idx = MONTHS_AR.index(month_ar)
            return MONTHS_EN[idx]
        except ValueError:
            return month_ar
    return month_ar

# ─────────────────────────────────────────────
# جلسات المستخدمين (in-memory — تُمسح عند إعادة تشغيل السيرفر)
# ─────────────────────────────────────────────
sessions = {}   # phone → dict

def get_session(phone):
    if phone not in sessions:
        sessions[phone] = {"step": "idle"}
    return sessions[phone]

def reset_session(phone):
    sessions[phone] = {"step": "idle"}

# ─────────────────────────────────────────────
# تحميل بيانات JSON
# ─────────────────────────────────────────────
_cached_data = None

def load_data():
    global _cached_data
    if _cached_data:
        return _cached_data
    try:
        req = urllib.request.Request(DATA_URL, headers={"User-Agent": "Metochi-Bot/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            _cached_data = json.loads(r.read().decode())
        return _cached_data
    except Exception as e:
        print("DATA LOAD ERROR:", e)
        return None

def invalidate_cache():
    global _cached_data
    _cached_data = None

# ─────────────────────────────────────────────
# حساب إجماليات عقار لشهر معين من بيانات JSON
#
# بنية JSON المتوقعة (مطابقة لـ saveAllData في monastery.html):
# {
#   "version": "2.0",
#   "monthly": {
#     "يناير_2025": {
#       "month": "يناير", "year": "2025",
#       "arrearState": { "0": {"late": true}, ... },
#       "collectionRows": { "street_key": [{"name":"..","amount":"500"}, ...] },
#       "expenseRows":    { "street_key": [{"desc":"..","amount":"200"}, ...] },
#       "expSelectedStreets": ["14 شارع سعيد..."],
#       "expGov": "cairo"
#     }
#   }
# }
# ─────────────────────────────────────────────
import re

def safe_street_key(streets):
    """نفس دالة safeStreetKey في monastery.html"""
    return re.sub(r'[^a-zA-Z0-9\u0600-\u06FF]', '_', '|'.join(sorted(streets)))[:60]

def calc_totals(month_data, street):
    """
    يحسب: propTotal, collTotal, lateTotal, expTotal, netTotal
    لعقار واحد في شهر معين.
    """
    data = load_data()
    if not data:
        return None

    # قراءة بيانات الإيصالات (localRecords في التطبيق)
    # البيانات محفوظة في monthly snapshot كـ arrearState + collectionRows + expenseRows
    snap = data.get("monthly", {}).get(month_data["key"])
    if not snap:
        return None

    # ─── إجمالي العقار (propTotal) ───
    # البيانات الأصلية للمستأجرين محفوظة في التطبيق كـ localRecords
    # في ملف JSON قد تكون محفوظة بطريقة أخرى؛ نحاول استخراجها
    receipts = data.get("receipts", [])  # إن وُجدت
    prop_records = [r for r in receipts if r.get("street") == street]
    prop_total = sum(r.get("total", 0) for r in prop_records)

    # ─── المتأخرات (lateTotal) ───
    arrear_state = snap.get("arrearState", {})
    late_total = 0
    for idx_str, state in arrear_state.items():
        if state.get("late"):
            idx = int(idx_str)
            if 0 <= idx < len(prop_records):
                late_total += prop_records[idx].get("total", 0)

    # ─── التحصيل (collTotal) ───
    mk = safe_street_key([street])
    coll_rows = snap.get("collectionRows", {}).get(mk, [])
    coll_total = sum(float(r.get("amount", 0) or 0) for r in coll_rows)

    # ─── المصروفات (expTotal) ───
    exp_rows = snap.get("expenseRows", {}).get(mk, [])
    exp_total = sum(float(r.get("amount", 0) or 0) for r in exp_rows)

    # ─── الصافي ───
    net_total = prop_total + coll_total - late_total - exp_total

    return {
        "prop_total": prop_total,
        "coll_total": coll_total,
        "late_total": late_total,
        "exp_total":  exp_total,
        "net_total":  net_total,
    }

def get_saved_months(data):
    """استخراج قائمة الشهور المحفوظة من JSON مرتبة تنازلياً"""
    monthly = data.get("monthly", {})
    keys = list(monthly.keys())  # شكل: ["يناير_2025", "فبراير_2025", ...]
    # ترتيب: استخراج السنة ثم الشهر
    def sort_key(k):
        parts = k.split("_")
        year  = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        month = MONTHS_AR.index(parts[0]) if parts[0] in MONTHS_AR else 0
        return (year, month)
    keys.sort(key=sort_key, reverse=True)
    return keys

def get_years_from_data(data):
    """استخراج السنوات المتاحة"""
    months = get_saved_months(data)
    years = []
    seen = set()
    for mk in months:
        parts = mk.split("_")
        if len(parts) > 1:
            y = parts[1]
            if y not in seen:
                seen.add(y)
                years.append(y)
    return years

def get_months_for_year(data, year):
    """استخراج الشهور لسنة معينة"""
    months = get_saved_months(data)
    return [mk for mk in months if mk.endswith("_" + year)]

# ─────────────────────────────────────────────
# إرسال رسالة واتساب
# ─────────────────────────────────────────────
def send_message(to, text):
    if not WA_TOKEN or not PHONE_ID:
        print("NO WA_TOKEN or PHONE_ID set")
        return
    url = f"https://graph.facebook.com/v19.0/{PHONE_ID}/messages"
    payload = json.dumps({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print("WA SEND:", r.status)
    except Exception as e:
        print("WA SEND ERROR:", e)

# ─────────────────────────────────────────────
# منطق المحادثة
# ─────────────────────────────────────────────
def handle_message(phone, text):
    text = text.strip()
    sess = get_session(phone)

    # تفعيل البوت
    if text.lower() == "metochi":
        reset_session(phone)
        sess = get_session(phone)
        sess["step"] = "choose_lang"
        send_message(phone,
            "أهلاً بك في *Metochi* 🏢\n"
            "اختر اللغة / Choose language:\n\n"
            "1️⃣ العربية\n"
            "2️⃣ English"
        )
        return

    # الصمت إن لم تكن الجلسة نشطة
    if sess["step"] == "idle":
        return

    lang = sess.get("lang", "ar")

    # ─── اختيار اللغة ───
    if sess["step"] == "choose_lang":
        if text in ("1", "١", "العربية", "عربي", "arabic"):
            sess["lang"] = "ar"
        elif text in ("2", "٢", "English", "english", "en", "انجليزي"):
            sess["lang"] = "en"
        else:
            send_message(phone, "أرسل 1 للعربية أو 2 for English")
            return
        lang = sess["lang"]
        # اعرض السنوات
        _show_years(phone, sess, lang)
        return

    # ─── اختيار السنة ───
    if sess["step"] == "choose_year":
        years = sess.get("years", [])
        try:
            idx = int(text) - 1
            if idx < 0 or idx >= len(years):
                raise ValueError()
            sess["selected_year"] = years[idx]
            _show_months(phone, sess, lang)
        except (ValueError, IndexError):
            send_message(phone, t("expired", lang))
        return

    # ─── اختيار الشهر ───
    if sess["step"] == "choose_month":
        month_keys = sess.get("month_keys", [])
        try:
            idx = int(text) - 1
            if idx < 0 or idx >= len(month_keys):
                raise ValueError()
            sess["selected_month_key"] = month_keys[idx]
            _show_govs(phone, sess, lang)
        except (ValueError, IndexError):
            send_message(phone, t("expired", lang))
        return

    # ─── اختيار المحافظة ───
    if sess["step"] == "choose_gov":
        gov_keys = sess.get("gov_keys", [])
        try:
            idx = int(text) - 1
            if idx < 0 or idx >= len(gov_keys):
                raise ValueError()
            sess["selected_gov"] = gov_keys[idx]
            _show_props(phone, sess, lang)
        except (ValueError, IndexError):
            send_message(phone, t("expired", lang))
        return

    # ─── اختيار العقار ───
    if sess["step"] == "choose_prop":
        streets = sess.get("streets", [])
        try:
            idx = int(text) - 1
            if idx < 0 or idx >= len(streets):
                raise ValueError()
            sess["selected_street"] = streets[idx]
            _show_result(phone, sess, lang)
        except (ValueError, IndexError):
            send_message(phone, t("expired", lang))
        return

    # fallback
    send_message(phone, t("expired", lang))


# ─── دوال العرض ───

def _show_years(phone, sess, lang):
    data = load_data()
    if not data:
        send_message(phone, t("data_error", lang))
        return
    years = get_years_from_data(data)
    if not years:
        send_message(phone, t("no_data", lang))
        return
    sess["years"] = years
    sess["step"]  = "choose_year"
    lines = [t("choose_year", lang)]
    for i, y in enumerate(years, 1):
        lines.append(f"{i}️⃣ {y}")
    send_message(phone, "\n".join(lines))


def _show_months(phone, sess, lang):
    data = load_data()
    if not data:
        send_message(phone, t("data_error", lang))
        return
    year = sess["selected_year"]
    month_keys = get_months_for_year(data, year)
    if not month_keys:
        send_message(phone, t("no_data", lang))
        return
    sess["month_keys"] = month_keys
    sess["step"]       = "choose_month"
    lines = [t("choose_month", lang)]
    for i, mk in enumerate(month_keys, 1):
        month_ar = mk.split("_")[0]
        label    = month_label(month_ar, lang)
        lines.append(f"{i}️⃣ {label} {year}")
    send_message(phone, "\n".join(lines))


def _show_govs(phone, sess, lang):
    gov_keys = list(GOV_MAP.keys())
    sess["gov_keys"] = gov_keys
    sess["step"]     = "choose_gov"
    lines = [t("choose_gov", lang)]
    for i, gk in enumerate(gov_keys, 1):
        label = GOV_MAP[gk].get(lang, GOV_MAP[gk]["ar"])
        lines.append(f"{i}️⃣ {label}")
    send_message(phone, "\n".join(lines))


def _show_props(phone, sess, lang):
    gov_key = sess["selected_gov"]
    streets = GOV_MAP[gov_key]["streets"]
    sess["streets"] = streets
    sess["step"]    = "choose_prop"
    lines = [t("choose_prop", lang)]
    for i, s in enumerate(streets, 1):
        lines.append(f"{i}️⃣ {short_street(s)}")
    send_message(phone, "\n".join(lines))


def _show_result(phone, sess, lang):
    street    = sess["selected_street"]
    month_key = sess["selected_month_key"]
    year      = sess["selected_year"]
    month_ar  = month_key.split("_")[0]
    month_lbl = month_label(month_ar, lang)

    totals = calc_totals({"key": month_key}, street)

    lines = [
        t("report_header", lang),
        f"{t('lbl_prop', lang)}: {short_street(street)}",
        f"{t('lbl_period', lang)}: {month_lbl} {year}",
        "――――――――――――",
        f"{t('lbl_total', lang)}: {fmt(totals['prop_total'])} {t('currency', lang)}" if totals else "",
        f"{t('lbl_collected', lang)}: {fmt(totals['coll_total'])} {t('currency', lang)}" if totals else "",
        f"{t('lbl_late', lang)}: {fmt(totals['late_total'])} {t('currency', lang)}" if totals else "",
        f"{t('lbl_expenses', lang)}: {fmt(totals['exp_total'])} {t('currency', lang)}" if totals else "",
        "――――――――――――",
        f"{t('lbl_net', lang)}: {fmt(totals['net_total'])} {t('currency', lang)}" if totals else t("no_data", lang),
        "",
        f"↩️ {t('back_menu', lang)}: أرسل *Metochi*" if lang == "ar" else f"↩️ {t('back_menu', lang)}: Send *Metochi*",
    ]

    if not totals:
        send_message(phone, t("no_data", lang))
    else:
        send_message(phone, "\n".join(l for l in lines if l is not None))

    reset_session(phone)


# ─────────────────────────────────────────────
# Vercel Handler
# ─────────────────────────────────────────────
class handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # تعطيل سجلات HTTP الافتراضية

    # ─── GET: التحقق من Webhook ───
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        if params.get("hub.verify_token") == VERIFY_TOKEN:
            challenge = params.get("hub.challenge", "")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(challenge.encode())
        else:
            self.send_response(403)
            self.end_headers()

    # ─── POST: استقبال الرسائل ───
    def do_POST(self):
        length  = int(self.headers.get("Content-Length", 0))
        body    = self.rfile.read(length)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

        try:
            payload = json.loads(body)
            entry   = payload["entry"][0]
            changes = entry["changes"][0]["value"]
            messages = changes.get("messages", [])

            for msg in messages:
                phone = msg["from"]
                mtype = msg.get("type", "")

                # تحقق من الـ Whitelist
                if WHITELIST and phone not in WHITELIST and "" not in WHITELIST:
                    print(f"BLOCKED: {phone}")
                    continue

                if mtype == "text":
                    text = msg["text"]["body"]
                    handle_message(phone, text)

        except Exception as e:
            print("WEBHOOK ERROR:", e)
