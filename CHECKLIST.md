# Health-check checklist

- Verified router registration order includes ChatGuard first and warnings router registered.
- Ensured catch-all member collector ignores commands so /warn and /mywarns can reach handlers.
- Added INFO logs for router registration, polling startup (bot.get_me), and /mywarns handler entry.
- Manually reviewed /warn, /unwarn, /winfo, /mywarns handlers for allowed contexts and logging behavior.

## Commands to validate
- /ping (private)
- /mywarns (private)
- /warn, !warn (group in allowed chat)
- /winfo (group in allowed chat; logs to ADMIN_LOG_CHAT_ID)
