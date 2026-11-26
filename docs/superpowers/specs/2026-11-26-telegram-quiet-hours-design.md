# Telegram: quiet hours — design

Date: 2026-11-26. Small change. The operator asked whether the bot can send silent messages: silent by default from
22:00 to 09:00, the window set on the dashboard. They approved the design below ("делай").

## Decisions

1. **Silent, not held.** In the window every message the bot sends goes with Telegram's `disable_notification`: it
   arrives at once, without sound or vibration. No exceptions: a reply while the operator is chatting with the bot
   at night is silent too (they see it anyway). Edits of a sent message or its buttons never notify, so only
   `TelegramAdapter.send_message` changes.
2. **One global setting.** It is about the operator's night, and every project's messages reach the same person; no
   per-project window.
3. **Default on, 22:00–09:00, host local time.** The window may cross midnight (start later than end). Start equal to
   end is refused. An off switch keeps every message audible.
4. **Stored in the dashboard's settings table** (`data/events.db`, key `quiet_hours`, JSON), like the Claude model;
   no row means the default. A shared `QuietHours` object is loaded from it at startup, read by the adapter on each
   send, and changed in place by the dashboard's save, so a save applies to the next message without a restart.
5. **Dashboard: Settings → Quiet hours.** An on/off checkbox, `from` and `to` time fields, Save.
   `GET`/`PUT /api/settings/quiet-hours`.

## Approaches

- `disable_notification` in a time window (chosen).
- Hold messages until the window ends: a question the operator could answer at night waits until morning. Not chosen.
- Mute the chat in Telegram: Telegram has no scheduled mute; it is manual each night. Not chosen.
- Store in `global.yaml` `telegram.quiet_hours` like the language: needs a line editor for a nested block and buys
  nothing over the settings table for a runtime preference. Not chosen.
