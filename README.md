# Shunt-хуки для Claude Code (без внешней модели)

Три `PreToolUse`-хука, которые не дают Claude Code тянуть большие файлы
целиком в контекст, плюс валидатор структуры OKF-концептов. Работают
только на bash/jq — без вызова какой-либо LLM.

| Файл | Что делает |
|---|---|
| `check-file-size.sh` | Блокирует `Read` файлов больше порога, возвращает grep-фрагмент по ключевым словам запроса или хвост файла |
| `check-bash-read.sh` | То же самое для `cat`/`head`/`tail`/`less`/`more` внутри `Bash` |
| `check-okf-type.sh` | Блокирует запись `.md`-файла, если в YAML frontmatter нет обязательного поля `type` |
| `settings-snippet.json` | Пример регистрации всех трёх в конфиге |

---

## 1. Требования

- `bash` (уже есть на macOS/Linux)
- `jq` — если не установлен:
  ```bash
  # macOS
  brew install jq
  # Debian/Ubuntu/Fedora
  sudo apt install jq   # или: sudo dnf install jq
  ```

## 2. Куда положить файлы

Хуки удобнее хранить **в самом проекте**, чтобы они попадали в git вместе с кодом:

```bash
mkdir -p .claude/hooks
cp check-file-size.sh check-bash-read.sh check-okf-type.sh .claude/hooks/
chmod +x .claude/hooks/*.sh
```

Если нужно применить хуки ко **всем** проектам, а не только к одному —
положите их в `~/.claude/hooks/` вместо `.claude/hooks/` внутри проекта,
и правьте пути в шаге 3 соответственно (`~/.claude/settings.json`
вместо `.claude/settings.json` проекта).

## 3. Регистрация в settings.json

Откройте (или создайте) `.claude/settings.json` в корне проекта и
добавьте туда содержимое `settings-snippet.json`. Если файл уже
существует и в нём есть свои `hooks`/`env` — **слейте вручную**,
не затирайте существующие настройки:

```bash
cat settings-snippet.json
# скопируйте нужные секции в ваш существующий .claude/settings.json
```

Минимальный рабочий `settings.json` с нуля — это просто
`settings-snippet.json` как есть:

```bash
cp settings-snippet.json .claude/settings.json
```

## 4. Проверка, что хуки подключились

Перезапустите Claude Code в проекте (или начните новую сессию), затем
внутри сессии выполните:

```
/hooks
```

Claude Code покажет список зарегистрированных хуков — там должны
появиться `check-file-size.sh`, `check-bash-read.sh`, `check-okf-type.sh`
под событием `PreToolUse`.

## 5. Ручной тест каждого хука (без запуска Claude Code)

Хуки — это обычные скрипты, читающие JSON из stdin. Можно проверить
их напрямую:

```bash
# создайте тестовый файл побольше порога
seq 1 500 > /tmp/big.md

# check-file-size.sh
echo '{"tool_name":"Read","tool_input":{"file_path":"/tmp/big.md"}}' \
  | .claude/hooks/check-file-size.sh

# check-bash-read.sh
echo '{"tool_name":"Bash","tool_input":{"command":"cat /tmp/big.md"}}' \
  | .claude/hooks/check-bash-read.sh

# check-okf-type.sh — файл без обязательного поля type (должен заблокировать)
echo '{"tool_name":"Write","tool_input":{"file_path":"/tmp/concept.md","content":"---\ntitle: Пример\n---\nТекст"}}' \
  | .claude/hooks/check-okf-type.sh

# check-okf-type.sh — файл с полем type (должен пропустить, вывод {})
echo '{"tool_name":"Write","tool_input":{"file_path":"/tmp/concept.md","content":"---\ntype: service\ntitle: Пример\n---\nТекст"}}' \
  | .claude/hooks/check-okf-type.sh
```

Ожидаемый результат для первых трёх вызовов — JSON с
`"permissionDecision": "deny"` и текстом-объяснением в
`permissionDecisionReason`. Для последнего — пустой объект `{}`
(разрешение без вмешательства).

## 6. Настройка порогов

Правьте через `env` в `settings.json` (значения по умолчанию):

| Переменная | По умолчанию | Что меняет |
|---|---|---|
| `SHUNT_MIN_LINES` | `350` | С какого числа строк файл считается "большим" |
| `SHUNT_CONTEXT_LINES` | `3` | Сколько строк контекста показывать вокруг grep-совпадения |
| `SHUNT_TAIL_LINES` | `80` | Сколько строк хвоста показывать, если grep ничего не нашёл |

## 7. Если что-то не работает

- **Хук не сработал вообще** — проверьте, что путь в `command` в
  `settings.json` правильный и файл исполняемый (`chmod +x`).
- **`jq: command not found`** — установите `jq` (шаг 1).
- **Ошибка вместо блокировки** — запустите хук вручную (шаг 5) и
  посмотрите stderr — синтаксис bash можно проверить так:
  ```bash
  bash -n .claude/hooks/check-file-size.sh
  ```
- **Хук блокирует то, что не должен** (например, целевое чтение с
  offset) — убедитесь, что вызов действительно идёт без
  `offset`/`limit` в `tool_input`; хуки специально пропускают такие
  вызовы без вмешательства.

## 8. Отключение

Чтобы временно отключить конкретный хук — удалите соответствующий
блок из `hooks.PreToolUse` в `settings.json` (не обязательно удалять
сам файл скрипта). Чтобы отключить всё — удалите весь раздел `"hooks"`.
