# hooks-for-llm

Репозиторий содержит два независимых набора инструментов:

1. **Shunt-хуки для Claude Code** — bash/jq-хуки, ограничивающие контекст.
2. **Инструменты проекта Digital Accounting** — Python/Node-утилиты для
   извлечения и синхронизации данных.

---

## 1. Shunt-хуки для Claude Code (без внешней модели)

Три `PreToolUse`-хука, которые не дают Claude Code тянуть большие файлы
целиком в контекст, плюс валидатор структуры OKF-концептов. Работают
только на bash/jq — без вызова какой-либо LLM.

| Файл | Что делает |
|---|---|
| `check-file-size.sh` | Блокирует `Read` файлов больше порога, возвращает grep-фрагмент по ключевым словам запроса или хвост файла |
| `check-bash-read.sh` | То же самое для `cat`/`head`/`tail`/`less`/`more` внутри `Bash` |
| `check-okf-type.sh` | Блокирует запись `.md`-файла, если в YAML frontmatter нет обязательного поля `type` |
| `settings-snippet.json` | Пример регистрации всех трёх в конфиге |

Исходники лежат в [`tools/src/sh/`](tools/src/sh/).

### 1.1. Требования

- `bash` (уже есть на macOS/Linux)
- `jq` — если не установлен:
  ```bash
  # macOS
  brew install jq
  # Debian/Ubuntu/Fedora
  sudo apt install jq   # или: sudo dnf install jq
  ```

### 1.2. Куда положить файлы

Хуки удобнее хранить **в самом проекте**, чтобы они попадали в git вместе с кодом:

```bash
mkdir -p .claude/hooks
cp tools/src/sh/check-file-size.sh tools/src/sh/check-bash-read.sh tools/src/sh/check-okf-type.sh .claude/hooks/
chmod +x .claude/hooks/*.sh
```

Если нужно применить хуки ко **всем** проектам, а не только к одному —
положите их в `~/.claude/hooks/` вместо `.claude/hooks/` внутри проекта,
и правьте пути в шаге 1.3 соответственно (`~/.claude/settings.json`
вместо `.claude/settings.json` проекта).

### 1.3. Регистрация в settings.json

Откройте (или создайте) `.claude/settings.json` в корне проекта и
добавьте туда содержимое `settings-snippet.json`. Если файл уже
существует и в нём есть свои `hooks`/`env` — **слейте вручную**,
не затирайте существующие настройки:

```bash
cat tools/src/sh/settings-snippet.json
# скопируйте нужные секции в ваш существующий .claude/settings.json
```

Минимальный рабочий `settings.json` с нуля — это просто
`settings-snippet.json` как есть:

```bash
cp tools/src/sh/settings-snippet.json .claude/settings.json
```

### 1.4. Проверка, что хуки подключились

Перезапустите Claude Code в проекте (или начните новую сессию), затем
внутри сессии выполните:

```
/hooks
```

Claude Code покажет список зарегистрированных хуков — там должны
появиться `check-file-size.sh`, `check-bash-read.sh`, `check-okf-type.sh`
под событием `PreToolUse`.

### 1.5. Ручной тест каждого хука (без запуска Claude Code)

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

### 1.6. Настройка порогов

Правьте через `env` в `settings.json` (значения по умолчанию):

| Переменная | По умолчанию | Что меняет |
|---|---|---|
| `SHUNT_MIN_LINES` | `350` | С какого числа строк файл считается "большим" |
| `SHUNT_CONTEXT_LINES` | `3` | Сколько строк контекста показывать вокруг grep-совпадения |
| `SHUNT_TAIL_LINES` | `80` | Сколько строк хвоста показывать, если grep ничего не нашёл |

### 1.7. Если что-то не работает

- **Хук не сработал вообще** — проверьте, что путь в `command` в
  `settings.json` правильный и файл исполняемый (`chmod +x`).
- **`jq: command not found`** — установите `jq` (шаг 1.1).
- **Ошибка вместо блокировки** — запустите хук вручную (шаг 1.5) и
  посмотрите stderr — синтаксис bash можно проверить так:
  ```bash
  bash -n .claude/hooks/check-file-size.sh
  ```
- **Хук блокирует то, что не должен** (например, целевое чтение с
  offset) — убедитесь, что вызов действительно идёт без
  `offset`/`limit` в `tool_input`; хуки специально пропускают такие
  вызовы без вмешательства.

### 1.8. Отключение

Чтобы временно отключить конкретный хук — удалите соответствующий
блок из `hooks.PreToolUse` в `settings.json` (не обязательно удалять
сам файл скрипта). Чтобы отключить всё — удалите весь раздел `"hooks"`.

---

## 2. Инструменты проекта Digital Accounting

Инструменты проекта Digital Accounting: в основном Python (извлечение данных
в структурированные форматы под `data/`, синхронизация каталога систем
`data/systems/digital_accounting_systems.yaml` и т.п.), плюс отдельные
Node-инструменты там, где нужная экосистема — JS (например, рендер mermaid).

### 2.1. Структура

- `tools/src/` — исходный код инструментов (Python и Node вперемешку, см. список ниже)
- `tools/requirements.txt` — зависимости Python-инструментов (`pip install -r tools/requirements.txt`)
- `tools/package.json` — зависимости Node-инструментов (`npm install` внутри `tools/`,
  либо не устанавливать вовсе — скрипты сами тянут точную версию через `npx`)

### 2.2. Соглашение

Инструменты читают сырые материалы из `sources/` и `chats/`, а результат извлечения
кладут в `data/`. Диаграммы (`diagrams/`) и презентации (`presentation/`) — производные
артефакты, которые собираются вручную или другими инструментами на основе `data/`.

Инструменты, извлекающие материалы о **сторонних компаниях** (не о самом DA), пишут
результат в `company_cases/<company_slug>/`, а не в `data/` — это отдельная зона для
внешних референсов, не входящая в канонический каталог систем DA.

### 2.3. Инструменты

- `tools/src/js/youtube_case_extractor.py` — извлекает транскрипт, описание и ссылки из
  YouTube-видео (доклад/кейс о финтех- или банковской архитектуре) в
  `company_cases/<company_slug>/`. Используется скиллом
  `.claude/skills/youtube-case-study` (см. его `SKILL.md` для полного workflow, включая
  анализ сайта компании и формирование `analysis.md`). Требует `yt-dlp`
  (`pip install -r tools/requirements.txt`) и настоящий Python 3 (не Windows Store
  заглушку).

- `tools/src/js/sync_diagram.js` — держит триаду файлов одной диаграммы в синхроне:
  `<name>.mmd` (исходник mermaid, каноническая правка), `<name>.md` (тот же
  исходник в ```` ```mermaid ```` для просмотра в редакторе) и `<name>.svg`
  (рендер через `@mermaid-js/mermaid-cli`, версия зафиксирована в
  `tools/package.json` и в самом скрипте — работает независимо от того,
  какие mermaid-превью-расширения установлены). Передавайте путь либо к
  `.mmd`, либо к `.md` — какой из двух текстовых файлов только что правили
  руками, тот и становится источником истины для этого запуска:
  ```
  node tools/src/js/sync_diagram.js diagrams/<papka>/<name>.mmd ["Заголовок"]
  ```
  Требует Node.js и доступ в интернет при первом запуске (`npx` тянет
  `@mermaid-js/mermaid-cli` нужной версии; при повторных запусках npm кеширует
  пакет локально). Используется при любой правке диаграмм в `diagrams/`,
  включая создание диаграмм модели данных (`erDiagram`), не только
  `flowchart`.
