"""Persian (Farsi) interface texts, keyed by the English text used in the code.

Keep every {placeholder} of the English key. Numbers, sizes and HTML tags stay as they are.
"""

TEXTS: dict[str, str] = {
    # --- welcome, help and language
    "👋 Hello!": "👋 سلام!",
    "📥 I can help you download videos, photos and music from:": (
        "📥 می‌تونم ویدیو، عکس و موسیقی رو از این سرویس‌ها برات دانلود کنم:"
    ),
    "• To download, just send me a link to the video, photo or track.": (
        "• برای دانلود، کافیه لینک ویدیو، عکس یا آهنگ رو برام بفرستی."
    ),
    "<i>(I also work in groups. To add me to a group, press the button below 👇)</i>": (
        "<i>(توی گروه‌ها هم کار می‌کنم. برای اضافه کردن من به گروه، دکمه‌ی زیر رو بزن 👇)</i>"
    ),
    "🌐 To change the language, send /language": "🌐 برای تغییر زبان، /language رو بفرست",
    "Current language: {language}": "زبان فعلی: {language}",
    "✅ Language changed to English.": "✅ زبان ربات به فارسی تغییر کرد.",
    "🔗 Send me a link to a video, photo or track.": "🔗 لینک یک ویدیو، عکس یا آهنگ رو برام بفرست.",
    "Start and see what I can download": "شروع و دیدن سرویس‌های قابل دانلود",
    "Change the bot's language 🌐": "تغییر زبان ربات 🌐",
    "How to use the bot": "راهنمای استفاده از ربات",
    "Add to a group ⤴️": "افزودن به گروه ⤴️",
    "📖 Usage guide": "📖 راهنمای استفاده",
    "• Playlists, albums and several links in one message work too.": "• پلی‌لیست، آلبوم و چند لینک توی یک پیام هم پشتیبانی می‌شه.",
    "• For the full guide, send /help.": "• برای راهنمای کامل، /help رو بفرست.",
    "📖 <b>How to use the bot</b>": "📖 <b>راهنمای استفاده از ربات</b>",
    "<b>🔗 One video, photo or track</b>": "<b>🔗 یک ویدیو، عکس یا آهنگ</b>",
    "1. Send me a link (or share it to me from the app).": "۱. لینک رو برام بفرست (یا از داخل برنامه با من به اشتراک بذار).",
    "2. Pick a quality or format from the buttons.": "۲. از دکمه‌ها کیفیت یا فرمت رو انتخاب کن.",
    "3. Follow the progress; press ✖️ Cancel download if you change your mind.": "۳. پیشرفت دانلود رو ببین؛ اگه پشیمون شدی، ✖️ لغو دانلود رو بزن.",
    "Posts with several photos or videos are sent together as an album.": "پست‌هایی که چند عکس یا ویدیو دارن، با هم به صورت آلبوم فرستاده می‌شن.",
    "• YouTube playlists and YouTube Music albums": "• پلی‌لیست‌های یوتیوب و آلبوم‌های یوتیوب موزیک",
    "• Spotify albums and playlists": "• آلبوم‌ها و پلی‌لیست‌های اسپاتیفای",
    "• SoundCloud sets": "• مجموعه‌های (Set) ساندکلود",
    "<b>📃 Playlists and albums</b>": "<b>📃 پلی‌لیست و آلبوم</b>",
    "Send a link to:": "لینک یکی از این‌ها رو بفرست:",
    "I'll show you the list first. Then you can:": "اول فهرستش رو نشونت می‌دم. بعد می‌تونی:",
    "• pick one quality for everything, or 🎵 MP3 for audio only;": "• یک کیفیت برای همه انتخاب کنی، یا 🎵 MP3 برای فقط صدا؛",
    "• press ☑️ Choose items to tick only the ones you want;": "• با ☑️ انتخاب موارد، فقط اون‌هایی رو که می‌خوای تیک بزنی؛",
    "• press 📥 Download. Items arrive one by one, with a card showing the overall progress;": "• 📥 دانلود رو بزنی. موارد یکی‌یکی می‌رسن و یک کارت پیشرفت کلی رو نشون می‌ده؛",
    "• press ⏹ Stop after this item whenever you've had enough.": "• هر وقت خواستی، ⏹ توقف بعد از این مورد رو بزنی.",
    "Up to {n} items per list.": "حداکثر {n} مورد از هر فهرست.",
    "<b>🧺 Several links at once</b>": "<b>🧺 چند لینک با هم</b>",
    "Put up to {n} links in one message. I'll show a summary of everything you asked for, and you confirm once.": "تا {n} لینک رو توی یک پیام بفرست. خلاصه‌ی همه‌ی درخواست‌هات رو نشونت می‌دم و فقط یک بار تأیید می‌کنی.",
    "<b>🎵 Music</b>": "<b>🎵 موسیقی</b>",
    "• Under a downloaded video, press 🎵 Download music from video to get its audio as MP3.": "• زیر ویدیوی دانلودشده، دکمه‌ی 🎵 دانلود موسیقی این ویدیو رو بزن تا صداش رو به صورت MP3 بگیری.",
    "• For Spotify links, I find the song and send it to you as MP3.": "• برای لینک‌های اسپاتیفای، آهنگ رو پیدا می‌کنم و به صورت MP3 برات می‌فرستم.",
    "<b>👥 In groups</b>": "<b>👥 توی گروه‌ها</b>",
    "Add me to a group and send links there as usual. Only the person who sent a list can use its buttons.": "من رو به گروه اضافه کن و مثل همیشه لینک بفرست. فقط کسی که فهرست رو فرستاده می‌تونه از دکمه‌هاش استفاده کنه.",
    "<b>💡 Good to know</b>": "<b>💡 خوبه بدونی</b>",
    "• Files up to {size} can be sent.": "• فایل‌ها تا حجم {size} قابل ارسال هستن.",
    "• Anything downloaded before is sent instantly.": "• هر چیزی که قبلاً دانلود شده، فوری فرستاده می‌شه.",
    "• You can run one download at a time; a list waits its turn on its own.": "• هر بار یک دانلود برات انجام می‌شه؛ فهرست‌ها خودشون منتظر نوبت می‌مونن.",
    "• If I say delivery couldn't be confirmed, check the chat before asking again.": "• اگه گفتم ارسال تأیید نشد، قبل از درخواست دوباره چت رو چک کن.",
    "<b>⌨️ Commands</b>": "<b>⌨️ دستورها</b>",
    "/start: welcome and supported sites": "/start: خوش‌آمد و سایت‌های پشتیبانی‌شده",
    "/help: this guide": "/help: همین راهنما",
    "/language: change the language": "/language: تغییر زبان",
    # --- link lookup and the format card
    "🔗 Link received. Waiting to fetch media details…": "🔗 لینک دریافت شد. در انتظار دریافت اطلاعات رسانه…",
    "🔎 Checking your {platform} link…\nFetching media details and available formats.": (
        "🔎 در حال بررسی لینک {platform}…\nدریافت اطلاعات رسانه و کیفیت‌های موجود."
    ),
    "⏱ {elapsed}s elapsed. Some sites take longer to respond.": (
        "⏱ {elapsed} ثانیه گذشته. بعضی سایت‌ها دیرتر پاسخ می‌دن."
    ),
    "Formats for download ⤵️": "کیفیت‌های قابل دانلود ⤵️",
    "🖼 {n} photo": "🖼 {n} عکس",
    "🖼 {n} photos": "🖼 {n} عکس",
    "🎬 {n} video": "🎬 {n} ویدیو",
    "🎬 {n} videos": "🎬 {n} ویدیو",
    "🎵 {n} track": "🎵 {n} آهنگ",
    "🎵 {n} tracks": "🎵 {n} آهنگ",
    "{n} files": "{n} فایل",
    "📥 Download ({n})": "📥 دانلود ({n})",
    "🎬 Original": "🎬 صدای اصلی",
    "🎵 Download music from video": "🎵 دانلود موسیقی این ویدیو",
    "Downloaded by @{bot} 🤖": "دانلود شده توسط @{bot} 🤖",
    # --- download status
    "🕓 <b>Queued</b>\nWaiting for a download slot…": "🕓 <b>در صف</b>\nدر انتظار نوبت دانلود…",
    "⏳ <b>Preparing download</b>\nWaiting for the first bytes…": (
        "⏳ <b>آماده‌سازی دانلود</b>\nدر انتظار دریافت اولین داده‌ها…"
    ),
    "⬇️ <b>Downloading</b>": "⬇️ <b>در حال دانلود</b>",
    "⬇️ <b>Downloading</b>\n<b>{n}</b> files downloaded\n<i>Total count not provided by the source.</i>": (
        "⬇️ <b>در حال دانلود</b>\n<b>{n}</b> فایل دانلود شد\n<i>تعداد کل فایل‌ها توسط منبع اعلام نشده.</i>"
    ),
    "video track": "ترک ویدیو",
    "audio track": "ترک صدا",
    "📦 <b>{size}</b> received": "📦 <b>{size}</b> دریافت شد",
    "<i>Total size not provided by the source.</i>": "<i>حجم کل توسط منبع اعلام نشده.</i>",
    "🗂 <b>{done}</b> / {total} files": "🗂 <b>{done}</b> از {total} فایل",
    "📥 Fetching file {n} of {total}": "📥 در حال دریافت فایل {n} از {total}",
    "🧩 Part <b>{part}</b> of {parts}": "🧩 بخش <b>{part}</b> از {parts}",
    "🔗 {n} parallel connections": "🔗 {n} اتصال هم‌زمان",
    "🚀 measuring speed…": "🚀 در حال محاسبه‌ی سرعت…",
    "⏳ {time} left": "⏳ {time} مانده",
    "⏱ Elapsed {time}": "⏱ زمان سپری‌شده: {time}",
    "⚙️ Step time {time}": "⚙️ زمان این مرحله: {time}",
    "Queue": "صف",
    "Download": "دانلود",
    "Process": "پردازش",
    "Upload": "ارسال",
    "Merging video and audio": "ادغام ویدیو و صدا",
    "Combining both tracks into one MP4 file…": "در حال ترکیب هر دو ترک در یک فایل MP4…",
    "Converting to MP3": "تبدیل به MP3",
    "Extracting the audio track at 192 kbps…": "در حال استخراج صدا با کیفیت 192 kbps…",
    "Adding tags and cover art": "افزودن مشخصات و کاور",
    "Almost done…": "تقریباً تمام شد…",
    "📤 <b>Uploading to Telegram</b>\nDownload complete. Sending your file…": (
        "📤 <b>در حال ارسال به تلگرام</b>\nدانلود کامل شد. در حال ارسال فایل…"
    ),
    "{s}s": "{s} ثانیه",
    "{m}m {s:02}s": "{m} دقیقه و {s} ثانیه",
    "{h}h {m:02}m": "{h} ساعت و {m} دقیقه",
    # --- retries and stalls
    "⏸ No data for {idle}, retrying after {limit}": "⏸ {idle} هیچ داده‌ای نرسیده؛ تلاش دوباره بعد از {limit}",
    "⏸ No data for {idle}": "⏸ {idle} هیچ داده‌ای نرسیده",
    "⚠️ <b>Connection problem</b>: {reason}.": "⚠️ <b>مشکل اتصال</b>: {reason}.",
    "⚠️ <b>Download interrupted</b>: {reason}.": "⚠️ <b>دانلود قطع شد</b>: {reason}.",
    "🔁 Retry <b>{retry}</b> of {retries} · {when}": "🔁 تلاش دوباره <b>{retry}</b> از {retries} · {when}",
    "🔁 Retry <b>{retry}</b> of {retries} of part {part} · {when}": (
        "🔁 تلاش دوباره <b>{retry}</b> از {retries} برای بخش {part} · {when}"
    ),
    "🔁 Restarting, retry <b>{retry}</b> of {retries} · {when}": (
        "🔁 شروع دوباره، تلاش <b>{retry}</b> از {retries} · {when}"
    ),
    "next try in {time}": "تلاش بعدی {time} دیگر",
    "reconnecting…": "در حال اتصال دوباره…",
    "♻️ Resuming from {size}, nothing is downloaded twice": (
        "♻️ ادامه از {size}؛ هیچ بخشی دوباره دانلود نمی‌شه"
    ),
    "♻️ {size} already downloaded will be reused": "♻️ {size} دانلودشده دوباره استفاده می‌شه",
    "🔁 Recovered after {n} retry": "🔁 پس از {n} تلاش دوباره، دانلود ادامه پیدا کرد",
    "🔁 Recovered after {n} retries": "🔁 پس از {n} تلاش دوباره، دانلود ادامه پیدا کرد",
    "a network error": "خطای شبکه",
    "the server stopped sending data": "سرور ارسال داده را متوقف کرد",
    "the server replied {code}": "سرور با کد {code} پاسخ داد",
    "the connection dropped mid-transfer": "اتصال وسط انتقال قطع شد",
    "the connection was lost": "اتصال قطع شد",
    # --- cancel
    "✖️ Cancel download": "✖️ لغو دانلود",
    "🛑 <i>Cancelling…</i>": "🛑 <i>در حال لغو…</i>",
    "🚫 <b>Download cancelled</b>\nNothing was sent. Pick a format below to start again.": (
        "🚫 <b>دانلود لغو شد</b>\nچیزی ارسال نشد. برای شروع دوباره، یکی از کیفیت‌های زیر رو انتخاب کن."
    ),
    "🚫 Download cancelled.": "🚫 دانلود لغو شد.",
    "🛑 Cancelling… the download stops within a few seconds.": "🛑 در حال لغو… دانلود تا چند ثانیه‌ی دیگه متوقف می‌شه.",
    "📤 Too late to cancel: the file is already being sent.": "📤 دیگه نمی‌شه لغو کرد: فایل در حال ارساله.",
    "Only the person who started this download can cancel it.": "فقط کسی که این دانلود رو شروع کرده می‌تونه لغوش کنه.",
    # --- errors
    "⏳ You already have a download in progress. Please wait until it finishes.": (
        "⏳ یک دانلود در حال انجام داری. لطفاً صبر کن تا تمام بشه."
    ),
    "⌛ This request has expired, please send the link again.": "⌛ این درخواست منقضی شده، لطفاً لینک رو دوباره بفرست.",
    "⚠️ Download failed. Please try again later or pick another format.": (
        "⚠️ دانلود ناموفق بود. لطفاً بعداً دوباره امتحان کن یا کیفیت دیگری انتخاب کن."
    ),
    "❌ This format is larger than the upload limit.": "❌ حجم این کیفیت از حد مجاز ارسال بیشتر است.",
    "⚠️ This format or audio language is no longer available. Please send the link again.": (
        "⚠️ این کیفیت یا زبان صدا دیگر در دسترس نیست. لطفاً لینک رو دوباره بفرست."
    ),
    "⚠️ Delivery could not be confirmed. Please check the chat before requesting it again.": (
        "⚠️ ارسال فایل تأیید نشد. لطفاً قبل از درخواست دوباره، گفتگو رو بررسی کن."
    ),
    "⚠️ The service is temporarily unavailable. Please try again later.": (
        "⚠️ سرویس موقتاً در دسترس نیست. لطفاً بعداً دوباره امتحان کن."
    ),
    "⚠️ This request took too long. Please try again later or choose a smaller format.": (
        "⚠️ این درخواست بیش از حد طول کشید. لطفاً بعداً امتحان کن یا کیفیت کم‌حجم‌تری انتخاب کن."
    ),
    "⚠️ Couldn't access this media. It may be private, removed, or require login.": (
        "⚠️ دسترسی به این رسانه ممکن نشد. ممکنه خصوصی یا حذف شده باشه یا نیاز به ورود داشته باشه."
    ),
    "⚠️ Too many requests. Please wait a little before trying again.": (
        "⚠️ تعداد درخواست‌ها زیاده. لطفاً کمی صبر کن و دوباره امتحان کن."
    ),
    "⚠️ I can't send media here. Check the bot's chat permissions and try again.": (
        "⚠️ نمی‌تونم اینجا رسانه بفرستم. دسترسی‌های ربات در این گفتگو رو بررسی کن و دوباره امتحان کن."
    ),
    "⚠️ This group has moved. Please send your link again in the new group.": (
        "⚠️ این گروه منتقل شده. لطفاً لینک رو در گروه جدید دوباره بفرست."
    ),
    "⚠️ Couldn't process this media. Please try another format.": (
        "⚠️ پردازش این رسانه ممکن نشد. لطفاً کیفیت دیگری رو امتحان کن."
    ),
    "The server is low on storage right now. Please try again in a few minutes.": (
        "فضای ذخیره‌سازی سرور در حال حاضر کم است. لطفاً چند دقیقه‌ی دیگر دوباره امتحان کن."
    ),
    # --- platform messages
    "This format is no longer available. Please send the link again.": (
        "این کیفیت دیگر در دسترس نیست. لطفاً لینک رو دوباره بفرست."
    ),
    "This URL does not belong to the selected platform.": "این لینک مربوط به سرویس انتخاب‌شده نیست.",
    "No downloadable media found in this post.": "در این پست رسانه‌ی قابل دانلودی پیدا نشد.",
    "Couldn't read every item in this post. Please try again later.": (
        "خواندن همه‌ی موارد این پست ممکن نشد. لطفاً بعداً دوباره امتحان کن."
    ),
    "No downloadable items found in this playlist.": "در این پلی‌لیست مورد قابل دانلودی پیدا نشد.",
    "Only Spotify tracks, albums and playlists are supported.": (
        "فقط آهنگ، آلبوم و پلی‌لیست‌های Spotify پشتیبانی می‌شوند."
    ),
    "Couldn't read this Spotify album or playlist.": "خواندن این آلبوم یا پلی‌لیست Spotify ممکن نشد.",
    "Couldn't read this Spotify track.": "خواندن این آهنگ Spotify ممکن نشد.",
    "Couldn't find this track on YouTube.": "این آهنگ در یوتیوب پیدا نشد.",
    "This Threads post has no photos or videos.": "این پست Threads عکس یا ویدیویی ندارد.",
    "Couldn't read this Threads post (it may be private or need login).": (
        "خواندن این پست Threads ممکن نشد (ممکنه خصوصی باشه یا نیاز به ورود داشته باشه)."
    ),
    # --- required channels
    "🔒 <b>Join our channel</b>\n\nTo use this bot, join <b>{title}</b>.": (
        "🔒 <b>عضویت در کانال</b>\n\nبرای استفاده از ربات، عضو کانال <b>{title}</b> شو."
    ),
    "🔒 <b>Join our channels</b>\n\nTo use this bot, join:": (
        "🔒 <b>عضویت در کانال‌ها</b>\n\nبرای استفاده از ربات، عضو این کانال‌ها شو:"
    ),
    "Join the channel first, then check your membership.": "اول عضو کانال شو، بعد عضویتت رو بررسی کن.",
    "Membership not found yet. Join the channel, then check again.": (
        "هنوز عضویتت پیدا نشد. عضو کانال شو و دوباره بررسی کن."
    ),
    "Membership verification is temporarily unavailable. Please try again.": (
        "بررسی عضویت موقتاً ممکن نیست. لطفاً دوباره امتحان کن."
    ),
    "Join all of these channels first, then check your membership.": (
        "اول عضو همه‌ی این کانال‌ها شو، بعد عضویتت رو بررسی کن."
    ),
    "Membership not found in every channel yet. Join the remaining channels, then check again.": (
        "هنوز عضویتت در همه‌ی کانال‌ها پیدا نشد. عضو کانال‌های باقی‌مانده شو و دوباره بررسی کن."
    ),
    "Join channel ↗️": "عضویت در کانال ↗️",
    "Join {title} ↗️": "عضویت در {title} ↗️",
    "✅ Check membership": "✅ بررسی عضویت",
    "Send /start to check your own membership.": "برای بررسی عضویت خودت، /start رو بفرست.",
    # --- playlists, albums and messages with several links
    "🔎 Reading {n} links… {done} of {n} done": "🔎 در حال بررسی {n} لینک… {done} از {n} انجام شد",
    "🧺 <b>{n} links</b>": "🧺 <b>{n} لینک</b>",
    "🖼 {n} post": "🖼 {n} پست",
    "🖼 {n} posts": "🖼 {n} پست",
    "<i>Only the first {shown} of {total} items are offered.</i>": (
        "<i>فقط {shown} مورد اول از {total} مورد قابل انتخاب است.</i>"
    ),
    "…and {n} more": "…و {n} مورد دیگر",
    "⚠️ Couldn't read {n} link:": "⚠️ خواندن {n} لینک ممکن نشد:",
    "⚠️ Couldn't read {n} links:": "⚠️ خواندن {n} لینک ممکن نشد:",
    "🔗 Skipped {n} other link(s): not supported, or over the limit.": (
        "🔗 {n} لینک دیگر نادیده گرفته شد: پشتیبانی نمی‌شوند یا بیش از حد مجاز بودند."
    ),
    "✅ <b>{n}</b> of {total} selected": "✅ <b>{n}</b> از {total} مورد انتخاب شده",
    "Quality: {quality}": "کیفیت: {quality}",
    "They are downloaded and sent one by one. Confirm below ⤵️": (
        "موارد یکی‌یکی دانلود و ارسال می‌شوند. از دکمه‌های زیر تأیید کن ⤵️"
    ),
    "⚠️ Nothing to download in this message.": "⚠️ در این پیام چیزی برای دانلود پیدا نشد.",
    "Tap an item to include it or leave it out.": "روی هر مورد بزن تا انتخاب یا حذف بشه.",
    "Page {page} of {pages} · {n} of {total} selected": "صفحه‌ی {page} از {pages} · {n} از {total} انتخاب شده",
    "📥 Download {n}": "📥 دانلود {n} مورد",
    "☑️ Choose items": "☑️ انتخاب موارد",
    "✖️ Cancel": "✖️ لغو",
    "☑️ Select all": "☑️ انتخاب همه",
    "⬜ Select none": "⬜ حذف همه",
    "✔️ Done": "✔️ تأیید",
    "⏹ Stop after this item": "⏹ توقف بعد از این مورد",
    "▶️ <b>Item {n} of {total}</b>": "▶️ <b>مورد {n} از {total}</b>",
    "Now: {title}": "در حال انجام: {title}",
    "⏹ Stopping after the current item…": "⏹ بعد از مورد فعلی متوقف می‌شود…",
    "<i>Each item shows its own progress card below.</i>": "<i>پیشرفت هر مورد در پیام جداگانه‌ای در پایین نمایش داده می‌شود.</i>",
    "✅ <b>Done</b>: {sent} of {total} sent": "✅ <b>تمام شد</b>: {sent} از {total} مورد ارسال شد",
    "⏹ <b>Stopped</b>: {sent} of {total} sent": "⏹ <b>متوقف شد</b>: {sent} از {total} مورد ارسال شد",
    "cancelled": "لغو شد",
    "{n} item(s) were not started.": "{n} مورد شروع نشد.",
    "🚫 Cancelled. Nothing was downloaded.": "🚫 لغو شد. چیزی دانلود نشد.",
    "Only the person who sent these links can use these buttons.": (
        "فقط کسی که این لینک‌ها رو فرستاده می‌تونه از این دکمه‌ها استفاده کنه."
    ),
    "This list has already started downloading.": "دانلود این فهرست قبلاً شروع شده.",
    "Select at least one item first.": "اول حداقل یک مورد رو انتخاب کن.",
    # --- hidden /admin panel
    "🛠 <b>Admin panel</b>": "🛠 <b>پنل مدیریت</b>",
    "🕒 {time} · time zone {zone}": "🕒 {time} · منطقه‌ی زمانی {zone}",
    "👥 <b>Users</b>: {total} · 🆕 today {new}": "👥 <b>کاربران</b>: {total} · 🆕 امروز {new}",
    "🟢 Active: 1h {h} · 24h {d} · 7d {w} · 30d {m}": "🟢 فعال: ۱ ساعت {h} · ۲۴ ساعت {d} · ۷ روز {w} · ۳۰ روز {m}",
    "📥 <b>Downloads today</b>: {outcomes}": "📥 <b>دانلودهای امروز</b>: {outcomes}",
    "⚙️ <b>Now</b>: ⬇️ {running} running · ⏳ {queued} waiting": "⚙️ <b>الان</b>: ⬇️ {running} در حال اجرا · ⏳ {queued} در صف",
    "🖥 Workers online: {workers}": "🖥 ورکرهای آنلاین: {workers}",
    "🚧 <b>Maintenance mode is on</b>": "🚧 <b>حالت تعمیر و نگهداری روشنه</b>",
    "🚧 <b>Maintenance mode is off</b>": "🚧 <b>حالت تعمیر و نگهداری خاموشه</b>",
    "👥 Users": "👥 کاربران",
    "📥 Downloads": "📥 دانلودها",
    "⚙️ Queue": "⚙️ صف",
    "🖥 System": "🖥 سیستم",
    "🏆 Top users": "🏆 کاربران برتر",
    "⚠️ Errors": "⚠️ خطاها",
    "🔎 Find user": "🔎 جستجوی کاربر",
    "📣 Broadcast": "📣 ارسال همگانی",
    "🚧 Maintenance": "🚧 تعمیر و نگهداری",
    "📄 Export users": "📄 خروجی کاربران",
    "🔄 Refresh": "🔄 به‌روزرسانی",
    "✖️ Close": "✖️ بستن",
    "⬅️ Back": "⬅️ بازگشت",
    "{d}d {h:02}h": "{d} روز و {h} ساعت",
    "{time} ago": "{time} پیش",
    "👥 <b>Users</b>": "👥 <b>کاربران</b>",
    "Total: <b>{total}</b>": "مجموع: <b>{total}</b>",
    "🆕 New: today {today} · yesterday {yesterday}": "🆕 جدید: امروز {today} · دیروز {yesterday}",
    "🟢 Last seen within: 1h {h} · 24h {d} · 7d {w} · 30d {m}": "🟢 آخرین بازدید در: ۱ ساعت {h} · ۲۴ ساعت {d} · ۷ روز {w} · ۳۰ روز {m}",
    "📊 DAU {dau} · WAU {wau} · MAU {mau} · DAU/MAU {ratio}": "📊 فعال روزانه {dau} · هفتگی {wau} · ماهانه {mau} · نسبت روزانه به ماهانه {ratio}",
    "🚫 Blocked the bot: {blocked} · ⛔ Banned: {banned}": "🚫 ربات رو بلاک کردن: {blocked} · ⛔ مسدود شده: {banned}",
    "👪 Groups: {groups} ({active} active in 7 days)": "👪 گروه‌ها: {groups} ({active} فعال در ۷ روز)",
    "<b>Active users per day</b> (new users after +)": "<b>کاربران فعال روزانه</b> (کاربران جدید بعد از +)",
    "<b>Active users per hour</b> (last 24 h)": "<b>کاربران فعال در هر ساعت</b> (۲۴ ساعت اخیر)",
    "Peak: {count} users at {hour}:00": "اوج: {count} کاربر در ساعت {hour}:00",
    "📥 <b>Downloads</b>": "📥 <b>دانلودها</b>",
    "Today: {outcomes}": "امروز: {outcomes}",
    "Yesterday: {outcomes}": "دیروز: {outcomes}",
    "📦 Uploaded today: {size} · ⏱ average {time} per download": "📦 آپلود امروز: {size} · ⏱ میانگین {time} برای هر دانلود",
    "🎯 Success rate (7 days): {rate} of {count} finished": "🎯 نرخ موفقیت (۷ روز): {rate} از {count} دانلود تمام‌شده",
    "<b>Delivered per day</b> (failures after ❌)": "<b>ارسال‌شده در هر روز</b> (ناموفق‌ها بعد از ❌)",
    "<b>Downloads per hour</b> (last 24 h)": "<b>دانلود در هر ساعت</b> (۲۴ ساعت اخیر)",
    "<b>Platforms</b> (7 days: delivered, success rate, failures)": "<b>سرویس‌ها</b> (۷ روز: ارسال‌شده، نرخ موفقیت، ناموفق)",
    " · ⏸ no data for {time}": " · ⏸ {time} بدون داده",
    "⬇️ {done} of {total} files": "⬇️ {done} از {total} فایل",
    "📤 uploading {size}": "📤 در حال آپلود {size}",
    "🎞 processing ({stage})": "🎞 در حال پردازش ({stage})",
    "⏳ preparing": "⏳ در حال آماده‌سازی",
    "⚙️ <b>Download queue</b>": "⚙️ <b>صف دانلود</b>",
    "⬇️ Running: {running} of {capacity} slots ({workers} workers × {per})": "⬇️ در حال اجرا: {running} از {capacity} ظرفیت ({workers} ورکر × {per})",
    "⏳ Waiting: {waiting} ({downloads} downloads, {steps} batch steps)": "⏳ در صف: {waiting} ({downloads} دانلود، {steps} مرحله‌ی فهرست)",
    "📬 Handed to workers, not finished: {count}": "📬 تحویل ورکرها، هنوز تمام‌نشده: {count}",
    "<b>Running now</b>": "<b>در حال اجرا</b>",
    " · 🔁 {n} retries": " · 🔁 {n} تلاش دوباره",
    "<b>Next in queue</b>": "<b>نفرات بعدی صف</b>",
    " · from a batch": " · از یک فهرست",
    "😴 Nothing is running or waiting.": "😴 هیچ دانلودی در حال اجرا یا در صف نیست.",
    "✖️ <b>Cancel this download?</b>": "✖️ <b>این دانلود لغو بشه؟</b>",
    "The user sees that it was cancelled and can request it again.": "کاربر می‌بینه که لغو شده و می‌تونه دوباره درخواستش بده.",
    "✖️ Yes, cancel it": "✖️ بله، لغوش کن",
    "<b>{role}</b> · {host} · up {uptime}": "<b>{role}</b> · {host} · روشن از {uptime} پیش",
    " · ⚠️ no heartbeat for {time}": " · ⚠️ {time} بدون گزارش سلامت",
    "   🧠 RAM {memory} · process {rss}": "   🧠 رم {memory} · پروسه {rss}",
    "   ⚙️ CPU {cpu} of one core ({cpus} available) · load {load}": "   ⚙️ پردازنده {cpu} از یک هسته ({cpus} هسته در دسترس) · بار {load}",
    "   💾 Disk {free} free of {total} ({used} used) · kept {items} files ({kept})": "   💾 دیسک {free} آزاد از {total} ({used} پر) · {items} فایل نگه‌داری‌شده ({kept})",
    "🖥 <b>System</b>": "🖥 <b>سیستم</b>",
    "⚠️ No bot or worker heartbeat in the last 90 s.": "⚠️ در ۹۰ ثانیه‌ی اخیر هیچ گزارش سلامتی از ربات یا ورکر نرسیده.",
    "⚠️ <b>No worker is online</b>: downloads wait in the queue.": "⚠️ <b>هیچ ورکری آنلاین نیست</b>: دانلودها توی صف می‌مونن.",
    "<b>Redis</b> {version} · up {uptime}": "<b>Redis</b> {version} · روشن از {uptime} پیش",
    "   🧠 {memory}{limit} (peak {peak}) · 🔑 {keys} keys": "   🧠 {memory}{limit} (بیشینه {peak}) · 🔑 {keys} کلید",
    "   ⚡️ {ops} ops/s · {clients} clients · AOF {aof} · cache hit rate {hits}": "   ⚡️ {ops} عملیات در ثانیه · {clients} اتصال · AOF {aof} · نرخ موفقیت کش {hits}",
    "on": "روشن",
    "off": "خاموش",
    "⚠️ Redis is unreachable.": "⚠️ دسترسی به Redis ممکن نیست.",
    "<b>Settings</b>": "<b>تنظیمات</b>",
    "Platforms: {platforms}": "سرویس‌ها: {platforms}",
    "Downloads per worker {parallel} · connections per file {connections} · retries {retries} · stall timeout {timeout}s": "دانلود هم‌زمان هر ورکر {parallel} · اتصال برای هر فایل {connections} · تلاش دوباره {retries} · مهلت توقف {timeout} ثانیه",
    "Max file {file} MB · queue expiry {queue} · task limit {task} · lookups at once {slots}": "حداکثر فایل {file} مگابایت · انقضای صف {queue} · محدودیت هر کار {task} · جستجوی هم‌زمان {slots}",
    "Required channels {channels} · admins {admins} · bot uptime {uptime}": "کانال‌های اجباری {channels} · مدیران {admins} · روشن بودن ربات {uptime}",
    "🏆 <b>Top users</b> (delivered downloads, all time)": "🏆 <b>کاربران برتر</b> (دانلودهای ارسال‌شده، از ابتدا)",
    "No downloads recorded yet.": "هنوز دانلودی ثبت نشده.",
    "⚠️ <b>Recent failures</b>": "⚠️ <b>خطاهای اخیر</b>",
    "No failures recorded. 🎉": "هیچ خطایی ثبت نشده. 🎉",
    "🔎 No user with ID <code>{id}</code> has used the bot.": "🔎 کاربری با شناسه‌ی <code>{id}</code> از ربات استفاده نکرده.",
    "🔎 Find another": "🔎 جستجوی دوباره",
    "⛔ banned": "⛔ مسدود",
    "🚫 blocked the bot": "🚫 ربات رو بلاک کرده",
    "👤 <b>User</b> {who}": "👤 <b>کاربر</b> {who}",
    '<a href="tg://user?id={id}">Open profile</a>': '<a href="tg://user?id={id}">باز کردن پروفایل</a>',
    "🆕 First seen: {time}": "🆕 اولین بازدید: {time}",
    "🟢 Last seen: {time} ({ago})": "🟢 آخرین بازدید: {time} ({ago})",
    "🌐 Telegram language: {lang}": "🌐 زبان تلگرام: {lang}",
    "📥 Delivered downloads: {count}": "📥 دانلودهای ارسال‌شده: {count}",
    "⬇️ Downloading now: {platform} · {fmt}": "⬇️ در حال دانلود: {platform} · {fmt}",
    "✅ Unban": "✅ رفع مسدودی",
    "⛔ Ban": "⛔ مسدود کردن",
    "✉️ Message": "✉️ پیام",
    "📣 <b>Broadcast</b>": "📣 <b>ارسال همگانی</b>",
    "Choose who receives the message. Users who blocked the bot or are banned are skipped.": "انتخاب کن پیام به چه کسانی برسه. کاربرانی که ربات رو بلاک کردن یا مسدود شدن حذف می‌شن.",
    "⚠️ A broadcast is already running; wait for it to finish or stop it.": "⚠️ یک ارسال همگانی در حال اجراست؛ صبر کن تمام بشه یا متوقفش کن.",
    "⏹ Stop broadcast": "⏹ توقف ارسال همگانی",
    "Everyone ({n})": "همه ({n})",
    "Active in 30 days ({n})": "فعال در ۳۰ روز ({n})",
    "Active in 7 days ({n})": "فعال در ۷ روز ({n})",
    "Active in 24 hours ({n})": "فعال در ۲۴ ساعت ({n})",
    "📣 Send the message above to <b>{count}</b> users ({audience})?\n\nIt is copied as is, without a forward header. About {time} at 20 messages per second.": "📣 پیام بالا برای <b>{count}</b> کاربر ({audience}) فرستاده بشه؟\n\nعیناً و بدون برچسب «فوروارد شده» کپی می‌شه. با سرعت ۲۰ پیام در ثانیه حدود {time} طول می‌کشه.",
    "📣 Send": "📣 ارسال",
    "✅ <b>Broadcast finished</b>": "✅ <b>ارسال همگانی تمام شد</b>",
    "📣 <b>Broadcasting…</b>": "📣 <b>در حال ارسال همگانی…</b>",
    "⏹ <b>Broadcast stopped</b>": "⏹ <b>ارسال همگانی متوقف شد</b>",
    "Progress: {done} of {total} ({share})": "پیشرفت: {done} از {total} ({share})",
    "✅ Delivered {sent} · 🚫 blocked {blocked} · ❌ failed {failed}": "✅ رسیده {sent} · 🚫 بلاک‌کرده {blocked} · ❌ ناموفق {failed}",
    "Users get this notice instead of the bot; admins keep full access:": "کاربران به‌جای ربات این پیام رو می‌بینن؛ مدیران دسترسی کامل دارن:",
    "Downloads already queued keep running.": "دانلودهایی که از قبل در صف هستن ادامه پیدا می‌کنن.",
    "✅ Turn off": "✅ خاموش کردن",
    "While it is on, users only get a notice (the bot ignores their links); admins keep full access.": "وقتی روشنه، کاربران فقط یک پیام می‌گیرن (ربات لینک‌هاشون رو نادیده می‌گیره)؛ مدیران دسترسی کامل دارن.",
    "🚧 Turn on with the default notice": "🚧 روشن کردن با پیام پیش‌فرض",
    "✏️ Turn on with my own notice": "✏️ روشن کردن با پیام دلخواه",
    "🚧 The bot is under maintenance right now. Please try again a little later.": "🚧 ربات در حال حاضر در حال تعمیر و به‌روزرسانیه. لطفاً کمی بعد دوباره امتحان کن.",
    "That download has already finished.": "این دانلود قبلاً تمام شده.",
    "✅ Cancelled before it started.": "✅ قبل از شروع لغو شد.",
    "✅ Stopping: the worker cancels it within a few seconds.": "✅ در حال توقف: ورکر تا چند ثانیه‌ی دیگه لغوش می‌کنه.",
    "It is already uploading and can't be stopped.": "در حال آپلوده و دیگه نمی‌شه متوقفش کرد.",
    "🔎 Send a user's numeric ID or @username (of someone who has used the bot).": "🔎 شناسه‌ی عددی یا @نام‌کاربری یک کاربر رو بفرست (کسی که از ربات استفاده کرده).",
    "Admins can't be banned.": "مدیران رو نمی‌شه مسدود کرد.",
    "⛔ Banned: the bot now ignores this user.": "⛔ مسدود شد: ربات از این به بعد این کاربر رو نادیده می‌گیره.",
    "✅ Unbanned.": "✅ مسدودی برداشته شد.",
    "✉️ Send the message for user <code>{id}</code>. It is copied to them as is.": "✉️ پیام مورد نظر برای کاربر <code>{id}</code> رو بفرست. عیناً براش کپی می‌شه.",
    "📣 Send the message to broadcast: text, photo, video or any other message. You'll see it before it goes out.": "📣 پیام ارسال همگانی رو بفرست: متن، عکس، ویدیو یا هر پیام دیگه. قبل از ارسال پیش‌نمایشش رو می‌بینی.",
    "⏹ Stopping after the current message…": "⏹ بعد از پیام فعلی متوقف می‌شه…",
    "✏️ Send the notice users should see during maintenance.": "✏️ پیامی رو بفرست که کاربران در زمان تعمیر و نگهداری ببینن.",
    "📄 Preparing the export…": "📄 در حال آماده‌سازی خروجی…",
    "📄 {count} users": "📄 {count} کاربر",
    "⚠️ That didn't work; the details are in the bot's log.": "⚠️ انجام نشد؛ جزئیات در لاگ ربات ثبت شده.",
    "🔎 No user found. Send a numeric ID or the @username of someone who has used the bot.": "🔎 کاربری پیدا نشد. شناسه‌ی عددی یا @نام‌کاربری کسی رو بفرست که از ربات استفاده کرده.",
    "✅ Sent to <code>{id}</code>.": "✅ برای <code>{id}</code> فرستاده شد.",
    "🚫 Not sent: the user blocked the bot.": "🚫 فرستاده نشد: کاربر ربات رو بلاک کرده.",
    "❌ Not sent: {reason}": "❌ فرستاده نشد: {reason}",
    "everyone": "همه",
    "active in 30 days": "فعال در ۳۰ روز",
    "active in 7 days": "فعال در ۷ روز",
    "active in 24 hours": "فعال در ۲۴ ساعت",
    "✏️ Send the notice as text.": "✏️ پیام رو به‌صورت متن بفرست.",
    # --- inline mode (@bot <link> in any chat)
    "<b>✍️ In any chat, without adding me</b>": "<b>✍️ توی هر چتی، بدون اضافه کردن من</b>",
    "1. In the message box, type @{bot} and a space, then paste the link.": (
        "1. توی کادر پیام بنویس @{bot} و یک فاصله، بعد لینک رو بچسبون."
    ),
    "2. Pick a quality from the list that appears (⚡ means it is sent instantly).": (
        "2. از فهرستی که باز می‌شه یک کیفیت رو انتخاب کن (⚡ یعنی فوری فرستاده می‌شه)."
    ),
    "3. The progress shows in the chat, then turns into the file.": (
        "3. پیشرفت دانلود توی همون چت نشون داده می‌شه و بعد به فایل تبدیل می‌شه."
    ),
    "If that chat doesn't accept files, I send it to our private chat instead.": (
        "اگه اون چت فایل قبول نکنه، فایل رو توی چت خصوصی خودمون برات می‌فرستم."
    ),
    "⏳ <b>Starting the download…</b>\nIf nothing happens in a few seconds, press Start.": (
        "⏳ <b>در حال شروع دانلود…</b>\nاگه تا چند ثانیه اتفاقی نیفتاد، دکمه‌ی شروع رو بزن."
    ),
    "🚫 <b>Download cancelled</b>\nNothing was sent.": "🚫 <b>دانلود لغو شد</b>\nچیزی فرستاده نشد.",
    "📬 <b>Sent to your private chat with the bot</b>\nThis chat didn't accept the file.": (
        "📬 <b>توی چت خصوصی با ربات برات فرستادم</b>\nاین چت فایل رو قبول نکرد."
    ),
    "🔓 <b>Open the bot and press Start</b>\nThis chat didn't accept the file, and I can't message you before you start the bot. The file arrives right after.": (
        "🔓 <b>ربات رو باز کن و Start رو بزن</b>\nاین چت فایل رو قبول نکرد و تا ربات رو استارت نکنی نمی‌تونم بهت پیام بدم. بلافاصله بعدش فایل می‌رسه."
    ),
    "This download has already started.": "این دانلود قبلاً شروع شده.",
    "Only the person who sent this can start it.": "فقط کسی که این رو فرستاده می‌تونه شروعش کنه.",
    "🚧 The bot is under maintenance. Please try again later.": "🚧 ربات در حال تعمیر و نگهداریه. لطفاً بعداً دوباره امتحان کن.",
    "⏳ Still looking… add a space to refresh": "⏳ هنوز در حال بررسی… برای به‌روزرسانی یک فاصله اضافه کن",
    "📖 Paste a link here to download it": "📖 لینک رو اینجا بچسبون تا دانلودش کنم",
    "🔒 Join our channel to use the bot": "🔒 برای استفاده از ربات عضو کانال شو",
    "📂 Open the playlist in the bot": "📂 باز کردن پلی‌لیست توی ربات",
    "📂 Open the album in the bot": "📂 باز کردن آلبوم توی ربات",
    "🌐 More audio languages in the bot": "🌐 زبان‌های صوتی بیشتر توی ربات",
    "📂 More options in the bot": "📂 گزینه‌های بیشتر توی ربات",
    "⚡ {label} · instant": "⚡ {label} · فوری",
    "🖼 Photo": "🖼 عکس",
    "🎬 Video": "🎬 ویدیو",
    "🎵 Track": "🎵 آهنگ",
    "▶️ Start download": "▶️ شروع دانلود",
    "🔁 Choose again": "🔁 انتخاب دوباره",
    "🎵 Get the music (MP3)": "🎵 دریافت موسیقی (MP3)",
    "🤖 Open the bot": "🤖 باز کردن ربات",
}
