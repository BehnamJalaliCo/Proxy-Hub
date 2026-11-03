# داشبورد مالک (dashboard/)

برنامه‌ی وب فارسی و راست‌به‌چپ پنل (تصمیم D12، `docs/10_IMPLEMENTATION_PLAN.md` بخش ۳.۹). روی گوشی و دسکتاپ، روشن و تاریک.

| لایه | انتخاب (نسخه‌ها دقیق pin شده‌اند؛ `package-lock.json`) |
|---|---|
| پایه | React 19 + TypeScript (strict) + Vite 8 |
| ظاهر | Tailwind CSS 4 + Radix، فونت Vazirmatn از npm (بی CDN؛ فیلتر ایران) |
| نمودار و انیمیشن | Apache ECharts 6 (SVG)، Motion |
| داده | TanStack Query 5 (به‌روزرسانی خودکار) |
| تاریخ | date-fns-jalali، منطقه‌ی زمانی Asia/Tehran |

**سیاست نسخه:** هر وابستگی تازه‌ترین نسخه‌ای است که دست‌کم ۷ روز پیش از نصب منتشر شده بود (`npm install --save-exact --before=<تاریخ>`)، تا نسخه‌ی تازه‌ی آلوده به زنجیره‌ی تأمین وارد نشود. TypeScript روی 6.0 مانده چون typescript-eslint هنوز 7 را نمیپذیرد.

## صفحه‌ها
`/setup` (لینک یک‌بارمصرف `/setup#<token>`؛ توکن فوراً از نوار نشانی پاک میشود) · `/login` · `/` نمای کلی · `/accounts` · `/settings` · `/events` · `/security`

## قانون‌ها در کد داشبورد
- هر درخواست: same-origin با کوکی جلسه و سرآیند `X-Requested-With: proxyhub`. پاسخ 401 → `/login`.
- رمز، کد 2FA و توکن هتزنر هرگز در cache داده (TanStack Query)، نشانی، localStorage یا کنسول نمیروند. `no-console` در eslint خطاست.
- فقط فیلدهای قرارداد API (`src/lib/types.ts`)؛ هیچ IP کاربری در هیچ صفحه‌ای نیست.
- CSP سخت (`panel/proxyhub/api/routes_zz_static.py`): اسکریپت فقط از خود پنل؛ هیچ اسکریپت درون‌خطی.

## فرمان‌ها
```sh
npm ci
npm run lint && npm run typecheck && npm test && npm run build   # همان کار CI
npm run dev        # Vite روی 5173؛ /api به API واقعی روی 127.0.0.1:8000 میرود
```

پنل `dist/` را خودش سرو میکند: در image در `/app/dashboard` (مرحله‌ی node در `panel/Dockerfile` با build context نام‌دار `dashboard`)، و در توسعه از `dashboard/dist` (یا `PROXYHUB_DASHBOARD_DIR`).

## اسکرین‌شات‌ها (`screenshots/`)
با `scripts/screenshots.mjs` و Playwright روی **API واقعی** و Postgres واقعی گرفته شده‌اند (۳۹۰×۸۴۴ و ۱۴۴۰×۹۰۰، روشن و تاریک). اسکریپت هر خطای کنسول یا صفحه، نبودن RTL و اسکرول افقی را خطا میداند. با `SETUP_TOKEN` (فقط روی دیتابیس دورریختنی) راه‌اندازی واقعی را هم طی میکند. QR، کلید 2FA و کدهای بازیابی در تصویرها پوشانده شده‌اند.

```sh
BASE_URL=http://localhost:8000 OUT=screenshots-raw node scripts/screenshots.mjs
```
