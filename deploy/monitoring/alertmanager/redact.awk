# Log redactor (rule 3): copies stdin to stdout one line at a time (flushed), with secrets replaced
# by "<redacted>". Portable awk (busybox awk in the Alertmanager image, mawk/gawk on the host).
#
# Why: Alertmanager v0.34.2 writes the full Telegram Bot API URL, ".../bot<TOKEN>/sendMessage",
# into its log whenever a send fails (timeout, DNS, 429...). alertmanager/entrypoint.sh therefore
# pipes everything Alertmanager prints through this filter, and deploy/monitoring/safe_logs.sh uses
# it for logs that go into a report. deploy/monitoring/check_configs.sh proves it with a real failed
# send.
#
#   awk -v secret_files="FILE ..." -f redact.awk
#
# secret_files: space-separated paths. Every line of 8+ characters in those files is replaced
# literally wherever it appears (no regex, so any character is safe), and so is the part after its
# first ":" (the secret half of a bot token "<id>:<secret>"). The values are read from the files,
# never passed on the command line (argv is visible in ps). Independently of the files, the token
# in any Bot API URL path ("/bot<id>:<secret>/", also with ":" URL-encoded) is always redacted.

function add_literal(s) {
  if (length(s) >= 8) {
    n_literals++
    literals[n_literals] = s
  }
}

function replace_literal(line, s,    out, i) {
  out = ""
  while ((i = index(line, s)) > 0) {
    out = out substr(line, 1, i - 1) "<redacted>"
    line = substr(line, i + length(s))
  }
  return out line
}

BEGIN {
  n_literals = 0
  n_files = split(secret_files, files, " ")
  for (f = 1; f <= n_files; f++) {
    while ((getline value < files[f]) > 0) {
      sub(/\r$/, "", value)
      add_literal(value)
      colon = index(value, ":")
      if (colon > 0) add_literal(substr(value, colon + 1))
    }
    close(files[f])
  }
}

{
  line = $0
  for (k = 1; k <= n_literals; k++) line = replace_literal(line, literals[k])
  gsub(/bot[0-9]+(:|%3[Aa])[A-Za-z0-9_-]+/, "bot<redacted>", line)
  print line
  fflush()
}
