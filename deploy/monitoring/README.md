# پایش، هشدار و backup سرور پنل (گام M1.7)

> پشته‌ی کامل تصمیم D11. همه‌ی imageها با digest قفل شده‌اند (`deploy/docker-compose.yml`). نصب و آزمون واقعی روی سرور: `ops/prompts/P05.md`.

## اجزا
| سرویس | کار | از بیرون؟ |
|---|---|---|
| `prometheus` | متریک‌های `api:8000/internal/metrics`، node-exporter و خود پشته؛ قانون‌های هشدار (`prometheus/rules/panel.yml`) | نه |
| `alertmanager` | فرستادن هشدار به چت خصوصی مالک در تلگرام؛ Watchdog به نگهبان بیرونی | نه |
| `loki` | لاگ **فقط** کانتینرهای پنل (`api`، `worker`، `migrate`)، ۱۴ روز | نه |
| `alloy` | فرستنده‌ی لاگ: فایل‌های json-file داکر را فقط‌خواندنی میخواند، بی سوکت داکر | نه |
| `grafana` | نمای فنی (داشبورد `Proxy-Hub panel`) | فقط `127.0.0.1:3000` |
| `node-exporter` | CPU، حافظه، دیسک میزبان و متریک‌های backup (پوشه‌ی textfile) | نه |

تنظیم هر سرویس **درون image خودش** ساخته میشود (`*/Dockerfile`). پس هر تغییر تنظیم در git، با `compose build` و `up -d` خود deployer، همان سرویس را از نو میسازد. با bind mount، سرویس با تنظیم کهنه میماند.

## هشدارها
| هشدار | شرط |
|---|---|
| `PanelApiDown` | Prometheus بیش از ۲ دقیقه از API متریک نگرفته است |
| `WorkerHeartbeatStale` | ضربان worker کهنه‌تر از ۹۰ ثانیه (همان عدد `/healthz`؛ یک تست هر دو را برابر نگه میدارد) |
| `PanelDatabaseDown`، `PanelRedisDown` | API بیش از ۱ دقیقه به Postgres یا Redis وصل نشده است |
| `DiskAlmostFull` | بیش از ۸۵٪ دیسک پر است (۵ دقیقه) |
| `BackupFailed`، `BackupTooOld`، `BackupMetricsMissing` | آخرین backup شکست خورده، یا آخرین backup موفق کهنه‌تر از ۲۶ ساعت است، یا backup اصلاً اجرا نشده است |
| `RestoreTestFailing`، `RestoreTestTooOld` | آخرین آزمون بازگردانی شکست خورده، یا بیش از ۸ روز است که آزمون موفق نبوده است |
| `MonitoringTargetDown`، `TextfileMetricsError` | بخشی از خود پشته‌ی پایش از کار افتاده است |
| `Watchdog` | همیشه روشن است. فقط به نگهبان بیرونی میرود. |

متن هشدارها فارسی است، چون به مالک میرسد. هر قانون با `promtool test rules` آزمون واحد دارد (`prometheus/tests/panel_test.yml`)، و متن فارسی هم در همان آزمون سنجیده میشود.

## گیرنده‌های هشدار: پیش‌فرض «null»
`alertmanager/entrypoint.sh` تنظیم را هنگام شروع کانتینر از سه فایل اختیاری میسازد. `deploy/ensure_secrets.sh` این سه فایل را **خالی** میسازد و هرگز روی مقدار مالک نمینویسد:

| فایل در `/etc/proxy-hub/secrets/` | اگر پر و درست باشد | اگر خالی یا نادرست باشد |
|---|---|---|
| `alert_bot_token` و `alert_chat_id` (هر دو) | پیام تلگرام به چت خصوصی مالک (`telegram.tmpl`) | **گیرنده‌ی null**: هشدار در Alertmanager و Grafana دیده میشود، ولی به جایی فرستاده نمیشود |
| `healthchecks_watchdog_url` (فقط `https://`) | هشدار Watchdog هر ۵ دقیقه به healthchecks.io میرود. اگر سرور، Prometheus یا Alertmanager بیفتد، healthchecks.io از بیرون به مالک خبر میدهد. | گیرنده‌ی null |

