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

        let isManga = false;
        let bookData = null;
        let currentMangaPage = 0;

        function escapeHtml(s) {
            const div = document.createElement('div');
            div.textContent = s;
            return div.innerHTML;
        }

        // TASK-34: real page filenames (from scanlator archive names, e.g.
        // "...Journey's End...") routinely contain apostrophes. escapeHtml()
        // only protects HTML markup - it does nothing for a value embedded
        // inside a single-quoted JS string in an inline onclick="" attribute,
        // since the '&#39;' it produces decodes right back to a literal '
        // before the JS parser ever sees it. This escapes for that context
        // specifically: backslash-escape backslashes and single quotes so
        // the string stays one valid JS string literal after HTML parsing.
        function jsAttrEscape(s) {
            return String(s).replace(/\\/g, '\\\\').replace(/'/g, "\\'");
        }

        function htmlEscape(s) {
            return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        }

        function wordDiffHtml(oldText, newText, showNew) {
            const oldWords = (oldText || '').split(/(\s+)/);
            const newWords = (newText || '').split(/(\s+)/);
            const m = oldWords.length, n = newWords.length;
            const dp = Array.from({length: m + 1}, () => new Array(n + 1).fill(0));
            for (let i = m - 1; i >= 0; i--) {
                for (let j = n - 1; j >= 0; j--) {
                    dp[i][j] = oldWords[i] === newWords[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
                }
            }
            let i = 0, j = 0;
            const out = [];
            while (i < m && j < n) {
                if (oldWords[i] === newWords[j]) {
                    out.push({ w: showNew ? newWords[j] : oldWords[i], same: true });
                    i++; j++;
                } else if (dp[i + 1][j] >= dp[i][j + 1]) {
                    if (!showNew) out.push({ w: oldWords[i], same: false });
                    i++;
                } else {
                    if (showNew) out.push({ w: newWords[j], same: false });
                    j++;
                }
            }
            while (!showNew && i < m) { out.push({ w: oldWords[i], same: false }); i++; }
            while (showNew && j < n) { out.push({ w: newWords[j], same: false }); j++; }
            return out.map(o => o.same ? escapeHtml(o.w) :
                `<span style="background:${showNew ? 'rgba(16,185,129,0.25)' : 'rgba(239,68,68,0.25)'}; text-decoration:${showNew ? 'none' : 'line-through'};">${escapeHtml(o.w)}</span>`
            ).join('');
        }

        // TASK-23: while status=running, live edits made from this page are
        // applied with priority by the running generator itself (manga/batch
        // page-and-batch boundary checks, or the audio priority queue) —
        // this banner just surfaces that state so it isn't a silent wait.
        let liveStatusInterval = null;

        async function pollLiveGenerationStatus() {
            try {
                const res = await fetch(`/api/status/${slug}`);
                if (!res.ok) return;
                const status = await res.json();
                const banner = document.getElementById("live-edit-banner");

                if (status.is_running) {
                    let pendingCount = 0;
                    try {
                        const resEdits = await fetch(`/api/edit/queue/${slug}?status=pending`);
                        if (resEdits.ok) {
                            const edits = await resEdits.json();
                            pendingCount = Array.isArray(edits) ? edits.length : 0;
                        }
                    } catch (e) { /* non-fatal - banner still shows without a count */ }

                    const pct = Math.round(status.translation_percent || 0);
                    let msg = `🔴 Generation in progress (${pct}% translated). Edits you make here are applied with priority as the pipeline reaches each page/batch.`;
                    if (pendingCount > 0) {
                        msg += ` ${pendingCount} edit(s) currently queued.`;
                    }
                    banner.textContent = msg;
                    banner.style.display = "block";

                    if (!liveStatusInterval) {
                        liveStatusInterval = setInterval(pollLiveGenerationStatus, 12000);
                    }
                } else {
                    banner.style.display = "none";
                    if (liveStatusInterval) {
                        clearInterval(liveStatusInterval);
                        liveStatusInterval = null;
                    }
                }
            } catch (err) {
                console.error("pollLiveGenerationStatus failed:", err);
            }
        }

        async function fetchBookData() {
            try {
                // First, check list of books to find metadata
                const resBooks = await fetch("/api/books");
                const books = await resBooks.json();
                const book = books.find(b => b.slug === slug);
                if (book) {
                    document.getElementById("book-title").textContent = book.title;
                    document.getElementById("book-slug").textContent = `Автор: ${book.authors} | Мова: ${book.target_lang.toUpperCase()} | Рушій: ${book.tts_engine}`;
                    isManga = book.is_manga || false;
                }

                if (isManga) {
                    const resManga = await fetch(`/api/preview/manga/${slug}`);
                    bookData = await resManga.json();
                    renderManga();
                } else {
                    const resBook = await fetch(`/api/preview/book/${slug}`);
                    bookData = await resBook.json();
                    renderBook();
                }
                
                document.getElementById("loader").style.display = "none";
                document.getElementById("content-area").style.display = "block";
                pollLiveGenerationStatus();
            } catch (err) {
                console.error(err);
                document.getElementById("loader").innerHTML = `<div style="color:var(--danger)">Failed to load preview data. Make sure pipeline has run.</div>`;
            }
        }

        function renderManga() {
            const area = document.getElementById("content-area");
            if (!bookData.source_pages || bookData.source_pages.length === 0) {
                area.innerHTML = `<div style="text-align:center;color:var(--text-secondary);padding:3rem 0;">
                    Сторінок манги не знайдено. Запустіть процес перекладу манги.
                </div>`;
                return;
            }

            area.innerHTML = `
                <div class="tabs" style="display: flex; gap: 1rem; border-bottom: 1px solid var(--border-color); padding-bottom: 1rem; margin-bottom: 1.5rem;">
                    <button class="tab-btn active" id="manga-tab-viewer" style="color: var(--primary); border-bottom: 2px solid var(--primary); padding: 0.5rem 1rem;">📖 Viewer</button>
                    <button class="tab-btn" id="manga-tab-pending" style="color: var(--text-secondary); padding: 0.5rem 1rem; border-bottom: 2px solid transparent;">📝 Pending Edits</button>
                    <button class="tab-btn" id="manga-tab-cast" style="color: var(--text-secondary); padding: 0.5rem 1rem; border-bottom: 2px solid transparent;">🧬 Cast & Context</button>
                    <button class="tab-btn" id="manga-tab-agent" style="color: var(--text-secondary); padding: 0.5rem 1rem; border-bottom: 2px solid transparent;">🤖 Агент</button>
                </div>
                <div id="manga-tab-content"></div>
            `;

            const mangaTabContent = document.getElementById("manga-tab-content");
            const btnViewer = document.getElementById("manga-tab-viewer");
            const btnPending = document.getElementById("manga-tab-pending");

            btnViewer.addEventListener("click", () => {
                btnViewer.style.color = "var(--primary)"; btnViewer.style.borderBottomColor = "var(--primary)";
                btnPending.style.color = "var(--text-secondary)"; btnPending.style.borderBottomColor = "transparent";
                renderMangaViewerTab();
            });
            btnPending.addEventListener("click", () => {
                btnPending.style.color = "var(--primary)"; btnPending.style.borderBottomColor = "var(--primary)";
                btnViewer.style.color = "var(--text-secondary)"; btnViewer.style.borderBottomColor = "transparent";
                renderMangaPendingEditsTab();
                const bc = document.getElementById("manga-tab-cast");
                bc.style.color = "var(--text-secondary)"; bc.style.borderBottomColor = "transparent";
            });
            const btnCast = document.getElementById("manga-tab-cast");
            btnCast.addEventListener("click", () => {
                btnCast.style.color = "var(--primary)"; btnCast.style.borderBottomColor = "var(--primary)";
                btnViewer.style.color = "var(--text-secondary)"; btnViewer.style.borderBottomColor = "transparent";
                btnPending.style.color = "var(--text-secondary)"; btnPending.style.borderBottomColor = "transparent";
                const ba = document.getElementById("manga-tab-agent");
                ba.style.color = "var(--text-secondary)"; ba.style.borderBottomColor = "transparent";
                renderCastTab(mangaTabContent);
            });
            const btnAgent = document.getElementById("manga-tab-agent");
            btnAgent.addEventListener("click", () => {
                btnAgent.style.color = "var(--primary)"; btnAgent.style.borderBottomColor = "var(--primary)";
                [btnViewer, btnPending, btnCast].forEach(b => { b.style.color = "var(--text-secondary)"; b.style.borderBottomColor = "transparent"; });
                renderAgentTab();
            });

            // ── 🤖 Agent tab: explicit owner-controlled start/stop with a
            // live process view. The agent NEVER starts by itself - the
            // only triggers are this button and the API call behind it.
            let _agentPollTimer = null;
            let _agentWeStoppedLlama = false;

            async function renderAgentTab() {
                if (_agentPollTimer) { clearInterval(_agentPollTimer); _agentPollTimer = null; }
                // Friendly premium state instead of a 403 alert on the
                // start button - the clumsiest user must never hit a
                // dead-end error for a feature they simply don't have.
                try {
                    const pr = await fetch('/api/support/profile', { cache: 'no-store' });
                    if (pr.ok) {
                        const pd = await pr.json();
                        if (!(pd.entitlements && pd.entitlements.length)) {
                            mangaTabContent.innerHTML = `
                                <div class="glass-card" style="padding:1.2rem; text-align:center;">
                                    <div style="font-size:2rem;">💎</div>
                                    <h3>Агент пошуку проблем — розширена можливість</h3>
                                    <p style="color:var(--text-secondary); max-width:480px; margin:0.5rem auto;">
                                        ШІ-агент сам знаходить проблемні місця перекладу манґи
                                        (тексти, що накладаються; спотворені рамки) і пропонує
                                        виправлення — а ви лише підтверджуєте.
                                    </p>
                                    <a href="https://t.me/GetVydraBot" target="_blank" class="nav-btn"
                                       style="display:inline-block; margin-top:0.6rem; padding:0.5rem 1.2rem; background:var(--primary); color:#fff; text-decoration:none;">
                                        Активувати в @GetVydraBot (/premium)</a>
                                </div>`;
                            return;
                        }
                    }
                } catch (e) { /* on profile failure fall through to the normal tab */ }
                mangaTabContent.innerHTML = `
                    <div class="glass-card" style="padding:1rem;">
                        <h3 style="margin-top:0;">🤖 Агент пошуку проблем перекладу (розширені можливості)</h3>
                        <div id="agent-status-line" style="color:var(--text-secondary); margin-bottom:0.6rem;">Перевірка стану...</div>
                        <div id="agent-progress-wrap" style="display:none; margin-bottom:0.8rem;">
                            <div style="display:flex; justify-content:space-between; font-size:0.78rem; color:var(--text-secondary); margin-bottom:0.25rem;">
                                <span>Опрацьовано кейсів</span>
                                <span id="agent-progress-label">0 / 0</span>
                            </div>
                            <div style="background:rgba(255,255,255,0.06); border-radius:6px; height:10px; overflow:hidden;">
                                <div id="agent-progress-fill" style="background:var(--primary); height:100%; width:0%; transition:width 0.4s ease;"></div>
                            </div>
                        </div>
                        <div style="font-size:0.85rem; color:var(--text-secondary); margin-bottom:0.8rem;">
                            Агент запускається <b>тільки цією кнопкою</b> - ніколи сам. Аналіз навантажує
                            процесор (рекомендоване активне охолодження) і потребує зупинки сервера
                            перекладу на час роботи - після завершення сервер повернеться автоматично.
                        </div>
                        <div style="display:flex; gap:0.6rem; align-items:center; flex-wrap:wrap;">
                            <label style="font-size:0.85rem; color:var(--text-secondary);">Кейсів за раз:
                                <input type="number" id="agent-limit" value="8" min="1" max="20" style="width:64px; background:#18181b; color:#fff; border:1px solid var(--border-color); border-radius:6px; padding:0.3rem; margin-left:0.3rem;">
                            </label>
                            <div style="display:flex; align-items:center; gap:0.3rem; border:1px solid var(--border-color); border-radius:6px; padding:0.2rem 0.4rem;">
                                <span style="font-size:0.8rem; color:var(--text-secondary);">з:</span>
                                <input type="number" id="agent-page-start" placeholder="початок" style="width:60px; font-size:0.85rem; padding:0.1rem 0.2rem; background:transparent; border:none; border-bottom:1px solid var(--border-color); color:var(--text-primary);">
                                <span style="font-size:0.8rem; color:var(--text-secondary);">по:</span>
                                <input type="number" id="agent-page-end" placeholder="кінець" style="width:60px; font-size:0.85rem; padding:0.1rem 0.2rem; background:transparent; border:none; border-bottom:1px solid var(--border-color); color:var(--text-primary);">
                            </div>
                            <button class="nav-btn" id="agent-start-btn" style="padding:0.5rem 1.1rem; background:var(--primary); color:#fff;" onclick="startAgentScan(this)">▶️ Запустити пошук</button>
                            <button class="nav-btn" id="agent-stop-btn" style="padding:0.5rem 1.1rem; background:#7f1d1d; color:#fff; display:none;" onclick="stopAgentScan(this)">⏹ Зупинити</button>
                        </div>
                        <pre id="agent-log" style="background:#0b0b12; border:1px solid var(--border-color); border-radius:8px; padding:0.7rem; margin-top:0.9rem; font-size:0.76rem; max-height:320px; overflow-y:auto; white-space:pre-wrap; display:none;"></pre>
                    </div>`;
                refreshAgentStatus();
                _agentPollTimer = setInterval(refreshAgentStatus, 5000);
            }

            async function refreshAgentStatus() {
                const line = document.getElementById("agent-status-line");
                if (!line) { if (_agentPollTimer) { clearInterval(_agentPollTimer); _agentPollTimer = null; } return; }
                try {
                    const r = await fetch(`/api/agent-editor/status/${slug}`, { cache: "no-store" });
                    const s = await r.json();
                    const startBtn = document.getElementById("agent-start-btn");
                    const stopBtn = document.getElementById("agent-stop-btn");
                    const logEl = document.getElementById("agent-log");
                    const progWrap = document.getElementById("agent-progress-wrap");
                    const progFill = document.getElementById("agent-progress-fill");
                    const progLabel = document.getElementById("agent-progress-label");
                    if (progWrap) {
                        if (s.case_total) {
                            progWrap.style.display = "";
                            const pct = Math.min(100, Math.round((s.case_done / s.case_total) * 100));
                            progFill.style.width = pct + "%";
                            progLabel.textContent = `${s.case_done} / ${s.case_total}`;
                        } else if (!s.running) {
                            progWrap.style.display = "none";
                        }
                    }
                    line.innerHTML = (s.running
                        ? '<span style="color:#f0b429;">⏳ Агент працює - аналізує сторінки...</span>'
                        : '<span style="color:#22c55e;">🟢 Агент не запущений.</span>')
                        + ` Позначених проблем: <b>${s.flagged}</b> · пропозицій агента в черзі: <b>${s.agent_pending}</b>`
                        + (s.llama_running ? ' · сервер перекладу: 🟢' : ' · сервер перекладу: ⚪ зупинений');
                    if (startBtn) startBtn.style.display = s.running ? "none" : "";
                    if (stopBtn) stopBtn.style.display = s.running ? "" : "none";
                    if (logEl && s.log && s.log.length) {
                        logEl.style.display = "";
                        const atBottom = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 20;
                        logEl.textContent = s.log.join("\n");
                        if (atBottom) logEl.scrollTop = logEl.scrollHeight;
                    }
                    if (!s.running && _agentWeStoppedLlama) {
                        _agentWeStoppedLlama = false;
                        fetch("/api/models/start", { method: "POST" }).catch(() => {});
                    }
                } catch (e) { line.textContent = "Не вдалося отримати стан агента."; }
            }

            window.startAgentScan = async (btn, options = {}) => {
                const limit = options.limit !== undefined ? options.limit : (parseInt(document.getElementById("agent-limit").value) || 8);
                const pageStartVal = options.page_start !== undefined ? options.page_start : (document.getElementById("agent-page-start") ? document.getElementById("agent-page-start").value : "");
                const pageEndVal = options.page_end !== undefined ? options.page_end : (document.getElementById("agent-page-end") ? document.getElementById("agent-page-end").value : "");
                const ok = confirm("Запустити агента?\n\n• Якщо модель перекладу зараз завантажена в пам'яті - її тимчасово зупинимо (увімкнеться назад автоматично)\n• Телефон буде під навантаженням - бажане активне охолодження\n• Кожна пропозиція чекатиме вашого підтвердження, нічого не застосується саме");
                if (!ok) return;
                btn.disabled = true;
                try {
                    const st = await (await fetch(`/api/agent-editor/status/${slug}`, { cache: "no-store" })).json();
                    if (st.llama_running) {
                        await fetch("/api/models/stop", { method: "POST" });
                        _agentWeStoppedLlama = true;
                        await new Promise(res => setTimeout(res, 2500));
                    }
                    const payload = { limit };
                    if (pageStartVal) payload.page_start = parseInt(pageStartVal, 10);
                    if (pageEndVal) payload.page_end = parseInt(pageEndVal, 10);
                    const r = await fetch(`/api/agent-editor/scan/${slug}`, {
                        method: "POST", headers: { "Content-Type": "application/json" },
                        body: JSON.stringify(payload) });
                    const d = await r.json();
                    if (!r.ok) throw new Error(d.message);
                } catch (err) {
                    alert("Не вдалося запустити: " + err.message);
                    if (_agentWeStoppedLlama) { _agentWeStoppedLlama = false; fetch("/api/models/start", { method: "POST" }).catch(() => {}); }
                } finally {
                    btn.disabled = false;
                    refreshAgentStatus();
                }
            };

            window.stopAgentScan = async (btn) => {
                btn.disabled = true;
                try {
                    await fetch(`/api/agent-editor/stop/${slug}`, { method: "POST" });
                } finally {
                    btn.disabled = false;
                    refreshAgentStatus();
                }
            };

            let activeState = "processed";
            let currentPageBubbles = [];
            let selectedBubbleId = null;
            // TASK-36: manual geometry/font-size editing state - all in the
            // selected bubble's OWN reference pixel space (bbox_ref_size),
            // not CSS display pixels. null/cleared whenever no bubble is
            // selected (see closeBubblePanel).
            let editingBubbleBox = null;      // {x1, y1, x2, y2}
            let editingBubbleRefSize = null;  // [w, h]
            let editingFontSize = null;       // int px, or null = auto
            let editingPanelSnapshot = null;  // {text, box, fontSize} at open time - unsaved-edit guard

            async function renderMangaViewerTab() {
                let enableAgentEditor = false;
                try {
                    const settingsRes = await fetch(`/api/book-settings/${slug}`);
                    if (settingsRes.ok) {
                        const settingsData = await settingsRes.json();
                        enableAgentEditor = settingsData.enable_agent_editor;
                    }
                } catch (e) {
                    console.error("Failed to load book settings", e);
                }

                mangaTabContent.innerHTML = `
                    <div class="unified-viewer">
                        <div class="viewer-viewport" id="viewer-viewport"></div>

                        <div class="unified-controls">
                            <button class="nav-btn" id="btn-prev">← Назад</button>

                            <div class="segmented-control" id="viewer-state-selector">
                                <button class="segment-btn active" data-state="original">Original</button>
                                <button class="segment-btn" data-state="cleaned">Cleaned</button>
                                <button class="segment-btn" data-state="processed">Translated</button>
                            </div>

                            <button class="nav-btn" id="btn-next">Вперед →</button>

                            <div style="display:flex; align-items:center; gap:0.4rem; margin-left:0.6rem;">
                                <input type="number" id="manga-page-jump" min="1" max="${bookData.source_pages.length}" placeholder="#" style="width: 3.5rem; padding: 0.4rem 0.5rem; font-size: 0.85rem; background: #18181b; color: white; border: 1px solid var(--border-color); border-radius: 6px; text-align: center;">
                                <button class="nav-btn" id="manga-jump-btn" style="padding: 0.4rem 0.8rem; font-size: 0.85rem;">Go</button>
                            </div>

                            ${enableAgentEditor ? `
                            <button class="nav-btn" id="viewer-agent-btn" style="background:var(--primary); color:#fff; display:flex; align-items:center; gap:0.3rem; margin-left:0.6rem; font-size:0.85rem; padding:0.4rem 0.8rem;">
                                🤖 Агент на цю сторінку
                            </button>
                            ` : ''}
                        </div>
                        <div class="unified-status" id="page-indicator">Сторінка ...</div>
                        <div id="manga-regen-bar" style="display:none; justify-content:center; margin-top:0.5rem;">
                            <button class="nav-btn" id="manga-regen-btn" style="background:var(--primary); color:#fff; padding:0.6rem 1.2rem;">🔄 Regenerate page</button>
                        </div>
                        <div id="manga-bubble-panel" style="display:none;"></div>
                    </div>
                `;
                wireMangaViewer();
                showPage(currentMangaPage);

                if (enableAgentEditor) {
                    const viewAgentBtn = document.getElementById("viewer-agent-btn");
                    if (viewAgentBtn) {
                        viewAgentBtn.addEventListener("click", () => {
                            const pageNum = currentMangaPage + 1;
                            window.startAgentScan(viewAgentBtn, { limit: 20, page_start: pageNum, page_end: pageNum });
                        });
                    }
                }
            }

            const updateSegments = () => {
                document.querySelectorAll(".segment-btn").forEach(btn => {
                    if (btn.dataset.state === activeState) {
                        btn.classList.add("active");
                    } else {
                        btn.classList.remove("active");
                    }
                });
            };

            const showPage = (idx) => {
                currentMangaPage = idx;
                const srcFile = bookData.source_pages[idx];
                const cleanFile = bookData.cleaned_pages && bookData.cleaned_pages.length > idx ? bookData.cleaned_pages[idx] : null;
                const tgtFile = bookData.translated_pages && bookData.translated_pages.length > idx ? bookData.translated_pages[idx] : null;

                const viewport = document.getElementById("viewer-viewport");
                
                let imageUrl = "";
                let titleText = "";
                let isEmpty = false;

                if (activeState === "original") {
                    imageUrl = `/api/preview/manga-file/${slug}/source/${srcFile}`;
                    titleText = `Original: ${srcFile}`;
                } else if (activeState === "cleaned") {
                    if (cleanFile) {
                        imageUrl = `/api/preview/manga-file/${slug}/cleaned/${cleanFile}`;
                        titleText = `Cleaned: ${cleanFile}`;
                    } else {
                        isEmpty = true;
                    }
                } else if (activeState === "processed") {
                    if (tgtFile) {
                        imageUrl = `/api/preview/manga-file/${slug}/translated/${tgtFile}`;
                        titleText = `Translated: ${tgtFile}`;
                    } else {
                        isEmpty = true;
                    }
                }

                closeBubblePanel();
                document.getElementById("manga-regen-bar").style.display = "none";

                if (isEmpty) {
                    viewport.innerHTML = `
                        <div style="display:flex; flex-direction:column; justify-content:center; align-items:center; height:50vh; color:var(--text-secondary)">
                            <div>Не знайдено файл стадії "${activeState}"</div>
                            <div style="font-size:0.8rem; margin-top:0.5rem">Запустіть конвеєр перекладу манги</div>
                        </div>
                    `;
                } else if (activeState === "processed") {
                    // TASK-21: clickable bubble overlay only on the
                    // Translated view - the wrapper below is sized exactly
                    // to the rendered <img> (inline-block) so absolutely
                    // positioned bubble divs inside it line up correctly.
                    viewport.innerHTML = `
                        <div class="viewport-image-wrapper">
                            <div id="bubble-overlay-container" style="position:relative; display:inline-block; line-height:0;">
                                <img class="viewport-image" id="manga-viewport-img" src="${imageUrl}" alt="${titleText}">
                            </div>
                        </div>
                    `;
                    const imgEl = document.getElementById("manga-viewport-img");
                    const onReady = () => loadBubblesOverlay(slug, tgtFile);
                    if (imgEl.complete) onReady(); else imgEl.addEventListener("load", onReady);
                } else {
                    viewport.innerHTML = `
                        <div class="viewport-image-wrapper">
                            <img class="viewport-image" src="${imageUrl}" alt="${titleText}">
                        </div>
                    `;
                }

                document.getElementById("page-indicator").textContent = `Сторінка ${idx + 1} з ${bookData.source_pages.length} | ${activeState.toUpperCase()}`;
                document.getElementById("btn-prev").disabled = idx === 0;
                document.getElementById("btn-next").disabled = idx === bookData.source_pages.length - 1;

                updateSegments();
            };

            // TASK-21: bubble quality-flag color coding, per the doc's
            // thresholds - red = auto-fix didn't fully resolve it, yellow
            // = borderline, none = clean.
            const bubbleFlagColor = (qf) => {
                if (!qf || Object.keys(qf).length === 0) return null;
                // TASK-36: box_overlap is a distinct blue so it's visually
                // separable from the existing red/orange render-geometry
                // flags - a bubble can carry both at once.
                if (qf.box_overlap) return "#3b82f6";
                if (qf.overflow_ratio > 1.0 || qf.hit_min_size) return "#ef4444";
                if (qf.overflow_ratio >= 0.9) return "#f59e0b";
                return null;
            };

            async function loadBubblesOverlay(bookSlug, pageFilename) {
                try {
                    const res = await fetch(`/api/preview/manga-bubbles/${bookSlug}/${pageFilename}`);
                    if (!res.ok) { currentPageBubbles = []; renderBubbleOverlayDivs(); return; }
                    const data = await res.json();
                    currentPageBubbles = data.bubbles || [];
                } catch (err) {
                    currentPageBubbles = [];
                }
                renderBubbleOverlayDivs();
                refreshRegenerateButton();
            }

            function renderBubbleOverlayDivs() {
                const container = document.getElementById("bubble-overlay-container");
                const img = document.getElementById("manga-viewport-img");
                if (!container || !img || !img.naturalWidth) return;

                container.querySelectorAll(".bubble-hotspot").forEach(el => el.remove());

                currentPageBubbles.forEach(b => {
                    const [x1, y1, x2, y2] = b.bbox;
                    // TASK-26: bbox is recorded in the pixel space of the
                    // page image bbox was computed against (bbox_ref_size),
                    // NOT necessarily this <img>'s own natural size -
                    // cleaned/translated files are frequently downscaled
                    // relative to that reference (confirmed 156/193 pages
                    // on frieren). Scale against bbox_ref_size when present;
                    // fall back to the old (may drift) 1:1-with-displayed-
                    // image behavior only for entries written before this fix.
                    const refW = (b.bbox_ref_size && b.bbox_ref_size[0]) || img.naturalWidth;
                    const refH = (b.bbox_ref_size && b.bbox_ref_size[1]) || img.naturalHeight;
                    const scaleX = img.clientWidth / refW;
                    const scaleY = img.clientHeight / refH;
                    // TASK-25: a backfilled bubble was never checked by
                    // TASK-20's post_render_check (quality_flags is null,
                    // not "checked and clean") - grey it distinctly instead
                    // of letting it fall through to bubbleFlagColor's
                    // no-flags-found path, which looks identical to genuinely clean.
                    const color = b.backfilled ? "rgba(148,163,184,0.6)" : bubbleFlagColor(b.quality_flags);
                    const div = document.createElement("div");
                    div.className = "bubble-hotspot";
                    div.style.cssText = `position:absolute; left:${x1 * scaleX}px; top:${y1 * scaleY}px; width:${(x2 - x1) * scaleX}px; height:${(y2 - y1) * scaleY}px; border:2px solid ${color || 'rgba(139,92,246,0.5)'}; border-radius:4px; cursor:pointer; box-sizing:border-box; transition:background-color 120ms;`;
                    div.title = b.translated_text;
                    div.addEventListener("mouseenter", () => { div.style.backgroundColor = "rgba(139,92,246,0.15)"; });
                    div.addEventListener("mouseleave", () => { div.style.backgroundColor = "transparent"; });
                    div.addEventListener("click", () => selectBubble(b.id));
                    container.appendChild(div);
                });
            }
            window.addEventListener("resize", () => {
                if (document.getElementById("bubble-overlay-container")) renderBubbleOverlayDivs();
                if (editingBubbleBox) renderResizeHandles();
            });

            function selectBubble(bubbleId) {
                selectedBubbleId = bubbleId;
                const bubble = currentPageBubbles.find(b => b.id === bubbleId);
                if (!bubble) return;

                const img = document.getElementById("manga-viewport-img");
                const panel = document.getElementById("manga-bubble-panel");
                const [x1, y1, x2, y2] = bubble.bbox;

                // TASK-26: drawImage's source rect must be in THIS <img>'s
                // own natural pixel space, but bbox is in bbox_ref_size's
                // space (frequently a different, larger pre-downscale
                // image) - scale before cropping, or the crop silently
                // reads the wrong region (the reported "crop looks shifted"
                // bug). Fall back to 1:1 only for pre-fix entries.
                const cropRefW = (bubble.bbox_ref_size && bubble.bbox_ref_size[0]) || img.naturalWidth;
                const cropRefH = (bubble.bbox_ref_size && bubble.bbox_ref_size[1]) || img.naturalHeight;
                const cropScaleX = img.naturalWidth / cropRefW;
                const cropScaleY = img.naturalHeight / cropRefH;
                const cx1 = x1 * cropScaleX, cy1 = y1 * cropScaleY;
                const cw = (x2 - x1) * cropScaleX, ch = (y2 - y1) * cropScaleY;

                // Crop the bubble region from the already-loaded <img> via
                // canvas - no extra network request needed for the preview.
                const canvas = document.createElement("canvas");
                canvas.width = cw;
                canvas.height = ch;
                const ctx = canvas.getContext("2d");
                try { ctx.drawImage(img, cx1, cy1, cw, ch, 0, 0, cw, ch); } catch (e) {}
                const cropDataUrl = canvas.toDataURL();

                const qf = bubble.quality_flags || {};
                let qfText;
                if (bubble.backfilled) {
                    // TASK-25: quality_flags is null for backfilled bubbles
                    // by design - never approximate it, say so plainly.
                    qfText = "Ця бульбашка відновлена після факту (TASK-25 backfill) — TASK-20 автофікс ніколи не перевіряв її реальний рендер, тому статус якості невідомий.";
                } else {
                    qfText = "Автофікс не знайшов проблем із цією бульбашкою.";
                    if (Object.keys(qf).length > 0) {
                        const parts = [];
                        if (qf.overflow_ratio > 1.0) parts.push(`текст виходить за межі бульбашки на ${Math.round((qf.overflow_ratio - 1) * 100)}%`);
                        if (qf.hit_min_size) parts.push(`шрифт на мінімальному розмірі ${qf.chosen_size}px`);
                        if (qf.box_overlap) parts.push(`⚠️ накладається на ${qf.overlapping_with.length} сусідн${qf.overlapping_with.length === 1 ? 'ю бульбашку' : 'і бульбашки'} (IoU ${qf.iou}) — розсуньте вручну нижче`);
                        qfText = parts.join("; ") || "Позначено автофіксом як межовий випадок.";
                    }
                }

                // TASK-36: local editing state for manual geometry/font-size
                // override - a plain copy of the bubble's current box in its
                // OWN reference pixel space (bubble.bbox_ref_size), not CSS
                // display pixels. Drag handles and the numeric fields below
                // both read/write this same object, then renderResizeHandles()
                // converts it to CSS pixels for on-screen positioning.
                editingBubbleBox = { x1, y1, x2, y2 };
                editingBubbleRefSize = bubble.bbox_ref_size || [img.naturalWidth, img.naturalHeight];
                editingFontSize = qf.chosen_size || null;

                panel.style.display = "block";
                panel.innerHTML = `
                    <div class="paragraph-card" style="margin-top:1rem;">
                        <div class="card-meta">
                            <span>Бульбашка ${bubble.id}</span>
                            <button class="nav-btn" style="padding:0.25rem 0.6rem; font-size:0.78rem;" onclick="closeBubblePanel()">✕ Закрити</button>
                        </div>
                        <div style="text-align:center; margin-bottom:0.8rem;">
                            <img src="${cropDataUrl}" style="max-width:100%; border-radius:6px; border:1px solid var(--border-color);">
                        </div>
                        <div class="stage-box" style="margin-bottom:0.6rem;">
                            <div class="stage-title">Оригінал (OCR, read-only)</div>
                            <div class="stage-content">${escapeHtml(bubble.original_text)}</div>
                        </div>
                        <label style="font-size:0.8rem; color:var(--text-secondary); display:block; margin-bottom:0.3rem;">Переклад</label>
                        <textarea id="bubble-edit-textarea" class="edit-textarea" style="width:100%; min-height:80px; box-sizing:border-box; background:#18181b; color:#fff; border:1px solid var(--border-color); border-radius:6px; padding:0.5rem; font-family:inherit;">${escapeHtml(bubble.translated_text)}</textarea>
                        <div style="font-size:0.8rem; color:var(--text-secondary); margin:0.5rem 0;">📊 ${qfText}</div>
                        <div style="display:flex; align-items:center; gap:0.6rem;">
                            <button class="nav-btn" style="padding:0.4rem 0.9rem;" onclick="saveBubbleEdit('${slug}', '${jsAttrEscape(tgtFileForPanel())}')">💾 Save</button>
                            <span id="bubble-edit-status" style="font-size:0.8rem; color:var(--text-secondary);"></span>
                        </div>

                        <div class="stage-box" style="margin:1rem 0 0.6rem;">
                            <div class="stage-title">📐 Геометрія та розмір шрифта (TASK-36)</div>
                            <div style="font-size:0.78rem; color:var(--text-secondary); margin:0.3rem 0 0.6rem;">Перетягніть рамку на зображенні (кути/сторони = розмір, середина = переміщення) або введіть точні координати нижче.</div>
                            <div style="display:grid; grid-template-columns:1fr 1fr; gap:0.5rem;">
                                <label style="font-size:0.75rem; color:var(--text-secondary);">x1<input type="number" id="bbox-field-x1" value="${x1}" style="width:100%; box-sizing:border-box; background:#18181b; color:#fff; border:1px solid var(--border-color); border-radius:4px; padding:0.35rem;"></label>
                                <label style="font-size:0.75rem; color:var(--text-secondary);">y1<input type="number" id="bbox-field-y1" value="${y1}" style="width:100%; box-sizing:border-box; background:#18181b; color:#fff; border:1px solid var(--border-color); border-radius:4px; padding:0.35rem;"></label>
                                <label style="font-size:0.75rem; color:var(--text-secondary);">x2<input type="number" id="bbox-field-x2" value="${x2}" style="width:100%; box-sizing:border-box; background:#18181b; color:#fff; border:1px solid var(--border-color); border-radius:4px; padding:0.35rem;"></label>
                                <label style="font-size:0.75rem; color:var(--text-secondary);">y2<input type="number" id="bbox-field-y2" value="${y2}" style="width:100%; box-sizing:border-box; background:#18181b; color:#fff; border:1px solid var(--border-color); border-radius:4px; padding:0.35rem;"></label>
                            </div>
                            <label style="font-size:0.75rem; color:var(--text-secondary); display:block; margin-top:0.5rem;">Розмір шрифта (px, порожньо = авто)
                                <div style="display:flex; align-items:center; gap:0.4rem; margin-top:0.2rem;">
                                    <button class="nav-btn" style="padding:0.3rem 0.6rem;" onclick="adjustFontSize(-2)">−</button>
                                    <input type="number" id="bbox-field-font-size" value="${editingFontSize ?? ''}" placeholder="авто" style="width:80px; text-align:center; box-sizing:border-box; background:#18181b; color:#fff; border:1px solid var(--border-color); border-radius:4px; padding:0.35rem;">
                                    <button class="nav-btn" style="padding:0.3rem 0.6rem;" onclick="adjustFontSize(2)">+</button>
                                </div>
                            </label>
                            <div style="display:flex; align-items:center; gap:0.6rem; margin-top:0.7rem;">
                                <button class="nav-btn" style="padding:0.4rem 0.9rem; background:var(--primary); color:#fff;" onclick="saveBubbleGeometry('${slug}', '${jsAttrEscape(tgtFileForPanel())}')">💾 Зберегти позицію</button>
                                <span id="bbox-edit-status" style="font-size:0.8rem; color:var(--text-secondary);"></span>
                            </div>
                        </div>
                    </div>
                `;
                wireGeometryNumericFields();
                renderResizeHandles();
                const liveTa = document.getElementById("bubble-edit-textarea");
                if (liveTa) liveTa.addEventListener("input", renderLiveTextPreview);
                const liveFs = document.getElementById("bbox-field-font-size");
                if (liveFs) liveFs.addEventListener("input", renderLiveTextPreview);
                panel.scrollIntoView({ behavior: "smooth", block: "nearest" });

                // Real incident risk: closeBubblePanel() used to discard
                // in-progress edits with zero warning (typed translation
                // text or a dragged bbox, closed by mis-tap or navigating
                // away). Snapshot the opening state here so close can diff
                // against it and confirm before throwing work away.
                editingPanelSnapshot = {
                    text: liveTa ? liveTa.value : null,
                    box: editingBubbleBox ? { ...editingBubbleBox } : null,
                    fontSize: editingFontSize,
                };
            }

            function tgtFileForPanel() {
                return bookData.translated_pages[currentMangaPage];
            }

            window.closeBubblePanel = (skipConfirm) => {
                if (!skipConfirm && editingPanelSnapshot) {
                    const liveTa = document.getElementById("bubble-edit-textarea");
                    const curText = liveTa ? liveTa.value : null;
                    const boxChanged = JSON.stringify(editingBubbleBox) !== JSON.stringify(editingPanelSnapshot.box);
                    const fontChanged = editingFontSize !== editingPanelSnapshot.fontSize;
                    const textChanged = curText !== editingPanelSnapshot.text;
                    if (boxChanged || fontChanged || textChanged) {
                        if (!confirm("Є незбережені зміни (переклад, позиція або розмір шрифта) — закрити без збереження?")) {
                            return;
                        }
                    }
                }
                selectedBubbleId = null;
                editingBubbleBox = null;
                editingBubbleRefSize = null;
                editingFontSize = null;
                editingPanelSnapshot = null;
                clearResizeHandles();
                const panel = document.getElementById("manga-bubble-panel");
                if (panel) { panel.style.display = "none"; panel.innerHTML = ""; }
            };

            // TASK-36: 8 resize handles (4 corners + 4 edge-midpoints) drawn
            // over the selected bubble's box, plus a draggable body for
            // moving the whole box. Handle names follow compass points -
            // each one knows which edge(s) of editingBubbleBox it controls.
            const RESIZE_HANDLES = [
                { name: "nw", edges: ["x1", "y1"] }, { name: "n", edges: ["y1"] }, { name: "ne", edges: ["x2", "y1"] },
                { name: "w",  edges: ["x1"] },                                     { name: "e",  edges: ["x2"] },
                { name: "sw", edges: ["x1", "y2"] }, { name: "s", edges: ["y2"] }, { name: "se", edges: ["x2", "y2"] },
            ];
            // Mobile-critical: the visible frame is thin, but the actual
            // pointer-catching target is this size (px) regardless -
            // matches the task's own 32-40px minimum tap-target requirement.
            const HANDLE_TAP_SIZE = 36;

            function clearResizeHandles() {
                const container = document.getElementById("bubble-overlay-container");
                if (container) container.querySelectorAll(".bbox-edit-handle, .bbox-edit-body, .bbox-live-preview").forEach(el => el.remove());
            }

            // Live "як то буде" preview while editing: the textarea's
            // current text rendered INSIDE the bubble zone on the page
            // image - translucent violet so the underlying render stays
            // visible. Repositioned on every drag/resize (called from
            // renderResizeHandles) and re-rendered on every keystroke.
            function renderLiveTextPreview() {
                const container = document.getElementById("bubble-overlay-container");
                const scale = _geometryScale();
                if (!container || !scale || !editingBubbleBox) return;
                container.querySelectorAll(".bbox-live-preview").forEach(el => el.remove());
                const ta = document.getElementById("bubble-edit-textarea");
                if (!ta) return;
                const { scaleX, scaleY } = scale;
                const { x1, y1, x2, y2 } = editingBubbleBox;
                const fsField = document.getElementById("bbox-field-font-size");
                const refFont = (fsField && parseInt(fsField.value)) || editingFontSize || 24;
                const el = document.createElement("div");
                el.className = "bbox-live-preview";
                el.style.cssText = `position:absolute; left:${x1 * scaleX}px; top:${y1 * scaleY}px;`
                    + `width:${(x2 - x1) * scaleX}px; height:${(y2 - y1) * scaleY}px;`
                    + `background:rgba(139,92,246,0.35); color:#fff; overflow:hidden;`
                    + `display:flex; align-items:center; justify-content:center; text-align:center;`
                    + `font-size:${Math.max(6, refFont * scaleY)}px; line-height:1.15;`
                    + `word-break:break-word; border-radius:4px; pointer-events:none; z-index:4;`
                    + `text-shadow:0 0 3px rgba(0,0,0,0.9);`;
                el.textContent = ta.value;
                container.appendChild(el);
            }

            function _geometryScale() {
                const img = document.getElementById("manga-viewport-img");
                if (!img || !editingBubbleRefSize) return null;
                return {
                    img,
                    scaleX: img.clientWidth / editingBubbleRefSize[0],
                    scaleY: img.clientHeight / editingBubbleRefSize[1],
                };
            }

            function renderResizeHandles() {
                clearResizeHandles();
                const container = document.getElementById("bubble-overlay-container");
                const scale = _geometryScale();
                if (!container || !scale || !editingBubbleBox) return;
                const { scaleX, scaleY } = scale;
                const { x1, y1, x2, y2 } = editingBubbleBox;
                const left = x1 * scaleX, top = y1 * scaleY;
                const w = (x2 - x1) * scaleX, h = (y2 - y1) * scaleY;

                const body = document.createElement("div");
                body.className = "bbox-edit-body";
                body.style.cssText = `position:absolute; left:${left}px; top:${top}px; width:${w}px; height:${h}px; border:2px dashed #a78bfa; border-radius:4px; box-sizing:border-box; cursor:move; background:rgba(167,139,250,0.08); touch-action:none;`;
                body.addEventListener("pointerdown", (e) => startGeometryDrag(e, "move"));
                container.appendChild(body);

                const points = {
                    nw: [left, top], n: [left + w / 2, top], ne: [left + w, top],
                    w:  [left, top + h / 2],                 e:  [left + w, top + h / 2],
                    sw: [left, top + h], s: [left + w / 2, top + h], se: [left + w, top + h],
                };
                const cursors = { nw: "nwse-resize", n: "ns-resize", ne: "nesw-resize", w: "ew-resize", e: "ew-resize", sw: "nesw-resize", s: "ns-resize", se: "nwse-resize" };

                RESIZE_HANDLES.forEach(({ name }) => {
                    const [px, py] = points[name];
                    const handle = document.createElement("div");
                    handle.className = "bbox-edit-handle";
                    handle.style.cssText = `position:absolute; left:${px - HANDLE_TAP_SIZE / 2}px; top:${py - HANDLE_TAP_SIZE / 2}px; width:${HANDLE_TAP_SIZE}px; height:${HANDLE_TAP_SIZE}px; cursor:${cursors[name]}; touch-action:none; display:flex; align-items:center; justify-content:center; z-index:5;`;
                    const dot = document.createElement("div");
                    dot.style.cssText = `width:12px; height:12px; border-radius:50%; background:#a78bfa; border:2px solid #fff; box-shadow:0 0 3px rgba(0,0,0,0.5); pointer-events:none;`;
                    handle.appendChild(dot);
                    handle.addEventListener("pointerdown", (e) => startGeometryDrag(e, name));
                    container.appendChild(handle);
                });
                renderLiveTextPreview();
            }

            let _dragState = null;
            function startGeometryDrag(e, mode) {
                e.preventDefault();
                e.stopPropagation();
                const scale = _geometryScale();
                if (!scale || !editingBubbleBox) return;
                _dragState = {
                    mode,
                    scale,
                    startClientX: e.clientX,
                    startClientY: e.clientY,
                    startBox: { ...editingBubbleBox },
                };
                e.target.setPointerCapture(e.pointerId);
                document.addEventListener("pointermove", onGeometryDragMove);
                document.addEventListener("pointerup", onGeometryDragEnd, { once: true });
            }

            function onGeometryDragMove(e) {
                if (!_dragState || !editingBubbleBox) return;
                const { mode, scale, startClientX, startClientY, startBox } = _dragState;
                // CSS-pixel delta on screen -> reference-pixel delta, since
                // editingBubbleBox lives in the bubble's own reference
                // pixel space (bbox_ref_size), not display pixels.
                const dxRef = (e.clientX - startClientX) / scale.scaleX;
                const dyRef = (e.clientY - startClientY) / scale.scaleY;

                if (mode === "move") {
                    const w = startBox.x2 - startBox.x1, h = startBox.y2 - startBox.y1;
                    editingBubbleBox = {
                        x1: startBox.x1 + dxRef, y1: startBox.y1 + dyRef,
                        x2: startBox.x1 + dxRef + w, y2: startBox.y1 + dyRef + h,
                    };
                } else {
                    const handleDef = RESIZE_HANDLES.find(h => h.name === mode);
                    const next = { ...startBox };
                    if (handleDef.edges.includes("x1")) next.x1 = startBox.x1 + dxRef;
                    if (handleDef.edges.includes("x2")) next.x2 = startBox.x2 + dxRef;
                    if (handleDef.edges.includes("y1")) next.y1 = startBox.y1 + dyRef;
                    if (handleDef.edges.includes("y2")) next.y2 = startBox.y2 + dyRef;
                    // Never let a dragged edge invert past its opposite edge -
                    // keep at least a few reference px of width/height.
                    if (next.x2 - next.x1 < 8) { if (handleDef.edges.includes("x1")) next.x1 = next.x2 - 8; else next.x2 = next.x1 + 8; }
                    if (next.y2 - next.y1 < 8) { if (handleDef.edges.includes("y1")) next.y1 = next.y2 - 8; else next.y2 = next.y1 + 8; }
                    editingBubbleBox = next;
                }
                // Live preview only - reposition the rectangle indicator and
                // handles, no text/image re-render (that's expensive and
                // only happens server-side on an actual Regenerate).
                renderResizeHandles();
                syncGeometryNumericFields();
            }

            function onGeometryDragEnd() {
                document.removeEventListener("pointermove", onGeometryDragMove);
                _dragState = null;
            }

            function syncGeometryNumericFields() {
                if (!editingBubbleBox) return;
                const ids = ["x1", "y1", "x2", "y2"];
                ids.forEach(k => {
                    const el = document.getElementById(`bbox-field-${k}`);
                    if (el) el.value = Math.round(editingBubbleBox[k]);
                });
            }

            function wireGeometryNumericFields() {
                ["x1", "y1", "x2", "y2"].forEach(k => {
                    const el = document.getElementById(`bbox-field-${k}`);
                    if (!el) return;
                    el.addEventListener("change", () => {
                        if (!editingBubbleBox) return;
                        const v = parseFloat(el.value);
                        if (!Number.isNaN(v)) editingBubbleBox = { ...editingBubbleBox, [k]: v };
                        renderResizeHandles();
                    });
                });
                const fontEl = document.getElementById("bbox-field-font-size");
                if (fontEl) {
                    fontEl.addEventListener("change", () => {
                        const v = parseInt(fontEl.value, 10);
                        editingFontSize = Number.isNaN(v) ? null : v;
                    });
                }
            }

            window.adjustFontSize = (delta) => {
                const fontEl = document.getElementById("bbox-field-font-size");
                if (!fontEl) return;
                const current = parseInt(fontEl.value, 10) || editingFontSize || 20;
                const next = Math.max(8, Math.min(200, current + delta));
                fontEl.value = next;
                editingFontSize = next;
            };

            window.saveBubbleGeometry = async (bookSlug, pageFilename) => {
                const statusEl = document.getElementById("bbox-edit-status");
                if (!editingBubbleBox || !editingBubbleRefSize) return;
                const { x1, y1, x2, y2 } = editingBubbleBox;
                const body = {
                    bubble_id: selectedBubbleId,
                    bbox: [Math.round(x1), Math.round(y1), Math.round(x2), Math.round(y2)],
                    ref_size: editingBubbleRefSize,
                };
                if (editingFontSize) body.font_size = editingFontSize;
                statusEl.textContent = "Saving...";
                try {
                    const res = await fetch(`/api/edit/manga-bbox/${bookSlug}/${pageFilename}`, {
                        method: "PUT",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify(body)
                    });
                    const data = await res.json();
                    if (!res.ok) throw new Error(data.message);
                    statusEl.textContent = "Saved as pending edit.";
                    refreshRegenerateButton();
                    // Reset the unsaved-changes baseline to what was just
                    // persisted, so closing right after a successful save
                    // doesn't falsely warn about "unsaved changes".
                    if (editingPanelSnapshot) editingPanelSnapshot.box = editingBubbleBox ? { ...editingBubbleBox } : null;
                    if (editingPanelSnapshot) editingPanelSnapshot.fontSize = editingFontSize;
                } catch (err) {
                    statusEl.textContent = "Error: " + err.message;
                }
            };

            window.saveBubbleEdit = async (bookSlug, pageFilename) => {
                const textarea = document.getElementById("bubble-edit-textarea");
                const statusEl = document.getElementById("bubble-edit-status");
                const newText = textarea.value.trim();
                if (!newText) return;
                statusEl.textContent = "Saving...";
                try {
                    const res = await fetch(`/api/edit/manga-text/${bookSlug}/${pageFilename}`, {
                        method: "PUT",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ bubble_id: selectedBubbleId, translated_text: newText })
                    });
                    const data = await res.json();
                    if (!res.ok) throw new Error(data.message);
                    statusEl.textContent = "Saved as pending edit.";
                    refreshRegenerateButton();
                    // Same baseline-reset as saveBubbleGeometry above.
                    if (editingPanelSnapshot) editingPanelSnapshot.text = newText;
                } catch (err) {
                    statusEl.textContent = "Error: " + err.message;
                }
            };

            async function refreshRegenerateButton() {
                const bar = document.getElementById("manga-regen-bar");
                if (!bar) return;
                const tgtFile = bookData.translated_pages && bookData.translated_pages[currentMangaPage];
                if (!tgtFile) { bar.style.display = "none"; return; }
                try {
                    const res = await fetch(`/api/edit/queue/${slug}?mode=manga&status=pending`);
                    const edits = await res.json();
                    const hasPending = edits.some(e => e.target_id.startsWith(`${tgtFile}#`));
                    bar.style.display = hasPending ? "flex" : "none";
                } catch (err) {
                    bar.style.display = "none";
                }
            }

            function wireMangaViewer() {
                document.getElementById("manga-regen-btn").addEventListener("click", async () => {
                    const btn = document.getElementById("manga-regen-btn");
                    const tgtFile = bookData.translated_pages[currentMangaPage];
                    // TASK-25: a backfilled page has no real quality_flags -
                    // Regenerate runs the FULL A-E pipeline on the whole
                    // page, not just the edited bubble, so warn before that
                    // surprises someone expecting a narrow single-bubble fix.
                    if (currentPageBubbles.some(b => b.backfilled)) {
                        const ok = confirm("Ця сторінка відновлена через TASK-25 backfill (без реальних quality_flags). Regenerate оновить типографію ВСІЄЇ сторінки до нового пайплайна, не тільки відредаговану бульбашку. Продовжити?");
                        if (!ok) return;
                    }
                    btn.disabled = true;
                    btn.textContent = "⏳ Regenerating... (may take up to a minute)";
                    try {
                        const res = await fetch(`/api/edit/regenerate-manga-page/${slug}/${tgtFile}`, { method: "POST" });
                        const data = await res.json();
                        if (!res.ok) throw new Error(data.message);
                        // skipConfirm=true: the regenerate call above just
                        // persisted everything server-side, so there is
                        // nothing unsaved to warn about here.
                        closeBubblePanel(true);
                        // Cache-bust the <img> src since the file changed
                        // but the filename didn't.
                        showPage(currentMangaPage);
                    } catch (err) {
                        alert("Regenerate failed: " + err.message);
                    } finally {
                        btn.disabled = false;
                        btn.textContent = "🔄 Regenerate page";
                    }
                });

                document.querySelectorAll(".segment-btn").forEach(btn => {
                    btn.addEventListener("click", () => {
                        activeState = btn.dataset.state;
                        showPage(currentMangaPage);
                    });
                });

                document.getElementById("btn-prev").addEventListener("click", () => {
                    if (currentMangaPage > 0) showPage(currentMangaPage - 1);
                });
                document.getElementById("btn-next").addEventListener("click", () => {
                    if (currentMangaPage < bookData.source_pages.length - 1) showPage(currentMangaPage + 1);
                });

                // Same page-jump pattern as the Paragraphs tab (TASK-17) -
                // manga pages are already all loaded client-side in
                // bookData, so this is a direct showPage() call, no fetch needed.
                const jumpToMangaPage = () => {
                    const input = document.getElementById("manga-page-jump");
                    const target = parseInt(input.value, 10);
                    if (!Number.isNaN(target)) {
                        const clamped = Math.min(Math.max(target, 1), bookData.source_pages.length) - 1;
                        if (clamped !== currentMangaPage) {
                            showPage(clamped);
                        }
                    }
                };
                document.getElementById("manga-jump-btn").addEventListener("click", jumpToMangaPage);
                document.getElementById("manga-page-jump").addEventListener("keydown", (e) => {
                    if (e.key === "Enter") jumpToMangaPage();
                });

                const keyHandler = (e) => {
                    if (e.key === "ArrowLeft" && currentMangaPage > 0) {
                        showPage(currentMangaPage - 1);
                    } else if (e.key === "ArrowRight" && currentMangaPage < bookData.source_pages.length - 1) {
                        showPage(currentMangaPage + 1);
                    }
                };
                document.removeEventListener("keydown", keyHandler);
                document.addEventListener("keydown", keyHandler);
            }

            async function renderMangaPendingEditsTab() {
                mangaTabContent.innerHTML = `<div style="text-align:center; padding:2rem; color:var(--text-secondary);">Завантаження...</div>`;

                let edits = [];
                let flags = [];
                try {
                    const [editsRes, flagsRes] = await Promise.all([
                        fetch(`/api/edit/queue/${slug}?mode=manga&status=pending`),
                        fetch(`/api/preview/manga-quality-flags/${slug}`)
                    ]);
                    edits = await editsRes.json();
                    const flagsData = await flagsRes.json();
                    flags = flagsData.flags || [];
                } catch (err) {
                    mangaTabContent.innerHTML = `<div style="text-align:center; padding:2rem; color:#ef4444;">Failed to load: ${err.message}</div>`;
                    return;
                }

                // Group pending edits by page (target_id = "<page>#<bubble_id>").
                const byPage = {};
                edits.forEach(e => {
                    const [page] = e.target_id.split("#");
                    (byPage[page] = byPage[page] || []).push(e);
                });

                let html = "";
                if (window._lastApprovedPage) {
                    const ap = window._lastApprovedPage;
                    const apMatch = ap.match(/- p(\d+)/);
                    const apLabel = apMatch ? `сторінку ${parseInt(apMatch[1], 10)}` : "сторінку";
                    html += `<div class="glass-card" style="padding:0.8rem 1rem; margin-bottom:1rem; border:1px solid rgba(34,197,94,.4); display:flex; align-items:center; gap:0.8rem; flex-wrap:wrap;">
                        <span style="color:#22c55e;">✅ Правку прийнято.</span>
                        <button class="nav-btn" style="padding:0.35rem 0.8rem; font-size:0.85rem; background:var(--primary); color:#fff;" onclick="viewPageResult('${jsAttrEscape(ap)}', this)">👁 Подивитись ${apLabel}</button>
                    </div>`;
                    window._lastApprovedPage = null;
                }
                if (Object.keys(byPage).length === 0) {
                    html += `<div class="glass-card" style="text-align:center; padding:2rem; color:var(--text-secondary);">Немає pending-правок для манги.</div>`;
                } else {
                    for (const [page, pageEdits] of Object.entries(byPage)) {
                        const pm = page.match(/- p(\d+)/);
                        const pageLabel = pm ? `Сторінка ${parseInt(pm[1], 10)}` : page;
                        html += `
                            <div class="paragraph-card" style="margin-bottom:1rem;">
                                <div class="card-meta">
                                    <span title="${jsAttrEscape(page)}">📄 ${pageLabel} — ${pageEdits.length} правк${pageEdits.length === 1 ? 'а' : 'и'}</span>
                                    <span style="display:flex; gap:0.4rem;">
                                        <button class="nav-btn" style="padding:0.3rem 0.7rem; font-size:0.8rem; background:var(--primary); color:#fff;" onclick="regeneratePageFromQueue('${jsAttrEscape(page)}', this)">🔄 Застосувати</button>
                                        <button class="nav-btn" style="padding:0.3rem 0.7rem; font-size:0.8rem;" onclick="viewPageResult('${jsAttrEscape(page)}', this)">👁 Результат</button>
                                    </span>
                                </div>
                                ${pageEdits.map(e => `
                                    <div style="border-top:1px dashed var(--border-color); padding-top:0.6rem; margin-top:0.6rem;">
                                        <div style="font-size:0.8rem; color:var(--text-secondary); margin-bottom:0.4rem;">${e.target_id.split("#")[1]}${e.source === 'gemma_agent' ? ' <span style="background:rgba(139,92,246,.25); color:#c4b5fd; border-radius:6px; padding:0.1rem 0.45rem; font-size:0.72rem;">🤖 запропоновано агентом</span>' : ''}</div>
                                        ${e.note ? `<div style="font-size:0.82rem; color:#fbbf24; background:rgba(251,191,36,.08); border:1px solid rgba(251,191,36,.3); border-radius:8px; padding:0.5rem 0.7rem; margin-bottom:0.5rem;">${htmlEscape(e.note)}</div>` : ''}
                                        ${typeof e.original_value === 'string' && typeof e.edited_value === 'string' ? `
                                        <div class="grid-stages">
                                            <div class="stage-box">
                                                <div class="stage-title">Before</div>
                                                <div class="stage-content">${wordDiffHtml(e.original_value, e.edited_value, false)}</div>
                                            </div>
                                            <div class="stage-box">
                                                <div class="stage-title">After</div>
                                                <div class="stage-content stage-stressed">${wordDiffHtml(e.original_value, e.edited_value, true)}</div>
                                            </div>
                                        </div>` : `
                                        <div class="grid-stages">
                                            <div class="stage-box">
                                                <div class="stage-title">Як є</div>
                                                <canvas id="peb-${e.id}" style="max-width:100%; border-radius:6px; border:1px solid var(--border-color);"></canvas>
                                            </div>
                                            <div class="stage-box">
                                                <div class="stage-title">Як буде</div>
                                                <canvas id="pea-${e.id}" style="max-width:100%; border-radius:6px; border:1px solid var(--border-color);"></canvas>
                                            </div>
                                        </div>
                                        <div style="display:grid; grid-template-columns:1fr auto; gap:0.5rem; margin-top:0.5rem; align-items:end;">
                                            <label style="font-size:0.75rem; color:var(--text-secondary);">Текст (можна поправити перед прийняттям)
                                                <textarea id="petxt-${e.id}" style="width:100%; min-height:56px; box-sizing:border-box; background:#18181b; color:#fff; border:1px solid var(--border-color); border-radius:6px; padding:0.4rem; font-family:inherit; margin-top:0.2rem;" placeholder="завантаження тексту бульбашки..."></textarea>
                                            </label>
                                            <label style="font-size:0.75rem; color:var(--text-secondary);">Шрифт, px
                                                <input type="number" id="pefs-${e.id}" value="${(e.edited_value && e.edited_value.font_size) || ''}" placeholder="авто" style="width:74px; box-sizing:border-box; background:#18181b; color:#fff; border:1px solid var(--border-color); border-radius:6px; padding:0.4rem; margin-top:0.2rem;">
                                            </label>
                                        </div>`}
                                        <div style="display:flex; gap:0.5rem; margin-top:0.5rem; flex-wrap:wrap;">
                                            <button class="nav-btn" style="padding:0.3rem 0.7rem; font-size:0.8rem;" onclick="approveMangaEdit('${e.id}', this, '${jsAttrEscape(page)}')">✅ Approve</button>
                                            ${typeof e.edited_value === 'object' ? `<button class="nav-btn" style="padding:0.3rem 0.7rem; font-size:0.8rem; background:var(--primary); color:#fff;" onclick="replacePendingEditWithMine('${e.id}', this)">💾 Замінити моєю правкою</button>` : ''}
                                            <button class="nav-btn" style="padding:0.3rem 0.7rem; font-size:0.8rem;" onclick="discardEdit('${e.id}', this)">🗑️ Discard</button>
                                        </div>
                                    </div>
                                `).join('')}
                            </div>
                        `;
                    }
                }

                // Auto-flagged subsection: bubbles TASK-20's auto-fix didn't
                // fully resolve, whether or not a human has touched them.
                html += `<h3 style="color:var(--text-secondary); font-size:0.95rem; margin:1.5rem 0 0.8rem;">⚠️ Auto-flagged (не редаговано вручну)</h3>`;
                if (flags.length === 0) {
                    html += `<div class="glass-card" style="text-align:center; padding:1.5rem; color:var(--text-secondary); font-size:0.9rem;">Немає авто-позначених проблем.</div>`;
                } else {
                    html += `<div class="glass-card" style="padding:1rem;"><table style="width:100%; font-size:0.85rem; border-collapse:collapse;">
                        <thead><tr style="color:var(--text-secondary); text-align:left;"><th style="padding:0.4rem;">Page</th><th>Reason</th><th>Overflow</th><th>Size</th></tr></thead>
                        <tbody>
                        ${flags.map(f => `
                            <tr style="border-top:1px solid var(--border-color);">
                                <td style="padding:0.4rem;">${f.page}</td>
                                <td>${f.reason === 'overflow' ? 'Overflow' : 'Min font size'}</td>
                                <td>${f.overflow_ratio}x</td>
                                <td>${f.chosen_size}px</td>
                            </tr>
                        `).join('')}
                        </tbody>
                    </table></div>`;
                }

                mangaTabContent.innerHTML = html;
                drawPendingEditVisuals(byPage);
            }

            window.approveMangaEdit = async (editId, btn, page) => {
                btn.disabled = true;
                btn.textContent = "Approving...";
                try {
                    const res = await fetch(`/api/edit/approve/${slug}/${editId}`, { method: "POST" });
                    const data = await res.json();
                    if (!res.ok) throw new Error(data.message);
                    if (page) window._lastApprovedPage = page;
                    renderMangaPendingEditsTab();
                } catch (err) {
                    alert("Approve failed: " + err.message);
                    btn.disabled = false;
                    btn.textContent = "✅ Approve";
                }
            };

            // "Де шукати результат?" - one button that bakes every
            // pending+approved change for the page (the backend now
            // includes approved edits - see the TASK-65 UX fix in app.py)
            // and jumps straight to that page in the Viewer, translated
            // state. Tolerates "nothing to regenerate" (already baked).
            window.viewPageResult = async (page, btn) => {
                const orig = btn ? btn.textContent : "";
                if (btn) { btn.disabled = true; btn.textContent = "⏳ Оновлюємо..."; }
                try {
                    const res = await fetch(`/api/edit/regenerate-manga-page/${slug}/${page}`, { method: "POST" });
                    if (!res.ok) {
                        const d = await res.json();
                        if (!/nothing to regenerate/i.test(d.message || "")) throw new Error(d.message);
                    }
                } catch (err) {
                    alert("Не вдалося оновити сторінку: " + err.message);
                    if (btn) { btn.disabled = false; btn.textContent = orig; }
                    return;
                }
                const idx = (bookData.translated_pages || []).indexOf(page);
                if (idx >= 0) currentMangaPage = idx;
                activeState = "translated";
                document.getElementById("manga-tab-viewer").click();
            };

            window.regeneratePageFromQueue = async (page, btn) => {
                // TASK-25: this tab doesn't have the target page's bubbles
                // preloaded (unlike the viewer, which uses currentPageBubbles
                // for the currently-displayed page) - fetch fresh to check.
                try {
                    const resBubbles = await fetch(`/api/preview/manga-bubbles/${slug}/${page}`);
                    if (resBubbles.ok) {
                        const bubblesData = await resBubbles.json();
                        const hasBackfilled = (bubblesData.bubbles || []).some(b => b.backfilled);
                        if (hasBackfilled) {
                            const ok = confirm("Ця сторінка відновлена через TASK-25 backfill (без реальних quality_flags). Regenerate оновить типографію ВСІЄЇ сторінки до нового пайплайна, не тільки відредаговану бульбашку. Продовжити?");
                            if (!ok) return;
                        }
                    }
                } catch (e) { /* non-fatal - if the check fails, fall through to regenerate as before */ }

                btn.disabled = true;
                btn.textContent = "⏳...";
                try {
                    const res = await fetch(`/api/edit/regenerate-manga-page/${slug}/${page}`, { method: "POST" });
                    const data = await res.json();
                    if (!res.ok) throw new Error(data.message);
                    renderMangaPendingEditsTab();
                } catch (err) {
                    alert("Regenerate failed: " + err.message);
                    btn.disabled = false;
                    btn.textContent = "🔄 Regenerate page";
                }
            };

            window.discardEdit = async (editId, btn) => {
                btn.disabled = true;
                try {
                    const res = await fetch(`/api/edit/discard/${slug}/${editId}`, { method: "POST" });
                    const data = await res.json();
                    if (!res.ok) throw new Error(data.message);
                    renderMangaPendingEditsTab();
                } catch (err) {
                    alert("Discard failed: " + err.message);
                    btn.disabled = false;
                }
            };

            // Humans parse pictures, not coordinate tuples: for every
            // geometry edit draw the REAL translated page crop twice -
            // current zone (red) vs proposed zone (green) - instead of
            // asking anyone to mentally diff two JSON arrays.
            const _pendingEditMeta = {};
            async function drawPendingEditVisuals(byPage) {
                for (const [page, pageEdits] of Object.entries(byPage)) {
                    const objEdits = pageEdits.filter(e => typeof e.edited_value === 'object' && e.edited_value !== null);
                    if (objEdits.length === 0) continue;
                    let bubbles = [];
                    try {
                        const r = await fetch(`/api/preview/manga-bubbles/${slug}/${page}`);
                        if (r.ok) bubbles = (await r.json()).bubbles || [];
                    } catch (e) { /* visuals are best-effort */ }
                    const img = new Image();
                    img.src = `/api/preview/manga-file/${slug}/translated/${page}`;
                    try { await img.decode(); } catch (e) { continue; }

                    for (const e of objEdits) {
                        const bubbleId = e.target_id.split("#")[1];
                        const bubble = bubbles.find(b => b.id === bubbleId);
                        const refSize = (e.edited_value && e.edited_value.ref_size)
                            || (bubble && bubble.bbox_ref_size)
                            || [img.naturalWidth, img.naturalHeight];
                        const sx = img.naturalWidth / refSize[0], sy = img.naturalHeight / refSize[1];
                        const oldBox = (e.original_value && e.original_value.bbox) || (bubble && bubble.bbox);
                        const newBox = (e.edited_value && e.edited_value.bbox) || oldBox;
                        if (!oldBox || !newBox) continue;
                        _pendingEditMeta[e.id] = { page, bubbleId, newBox, refSize,
                                                   bubbleText: bubble ? bubble.translated_text : null };
                        const txtEl = document.getElementById(`petxt-${e.id}`);
                        if (txtEl && bubble) txtEl.value = bubble.translated_text || "";

                        const pad = Math.round(30 / sx);
                        const ux1 = Math.max(0, Math.min(oldBox[0], newBox[0]) - pad);
                        const uy1 = Math.max(0, Math.min(oldBox[1], newBox[1]) - pad);
                        const ux2 = Math.min(refSize[0], Math.max(oldBox[2], newBox[2]) + pad);
                        const uy2 = Math.min(refSize[1], Math.max(oldBox[3], newBox[3]) + pad);
                        const cw = (ux2 - ux1) * sx, ch = (uy2 - uy1) * sy;

                        const drawCrop = (canvasId, box, color) => {
                            const canvas = document.getElementById(canvasId);
                            if (!canvas) return;
                            canvas.width = cw; canvas.height = ch;
                            const ctx = canvas.getContext("2d");
                            try {
                                ctx.drawImage(img, ux1 * sx, uy1 * sy, cw, ch, 0, 0, cw, ch);
                            } catch (err) { return; }
                            ctx.lineWidth = Math.max(3, cw / 120);
                            ctx.strokeStyle = color;
                            ctx.strokeRect((box[0] - ux1) * sx, (box[1] - uy1) * sy,
                                           (box[2] - box[0]) * sx, (box[3] - box[1]) * sy);
                        };
                        drawCrop(`peb-${e.id}`, oldBox, "#ef4444");
                        drawCrop(`pea-${e.id}`, newBox, "#22c55e");
                    }
                }
            }

            // "Замінити моєю правкою": human tweaks the agent's proposal
            // (text and/or font size) before accepting - submits through
            // the SAME endpoints as any manual edit (source=human), then
            // discards the agent's original. Result stays pending: the
            // human still approves the final version explicitly.
            window.replacePendingEditWithMine = async (editId, btn) => {
                const meta = _pendingEditMeta[editId];
                if (!meta) { alert("Метадані ще завантажуються - спробуйте за секунду."); return; }
                const fsEl = document.getElementById(`pefs-${editId}`);
                const txtEl = document.getElementById(`petxt-${editId}`);
                const fontSize = fsEl && fsEl.value ? parseInt(fsEl.value) : null;
                const newText = txtEl ? txtEl.value.trim() : "";
                btn.disabled = true;
                btn.textContent = "💾...";
                try {
                    const bboxBody = { bubble_id: meta.bubbleId, bbox: meta.newBox, ref_size: meta.refSize, source: "human" };
                    if (fontSize) bboxBody.font_size = fontSize;
                    let res = await fetch(`/api/edit/manga-bbox/${slug}/${meta.page}`, {
                        method: "PUT", headers: { "Content-Type": "application/json" },
                        body: JSON.stringify(bboxBody) });
                    let data = await res.json();
                    if (!res.ok) throw new Error(data.message);
                    if (newText && meta.bubbleText !== null && newText !== meta.bubbleText) {
                        res = await fetch(`/api/edit/manga-text/${slug}/${meta.page}`, {
                            method: "PUT", headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ bubble_id: meta.bubbleId, translated_text: newText, source: "human" }) });
                        data = await res.json();
                        if (!res.ok) throw new Error(data.message);
                    }
                    await fetch(`/api/edit/discard/${slug}/${editId}`, { method: "POST" });
                    renderMangaPendingEditsTab();
                } catch (err) {
                    alert("Не вдалося замінити: " + err.message);
                    btn.disabled = false;
                    btn.textContent = "💾 Замінити моєю правкою";
                }
            };

            renderMangaViewerTab();
        }

        let currentParagraphsPage = 1;
        let showParagraphsTabRef = null;

        async function fetchParagraphsPage(page) {
            currentParagraphsPage = page;
            const loader = document.getElementById("loader");
            if (loader) loader.style.display = "block";
            try {
                const resBook = await fetch(`/api/preview/book/${slug}?page=${page}&limit=30`);
                const data = await resBook.json();
                bookData.paragraphs = data.paragraphs;
                bookData.total_pages = data.total_pages;
                bookData.total_chunks = data.total_chunks;
            } catch (err) {
                console.error(err);
            } finally {
                if (loader) loader.style.display = "none";
            }
            // If the paragraphs list tab is still active, redraw it
            const list = document.getElementById("paragraphs-list");
            if (list && showParagraphsTabRef) {
                showParagraphsTabRef();
            }
        }

        function renderBook() {
            const area = document.getElementById("content-area");
            const isEpub = bookData.epub_available || bookData.is_epub_book;
            const hasParagraphs = bookData.paragraphs && bookData.paragraphs.length > 0;

            // For EPUB books without MD paragraphs — go straight to Full Page Viewer
            if (!hasParagraphs && isEpub) {
                renderEpubOnlyView(area);
                return;
            }

            if (!hasParagraphs && !isEpub) {
                area.innerHTML = `<div style="text-align:center;color:var(--text-secondary);padding:3rem 0;">
                    <div style="font-size:3rem;margin-bottom:1rem;">📚</div>
                    <div style="font-size:1.1rem;margin-bottom:0.5rem;">Немає даних для перегляду</div>
                    <div style="font-size:0.9rem;">Запустіть конвеєр перекладу щоб побачити результат</div>
                </div>`;
                return;
            }

            area.innerHTML = `
                <div class="tabs" style="display: flex; gap: 1rem; border-bottom: 1px solid var(--border-color); padding-bottom: 1rem; margin-bottom: 1.5rem; flex-wrap: wrap;">
                    <button class="tab-btn active" id="tab-paragraphs" style="color: var(--primary); border-bottom: 2px solid var(--primary); padding: 0.5rem 1rem;">📋 Текст та Аудіо</button>
                    <button class="tab-btn" id="tab-page-viewer" style="color: var(--text-secondary); padding: 0.5rem 1rem; border-bottom: 2px solid transparent;">📖 Перегляд сторінок</button>
                    <button class="tab-btn" id="tab-pending" style="color: var(--text-secondary); padding: 0.5rem 1rem; border-bottom: 2px solid transparent;">📝 Черга правок</button>
                    <button class="tab-btn" id="tab-cast" style="color: var(--text-secondary); padding: 0.5rem 1rem; border-bottom: 2px solid transparent;">🧬 Персонажі та Контекст</button>
                </div>
                <div id="tab-content-area"></div>
            `;

            const tabContent = document.getElementById("tab-content-area");
            const btnParagraphs = document.getElementById("tab-paragraphs");
            const btnPageViewer = document.getElementById("tab-page-viewer");
            const btnPending = document.getElementById("tab-pending");
            const btnCast = document.getElementById("tab-cast");

            const resetTabStyles = () => {
                [btnParagraphs, btnPageViewer, btnPending, btnCast].forEach(b => {
                    if (b) { b.style.color = "var(--text-secondary)"; b.style.borderBottomColor = "transparent"; }
                });
            };

            btnParagraphs.addEventListener("click", () => {
                resetTabStyles();
                btnParagraphs.style.color = "var(--primary)";
                btnParagraphs.style.borderBottomColor = "var(--primary)";
                showParagraphsTab();
            });

            btnPageViewer.addEventListener("click", () => {
                resetTabStyles();
                btnPageViewer.style.color = "var(--primary)";
                btnPageViewer.style.borderBottomColor = "var(--primary)";
                showPageViewerTab();
            });

            btnPending.addEventListener("click", () => {
                resetTabStyles();
                btnPending.style.color = "var(--primary)";
                btnPending.style.borderBottomColor = "var(--primary)";
                showParagraphsTab();
                const pendingFilterBtn = document.querySelector('.filter-btn[data-filter="pending-edits"]');
                if (pendingFilterBtn) {
                    pendingFilterBtn.click();
                }
            });

            btnCast.addEventListener("click", () => {
                resetTabStyles();
                btnCast.style.color = "var(--primary)";
                btnCast.style.borderBottomColor = "var(--primary)";
                renderCastTab(tabContent);
            });

            function showParagraphsTab() {
                showParagraphsTabRef = showParagraphsTab;
                const totalPages = bookData.total_pages || 1;
                const totalChunks = bookData.total_chunks || 0;

                tabContent.innerHTML = `
                    <div class="filters" style="display: flex; justify-content: space-between; align-items: center; gap: 1rem; margin-bottom: 1.5rem; flex-wrap: wrap;">
                        <div style="display: flex; gap: 0.5rem; flex-wrap: wrap;">
                            <button class="filter-btn active" data-filter="all">Показати все</button>
                            <button class="filter-btn" data-filter="audio">Лише з аудіо</button>
                            <button class="filter-btn" data-filter="no-audio">Лише без аудіо</button>
                            <button class="filter-btn" data-filter="pending-edits">📝 Черга правок</button>
                        </div>
                        <div class="pagination-controls" style="display: flex; align-items: center; gap: 0.8rem; background: rgba(255,255,255,0.02); padding: 0.3rem 0.6rem; border-radius: 8px; border: 1px solid var(--border-color);">
                            <button class="nav-btn" id="para-prev-btn" style="padding: 0.4rem 0.8rem; font-size: 0.85rem;" ${currentParagraphsPage <= 1 ? 'disabled' : ''}>← Попередня</button>
                            <span id="para-page-indicator" style="font-size: 0.85rem; color: var(--text-secondary); white-space: nowrap; font-family: monospace;">Сторінка ${currentParagraphsPage}/${totalPages} (${totalChunks} фрагментів)</span>
                            <button class="nav-btn" id="para-next-btn" style="padding: 0.4rem 0.8rem; font-size: 0.85rem;" ${currentParagraphsPage >= totalPages ? 'disabled' : ''}>Наступна →</button>
                            <input type="number" id="para-page-jump" min="1" max="${totalPages}" placeholder="#" style="width: 3.5rem; padding: 0.4rem 0.5rem; font-size: 0.85rem; background: #18181b; color: white; border: 1px solid var(--border-color); border-radius: 6px; text-align: center;">
                            <button class="nav-btn" id="para-jump-btn" style="padding: 0.4rem 0.8rem; font-size: 0.85rem;">Перейти</button>
                        </div>
                    </div>
                    <div class="book-stages-list" id="paragraphs-list"></div>
                `;

                const list = document.getElementById("paragraphs-list");

                const renderItems = (filter) => {
                    if (filter === "pending-edits") {
                        renderPendingEditsList();
                        return;
                    }
                    list.innerHTML = "";
                    let visibleIndex = 0;
                    bookData.paragraphs.forEach((p, idx) => {
                        if (filter === "audio" && !p.has_audio) return;
                        if (filter === "no-audio" && p.has_audio) return;

                        const absoluteIdx = (currentParagraphsPage - 1) * 30 + idx + 1;

                        const card = document.createElement("div");
                        card.className = "paragraph-card";
                        card.style = `animation: fadeInUp 400ms var(--ease-out-snappy) forwards; animation-delay: ${visibleIndex * 15}ms; opacity: 0; animation-fill-mode: forwards;`;
                        card.innerHTML = `
                            <div class="card-meta">
                                <span>Фрагмент #${absoluteIdx} | Хеш: ${p.hash.substring(0, 8)}...</span>
                                <span style="display:flex; gap:0.5rem; align-items:center;">
                                    <button class="nav-btn" style="padding:0.25rem 0.6rem; font-size:0.78rem;" onclick="toggleEditForm('${p.hash}')">✏️ Редагувати</button>
                                    <span class="badge ${p.has_audio ? 'badge-audio' : 'badge-no-audio'}">
                                        ${p.has_audio ? 'Аудіо синтезовано' : 'Без аудіо'}
                                    </span>
                                </span>
                            </div>
                            <div class="paragraph-tabs">
                                <button class="tab-btn active" onclick="switchParagraphTab(this, 'original')">Оригінал</button>
                                <button class="tab-btn" onclick="switchParagraphTab(this, 'translated')">Переклад</button>
                            </div>
                            <div class="grid-stages">
                                <div class="stage-box original-stage">
                                    <div class="stage-title">Оригінал (RU / EN)</div>
                                    <div class="stage-content">${p.original}</div>
                                </div>
                                <div class="stage-box translated-stage">
                                    <div class="stage-title">Переклад та Наголоси (UK)</div>
                                    <div class="stage-content stage-stressed">${p.stressed}</div>
                                </div>
                            </div>
                            ${p.has_audio ? `
                            <div class="audio-wrapper">
                                <span class="audio-label">🔊 Прослухати фрагмент:</span>
                                <audio controls id="audio-player-${p.hash}" src="/api/preview/audio/${slug}/${p.hash}"></audio>
                            </div>
                            ` : ''}
                            <div id="edit-form-${p.hash}" class="edit-form" style="display:none; margin-top:1rem; padding-top:1rem; border-top:1px dashed var(--border-color);">
                                <label style="font-size:0.8rem; color:var(--text-secondary); display:block; margin-bottom:0.3rem;">Перекладений текст</label>
                                <textarea id="edit-translated-${p.hash}" class="edit-textarea" style="width:100%; min-height:70px; box-sizing:border-box; background:#18181b; color:#fff; border:1px solid var(--border-color); border-radius:6px; padding:0.5rem; font-family:inherit;">${escapeHtml(p.translated)}</textarea>
                                <label style="font-size:0.8rem; color:var(--text-secondary); display:block; margin:0.6rem 0 0.3rem;">Розстановка наголосів</label>
                                <textarea id="edit-stress-${p.hash}" class="edit-textarea" style="width:100%; min-height:70px; box-sizing:border-box; background:#18181b; color:#fff; border:1px solid var(--border-color); border-radius:6px; padding:0.5rem; font-family:inherit;">${escapeHtml(p.stressed)}</textarea>
                                <div style="display:flex; align-items:center; gap:0.6rem; margin-top:0.6rem; flex-wrap:wrap;">
                                    <button class="nav-btn" style="padding:0.4rem 0.9rem;" onclick="saveEdit('${p.hash}')">Зберегти</button>
                                    <button class="nav-btn" id="regen-audio-btn-${p.hash}" style="padding:0.4rem 0.9rem; display:none;" onclick="regenerateAudio('${p.hash}')">🔊 Пересинтезувати аудіо</button>
                                    <span id="edit-status-${p.hash}" style="font-size:0.8rem; color:var(--text-secondary);"></span>
                                </div>
                            </div>
                        `;
                        list.appendChild(card);
                        visibleIndex++;
                    });
                };

                async function renderPendingEditsList() {
                    list.innerHTML = `<div style="text-align:center; padding:2rem; color:var(--text-secondary);">Loading pending edits and ASR mismatches...</div>`;
                    let edits = [];
                    let asrFlags = [];
                    try {
                        const [res1, res2] = await Promise.all([
                            fetch(`/api/edit/queue/${slug}?status=pending`),
                            fetch(`/api/preview/asr-quality-flags/${slug}`)
                        ]);
                        edits = await res1.json();
                        const flagsData = await res2.json();
                        asrFlags = flagsData.flags || [];
                    } catch (err) {
                        list.innerHTML = `<div style="text-align:center; padding:2rem; color:#ef4444;">Failed to load pending edits: ${err.message}</div>`;
                        return;
                    }
                    if (edits.length === 0 && asrFlags.length === 0) {
                        list.innerHTML = `<div style="text-align:center; padding:2rem; color:var(--text-secondary);">No pending edits or ASR mismatches.</div>`;
                        return;
                    }
                    
                    let html = '';
                    if (edits.length > 0) {
                        html += `<h3 style="margin:1rem 0 0.5rem; color:var(--text-primary);">📝 Pending Edits (${edits.length})</h3>`;
                        html += edits.map(e => `
                            <div class="paragraph-card">
                                <div class="card-meta">
                                    <span>${e.mode === 'text' ? 'Text edit' : 'Stress edit'} | Hash: ${e.target_id.substring(0, 8)}... | ${new Date(e.created_at).toLocaleString()}</span>
                                    <span class="badge badge-no-audio">${e.status}</span>
                                </div>
                                <div class="grid-stages">
                                    <div class="stage-box">
                                        <div class="stage-title">Before</div>
                                        <div class="stage-content">${wordDiffHtml(e.original_value, e.edited_value, false)}</div>
                                    </div>
                                    <div class="stage-box">
                                        <div class="stage-title">After</div>
                                        <div class="stage-content stage-stressed">${wordDiffHtml(e.original_value, e.edited_value, true)}</div>
                                    </div>
                                </div>
                                <div style="display:flex; gap:0.6rem; margin-top:0.8rem;">
                                    <button class="nav-btn" style="padding:0.4rem 0.9rem;" onclick="approveEdit('${e.id}', this)">✅ Approve</button>
                                </div>
                            </div>
                        `).join('');
                    }

                    if (asrFlags.length > 0) {
                        html += `<h3 style="margin:2rem 0 0.5rem; color:var(--text-primary);">🎙️ Auto-flagged ASR Stress Mismatches (${asrFlags.length})</h3>`;
                        html += asrFlags.map(f => {
                            const hash = f.chunk_id;
                            const b64Original = btoa(unescape(encodeURIComponent(f.original_text)));
                            return `
                            <div class="paragraph-card" id="asr-card-${hash}">
                                <div class="card-meta">
                                    <span>ASR Warning | Hash: ${hash.substring(0, 8)}... | CER: ${(f.char_error_rate * 100).toFixed(1)}% (Threshold: ${(f.cer_threshold * 100).toFixed(0)}%)</span>
                                    <span class="badge badge-no-audio" style="background:#f0b429; color:#18181b;">mismatch</span>
                                </div>
                                <div class="grid-stages">
                                    <div class="stage-box">
                                        <div class="stage-title">Expected (Original Text)</div>
                                        <div class="stage-content">${wordDiffHtml(f.original_text, f.transcribed_text, false)}</div>
                                    </div>
                                    <div class="stage-box">
                                        <div class="stage-title">Observed (ASR Transcription)</div>
                                        <div class="stage-content stage-stressed" style="color:#fca5a5;">${wordDiffHtml(f.original_text, f.transcribed_text, true)}</div>
                                    </div>
                                </div>
                                <div style="margin-top:0.8rem;">
                                    <audio controls src="/api/preview/audio/${slug}/${hash}" style="width:100%; max-height:40px; margin-bottom:0.6rem;"></audio>
                                </div>
                                
                                <div id="asr-edit-container-${hash}" style="display:none; margin-top:0.8rem; border-top:1px solid var(--border-color); padding-top:0.8rem;">
                                    <label style="font-size:0.8rem; color:var(--text-secondary); display:block; margin-bottom:0.3rem;">Stress marks:</label>
                                    <textarea id="asr-edit-input-${hash}" class="edit-textarea" style="width:100%; min-height:60px; box-sizing:border-box; background:#18181b; color:#fff; border:1px solid var(--border-color); border-radius:6px; padding:0.5rem; font-family:inherit;">${escapeHtml(f.original_text)}</textarea>
                                    <div style="display:flex; gap:0.6rem; margin-top:0.4rem;">
                                        <button class="nav-btn" style="padding:0.3rem 0.8rem;" onclick="submitAsrCorrection('${hash}', '${b64Original}')">💾 Save stress mark</button>
                                        <button class="nav-btn" style="padding:0.3rem 0.8rem; background:#3f3f46; color:#fff;" onclick="toggleAsrEditForm('${hash}')">Cancel</button>
                                    </div>
                                </div>
                                
                                <div style="display:flex; gap:0.6rem; margin-top:0.8rem;" id="asr-actions-${hash}">
                                    <button class="nav-btn" style="padding:0.4rem 0.9rem;" onclick="toggleAsrEditForm('${hash}')">✍️ Edit Stress</button>
                                    <button class="nav-btn" style="padding:0.4rem 0.9rem; background:#7f1d1d; color:#fca5a5;" onclick="discardAsrWarning('${hash}', this)">❌ Discard</button>
                                </div>
                            </div>
                            `;
                        }).join('');
                    }
                    list.innerHTML = html;
                }

                window.toggleAsrEditForm = (hash) => {
                    const el = document.getElementById(`asr-edit-container-${hash}`);
                    if (el) el.style.display = el.style.display === 'none' ? 'block' : 'none';
                    const actions = document.getElementById(`asr-actions-${hash}`);
                    if (actions) actions.style.display = actions.style.display === 'none' ? 'flex' : 'none';
                };

                window.submitAsrCorrection = async (hash, encodedText) => {
                    const originalText = decodeURIComponent(escape(atob(encodedText)));
                    const inputEl = document.getElementById(`asr-edit-input-${hash}`);
                    const newStress = inputEl.value.trim();
                    if (!newStress) {
                        alert('Stress marks are required');
                        return;
                    }
                    try {
                        const res = await fetch(`/api/edit/stress/${slug}/${hash}`, {
                            method: "PUT",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ original_stress: originalText, new_stress: newStress })
                        });
                        const data = await res.json();
                        if (!res.ok) throw new Error(data.message);
                        renderPendingEditsList();
                    } catch (err) {
                        alert('Failed to save stress: ' + err.message);
                    }
                };

                window.discardAsrWarning = async (hash, btn) => {
                    btn.disabled = true;
                    btn.textContent = 'Discarding...';
                    try {
                        const res = await fetch(`/api/edit/stress/discard/${slug}/${hash}`, { method: 'POST' });
                        if (!res.ok) {
                            const data = await res.json();
                            throw new Error(data.message);
                        }
                        renderPendingEditsList();
                    } catch (err) {
                        alert('Discard failed: ' + err.message);
                        btn.disabled = false;
                        btn.textContent = '❌ Discard';
                    }
                };

                window.approveEdit = async (editId, btn) => {
                    btn.disabled = true;
                    btn.textContent = 'Approving...';
                    try {
                        const res = await fetch(`/api/edit/approve/${slug}/${editId}`, { method: 'POST' });
                        const data = await res.json();
                        if (!res.ok) throw new Error(data.message);
                        renderPendingEditsList();
                    } catch (err) {
                        alert('Approve failed: ' + err.message);
                        btn.disabled = false;
                        btn.textContent = '✅ Approve';
                    }
                };

                window.toggleEditForm = (hash) => {
                    const el = document.getElementById(`edit-form-${hash}`);
                    if (el) el.style.display = el.style.display === 'none' ? 'block' : 'none';
                };

                window.saveEdit = async (hash) => {
                    const translatedEl = document.getElementById(`edit-translated-${hash}`);
                    const stressEl = document.getElementById(`edit-stress-${hash}`);
                    // defaultValue holds the original pre-fill text (unescaped by the
                    // browser), independent of live user edits in .value - avoids
                    // ever needing to smuggle raw book text through an onclick attr.
                    const originalTranslated = translatedEl.defaultValue;
                    const originalStress = stressEl.defaultValue;
                    const newTranslated = translatedEl.value.trim();
                    const newStress = stressEl.value.trim();
                    const statusEl = document.getElementById(`edit-status-${hash}`);
                    const regenBtn = document.getElementById(`regen-audio-btn-${hash}`);
                    statusEl.textContent = 'Saving...';
                    try {
                        let changed = false;
                        if (newTranslated && newTranslated !== originalTranslated) {
                            const res = await fetch(`/api/edit/text/${slug}/${hash}`, {
                                method: 'PUT',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ original_text: originalTranslated, new_text: newTranslated })
                            });
                            const data = await res.json();
                            if (!res.ok) throw new Error(data.message);
                            changed = true;
                        }
                        if (newStress && newStress !== originalStress) {
                            const res = await fetch(`/api/edit/stress/${slug}/${hash}`, {
                                method: 'PUT',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ original_stress: originalStress, new_stress: newStress })
                            });
                            const data = await res.json();
                            if (!res.ok) throw new Error(data.message);
                            changed = true;
                        }
                        if (changed) {
                            statusEl.textContent = 'Saved as pending edit.';
                            if (regenBtn) regenBtn.style.display = 'inline-block';
                        } else {
                            statusEl.textContent = 'No changes to save.';
                        }
                    } catch (err) {
                        statusEl.textContent = 'Error: ' + err.message;
                    }
                };

                window.regenerateAudio = async (hash) => {
                    const btn = document.getElementById(`regen-audio-btn-${hash}`);
                    const statusEl = document.getElementById(`edit-status-${hash}`);
                    btn.disabled = true;
                    btn.textContent = 'Regenerating...';
                    try {
                        const res = await fetch(`/api/edit/regenerate-audio/${slug}/${hash}`, { method: 'POST' });
                        const data = await res.json();
                        if (!res.ok) throw new Error(data.message);
                        const audioEl = document.getElementById(`audio-player-${hash}`);
                        if (audioEl) {
                            audioEl.src = `/api/preview/audio/${slug}/${data.new_hash}`;
                            audioEl.load();
                        }
                        statusEl.textContent = 'Audio regenerated - listen above, then Approve from the Pending Edits tab.';
                        btn.textContent = '✓ Regenerated';
                    } catch (err) {
                        statusEl.textContent = 'Regenerate failed: ' + err.message;
                        btn.textContent = '🔊 Regenerate audio';
                        btn.disabled = false;
                    }
                };

                const filters = document.querySelectorAll(".filter-btn");
                filters.forEach(btn => {
                    btn.addEventListener("click", (e) => {
                        filters.forEach(b => b.classList.remove("active"));
                        btn.classList.add("active");
                        renderItems(btn.dataset.filter);
                    });
                });

                document.getElementById("para-prev-btn").addEventListener("click", () => {
                    if (currentParagraphsPage > 1) {
                        fetchParagraphsPage(currentParagraphsPage - 1);
                    }
                });

                document.getElementById("para-next-btn").addEventListener("click", () => {
                    if (currentParagraphsPage < totalPages) {
                        fetchParagraphsPage(currentParagraphsPage + 1);
                    }
                });

                const jumpToPage = () => {
                    const input = document.getElementById("para-page-jump");
                    const target = parseInt(input.value, 10);
                    if (!Number.isNaN(target)) {
                        const clamped = Math.min(Math.max(target, 1), totalPages);
                        if (clamped !== currentParagraphsPage) {
                            fetchParagraphsPage(clamped);
                        }
                    }
                };
                document.getElementById("para-jump-btn").addEventListener("click", jumpToPage);
                document.getElementById("para-page-jump").addEventListener("keydown", (e) => {
                    if (e.key === "Enter") jumpToPage();
                });

                renderItems("all");
            }

            async function showPageViewerTab() {
                tabContent.innerHTML = `
                    <div class="viewer-controls" style="display: flex; align-items: center; gap: 1rem; margin-bottom: 1.5rem; background: rgba(255,255,255,0.02); padding: 0.8rem; border-radius: 8px; border: 1px solid var(--border-color);">
                        <label for="chapter-select" style="font-weight: 600; color: var(--text-secondary);">Select Page/File:</label>
                        <select id="chapter-select" class="form-select" style="background: #18181b; color: white; border: 1px solid var(--border-color); padding: 0.5rem 1rem; border-radius: 6px; flex-grow: 1; outline: none; font-family: inherit; font-size: 0.95rem; cursor: pointer; max-width: 100%; min-width: 0;">
                            <option value="">-- Loading files from EPUB... --</option>
                        </select>
                    </div>
                    
                    <div class="unified-viewer">
                        <div class="viewer-viewport" id="viewer-viewport">
                            <iframe id="viewport-iframe" class="viewport-iframe"></iframe>
                        </div>
                        
                        <div class="unified-controls">
                            <button class="nav-btn" id="btn-prev">← Попередній розділ</button>
                            
                            <div class="segmented-control" id="viewer-state-selector">
                                <button class="segment-btn active" data-state="original">Original</button>
                                <button class="segment-btn" data-state="processed">Translated</button>
                            </div>
                            
                            <button class="nav-btn" id="btn-next">Наступний розділ →</button>
                        </div>
                        <div class="unified-status" id="page-indicator">Розділ 1 з 1</div>
                    </div>
                `;

                let activeState = "processed";
                let chapters = [];
                let currentChapterIdx = 0;

                const updateSegments = () => {
                    document.querySelectorAll(".segment-btn").forEach(btn => {
                        if (btn.dataset.state === activeState) {
                            btn.classList.add("active");
                        } else {
                            btn.classList.remove("active");
                        }
                    });
                };

                const loadPageContent = async () => {
                    if (chapters.length === 0) return;
                    const href = chapters[currentChapterIdx].href;
                    const iframe = document.getElementById("viewport-iframe");
                    if (!iframe) return;

                    const doc = iframe.contentDocument || iframe.contentWindow.document;
                    doc.open();
                    doc.write(`<body style="background:#09090b; color:#a1a1aa; font-family:sans-serif; display:flex; justify-content:center; align-items:center; height:100vh; margin:0;"><div>Loading content...</div></body>`);
                    doc.close();

                    try {
                        const res = await fetch(`/api/preview/book-page/${slug}/${href}`);
                        const data = await res.json();
                        if (data.status === "success") {
                            doc.open();
                            if (activeState === "original") {
                                doc.write(data.original_html);
                            } else {
                                doc.write(data.translated_html);
                            }
                            doc.close();
                        } else {
                            throw new Error(data.message || "Failed to load page content");
                        }
                    } catch (err) {
                        doc.open();
                        doc.write(`<body style="background:#09090b; color:#ef4444; font-family:sans-serif; padding:20px;"><h3>Failed to load page content</h3><p>${err.message}</p></body>`);
                        doc.close();
                    }

                    document.getElementById("page-indicator").textContent = `Розділ ${currentChapterIdx + 1} з ${chapters.length} | ${activeState.toUpperCase()}`;
                    document.getElementById("btn-prev").disabled = currentChapterIdx === 0;
                    document.getElementById("btn-next").disabled = currentChapterIdx === chapters.length - 1;
                    document.getElementById("chapter-select").value = href;
                    updateSegments();
                };

                document.querySelectorAll(".segment-btn").forEach(btn => {
                    btn.addEventListener("click", () => {
                        activeState = btn.dataset.state;
                        loadPageContent();
                    });
                });

                document.getElementById("btn-prev").addEventListener("click", () => {
                    if (currentChapterIdx > 0) {
                        currentChapterIdx--;
                        loadPageContent();
                    }
                });
                document.getElementById("btn-next").addEventListener("click", () => {
                    if (currentChapterIdx < chapters.length - 1) {
                        currentChapterIdx++;
                        loadPageContent();
                    }
                });

                try {
                    const res = await fetch(`/api/preview/book-chapters/${slug}`);
                    const data = await res.json();
                    if (data.status === "success" && data.chapters) {
                        chapters = data.chapters;
                        const select = document.getElementById("chapter-select");
                        select.innerHTML = "";
                        chapters.forEach((ch, idx) => {
                            const opt = document.createElement("option");
                            opt.value = ch.href;
                            opt.textContent = ch.href;
                            select.appendChild(opt);
                        });

                        select.addEventListener("change", (e) => {
                            const idx = chapters.findIndex(ch => ch.href === e.target.value);
                            if (idx !== -1) {
                                currentChapterIdx = idx;
                                loadPageContent();
                            }
                        });

                        if (chapters.length > 0) {
                            currentChapterIdx = 0;
                            loadPageContent();
                        }
                    } else {
                        document.getElementById("chapter-select").innerHTML = `<option value="">Error: ${data.message || 'Failed to parse EPUB'}</option>`;
                    }
                } catch (err) {
                    console.error(err);
                    document.getElementById("chapter-select").innerHTML = `<option value="">Failed to load chapters</option>`;
                }
            }

            // For EPUB books — default to Full Page Viewer tab
            if (bookData.is_epub_book || bookData.epub_available) {
                showPageViewerTab();
                btnPageViewer.style.color = "var(--primary)";
                btnPageViewer.style.borderBottomColor = "var(--primary)";
                btnParagraphs.style.color = "var(--text-secondary)";
                btnParagraphs.style.borderBottomColor = "transparent";
            } else {
                showParagraphsTab();
            }
        }

        async function renderEpubOnlyView(area) {
            // Show stats banner + Full Page Viewer for pure EPUB books
            const stats = bookData.cache_stats || {};
            const pct = stats.percent || 0;
            const currentFile = stats.current_file || 0;
            const totalFiles = stats.total_files || 0;
            const translatedBlocks = stats.translated_blocks || 0;

            area.innerHTML = `
                <div style="background: rgba(139,92,246,0.08); border: 1px solid rgba(139,92,246,0.25); border-radius: 12px; padding: 1.2rem 1.5rem; margin-bottom: 1.5rem; display: flex; align-items: center; gap: 1.5rem; flex-wrap: wrap;">
                    <div style="flex: 1; min-width: 200px;">
                        <div style="font-size: 0.8rem; color: var(--text-secondary); margin-bottom: 0.3rem;">Прогрес перекладу EPUB</div>
                        <div style="font-size: 1.4rem; font-weight: 700; color: var(--primary);">${pct.toFixed(1)}%</div>
                        <div style="font-size: 0.85rem; color: var(--text-secondary); margin-top:0.2rem;">Файл ${currentFile}/${totalFiles} · ${translatedBlocks} блоків у кеші</div>
                    </div>
                    <div style="flex: 2; min-width: 200px;">
                        <div style="background: rgba(255,255,255,0.05); border-radius: 6px; height: 8px; overflow: hidden;">
                            <div style="background: linear-gradient(90deg, var(--primary), #3b82f6); height: 100%; width: ${Math.min(pct,100)}%; border-radius: 6px; transition: width 0.5s ease;"></div>
                        </div>
                    </div>
                    <div>
                        <span style="font-size: 0.8rem; color: #10b981; background: rgba(16,185,129,0.1); padding: 0.3rem 0.8rem; border-radius: 20px; border: 1px solid rgba(16,185,129,0.2);">📖 EPUB Book</span>
                    </div>
                </div>

                <div class="tabs" style="display: flex; gap: 1rem; border-bottom: 1px solid var(--border-color); padding-bottom: 1rem; margin-bottom: 1.5rem;">
                    <button class="tab-btn active" id="tab-page-viewer-only" style="background: none; border: none; color: var(--primary); font-size: 1.1rem; font-weight: 600; cursor: pointer; border-bottom: 2px solid var(--primary); padding: 0.5rem 1rem; outline: none; transition: color 0.2s ease, border-color 0.2s ease;">📖 Переклад по сторінках</button>
                </div>
                <div id="epub-page-viewer-area"></div>
            `;

            await loadEpubPageViewer(document.getElementById("epub-page-viewer-area"));
        }

        async function loadEpubPageViewer(container) {
            container.innerHTML = `
                <div style="display: flex; align-items: center; gap: 1rem; margin-bottom: 1.5rem; background: rgba(255,255,255,0.02); padding: 0.8rem; border-radius: 8px; border: 1px solid var(--border-color);">
                    <label for="chapter-select-epub" style="font-weight: 600; color: var(--text-secondary); min-width: 140px;">📄 Файл розділу:</label>
                    <select id="chapter-select-epub" style="background: #18181b; color: white; border: 1px solid var(--border-color); padding: 0.5rem 1rem; border-radius: 6px; flex-grow: 1; outline: none; font-family: inherit; font-size: 0.95rem; cursor: pointer;">
                        <option value="">⏳ Завантаження файлів EPUB...</option>
                    </select>
                </div>
                
                <div class="unified-viewer">
                    <div class="viewer-viewport" id="viewer-viewport">
                        <iframe id="viewport-iframe-epub" class="viewport-iframe"></iframe>
                    </div>
                    
                    <div class="unified-controls">
                        <button class="nav-btn" id="btn-prev-epub">← Попередній розділ</button>
                        
                        <div class="segmented-control" id="viewer-state-selector-epub">
                            <button class="segment-btn active" data-state="original">Original</button>
                            <button class="segment-btn" data-state="processed">Translated</button>
                        </div>
                        
                        <button class="nav-btn" id="btn-next-epub">Наступний розділ →</button>
                    </div>
                    <div class="unified-status" id="page-indicator-epub">Розділ 1 з 1</div>
                </div>
            `;

            let activeState = "processed";
            let chapters = [];
            let currentChapterIdx = 0;

            const updateSegments = () => {
                document.querySelectorAll("#viewer-state-selector-epub .segment-btn").forEach(btn => {
                    if (btn.dataset.state === activeState) {
                        btn.classList.add("active");
                    } else {
                        btn.classList.remove("active");
                    }
                });
            };

            const loadPageContent = async () => {
                if (chapters.length === 0) return;
                const href = chapters[currentChapterIdx].href;
                const iframe = document.getElementById("viewport-iframe-epub");
                if (!iframe) return;

                const doc = iframe.contentDocument || iframe.contentWindow.document;
                doc.open();
                doc.write(`<body style="background:#09090b; color:#a1a1aa; font-family:sans-serif; display:flex; justify-content:center; align-items:center; height:100vh; margin:0;"><div>Loading content...</div></body>`);
                doc.close();

                try {
                    const res = await fetch(`/api/preview/book-page/${slug}/${href}`);
                    const data = await res.json();
                    if (data.status === "success") {
                        doc.open();
                        if (activeState === "original") {
                            doc.write(data.original_html);
                        } else {
                            doc.write(data.translated_html);
                        }
                        doc.close();
                    } else {
                        throw new Error(data.message || "Failed to load page content");
                    }
                } catch (err) {
                    doc.open();
                    doc.write(`<body style="background:#09090b; color:#ef4444; font-family:sans-serif; padding:20px;"><h3>Failed to load page content</h3><p>${err.message}</p></body>`);
                    doc.close();
                }

                document.getElementById("page-indicator-epub").textContent = `Розділ ${currentChapterIdx + 1} з ${chapters.length} | ${activeState.toUpperCase()}`;
                document.getElementById("btn-prev-epub").disabled = currentChapterIdx === 0;
                document.getElementById("btn-next-epub").disabled = currentChapterIdx === chapters.length - 1;
                document.getElementById("chapter-select-epub").value = href;
                updateSegments();
            };

            document.querySelectorAll("#viewer-state-selector-epub .segment-btn").forEach(btn => {
                btn.addEventListener("click", () => {
                    activeState = btn.dataset.state;
                    loadPageContent();
                });
            });

            document.getElementById("btn-prev-epub").addEventListener("click", () => {
                if (currentChapterIdx > 0) {
                    currentChapterIdx--;
                    loadPageContent();
                }
            });
            document.getElementById("btn-next-epub").addEventListener("click", () => {
                if (currentChapterIdx < chapters.length - 1) {
                    currentChapterIdx++;
                    loadPageContent();
                }
            });

            try {
                const res = await fetch(`/api/preview/book-chapters/${slug}`);
                const data = await res.json();
                if (data.status === "success" && data.chapters) {
                    chapters = data.chapters;
                    const select = document.getElementById("chapter-select-epub");
                    select.innerHTML = "";
                    chapters.forEach(ch => {
                        const opt = document.createElement("option");
                        opt.value = ch.href;
                        opt.textContent = ch.href;
                        select.appendChild(opt);
                    });
                    select.addEventListener("change", e => {
                        const idx = chapters.findIndex(ch => ch.href === e.target.value);
                        if (idx !== -1) {
                            currentChapterIdx = idx;
                            loadPageContent();
                        }
                    });
                    if (chapters.length > 0) {
                        currentChapterIdx = 0;
                        loadPageContent();
                    }
                } else {
                    document.getElementById("chapter-select-epub").innerHTML = `<option>❌ ${data.message || 'Не вдалося завантажити розділи'}</option>`;
                }
            } catch(err) {
                document.getElementById("chapter-select-epub").innerHTML = `<option>❌ Помилка: ${err.message}</option>`;
            }
        }

        fetchBookData();
    
        // ── TASK-54: Cast & Context tab ────────────────────────────────
        let _castScanPollTimer = null;
        let _castScanWeStoppedLlama = false;

        async function refreshCastScanStatus() {
            try {
                const statusRes = await fetch(`/api/agent-editor/status/${slug}`, {cache:'no-store'});
                const statusData = await statusRes.json();
                const isRunning = statusData.ner_running;

                const startBtn = document.getElementById('cast-scan');
                const stopBtn = document.getElementById('cast-scan-stop');
                const progressWrap = document.getElementById('cast-progress-wrap');
                const progressStage = document.getElementById('cast-progress-stage');
                const progressLabel = document.getElementById('cast-progress-label');
                const progressFill = document.getElementById('cast-progress-fill');
                const logEl = document.getElementById('cast-log');
                const msg = document.getElementById('cast-msg');

                if (startBtn) startBtn.style.display = isRunning ? 'none' : '';
                if (stopBtn) stopBtn.style.display = isRunning ? '' : 'none';

                if (isRunning) {
                    if (progressWrap) progressWrap.style.display = '';
                    
                    const progRes = await fetch(`/api/characters/${slug}/scan-progress`, {cache:'no-store'});
                    const progData = await progRes.json();
                    
                    if (progressStage) progressStage.textContent = progData.stage || 'Аналіз...';
                    if (progressLabel) progressLabel.textContent = `${progData.percent}%`;
                    if (progressFill) progressFill.style.width = `${progData.percent}%`;
                    
                    if (logEl && progData.log_tail && progData.log_tail.length) {
                        const atBottom = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 20;
                        logEl.textContent = progData.log_tail.join('\n');
                        if (atBottom) logEl.scrollTop = logEl.scrollHeight;
                    }
                } else {
                    if (_castScanPollTimer) {
                        clearInterval(_castScanPollTimer);
                        _castScanPollTimer = null;
                    }
                    if (progressWrap) progressWrap.style.display = 'none';
                    if (_castScanWeStoppedLlama) {
                        _castScanWeStoppedLlama = false;
                        fetch('/api/models/start', {method:'POST'}).catch(() => {});
                        if (msg) msg.textContent = 'Сервер перекладу відновлено.';
                    }
                }
            } catch (e) {
                console.error("Error polling cast progress:", e);
            }
        }

        async function renderCastTab(container) {
            container.innerHTML = '<p style="color:var(--text-secondary); padding:1rem;">Завантаження реєстру персонажів…</p>';
            const r = await fetch(`/api/characters/${slug}`);
            const d = await r.json();
            const chars = d.characters || [];

            // TASK-67: bubble-tone toggle - deliberately NOT wrapped in the
            // d.entitled check below. Classification of bubble shape
            // (крик/думка/наратив) always runs for every book regardless of
            // premium status; this only decides whether it's allowed to
            // influence the translation prompt, so it's free for everyone.
            let toneHead = "";
            if (isManga) {
                try {
                    const toneR = await fetch(`/api/manga/${slug}/bubble-tone`);
                    const toneD = await toneR.json();
                    toneHead = `<div style="border:1px solid var(--border-color); border-radius:10px; padding:0.8rem 1rem; margin-bottom:1rem;">
                        <label style="display:flex; align-items:center; gap:0.4rem;">
                            <input type="checkbox" id="bubble-tone-enable" ${toneD.enable_bubble_tone ? 'checked' : ''}>
                            Враховувати тип бульбашки (крик / думка / наратив) у перекладі
                        </label>
                        <p style="color:var(--text-secondary); font-size:0.85rem; margin:0.4rem 0 0;">
                            Форма бульбашки автоматично визначається для кожного тексту (безкоштовно,
                            для всіх книг). Тут — чи повинен переклад враховувати цей тип: гучніше й
                            експресивніше для крику, м'якше для думок, книжковий стиль для наративу.</p>
                    </div>`;
                } catch (e) {
                    console.error("Failed to load bubble tone:", e);
                }
            }

            let head = toneHead;
            if (!d.entitled) {
                head += `<div style="border:1px solid #f0b429; border-radius:10px; padding:0.8rem 1rem; margin-bottom:1rem;">
                    🔒 <b>Cast Registry — розширена можливість.</b> Активуйте через
                    <a href="https://t.me/GetVydraBot" target="_blank">@GetVydraBot</a> (/premium).</div>`;
            } else {
                head += `<div style="display:flex; gap:0.6rem; flex-wrap:wrap; margin-bottom:1rem; align-items:center;">
                    <label style="display:flex; align-items:center; gap:0.4rem; margin-right:1rem;">
                        <input type="checkbox" id="cast-enable" ${d.enabled ? 'checked' : ''}>
                        Застосовувати правила персонажів у перекладі цієї книги
                    </label>
                    <div style="display:flex; align-items:center; gap:0.3rem; border:1px solid var(--border-color); border-radius:6px; padding:0.2rem 0.4rem;">
                        <span style="font-size:0.8rem; color:var(--text-secondary);">з:</span>
                        <input type="number" id="cast-page-start" placeholder="початок" style="width:60px; font-size:0.85rem; padding:0.1rem 0.2rem; background:transparent; border:none; border-bottom:1px solid var(--border-color); color:var(--text-primary);">
                        <span style="font-size:0.8rem; color:var(--text-secondary);">по:</span>
                        <input type="number" id="cast-page-end" placeholder="кінець" style="width:60px; font-size:0.85rem; padding:0.1rem 0.2rem; background:transparent; border:none; border-bottom:1px solid var(--border-color); color:var(--text-primary);">
                    </div>
                    <button class="nav-btn" id="cast-scan">▶️ Сканувати персонажів</button>
                    <button class="nav-btn" id="cast-scan-stop" style="background:#7f1d1d; color:#fff; display:none;">⏹ Зупинити</button>
                    <button class="nav-btn" id="cast-add">➕ Додати вручну</button>
                    <span id="cast-msg" style="color:var(--text-secondary);"></span>
                </div>
                <div id="cast-progress-wrap" style="display:none; margin-top:0.8rem; margin-bottom:1rem; width:100%;">
                    <div style="font-size:0.85rem; color:var(--text-secondary); margin-bottom:0.3rem; display:flex; justify-content:space-between;">
                        <span id="cast-progress-stage">Сканування...</span>
                        <span id="cast-progress-label">0%</span>
                    </div>
                    <div style="width:100%; height:8px; background:var(--border-color); border-radius:4px; overflow:hidden; margin-bottom:0.8rem;">
                        <div id="cast-progress-fill" style="width:0%; height:100%; background:var(--primary); transition:width 0.3s ease;"></div>
                    </div>
                    <pre id="cast-log" style="background:#0b0b12; border:1px solid var(--border-color); border-radius:8px; padding:0.7rem; font-family:monospace; font-size:0.76rem; max-height:200px; overflow-y:auto; white-space:pre-wrap; color:var(--text-secondary);"></pre>
                </div>`;
            }
            const rows = chars.map((c, i) => `
                <tr data-i="${i}" style="border-bottom:1px solid var(--border-color);">
                    <td style="padding:0.4rem; width:50px; text-align:center;">
                        <div class="cast-thumb-wrap" data-char-id="${c.id}" style="width:48px; height:48px; margin:0 auto; cursor:pointer;" title="Клацніть, щоб додати/змінити фото персонажа">
                            <div class="cast-thumb-placeholder" style="width:48px; height:48px; border:1px dashed var(--border-color); border-radius:6px; display:flex; align-items:center; justify-content:center; font-size:1.2rem; color:var(--text-secondary);">📷</div>
                            <img src="/api/characters/${slug}/thumbnail/${c.id}" style="width:48px;height:48px;object-fit:cover;border-radius:6px; display:none;" onerror="this.style.display='none'">
                            <input type="file" accept="image/*" class="cast-thumb-input" style="display:none;">
                        </div>
                        <a href="#" class="cast-thumb-reset" data-char-id="${c.id}" style="font-size:0.7rem; color:var(--text-secondary); display:none;" title="Прибрати фото">↺ скинути</a>
                    </td>
                    <td style="padding:0.4rem;"><input value="${(c.name_source||[]).join(', ')}" data-f="name_source" style="width:98%"></td>
                    <td><input value="${c.name_target||''}" data-f="name_target" style="width:95%"></td>
                    <td><select data-f="gender">
                        ${['feminine','masculine','neutral'].map(g => `<option value="${g}" ${c.gender===g?'selected':''}>${g}</option>`).join('')}
                    </select></td>
                    <td style="text-align:center;"><input type="checkbox" data-f="is_pov_narrator" ${c.is_pov_narrator?'checked':''}></td>
                    <td><span style="color:${c.status==='verified'?'#48bb78':(c.status==='auto_drafted'?'#f0b429':'#a0aec0')}">${c.status}</span></td>
                    <td>${c.status==='verified'
                        ? `<button class="nav-btn" data-act="unverify">↩</button>`
                        : `<button class="nav-btn" data-act="approve" ${d.entitled?'':'disabled'}>✅ Approve</button>`}
                        <button class="nav-btn" data-act="del">🗑</button></td>
                </tr>`).join('');
            container.innerHTML = head + `
                <p style="color:var(--text-secondary); font-size:0.88rem; margin-bottom:0.6rem;">
                   Правила застосовуються в перекладі <b>тільки</b> для персонажів зі статусом
                   <b>verified</b> (граматичний рід, займенники). Gender <b>neutral</b> —
                   для спойлер-чутливої неоднозначності.</p>
                <div style="overflow-x:auto;"><table style="width:100%; font-size:0.9rem; border-collapse:collapse;">
                    <tr style="color:var(--text-secondary); text-align:left;">
                        <th style="width:50px;"></th>
                        <th style="padding:0.4rem;">Імена в оригіналі (через кому)</th><th>Українською</th>
                        <th>Рід</th><th>POV</th><th>Статус</th><th></th></tr>
                    ${rows || '<tr><td colspan="7" style="padding:1rem; color:var(--text-secondary);">Поки порожньо — запустіть сканування або додайте вручну.</td></tr>'}
                </table></div>`;

            const collect = () => Array.from(container.querySelectorAll('tr[data-i]')).map((tr, idx) => {
                const c = Object.assign({}, chars[idx]);
                tr.querySelectorAll('[data-f]').forEach(el => {
                    const f = el.dataset.f;
                    if (f === 'name_source') c[f] = el.value.split(',').map(x => x.trim()).filter(Boolean);
                    else if (el.type === 'checkbox') c[f] = el.checked;
                    else c[f] = el.value;
                });
                return c;
            });
            const save = async (list, msg) => {
                const res = await fetch(`/api/characters/${slug}`, {method:'PUT',
                    headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({characters:list})});
                const j = await res.json();
                if (res.ok) { renderCastTab(container); }
                else alert(j.message || 'Помилка збереження');
            };
            container.querySelectorAll('[data-act]').forEach(btn => btn.addEventListener('click', () => {
                const list = collect();
                const idx = +btn.closest('tr').dataset.i;
                if (btn.dataset.act === 'approve') list[idx].status = 'verified';
                if (btn.dataset.act === 'unverify') list[idx].status = 'unverified';
                if (btn.dataset.act === 'del') list.splice(idx, 1);
                save(list);
            }));
            // TASK-78: per-character image, for BOTH auto-drafted and
            // manually-added characters (the manual-add flow otherwise
            // has no way to attach a picture at all).
            // TASK-81: the empty state used to be a blank 48x48 box with
            // nothing visible at all (img.onerror just hides itself) - Q
            // circled it in a screenshot asking what it even was. Now a
            // dashed-border 📷 placeholder fills that space until a real
            // image loads, and the ↺ reset link only shows once there's
            // actually something to reset.
            const setThumbState = (wrap, hasImage) => {
                const charId = wrap.dataset.charId;
                wrap.querySelector('.cast-thumb-placeholder').style.display = hasImage ? 'none' : 'flex';
                wrap.querySelector('img').style.display = hasImage ? 'block' : 'none';
                const resetLink = container.querySelector(`.cast-thumb-reset[data-char-id="${charId}"]`);
                if (resetLink) resetLink.style.display = hasImage ? '' : 'none';
            };
            container.querySelectorAll('.cast-thumb-wrap').forEach(wrap => {
                const charId = wrap.dataset.charId;
                const fileInput = wrap.querySelector('.cast-thumb-input');
                const img = wrap.querySelector('img');
                img.addEventListener('load', () => setThumbState(wrap, true));
                img.addEventListener('error', () => setThumbState(wrap, false));
                wrap.addEventListener('click', () => fileInput.click());
                fileInput.addEventListener('change', async () => {
                    const f = fileInput.files[0];
                    if (!f) return;
                    const fd = new FormData();
                    fd.append('file', f);
                    const res = await fetch(`/api/characters/${slug}/thumbnail/${charId}`, {method:'POST', body: fd});
                    const j = await res.json();
                    if (res.ok) {
                        img.src = `/api/characters/${slug}/thumbnail/${charId}?v=${Date.now()}`;
                    } else alert(j.message || 'Помилка завантаження зображення');
                });
            });
            container.querySelectorAll('.cast-thumb-reset').forEach(a => a.addEventListener('click', async (e) => {
                e.preventDefault();
                const charId = a.dataset.charId;
                await fetch(`/api/characters/${slug}/thumbnail/${charId}`, {method:'DELETE'});
                const wrap = container.querySelector(`.cast-thumb-wrap[data-char-id="${charId}"]`);
                const img = wrap.querySelector('img');
                img.src = `/api/characters/${slug}/thumbnail/${charId}?v=${Date.now()}`;
            }));
            const en = document.getElementById('cast-enable');
            if (en) en.addEventListener('change', async () => {
                const res = await fetch(`/api/characters/${slug}/settings`, {method:'POST',
                    headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({enable_cast_registry: en.checked})});
                if (!res.ok) { const j = await res.json(); alert(j.message); en.checked = !en.checked; }
            });
            const tone = document.getElementById('bubble-tone-enable');
            if (tone) tone.addEventListener('change', async () => {
                const res = await fetch(`/api/manga/${slug}/bubble-tone`, {method:'POST',
                    headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({enable_bubble_tone: tone.checked})});
                if (!res.ok) { const j = await res.json(); alert(j.message); tone.checked = !tone.checked; }
            });
            // Initial poll setup or status check
            if (_castScanPollTimer) {
                clearInterval(_castScanPollTimer);
                _castScanPollTimer = null;
            }
            refreshCastScanStatus();
            fetch(`/api/agent-editor/status/${slug}`, {cache:'no-store'})
                .then(r => r.json())
                .then(statusData => {
                    if (statusData.ner_running) {
                        refreshCastScanStatus();
                        _castScanPollTimer = setInterval(refreshCastScanStatus, 2000);
                    }
                }).catch(() => {});

            const sc = document.getElementById('cast-scan');
            if (sc) sc.addEventListener('click', async () => {
                const msg = document.getElementById('cast-msg');
                const pageStartInput = document.getElementById('cast-page-start');
                const pageEndInput = document.getElementById('cast-page-end');
                const payload = {};
                if (pageStartInput && pageStartInput.value) {
                    payload.page_start = parseInt(pageStartInput.value, 10);
                }
                if (pageEndInput && pageEndInput.value) {
                    payload.page_end = parseInt(pageEndInput.value, 10);
                }
                let weStoppedLlama = false;
                msg.textContent = 'Запуск...';
                
                const fetchScan = async () => {
                    return fetch(`/api/characters/${slug}/scan`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    });
                };
                
                let res = await fetchScan();
                let j = await res.json();
                if (res.status === 409 && /Модель перекладу/.test(j.message || '')) {
                    // No confirm() here - there's no real "no" other than
                    // not scanning at all, and the restart below is already
                    // automatic, so a blocking dialog only adds friction
                    // for a decision that's always "yes, obviously" (Q's
                    // call, 2026-07-20).
                    msg.textContent = 'Звільняємо памʼять від моделі перекладу...';
                    await fetch('/api/models/stop', {method:'POST'});
                    weStoppedLlama = true;
                    await new Promise(r => setTimeout(r, 2500));
                    res = await fetchScan();
                    j = await res.json();
                }
                msg.textContent = j.message || res.status;
                if (res.ok) {
                    _castScanWeStoppedLlama = weStoppedLlama;
                    if (_castScanPollTimer) clearInterval(_castScanPollTimer);
                    refreshCastScanStatus();
                    _castScanPollTimer = setInterval(refreshCastScanStatus, 2000);
                }
            });

            const scStop = document.getElementById('cast-scan-stop');
            if (scStop) scStop.addEventListener('click', async () => {
                const msg = document.getElementById('cast-msg');
                const ok = confirm("Зупинити сканування персонажів?");
                if (!ok) return;
                msg.textContent = 'Зупинка...';
                const res = await fetch(`/api/characters/${slug}/scan/stop`, {method:'POST'});
                const j = await res.json();
                msg.textContent = j.message || res.status;
                refreshCastScanStatus();
            });

            const ad = document.getElementById('cast-add');
            if (ad) ad.addEventListener('click', () => {
                chars.push({id:'char_manual_'+Date.now(), name_source:[], name_target:'',
                            gender:'neutral', grammar_rules:'', speech_style:'',
                            is_pov_narrator:false, status:'unverified'});
                save(chars);
            });
        }
        // Paragraph Tab Handler for Mobile View
        window.switchParagraphTab = (button, tab) => {
            const card = button.closest('.paragraph-card');
            if (!card) return;
            card.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
            button.classList.add('active');
            if (tab === 'translated') {
                card.classList.add('tab-translated-active');
            } else {
                card.classList.remove('tab-translated-active');
            }
        };
    