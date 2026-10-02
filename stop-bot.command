#!/bin/zsh
# Stops only the local dashboard listening on port 8787.
bot_pid=$(lsof -tiTCP:8787 -sTCP:LISTEN)
if [[ -n "$bot_pid" ]]; then
  kill "$bot_pid"
  echo "Bot stopped."
else
  echo "No bot is running on port 8787."
fi
