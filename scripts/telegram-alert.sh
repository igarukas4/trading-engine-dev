#!/usr/bin/env bash
set -euo pipefail

token_file=/etc/trading-engine/telegram-bot-token
chat_id_file=/etc/trading-engine/telegram-chat-id
[[ $# -gt 0 ]] || { printf 'usage: telegram-alert.sh MESSAGE\n' >&2; exit 2; }
message=$*

[[ -s "$token_file" ]] || { printf 'Telegram token file is missing or empty\n' >&2; exit 1; }
[[ -s "$chat_id_file" ]] || { printf 'Telegram chat ID file is missing or empty\n' >&2; exit 1; }

token=$(<"$token_file")
chat_id=$(<"$chat_id_file")
printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$token" | \
  curl --fail --silent --show-error --config - \
    --data-urlencode "chat_id=$chat_id" \
    --data-urlencode "text=$message" >/dev/null
