"""posts: content library, automatic posts and the links each post carries (M3.2-M3.4)

* ``content_items``: short evergreen Persian texts for the top of a post (title + Telegram HTML
  body). 36 approved items are seeded here (Telegram tips, phone, privacy and security know-how;
  no politics, no dated news, no medical or financial claims). Nothing is ever removed from this
  table; the owner retires an item instead.
* ``posts``: one row per automatic post (its outbox job, the content as posted, the plan slot).
  Posts are never deleted (CLAUDE.md rule 6).
* ``post_links``: which links a post carries and their status in that post. No link URL is stored
  (it holds a proxy secret or a VLESS id; rule 3).

Revision ID: 0014_posts
Revises: 0010_domains
Create Date: 2026-11-04 21:00:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0014_posts"
down_revision = "0010_domains"
branch_labels = None
depends_on = None


# (title, body, tags). Bodies use the Telegram HTML subset the posts team allows in a body
# (proxyhub/telegram/templates.py BODY_TAGS). Menu names are given in Persian and, in brackets,
# as they appear in Telegram's English interface.
SEED: list[tuple[str, str, list[str]]] = [
    (
        "🧦 پروکسی تلگرام را چطور اضافه کنم؟",
        "ساده‌ترین راه: روی یکی از لینک‌های پروکسی همین کانال بزنید. تلگرام صفحه‌ی «اتصال به پروکسی» "
        "(Connect Proxy) را باز میکند؛ <b>اتصال</b> را بزنید.\n"
        "فهرست پروکسی‌هایی که اضافه کرده‌اید در <b>تنظیمات ← داده و ذخیره‌سازی ← پروکسی</b> هست و "
        "از همان‌جا میتوانید پروکسی را خاموش یا عوض کنید.",
        ["پروکسی", "تلگرام"],
    ),
    (
        "🔐 تأیید دومرحله‌ای تلگرام را روشن کنید",
        "با این قابلیت، ورود به حساب شما در دستگاه تازه علاوه بر کد ورود، <b>یک رمز</b> هم میخواهد؛ "
        "پس اگر کسی کد را به دست بیاورد، باز هم نمیتواند وارد شود.\n"
        "مسیر: <b>تنظیمات ← حریم خصوصی و امنیت ← تأیید دومرحله‌ای</b> (Two-Step Verification). "
        "یک ایمیل بازیابی هم بگذارید تا اگر رمز را فراموش کردید، راه برگشت داشته باشید.",
        ["تلگرام", "امنیت"],
    ),
    (
        "🗂 پوشه‌های گفتگو برای نظم دادن به چت‌ها",
        "اگر فهرست چت‌هایتان شلوغ است، از <b>تنظیمات ← پوشه‌های گفتگو</b> (Chat Folders) پوشه بسازید؛ "
        "مثلاً «کار»، «خانواده» و «کانال‌ها».\n"
        "هر پوشه یک زبانه‌ی جدا بالای فهرست چت‌ها میشود و فقط چت‌هایی را نشان میدهد که خودتان "
        "انتخاب کرده‌اید.",
        ["تلگرام", "ترفند"],
    ),
    (
        "🚫 کد ورود تلگرام را به هیچ‌کس ندهید",
        "تلگرام <b>هرگز</b> در چت از شما کد ورود نمیخواهد. پیام‌هایی مثل «حسابتان مسدود میشود، کد را "
        "بفرستید» یا «پرمیوم رایگان بگیرید» کلاهبرداری‌اند.\n"
        "کد فقط برای ورود خودتان است؛ حتی اگر فرستنده اسم یا عکس آشنایی داشت، آن را برای کسی نفرستید.",
        ["تلگرام", "امنیت"],
    ),
    (
        "🔁 چند پروکسی نگه دارید، نه فقط یکی",
        "تلگرام همه‌ی پروکسی‌هایی را که اضافه کرده‌اید در <b>تنظیمات ← داده و ذخیره‌سازی ← پروکسی</b> "
        "نگه میدارد و کنار هر کدام وضعیت اتصالش را نشان میدهد.\n"
        "اگر یکی کند یا قطع شد، کافی است روی دیگری بزنید. پس چند پروکسی از هر پست را با هم اضافه کنید.",
        ["پروکسی", "تلگرام"],
    ),
    (
        "💾 پیام‌های ذخیره‌شده؛ دفترچه‌ی شخصی شما",
        "«پیام‌های ذخیره‌شده» (Saved Messages) یک چت با خودتان است. متن، لینک، عکس و فایل را آنجا "
        "بفرستید تا روی همه‌ی دستگاه‌هایتان در دسترس باشد.\n"
        "پیامی را که نمیخواهید گم شود با <b>فوروارد</b> به همین چت بفرستید.",
        ["تلگرام", "ترفند"],
    ),
    (
        "📱 دستگاه‌های ناشناس را از حسابتان بیرون کنید",
        "در <b>تنظیمات ← دستگاه‌ها</b> (Devices) همه‌ی جاهایی که با حساب شما وارد شده‌اند دیده میشود: "
        "گوشی، کامپیوتر و مرورگر.\n"
        "هر نشستی را که نمیشناسید ببندید. گزینه‌ی «بستن همه‌ی نشست‌های دیگر» (Terminate All Other "
        "Sessions) همه را جز همین گوشی خارج میکند.",
        ["تلگرام", "امنیت"],
    ),
    (
        "🧭 پروکسی تلگرام با VPN چه فرقی دارد؟",
        "پروکسی MTProto <b>فقط ترافیک خود تلگرام</b> را رد میکند؛ بقیه‌ی برنامه‌ها و مرورگر مثل قبل "
        "از اینترنت معمولی استفاده میکنند. پس روی سرعت بقیه‌ی برنامه‌ها اثری ندارد و لازم نیست مدام "
        "روشن و خاموشش کنید.\n"
        "برای بقیه‌ی برنامه‌ها به یک کانفیگ مثل ریلیتی نیاز دارید.",
        ["پروکسی"],
    ),
    (
        "⏰ پیام را برای بعد زمان‌بندی کنید",
        "دکمه‌ی ارسال را <b>نگه دارید</b> و «زمان‌بندی پیام» (Schedule Message) را بزنید. پیام در "
        "ساعتی که انتخاب میکنید خودش فرستاده میشود، حتی اگر آن موقع گوشی‌تان خاموش باشد.\n"
        "در «پیام‌های ذخیره‌شده» همین کار یک یادآور برای خودتان میسازد.",
        ["تلگرام", "ترفند"],
    ),
    (
        "📲 برنامه‌ها را فقط از منبع رسمی نصب کنید",
        "نسخه‌های دستکاری‌شده‌ی تلگرام و برنامه‌های «طلایی» یا «پیشرفته» ممکن است پیام‌ها، کد ورود "
        "یا اطلاعات گوشی را جای دیگری بفرستند.\n"
        "تلگرام را از <b>Google Play</b>، <b>App Store</b> یا سایت رسمی <b>telegram.org</b> بگیرید و "
        "برنامه‌های اتصال را هم از صفحه‌ی رسمی سازنده‌شان.",
        ["امنیت", "گوشی"],
    ),
    (
        "📋 کانفیگ ریلیتی را در کدام برنامه وارد کنم؟",
        "کانفیگ‌های VLESS Reality در برنامه‌هایی مثل <b>v2rayNG</b> و <b>Hiddify</b> (اندروید)، "
        "<b>Streisand</b> یا <b>Hiddify</b> (آیفون) و <b>v2rayN</b> یا <b>Hiddify</b> (ویندوز) کار "
        "میکنند.\n"
        "روی متن کانفیگ بزنید تا کپی شود، بعد در برنامه گزینه‌ی وارد کردن از کلیپ‌بورد (Import from "
        "Clipboard) را بزنید و وصل شوید.",
        ["ریلیتی", "ترفند"],
    ),
    (
        "🔕 پیام بی‌صدا بفرستید",
        "اگر دیروقت است و نمیخواهید گوشی طرف مقابل زنگ بزند، دکمه‌ی ارسال را نگه دارید و «ارسال "
        "بی‌صدا» (Send Without Sound) را بزنید.\n"
        "پیام میرسد، ولی اعلانش بی‌صداست.",
        ["تلگرام", "ترفند"],
    ),
    (
        "☎️ شماره‌تان را از غریبه‌ها پنهان کنید",
        "در <b>تنظیمات ← حریم خصوصی و امنیت ← شماره‌ی تلفن</b> (Phone Number)، «چه کسی شماره‌ام را "
        "ببیند» را روی <b>هیچ‌کس</b> (Nobody) بگذارید.\n"
        "در همان صفحه میتوانید تعیین کنید فقط مخاطبانتان بتوانند شما را با شماره پیدا کنند.",
        ["تلگرام", "حریم‌خصوصی"],
    ),
    (
        "🔑 رمز قوی چه شکلی است؟",
        "رمزی که از چند کلمه‌ی بی‌ربط کنار هم ساخته شده باشد، هم به خاطر میماند و هم حدس زدنش سخت است.\n"
        "برای هر حساب <b>رمز جدا</b> بگذارید تا لو رفتن یکی، بقیه را به خطر نیندازد. یک برنامه‌ی "
        "مدیریت رمز (Password Manager) نگه داشتن همه را آسان میکند.",
        ["امنیت"],
    ),
    (
        "🛡 چت مخفی چه فرقی با چت عادی دارد؟",
        "چت‌های عادی تلگرام روی سرورهای تلگرام نگه داشته میشوند تا روی همه‌ی دستگاه‌هایتان باشند. "
        "<b>چت مخفی</b> (Secret Chat) رمزنگاری سرتاسری دارد، فقط روی همان دو دستگاه است و میشود برایش "
        "زمان پاک شدن خودکار گذاشت.\n"
        "برای شروع، در صفحه‌ی مخاطب از منوی بیشتر «شروع چت مخفی» (Start Secret Chat) را بزنید.",
        ["تلگرام", "حریم‌خصوصی"],
    ),
    (
        "🧹 حافظه‌ی گوشی را از فایل‌های تلگرام خالی کنید",
        "<b>تنظیمات ← داده و ذخیره‌سازی ← مصرف حافظه</b> (Storage Usage). با «پاک کردن حافظه‌ی موقت» "
        "(Clear Cache) فایل‌های دانلودشده از روی گوشی برداشته میشوند، ولی در خود تلگرام میمانند و هر "
        "وقت لازم شد دوباره دانلود میشوند.\n"
        "با «نگه داشتن رسانه» (Keep Media) میتوانید این کار را خودکار کنید.",
        ["تلگرام", "گوشی"],
    ),
    (
        "🔄 به‌روزرسانی‌ها را عقب نیندازید",
        "بیشتر به‌روزرسانی‌های اندروید، iOS و برنامه‌ها حفره‌های امنیتی را میبندند.\n"
        "به‌روزرسانی خودکار برنامه‌ها را روشن بگذارید و وقتی گوشی به‌روزرسانی سیستم پیشنهاد داد، "
        "روی وای‌فای و با باتری کافی نصبش کنید.",
        ["امنیت", "گوشی"],
    ),
    (
        "📶 مصرف اینترنت تلگرام را کم کنید",
        "<b>تنظیمات ← داده و ذخیره‌سازی ← دانلود خودکار رسانه</b> (Auto-Download Media). دانلود خودکار "
        "ویدیو و فایل را روی «داده‌ی همراه» خاموش کنید تا فقط چیزی دانلود شود که خودتان رویش میزنید.\n"
        "برای وای‌فای میتوانید روشنش بگذارید.",
        ["تلگرام", "ترفند"],
    ),
    (
        "🔒 قفل رمزدار برای خود تلگرام",
        "اگر گوشی‌تان دست دیگران هم میافتد، از <b>تنظیمات ← حریم خصوصی و امنیت ← قفل رمز</b> "
        "(Passcode Lock) یک رمز بگذارید.\n"
        "تلگرام بعد از مدتی که خودتان تعیین میکنید رمز میخواهد. باز کردن با اثر انگشت یا Face ID را "
        "هم میتوانید روشن کنید.",
        ["تلگرام", "امنیت"],
    ),
    (
        "🎣 لینک‌های فریبنده را از کجا بشناسیم؟",
        "پیش از زدن روی لینک، نشانی را با دقت بخوانید: یک حرف جابه‌جا، دامنه‌ی عجیب یا نشانی کوتاه‌شده "
        "نشانه‌ی خطر است.\n"
        "پیامی که مدام بر «همین حالا» و «فقط امروز» تأکید میکند یا جایزه‌ی بی‌دلیل وعده میدهد را جدی "
        "نگیرید. رمز را فقط در برنامه یا سایتی وارد کنید که <b>خودتان</b> باز کرده‌اید.",
        ["امنیت"],
    ),
    (
        "👀 چه کسی آخرین بازدید شما را ببیند؟",
        "از <b>تنظیمات ← حریم خصوصی و امنیت ← آخرین بازدید و آنلاین</b> (Last Seen &amp; Online) "
        "تعیین کنید چه کسانی زمان آنلاین بودنتان را ببینند. استثنا هم میشود گذاشت؛ مثلاً «هیچ‌کس» به‌جز "
        "چند نفر.\n"
        "اگر آن را پنهان کنید، آخرین بازدید دقیق دیگران را هم نمیبینید.",
        ["تلگرام", "حریم‌خصوصی"],
    ),
    (
        "📧 ایمیل‌تان را هم دومرحله‌ای کنید",
        "ایمیل کلید بازیابی بیشتر حساب‌های شماست؛ اگر کسی به آن برسد، میتواند رمز بقیه را هم عوض کند.\n"
        "در تنظیمات امنیتی حساب گوگل یا هر ایمیل دیگری، <b>تأیید دومرحله‌ای</b> را روشن کنید و "
        "کدهای پشتیبان را جای امنی نگه دارید.",
        ["امنیت"],
    ),
    (
        "🌐 ترجمه‌ی پیام‌ها داخل خود تلگرام",
        "در <b>تنظیمات ← زبان</b> (Language)، گزینه‌ی «نمایش دکمه‌ی ترجمه» (Show Translate Button) را "
        "روشن کنید.\n"
        "از آن به بعد روی هر پیام به زبان دیگر بزنید و «ترجمه» را انتخاب کنید تا همان‌جا به فارسی "
        "برگردانده شود.",
        ["تلگرام", "ترفند"],
    ),
    (
        "☕️ وای‌فای عمومی؛ راحت ولی با احتیاط",
        "در وای‌فای کافه، فرودگاه یا هتل، هر کسی ممکن است در همان شبکه باشد.\n"
        "سایت‌هایی را باز کنید که نشانی‌شان با <b>https</b> شروع میشود، کارهای حساس را به اینترنت "
        "همراه بسپارید و اتصال خودکار گوشی به شبکه‌های باز را خاموش کنید.",
        ["امنیت", "گوشی"],
    ),
    (
        "🔇 کانال را بی‌صدا کنید، نه ترک",
        "اگر اعلان‌های زیاد اذیتتان میکند، لازم نیست کانال را ترک کنید. روی نام کانال بزنید و "
        "<b>بی‌صدا</b> (Mute) را انتخاب کنید.\n"
        "پست‌ها مثل قبل میآیند ولی گوشی زنگ نمیزند و هر وقت خواستید سر میزنید.",
        ["تلگرام", "ترفند"],
    ),
    (
        "🔎 گوشی گم شد؟ از قبل آماده باشید",
        "در اندروید قابلیت <b>Find My Device</b> و در آیفون <b>Find My</b> را از همین حالا روشن کنید.\n"
        "اگر گوشی گم شد، از یک دستگاه دیگر میتوانید جایش را ببینید، صدایش را دربیاورید، قفلش کنید یا "
        "اطلاعاتش را از راه دور پاک کنید.",
        ["گوشی", "امنیت"],
    ),
    (
        "📌 چت‌های مهم را بالای فهرست سنجاق کنید",
        "روی یک چت انگشت را نگه دارید و «سنجاق» (Pin) را بزنید تا همیشه بالای فهرست بماند.\n"
        "کانال‌ها و گروه‌هایی را که هر روز سر میزنید سنجاق کنید تا بین چت‌های دیگر گم نشوند؛ مثلاً "
        "همین کانال.",
        ["تلگرام", "ترفند"],
    ),
    (
        "📵 قفل صفحه فقط برای پنهان‌کاری نیست",
        "در گوشی‌های امروزی، وقتی قفل صفحه (رمز، پین یا الگو) دارید، اطلاعات داخل گوشی "
        "<b>رمزنگاری‌شده</b> نگه داشته میشود. بی قفل، هر کسی گوشی را بردارد به همه‌چیز دسترسی دارد.\n"
        "یک پین شش‌رقمی یا رمز، از الگوی ساده امن‌تر است.",
        ["گوشی", "امنیت"],
    ),
    (
        "🗃 بایگانی برای خلوت کردن فهرست چت‌ها",
        "چت‌هایی را که کمتر سر میزنید به <b>بایگانی</b> (Archive) ببرید: چت را به سمت چپ بکشید و "
        "بایگانی را بزنید.\n"
        "چت‌های بی‌صدای بایگانی‌شده با آمدن پیام تازه از بایگانی بیرون نمیآیند.",
        ["تلگرام", "ترفند"],
    ),
    (
        "🖼 از عکس‌ها و فایل‌های مهم نسخه‌ی پشتیبان بگیرید",
        "گوشی ممکن است گم یا خراب شود. یک قاعده‌ی ساده: از هر چیز مهم <b>سه نسخه</b> داشته باشید، روی "
        "<b>دو جای مختلف</b>، که <b>یکی بیرون از خانه</b> باشد (مثلاً یک فضای ابری).\n"
        "Google Photos، iCloud یا یک هارد بیرونی برای شروع کافی است.",
        ["گوشی"],
    ),
    (
        "⏱ پاک شدن خودکار پیام‌ها در چت‌های عادی",
        "برای چت‌هایی که نمیخواهید تاریخچه‌شان بماند، در صفحه‌ی همان چت از منوی بیشتر، «پاک شدن "
        "خودکار» (Auto-Delete) را روی یک روز، یک هفته یا یک ماه بگذارید.\n"
        "پیام‌های تازه پس از این مدت برای هر دو طرف پاک میشوند.",
        ["تلگرام", "حریم‌خصوصی"],
    ),
    (
        "🧾 عکس مدارک و رمزها را در گالری نگه ندارید",
        "عکس کارت بانکی، مدارک شناسایی یا رمزها اگر در گالری بماند، با همگام‌سازی ابری همه‌جا میرود و "
        "هر کسی که گالری را ورق بزند آن را میبیند.\n"
        "این‌ها را در یک پوشه‌ی رمزدار یا برنامه‌ی مدیریت رمز نگه دارید.",
        ["حریم‌خصوصی", "گوشی"],
    ),
    (
        "🔋 گوشی را در گرما شارژ نکنید",
        "گرمای زیاد بیشترین آسیب را به باتری گوشی میزند. گوشی را زیر آفتاب، داخل ماشین داغ یا زیر "
        "بالش شارژ نکنید.\n"
        "گزینه‌هایی مثل «شارژ بهینه» (Optimized یا Adaptive Charging) که در بیشتر گوشی‌ها هست، هم به "
        "عمر باتری کمک میکنند.",
        ["گوشی"],
    ),
    (
        "📷 پیش از باز کردن QR کد، نشانی را ببینید",
        "QR کد فقط یک لینک است. دوربین گوشی پیش از باز کردن، نشانی را نشان میدهد؛ آن را بخوانید و "
        "اگر ناآشنا یا عجیب بود بازش نکنید.\n"
        "به QR کدهایی که روی برچسب‌های چسبانده‌شده در خیابان یا پارکینگ میبینید بیشتر شک کنید.",
        ["امنیت"],
    ),
    (
        "🔗 نام کاربری؛ پیدا شدن بدون شماره",
        "با ساختن <b>نام کاربری</b> (Username) در تنظیمات، دیگران میتوانند با لینک t.me و نام کاربری "
        "شما پیام بدهند، بی آنکه شماره‌تان را بدانند.\n"
        "اگر نمیخواهید کسی پیدایتان کند، نام کاربری را خالی بگذارید.",
        ["تلگرام", "حریم‌خصوصی"],
    ),
    (
        "🌙 حالت شب تلگرام",
        "در <b>تنظیمات ← ظاهر</b> (Appearance) یا تنظیمات گفتگو (Chat Settings)، حالت تیره را روشن "
        "کنید یا «حالت شب خودکار» (Auto-Night Mode) را بگذارید تا بعد از غروب خودش تیره شود.\n"
        "در تاریکی خواناتر است و روی صفحه‌های OLED کمی هم باتری کمتری مصرف میکند.",
        ["تلگرام", "ترفند"],
    ),
]


def upgrade() -> None:
    content_items = op.create_table(
        "content_items",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("tags", postgresql.ARRAY(sa.String(length=32)), server_default="{}", nullable=False),
        sa.Column("status", sa.String(length=10), server_default="draft", nullable=False),
        sa.Column("origin", sa.String(length=10), server_default="owner", nullable=False),
        sa.Column("used_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("length(body) BETWEEN 1 AND 3000", name=op.f("ck_content_items_body_length")),
        sa.CheckConstraint("length(title) BETWEEN 1 AND 120", name=op.f("ck_content_items_title_length")),
        sa.CheckConstraint("origin IN ('seed', 'owner')", name=op.f("ck_content_items_origin")),
        sa.CheckConstraint(
            "status IN ('draft', 'approved', 'used', 'retired')", name=op.f("ck_content_items_status")
        ),
        sa.CheckConstraint("used_count >= 0", name=op.f("ck_content_items_used_count")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_content_items")),
    )
    op.create_index("ix_content_items_status_id", "content_items", ["status", "id"], unique=False)
    op.create_table(
        "posts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("role", sa.String(length=10), nullable=False),
        sa.Column("outbox_id", sa.BigInteger(), nullable=True),
        sa.Column("content_item_id", sa.BigInteger(), nullable=True),
        sa.Column("content_html", sa.Text(), server_default="", nullable=False),
        sa.Column("channel_tag", sa.String(length=40), nullable=False),
        sa.Column("poster", sa.String(length=20), server_default="bot", nullable=False),
        sa.Column("plan_day", sa.Date(), nullable=False),
        sa.Column("plan_slot", sa.Integer(), nullable=False),
        sa.Column("planned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_edit_outbox_id", sa.BigInteger(), nullable=True),
        sa.Column("last_edit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("edits", sa.Integer(), server_default="0", nullable=False),
        sa.Column("edit_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("edit_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("edits >= 0 AND edit_failures >= 0", name=op.f("ck_posts_edit_counts")),
        sa.CheckConstraint("plan_slot >= 0", name=op.f("ck_posts_plan_slot")),
        sa.CheckConstraint("role IN ('main', 'test')", name=op.f("ck_posts_role")),
        sa.ForeignKeyConstraint(
            ["content_item_id"],
            ["content_items.id"],
            name=op.f("fk_posts_content_item_id_content_items"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["last_edit_outbox_id"],
            ["outbox.id"],
            name=op.f("fk_posts_last_edit_outbox_id_outbox"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["outbox_id"], ["outbox.id"], name=op.f("fk_posts_outbox_id_outbox"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_posts")),
        sa.UniqueConstraint("outbox_id", name="uq_posts_outbox_id"),
        sa.UniqueConstraint("role", "plan_day", "plan_slot", name="uq_posts_role_plan"),
    )
    op.create_index("ix_posts_created", "posts", ["created_at"], unique=False)
    op.create_table(
        "post_links",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("post_id", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("slot", sa.Integer(), nullable=True),
        sa.Column("label", sa.String(length=40), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("status_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('mtproto', 'reality')", name=op.f("ck_post_links_kind")),
        sa.CheckConstraint("position >= 0", name=op.f("ck_post_links_position")),
        sa.CheckConstraint(
            "status IN ('active', 'expired', 'full', 'burned')", name=op.f("ck_post_links_status")
        ),
        sa.ForeignKeyConstraint(
            ["post_id"], ["posts.id"], name=op.f("fk_post_links_post_id_posts"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_post_links")),
        sa.UniqueConstraint("post_id", "key", name="uq_post_links_post_key"),
        sa.UniqueConstraint("post_id", "position", name="uq_post_links_post_position"),
    )
    op.bulk_insert(
        content_items,
        [
            {"title": title, "body": body, "tags": tags, "status": "approved", "origin": "seed"}
            for title, body, tags in SEED
        ],
    )


def downgrade() -> None:
    op.drop_table("post_links")
    op.drop_index("ix_posts_created", table_name="posts")
    op.drop_table("posts")
    op.drop_index("ix_content_items_status_id", table_name="content_items")
    op.drop_table("content_items")
