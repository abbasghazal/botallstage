/* Telegram Mini App client. All user identity comes from verified initData. */
(() => {
  'use strict';
  const app = document.getElementById('app');
  const state = { me: null, view: 'home', arg: null, initData: '' };
  // Escape text interpolated into markup exactly once.
  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
  const labelIcons = {
    '📚': 'M4 4h6a3 3 0 0 1 3 3v14a4 4 0 0 0-4-3H4z M13 7a3 3 0 0 1 3-3h5v14h-4a4 4 0 0 0-4 3',
    '📖': 'M3 5h7l2 2 2-2h7v14h-7l-2 2-2-2H3z M12 7v14',
    '🔬': 'M9 3h6 M10 3v7l-6 9a1 1 0 0 0 1 2h14a1 1 0 0 0 1-2l-6-9V3 M8 15h8',
    '🗓️': 'M5 5h14v16H5z M8 3v4 M16 3v4 M5 10h14 M9 14h1 M14 14h1',
    '✍️': 'M4 20l5-1L21 7l-4-4L5 15z M14 6l4 4 M4 20h16',
    '🎓': 'M2 9l10-5 10 5-10 5z M6 11v6q6 5 12 0v-6 M22 9v9',
    '⭐': 'M12 3l3 6 7 1-5 5 1 7-6-3-6 3 1-7-5-5 7-1z',
    '🧪': 'M8 3h8 M9 3v8L5 19q-1 2 2 2h10q3 0 2-2l-4-8V3 M8 15h8',
    '🔎': 'M20 20l-5-5 M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0',
    '🤖': 'M5 8h14v12H5z M12 4v4 M10 4h4 M8 12h1 M15 12h1 M9 16h6',
    '📬': 'M3 5h18v15H3z M3 6l9 7 9-7',
  };
  function appendLabel(element, label) {
    const text = String(label ?? '');
    const prefix = Object.keys(labelIcons).find(key => text.startsWith(key));
    if (!prefix) { element.textContent = text; return; }
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('aria-hidden', 'true'); svg.classList.add('ui-icon');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', labelIcons[prefix]); svg.append(path);
    element.append(svg, document.createTextNode(text.slice(prefix.length).trimStart()));
  }
  const button = (label, action, value = '', css = '', attributes = {}) => {
    const element = document.createElement('button');
    element.className = css;
    appendLabel(element, label);
    element.dataset.action = action;
    element.dataset.value = value;
    for (const [key, value] of Object.entries(attributes)) element.dataset[key] = value;
    if (attributes.current) element.setAttribute('aria-current', attributes.current);
    return element.outerHTML;
  };
  function validateChapter(arg) {
    if (!arg || typeof arg.k !== 'string' || !arg.k.trim() || !Number.isInteger(arg.c) || arg.c < 1 || arg.c > 6) {
      throw new Error('بيانات الفصل غير صالحة. ارجع إلى المادة واختر الفصل مجدداً.');
    }
    return arg;
  }
  const card = html => `<section class="card">${html}</section>`;

  async function request(path, options = {}) {
    const headers = { ...(options.headers || {}), 'X-Telegram-Init-Data': state.initData };
    if (options.body) headers['Content-Type'] = 'application/json';
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), path === 'ai' ? 120000 : 30000);
    try {
      const response = await fetch(`/api/${path}`, { ...options, headers, signal: controller.signal });
      const isJson = (response.headers.get('Content-Type') || '').split(';')[0].trim().toLowerCase() === 'application/json';
      if (!response.ok) {
        const body = await response.text();
        console.error('API request failed', path, response.status, body.slice(0, 500));
        let message;
        if (isJson) {
          try { message = JSON.parse(body)?.error; }
          catch (error) { console.error('Invalid API error JSON', path, error); }
        }
        throw new Error(typeof message === 'string' ? message : `تعذر تنفيذ الطلب (${response.status}). أعد المحاولة.`);
      }
      if (!isJson) throw new Error('استجابة الخادم غير صالحة. أعد المحاولة لاحقاً.');
      let data;
      try { data = await response.json(); }
      catch (error) {
        console.error('Invalid API JSON', path, error);
        throw new Error('تعذر قراءة استجابة الخادم. أعد المحاولة.');
      }
      if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('بيانات الخادم غير صالحة. أعد المحاولة.');
      return data;
    } catch (error) {
      console.error('Mini App request error', path, error);
      if (error.name === 'AbortError') throw new Error('انتهت مهلة الاتصال بالخادم. أعد المحاولة.');
      if (error instanceof TypeError) throw new Error('تعذر الاتصال بالخادم. تحقق من الشبكة ثم أعد المحاولة.');
      throw error;
    } finally { clearTimeout(timeout); }
  }
  function showError(error, retry = true) {
    console.error('Mini App view/action error', error);
    const message = error instanceof TypeError || error instanceof SyntaxError ? 'تعذر عرض بيانات الصفحة. أعد المحاولة أو ارجع للرئيسية.' : error.message || error;
    app.innerHTML = card(`<h3>تعذر فتح الصفحة</h3><p class="error">${escapeHtml(message)}</p>${retry ? button('إعادة المحاولة', 'retry') + button('← الرئيسية', 'go', 'home') : ''}`);
  }
  const stageNames = ['', 'الأولى', 'الثانية', 'الثالثة', 'الرابعة'];
  const heading = (eyebrow, title, description = '') => `<header class="page-heading"><span class="eyebrow">${escapeHtml(eyebrow)}</span><h1>${escapeHtml(title)}</h1>${description ? `<p>${escapeHtml(description)}</p>` : ''}</header>`;
  function tile(label, action, value, number, description, css = '', attributes = {}) {
    const template = document.createElement('template');
    template.innerHTML = button(label, action, value, `explore-card ${css}`, attributes);
    const element = template.content.firstElementChild;
    element.textContent = '';
    for (const [className, text] of [['tile-number', number], ['tile-title', label], ['tile-description', description], ['tile-arrow', '↗']]) {
      const span = document.createElement('span'); span.className = className; if (className === 'tile-title') appendLabel(span, text); else span.textContent = text; element.append(span);
    }
    return element.outerHTML;
  }
  function nav() {
    return `<nav class="main-nav" aria-label="التنقل الرئيسي">${[['home', 'الرئيسية'], ['fav', '⭐ المفضلة'], ['quiz', '🧪 اختبار'], ['profile', 'حسابي']].map(([view, label]) => button(label, 'go', view, state.view === view ? 'nav-link active' : 'nav-link', state.view === view ? { current: 'page' } : {})).join('')}</nav>`;
  }
  async function setView(view, arg = null) {
    try {
      if (view === 'list') validateChapter(arg);
      state.view = view; state.arg = arg;
      window.scrollTo({ top: 0, behavior: 'instant' });
      await render();
    } catch (error) { showError(error); }
  }
  async function render() {
    app.dataset.view = state.view;
    app.innerHTML = '<div class="loading-panel" role="status"><span class="loader"></span><p>نجهّز مساحتك…</p><div class="skeleton-grid"><i></i><i></i><i></i></div></div>';
    state.me = await request('me');
    if (!state.me.registered) return registration();
    const views = { home, sub, chap, list, item, fav, profile, quiz, search, support, ai };
    if (!views[state.view]) return await setView('home');
    await views[state.view]();
  }
  function registration() {
    app.innerHTML = `<div class="registration">${heading('بداية جديدة', 'كل إنجاز يبدأ بخطوة.', 'أنشئ مساحتك، واجمع موادك وفصولك في مكان واحد.')}${card(`<span class="eyebrow">لنتعرّف عليك</span><h2>أكمل التسجيل</h2><label for="full-name">الاسم الثلاثي</label><input id="full-name" autocomplete="name" maxlength="120" placeholder="اكتب اسمك هنا"><label for="stage">مرحلتك الدراسية</label><select id="stage"><option value="1">الأولى</option><option value="2">الثانية</option><option value="3">الثالثة</option><option value="4">الرابعة</option></select>${button('بدء التعلم', 'register')}`)}</div>`;
  }
  function home() {
    const categories = [['subjects','📚 المواد','مادتك القادمة تبدأ من هنا'],['explanations','📖 الشروحات','افهم الفكرة، خطوة بخطوة'],['lab','🔬 المختبر','من المعرفة إلى التطبيق'],['monthly_exams','🗓️ الشهرية','مساحة للمراجعة والاستعداد'],['final_exams','✍️ النهائية','رتّب استعدادك للخطوة الكبيرة']];
    if (state.me.stage === 4) categories.push(['research','🎓 قسم الرابعة','أفكارك اليوم، إنجازك غداً']);
    const date = new Intl.DateTimeFormat('ar-IQ', { weekday: 'long', day: 'numeric', month: 'long' }).format(new Date());
    app.innerHTML = nav() + `<section class="hero"><div class="hero-copy"><div class="hero-meta"><span class="pill">المرحلة ${escapeHtml(stageNames[state.me.stage] || state.me.stage)}</span><span>${escapeHtml(date)}</span></div><p class="welcome">أهلاً، ${escapeHtml(state.me.full_name)}</p><h1>مساحة لأفكارك.<br><em>وخطوة لمستقبلك.</em></h1><p class="hero-description">موادك، شروحاتك واختباراتك. كل ما تحتاجه لتكمل رحلتك، في مكان واحد.</p>${button('استكشف موادك ↗', 'sub', 'subjects', 'hero-cta')}</div><div class="hero-art" aria-hidden="true"><div class="orbit orbit-one"></div><div class="orbit orbit-two"></div><span class="art-star">✦</span><div class="book book-back"></div><div class="book book-front"><span>B4 / STUDY CLUB</span><strong>العلم<br>يصنع<br><em>الفرق.</em></strong><i>✳</i></div><span class="art-note">فكرة. معرفة. إنجاز.</span></div></section><div class="section-heading"><div><span class="eyebrow">مكتبتك الدراسية</span><h2>ماذا ستتعلّم اليوم؟</h2></div><span class="section-count">${categories.length} مساحات للاستكشاف</span></div><div class="category-grid">${categories.map(([key, label, description], index) => tile(label, 'sub', key, String(index + 1).padStart(2, '0'), description, `category-${key}`)).join('')}</div><section class="study-tools"><div class="assistant-panel"><span class="assistant-symbol" aria-hidden="true">✳</span><div><span class="eyebrow">مساحة للأسئلة الكبيرة</span><h2>فكرة صعبة؟ نفكّر فيها معًا.</h2><p>اسأل مساعدك الذكي عن سؤالك الدراسي.</p></div>${button('🤖 مساعد ذكي', 'go', 'ai', 'secondary')}</div><div class="utility-panel">${button('🔎 بحث', 'go', 'search', 'utility-button')}${button('📬 الدعم', 'go', 'support', 'utility-button')}</div></section>`;
  }
  async function sub() {
    const data = await request(`subjects/${encodeURIComponent(state.arg)}`);
    app.innerHTML = nav() + heading('خطوتك التالية', data.title, 'اختر مادتك، وابدأ من الفصل الذي يناسبك.') + `<div class="filter-bar"><label for="subject-filter">ابحث بين موادك</label><input id="subject-filter" type="search" placeholder="اكتب اسم المادة…" autocomplete="off"><span id="subject-count" class="muted">${data.items.length} مادة</span></div><div class="grid subject-grid">${data.items.map((item, index) => tile(item.name, 'chap', item.key, String(index + 1).padStart(2, '0'), 'تصفّح الفصول ↗', 'subject-card')).join('') || card('<p class="empty">لا توجد مواد.</p>')}</div><p id="filter-empty" class="empty" hidden>لم نجد مادة بهذا الاسم. جرّب كلمة أخرى.</p>`;
  }
  async function chap() {
    const data = await request(`subjects/${encodeURIComponent(state.arg)}/chapters`);
    app.innerHTML = `${button('← رجوع', 'go', 'home', 'back-button')}${heading('فصل جديد، معرفة جديدة', data.name, 'اختر الفصل لاستكشاف المحتوى والملفات الدراسية.')}<div class="chapter-summary"><span class="chapter-emblem">▤</span><div><strong>رحلتك مع المادة</strong><p>${data.chapters.length} فصول · اختر نقطة البداية</p></div></div><div class="grid chapter-grid">${data.chapters.map(chapter => tile(`الفصل ${chapter}`, 'list', '', String(chapter).padStart(2, '0'), 'استكشف محتوى الفصل', 'chapter-card', { subject: state.arg, chapter: validateChapter({ k: state.arg, c: chapter }).c })).join('')}</div>`;
  }
  async function list() {
    validateChapter(state.arg);
    const data = await request(`content?subject=${encodeURIComponent(state.arg.k)}&chapter=${state.arg.c}`);
    const items = data.items.map((item, index) => `<section class="card content-row"><span class="content-icon">${escapeHtml(item.icon)}</span><div><span class="eyebrow">محتوى ${String(index + 1).padStart(2, '0')}</span><h3>${escapeHtml(item.title)}</h3></div>${button('فتح', 'item', item.id)}</section>`).join('') || card('<p class="empty">لا يوجد محتوى.</p>');
    app.innerHTML = `${button('← رجوع', 'chap', state.arg.k, 'back-button')}${heading(`الفصل ${state.arg.c}`, data.subject, `${data.items.length} محتوى دراسي في مكان واحد`)}<div class="content-list">${items}</div>`;
  }
  async function item() { const data = await request(`content/${state.arg}`); app.innerHTML = `${button('← الرئيسية', 'go', 'home', 'back-button')}${card(`<h2>${escapeHtml(data.title)}</h2><small>${escapeHtml(data.subject)} · الفصل ${escapeHtml(data.chapter)}</small><div class="text">${escapeHtml(data.text)}</div>${data.has_file ? button('📩 إرسال الملف للمحادثة', 'send', data.id) : ''}${button(data.favorite ? 'إزالة من المفضلة' : 'إضافة للمفضلة', 'favorite', data.id, 'secondary')}`)}${card(`<h3>💬 التعليقات</h3><div id="comments">تحميل…</div><label for="comment-text">تعليقك</label><textarea id="comment-text" maxlength="1000" placeholder="شارك فكرتك…"></textarea>${button('إضافة', 'comment', data.id)}`)}`; await loadComments(data.id); }
  async function loadComments(contentId) { const data = await request(`content/${contentId}/comments`); const target = document.getElementById('comments'); if (target) target.innerHTML = data.items.map(item => `<p><b>${escapeHtml(item.name)}</b>: ${escapeHtml(item.text)} ${item.can_delete ? button('حذف', 'delete-comment', `${contentId}:${item.id}`) : ''}</p>`).join('') || '<p class="empty">لا توجد تعليقات.</p>'; }
  async function fav() { const data = await request('favorites'); app.innerHTML = nav() + heading('احتفظ بما يلهمك', '⭐ المفضلة', 'محتواك المحفوظ، قريب منك دائمًا.') + (data.items.map(item => card(`${escapeHtml(item.title)}${button('فتح', 'item', item.id)}`)).join('') || card('<p class="empty">المفضلة فارغة.</p>')); }
  async function profile() {
    const data = await request('progress');
    const percent = Math.max(0, Math.min(100, Number(data.quiz_percent) || 0));
    app.innerHTML = nav() + heading('كل خطوة تُحسب', 'رحلتك بالأرقام', 'تابع ما تعلمته، واستعد لإنجازك القادم.') + `<section class="profile-banner"><span class="avatar">${escapeHtml(Array.from(state.me.full_name || 'ط')[0])}</span><div><h2>${escapeHtml(state.me.full_name)}</h2><p>المرحلة ${escapeHtml(stageNames[state.me.stage] || state.me.stage)}</p></div></section><div class="stats-grid">${[[data.viewed, 'محتوى شاهدته'], [data.quiz_attempts, 'محاولة اختبار'], [data.quiz_correct, 'إجابة صحيحة']].map(([value, label]) => `<section class="card stat-card"><strong>${escapeHtml(value)}</strong><span>${label}</span></section>`).join('')}</div>${card(`<div class="section-heading"><h3>نسبة الإجابات الصحيحة</h3><strong>${percent}%</strong></div><progress max="100" value="${percent}" aria-label="نسبة الإجابات الصحيحة"></progress><p class="muted">كل محاولة فرصة لتتعلّم شيئًا جديدًا.</p>${button(`🔔 الإشعارات ${state.me.notifications ? 'مفعلة' : 'معطلة'}`, 'notifications', '', 'secondary')}`)}`;
  }
  async function quiz() {
    const data = await request('quiz');
    app.innerHTML = nav() + heading('اختبر معرفتك', 'جاهز لتحدٍّ صغير؟', 'اقرأ السؤال، وخذ وقتك لاختيار الإجابة.') + (data.quiz ? card(`<span class="eyebrow">فكّر. اختر. تعلّم.</span><h2>${escapeHtml(data.quiz.question)}</h2><div class="quiz-options">${data.quiz.options.map((option, index) => button(option, 'answer', `${data.quiz.id}:${index}`, 'secondary quiz-option')).join('')}</div><div id="result" role="status"></div>`) : card('<p class="empty">لا توجد أسئلة.</p>'));
  }
  function search() { app.innerHTML = nav() + heading('اعثر على فكرتك التالية', 'ماذا تبحث عنه؟', 'ابحث في محتوى مرحلتك الدراسية.') + card(`<label for="search-query">كلمة البحث</label><div class="search-form"><input id="search-query" type="search" placeholder="موضوع، عنوان أو كلمة…">${button('بحث', 'search')}</div><div id="search-results" aria-live="polite"></div>`); }
  function support() { app.innerHTML = nav() + heading('نحن هنا لأجلك', 'لنتحدث.', 'لديك سؤال أو تواجه مشكلة؟ أرسل رسالتك لفريق الدعم.') + card(`<label for="ticket-message">رسالتك</label><textarea id="ticket-message" maxlength="2000" placeholder="اكتب التفاصيل التي تساعدنا على مساعدتك…"></textarea>${button('إرسال', 'ticket')}`); }
  function ai() { app.innerHTML = nav() + heading('مساعدك الدراسي', 'امنح فضولك مساحة.', 'اسأل، استكشف، وافهم الأفكار خطوة بخطوة.') + card(`<div class="ai-intro"><span class="assistant-symbol" aria-hidden="true">✳</span><p>ما الفكرة التي تريد فهمها اليوم؟</p></div><label for="ai-question">سؤالك الدراسي</label><textarea id="ai-question" maxlength="1500" placeholder="اشرح لي فكرة، أو ساعدني على فهم موضوع…"></textarea>${button('إرسال', 'ask')}<div id="ai-reply" class="text" role="status"></div>`); }
  function input(id) { return document.getElementById(id); }
  app.addEventListener('click', async event => {
    const element = event.target.closest('[data-action]'); if (!element || app.dataset.busy === 'true') return;
    app.dataset.busy = 'true';
    const { action, value } = element.dataset; element.disabled = true;
    try {
      if (action === 'retry') return await render(); if (action === 'go') return await setView(value); if (action === 'sub') return await setView('sub', value); if (action === 'chap') return await setView('chap', value); if (action === 'list') return await setView('list', { k: element.dataset.subject, c: Number(element.dataset.chapter) }); if (action === 'item') return await setView('item', Number(value));
      if (action === 'register') { await request('register', { method: 'POST', body: JSON.stringify({ full_name: input('full-name').value, stage: Number(input('stage').value) }) }); return await setView('home'); }
      if (action === 'send') { await request(`content/${value}/send`, { method: 'POST' }); return alert('أرسل البوت المحتوى إلى محادثتك.'); }
      if (action === 'favorite') { await request(`favorites/${value}`, { method: 'POST' }); return await render(); }
      if (action === 'comment') { await request(`content/${value}/comments`, { method: 'POST', body: JSON.stringify({ text: input('comment-text').value }) }); input('comment-text').value = ''; return await loadComments(value); }
      if (action === 'delete-comment') { const [contentId, commentId] = value.split(':'); await request(`comments/${commentId}`, { method: 'DELETE' }); return await loadComments(contentId); }
      if (action === 'notifications') { await request('notifications', { method: 'POST' }); return await render(); }
      if (action === 'answer') { const [quizId, answer] = value.split(':'); const data = await request(`quiz/${quizId}/answer`, { method: 'POST', body: JSON.stringify({ answer: Number(answer) }) }); input('result').textContent = data.correct ? '✅ إجابة صحيحة' : `❌ الصحيح: ${data.correct_answer}`; input('result').className = data.correct ? 'answer-result success' : 'answer-result incorrect'; app.querySelectorAll('[data-action=answer]').forEach(option => { option.setAttribute('aria-pressed', String(option === element)); }); return; }
      if (action === 'search') { const data = await request(`search?q=${encodeURIComponent(input('search-query').value)}`); input('search-results').innerHTML = data.items.map(item => card(`${escapeHtml(item.subject)} — ${escapeHtml(item.title)}${button('فتح', 'item', item.id)}`)).join('') || '<p class="empty">لا توجد نتائج.</p>'; return; }
      if (action === 'ticket') { const data = await request('support', { method: 'POST', body: JSON.stringify({ message: input('ticket-message').value }) }); alert(`تم إرسال التذكرة #${data.id}`); return; }
      if (action === 'ask') { input('ai-reply').textContent = 'جارِ الإجابة…'; const data = await request('ai', { method: 'POST', body: JSON.stringify({ question: input('ai-question').value }) }); input('ai-reply').textContent = data.answer; }
    } catch (error) { showError(error); } finally { element.disabled = false; delete app.dataset.busy; }
  });
  app.addEventListener('input', event => {
    if (event.target.id !== 'subject-filter') return;
    const query = event.target.value.trim().toLocaleLowerCase('ar');
    let count = 0;
    app.querySelectorAll('.subject-card').forEach(element => {
      element.hidden = !element.querySelector('.tile-title').textContent.toLocaleLowerCase('ar').includes(query);
      if (!element.hidden) count++;
    });
    input('subject-count').textContent = `${count} مادة`;
    input('filter-empty').hidden = count !== 0;
  });
  app.addEventListener('keydown', event => {
    if (event.key === 'Enter' && event.target.id === 'search-query') {
      event.preventDefault(); app.querySelector('[data-action=search]')?.click();
    }
  });
  const themeToggle = document.getElementById('theme-toggle');
  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    const light = theme === 'light';
    themeToggle.textContent = light ? '☾' : '☀';
    themeToggle.setAttribute('aria-label', light ? 'تفعيل الوضع الداكن' : 'تفعيل الوضع الفاتح');
    themeToggle.setAttribute('aria-pressed', String(light));
    document.querySelector('meta[name="theme-color"]').content = light ? '#f3f5ee' : '#101b1b';
  }
  try { applyTheme(localStorage.getItem('b4-theme') || 'dark'); } catch { applyTheme('dark'); }
  themeToggle.addEventListener('click', () => {
    const theme = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
    applyTheme(theme);
    try { localStorage.setItem('b4-theme', theme); } catch { /* Storage may be unavailable in a WebView. */ }
  });
  function boot(attempt = 0) { const telegram = window.Telegram?.WebApp; if (!telegram) { if (attempt < 100) return setTimeout(() => boot(attempt + 1), 100); showError('افتح الموقع من زر Open داخل تيليغرام.', false); return; } telegram.ready(); telegram.expand(); state.initData = telegram.initData || ''; if (!state.initData) return showError('بيانات جلسة تيليغرام غير متاحة. أعد فتح التطبيق.', false); render().catch(showError); }
  boot();
})();
