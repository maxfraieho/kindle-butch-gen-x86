# 🦦 Vydra (kindle-butch-gen)

> **Документація українською: [docs/uk/](docs/uk/README.md)** — встановлення,
> користування, бот і підтримка. Сайт: https://vydra.appwrite.network ·
> Бот: [@GetVydraBot](https://t.me/GetVydraBot) ·
> **Правові застереження: [docs/uk/legal.md](docs/uk/legal.md)**


A tool suite to automate EPUB/Markdown translation and generate high-quality Ukrainian audiobooks using premium neural TTS models.

## TTS Engine & Voice Support

This project utilizes the premium **Supertonic 3 text-to-speech synthesis** engine (Flow Matching, ~99M parameters) to generate natural, CD-quality speech on mobile hardware. 

The previous legacy engine (Piper) has been completely deprecated and removed from the codebase.

### Supertonic 3 Highlights:
*   **Acoustic Quality**: High-fidelity mono audio natively processed and downsampled to `22050 Hz` (delivering crisp voice, minimal noise, and 50% smaller file sizes for fast merge).
*   **Hardware Acceleration (GPU/NPU)**: Fully optimized for Android Adreno GPUs using **Android NNAPI Execution Provider** natively in Termux. To ensure maximum stability and prevent driver segfaults, the execution is distributed in a hybrid model layout:
    *   `duration_predictor`, `text_encoder`, and `vector_estimator` are accelerated on **GPU** (NNAPI).
    *   `vocoder` (generative neural vocoder) is processed on **CPU** using 4 threads (NEON/xnnpack optimized).
*   **Flow Matching Convergence**: Fixed at `num_steps = 5` for a **1.6x overall speedup** with identical intonation and tone quality.
*   **Ukrainian Voice Support**: Multi-speaker model containing 10 distinct high-quality voices (`0` to `9`).

### Resiliency & Generation History (Dynamic Cache)
To handle interruptions (app termination, low battery, deep sleep):
1.  **Dynamic Chunk Caching**: Chunks are written to disk and saved to the main cache file `tts_cache_supertonic-3-tts-int8.json` immediately as they are generated. If interrupted, the generator skips all finished chunks and resumes from the exact paragraph.
2.  **NLP Stress Cache**: Stanza/ukrainian_word_stress analysis is cached in `stress_cache_uk.json`. Rerunning the generation does NOT query the PRoot Ubuntu NLP container for existing paragraphs, shortening restart time from 10 minutes to 1 second.

### How to Configure Voice in `config.json`

Set `tts_voice` to `"supertonic3"`. 

```json
{
  "target_lang": "uk",
  "tts_voice": "supertonic3",
  "tts_speaker_id": 2,
  "tts_speed": 1.0
}
```

## Встановлення та швидкий старт

Інструментарій `kindle-butch-gen` розроблений для запуску в середовищі **Termux (Android)** з інтеграцією Ubuntu-контейнера через `proot-distro` для виконання важких лінгвістичних завдань (наприклад, пакетного наголошувача `ukrainian-word-stress`).

### 1. Передумови та встановлення

Для запуску перекладу та наголошувача необхідно, щоб у вашому Ubuntu-контейнері (`proot-distro login ubuntu`) були встановлені:
- `python3` з бібліотекою `ukrainian-word-stress`
- `ffmpeg` та `calibre` (для конвертації книг)

### 2. Керування книгами через CLI (`kbg.sh`)

Усі підкоманди запускаються через головний скрипт керування:

*   **Додати нову книгу до обробки**:
    ```bash
    ./kbg.sh add --slug <унікальний_слаг> --pdf </шлях/до/файлу.pdf> --title "<Назва книги>" --authors "<Автори>" --lang <uk|ru>
    ```

*   **Запустити повний пайплайн**:
    ```bash
    ./kbg.sh run <slug>
    ```
    Команда автоматично виконає:
    1. Екстракцію сторінок PDF у Markdown за допомогою Marker.
    2. Поабзацний переклад (за наявності локального сервера перекладу).
    3. Збірку книжкових форматів EPUB та AZW3.
    4. Синтез аудіокниги в MP3 через Supertonic 3 (із застосуванням NNAPI GPU-прискорення).

*   **Переглянути статус та прогрес**:
    ```bash
    ./kbg.sh status <slug>
    ```

*   **Запустити веб-дашборд**:
    ```bash
    ./kbg.sh serve --port 5000
    ```

## Веб-інтерфейс (Дашборд)

Дашборд на порту `5000` повністю інтегрований із Supertonic 3:
*   Дозволяє обирати дикторів (0-9) та повзунки швидкості мовлення.
*   Відображає точний прогрес генерації у реальному часі.
*   Надає можливість миттєвого прослуховування аудіо-прев'ю перед повним запуском озвучення книги.
*   Дозволяє завантажувати готові файли (`.epub`, `.azw3`, `.mp3`) в один клік.

## Швидкий старт на своєму Android (Vydra 🦦)

1. Встановіть [Termux з F-Droid](https://f-droid.org/packages/com.termux/) (не з Play Market).
2. Встановіть [Termux:Boot](https://f-droid.org/packages/com.termux.boot/) і відкрийте його один раз.
3. У Termux виконайте:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/maxfraieho/kindle-butch-gen/master/deploy.sh) -a
```

Скрипт спершу проганяє діагностику вимог (aarch64, 6+GB RAM, 15+GB місця, мережа),
потім розгортає все сам. Тримайте екран увімкненим до «Deployment complete».
Супровід і реєстрація: [@GetVydraBot](https://t.me/GetVydraBot) (команда /install).

## Відмова від відповідальності (Disclaimer)

Vydra — програмне забезпечення загального призначення, розроблене виключно
для локальної обробки цифрових книг та документів, що перебувають у законній
власності користувача (твори з Public Domain, легально придбані DRM-free
файли або власні авторські матеріали).

ПЗ надається на умовах ліцензії «як є» (AS IS), без будь-яких гарантій.
Розробники не заохочують, не підтримують і не несуть відповідальності за
використання цього інструменту з метою порушення авторських прав третіх
осіб. Vydra не містить функцій обходу технічних засобів захисту (DRM
circumvention) та не надає доступу до сторонніх каталогів контенту.
Згадки форматів чи пристроїв (Kindle, AZW3 тощо) є виключно номінативним
використанням для опису технічної сумісності і не означають афіліації з
власниками відповідних торговельних марок.

Проєкт інтегрує ШІ-модель Google Gemma: використання преміум-функцій
підпорядковане [Gemma Terms of Use](https://ai.google.dev/gemma/terms) та
[Prohibited Use Policy](https://ai.google.dev/gemma/prohibited_use_policy).
Перелік сторонніх компонентів та їхніх ліцензій: [NOTICE.md](NOTICE.md).
