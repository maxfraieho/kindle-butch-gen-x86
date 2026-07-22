# TASK-21: Ручне редагування бульбашок манги
### Поверх інфраструктури TASK-20 (dedupe/inpaint/bubble-box/fit_text/quality_flags)

---

## 0. Що вже є (TASK-20) і що додається зараз

Вже задеплоєно: `dedupe_blocks`, `robust_inpaint`, `get_bubble_box`, виправлений `fit_text`/`wrap_text`, `post_render_check` → `quality_flags.json` на сторінку.

Зараз бракує **координат бульбашок у відповіді API** — `/api/preview/manga/<slug>` віддає лише списки файлів (source/cleaned/translated), без bbox і без стабільного ідентифікатора бульбашки. Без цього UI не може намалювати клікабельні зони поверх картинки. Це перша річ, яку треба додати.

---

## 1. Стабільний ID бульбашки — ключова інженерна проблема

`dedupe_blocks` може змінити кількість/порядок блоків між прогонами (перерахунок при регенерації). Якщо просто нумерувати блоки за індексом у списку — правки "втратять" свою бульбашку після regen.

**Рішення:** `bubbles_meta.json` на сторінку, що пишеться після Stage C (`get_bubble_box`):

```json
[
  {
    "id": "page_003_b00",
    "bbox": [120, 340, 410, 520],
    "original_text": "What's that noise?",
    "translated_text": "Що це за шум?",
    "quality_flags": {"overflow_ratio": 1.14, "font_size_px": 12, "at_min_size": true}
  }
]
```

ID призначається сортуванням блоків за `(y1, x1)` — стабільно для однакового набору детекцій. При **регенерації сторінки** нова версія `bubbles_meta.json` зіставляється зі старою через **IoU bbox** (поріг > 0.5) — якщо збіг знайдено, стара правка (якщо була) переноситься на новий `id`; якщо ні — правка позначається `orphaned` і показується окремо в Pending Edits для ручного дозволу конфлікту, а не мовчки губиться.

---

## 2. Нові/змінені endpoints

| Endpoint | Зміна |
|---|---|
| `GET /api/preview/manga-bubbles/<slug>/<page_filename>` | **новий.** Віддає вміст `bubbles_meta.json` для сторінки — bbox + тексти + quality_flags. Основа для overlay в UI. |
| `PUT /api/edit/manga-text/<slug>/<page_filename>` | body `{bubble_id, translated_text}`. Зберігає overlay-правку в `edit_store` (`mode="manga"`), статус `pending`. Не чіпає файли одразу. |
| `POST /api/edit/regenerate-manga-page/<slug>/<page_filename>` | Перепускає сторінку через **весь** пайплайн A→E, але з override-мапою: для бульбашок з pending-правкою пропускається LLM-переклад, використовується `edited_value` напряму. Після завершення — оновлений `bubbles_meta.json`, оновлені quality_flags, edit-статус → `regenerated`. |
| `GET /api/edit/queue/<slug>?mode=manga` | розширення існуючого — тепер повертає і manga-правки, згруповані по сторінках. |

**Важлива зміна пайплайна:** `translate_batch_llm` і виклик у `main()` мають прийняти необов'язковий параметр `overrides: {orig_txt: edited_translation}` — для override-бульбашок переклад береться звідти, а не з LLM. Це зміна сигнатури функції, що викликається з монолітного `main()` — обов'язковий `impact()` перед зміною (див. розділ 5).

---

## 3. UI/UX

### 3.1 Overlay на "translated" панелі

У наявному triptych-переглядачі манги додається клікабельний overlay поверх зображення "translated" (не окрема вкладка — саме overlay, щоб бачити картинку і зони одночасно):

- Кожна бульбашка з `bubbles_meta.json` малюється як напівпрозорий прямокутник (`position: absolute`, координати масштабуються від натурального розміру зображення до відображеного).
- Колірне кодування рамки за `quality_flags`:
  - **червона** — `overflow_ratio > 1.0` або `at_min_size = true` (проблема, яку авто-фікс не подолав повністю);
  - **жовта** — межове значення (напр. `overflow_ratio` 0.9–1.0 або шрифт близько до мінімуму);
  - **без рамки / зелена крапка** — чисто.
- Клік на бульбашку → бічна панель (не модалка на весь екран — на телефоні модалка перекриває картинку, а порівняння важливе).

### 3.2 Бічна панель редагування

Показує:
- crop зображення саме цієї бульбашки (для контексту, без потреби скролити всю сторінку);
- оригінальний OCR-текст (read-only);
- поточний переклад — `<textarea>`, редагована;
- деталі quality_flags людською мовою ("текст виходить за межі бульбашки на 14%", "шрифт на мінімальному розмірі 12px");
- кнопка **Save** → `PUT /api/edit/manga-text/...`, статус локально позначається "Pending regen".

### 3.3 Кнопка "Regenerate page"

З'являється на сторінці, щойно є ≥1 pending-правка. Один клік → `POST /api/edit/regenerate-manga-page/...`. Під час виконання — індикатор прогресу (сторінка обробляється через увесь A→E пайплайн, це не миттєво). Після завершення — картинка й overlay оновлюються без перезавантаження сторінки (polling або просто refetch після відповіді).

### 3.4 Вкладка "Pending Edits" — секція Manga

Розширення вже наявної вкладки (з text/audio):
- Групування: книга → сторінка → бульбашка.
- Для кожної: thumbnail бульбашки, word-diff original translation vs edited, quality_flags, кнопки Approve / Regenerate / Discard.
- **Окремий підрозділ "Auto-flagged (не редаговано вручну)"** — тягне напряму з `quality_flags.json` усі бульбашки, де авто-фікс (TASK-20) не досяг чистого результату, навіть якщо людина ще жодного разу туди не заходила. Це дає рецензенту прямий список "піти й подивитись", а не покладатись на випадкове виявлення при гортанні сторінок.