- مقدار رمزها هرگز در تنظیم ساخته‌شده نمینشیند. Alertmanager آن‌ها را با `bot_token_file` و `url_file` از یک tmpfs میخواند. اگر رمزی در تنظیم پیدا شود، کانتینر بالا نمیآید.
- **توکن در لاگ (قانون ۳):** Alertmanager نسخه‌ی v0.34.2 هر بار که فرستادن به تلگرام شکست بخورد (قطعی شبکه، timeout، `429`)، نشانی کامل API تلگرام را با **توکن ربات** در لاگ خودش مینویسد. برای همین `entrypoint.sh` آن را مستقیم اجرا نمیکند. Alertmanager فرزند پوسته است و هر خطی که مینویسد، پیش از رسیدن به لاگ داکر، از `alertmanager/redact.awk` میگذرد. این فیلتر مقدار توکن، نیمه‌ی رمز آن و نشانی watchdog را (از همان فایل‌های tmpfs، نه از argv) و هر توکن در نشانی `/bot…/` را به `<redacted>` تبدیل میکند. اگر فیلتر بمیرد، Alertmanager در نوشتن بعدی لاگ میمیرد و کانتینر از نو بالا میآید؛ هیچ خط فیلترنشده‌ای بیرون نمیرود. پوسته `SIGTERM` را به Alertmanager میرساند (توقف آرام) و `SIGHUP` را هم (بارگذاری دوباره‌ی تنظیم).
- `check_configs.sh` این را واقعاً میسنجد: Alertmanager را با یک توکن ساختگی تصادفی و بی شبکه بالا میآورد، با `amtool` یک هشدار میسازد، صبر میکند تا فرستادن واقعاً شکست بخورد و در لاگ بیاید، و اگر توکن یا نیمه‌ی رمزش در `docker logs` باشد، شکست میخورد. توقف آرام با `SIGTERM` را هم میسنجد.
- برای گزارش، لاگ هیچ سرویسی را خام نچسبان. همیشه از `sudo bash deploy/monitoring/safe_logs.sh <service> [lines]` استفاده کن. همین فیلتر را با مقدار همه‌ی فایل‌های `/etc/proxy-hub/secrets/` به کار میبرد.
- راه بهتر بعدی (هماهنگی با تیم API): هشدارها با `webhook_configs` و `url_file` به یک endpoint داخلی پنل بروند که پیام را در صف تلگرام خود پنل بگذارد. خطای webhook در Alertmanager نشانی را `<redacted>` چاپ میکند. ولی اگر API پنل بیفتد، هشدار `PanelApiDown` هم نمیرسد؛ پس مسیر مستقیم تلگرام با همین فیلتر فعلاً میماند.
- خط وضعیت در لاگ کانتینر: `telegram receiver: on|off; external watchdog: on|off`.
- پس از پر کردن فایل‌ها: `sudo docker compose -p proxyhub restart alertmanager`. راه ورود توکن ربات هشدار به سرور، بی چت، با مالک تعیین میشود (قانون ۳ در `CLAUDE.md`).

