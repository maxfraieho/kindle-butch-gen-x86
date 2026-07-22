# Методика редагування прев'ю (kindle-butch-gen)
### Проміжне та постгенеративне редагування: аудіокниги, текстові переклади, манга

Документ призначений як бриф для `agy` (Claude Code) — містить архітектуру, дані, API, UI та покроковий план впровадження з обов'язковим використанням GitNexus-індексу проєкту.

---

## 0. Важливий урок з історії проєкту (TASK-17)

У логах проєкту вже була спроба автоматичного другого проходу редагування — `edit_epub.py` / `kbg.sh edit` / `editor_model`, який LLM-ом "сліпо" перечитував весь переклад. Він **був видалений**, бо "виявився непотрібним на практиці" (TASK-17).

Це критично: нова методика — **не** відновлення того самого підходу. Це протилежна модель:
- не автоматичний суцільний re-pass LLM по всій книзі;
- а **точкове, кероване людиною (або агентом за явним запитом) редагування** конкретного чанка/сторінки/бульбашки, з вибірковою регенерацією лише зміненого фрагмента.

---

## 1. Поточний стан (за кодом у `app.py`)

Всі три прев'ю зараз **тільки для читання**:

| Режим | Endpoint | Дані |
|---|---|---|
| Аудіокнига/текст | `GET /api/preview/book/<slug>` | пагіновані параграфи: `hash, original, translated, stressed, has_audio` |
| Аудіо-чанк | `GET /api/preview/audio/<slug>/<chunk_hash>` | wav за хешем перекладеного тексту |
| Текст (EPUB reader) | `GET /api/preview/book-chapters/<slug>`, `GET /api/preview/book-page/<slug>/<href>` | глави/сторінки з EPUB |
| Манга | `GET /api/preview/manga/<slug>`, `GET /api/preview/manga-file/...` | source/cleaned/translated сторінки |

Ключовий факт архітектури, який треба використати, а не ламати: **аудіо-чанк іменується хешем перекладеного тексту** (`chunk_hash = sha256(translated_text)`). Це вже готовий механізм інвалідації — зміна тексту природно "осиротить" старий wav і вимагає нового.

---

## 2. Принципи методики

1. **Non-destructive overlay.** Правки зберігаються окремо від згенерованих артефактів (не перезаписують `translate_cache`/`merged_*.md`/CBZ напряму), доки не будуть застосовані.
2. **Гранулярність.** Одиниця редагування — параграф/чанк (текст+аудіо) або сторінка/бульбашка (манга). Ніякого суцільного re-pass.
3. **Дві стадії життєвого циклу:**
   - **Проміжне (intermediate)** — поки книга/манга ще `status=running`: QA-гейт, правки випереджають чергу генерації, щоб не витрачати compute на те, що буде перероблено.
   - **Постгенеративне (post-generative)** — після `status=complete`: повний прохід по готовій книзі, пакетні правки, вибіркова регенерація, і лише в кінці — переекспорт (EPUB/AZW3/CBZ/M4B).
4. **GitNexus-first.** Кожна зміна символу в pipeline-коді має проходити `impact()` перед редагуванням — це вже правило проєкту (рядок 2213 індексу), просто зараз воно застосовується і до нових edit-фіч.

---

## 3. Спільна модель даних — edit store

Новий файл на книгу: `books/<slug>/edits/edits.json` (простий append-only JSON; за потреби пізніше — SQLite).

```json
{
  "id": "e_...",
  "mode": "text | audio | manga",
  "target_id": "<chunk_hash>  або  <page_filename>  або  <page_filename>#<bubble_id>",
  "field": "translated_text | stress | manga_bubble_text",
  "original_value": "...",
  "edited_value": "...",
  "status": "pending | queued_for_regen | regenerated | approved",
  "created_at": "...",
  "applied_at": "..."
}
```

Стан-машина: `pending → queued_for_regen → regenerated → approved`.

Модуль `kbg_web/edit_store.py`: `add_edit()`, `list_pending(slug, mode)`, `mark_status()`, `get_edit(target_id)`. Все інше (TTS, typesetting, epub-merge) звертається сюди, а не напряму до файлів кешу.

---

## 4. Дизайн по режимах

### 4.1 Аудіокнига / Текст (спільний рушій, бо `/api/preview/book` вже об'єднує обидва)