---

## 4. Чому саме так (обґрунтування ключових рішень)

- **Overlay, не окрема вкладка "Edit"** — щоб бачити картинку в момент редагування; на телефоні перемикання вкладок туди-сюди для звірки з оригіналом дратує.
- **Override-мапа замість "просто перезаписати картинку"** — інакше `regenerate-manga-page` міг би знову прогнати весь текст через LLM-переклад і **втратити** ручну правку, якщо детектор чи інпейнт трохи змінили розкладку. Override має пріоритет над свіжим перекладом конкретно для тих бульбашок, які людина торкнулась.
- **IoU-зіставлення bubble_id при regen, а не жорстка прив'язка до індексу** — бо `dedupe_blocks` (TASK-20) може легітимно змінити кількість блоків між прогонами; без цього кожен regen "губив" би правки.

---

## 5. GitNexus-кроки (обов'язково, з урахуванням досвіду TASK-20)

Нагадування з попередньої сесії: MCP-виклик GitNexus потребує повного handshake — `initialize` → зберегти `Mcp-Session-Id` → `notifications/initialized`, а не голий `tools/call`.

1. `impact({target: "translate_batch_llm", direction: "upstream"})` — сигнатура змінюється (додається `overrides`), перевірити всіх викликачів.
2. `impact({target: "main", direction: "upstream"})` для `translate_manga.py` — оцінити ризик додавання single-page entry point, що приймає override-мапу (потрібен для `regenerate-manga-page`, аналогічно до того, як TASK-20 вже виокремив стадії A-E).
3. `impact({target: "preview_manga", direction: "upstream"})` в `app.py` перед додаванням нового `manga-bubbles` endpoint поруч.
4. Після реалізації — `gitnexus://repo/kindle-butch-gen/process/{name}` зафіксувати новий execution flow (single-page regen з override), `node .gitnexus/run.cjs analyze`.

---

## 6. Тестування

- Unit: IoU-зіставлення bubble_id між двома штучними `bubbles_meta.json` (однакова кількість блоків / блок додався / блок зник) — перевірити, що правки коректно переносяться або позначаються `orphaned`.
- End-to-end на `test_manga.jpg`: відредагувати одну бульбашку, натиснути Regenerate, візуально звірити — переклад саме відредагований, а не переписаний LLM заново; quality_flags для цієї бульбашки оновлені.
- Регрес: переконатись, що сторінки без жодної правки не викликають `regenerate-manga-page` взагалі (нема зайвого навантаження на телефоні).

---

## PROMPT ДЛЯ AGY (готовий до вставки)

```
Реалізуй TASK-21_manga_manual_edit_ui.md повністю, у такому порядку:

1. bubbles_meta.json: додай запис цього файлу в кінці Stage C (get_bubble_box) 
   основного пайплайна translate_manga.py — bbox + original_text + translated_text 
   + quality_flags (уже пораховані в Stage E post_render_check, просто об'єднай 
   у той самий файл). ID бульбашки = сортування за (y1,x1), формат "{page_stem}_b{idx:02d}".

2. Реалізуй IoU-зіставлення (поріг 0.5) для перенесення edit-статусу зі старого 
   bubbles_meta.json на нове при регенерації сторінки. Незіставлені правки — 
   статус "orphaned", НЕ видаляти мовчки.

3. Онови translate_batch_llm (і main()) — додай необов'язковий параметр 
   overrides: dict[str, str]. Перед РЕАЛЬНОЮ зміною сигнатури обов'язково 
   виконай impact({target: "translate_batch_llm", direction: "upstream"}) 
   і повідом мені результат перед тим, як продовжувати.

4. Endpoint GET /api/preview/manga-bubbles/<slug>/<page_filename> — читає 
   bubbles_meta.json, 404 якщо відсутній (сторінка ще не оброблена).

5. Endpoint PUT /api/edit/manga-text/<slug>/<page_filename> — reuse наявного 
   edit_store.py (mode="manga"), body {bubble_id, translated_text}, статус pending.

6. Endpoint POST /api/edit/regenerate-manga-page/<slug>/<page_filename> — 
   збирає overrides з pending-правок цієї сторінки, прогонить сторінку через 
   ПОВНИЙ pipeline A→E з цими overrides, оновлює bubbles_meta.json і 
   quality_flags.json, позначає застосовані правки як "regenerated".

7. UI в stages.html (чи окремому шаблоні прев'ю манги): клікабельний overlay 
   бульбашок поверх "translated" панелі (кольорове кодування за quality_flags), 
   бічна панель редагування (crop + original + textarea + Save), 
   кнопка "Regenerate page" (з'являється лише коли є pending-правки на сторінці).

8. Розширення вкладки Pending Edits: секція Manga (згруповано книга→сторінка→
   бульбашка, word-diff, Approve/Regenerate/Discard) + окремий підрозділ 
   "Auto-flagged" з quality_flags.json для сторінок, яких людина ще не торкалась.

9. Тести: unit на IoU-зіставлення (3 сценарії: без змін / блок додано / блок 
   зник), end-to-end на test_manga.jpg з реальним редагуванням однієї бульбашки 
   і Regenerate.

10. Онови TASKS.md (TASK-21), задокументуй у ai-memory. Задеплой на телефон, 
    live-перевір через https://kindle.exodus.pp.ua, і дай звіт з реальними 
    знайденими багами (якщо будуть) — не рапортуй "готово" без реального 
    end-to-end тесту на живій книзі з manga.
```
