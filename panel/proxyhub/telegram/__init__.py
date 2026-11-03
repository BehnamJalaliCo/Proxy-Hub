"""The panel's one Telegram bot (docs/04_CHANNEL.md, docs/10 section 3.6).

The bot does two things only: messages to the owner's private chat (alerts, pairing replies) and
posting/EDITING in the owner's channels. Nothing here can remove a message (CLAUDE.md rule 6), and
the bot token never reaches a log, an API response, actions_log or a plaintext column (rule 3).
"""
