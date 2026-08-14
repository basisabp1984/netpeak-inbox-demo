/* Витрина результатов классификации.
   Ванильный JS без сборки: приложение на один экран, фреймворк здесь был бы
   сложным вместо простого. */

const state = { items: [], stats: null, run: null, filter: 'all', search: '' };

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

async function boot() {
  try {
    const [statsRes, itemsRes] = await Promise.all([
      fetch('/api/stats').then((r) => r.json()),
      fetch('/api/items').then((r) => r.json()),
    ]);
    state.stats = statsRes.stats;
    state.run = statsRes.run;
    state.items = itemsRes.items;
  } catch (err) {
    $('#feed').innerHTML = `<div class="empty">Не вдалося завантажити дані: ${esc(err.message)}</div>`;
    return;
  }
  renderMeta();
  renderStats();
  renderBreakdowns();
  bindControls();
  renderFeed();
}

function renderMeta() {
  const r = state.run;
  $('#runMeta').innerHTML =
    `${esc(r.model)} · temp ${r.temperature} · prompt ${esc(r.prompt_version)}<br>` +
    `прогін #${r.id} · ${esc(r.generated_at.replace('T', ' '))}`;
}

function renderStats() {
  const c = state.stats.counts;
  const tiles = [
    { v: c.total, l: 'запитів', cls: '' },
    { v: c.ok, l: 'класифіковано', cls: 'accent' },
    { v: c.failed, l: 'збоїв', cls: c.failed ? 'danger' : '' },
    { v: state.stats.by_priority.high || 0, l: 'термінових', cls: 'danger' },
    { v: c.need_clarification, l: 'потребують уточнення', cls: 'purple' },
    { v: c.low_confidence, l: 'низька впевненість', cls: 'warn' },
    { v: c.duplicates, l: 'ймовірних дублів', cls: '' },
  ];
  $('#stats').innerHTML = tiles.map((t) =>
    `<div class="stat ${t.cls}"><div class="v">${t.v}</div><div class="l">${t.l}</div></div>`).join('');
}

function bars(el, data, colorByKey = false) {
  const max = Math.max(...Object.values(data), 1);
  el.innerHTML = Object.entries(data).map(([k, n]) => {
    const cls = colorByKey ? ` ${k}` : '';
    return `<div class="bar-row">
      <div class="bar-top"><span>${esc(k)}</span><span class="n">${n}</span></div>
      <div class="bar-track"><div class="bar-fill${cls}" style="width:${(n / max) * 100}%"></div></div>
    </div>`;
  }).join('');
}

function renderBreakdowns() {
  bars($('#byCategory'), state.stats.by_category);
  // приоритеты в осмысленном порядке, а не по алфавиту
  const p = state.stats.by_priority;
  bars($('#byPriority'), { high: p.high || 0, medium: p.medium || 0, low: p.low || 0 }, true);
  bars($('#byDepartment'), state.stats.by_department);
}

function bindControls() {
  document.querySelectorAll('.chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      document.querySelectorAll('.chip').forEach((c) => c.classList.remove('active'));
      chip.classList.add('active');
      state.filter = chip.dataset.filter;
      renderFeed();
    });
  });

  let timer;
  $('#search').addEventListener('input', (e) => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      state.search = e.target.value.trim().toLowerCase();
      renderFeed();
    }, 160);
  });
}

function matches(item) {
  const f = state.filter;
  if (f === 'high' && item.priority !== 'high') return false;
  if (f === 'clarify' && !item.needs_clarification) return false;
  if (f === 'lowconf' && !(item.confidence !== null && item.confidence < 0.6)) return false;
  if (f === 'dupes' && !item.possible_duplicate_of) return false;
  if (f === 'failed' && item.status !== 'failed') return false;

  if (state.search) {
    const hay = `${item.request_id} ${item.raw_text} ${item.short_summary || ''}`.toLowerCase();
    if (!hay.includes(state.search)) return false;
  }
  return true;
}

function renderFeed() {
  const rows = state.items.filter(matches);
  $('#count').textContent = `${rows.length} з ${state.items.length}`;

  if (!rows.length) {
    $('#feed').innerHTML = '<div class="empty">Нічого не знайдено за цим фільтром</div>';
    return;
  }
  $('#feed').innerHTML = rows.map(card).join('');
}

function card(it) {
  const failed = it.status === 'failed';
  const cls = failed ? 'is-failed' : `p-${it.priority || 'low'}`;

  const tags = [];
  if (failed) {
    tags.push('<span class="tag err">✕ збій класифікації</span>');
  } else {
    tags.push(`<span class="tag cat">${esc(it.category)}</span>`);
    tags.push(`<span class="tag ${it.priority}">${esc(it.priority)}</span>`);
    if (it.needs_clarification) tags.push('<span class="tag warn">❓ уточнити</span>');
    if (it.is_actionable === false) tags.push('<span class="tag">не задача юніту</span>');
  }
  tags.push(`<span class="tag">${esc(it.channel)}</span>`);

  const conf = (!failed && it.confidence !== null)
    ? `<span class="conf">confidence ${it.confidence.toFixed(2)}</span>` : '';
  const attempts = it.attempts > 1 ? `<span class="conf">спроб: ${it.attempts}</span>` : '';

  return `<article class="item ${cls}">
    <div class="item-head">
      <span class="rid">${esc(it.request_id)}</span>
      ${tags.join('')}
      <span class="head-right">${attempts}${conf}</span>
    </div>
    <div class="item-body">
      <div class="raw">
        <div class="side-label">Вихідний запит</div>
        <p>${esc(it.raw_text)}</p>
        <div class="meta">${esc(it.timestamp)}</div>
      </div>
      <div class="parsed">
        <div class="side-label">Витягнуто моделлю</div>
        ${failed ? failedBody(it) : okBody(it)}
      </div>
    </div>
  </article>`;
}

function okBody(it) {
  let html = `<p class="summary">${esc(it.short_summary)}</p>`;

  html += `<div class="field"><span class="k">Відділ</span><span>${
    it.target_department ? esc(it.target_department) : '<span style="opacity:.5">не визначено</span>'}</span></div>`;
  html += `<div class="field"><span class="k">Мова</span><span>${esc(it.language)}</span></div>`;

  if (it.requested_actions.length) {
    html += `<div class="field" style="display:block"><span class="k">Дії</span>
      <ul class="actions">${it.requested_actions.map((a) => `<li>${esc(a)}</li>`).join('')}</ul></div>`;
  } else {
    html += '<div class="field"><span class="k">Дії</span><span style="opacity:.5">конкретних дій не названо</span></div>';
  }

  if (it.clarification_questions.length) {
    html += `<div class="qbox"><div class="side-label">Що запитати в автора</div>
      <ul>${it.clarification_questions.map((q) => `<li>${esc(q)}</li>`).join('')}</ul></div>`;
  }

  if (it.possible_duplicate_of) {
    html += `<div class="dupe">♻️ ймовірний дубль <b>${esc(it.possible_duplicate_of)}</b> — визначено лексично, без LLM</div>`;
  }
  return html;
}

/* Сбои показываются намеренно: это доказательство, что валидация работает
   и запись не теряется, а сохраняется с диагностикой. */
function failedBody(it) {
  return `<div class="errbox">
    <div class="side-label">Модель не повернула валідну структуру</div>
    <div>${esc(it.error)}</div>
    ${it.raw_llm_output ? `<pre>${esc(it.raw_llm_output)}</pre>` : ''}
    <div style="margin-top:7px;opacity:.8">Запис збережено зі status="failed" — не втрачено.</div>
  </div>`;
}

boot();
