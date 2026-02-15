# JR-Foxy

## TikTok Notify setup

1. Додайте в `.env` RSS посилання профілю:
   - `TIKTOK_RSS_URL=https://...`
2. (Опційно) налаштуйте:
   - `TIKTOK_PROFILE_URL` (за замовчуванням `https://www.tiktok.com/@jr__ua`)
   - `TIKTOK_CHECK_INTERVAL_SECONDS` (за замовчуванням `3600`)
   - `TIKTOK_NOTIFY_ENABLED` (`true/false`, за замовчуванням `true`)
   - `TIKTOK_THREAD_ID` (ID форум-теми, опційно)
3. Щоб зафіксувати тему через бота: відкрийте потрібну форум-тему та викличте `/tiktok_set_thread`.
   Бот збереже `message_thread_id` у `chat_settings` і надалі поститиме TikTok повідомлення саме туди.
