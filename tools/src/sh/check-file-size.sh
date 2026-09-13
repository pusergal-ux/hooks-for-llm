#!/usr/bin/env bash
# PreToolUse хук: вместо полного чтения больших файлов через Read
# возвращает релевантный фрагмент (grep по ключевым словам из вопроса,
# либо хвост файла как fallback) — без вызова внешней модели.
#
# Регистрируется в .claude/settings.json на событие PreToolUse с matcher "Read".
set -euo pipefail

THRESHOLD="${SHUNT_MIN_LINES:-350}"
CONTEXT_LINES="${SHUNT_CONTEXT_LINES:-3}"
MAX_MATCHES="${SHUNT_MAX_MATCHES:-40}"
TAIL_LINES="${SHUNT_TAIL_LINES:-80}"

INPUT=$(cat)
TOOL_NAME=$(jq -r '.tool_name // empty' <<< "$INPUT")
[ "$TOOL_NAME" = "Read" ] || { echo '{}'; exit 0; }

FILE_PATH=$(jq -r '.tool_input.file_path // empty' <<< "$INPUT")
OFFSET=$(jq -r '.tool_input.offset // empty' <<< "$INPUT")

# Точечное чтение (offset/limit) — агент уже знает, что ему нужно, пропускаем
[ -z "$OFFSET" ] || { echo '{}'; exit 0; }
[ -n "$FILE_PATH" ] && [ -f "$FILE_PATH" ] || { echo '{}'; exit 0; }

LINES=$(wc -l < "$FILE_PATH" | tr -d ' ')
[ "$LINES" -le "$THRESHOLD" ] && { echo '{}'; exit 0; }

# Ключевые слова берём из последнего запроса пользователя, если он доступен в hook input;
# иначе просто отдаём хвост файла.
PROMPT=$(jq -r '.prompt // empty' <<< "$INPUT" 2>/dev/null)

EXTRACT=""
if [ -n "$PROMPT" ]; then
  # Простая эвристика: слова длиннее 3 символов, без пунктуации, максимум 6 штук
  KEYWORDS=$(tr -cs '[:alnum:]_а-яА-ЯёЁ' '\n' <<< "$PROMPT" \
    | awk 'length($0) > 3' | sort -u | head -6)

  if [ -n "$KEYWORDS" ]; then
    PATTERN=$(paste -sd'|' <<< "$KEYWORDS")
    EXTRACT=$(grep -n -i -E -A "$CONTEXT_LINES" -B "$CONTEXT_LINES" "$PATTERN" "$FILE_PATH" 2>/dev/null \
      | head -n "$MAX_MATCHES" || true)
  fi
fi

# Fallback: если по ключевым словам ничего не нашлось — берём хвост файла
# (обычно самая свежая/релевантная часть — log.md, changelog и т.п.)
if [ -z "$EXTRACT" ]; then
  EXTRACT=$(tail -n "$TAIL_LINES" "$FILE_PATH")
  MODE_NOTE="совпадений по ключевым словам не найдено, показан хвост файла ($TAIL_LINES строк)"
else
  MODE_NOTE="показаны совпадения по ключевым словам из запроса ($CONTEXT_LINES строк контекста вокруг каждого)"
fi

jq -n \
  --arg extract "$EXTRACT" \
  --arg note "$MODE_NOTE" \
  --arg lines "$LINES" \
  --arg threshold "$THRESHOLD" \
  --arg path "$FILE_PATH" \
  '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: ("Файл \($path) содержит \($lines) строк (порог: \($threshold)), полное чтение заблокировано.\n" + $note + ":\n\n" + $extract + "\n\nЕсли нужен другой участок — вызови Read с явным offset/limit по номерам строк выше.")
    }
  }'