## لاگ (قانون ۷)
- فقط کانتینرهایی که برچسب `io.proxyhub.logs=panel` دارند (`migrate`، `api`، `worker`) فرستاده میشوند. داکر این برچسب را در هر خط لاگ مینویسد، و Alloy هر خط بی آن را دور میریزد. از node و کاربر هیچ لاگی به این سرور نمیآید.
- پیش از فرستادن، هر نشانی IPv4 و IPv6 در متن به `<ip>` تبدیل میشود. خط‌های دسترسی `/internal/metrics`، `/livez` و `/healthz` هم دور ریخته میشوند.
- Alloy سوکت داکر ندارد (که برابر root است). فقط `/var/lib/docker/containers` را فقط‌خواندنی میبیند، با کاربر root و بی هیچ capability.
- لاگ سرویس‌های پنل (`migrate`، `api`، `worker`) و شش سرویس پایش چرخش دارد (`max-size` برابر 20m، ۵ فایل). `postgres`، `redis` و `caddy` هنوز با پیش‌فرض داکر‌اند و **چرخش ندارند**. افزودن `logging: *logging` به آن‌ها کار هماهنگ‌کننده است، چون آن سه کانتینر را در deploy بعدی از نو میسازد.

## backup (پوشه‌ی `deploy/backup/`)
- **روزانه** (`proxyhub-backup.timer`، ساعت 02:47 UTC): `pg_dump` درون کانتینر postgres. dump و شمارش ردیف همه‌ی جدول‌ها از **یک snapshot** گرفته میشوند. خروجی با `age` و کلید عمومی `/etc/proxy-hub/backup_age_recipient` رمز میشود و در `/var/lib/proxy-hub/backups/` میماند. ۱۴ نسخه‌ی آخر نگه داشته میشوند. نسخه‌ی رمزنشده هرگز روی دیسک میزبان نوشته نمیشود.
- **هفتگی** (`proxyhub-restore-test.timer`، یکشنبه 04:17 UTC): تازه‌ترین backup رمزگشایی میشود و در یک Postgres موقت بازگردانده میشود. این Postgres همان image را دارد، بی شبکه است و داده‌اش در حافظه است. شمارش ردیف هر جدول باید **دقیقاً** با manifest همان backup برابر باشد. کانتینر موقت همیشه پاک میشود. نتیجه در `/var/lib/proxy-hub/restore_test.status` و در متریک‌ها نوشته میشود.
- **کلید خصوصی** `backup_age_key` فقط روی همین سرور است (`0400`). اگر از دست برود، هیچ backup خوانا نیست. نسخه‌ی بیرون از سرور تصمیم مالک است.
- **نگهبان backup (اختیاری):** اگر `/etc/proxy-hub/secrets/healthchecks_url` پر باشد، هر backup موفق یا ناموفق به healthchecks.io خبر داده میشود. نشانی چاپ نمیشود.
- **اجرای دستی:** `sudo systemctl start proxyhub-backup.service` و `sudo systemctl start proxyhub-restore-test.service`؛ نتیجه در `journalctl -u ...`.
- **بازگردانی واقعی پس از فاجعه** (روی سرور تازه با همان کلید): `age -d -i backup_age_key proxyhub-<stamp>.dump.age > db.dump` درون پوشه‌ی موقت `0700`، سپس `pg_restore --exit-on-error --single-transaction -U proxyhub -d proxyhub db.dump` در Postgres خالی پنل، و در پایان پاک کردن `db.dump`.

## بررسی‌ها
- `bash deploy/monitoring/check_configs.sh`: همه‌ی تنظیم‌ها با همان imageهای قفل‌شده (promtool، آزمون واحد قانون‌ها، amtool برای هر پنج حالت گیرنده، آزمون واقعی نشت توکن در لاگ Alertmanager و توقف آرام آن، loki، alloy). حدود یک دقیقه طول میکشد.
- `sudo bash deploy/monitoring/safe_logs.sh <service> [lines] [project]`: لاگ یک سرویس با همه‌ی رمزها `<redacted>`؛ برای هر گزارشی.
- `sudo bash deploy/monitoring/verify_stack.sh [project]`: روی پشته‌ی در حال اجرا. همه‌ی targetها `up`اند؛ قانون‌ها بار شده‌اند؛ Watchdog از Prometheus به Alertmanager میرسد؛ Grafana سالم است و فقط روی loopback است؛ Loki آماده است؛ لاگ پنل رسیده است، فقط از سه سرویس پنل و بی هیچ IP.
