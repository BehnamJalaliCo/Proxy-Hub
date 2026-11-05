"""The owner's two cleaning buttons (owner only, each with a confirmation and an actions_log row).

* :mod:`proxyhub.cleanup.ended`: «پاک کردن پایان‌یافته‌ها», removes ENDED records from the
  database (deleted nodes and what hangs on them, ended Reality configs, retired sponsor
  registrations, finished TEST-channel post rows, finished outbox jobs), in one transaction, with a
  dry-run preview. Never cost_ledger, actions_log, settings, secrets, accounts or anything live.
* :mod:`proxyhub.cleanup.test_channel`: «پاک کردن کانال آزمایشی», empties the TEST Telegram
  channel (decision D44). Never the main channel, in any code path.
"""
