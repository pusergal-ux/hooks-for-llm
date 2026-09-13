#!/usr/bin/env bash
# PreToolUse хук: блокирует запись .md-концепта, если в YAML frontmatter
# отсутствует обязательное поле "type" (требование формата OKF).
# Чистая проверка, без вызова какой-либо модели.
#
# Регистрируется в .claude/settings.json на событие PreToolUse
# с matcher "Write|Edit" (или отдельно на каждый инструмент).
set -euo pipefail

INPUT=$(cat)
TOOL_NAME=$(jq -r '.tool_name // empty' <<< "$INPUT")

[[ "$TOOL_NAME" == "Write" || "$TOOL_NAME" == "Edit" ]] || { echo '{}'; exit 0; }

FILE_PATH=$(jq -r '.tool_input.file_path // empty' <<< "$INPUT")

# Реагируем только на .md-файлы внутри директории концептов OKF-базы
[[ "$FILE_PATH" == *.md ]] || { echo '{}'; exit 0; }

# Берём итоговое содержимое: для Write это content, для Edit — new_string
# (для Edit это не даст 100% полного файла, но ловит частый случай
# создания/переписывания frontmatter в рамках одного вызова).
CONTENT=$(jq -r '.tool_input.content // .tool_input.new_string // empty' <<< "$INPUT")
[ -n "$CONTENT" ] || { echo '{}'; exit 0; }

# Проверяем, что есть frontmatter (--- ... ---) и в нём есть поле type:
if ! grep -q '^---$' <<< "$CONTENT"; then
  echo '{}'; exit 0  # не похоже на OKF-концепт (нет frontmatter вообще) — не наше дело
fi

FRONTMATTER=$(awk '/^---$/{c++; if(c==2) exit} c==1' <<< "$CONTENT")

if ! grep -qE '^type:\s*\S+' <<< "$FRONTMATTER"; then
  jq -n --arg path "$FILE_PATH" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: ("Запись отклонена: " + $path + " — YAML frontmatter не содержит обязательного поля `type` (требование формата OKF). Добавь, например:\n\n---\ntype: service\ntitle: ...\n---")
    }
  }'
  exit 0
fi

echo '{}'
