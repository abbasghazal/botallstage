/* Execute the real client in a DOM with a stub Telegram SDK and deterministic API.
   These checks exercise rendering/events; they do not connect to real accounts. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'web/js/app.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'web/index.html'), 'utf8');
let scenarios = 0;

async function createClient(registered = true) {
  const dom = new JSDOM(html, { url: 'https://bot4stage.test/', runScripts: 'outside-only' });
  const w = dom.window;
  const errors = [], calls = [];
  let quizType = 'fill_blank';
  w.console.error = (...args) => errors.push(args.map(String).join(' '));
  w.scrollTo = () => {};
  w.Telegram = { WebApp: { initData: 'signed-test-fixture', ready() {}, expand() {} } };
  w.fetch = async (input, options = {}) => {
    const url = new URL(input, w.location.href), route = url.pathname.slice('/api/'.length);
    calls.push({ route, query: url.searchParams, options });
    assert.equal(options.headers['X-Telegram-Init-Data'], 'signed-test-fixture');
    let data;
    if (route === 'me') data = { registered, full_name: 'طالب الاختبار', stage: 4, notifications: true };
    else if (route === 'register') { registered = true; data = { ok: true }; }
    else if (route === 'subjects/subjects') data = { title: 'المواد', items: [{ key: 'laser', name: 'الليزر' }] };
    else if (route === 'subjects/lab') data = { title: 'المختبر', items: [{ key: 'nuclear_physics_lab', name: 'مختبر النووية' }] };
    else if (route === 'subjects/laser/chapters') data = { name: 'الليزر', lab: false, items: [{ id: 1, label: 'الفصل الأول' }] };
    else if (route === 'subjects/nuclear_physics_lab/chapters') data = { name: 'مختبر النووية', lab: true, items: [{ id: 1, label: 'التجربة الأولى', course: 1, experiment: 1 }] };
    else if (route === 'content') data = { subject: 'الليزر', label: 'الفصل الأول', items: [{ id: 21, title: 'ملف الليزر', icon: '📚' }] };
    else if (route === 'content/21') data = { id: 21, title: 'ملف الليزر', text: 'نص المحتوى', subject: 'الليزر', chapter: 1, has_file: false, favorite: false };
    else if (route === 'content/21/comments') data = { items: [{ name: 'طالب', text: 'تعليق محفوظ' }], has_more: false };
    else if (route === 'search') data = { items: [{ id: 21, title: url.searchParams.get('page') === '1' ? 'نتيجة الصفحة الثانية' : '<img src=x onerror=alert(1)>', subject: 'الليزر' }], has_more: url.searchParams.get('page') === '0' };
    else if (route === 'favorites') data = { items: [{ id: 21, title: 'المحتوى المفضل' }] };
    else if (route === 'progress') data = { viewed: 1, quiz_attempts: 2, quiz_correct: 1, quiz_percent: 50 };
    else if (route === 'quiz') data = { quiz: { id: 10, question: 'أكمل ___', question_type: quizType, options: quizType === 'fill_blank' ? [] : quizType === 'true_false' ? ['صح', 'خطأ'] : ['ألف', 'باء', 'جيم', 'دال'] } };
    else if (route === 'quiz/10/answer') data = { correct: true, correct_answer: 'أربعة' };
    else if (route === 'support') data = { id: 1 };
    else if (route === 'ai') data = { answer: 'إجابة المساعد' };
    else throw new Error(`Unexpected API route ${route}`);
    return { ok: true, status: 200, headers: { get: () => 'application/json' }, json: async () => data };
  };
  w.eval(source);
  async function waitFor(predicate, label) {
    for (let i = 0; i < 100; i++) {
      if (predicate()) return;
      await new Promise(resolve => setTimeout(resolve, 2));
    }
    throw new Error(`Timed out: ${label}; ${errors.join('; ')}`);
  }
  async function click(selector, ready) {
    const element = w.document.querySelector(selector);
    assert.ok(element, `Missing ${selector}`);
    element.click();
    await waitFor(() => w.document.getElementById('app').dataset.busy !== 'true' && (!ready || ready()), selector);
  }
  async function go(view) {
    if (!w.document.querySelector('[data-action=go][data-value=home]') && w.document.querySelector('[data-action=chap]')) {
      await click('[data-action=chap]');
    }
    if (!w.document.querySelector(`[data-action=go][data-value="${view}"]`)) {
      await click('[data-action=go][data-value=home]', () => w.document.querySelector('.hero'));
    }
    await click(`[data-action=go][data-value="${view}"]`, () => w.document.getElementById('app').dataset.view === view && !w.document.querySelector('.loading-panel'));
  }
  await waitFor(() => w.document.querySelector(registered ? '.hero' : '.registration'), 'boot');
  return { dom, w, errors, calls, click, go, waitFor, setQuiz: value => { quizType = value; } };
}

(async () => {
  const c = await createClient();
  try {
    assert.match(c.w.document.querySelector('.welcome').textContent, /طالب الاختبار/); scenarios++;
    await c.go('search');
    const query = c.w.document.getElementById('search-query');
    query.value = 'ضوء & موجة';
    query.dispatchEvent(new c.w.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    await c.waitFor(() => c.w.document.getElementById('search-results').textContent.includes('onerror'), 'search result');
    assert.equal(c.calls.find(x => x.route === 'search').query.get('q'), query.value);
    assert.equal(c.w.document.querySelector('#search-results img'), null); scenarios++;
    await c.click('[data-action=searchpage][data-value="1"]');
    assert.match(c.w.document.getElementById('search-results').textContent, /نتيجة الصفحة الثانية/); scenarios++;
    await c.click('[data-action=item][data-value="21"]');
    assert.match(c.w.document.getElementById('comments').textContent, /تعليق محفوظ/); scenarios++;
    await c.go('home');
    await c.click('[data-action=sub][data-value=subjects]');
    await c.click('[data-action=chap][data-value=laser]');
    await c.click('[data-action=list][data-chapter="1"]');
    assert.match(c.w.document.getElementById('app').textContent, /ملف الليزر/); scenarios++;
    await c.go('home');
    await c.click('[data-action=sub][data-value=lab]');
    await c.click('[data-action=chap][data-value=nuclear_physics_lab]');
    await c.click('[data-action=labcourse][data-value="nuclear_physics_lab:1"]');
    assert.ok(c.w.document.querySelector('[data-action=list][data-chapter="1"]')); scenarios++;
    await c.go('fav'); assert.match(c.w.document.getElementById('app').textContent, /المحتوى المفضل/); scenarios++;
    await c.go('profile'); assert.ok(c.w.document.querySelector('progress')); scenarios++;
    await c.go('quiz');
    await c.click('[data-action=answer-blank]');
    assert.ok(c.w.document.querySelector('.action-error'));
    assert.equal(c.w.document.querySelector('[data-action=answer-blank]').disabled, false);
    c.w.document.getElementById('blank-answer').value = '  أربعة  ';
    await c.click('[data-action=answer-blank]');
    assert.equal(JSON.parse(c.calls.filter(x => x.route === 'quiz/10/answer').at(-1).options.body).answer, 'أربعة');
    assert.equal(c.w.document.getElementById('blank-answer').disabled, true); scenarios++;
    // The deliberately empty answer above logs a handled validation error.
    assert.equal(c.errors.length, 1); assert.match(c.errors[0], /اكتب إجابة الفراغ/); c.errors.length = 0;
    for (const [kind, count] of [['true_false', 2], ['multiple_choice', 4]]) {
      c.setQuiz(kind); await c.click('[data-action=go][data-value=quiz]');
      assert.equal(c.w.document.querySelectorAll('[data-action=answer]').length, count);
      await c.click('[data-action=answer][data-value="10:0"]');
      assert.equal(JSON.parse(c.calls.filter(x => x.route === 'quiz/10/answer').at(-1).options.body).answer, 0);
      assert.ok([...c.w.document.querySelectorAll('[data-action=answer]')].every(x => x.disabled)); scenarios++;
    }
    await c.go('support'); assert.ok(c.w.document.getElementById('ticket-message')); scenarios++;
    assert.deepEqual(c.errors, []);
  } finally { c.dom.window.close(); }
  const ai = await createClient();
  try {
    await ai.go('ai'); ai.w.document.getElementById('ai-question').value = 'اشرح الضوء';
    await ai.click('[data-action=ask]'); assert.match(ai.w.document.getElementById('ai-reply').textContent, /إجابة المساعد/);
    assert.deepEqual(ai.errors, []); scenarios++;
  } finally { ai.dom.window.close(); }
  const unregistered = await createClient(false);
  try {
    unregistered.w.document.getElementById('full-name').value = 'طالب تسجيل جديد';
    await unregistered.click('[data-action=register]', () => unregistered.w.document.querySelector('.hero'));
    assert.deepEqual(unregistered.errors, []); scenarios++;
  } finally { unregistered.dom.window.close(); }
  console.log(`Client runtime: ${scenarios} scenarios passed (DOM, stub Telegram SDK/API).`);
})().catch(error => { console.error(error); process.exitCode = 1; });
