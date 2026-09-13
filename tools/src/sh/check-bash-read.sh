#!/usr/bin/env bash
# PreToolUse хук: перехватывает cat/head/tail/less/more на больших файлах
# в Bash-командах и отдаёт релевантный фрагмент вместо всего файла —
# без вызова внешней модели.
#
# Регистрируется в .claude/settings.json на событие PreToolUse с matcher "Bash".
set -euo pipefail

THRESHOLD="${SHUNT_MIN_LINES:-350}"
TAIL_LINES="${SHUNT_TAIL_LINES:-80}"

INPUT=$(cat)
TOOL_NAME=$(jq -r '.tool_name // empty' <<< "$INPUT")
[ "$TOOL_NAME" = "Bash" ] || { echo '{}'; exit 0; }

CMD=$(jq -r '.tool_input.command // empty' <<< "$INPUT")

# Конвейеры/фильтры (cat file | grep ...) — это уже целевой поиск, пропускаем как есть
if [[ "$CMD" == *"|"* || "$CMD" == *"&&"* || "$CMD" == *";"* ]]; then
  echo '{}'; exit 0
fi

# Ловим только простую форму: cat|head|tail|less|more <путь>
if [[ "$CMD" =~ ^(cat|head|tail|less|more)[[:space:]]+([^[:space:]-][^[:space:]]*)$ ]]; then
  FILE_PATH="${BASH_REMATCH[2]}"
  [ -f "$FILE_PATH" ] || { echo '{}'; exit 0; }

  LINES=$(wc -l < "$FILE_PATH" | tr -d ' ')
  if [ "$LINES" -gt "$THRESHOLD" ]; then
    EXTRACT=$(tail -n "$TAIL_LINES" "$FILE_PATH")
    jq -n \
      --arg extract "$EXTRACT" \
      --arg cmd "$CMD" \
      --arg lines "$LINES" \
      --arg threshold "$THRESHOLD" \
      --arg tailn "$TAIL_LINES" \
      '{
        hookSpecificOutput: {
          hookEventName: "PreToolUse",
          permissionDecision: "deny",
          permissionDecisionReason: ("Команда `" + $cmd + "` читает файл из \($lines) строк (порог: \($threshold)), заблокировано.\nПоказан хвост файла (\($tailn) строк):\n\n" + $extract + "\n\nДля поиска по конкретной части используй `grep -n \"шаблон\" " + $cmd + "` или `sed -n \"START,ENDp\" " + $cmd + "`.")
        }
      }'
    exit 0
  fi
fi

echo '{}'
