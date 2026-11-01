# ۷. مدل داده (پیش‌نویس)

> نام‌ها انگلیسی، توضیح فارسی. همه‌ی زمان‌ها UTC (`timestamptz`). هر جدول `id uuid`، `created_at` و `updated_at` دارد.

## زیرساخت
| جدول | ستون‌های اصلی | توضیح |
|---|---|---|
| `settings` | key, value (jsonb) | هدف‌ها، سقف‌ها، متن وضعیت‌ها، kill switch |
| `nodes` | hetzner_id, name, location, server_type, ipv4, ipv6, state, protocols[], reality_pubkey, mtproto_secret_ref, created_at, ready_at, burned_at, retired_at, deleted_at | چرخه‌ی عمر در `03_NODES.md`. `state` یکی از requested / creating / booting / registering / ready / unhealthy / burned / retiring / deleted |
| `node_heartbeats` | node_id, at, cpu, mem, conns_total, rx_bytes, tx_bytes | فقط عددهای کلی. نگهداری کوتاه (مثلاً ۷ روز)، و بعد خلاصه‌ی ساعتی |
| `probes` | node_id, source (iran_vps / traffic / public), at, ok, kind (tcp / tls / vless / mtproto), latency_ms | سنجش دسترسی |
| `node_events` | node_id, at, kind, detail | ساخت، reboot، سوختن، حذف؛ برای تاریخچه و گزارش |
| `cost_ledger` | day, node_id, euros_estimated, source | خرج روزانه‌ی تخمینی؛ تطبیق با فاکتور ماهانه |

## کانفیگ و کانال
| جدول | ستون‌های اصلی | توضیح |
|---|---|---|
| `configs` | node_id, protocol, xray_user_id / mtproto_secret_ref, expires_at, quota_bytes, used_bytes, state (ready / posted / expired / full / burned) | هر کانفیگ مشترک یک پست |
| `posts` | config_id, channel_id, message_id, posted_at, status_line, edited_at, creative_id, invite_link_id | **هرگز حذف نمیشود** |
| `creatives` | name, text, url, weight, starts_at, ends_at, advertiser | تبلیغ پایین پست (A/B و فروش تبلیغ) |
| `invite_links` | channel_id (کانال سیگنال), link, name, post_id, created_at | لینک دعوت اختصاصی هر پست |

## CRM جامعه
| جدول | ستون‌های اصلی | توضیح |
|---|---|---|
| `members` | telegram_user_id (یکتا), first_name, username, first_seen_at | یک ردیف برای هر آدم |
| `memberships` | member_id, channel_id, joined_at, left_at, via_invite_link_id | هر عضویت؛ رفت و برگشت ردیف تازه است |
| `tg_updates` | update_id (یکتا), received_at, kind, payload (jsonb) | رویداد خام برای پردازش idempotent؛ نگهداری کوتاه |
| `bot_subscribers` | member_id, started_at, blocked_at | فقط اگر ربات اختیاری (O6) روشن شد |

## گزارش‌ها (view یا جدول خلاصه)
- `daily_growth`: عضو تازه و ترک هر کانال در هر روز.
- `post_performance`: عضو آمده از لینک هر پست، به تفکیک ساعت، پروتکل و creative.
- `cost_per_member`: خرج دوره تقسیم بر عضو تازه‌ی کانال سیگنال در همان دوره.
- `cohort_retention`: چند درصد از عضوهای هر هفته بعد از ۱، ۷ و ۳۰ روز مانده‌اند.

## چیزهایی که **هیچ‌جا** نیست
IP کاربر، زمان اتصال هر کاربر، و مقصد ترافیک (قانون ۷).