Нові endpoints:
- `PUT /api/edit/text/<slug>/<chunk_hash>` — body `{translated_text}`. Зберігає overlay, рахує новий хеш, позначає старий `.wav` осиротілим, `needs_resynthesis=true`.
- `POST /api/edit/regenerate-audio/<slug>/<chunk_hash>` — синтезує **один** чанк повторно (виокремлений виклик з існуючого TTS-пайплайна, не весь прогін).
- `GET /api/edit/queue/<slug>` — список pending/queued правок книги.
- `POST /api/edit/approve/<slug>/<chunk_hash>` — мерджить overlay у `merged_translated_<lang>.md` і `translate_cache`.

Правка наголосу (`stress`) — легша операція: редагування конкретного запису в `stress_cache_<lang>.json` без повторного перекладу.

UI (`stages.html`): іконка "✏️" біля кожного параграфа → модалка з original/translated/stressed → Save викликає `PUT`; якщо `translated_text` змінився — з'являється кнопка "Regenerate audio"; плеєр оновлюється після завершення регену.

### 4.2 Текстова книга (EPUB reader)

Використовує той самий `merged_translated_<lang>.md`, що й аудіокнига — тобто той самий `PUT /api/edit/text/...`. Різниця лише в тому, що застосування overlay до фінального `.epub` відкладене: EPUB перегенеровується не на кожну правку, а лише при фіналізації/експорті (дорога операція).

### 4.3 Манга

Одиниця — сторінка + бульбашка тексту.

- `PUT /api/edit/manga-text/<slug>/<page_filename>` — body `{bubbles: [{id, text}]}`.
- `POST /api/edit/regenerate-manga-page/<slug>/<page_filename>` — перетайпсетити/переінпейнтити **тільки одну сторінку**. Вимагає рефакторингу: зараз typeset/inpaint — це стадії всередині монолітного `translate_manga.py`; треба виокремити функцію для однієї сторінки (детальніше — розділ 6, це кандидат на `gitnexus-refactoring`).
- `GET /api/edit/manga-queue/<slug>`.

---

## 5. Спільний UI-компонент

- Один переюзабельний `EditPanel` (JS) для text/audio-режиму, аналог для манги в triptych-viewer.
- Word-diff original vs edited (легкий client-side diff, без нових важких залежностей).
- Вкладка "Pending edits" — усі правки книги в одному списку з масовим "Regenerate all" — саме для постгенеративного проходу.

---

## 6. Використання GitNexus (обов'язкові кроки для agy)

1. `gitnexus://repo/kindle-butch-gen/context` — перевірити свіжість індексу перед стартом.
2. `gitnexus://repo/kindle-butch-gen/clusters` — визначити межі функціональних зон: TTS pipeline, manga pipeline, epub merge, web routes.
3. `gitnexus://repo/kindle-butch-gen/process/{name}` — прогнати існуючі execution flow для: синтезу аудіо-чанка, manga-конвеєра (segmentation→OCR→translate→inpaint→typeset), epub-merge — **до** додавання хуків редагування.
4. Перед будь-якою зміною функції — `impact({target, direction: "upstream"})` і звіт про blast radius (це вже правило проєкту, рядок 2213). Якщо HIGH/CRITICAL — попередити користувача.
5. `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` — використати саме для виокремлення "single-page regen" з `translate_manga.py` (це класичний extract-function рефакторинг, найризикованіша частина плану).
6. `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` — при діагностиці розбіжностей chunk_hash / осиротілих wav-файлів під час тестування.
7. Після завершення — `node .gitnexus/run.cjs analyze`, щоб індекс не застарів.

---

## 7. Поетапний план впровадження

| Фаза | Зміст | Ризик |
|---|---|---|
| 1 | `edit_store.py` + схема edits.json | низький |
| 2 | Text/Audio editing (reuse chunk_hash) — найбільша цінність, найменший ризик | середній |
| 3 | Manga bubble-editing + рефакторинг single-page regen | **високий** — обов'язковий impact() |
| 4 | UI: EditPanel, queue-вʼю, approve/regenerate кнопки | низький |
| 5 | Live-режим: банер "Live editing", пріоритезація черги правок над чергою генерації | середній |
| 6 | Export/finalize: EPUB/AZW3/CBZ перегенерація лише при фіналізації + регрес-тести | середній |

---

## 8. Ризики і застереження

- **Не повторювати TASK-17.** Ніякого автоматичного суцільного LLM re-pass — тільки точкові людські правки.
- Зміна перекладеного тексту змінює `chunk_hash` → старий `.wav` стає осиротілим. Потрібна прибиральна задача (orphan cleanup за TTL), інакше диск на телефоні засмічується.
- Manga per-page regen вимагає рефакторингу монолітного `translate_manga.py` — високий blast radius, обов'язково `impact()` перед зміною.
- EPUB-переекспорт "на кожну правку" — дорого; робити лениво, тільки при фіналізації книги.
