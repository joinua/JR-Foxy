# JR-Foxy

Telegram-бот JokerRecon на Python 3.11, aiogram v3, SQLite/aiosqlite та Docker Compose.

## Production deployment

Production deployments use only `.github/workflows/release-deploy.yml`.

- The workflow is started manually with a version in `vX.Y.Z` format and Ukrainian release notes.
- SSH access uses the restricted `deploy` account and the `DEPLOY_*` GitHub secrets.
- The workflow checks out the exact commit, runs `docker compose up -d --build --remove-orphans`, waits for the `jr-foxy` health check, creates a GitHub Release, and only then notifies the Officers chat.
- The bot database remains on the VPS in the bind-mounted `./data:/app/data` directory.
- Production deployment must not use direct root SSH access.

Required production secrets:

- `DEPLOY_HOST`
- `DEPLOY_USER`
- `DEPLOY_PATH`
- `DEPLOY_KNOWN_HOSTS`
- `DEPLOY_SSH_KEY`
- `TELEGRAM_BOT_TOKEN`
- `OFFICERS_CHAT_ID`
- `OFFICERS_THREAD_ID` (optional)

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
