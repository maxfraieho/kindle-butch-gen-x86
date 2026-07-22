// Intercept fetch to prevent credential errors when document URL has basic auth details
        (function() {
            const originalFetch = window.fetch;
            window.fetch = function(input, init) {
                if (typeof input === 'string' && input.startsWith('/')) {
                    input = window.location.origin + input;
                }
                return originalFetch(input, init);
            };
        })();

        let currentLogsSlug = null;
        let logsInterval = null;
        const lastLogsCache = {};

        function showUploadStatus(msg, type) {
            const statusDiv = document.getElementById('uploadStatus');
            statusDiv.className = 'upload-status ' + type;
            statusDiv.innerHTML = msg;
            statusDiv.style.display = 'block';
        }

        function clearUploadStatus() {
            const statusDiv = document.getElementById('uploadStatus');
            statusDiv.style.display = 'none';
        }

        // Auto-detect metadata on file selection
        function titleFromFilename(name) {
            // "My Public Domain Comic v01 (Digital).cbz"
            // -> "My Public Domain Comic v01" - strip extension,
            // bracketed release-scanner junk, and underscores.
            let t = name.replace(/\.[^.]+$/, '');
            t = t.replace(/[\[(][^\])]*[\])]/g, ' ');
            t = t.replace(/_/g, ' ').replace(/\s{2,}/g, ' ').trim();
            return t;
        }

        document.getElementById('file_upload').addEventListener('change', async function(e) {
            // Simple mode (TASK-68): picking a file fills the form by
            // itself - a non-technical user should only need Add after this.
            const f0 = e.target.files && e.target.files[0];
            if (f0) {
                const titleEl = document.getElementById('title');
                if (titleEl && !titleEl.value.trim()) {
                    titleEl.value = titleFromFilename(f0.name);
                    document.getElementById('slug').value = slugify(titleEl.value);
                }
                const isComic = /\.(cbz|cbr|cb7|zip|rar)$/i.test(f0.name);
                const mangaEl = document.getElementById('is_manga');
                if (mangaEl && isComic && !mangaEl.checked) {
                    mangaEl.checked = true;
                    updateSourcePathPlaceholder(true);
                }
            }
            if (this.files.length === 0) return;
            
            const file = this.files[0];
            showUploadStatus('Parsing file metadata...', 'info');
            
            const formData = new FormData();
            formData.append('file', file);
            
            try {
                const response = await fetch('/api/parse-metadata', {
                    method: 'POST',
                    body: formData
                });
                const res = await response.json();
                if (response.ok) {
                    if (res.detected_title) {
                        document.getElementById('title').value = res.detected_title;
                    }
                    // File metadata (e.g. EPUB) can carry its own slug -
                    // prefer it; otherwise derive from whatever title we
                    // ended up with (detected or not).
                    document.getElementById('slug').value = res.detected_slug
                        || slugify(document.getElementById('title').value);
                    if (res.detected_authors) {
                        document.getElementById('authors').value = res.detected_authors;
                    }
                    if (res.detected_lang && res.detected_lang !== 'auto') {
                        document.getElementById('source_lang').value = res.detected_lang;
                    } else {
                        document.getElementById('source_lang').value = 'auto';
                    }
                    showUploadStatus('Metadata detected and pre-filled successfully!', 'success');
                } else {
                    showUploadStatus('Failed to parse metadata: ' + res.message, 'error');
                }
            } catch (err) {
                showUploadStatus('Metadata detection failed: ' + err.message, 'error');
            }
        });

        document.getElementById('addBookForm').addEventListener('submit', async function(e) {
            e.preventDefault();
            const title = document.getElementById('title').value.trim();
            let baseSlug = document.getElementById('slug').value.trim() || slugify(title);
            const authors = document.getElementById('authors').value.trim() || 'Невідомий автор';
            const lang = document.getElementById('lang').value;
            const source_lang = document.getElementById('source_lang').value;
            const fileInput = document.getElementById('file_upload');
            const pdf_path = document.getElementById('pdf_path').value.trim();
            const is_manga = document.getElementById('is_manga').checked;
            const isUpload = fileInput.files.length > 0;

            if (!isUpload && !pdf_path) {
                showUploadStatus('Please upload a file or specify a local PDF path.', 'error');
                return;
            }

            const submitBtn = document.getElementById('addBookSubmit');
            submitBtn.disabled = true;
            submitBtn.innerText = 'Adding Book...';
            showUploadStatus(isUpload
                ? 'Uploading book file and extracting content (this may take a few seconds)...'
                : 'Adding local book on system...', 'info');

            // /api/add (local-path entries) rejects source_lang=auto with a
            // 400 - there's no file content to sniff a language from, unlike
            // an uploaded EPUB. Found live: this silently dead-ended a real
            // manga add (son's phone) with no clear feedback path noticed.
            // Auto-detect is the field's DEFAULT option, so a first-time
            // user typing a manual path never has a reason to change it.
            let effectiveSourceLang = source_lang;
            if (!isUpload && source_lang === 'auto') {
                effectiveSourceLang = 'en';
                showUploadStatus('Auto-detect works only for uploaded files - using English as the source language for this local path (change "Source Language" above and retry if that\'s wrong).', 'info');
            }

            async function attempt(currentSlug) {
                if (isUpload) {
                    const formData = new FormData();
                    formData.append('slug', currentSlug);
                    formData.append('title', title);
                    formData.append('authors', authors);
                    formData.append('lang', lang);
                    formData.append('source_lang', source_lang);
                    formData.append('file', fileInput.files[0]);
                    formData.append('is_manga', is_manga ? 'true' : 'false');
                    const response = await fetch('/api/upload', { method: 'POST', body: formData });
                    return { response, res: await response.json() };
                }
                const response = await fetch('/api/add', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ slug: currentSlug, pdf_path, title, authors, lang, source_lang: effectiveSourceLang, is_manga })
                });
                return { response, res: await response.json() };
            }

            // Slug is now invisible to the user, so a collision (e.g. two
            // books that transliterate to the same title) must resolve
            // itself instead of surfacing a "slug already exists" error
            // about a field they never saw.
            let currentSlug = baseSlug;
            for (let i = 0; i < 6; i++) {
                let result;
                try {
                    result = await attempt(currentSlug);
                } catch (err) {
                    showUploadStatus((isUpload ? 'Upload' : 'Request') + ' failed: ' + err.message, 'error');
                    break;
                }
                if (result.response.ok) {
                    showUploadStatus('✅ Книгу додано!', 'success');
                    document.getElementById('addBookForm').reset();
                    fetchBooks();
                    const addedSlug = currentSlug;
                    setTimeout(() => {
                        closeAddBookModal();
                        // Simple mode: the natural next step IS the next
                        // question - don't make the user hunt for the
                        // start button on the card.
                        if (confirm('Книгу додано! Почати переклад зараз?')) {
                            runConversion(addedSlug);
                        }
                    }, 600);
                    break;
                }
                if (result.response.status === 409 && i < 5) {
                    currentSlug = baseSlug + '-' + (i + 2);
                    continue;
                }
                showUploadStatus('Error: ' + result.res.message, 'error');
                break;
            }
            submitBtn.disabled = false;
            submitBtn.innerText = 'Add Book';
        });

        function handleVoiceChange(slug, voiceValue) {
            const speakerSelect = document.getElementById(`speaker-${slug}`);
            if (!speakerSelect) return;
            
            if (voiceValue === 'ukrainian_tts') {
                speakerSelect.innerHTML = `
                    <option value="0">Lada [0]</option>
                    <option value="1">Mykyta [1]</option>
                    <option value="2" selected>Tetiana [2]</option>
                `;
            } else {
                speakerSelect.innerHTML = `<option value="0" selected>Default [0]</option>`;
            }
        }

        function handleEngineChange(slug, engineValue, targetLang) {
            const speakerSelect = document.getElementById(`speaker-${slug}`);
            if (!speakerSelect) return;
            
            if (engineValue === 'styletts2') {
                if (targetLang !== 'uk') {
                    alert(`StyleTTS2 supports only Ukrainian language. This book's language is '${targetLang}'. Switching back to Supertonic 3.`);
                    document.getElementById(`engine-${slug}`).value = 'supertonic3';
                    handleEngineChange(slug, 'supertonic3', targetLang);
                    return;
                }
                speakerSelect.innerHTML = `<option value="0" selected>Single Speaker (Filatov)</option>`;
                speakerSelect.disabled = true;
            } else if (engineValue === 'supertonic3') {
                speakerSelect.disabled = false;
                speakerSelect.innerHTML = Array.from({length: 10}, (_, i) => 
                    `<option value="${i}">Speaker [${i}]</option>`
                ).join('');
            }

            const noiseScaleInput = document.getElementById(`noise-scale-${slug}`);
            const noiseWInput = document.getElementById(`noise-w-${slug}`);
            
            const noiseScaleGroup = noiseScaleInput ? noiseScaleInput.closest('.slider-group') : null;
            const noiseWGroup = noiseWInput ? noiseWInput.closest('.slider-group') : null;
            
            if (engineValue === 'supertonic3') {
                if (noiseScaleInput) {
                    noiseScaleInput.disabled = true;
                    if (noiseScaleGroup) {
                        noiseScaleGroup.style.opacity = '0.5';
                        noiseScaleGroup.style.pointerEvents = 'none';
                    }
                }
                if (noiseWInput) {
                    noiseWInput.disabled = true;
                    if (noiseWGroup) {
                        noiseWGroup.style.opacity = '0.5';
                        noiseWGroup.style.pointerEvents = 'none';
                    }
                }
            } else {
                if (noiseScaleInput) {
                    noiseScaleInput.disabled = false;
                    if (noiseScaleGroup) {
                        noiseScaleGroup.style.opacity = '1';
                        noiseScaleGroup.style.pointerEvents = 'auto';
                    }
                }
                if (noiseWInput) {
                    noiseWInput.disabled = false;
                    if (noiseWGroup) {
                        noiseWGroup.style.opacity = '1';
                        noiseWGroup.style.pointerEvents = 'auto';
                    }
                }
            }
        }

        let isFirstLoad = true;

        // TASK-51: support-banner status card. The Telegram bot is the
        // primary control (donation options live there); the local button
        // is the promised one-step direct switch.
        let supportLocalDisabled = false;
        async function refreshSupportCard() {
            try {
                const r = await fetch('/api/support/profile', { cache: 'no-store' });
                if (!r.ok) return;
                const d = await r.json();
                if (!d.config_enabled && !d.effective_disabled) return; // feature off entirely
                supportLocalDisabled = d.local_disabled;
                const card = document.getElementById('supportCard');
                const status = document.getElementById('supportStatus');
                const btn = document.getElementById('supportLocalBtn');
                card.style.display = '';
                if (d.effective_disabled) {
                    status.textContent = d.remote_disabled
                        ? 'вимкнено (через Telegram-бот)'
                        : 'вимкнено (локально)';
                } else {
                    status.textContent = 'увімкнено — дякуємо, що лишаєте 💙💛';
                }
                btn.textContent = d.local_disabled
                    ? 'Увімкнути локально' : 'Вимкнути локально';
                const prem = document.getElementById('premiumStatus');
                if (d.entitlements && d.entitlements.length) {
                    prem.style.display = '';
                    prem.textContent = '💎 Преміум активовано: Cast Registry, Агент-редактор, MQM.';
                    if (localStorage.getItem('vydra_premium_onboarded') !== '1') {
                        openPremiumOnboarding();
                    }
                } else { prem.style.display = 'none'; }
                const linkRow = document.getElementById('telegramLinkRow');
                if (linkRow) linkRow.style.display = d.telegram_id ? 'none' : 'flex';
                const devStatus = document.getElementById('deviceStatus');
                if (devStatus) { devStatus.style.display = 'none'; }
            } catch (e) { /* card simply stays hidden */ }
        }

        async function linkTelegramId() {
            const input = document.getElementById('telegramLinkInput');
            const tgId = (input.value || '').trim();
            if (!/^\d+$/.test(tgId)) {
                alert("Telegram ID має складатись лише з цифр - скопіюйте його з повідомлення бота після /start.");
                return;
            }
            try {
                const r = await fetch('/api/support/link-telegram', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ telegram_id: tgId }),
                });
                const d = await r.json();
                if (!r.ok) { alert(d.message || 'Не вдалося прив\'язати.'); return; }
                input.value = '';
                refreshSupportCard();
            } catch (e) { alert('Помилка мережі, спробуйте ще раз.'); }
        }

        async function openPremiumOnboarding() {
            const modal = document.getElementById('premiumOnboardModal');
            if (!modal) return;
            modal.classList.add('active');
            refreshPremiumModelStatus();
        }
        function closePremiumOnboarding(remember) {
            if (remember) localStorage.setItem('vydra_premium_onboarded', '1');
            const modal = document.getElementById('premiumOnboardModal');
            if (modal) modal.classList.remove('active');
            if (window._premStatusTimer) { clearInterval(window._premStatusTimer); window._premStatusTimer = null; }
        }
        async function refreshPremiumModelStatus() {
            const el = document.getElementById('premModelStatus');
            const btn = document.getElementById('premDownloadBtn');
            if (!el) return;
            try {
                const r = await fetch('/api/premium/model-status', { cache: 'no-store' });
                const s = await r.json();
                const doneBytes = (s.gemma.bytes || 0) + (s.mmproj.bytes || 0);
                if (s.gemma.ready && s.mmproj.ready) {
                    el.innerHTML = '✅ Vision-модель встановлена — агент-редактор готовий до роботи.';
                    if (btn) btn.style.display = 'none';
                    if (window._premStatusTimer) { clearInterval(window._premStatusTimer); window._premStatusTimer = null; }
                } else if (s.downloading) {
                    const pct = Math.min(99, Math.round(doneBytes / s.total_expected_bytes * 100));
                    el.innerHTML = `⏳ Завантаження триває: ~${pct}% (${(doneBytes / 1e9).toFixed(2)} ГБ). Можна закрити діалог — воно продовжиться у фоні.`;
                    if (btn) btn.style.display = 'none';
                    if (!window._premStatusTimer) window._premStatusTimer = setInterval(refreshPremiumModelStatus, 5000);
                } else {
                    el.innerHTML = doneBytes > 0
                        ? `⏸️ Завантажено частково (${(doneBytes / 1e9).toFixed(2)} ГБ) — натисніть, щоб продовжити з того ж місця.`
                        : '📦 Vision-модель (~3.5 ГБ) ще не завантажена. Потрібна для агент-редактора манґи (Cast Registry працює без неї).';
                    if (btn) btn.style.display = '';
                }
            } catch (e) {
                el.textContent = 'Не вдалося перевірити стан моделей.';
            }
        }
        async function startPremiumModelDownload() {
            const btn = document.getElementById('premDownloadBtn');
            if (btn) btn.disabled = true;
            try {
                const accepted = document.getElementById('premGemmaTerms') && document.getElementById('premGemmaTerms').checked;
                const r = await fetch('/api/premium/download-models', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ gemma_terms_accepted: accepted }) });
                const d = await r.json();
                if (!r.ok) throw new Error(d.message);
                refreshPremiumModelStatus();
            } catch (e) {
                alert('Не вдалося запустити завантаження: ' + e.message);
            } finally {
                if (btn) btn.disabled = false;
            }
        }
        async function toggleLocalBanner() {
            try {
                const r = await fetch('/api/support/local-optout', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ disabled: !supportLocalDisabled }),
                });
                if (r.ok) refreshSupportCard();
            } catch (e) { alert('Не вдалося перемкнути: ' + e); }
        }
        refreshSupportCard();

        // TASK-46: one-button service update for non-specialists. POST
        // /api/update checks the remote; "updating" means the server is
        // about to restart itself, so we poll until it's back and reload.
        async function runSelfUpdate() {
            window.closeHeaderMenu();
            const activeToast = showToast('⏳ Перевіряємо наявність оновлень Vydra...', 'info', 0);
            try {
                const r = await fetch('/api/update', { method: 'POST' });
                const d = await r.json();
                activeToast.remove();
                if (d.status === 'up_to_date') {
                    showToast(`✅ Vydra вже має найновішу версію (${d.version || 'актуальна'})`, 'success', 5000);
                } else if (d.status === 'busy') {
                    showToast(`⚠️ ${d.message}`, 'warning', 6000);
                } else if (d.status === 'updating') {
                    showToast(`🔄 Оновлення запущено (${d.behind} нових комітів). Перезапускаємо сервіс...`, 'info', 20000);
                    const poll = setInterval(async () => {
                        try {
                            const c = await fetch('/api/settings', { cache: 'no-store' });
                            if (c.ok) { clearInterval(poll); location.reload(); }
                        } catch (e) { /* keep polling */ }
                    }, 3000);
                } else {
                    showToast(`❌ Помилка оновлення: ${d.message || r.status}`, 'error', 7000);
                }
            } catch (e) {
                activeToast.remove();
                showToast(`❌ Не вдалося виконяти оновлення: ${e}`, 'error', 7000);
            }
        }

        function renderPrimaryAction(book) {
            const files = book.output_files || [];
            const pick = (exts) => { for (const e of exts) { const f = files.find(x => x.endsWith(e)); if (f) return f; } return null; };
            const primaryFile = pick(['.azw3', '.epub', '.cbz', '.mp3', '.md']);
            if (book.is_running) {
                return `<button onclick="stopConversion('${book.slug}')" class="btn btn-danger" style="flex:1; min-width:140px; font-size:0.9rem; padding:0.65rem 1rem; border-radius:8px;">⏸ Зупинити переклад</button>`;
            } else if (primaryFile) {
                return `
                    <a href="/view/${book.slug}" class="btn btn-primary" style="flex:1; min-width:130px; font-size:0.9rem; padding:0.65rem 1rem; border-radius:8px; text-decoration:none; text-align:center; display:inline-flex; align-items:center; justify-content:center; gap:0.4rem;">📖 Читати / Редагувати</a>
                    <a class="btn btn-success" style="flex:1; min-width:130px; font-size:0.9rem; padding:0.65rem 1rem; border-radius:8px; text-decoration:none; text-align:center; display:inline-flex; align-items:center; justify-content:center; gap:0.4rem;" href="/api/download/${book.slug}/${encodeURIComponent(primaryFile)}" target="_blank">⬇️ Завантажити (${primaryFile.split('.').pop().toUpperCase()})</a>
                `;
            } else {
                return `<button onclick="runConversion('${book.slug}')" class="btn btn-success" style="flex:1; min-width:160px; font-size:0.9rem; padding:0.65rem 1rem; border-radius:8px;">▶️ Почати переклад</button>`;
            }
        }

        async function fetchBooks() {
            try {
                const openDetails = {};
                const openAdvanced = {};
                const formValues = {};
                const activeId = document.activeElement ? document.activeElement.id : null;
                let selStart = null, selEnd = null;
                if (document.activeElement && document.activeElement.selectionStart !== undefined) {
                    selStart = document.activeElement.selectionStart;
                    selEnd = document.activeElement.selectionEnd;
                }

                const cards = document.querySelectorAll('.book-card');
                cards.forEach(card => {
                    const slugEl = card.querySelector('code');
                    if (slugEl) {
                        const slug = slugEl.innerText.trim();
                        
                        const detailsEl = document.getElementById(`details-${slug}`);
                        const engineEl = document.getElementById(`engine-${slug}`);
                        const speakerEl = document.getElementById(`speaker-${slug}`);
                        const speedEl = document.getElementById(`speed-${slug}`);
                        const noiseEl = document.getElementById(`noise-scale-${slug}`);
                        const noiseWEl = document.getElementById(`noise-w-${slug}`);
                        const previewEl = document.getElementById(`preview-text-${slug}`);
                        
                        const cleanEl = document.getElementById(`clean-${slug}`);
                        const translateEl = document.getElementById(`translate-${slug}`);
                        const ebookEl = document.getElementById(`ebook-${slug}`);
                        const audioEl = document.getElementById(`audio-${slug}`);
                        const mangaResEl = document.getElementById(`manga-res-${slug}`);

                        if (detailsEl) {
                            openDetails[slug] = detailsEl.open;
                        }
                        const advEl = document.getElementById(`adv-${slug}`);
                        if (advEl) {
                            openAdvanced[slug] = advEl.open;
                        }

                        formValues[slug] = {
                            engine: engineEl ? engineEl.value : null,
                            speaker: speakerEl ? speakerEl.value : null,
                            speed: speedEl ? speedEl.value : null,
                            noise_scale: noiseEl ? noiseEl.value : null,
                            noise_w: noiseWEl ? noiseWEl.value : null,
                            preview_text: previewEl ? previewEl.value : '',
                            clean: cleanEl ? cleanEl.checked : null,
                            translate: translateEl ? translateEl.checked : null,
                            ebook: ebookEl ? ebookEl.checked : null,
                            audio: audioEl ? audioEl.checked : null,
                            manga_res: mangaResEl ? mangaResEl.value : null
                        };
                    }
                });

                const response = await fetch('/api/books');
                if (response.status === 401) {
                    window.location.href = '/login';
                    return;
                }
                const books = await response.json();
                const container = document.getElementById('booksList');

                if (!Array.isArray(books)) {
                    throw new Error('Unexpected response: ' + JSON.stringify(books));
                }

                if (books.length === 0) {
                    container.innerHTML = '<p style="color: var(--text-secondary); text-align: center;">Поки що немає книг — натисніть «➕ Додати книгу», щоб почати 🦦</p>';
                    return;
                }

                // Не перемальовувати картки, поки користувач із ними
                // працює: інакше 5-секундний авто-рефреш закриває
                // «Додаткові налаштування», скидає чекбокси й скролить
                // термінал під руками. Замість повного innerHTML робимо
                // ЛЕГКЕ точкове оновлення прогресу і виходимо.
                const userBusy = !isFirstLoad && container.children.length > 0 && (
                    Array.from(container.querySelectorAll('details[id^="adv-"]')).some(d => d.open) ||
                    (document.activeElement && container.contains(document.activeElement)) ||
                    (currentLogsSlug && document.getElementById(`terminal-${currentLogsSlug}`)?.style.display === 'block')
                );
                if (userBusy) {
                    books.forEach(book => {
                        const badge = document.getElementById(`badge-${book.slug}`);
                        if (badge) {
                            badge.className = `badge ${book.is_running ? 'badge-running' : 'badge-idle'}`;
                            badge.textContent = book.is_running ? 'Працює' : 'Очікує';
                        }
                        const pf = document.getElementById(`progressfill-${book.slug}`);
                        const pp = document.getElementById(`progresspct-${book.slug}`);
                        const ps = document.getElementById(`progressstage-${book.slug}`);
                        // Real bug, reported live: light-update only ever
                        // touched the bar/percent, never the "Перекладаю
                        // сторінки: X із Y" text - so starting a fresh run
                        // right after a previous one finished (194/194)
                        // left that text frozen at the OLD run's final
                        // count while the percent correctly ticked back
                        // down to ~0%, showing a nonsensical "194 із 194 /
                        // 0.5%" combination.
                        if (book.progress.is_manga) {
                            const pct = book.progress.manga_percent || 0;
                            if (pf) pf.style.width = pct + '%';
                            if (pp) pp.textContent = pct + '%';
                            if (ps) ps.innerHTML = `Перекладаю сторінки: <strong>${book.progress.manga_pages_completed || 0} із ${book.progress.manga_total_pages || 0}</strong>`;
                        } else {
                            const op = Math.round(book.progress.overall_percent !== undefined ? book.progress.overall_percent : (((book.progress.marker_percent||0)+(book.progress.translation_percent||0)+(book.progress.stress_percent||0)+(book.progress.tts_percent||0))/4));
                            if (pf) pf.style.width = op + '%';
                            if (pp) pp.textContent = op + '%';
                        }
                        // Головна дія (Почати/Зупинити/Завантажити) мусить
                        // теж стежити за is_running, інакше при легкому
                        // оновленні лишається застарілою кнопкою "Почати"
                        // навіть коли переклад уже йде (реальний баг: Q
                        // отримав "почати", хоч конверсія була на 1.5%).
                        const primaryEl = document.getElementById(`primary-${book.slug}`);
                        if (primaryEl) {
                            const newHtml = renderPrimaryAction(book);
                            if (primaryEl.innerHTML.trim() !== newHtml.trim()) {
                                primaryEl.innerHTML = newHtml;
                            }
                        }
                    });
                    if (currentLogsSlug) pollLogs();
                    isFirstLoad = false;
                    return;
                }

                container.innerHTML = books.map((book, index) => {
                    const badgeClass = book.is_running ? 'badge-running' : 'badge-idle';
                    const badgeText = book.is_running ? 'Працює' : 'Очікує';
                    const detailsOpenAttr = openDetails[book.slug] ? 'open' : '';
                    const advOpenAttr = openAdvanced[book.slug] ? 'open' : '';
                    
                    let speakerOptions = '';
                    let speakerDisabled = '';
                    if (book.tts_engine === 'styletts2') {
                        speakerOptions = `<option value="0" selected>Single Speaker (Filatov)</option>`;
                        speakerDisabled = 'disabled';
                    } else {
                        speakerOptions = Array.from({length: 10}, (_, i) => 
                            `<option value="${i}" ${book.tts_speaker_id === i ? 'selected' : ''}>Speaker [${i}]</option>`
                        ).join('');
                    }
                    
                    const marker_p = book.progress.marker_percent || 0;
                    const trans_p = book.progress.translation_percent || 0;
                    const stress_p = book.progress.stress_percent || 0;
                    const tts_p = book.progress.tts_percent || 0;

                    let activeStage = "Очікує";
                    let activePercent = 0;

                    if (tts_p > 0 && tts_p < 100) {
                        activeStage = "Озвучую";
                        activePercent = tts_p;
                    } else if (stress_p > 0 && stress_p < 100) {
                        activeStage = "Розставляю наголоси";
                        activePercent = stress_p;
                    } else if (trans_p > 0 && trans_p < 100) {
                        activeStage = "Перекладаю";
                        activePercent = trans_p;
                    } else if (marker_p > 0 && marker_p < 100) {
                        activeStage = "Розпізнаю текст";
                        activePercent = marker_p;
                    } else if (tts_p === 100) {
                        activeStage = "Готово ✅";
                        activePercent = 100;
                    } else if (book.is_running) {
                        if (marker_p < 100) { activeStage = "Розпізнаю текст"; activePercent = marker_p; }
                        else if (trans_p < 100) { activeStage = "Перекладаю"; activePercent = trans_p; }
                        else if (stress_p < 100) { activeStage = "Розставляю наголоси"; activePercent = stress_p; }
                        else if (tts_p < 100) { activeStage = "Озвучую"; activePercent = tts_p; }
                    }

                    const overall_p = Math.round(book.progress.overall_percent !== undefined ? book.progress.overall_percent : ((marker_p + trans_p + stress_p + tts_p) / 4));
                    
                    let progressHtml = '';
                    let optionsHtml = '';
                    if (book.progress.is_manga) {
                        const comp = book.progress.manga_pages_completed || 0;
                        const tot = book.progress.manga_total_pages || 0;
                        const pct = book.progress.manga_percent || 0;

                        let mangaStepsHtml = '';
                        mangaStepsHtml += `<span class="step-node ${pct === 100 ? 'completed' : (book.is_running ? 'active' : '')}" title="Детекція бульбашок та OCR">Детекція</span>`;
                        
                        if (book.enable_cast_registry) {
                            mangaStepsHtml += `<span class="step-divider"></span>`;
                            mangaStepsHtml += `<span class="step-node ${pct === 100 ? 'completed' : (book.is_running ? 'active' : '')}" title="Cast Registry (персонажі)">Cast</span>`;
                        }
                        if (book.enable_bubble_tone) {
                            mangaStepsHtml += `<span class="step-divider"></span>`;
                            mangaStepsHtml += `<span class="step-node ${pct === 100 ? 'completed' : (book.is_running ? 'active' : '')}" title="Bubble Tone (тон реплік)">Тон</span>`;
                        }
                        
                        mangaStepsHtml += `<span class="step-divider"></span>`;
                        mangaStepsHtml += `<span class="step-node ${pct === 100 ? 'completed' : (book.is_running ? 'active' : '')}" title="Переклад тексту">Переклад</span>`;
                        
                        if (book.enable_agent_editor) {
                            mangaStepsHtml += `<span class="step-divider"></span>`;
                            mangaStepsHtml += `<span class="step-node ${pct === 100 ? 'completed' : (book.is_running ? 'active' : '')}" title="Агент-редактор">Агент</span>`;
                        }
                        
                        mangaStepsHtml += `<span class="step-divider"></span>`;
                        mangaStepsHtml += `<span class="step-node ${pct === 100 ? 'completed' : (book.is_running ? 'active' : '')}" title="Типографіка та рендер сторінок">Типографіка</span>`;

                        progressHtml = `
                            <div class="pipeline-progress" style="margin: 0.8rem 0;">
                                <div class="pipeline-progress-header" style="margin-bottom: 0.4rem; display: flex; justify-content: space-between; font-size: 0.85rem;">
                                    <span class="pipeline-active-stage" id="progressstage-${book.slug}">Перекладаю сторінки: <strong>${comp} із ${tot}</strong></span>
                                    <span class="pipeline-percent" id="progresspct-${book.slug}" style="font-weight: 600; color: var(--primary);">${pct}%</span>
                                </div>
                                <div class="progress-bar-bg" style="height: 8px; background: rgba(255,255,255,0.08); border-radius: 4px; overflow: hidden;">
                                    <div class="progress-bar-fill fill-translation" id="progressfill-${book.slug}" style="width: ${pct}%; height: 100%; background: var(--primary); border-radius: 4px; transition: width 0.3s ease;"></div>
                                </div>
                            </div>
                        `;
                        optionsHtml = `
                            <div class="options-group" id="opts-${book.slug}">
                                <label class="option-checkbox"><input type="checkbox" id="clean-${book.slug}"> Повторно очистити сторінки</label>
                                <label class="option-checkbox"><input type="checkbox" id="translate-${book.slug}" checked> Переклад</label>
                                <label class="option-checkbox"><input type="checkbox" id="ebook-${book.slug}" checked> Книга для Kindle (AZW3)</label>
                            </div>
                            
                            <div style="font-size: 0.8rem; color: var(--text-secondary); padding: 0.5rem 0.75rem; background: rgba(255,255,255,0.02); border-radius: 6px; border: 1px solid rgba(255,255,255,0.04);">
                                📖 Режим манґи: сторінки пакуються у CBZ і збираються в AZW3 для Kindle (читання справа наліво).
                                Роздільність і решта налаштувань — у ⚙️ Налаштування книги.
                            </div>
                        `;
                    } else {
                        let stepsHtml = '';
                        stepsHtml += `<span class="step-node ${marker_p === 100 ? 'completed' : (book.is_running && activeStage === 'Розпізнаю текст' ? 'active' : '')}" title="Розпізнавання тексту">Текст</span>`;
                        stepsHtml += `<span class="step-divider"></span>`;
                        stepsHtml += `<span class="step-node ${trans_p === 100 ? 'completed' : (book.is_running && activeStage === 'Перекладаю' ? 'active' : '')}" title="Переклад">Переклад</span>`;
                        
                        if (book.generate_audiobook !== false) {
                            stepsHtml += `<span class="step-divider"></span>`;
                            stepsHtml += `<span class="step-node ${stress_p === 100 ? 'completed' : (book.is_running && activeStage === 'Розставляю наголоси' ? 'active' : '')}" title="Наголоси для озвучення">Наголоси</span>`;
                            stepsHtml += `<span class="step-divider"></span>`;
                            stepsHtml += `<span class="step-node ${tts_p === 100 ? 'completed' : (book.is_running && activeStage === 'Озвучую' ? 'active' : '')}" title="Синтез аудіо">Звук</span>`;
                        }

                        progressHtml = `
                            <div class="pipeline-progress" style="margin: 0.8rem 0;">
                                <div class="pipeline-progress-header" style="margin-bottom: 0.4rem; display: flex; justify-content: space-between; font-size: 0.85rem;">
                                    <span class="pipeline-active-stage" id="progressstage-${book.slug}"><strong>${activeStage}</strong>${activePercent > 0 && activePercent < 100 ? ` (${activePercent}%)` : ''}</span>
                                    <span class="pipeline-percent" id="progresspct-${book.slug}" style="font-weight: 600; color: var(--primary);">${overall_p}%</span>
                                </div>
                                <div class="progress-bar-bg" style="height: 8px; background: rgba(255,255,255,0.08); border-radius: 4px; overflow: hidden;">
                                    <div class="progress-bar-fill pipeline-gradient-fill" id="progressfill-${book.slug}" style="width: ${overall_p}%; height: 100%; background: var(--primary); border-radius: 4px; transition: width 0.3s ease;"></div>
                                </div>
                            </div>
                        `;
                        optionsHtml = `
                            <div class="options-group" id="opts-${book.slug}">
                                <label class="option-checkbox"><input type="checkbox" id="clean-${book.slug}"> Повторне очищення</label>
                                <label class="option-checkbox"><input type="checkbox" id="translate-${book.slug}" checked> Переклад</label>
                                <label class="option-checkbox"><input type="checkbox" id="ebook-${book.slug}" checked> Електронна книга</label>
                                <label class="option-checkbox"><input type="checkbox" id="audio-${book.slug}" ${book.generate_audiobook !== false ? 'checked' : ''} onchange="saveAudiobookDefault('${book.slug}', this.checked)"> Аудіокнига</label>
                            </div>
                        `;
                    }

                    let settingsHtml = '';
                    if (!book.progress.is_manga) {
                        settingsHtml = `
                            <details class="settings-details" id="details-${book.slug}" ${detailsOpenAttr}>
                                <summary>🎙️ Налаштування голосу</summary>
                                <form onsubmit="saveTtsSettings(event, '${book.slug}')" class="settings-grid">

                                    <div class="form-group" style="margin-bottom:0;">
                                        <label for="engine-${book.slug}">TTS Engine</label>
                                        <select id="engine-${book.slug}" class="form-control" style="padding: 0.5rem;" onchange="handleEngineChange('${book.slug}', this.value, '${book.target_lang}')">
                                            <option value="supertonic3" ${book.tts_engine === 'supertonic3' ? 'selected' : ''}>Supertonic 3 (Flow Matching, 31 мова)</option>
                                            <option value="styletts2" ${book.tts_engine === 'styletts2' ? 'selected' : ''}>StyleTTS2 (українська)</option>
                                        </select>
                                    </div>
                                    <div class="form-group" style="margin-bottom:0;">
                                        <label for="speaker-${book.slug}">Speaker / Voice</label>
                                        <select id="speaker-${book.slug}" class="form-control" style="padding: 0.5rem;" ${speakerDisabled}>
                                            ${speakerOptions}
                                        </select>
                                    </div>
                                    <div class="slider-group">
                                        <div class="slider-header">
                                            <span>Speed</span>
                                            <span><span id="speed-val-${book.slug}">${book.tts_speed}</span>x</span>
                                        </div>
                                        <input type="range" id="speed-${book.slug}" class="range-slider" min="0.5" max="2.0" step="0.1" value="${book.tts_speed}" oninput="document.getElementById('speed-val-${book.slug}').innerText = this.value">
                                    </div>
                                    <div class="slider-group" id="noise-scale-group-${book.slug}" ${book.tts_engine === 'supertonic3' ? 'style="opacity: 0.5; pointer-events: none;"' : ''}>
                                        <div class="slider-header">
                                            <span>Noise Scale</span>
                                            <span id="noise-scale-val-${book.slug}">${book.tts_noise_scale}</span>
                                        </div>
                                        <input type="range" id="noise-scale-${book.slug}" class="range-slider" min="0.1" max="1.5" step="0.05" value="${book.tts_noise_scale}" oninput="document.getElementById('noise-scale-val-${book.slug}').innerText = this.value" ${book.tts_engine === 'supertonic3' ? 'disabled' : ''}>
                                    </div>
                                    <div class="slider-group" id="noise-w-group-${book.slug}" ${book.tts_engine === 'supertonic3' ? 'style="opacity: 0.5; pointer-events: none;"' : ''}>
                                        <div class="slider-header">
                                            <span>Noise Width</span>
                                            <span id="noise-w-val-${book.slug}">${book.tts_noise_w}</span>
                                        </div>
                                        <input type="range" id="noise-w-${book.slug}" class="range-slider" min="0.1" max="1.5" step="0.05" value="${book.tts_noise_w}" oninput="document.getElementById('noise-w-val-${book.slug}').innerText = this.value" ${book.tts_engine === 'supertonic3' ? 'disabled' : ''}>
                                    </div>
                                    <div style="display: flex; align-items: flex-end;">
                                        <button type="submit" class="btn btn-primary" style="width: 100%; padding: 0.5rem 1rem; font-size: 0.875rem;">Save Settings</button>
                                    </div>
                                    
                                    <div class="preview-section">
                                        <label style="font-size: 0.85rem; font-weight: 600; color: var(--text-secondary);">Live Preview (TTS Language)</label>
                                        <textarea id="preview-text-${book.slug}" class="preview-text" placeholder="Enter test sentence..."></textarea>
                                        <div class="preview-controls">
                                            <button type="button" onclick="generatePreview('${book.slug}')" id="preview-btn-${book.slug}" class="btn btn-secondary" style="padding: 0.5rem 1rem; font-size: 0.85rem;">Hear Preview</button>
                                            <audio id="preview-audio-${book.slug}" controls style="display: none; height: 32px; flex-grow: 1;"></audio>
                                        </div>
                                    </div>
                                </form>
                            </details>
                        `;
                    }

                    // Simple-mode primary action (TASK-68 UX): one big
                    // obvious next step per state - the reference user is
                    // a non-technical manga reader.
                    const primaryHtml = renderPrimaryAction(book);

                    const cardStyle = isFirstLoad
                        ? `style="animation: fadeInUp 400ms var(--ease-out-snappy) forwards; animation-delay: ${index * 50}ms; opacity: 0; animation-fill-mode: forwards;"`
                        : `style="opacity: 1;"`;

                    return `
                        <div class="glass-card book-card" ${cardStyle}>
                            <div class="book-header">
                                <div class="book-info">
                                    <h3>${book.title}</h3>
                                    ${book.authors && book.authors !== 'Unknown' ? `<p>${book.authors}</p>` : ''}
                                </div>
                                <div style="display:flex; align-items:center; gap:0.5rem;">
                                    <span id="badge-${book.slug}" class="badge ${badgeClass}">${badgeText}</span>
                                    <div class="kebab-menu-container">
                                        <button onclick="toggleKebabMenu(event, '${book.slug}')" class="kebab-trigger-btn" title="Дії">⋮</button>
                                        <div class="kebab-dropdown" id="kebab-dropdown-${book.slug}">
                                            <button onclick="openBookSettings('${book.slug}')" class="dropdown-item">⚙️ Налаштування книги</button>
                                            ${!book.is_running && book.output_files && book.output_files.length > 0
                                                ? `<button onclick="rerunConversion('${book.slug}')" class="dropdown-item">🔄 Перекласти заново</button>`
                                                : ''
                                            }
                                            ${!(book.output_files && book.output_files.length > 0) && !book.is_running ? `<a href="/view/${book.slug}" class="dropdown-item">📖 Читати / Редагувати</a>` : ''}
                                            <button onclick="toggleBookTerminal('${book.slug}', '${book.title}')" class="dropdown-item">🧾 Технічний журнал</button>
                                            ${!book.is_running
                                                ? `<button onclick="deleteBook('${book.slug}', '${book.title.replace(/'/g, "\'")}')" class="dropdown-item text-danger">🗑️ Видалити книгу</button>`
                                                : ''
                                            }
                                        </div>
                                    </div>
                                </div>
                            </div>

                            ${progressHtml}

                            <div class="controls" id="primary-${book.slug}" style="display:flex; gap:0.6rem; flex-wrap:wrap;">
                                ${primaryHtml}
                            </div>

                            <details class="settings-details" id="adv-${book.slug}" ${advOpenAttr} style="margin-top:0.8rem;">
                                <summary>⚙️ Додаткові налаштування</summary>
                                ${optionsHtml}
                                ${settingsHtml}
                            </details>

                            ${book.output_files && book.output_files.length > 0 ? `
                                <div class="downloads">
                                    ${book.output_files.map(file => `
                                        <a class="download-link" href="/api/download/${book.slug}/${file}" target="_blank">
                                            📥 ${file}
                                        </a>
                                        <button onclick="deleteOutputFile('${book.slug}', '${file.replace(/'/g, "\\'")}')" class="download-link" style="background: rgba(239, 68, 68, 0.1); border-color: rgba(239, 68, 68, 0.2); color: var(--danger); cursor: pointer;" title="Видалити файл">
                                            🗑️
                                        </button>
                                    `).join('')}
                                </div>
                            ` : ''}

                            <div id="terminal-${book.slug}" class="terminal-inside-card" style="display: ${currentLogsSlug === book.slug ? 'block' : 'none'}; margin-top: 1rem;">
                                <div class="terminal-header" style="display: flex; justify-content: space-between; align-items: center; background: rgba(0,0,0,0.2); padding: 0.5rem; border-top-left-radius: 8px; border-top-right-radius: 8px; border-bottom: 1px solid rgba(255,255,255,0.05);">
                                    <span style="font-size: 0.8rem; font-family: 'Fira Code', monospace; display: flex; align-items: center; gap: 0.5rem;">
                                        <span id="terminalDot-${book.slug}" class="status-dot"></span> Console Logs
                                    </span>
                                    <button class="btn-close" style="background: none; border: none; color: var(--text-secondary); cursor: pointer; font-size: 1.2rem; padding: 0 0.5rem;" onclick="hideBookTerminal('${book.slug}')">&times;</button>
                                </div>
                                <div class="terminal-log-inside" id="terminalLog-${book.slug}" style="height: 180px; overflow-y: auto; background: rgba(0, 0, 0, 0.4); font-family: 'Fira Code', monospace; font-size: 0.75rem; padding: 0.75rem; border-bottom-left-radius: 8px; border-bottom-right-radius: 8px; white-space: pre-wrap; word-break: break-all; color: #a7f3d0; text-shadow: 0 0 2px rgba(164, 250, 200, 0.2);">${lastLogsCache[book.slug] || 'Select a book to display live logs.'}</div>
                            </div>
                        </div>
                    `;
                }).join('');

                books.forEach(book => {
                    const slug = book.slug;
                    const vals = formValues[slug];
                    if (vals) {
                        if (vals.engine !== null && document.getElementById(`engine-${slug}`)) {
                            document.getElementById(`engine-${slug}`).value = vals.engine;
                            handleEngineChange(slug, vals.engine, book.target_lang);
                        }
                        if (vals.speaker !== null && document.getElementById(`speaker-${slug}`)) {
                            document.getElementById(`speaker-${slug}`).value = vals.speaker;
                        }
                        if (vals.speed !== null && document.getElementById(`speed-${slug}`)) {
                            document.getElementById(`speed-${slug}`).value = vals.speed;
                            document.getElementById(`speed-val-${slug}`).innerText = vals.speed;
                        }
                        if (vals.noise_scale !== null && document.getElementById(`noise-scale-${slug}`)) {
                            document.getElementById(`noise-scale-${slug}`).value = vals.noise_scale;
                            document.getElementById(`noise-scale-val-${slug}`).innerText = vals.noise_scale;
                        }
                        if (vals.noise_w !== null && document.getElementById(`noise-w-${slug}`)) {
                            document.getElementById(`noise-w-${slug}`).value = vals.noise_w;
                            document.getElementById(`noise-w-val-${slug}`).innerText = vals.noise_w;
                        }
                        if (vals.preview_text !== '' && document.getElementById(`preview-text-${slug}`)) {
                            document.getElementById(`preview-text-${slug}`).value = vals.preview_text;
                        }
                        if (vals.clean !== null && document.getElementById(`clean-${slug}`)) {
                            document.getElementById(`clean-${slug}`).checked = vals.clean;
                        }
                        if (vals.translate !== null && document.getElementById(`translate-${slug}`)) {
                            document.getElementById(`translate-${slug}`).checked = vals.translate;
                        }
                        if (vals.ebook !== null && document.getElementById(`ebook-${slug}`)) {
                            document.getElementById(`ebook-${slug}`).checked = vals.ebook;
                        }
                        if (vals.audio !== null && document.getElementById(`audio-${slug}`)) {
                            document.getElementById(`audio-${slug}`).checked = vals.audio;
                        }
                        if (vals.manga_res !== null && document.getElementById(`manga-res-${slug}`)) {
                            document.getElementById(`manga-res-${slug}`).value = vals.manga_res;
                        }
                    }
                });

                // Restore active cursor focus and selection
                if (activeId) {
                    const activeEl = document.getElementById(activeId);
                    if (activeEl) {
                        activeEl.focus();
                        if (selStart !== null && selEnd !== null) {
                            activeEl.selectionStart = selStart;
                            activeEl.selectionEnd = selEnd;
                        }
                    }
                }
                if (currentLogsSlug) {
                    pollLogs();
                }
                isFirstLoad = false;
            } catch (err) {
                console.error('Failed to fetch books:', err);
                const container = document.getElementById('booksList');
                if (container && (container.innerHTML.includes('Loading books') || container.innerHTML.includes('Завантажую список книг'))) {
                    container.innerHTML = '<p style="color: var(--text-secondary); text-align: center;">Не вдалося завантажити список книг. Сервер зайнятий або недоступний, повторюємо спробу...</p>';
                }
            }
        }

        // TASK-56: Q's explicit instruction - make the EXISTING one-time
        // "Аудіокнига" checkbox itself remember its state per book,
        // rather than adding a second persistent-default toggle
        // alongside it in the settings modal.
        async function saveAudiobookDefault(slug, checked) {
            try {
                await fetch(`/api/book-settings/${slug}`, {
                    method: "POST", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ generate_audiobook: checked })
                });
            } catch (e) { /* best-effort */ }
        }

        async function runConversion(slug) {
            // Real incident (TASK-68): this is the big primary "▶️ Почати
            // переклад" button - the safe, resume-friendly action. It must
            // NEVER honor a stale "Повторно очистити сторінки" checkbox
            // left checked in the hidden Advanced panel - that silently
            // wiped and redid 12 already-translated frieren pages when Q
            // just wanted to resume/start normally. Only the explicit
            // "🔄 Перекласти заново" button (rerunConversion, with its own
            // confirm dialog) is allowed to send clean=true.
            const translate = document.getElementById(`translate-${slug}`)?.checked ?? true;
            const ebook = document.getElementById(`ebook-${slug}`)?.checked ?? true;
            const audio = document.getElementById(`audio-${slug}`)?.checked ?? true;
            const no_translate = !translate;
            const no_ebook = !ebook;
            const no_audio = !audio;

            // TASK-56: the resolution picker moved to the per-book ⚙️
            // settings modal (persisted server-side) - not sending it
            // here at all lets /api/run's own config.json fallback be
            // authoritative, instead of this button silently overriding
            // the saved default with a hardcoded literal every time.
            try {
                const response = await fetch(`/api/run/${slug}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ clean: false, no_translate, no_ebook, no_audio })
                });
                const res = await response.json();
                if (response.ok) {
                    fetchBooks();
                    selectBookForLogs(slug, slug);
                } else {
                    alert('Не вдалося запустити переклад:\n\n' + res.message);
                }
            } catch (err) {
                alert('Request failed: ' + err.message);
            }
        }

        async function rerunConversion(slug) {
            const clean = document.getElementById(`clean-${slug}`)?.checked || false;
            const warn = clean
                ? `«Повторно очистити сторінки» увімкнено: усі вже перекладені сторінки книги "${slug}" будуть ЗНИЩЕНІ і перероблені з нуля. Це може зайняти багато часу. Продовжити?`
                : `Перезапустити переклад "${slug}" з нуля? (готові сторінки будуть пропущені й лишаться як є)`;
            if (!confirm(warn)) return;
            const translate = document.getElementById(`translate-${slug}`)?.checked ?? true;
            const ebook = document.getElementById(`ebook-${slug}`)?.checked ?? true;
            const audio = document.getElementById(`audio-${slug}`)?.checked ?? true;
            const no_translate = !translate;
            const no_ebook = !ebook;
            const no_audio = !audio;
            try {
                const response = await fetch(`/api/run/${slug}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ force: true, clean, no_translate, no_ebook, no_audio })
                });
                const res = await response.json();
                if (response.ok) {
                    fetchBooks();
                    selectBookForLogs(slug, slug);
                } else {
                    alert('Error re-running conversion: ' + res.message);
                }
            } catch (err) {
                alert('Request failed: ' + err.message);
            }
        }

        // Q's explicit ask: "запустити чистий переклад з нуля... повинен
        // мати можливість і користувач" - a fully visible, one-click,
        // unambiguous "start over, ignore every cached page" action next
        // to the download button, independent of any hidden checkbox
        // state (unlike rerunConversion above, which reads clean-${slug}
        // and can silently resume-skip everything if left unchecked -
        // exactly the mistake that produced a fake "completed" run today).
        async function rerunConversionClean(slug) {
            if (!confirm(`Повністю перекласти "${slug}" з нуля?\n\nУСІ вже готові сторінки/розділи будуть ЗНИЩЕНІ і перекладені заново без жодних кешованих даних. Це довго (може зайняти кілька годин) і незворотно.\n\nПродовжити?`)) return;
            try {
                const response = await fetch(`/api/run/${slug}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ force: true, clean: true })
                });
                const res = await response.json();
                if (response.ok) {
                    fetchBooks();
                    selectBookForLogs(slug, slug);
                } else {
                    alert('Не вдалося запустити: ' + res.message);
                }
            } catch (err) {
                alert('Request failed: ' + err.message);
            }
        }

        async function stopConversion(slug) {
            try {
                const response = await fetch(`/api/stop/${slug}`, { method: 'POST' });
                const res = await response.json();
                if (response.ok) {
                    fetchBooks();
                } else {
                    alert('Error stopping conversion: ' + res.message);
                }
            } catch (err) {
                alert('Request failed: ' + err.message);
            }
        }

        async function deleteBook(slug, title) {
            if (!confirm(`Delete "${title}" permanently? This removes all source, cache, and output files and cannot be undone.`)) {
                return;
            }
            try {
                const response = await fetch(`/api/delete/${slug}`, { method: 'POST' });
                const res = await response.json();
                if (response.ok) {
                    fetchBooks();
                } else {
                    alert('Error deleting book: ' + res.message);
                }
            } catch (err) {
                alert('Request failed: ' + err.message);
            }
        }

        async function deleteOutputFile(slug, filename) {
            if (!confirm(`Delete file "${filename}" permanently?`)) {
                return;
            }
            try {
                const response = await fetch(`/api/delete-file/${slug}/${encodeURIComponent(filename)}`, { method: 'POST' });
                const res = await response.json();
                if (response.ok) {
                    fetchBooks();
                } else {
                    alert('Error deleting file: ' + res.message);
                }
            } catch (err) {
                alert('Request failed: ' + err.message);
            }
        }

        function selectBookForLogs(slug, title) {
            if (currentLogsSlug && currentLogsSlug !== slug) {
                const prevTerminal = document.getElementById(`terminal-${currentLogsSlug}`);
                if (prevTerminal) prevTerminal.style.display = 'none';
            }
            
            currentLogsSlug = slug;
            const terminal = document.getElementById(`terminal-${slug}`);
            if (terminal) {
                terminal.style.display = 'block';
                terminal.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            }
            
            if (logsInterval) clearInterval(logsInterval);
            
            pollLogs();
            logsInterval = setInterval(pollLogs, 1500);
        }

        async function pollLogs() {
            if (!currentLogsSlug) return;
            try {
                const response = await fetch(`/api/status/${currentLogsSlug}`);
                if (!response.ok) return;
                const status = await response.json();
                
                const logBox = document.getElementById(`terminalLog-${currentLogsSlug}`);
                if (!logBox) return;
                const prevScrollHeight = logBox.scrollHeight;
                const prevScrollTop = logBox.scrollTop;
                const prevClientHeight = logBox.clientHeight;
                
                if (status.logs && status.logs.length > 0) {
                    const text = status.logs.join('');
                    logBox.innerText = text;
                    lastLogsCache[currentLogsSlug] = text;
                } else {
                    const text = 'No log entries found. Job may be starting...';
                    logBox.innerText = text;
                    lastLogsCache[currentLogsSlug] = text;
                }
                
                const dot = document.getElementById(`terminalDot-${currentLogsSlug}`);
                if (dot) {
                    if (status.is_running) {
                        dot.className = 'status-dot active';
                    } else {
                        dot.className = 'status-dot';
                    }
                }
                
                if (prevScrollHeight - prevScrollTop <= prevClientHeight + 50) {
                    logBox.scrollTop = logBox.scrollHeight;
                }
            } catch (err) {
                console.error('Failed to poll logs:', err);
            }
        }

        async function saveTtsSettings(event, slug) {
            event.preventDefault();
            const tts_engine = document.getElementById(`engine-${slug}`).value;
            const tts_voice = tts_engine;
            const tts_voice_quality = 'medium';
            const tts_speaker_id = parseInt(document.getElementById(`speaker-${slug}`).value);
            const tts_speed = parseFloat(document.getElementById(`speed-${slug}`).value);
            const tts_noise_scale = parseFloat(document.getElementById(`noise-scale-${slug}`).value);
            const tts_noise_w = parseFloat(document.getElementById(`noise-w-${slug}`).value);
            
            try {
                const response = await fetch(`/api/tts-settings/${slug}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ tts_engine, tts_voice, tts_voice_quality, tts_speaker_id, tts_speed, tts_noise_scale, tts_noise_w })
                });
                const res = await response.json();
                if (response.ok) {
                    alert('Settings saved successfully!');
                    fetchBooks();
                } else {
                    alert('Error saving settings: ' + res.message);
                }
            } catch (err) {
                alert('Request failed: ' + err.message);
            }
        }

        async function generatePreview(slug) {
            const text = document.getElementById(`preview-text-${slug}`).value.trim();
            if (!text) {
                alert('Please enter some text first.');
                return;
            }
            
            const btn = document.getElementById(`preview-btn-${slug}`);
            const audio = document.getElementById(`preview-audio-${slug}`);
            
            btn.disabled = true;
            btn.innerText = 'Generating...';
            audio.style.display = 'none';
            
            // Read current unsaved form values
            const tts_engine = document.getElementById(`engine-${slug}`).value;
            const tts_voice_quality = 'medium';
            const tts_speaker_id = parseInt(document.getElementById(`speaker-${slug}`).value);
            const tts_speed = parseFloat(document.getElementById(`speed-${slug}`).value);
            const tts_noise_scale = parseFloat(document.getElementById(`noise-scale-${slug}`).value);
            const tts_noise_w = parseFloat(document.getElementById(`noise-w-${slug}`).value);
            
            try {
                const response = await fetch(`/api/tts-preview/${slug}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ text, tts_engine, tts_voice_quality, tts_speaker_id, tts_speed, tts_noise_scale, tts_noise_w })
                });
                
                if (response.ok) {
                    const blob = await response.blob();
                    const blobUrl = URL.createObjectURL(blob);
                    audio.src = blobUrl;
                    audio.style.display = 'block';
                    audio.play();
                } else {
                    const res = await response.json();
                    alert('Error generating preview: ' + (res.message || 'unknown error'));
                }
            } catch (err) {
                alert('Request failed: ' + err.message);
            } finally {
                btn.disabled = false;
                btn.innerText = 'Hear Preview';
            }
        }

        // Initial load
        fetchBooks();
        // Periodically refresh book states to update progress bars
        setInterval(fetchBooks, 5000);

        let activeFsPath = "/storage/emulated/0";
        let parentFsPath = null;

        async function initSettings() {
            try {
                const res = await fetch("/api/settings");
                if (res.status === 401) {
                    window.location.href = '/login';
                    return;
                }
                const settings = await res.json();
                if (!settings || !settings.output_root) {
                    throw new Error('Unexpected response: ' + JSON.stringify(settings));
                }
                document.getElementById("currentSaveLocation").textContent = settings.output_root;
                activeFsPath = settings.output_root;
            } catch (err) {
                console.error("Failed to load settings:", err);
                const el = document.getElementById("currentSaveLocation");
                if (el && el.textContent.includes('Loading')) {
                    el.textContent = 'Помилка завантаження';
                }
            }
        }

        // TASK-56: app-level settings modal. Deliberately additive, not a
        // replacement for the existing always-visible supportCard widget
        // or the standalone Моделі/Куди зберігати/Пароль buttons - this
        // late in a large change, duplicating a fresh fetch here is safer
        // than risking a regression by rewiring already-working elements.
        async function openAppSettings() {
            const modal = document.getElementById("appSettingsModal");
            const body = document.getElementById("appSettingsBody");
            modal.classList.add("active");
            body.innerHTML = '<p style="color: var(--text-secondary); text-align: center;">Завантаження...</p>';
            let d;
            try {
                const r = await fetch('/api/support/profile', { cache: 'no-store' });
                d = await r.json();
            } catch (e) {
                body.innerHTML = `<p style="color: var(--danger); text-align: center;">Не вдалося завантажити.</p>`;
                return;
            }
            const statusTxt = d.effective_disabled
                ? (d.remote_disabled ? 'вимкнено (через Telegram-бот)' : 'вимкнено (локально)')
                : 'увімкнено — дякуємо, що лишаєте 💙💛';
            const btnTxt = d.local_disabled ? 'Увімкнути локально' : 'Вимкнути локально';
            body.innerHTML = `
                <div style="border:1px solid var(--border-color); border-radius:10px; padding:0.8rem; margin-bottom:0.8rem;">
                    <b>🦥 Примітки підтримки в книгах:</b> <span id="appSupportStatus">${statusTxt}</span>
                    <div style="display:flex; gap:0.5rem; flex-wrap:wrap; margin-top:0.6rem;">
                        <a href="https://t.me/GetVydraBot" target="_blank" class="btn btn-primary"
                           style="text-decoration:none; padding:0.5rem 1rem; font-size:0.85rem; border-radius:8px;">
                            🤖 Керувати в Telegram (донат / вимкнення)
                        </a>
                        <button id="appSupportLocalBtn" class="btn-outline"
                                style="padding:0.5rem 1rem; font-size:0.85rem; border-radius:8px; cursor:pointer;"
                                onclick="toggleLocalBannerFromSettings()">${btnTxt}</button>
                    </div>
                </div>
                <div style="border:1px solid var(--border-color); border-radius:10px; padding:0.8rem;">
                    <b>🤖 Моделі та сервер перекладу</b>
                    <div style="margin-top:0.5rem;">
                        <button class="btn-outline" style="padding:0.5rem 1rem; font-size:0.85rem; border-radius:8px; cursor:pointer;"
                                onclick="closeAppSettings(); openModelsSelector();">Відкрити Models Manager →</button>
                    </div>
                </div>`;
        }

        function closeAppSettings() {
            document.getElementById("appSettingsModal").classList.remove("active");
        }

        async function toggleLocalBannerFromSettings() {
            await toggleLocalBanner();
            const r = await fetch('/api/support/profile', { cache: 'no-store' });
            const d = await r.json();
            const statusEl = document.getElementById('appSupportStatus');
            const btnEl = document.getElementById('appSupportLocalBtn');
            if (statusEl) statusEl.textContent = d.effective_disabled
                ? (d.remote_disabled ? 'вимкнено (через Telegram-бот)' : 'вимкнено (локально)')
                : 'увімкнено — дякуємо, що лишаєте 💙💛';
            if (btnEl) btnEl.textContent = d.local_disabled ? 'Увімкнути локально' : 'Вимкнути локально';
        }

        // Auto-open modal helper for direct URL previews & screenshot testing
        window.addEventListener("DOMContentLoaded", () => {
            const params = new URLSearchParams(window.location.search);
            if (params.get("modal") === "settings") {
                setTimeout(() => openBookSettings(params.get("book") || "vibe-programming"), 600);
            } else if (params.get("modal") === "onboard") {
                setTimeout(() => {
                    const m = document.getElementById("premiumOnboardModal");
                    if (m) m.classList.add("active");
                }, 600);
            }
        });

        // TASK-56: per-book settings modal - Cast Registry stays a LINK to
        // the "Cast & Context" tab (its own dedicated UI already exists
        // there, deliberately not duplicated here), everything else
        // that previously had zero UI (enable_agent_editor - manual.html
        // used to literally tell users to hand-edit config.json) or lived
        // as a per-run-only checkbox now lives in one persistent place.
        async function openBookSettings(slug) {
            const modal = document.getElementById("bookSettingsModal");
            const body = document.getElementById("bookSettingsBody");
            modal.classList.add("active");
            body.innerHTML = '<p style="color: var(--text-secondary); text-align: center;">Завантаження...</p>';
            let s;
            try {
                const r = await fetch(`/api/book-settings/${slug}`, { cache: "no-store" });
                if (!r.ok) throw new Error("HTTP " + r.status);
                s = await r.json();
            } catch (e) {
                body.innerHTML = `<p style="color: var(--danger); text-align: center;">Не вдалося завантажити налаштування.</p>`;
                return;
            }

            const lockLine = s.entitled
                ? `<span style="color:#22c55e;">✓ Розблоковано</span>`
                : `<span style="color:#f0b429;">🔒 Потребує підтримки проєкту — <a href="https://t.me/GetVydraBot" target="_blank" style="color:var(--primary);">@GetVydraBot</a></span>`;

            let html = '';
            if (s.is_manga) {
                html += `
                    <div style="border:1px solid var(--border-color); border-radius:10px; padding:0.8rem; margin-bottom:0.8rem;">
                        <b>🧬 Cast Registry</b> — граматичний рід персонажів у перекладі.
                        <div style="margin-top:0.4rem;">
                            <a href="/view/${slug}#cast" style="color:var(--primary);">Відкрити «Cast & Context» →</a>
                        </div>
                    </div>
                    <div style="border:1px solid var(--border-color); border-radius:10px; padding:0.8rem; margin-bottom:0.8rem;">
                        <label style="font-size:0.85rem; color:var(--text-secondary); display:block; margin-bottom:0.4rem;">📖 Пристрій для читання (роздільність) — типово для цієї книги</label>
                        <select id="bs-resolution" class="form-control" style="padding: 0.5rem; font-size: 0.9rem; width: 100%;">
                            <option value="1280x1920" ${s.manga_resolution === '1280x1920' ? 'selected' : ''}>Safe Default (1280x1920)</option>
                            <option value="1860x2480" ${s.manga_resolution === '1860x2480' ? 'selected' : ''}>Kindle Scribe (1860x2480)</option>
                            <option value="1264x1680" ${s.manga_resolution === '1264x1680' ? 'selected' : ''}>Paperwhite 6 / 2024 (1264x1680)</option>
                            <option value="1236x1648" ${s.manga_resolution === '1236x1648' ? 'selected' : ''}>Paperwhite 5 / Oasis 3 (1236x1648)</option>
                            <option value="1072x1448" ${s.manga_resolution === '1072x1448' ? 'selected' : ''}>Paperwhite 3/4 / Voyage / Basic 11 (1072x1448)</option>
                            <option value="600x800" ${s.manga_resolution === '600x800' ? 'selected' : ''}>Kindle Basic / Older (600x800)</option>
                            <option value="original" ${s.manga_resolution === 'original' ? 'selected' : ''}>Original (без зміни розміру)</option>
                        </select>
                    </div>`;
            }

            html += `
                <div style="border:1px solid var(--border-color); border-radius:10px; padding:0.8rem; margin-bottom:0.8rem;">
                    <label style="display:flex; align-items:flex-start; gap:0.6rem; cursor:pointer;">
                        <input type="checkbox" id="bs-honorifics" ${s.keep_honorifics ? 'checked' : ''}
                               style="width:20px; height:20px; margin-top:0.1rem; accent-color:var(--primary);">
                        <span><b>🈂️ Зберігати гоноративи</b> — не перекладати суфікси на кшталт -сан, -чан, -кун, залишати як у оригіналі.</span>
                    </label>
                </div>`;

            // UI 2.0 Premium Section Header & Checkboxes for Audio & Text Translation
            const statusBadge = s.entitled
                ? `<span style="color:#22c55e; background:rgba(34,197,94,0.15); border:1px solid rgba(34,197,94,0.3); padding:0.2rem 0.5rem; border-radius:6px; font-size:0.75rem; font-weight:600;">✓ Активно</span>`
                : `<span style="color:#f0b429; background:rgba(240,180,41,0.15); border:1px solid rgba(240,180,41,0.3); padding:0.2rem 0.5rem; border-radius:6px; font-size:0.75rem; font-weight:600;">🔒 Потребує підтримки</span>`;

            html += `
                <div style="border:1px solid rgba(240,180,41,0.3); background:rgba(240,180,41,0.03); border-radius:12px; padding:0.9rem; margin-top:0.8rem;">
                    <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:0.8rem; padding-bottom:0.6rem; border-bottom:1px solid rgba(255,255,255,0.08);">
                        <span style="font-weight:700; color:#f0b429; font-size:0.95rem; display:flex; align-items:center; gap:0.4rem;">
                            👑 Преміум-можливості (UI 2.0)
                        </span>
                        ${statusBadge}
                    </div>

                    <!-- 🎙️ ASR-верифікація наголосів (Whisper) (аудіопайплайн) -->
                    <div style="margin-bottom:0.8rem; padding-bottom:0.8rem; border-bottom:1px solid rgba(255,255,255,0.06);">
                        <label style="display:flex; align-items:flex-start; gap:0.6rem; cursor:pointer;">
                            <input type="checkbox" id="bs-asr" ${s.enable_asr_verify ? 'checked' : ''} ${s.entitled ? '' : 'disabled'}
                                   style="width:20px; height:20px; margin-top:0.15rem; accent-color:var(--primary);">
                            <div>
                                <span style="font-weight:600; color:var(--text-main, #fff);">🎙️ ASR-верифікація наголосів (Whisper)</span>
                                <p style="font-size:0.82rem; color:var(--text-secondary); margin:0.25rem 0 0 0; line-height:1.35;">
                                    Порівнює синтезоване аудіо з текстом через Whisper для виявлення помилок наголосів і направлення їх у чергу верифікації.
                                </p>
                            </div>
                        </label>
                    </div>

                    <!-- 🧠 MQM-оцінка якості (текстовий переклад) -->
                    <div style="margin-bottom:0.8rem; padding-bottom:0.8rem; border-bottom:1px solid rgba(255,255,255,0.06);">
                        <label style="display:flex; align-items:flex-start; gap:0.6rem; cursor:pointer;">
                            <input type="checkbox" id="bs-mqm" ${s.enable_mqm_review ? 'checked' : ''} ${s.entitled ? '' : 'disabled'}
                                   style="width:20px; height:20px; margin-top:0.15rem; accent-color:var(--primary);">
                            <div>
                                <span style="font-weight:600; color:var(--text-main, #fff);">🧠 MQM-оцінка якості перекладу</span>
                                <p style="font-size:0.82rem; color:var(--text-secondary); margin:0.25rem 0 0 0; line-height:1.35;">
                                    Окрема модель-рецензент аналізує кожен перекладений абзац (1-10, шукає пропуски й смислові викривлення) і позначає сумнівні місця для перегляду.
                                </p>
                            </div>
                        </label>
                    </div>

                    <!-- 🤖 Агент-редактор (текстовий переклад & манґа) -->
                    <div>
                        <label style="display:flex; align-items:flex-start; gap:0.6rem; cursor:pointer;">
                            <input type="checkbox" id="bs-agent" ${s.enable_agent_editor ? 'checked' : ''} ${s.entitled ? '' : 'disabled'}
                                   style="width:20px; height:20px; margin-top:0.15rem; accent-color:var(--primary);">
                            <div>
                                <span style="font-weight:600; color:var(--text-main, #fff);">🤖 Агент-редактор (Gemma 3 4B)</span>
                                <p style="font-size:0.82rem; color:var(--text-secondary); margin:0.25rem 0 0 0; line-height:1.35;">
                                    Автономний ШІ перевіряє складні й проблемні місця перекладу та пропонує виправлення з вашим підтвердженням.
                                </p>
                            </div>
                        </label>
                    </div>
                </div>`;
            body.innerHTML = html;

            const save = async (field, value) => {
                try {
                    await fetch(`/api/book-settings/${slug}`, {
                        method: "POST", headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ [field]: value })
                    });
                } catch (e) { /* best-effort - user can retry by reopening */ }
            };

            const checkAndConsentModel = async (field, checkbox, modelKey, consentText, downloadTarget) => {
                if (!checkbox.checked) {
                    await save(field, false);
                    return;
                }

                if (modelKey && modelKey !== 'none') {
                    try {
                        const statusRes = await fetch('/api/premium/models-status');
                        const status = await statusRes.json();

                        let modelReady = false;
                        if (modelKey === 'asr_whisper' && status.asr_whisper) {
                            modelReady = status.asr_whisper.ready;
                        } else if (modelKey === 'gemma' && status.gemma) {
                            modelReady = status.gemma.ready && (status.mmproj ? status.mmproj.ready : true);
                        }

                        if (!modelReady) {
                            const userConfirmed = confirm(consentText || "Для роботи цієї функції потрібно завантажити додаткові нейромережеві моделі. Рекомендовано Wi-Fi. Завантажити зараз?");
                            if (!userConfirmed) {
                                checkbox.checked = false;
                                await save(field, false);
                                return;
                            }
                            // User consented to download
                            const dlRes = await fetch('/api/premium/download-models', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({
                                    target: downloadTarget,
                                    consent_accepted: true,
                                    gemma_terms_accepted: true
                                })
                            });
                            const dlData = await dlRes.json();
                            alert(dlData.message || "Завантаження моделей розпочато у фоні.");
                        }
                    } catch (e) {
                        console.warn("Could not verify model status:", e);
                    }
                }

                await save(field, true);
            };

            const agentEl = document.getElementById("bs-agent");
            if (agentEl) agentEl.addEventListener("change", () => checkAndConsentModel(
                "enable_agent_editor",
                agentEl,
                "gemma",
                "Для роботи цієї функції потрібні моделі Gemma 3 4B та Vision Projector (~3.5 ГБ). Рекомендовано Wi-Fi. Завантажити зараз?",
                "gemma"
            ));
            const resEl = document.getElementById("bs-resolution");
            if (resEl) resEl.addEventListener("change", () => save("manga_resolution", resEl.value));
            const honEl = document.getElementById("bs-honorifics");
            if (honEl) honEl.addEventListener("change", () => save("keep_honorifics", honEl.checked));
            const mqmEl = document.getElementById("bs-mqm");
            if (mqmEl) mqmEl.addEventListener("change", () => save("enable_mqm_review", mqmEl.checked));
            const asrEl = document.getElementById("bs-asr");
            if (asrEl) asrEl.addEventListener("change", () => checkAndConsentModel(
                "enable_asr_verify",
                asrEl,
                "asr_whisper",
                "Для роботи цієї функції потрібно завантажити додаткові нейромережеві моделі (наприклад, Whisper для розпізнавання мовлення, ~245 МБ). Рекомендовано Wi-Fi. Завантажити зараз?",
                "asr"
            ));
        }

        function closeBookSettings() {
            document.getElementById("bookSettingsModal").classList.remove("active");
        }

        async function openFolderSelector() {
            const modal = document.getElementById("folderModal");
            modal.classList.add("active");
            
            // Load current path
            try {
                const res = await fetch("/api/settings");
                const settings = await res.json();
                activeFsPath = settings.output_root;
            } catch (e) {
                activeFsPath = "/storage/emulated/0";
            }
            await loadDirectory(activeFsPath);
        }

        function closeFolderSelector() {
            const modal = document.getElementById("folderModal");
            modal.classList.remove("active");
        }

        async function loadDirectory(path) {
            const listEl = document.getElementById("fsList");
            listEl.innerHTML = `<p style="padding: 1rem; color: var(--text-secondary); text-align: center;">Loading folder contents...</p>`;
            
            try {
                const res = await fetch(`/api/browse-fs?path=${encodeURIComponent(path)}`);
                const data = await res.json();
                
                if (data.error) {
                    listEl.innerHTML = `<p style="padding: 1rem; color: var(--danger); text-align: center;">${data.error}</p>`;
                    return;
                }
                
                activeFsPath = data.current;
                parentFsPath = data.parent;
                
                document.getElementById("fsCurrentPath").value = data.current;
                
                // Show/hide up button
                const parentBtn = document.getElementById("fsParentBtn");
                if (parentFsPath) {
                    parentBtn.style.display = "flex";
                } else {
                    parentBtn.style.display = "none";
                }
                
                // List folders
                listEl.innerHTML = "";
                if (data.hint) {
                    listEl.innerHTML = `<p style="padding: 0.6rem 1rem; color: var(--text-secondary); font-size: 0.85rem;">${data.hint}</p>`;
                }
                if (data.dirs.length === 0) {
                    listEl.innerHTML = `<p style="padding: 1.5rem; color: var(--text-secondary); text-align: center;">No subfolders found</p>`;
                    return;
                }
                
                data.dirs.forEach(item => {
                    const div = document.createElement("div");
                    div.className = "fs-item";
                    div.onclick = () => loadDirectory(item.path);
                    
                    const icon = document.createElement("span");
                    icon.className = "fs-item-icon";
                    icon.textContent = "📁";
                    
                    const name = document.createElement("span");
                    name.textContent = item.name;
                    
                    div.appendChild(icon);
                    div.appendChild(name);
                    listEl.appendChild(div);
                });
            } catch (err) {
                // TASK-52: surface the REAL failure instead of a generic
                // message - next occurrence self-explains.
                listEl.innerHTML = `<p style="padding: 1rem; color: var(--danger); text-align: center;">Failed to load directories: ${err.message || err}</p>`;
            }
        }

        async function navigateFsParent() {
            if (parentFsPath) {
                await loadDirectory(parentFsPath);
            }
        }

        async function confirmFolderSelection() {
            const btn = document.getElementById("fsSelectBtn");
            btn.disabled = true;
            btn.textContent = "Saving...";
            
            const selectedPath = document.getElementById("fsCurrentPath").value.trim();
            
            try {
                const res = await fetch("/api/settings/output-root", {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json"
                    },
                    body: JSON.stringify({ path: selectedPath })
                });
                const data = await res.json();
                if (data.status === "success") {
                    document.getElementById("currentSaveLocation").textContent = data.output_root;
                    closeFolderSelector();
                } else {
                    alert("Error: " + (data.message || "Failed to set output directory"));
                }
            } catch (err) {
                alert("Failed to connect to server: " + err);
            } finally {
                btn.disabled = false;
                btn.textContent = "✓ Select This Folder";
            }
        }

        async function openModelsSelector() {
            const modal = document.getElementById("modelsModal");
            modal.classList.add("active");
            await loadModelsData();
        }

        function closeModelsSelector() {
            const modal = document.getElementById("modelsModal");
            modal.classList.remove("active");
        }

        async function loadModelsData() {
            try {
                const res = await fetch("/api/models");
                const data = await res.json();
                
                const dot = document.getElementById("modelsServerDot");
                const status = document.getElementById("modelsServerStatus");
                const loaded = document.getElementById("modelsServerLoaded");
                
                if (data.server_status.running) {
                    dot.className = "status-dot active";
                    status.innerText = "Running";
                    status.style.color = "var(--success)";
                    loaded.innerText = "Loaded model: " + (data.server_status.loaded_model || "Unknown");
                } else {
                    dot.className = "status-dot";
                    status.innerText = "Stopped";
                    status.style.color = "var(--text-secondary)";
                    loaded.innerText = "Port 8081 is closed";
                }
                
                const tSelect = document.getElementById("modelsTranslationSelect");

                tSelect.innerHTML = "";

                if (!data.available_models || data.available_models.length === 0) {
                    tSelect.innerHTML = `<option value="">No .gguf models found in ~/models/</option>`;
                } else {
                    data.available_models.forEach(model => {
                        const filename = model.split('/').pop();
                        tSelect.innerHTML += `<option value="${model}" ${data.translation_model === model ? 'selected' : ''}>${filename}</option>`;
                    });
                }
            } catch (err) {
                console.error("Failed to load models data:", err);
            }
        }

        async function saveModelsConfiguration() {
            const btn = document.getElementById("modelsSaveBtn");
            btn.disabled = true;
            btn.textContent = "Saving...";
            
            const translation_model = document.getElementById("modelsTranslationSelect").value;

            try {
                const res = await fetch("/api/models/configure", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ translation_model })
                });
                const data = await res.json();
                if (data.status === "success") {
                    alert("Models configuration saved successfully!");
                    closeModelsSelector();
                } else {
                    alert("Error saving configuration: " + data.message);
                }
            } catch (err) {
                alert("Request failed: " + err);
            } finally {
                btn.disabled = false;
                btn.textContent = "Save Configuration";
            }
        }

        async function startTranslationServer() {
            const btn = document.getElementById("modelsStartBtn");
            btn.disabled = true;
            btn.textContent = "Starting...";

            try {
                const res = await fetch("/api/models/start", { method: "POST" });
                const data = await res.json();
                if (data.status !== "success") {
                    alert("Error starting server: " + data.message);
                    return;
                }

                // Keep the button disabled until the server actually
                // reports running (model load can take up to ~30s+ on
                // device) instead of re-enabling right after the
                // fire-and-forget /api/models/start call returns - avoids
                // a double-click spawning a second server (TASK-18).
                const deadline = Date.now() + 90000;
                let running = false;
                while (Date.now() < deadline) {
                    await new Promise(r => setTimeout(r, 2000));
                    await loadModelsData();
                    const statusEl = document.getElementById("modelsServerStatus");
                    if (statusEl && statusEl.innerText === "Running") {
                        running = true;
                        break;
                    }
                }
                if (!running) {
                    alert("Server did not report as running within 90s - check ~/llama-translation-server.log on-device.");
                }
            } catch (err) {
                alert("Request failed: " + err);
            } finally {
                btn.disabled = false;
                btn.textContent = "Start Server";
            }
        }

        async function stopTranslationServer() {
            const btn = document.getElementById("modelsStopBtn");
            btn.disabled = true;
            btn.textContent = "Stopping...";
            
            try {
                const res = await fetch("/api/models/stop", { method: "POST" });
                const data = await res.json();
                if (data.status === "success") {
                    alert("Translation server stopped successfully.");
                    setTimeout(loadModelsData, 1000);
                } else {
                    alert("Error stopping server: " + data.message);
                }
            } catch (err) {
                alert("Request failed: " + err);
            } finally {
                btn.disabled = false;
                btn.textContent = "Stop Server";
            }
        }

        // Slug is an internal filesystem id; asking non-technical users to
        // type "lowercase a-z0-9_-" produced confusion (real feedback:
        // son didn't understand what it meant). Auto-derive it from the
        // title instead - transliterate Cyrillic, strip everything else
        // to a clean latin slug - and drop the field from the visible
        // form entirely (kept as a hidden input for the existing
        // submit/backend code, which is untouched).
        const CYRILLIC_TRANSLIT = {
            а:'a',б:'b',в:'v',г:'h',ґ:'g',д:'d',е:'e',є:'ie',ж:'zh',з:'z',и:'y',
            і:'i',ї:'i',й:'i',к:'k',л:'l',м:'m',н:'n',о:'o',п:'p',р:'r',с:'s',
            т:'t',у:'u',ф:'f',х:'kh',ц:'ts',ч:'ch',ш:'sh',щ:'shch',ь:'',ю:'iu',
            я:'ia',ы:'y',э:'e',ъ:'',
        };
        function slugify(text) {
            const translit = (text || '').toLowerCase().split('').map(
                ch => CYRILLIC_TRANSLIT[ch] !== undefined ? CYRILLIC_TRANSLIT[ch] : ch
            ).join('');
            let slug = translit.replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
            return slug || ('book-' + Date.now().toString(36));
        }

        function openPasswordModal() {
            document.getElementById('passwordModal').classList.add('active');
            document.getElementById('passwordStatus').className = 'upload-status';
            document.getElementById('passwordStatus').innerText = '';
            document.getElementById('newPassword1').value = '';
            document.getElementById('newPassword2').value = '';
        }
        function closePasswordModal() {
            document.getElementById('passwordModal').classList.remove('active');
        }
        async function submitPasswordChange() {
            const p1 = document.getElementById('newPassword1').value;
            const p2 = document.getElementById('newPassword2').value;
            const statusEl = document.getElementById('passwordStatus');
            if (p1 !== p2) {
                statusEl.className = 'upload-status error';
                statusEl.innerText = 'Passwords do not match.';
                return;
            }
            try {
                const res = await fetch('/api/change-password', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ new_password: p1 }),
                });
                const d = await res.json();
                statusEl.className = 'upload-status ' + (res.ok ? 'success' : 'error');
                statusEl.innerText = d.message;
                if (res.ok) setTimeout(closePasswordModal, 1500);
            } catch (err) {
                statusEl.className = 'upload-status error';
                statusEl.innerText = 'Request failed: ' + err.message;
            }
        }

        function updateSourcePathPlaceholder(isMangaChecked) {
            const label = document.getElementById("pdfPathLabel");
            const input = document.getElementById("pdf_path");
            // The file picker's `accept` filter hides everything not
            // listed - manga formats (CBZ/CBR/CB7/ZIP) were never in the
            // list, so Android's file browser silently hid them even
            // though the backend (extract_manga_pages) fully supports
            // them. Toggle both accept and the visible label with the
            // Is Manga checkbox instead of leaving it text-book-only.
            const fileLabel = document.getElementById("fileUploadLabel");
            const fileInput = document.getElementById("file_upload");
            if (isMangaChecked) {
                label.innerText = "Or Enter Source Directory/File Path (on system)";
                input.placeholder = "напр. /storage/emulated/0/Documents/MyComic";
                fileLabel.innerText = "Upload File (CBZ / CBR / CB7 / ZIP)";
                fileInput.setAttribute("accept", ".cbz,.cbr,.cb7,.zip,.rar,.pdf");
            } else {
                label.innerText = "Or Enter Source PDF Path (on system)";
                input.placeholder = "e.g. /path/to/book.pdf";
                fileLabel.innerText = "Upload File (PDF / EPUB / TXT / MD)";
                fileInput.setAttribute("accept", ".pdf,.epub,.txt,.md");
            }
        }

        function openAddBookModal() {
            document.getElementById("addBookModal").classList.add("active");
            document.getElementById("uploadStatus").className = "upload-status";
            document.getElementById("uploadStatus").innerText = "";
            updateSourcePathPlaceholder(false);
        }
        function closeAddBookModal() {
            document.getElementById("addBookModal").classList.remove("active");
        }
        function toggleBookTerminal(slug, title) {
            const terminal = document.getElementById(`terminal-${slug}`);
            if (terminal && terminal.style.display === 'block') {
                hideBookTerminal(slug);
            } else {
                selectBookForLogs(slug, title);
            }
        }
        function hideBookTerminal(slug) {
            const terminal = document.getElementById(`terminal-${slug}`);
            if (terminal) terminal.style.display = 'none';
            if (currentLogsSlug === slug) {
                currentLogsSlug = null;
                if (logsInterval) {
                    clearInterval(logsInterval);
                    logsInterval = null;
                }
            }
        }

        // Initialize settings on page load
        initSettings();
        // Kebab Menu Handlers
        window.toggleKebabMenu = (event, slug) => {
            event.stopPropagation();
            document.querySelectorAll('.kebab-dropdown.active').forEach(el => {
                if (el.id !== `kebab-dropdown-${slug}`) {
                    el.classList.remove('active');
                }
            });
            const dropdown = document.getElementById(`kebab-dropdown-${slug}`);
            if (dropdown) {
                dropdown.classList.toggle('active');
            }
        };

        document.addEventListener('click', () => {
            document.querySelectorAll('.kebab-dropdown.active').forEach(el => {
                el.classList.remove('active');
            });
            window.closeHeaderMenu();
        });
    
        window.toggleHeaderMenu = (event) => {
            event.stopPropagation();
            const dropdown = document.getElementById('headerSettingsDropdown');
            if (dropdown) dropdown.classList.toggle('active');
        };
        window.closeHeaderMenu = () => {
            const dropdown = document.getElementById('headerSettingsDropdown');
            if (dropdown) dropdown.classList.remove('active');
        };

        function showToast(msg, type = 'info', duration = 4000) {
            let container = document.querySelector('.toast-container');
            if (!container) {
                container = document.createElement('div');
                container.className = 'toast-container';
                document.body.appendChild(container);
            }
            const toast = document.createElement('div');
            toast.className = `toast toast-${type}`;
            toast.innerHTML = `<span>${msg}</span>`;
            container.appendChild(toast);

            if (duration > 0) {
                setTimeout(() => {
                    toast.style.opacity = '0';
                    toast.style.transform = 'translateY(-10px) scale(0.95)';
                    setTimeout(() => toast.remove(), 300);
                }, duration);
            }
            return toast;
        }
