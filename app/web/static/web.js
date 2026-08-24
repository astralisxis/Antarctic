const root = document.getElementById('app-main');
const modal = document.getElementById('modal');
const modalContent = document.getElementById('modal-content');
const drawer = document.getElementById('drawer');
const mode = document.body.dataset.mode || 'site';
let me = {authenticated: false, guest: false};
let currentView = 'shop';
let catalogItems = [];

const escapeHtml = value => String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const rubles = kopecks => `${new Intl.NumberFormat('ru-RU').format(Math.round(Number(kopecks || 0) / 100))} ₽`;
const shortDate = raw => raw ? new Intl.DateTimeFormat('ru-RU', {day:'2-digit',month:'2-digit',year:'numeric'}).format(new Date(raw)) : '—';
const refreshIcons = () => requestAnimationFrame(() => window.lucide?.createIcons());
const flagForCode = code => {
  const normalized = String(code || '').trim().toUpperCase();
  return /^[A-Z]{2}$/.test(normalized)
    ? String.fromCodePoint(...normalized.split('').map(char => 127397 + char.charCodeAt(0)))
    : '';
};

async function api(url, options = {}) {
  const response = await fetch(url, {...options, headers: {'Content-Type':'application/json', ...(options.headers || {})}});
  let data = {};
  try { data = await response.json(); } catch (_) {}
  if (!response.ok) throw new Error(data.detail || 'Не удалось выполнить запрос.');
  return data;
}

function loginCta(text = 'Войдите, чтобы продолжить.') {
  const action = mode === 'miniapp'
    ? '<p class="muted">Закройте страницу и откройте Mini App из меню бота.</p>'
    : '<a class="button button--primary" href="/login">Войти в аккаунт</a>';
  return `<div class="empty-state-app"><p>${escapeHtml(text)}</p>${action}</div>`;
}

function setIdentity() {
  const balance = document.getElementById('header-balance');
  const name = document.getElementById('drawer-name');
  const drawerBalance = document.getElementById('drawer-balance');
  const avatar = document.getElementById('drawer-avatar');
  const authAction = document.getElementById('drawer-auth-action');
  balance.textContent = me.authenticated ? me.balance_text : '0 ₽';
  name.textContent = me.authenticated ? me.name : 'Гость';
  drawerBalance.textContent = me.authenticated ? `Баланс · ${me.balance_text}` : 'Войдите для покупок';
  const initial = (me.name || 'A').replace('@','').trim().charAt(0).toUpperCase() || 'A';
  avatar.innerHTML = me.authenticated && me.avatar_url
    ? `<img src="${escapeHtml(me.avatar_url)}" alt="">`
    : escapeHtml(initial);
  if (authAction) authAction.innerHTML = me.authenticated
    ? '<i data-lucide="log-out"></i><span>Выйти</span>'
    : '<i data-lucide="log-in"></i><span>Войти</span>';
  refreshIcons();
}

function viewPath(view) {
  return mode === 'miniapp' ? `/app#${view}` : `/${view === 'shop' ? 'shop' : view}`;
}

function setActive(view) {
  currentView = view;
  const navView = ['referrals','leaders','promos'].includes(view) ? 'profile' : view;
  document.querySelectorAll('.bottom-nav [data-view]').forEach(button => button.classList.toggle('is-active', button.dataset.view === navView));
  history.replaceState({}, '', viewPath(view));
}

function openModal(html) {
  modalContent.innerHTML = html;
  modal.hidden = false;
  document.body.style.overflow = 'hidden';
}
function closeModal() { modal.hidden = true; document.body.style.overflow = ''; }
function openDrawer() { drawer.hidden = false; document.body.style.overflow = 'hidden'; }
function closeDrawer() { drawer.hidden = true; document.body.style.overflow = ''; }

modal.addEventListener('click', event => { if (event.target.closest('[data-close-modal]')) closeModal(); });
drawer.addEventListener('click', event => { if (event.target.closest('[data-close-drawer]')) closeDrawer(); });
document.getElementById('menu-open').addEventListener('click', openDrawer);

function cardDescription(item) {
  return item.description || 'Автоматическая выдача, код входа и гарантия замены.';
}

function syncCatalogChips() {
  const stockOnly = document.getElementById('filter-stock')?.checked;
  document.querySelectorAll('[data-catalog-chip]').forEach(button => {
    button.classList.toggle('is-active', button.dataset.catalogChip === (stockOnly ? 'stock' : 'all'));
  });
}

function applyCatalogFilters() {
  const available = document.getElementById('filter-stock')?.checked;
  const query = (document.getElementById('filter-search')?.value || '').trim().toLocaleLowerCase('ru');
  const min = Number(document.getElementById('filter-min')?.value || 0) * 100;
  const maxRaw = Number(document.getElementById('filter-max')?.value || 0);
  const max = maxRaw > 0 ? maxRaw * 100 : Number.POSITIVE_INFINITY;
  const sort = document.getElementById('filter-sort')?.value || 'default';
  let items = catalogItems.filter(item => {
    const searchable = `${item.title || ''} ${item.code || ''} ${cardDescription(item)}`.toLocaleLowerCase('ru');
    return (!available || item.stock === null || item.stock > 0)
      && item.price >= min
      && item.price <= max
      && (!query || searchable.includes(query));
  });
  if (sort === 'cheap') items.sort((a,b) => a.price - b.price);
  else if (sort === 'expensive') items.sort((a,b) => b.price - a.price);
  else if (sort === 'new') items.sort((a,b) => String(b.created_at).localeCompare(String(a.created_at)));
  else if (sort === 'popular') items.sort((a,b) => b.sold - a.sold);
  else items.sort((a,b) => a.title.localeCompare(b.title, 'ru'));
  const grid = document.getElementById('catalog-grid');
  const count = document.getElementById('catalog-count');
  if (!grid) return;
  count.textContent = `${items.length} товаров`;
  grid.innerHTML = items.length ? items.map(item => `
    <button class="offer-card" data-offer="${item.id}" type="button">
      <div>
        <div class="offer-card__top"><span class="offer-code">${escapeHtml(item.code)}</span><span class="offer-stock">${escapeHtml(item.stock_text)}</span></div>
        <h2>${flagForCode(item.code)} ${escapeHtml(item.title)}</h2>
        <p class="offer-card__desc">${escapeHtml(cardDescription(item))}</p>
      </div>
      <div class="offer-card__foot"><div><span class="offer-price">${escapeHtml(item.price_text)}</span><span class="offer-meta">Гарантия ${escapeHtml(item.guarantee_hours)} ч. · ${item.sold} купили</span></div><span class="offer-buy"><i data-lucide="shopping-cart"></i> Купить</span></div>
    </button>`).join('') : '<div class="empty-state-app">По выбранным фильтрам ничего не найдено.</div>';
  grid.querySelectorAll('[data-offer]').forEach(card => card.addEventListener('click', () => showOffer(Number(card.dataset.offer))));
  syncCatalogChips();
  refreshIcons();
}

async function renderShop() {
  setActive('shop');
  root.innerHTML = '<div class="loading-state">Загрузка каталога</div>';
  try {
    const {items} = await api('/api/catalog');
    catalogItems = items;
    root.innerHTML = `
      <section class="catalog-hero">
        <div class="page-head"><div><div class="page-kicker">ANTARCTIC SHOP</div><h1>Каталог аккаунтов</h1><p>Автоматическая выдача, получение кода и гарантия замены.</p></div></div>
        <label class="catalog-search"><i data-lucide="search"></i><input id="filter-search" type="search" placeholder="Поиск страны или товара" autocomplete="off"></label>
      </section>
      <div class="catalog-chips" aria-label="Быстрые фильтры">
        <button class="catalog-chip is-active" data-catalog-chip="all" type="button">Все товары</button>
        <button class="catalog-chip" data-catalog-chip="stock" type="button">Только в наличии</button>
        <button class="catalog-chip" data-catalog-sort="cheap" type="button">Сначала дешевле</button>
        <button class="catalog-chip" data-catalog-sort="popular" type="button">Популярные</button>
      </div>
      <div class="catalog-layout">
        <aside class="filters-panel">
          <div class="filters-title"><button class="filters-toggle" id="filter-toggle" type="button" aria-expanded="true"><i data-lucide="sliders-horizontal"></i><strong>Фильтры</strong><i data-lucide="chevron-down"></i></button><button class="filter-reset" id="filter-reset" type="button">Сбросить</button></div>
          <div class="filter-group"><span>НАЛИЧИЕ</span><label class="stock-filter"><span>Есть в продаже</span><input id="filter-stock" type="checkbox"></label></div>
          <div class="filter-group"><span>ЦЕНА, ₽</span><div class="price-fields"><label class="filter-field"><input id="filter-min" inputmode="numeric" type="number" min="0" placeholder="От"></label><label class="filter-field"><input id="filter-max" inputmode="numeric" type="number" min="0" placeholder="До"></label></div></div>
        </aside>
        <section class="catalog-results">
          <div class="catalog-toolbar"><span class="catalog-meta" id="catalog-count"></span><select class="sort-field" id="filter-sort" aria-label="Сортировка"><option value="default">По умолчанию</option><option value="cheap">Сначала дешевле</option><option value="expensive">Сначала дороже</option><option value="new">Новые</option><option value="popular">Популярные</option></select></div>
          <div class="catalog-grid" id="catalog-grid"></div>
        </section>
      </div>`;
    root.querySelectorAll('#filter-search,#filter-stock,#filter-min,#filter-max,#filter-sort').forEach(control => control.addEventListener('input', applyCatalogFilters));
    root.querySelectorAll('[data-catalog-chip]').forEach(button => button.addEventListener('click', () => {
      document.getElementById('filter-stock').checked = button.dataset.catalogChip === 'stock';
      applyCatalogFilters();
    }));
    root.querySelectorAll('[data-catalog-sort]').forEach(button => button.addEventListener('click', () => {
      document.getElementById('filter-sort').value = button.dataset.catalogSort;
      applyCatalogFilters();
    }));
    const filtersPanel = root.querySelector('.filters-panel');
    const filterToggle = document.getElementById('filter-toggle');
    if (window.matchMedia('(max-width: 680px)').matches) {
      filtersPanel.classList.add('is-collapsed');
      filterToggle.setAttribute('aria-expanded', 'false');
    }
    filterToggle.addEventListener('click', () => {
      const collapsed = filtersPanel.classList.toggle('is-collapsed');
      filterToggle.setAttribute('aria-expanded', String(!collapsed));
    });
    document.getElementById('filter-reset').addEventListener('click', () => {
      document.getElementById('filter-search').value = '';
      document.getElementById('filter-stock').checked = false;
      document.getElementById('filter-min').value = '';
      document.getElementById('filter-max').value = '';
      document.getElementById('filter-sort').value = 'default';
      applyCatalogFilters();
    });
    applyCatalogFilters();
  } catch (error) { root.innerHTML = `<div class="empty-state-app">${escapeHtml(error.message)}</div>`; }
}

async function showOffer(id) {
  openModal('<div class="loading-state">Открываем товар</div>');
  try {
    const item = await api(`/api/catalog/${id}`);
    const inStock = item.stock === null || item.stock > 0;
    const canAfford = me.authenticated && me.balance >= item.price;
    let action = '';
    if (!me.authenticated) action = mode === 'site' ? '<a class="button button--primary" href="/login">Войти для покупки</a>' : '<button class="button button--primary" disabled>Нужен вход Telegram</button>';
    else if (!inStock) action = '<button class="button button--primary" disabled>Нет в наличии</button>';
    else if (!canAfford) action = `<button class="button button--primary" data-go-topup type="button">Пополнить баланс</button><div class="inline-message">На балансе ${escapeHtml(me.balance_text)}, не хватает ${rubles(item.price - me.balance)}.</div>`;
    else action = `<button class="button button--primary" data-buy="${item.id}" type="button">Купить за ${escapeHtml(item.price_text)}</button>`;
    modalContent.innerHTML = `
      <div class="modal__head"><div><div class="page-kicker">${escapeHtml(item.code)}</div><h2>${escapeHtml(item.title)}</h2></div><button class="icon-button" data-close-modal type="button">×</button></div>
      <div class="details"><div class="detail-row"><span>Цена</span><strong>${escapeHtml(item.price_text)}</strong></div><div class="detail-row"><span>Наличие</span><span>${escapeHtml(item.stock_text)}</span></div><div class="detail-row"><span>Гарантия</span><span>${escapeHtml(item.guarantee_hours)} ч.</span></div></div>
      ${item.description ? `<p>${escapeHtml(item.description)}</p>` : ''}
      <div class="rules-block">
        <div class="rule"><strong>Условия покупки</strong><p>Автоматическая выдача товара после оплаты. Код входа можно получать в течение ${escapeHtml(item.guarantee_hours)} ч. Гарантия замены — ${escapeHtml(item.guarantee_hours)} ч.</p></div>
        <div class="rule"><strong>Рекомендация</strong><p>Желательно входить через VPN или прокси страны купленного номера.</p></div>
      </div>
      <div class="channel-links"><a href="https://t.me/reviews_antarctic" target="_blank">Отзывы · @reviews_antarctic</a><a href="https://t.me/antarcticXshop" target="_blank">Канал · @antarcticXshop</a></div>
      <div class="modal-actions">${action}</div>`;
    modalContent.querySelector('[data-go-topup]')?.addEventListener('click', () => { closeModal(); navigate('topup'); });
    const buyButton = modalContent.querySelector('[data-buy]');
    if (buyButton) buyButton.addEventListener('click', () => buyOffer(item, buyButton));
  } catch (error) { modalContent.innerHTML = `<div class="empty-state-app">${escapeHtml(error.message)}</div>`; }
}

async function buyOffer(item, button) {
  button.disabled = true; button.textContent = 'Подбираем аккаунт';
  try {
    const {order} = await api('/api/orders', {method:'POST', body:JSON.stringify({offer_id:item.id})});
    me = await api('/api/me'); setIdentity(); showOrder(order, true);
  } catch (error) {
    button.disabled = false; button.textContent = `Купить за ${item.price_text}`;
    let note = modalContent.querySelector('.purchase-error');
    if (!note) { note = document.createElement('div'); note.className = 'inline-message purchase-error'; modalContent.querySelector('.modal-actions').append(note); }
    note.textContent = error.message;
  }
}

function showOrder(order, fresh = false) {
  const valid = order.account_valid === true ? 'Действителен' : order.account_valid === false ? 'Недействителен' : 'Не проверялся';
  modalContent.innerHTML = `
    <div class="modal__head"><div><div class="page-kicker">ЗАКАЗ №${order.id}</div><h2>${escapeHtml(order.title)}</h2></div><button class="icon-button" data-close-modal type="button">×</button></div>
    ${fresh ? '<div class="auth-notice" style="margin-top:18px">Аккаунт выдан и сохранён в профиле.</div>' : ''}
    <div class="details"><div class="detail-row"><span>Номер</span><strong>${escapeHtml(order.phone || '—')}</strong></div><div class="detail-row"><span>Облачный пароль</span><strong>${escapeHtml(order.tg_password || 'нет')}</strong></div><div class="detail-row"><span>Статус</span><span>${escapeHtml(order.status_text)}</span></div><div class="detail-row"><span>Проверка</span><span>${escapeHtml(valid)}</span></div></div>
    <div id="order-code">${order.code ? `<div class="code-box">${escapeHtml(order.code)}</div>` : ''}</div>
    <div class="modal-actions">${order.can_code ? `<button class="button button--primary" data-code="${order.id}" type="button">Получить код</button>` : ''}${order.can_replace ? `<button class="button" data-replace="${order.id}" type="button">Запросить замену</button>` : ''}</div>`;
  const codeButton = modalContent.querySelector('[data-code]');
  if (codeButton) codeButton.addEventListener('click', async () => {
    codeButton.disabled = true; codeButton.textContent = 'Запрашиваем код';
    try { const data = await api(`/api/orders/${order.id}/code`, {method:'POST'}); document.getElementById('order-code').innerHTML = `<div class="code-box">${escapeHtml(data.code)}</div>`; codeButton.textContent = 'Запросить ещё раз'; codeButton.disabled = false; }
    catch (error) { codeButton.textContent = error.message; }
  });
  const replaceButton = modalContent.querySelector('[data-replace]');
  if (replaceButton) replaceButton.addEventListener('click', async () => {
    replaceButton.disabled = true; replaceButton.textContent = 'Отправляем заявку';
    try { await api(`/api/orders/${order.id}/replacement`, {method:'POST'}); replaceButton.textContent = 'Заявка отправлена'; }
    catch (error) { replaceButton.textContent = error.message; }
  });
}

async function renderProfile() {
  setActive('profile');
  if (!me.authenticated) { root.innerHTML = `<div class="page-head"><div><div class="page-kicker">АККАУНТ</div><h1>Профиль</h1><p>Покупки, баланс и полученные аккаунты.</p></div></div>${loginCta()}`; return; }
  root.innerHTML = '<div class="loading-state">Загрузка профиля</div>';
  try {
    const data = await api('/api/profile'); me = data.user; setIdentity();
    const avatar = me.avatar_url ? `<img src="${escapeHtml(me.avatar_url)}" alt="">` : escapeHtml((me.name || 'A').replace('@','').charAt(0).toUpperCase());
    root.innerHTML = `<div class="page-head"><div><div class="page-kicker">АККАУНТ</div><h1>Профиль</h1><p>Баланс, заказы и персональные возможности в одном месте.</p></div></div>
      <div class="profile-grid"><section class="panel"><div class="avatar profile-avatar">${avatar}</div><p class="profile-name">${escapeHtml(me.name)}</p><p class="muted">${escapeHtml(me.email || (me.username ? '@'+me.username : me.provider))}</p><div class="stats"><div class="stat"><strong>${escapeHtml(me.balance_text)}</strong><span>Баланс</span></div><div class="stat"><strong>${escapeHtml(me.orders_count)}</strong><span>Покупки</span></div><div class="stat"><strong>${data.leader_position || '—'}</strong><span>Место в рейтинге</span></div><div class="stat"><strong>${escapeHtml(data.referral.earned_text)}</strong><span>С рефералов</span></div></div><div class="modal-actions"><button class="button" data-profile-view="referrals">Рефералы</button><button class="button" data-profile-view="leaders">Лидеры</button><button class="button" data-profile-view="promos">Промокоды</button><button class="button" id="logout">Выйти</button></div></section>
      <section class="panel"><div class="section-title"><h2>Мои аккаунты</h2><span class="muted">${data.orders.length}</span></div><div class="order-list">${data.orders.length ? data.orders.map(order => `<button class="order-row" data-order="${order.id}" type="button"><div><strong>${escapeHtml(order.title)}</strong><span>${escapeHtml(order.phone || 'номер не выдан')}</span></div><div class="order-row__right"><strong>${escapeHtml(order.price_text)}</strong><span>${escapeHtml(order.status_text)}</span></div></button>`).join('') : '<div class="empty-state-app">Покупок пока нет.</div>'}</div></section></div>`;
    document.getElementById('logout').addEventListener('click', async () => { const result = await api('/auth/logout',{method:'POST'}); location.href=result.redirect; });
    root.querySelectorAll('[data-profile-view]').forEach(button => button.addEventListener('click', () => navigate(button.dataset.profileView)));
    root.querySelectorAll('[data-order]').forEach(row => row.addEventListener('click', () => { openModal(''); showOrder(data.orders.find(item => item.id === Number(row.dataset.order))); }));
  } catch (error) { root.innerHTML = `<div class="empty-state-app">${escapeHtml(error.message)}</div>`; }
}

async function renderSupport() {
  setActive('support'); root.innerHTML = '<div class="loading-state">Загрузка поддержки</div>';
  try {
    const data = await api('/api/support');
    const thread = data.logged_in ? `<div class="thread">${data.messages.length ? data.messages.map(message => `<div class="message message--${escapeHtml(message.sender)}">${escapeHtml(message.text)}</div>`).join('') : '<div class="empty-state-app">Напишите вопрос — он появится в админ-панели.</div>'}</div><form class="form-grid" id="support-form"><textarea name="text" maxlength="3000" placeholder="Опишите вопрос" required></textarea><button class="button button--primary" type="submit">Отправить</button><div class="inline-message" id="support-status"></div></form>` : loginCta('Войдите, чтобы написать в поддержку и видеть ответы.');
    root.innerHTML = `<div class="page-head"><div><div class="page-kicker">ПОМОЩЬ</div><h1>Поддержка</h1><p>Разберём вопрос по заказу, оплате или замене.</p></div></div><div class="support-layout"><section class="panel support-info"><h2>${escapeHtml(data.hours)}</h2><p class="muted">Сообщение можно оставить в любое время. Ответ появится здесь и в боте.</p></section><section class="panel">${thread}</section></div>`;
    const form = document.getElementById('support-form');
    if (form) form.addEventListener('submit', async event => {
      event.preventDefault(); const button=form.querySelector('button'); const status=document.getElementById('support-status'); button.disabled=true;
      try { await api('/api/support',{method:'POST',body:JSON.stringify({text:form.elements.text.value})}); status.textContent='Сообщение отправлено.'; setTimeout(renderSupport,350); }
      catch(error){status.textContent=error.message;button.disabled=false;}
    });
  } catch(error){root.innerHTML=`<div class="empty-state-app">${escapeHtml(error.message)}</div>`;}
}

async function renderTopup() {
  setActive('topup');
  if (!me.authenticated) { root.innerHTML=`<div class="page-head"><div><div class="page-kicker">БАЛАНС</div><h1>Пополнение</h1><p>Выберите сумму и удобный способ оплаты.</p></div></div>${loginCta('Войдите, чтобы пополнить баланс.')}`; return; }
  root.innerHTML='<div class="loading-state">Загрузка способов оплаты</div>';
  try {
    const data=await api('/api/topup');
    root.innerHTML=`<div class="page-head"><div><div class="page-kicker">БАЛАНС</div><h1>Пополнение</h1><p>От ${escapeHtml(data.minimum_text)} до ${escapeHtml(data.maximum_text)}. Баланс зачисляется после подтверждения провайдера.</p></div></div>
      <div class="topup-layout"><section class="panel"><form class="form-grid" id="topup-form"><label>Сумма пополнения</label><div class="amount-input"><input name="amount" type="number" min="${Math.ceil(data.minimum/100)}" max="${Math.floor(data.maximum/100)}" placeholder="500" required><span>₽</span></div><div class="payment-methods">${data.methods.length ? data.methods.map(method=>`<button class="payment-method" type="submit" data-provider="${escapeHtml(method.provider)}"><div><strong>${escapeHtml(method.title)}</strong><span>${escapeHtml(method.hint)}</span></div><b>→</b></button>`).join('') : '<div class="empty-state-app">Способы оплаты временно недоступны.</div>'}</div><div class="inline-message" id="topup-status"></div></form></section>
      <section class="panel"><div class="section-title"><h2>Активные счета</h2><span class="muted">${data.invoices.length}</span></div><div id="invoice-list">${data.invoices.length ? data.invoices.map(invoice=>invoiceHtml(invoice)).join('') : '<div class="empty-state-app">Неоплаченных счетов нет.</div>'}</div></section></div>`;
    const form=document.getElementById('topup-form');
    form?.querySelectorAll('[data-provider]').forEach(button=>button.addEventListener('click',()=>{form.dataset.provider=button.dataset.provider;}));
    form?.addEventListener('submit',async event=>{
      event.preventDefault(); const provider=form.dataset.provider; const amount=Math.round(Number(form.elements.amount.value)*100); const status=document.getElementById('topup-status');
      if(!provider){status.textContent='Выберите способ оплаты.';return;}
      form.querySelectorAll('button').forEach(btn=>btn.disabled=true); status.textContent='Создаём счёт.';
      try{const invoice=await api('/api/topup/invoices',{method:'POST',body:JSON.stringify({provider,amount})});status.textContent='Счёт создан. Открываем оплату.';openExternal(invoice.url);}
      catch(error){status.textContent=error.message;form.querySelectorAll('button').forEach(btn=>btn.disabled=false);}
    });
    bindInvoiceActions();
  } catch(error){root.innerHTML=`<div class="empty-state-app">${escapeHtml(error.message)}</div>`;}
}

function invoiceHtml(invoice){return `<div class="invoice-row" data-invoice="${invoice.id}"><span class="rank">№${invoice.id}</span><div><strong>${escapeHtml(invoice.amount_text)}</strong><span class="muted">${escapeHtml(invoice.provider)}</span></div><div><a class="button" href="${escapeHtml(invoice.url||'#')}" target="_blank">Оплатить</a><button class="button" data-check-invoice="${invoice.id}">Проверить</button></div></div>`;}
function bindInvoiceActions(){document.querySelectorAll('[data-check-invoice]').forEach(button=>button.addEventListener('click',async()=>{button.disabled=true;button.textContent='Проверяем';try{const result=await api(`/api/topup/invoices/${button.dataset.checkInvoice}/check`,{method:'POST'});if(result.status==='paid'){me.balance=result.balance;me.balance_text=result.balance_text;setIdentity();button.textContent='Оплачено';}else{button.textContent='Оплата не найдена';setTimeout(()=>{button.disabled=false;button.textContent='Проверить';},1800);}}catch(error){button.textContent=error.message;}}));}
function openExternal(url){const tg=window.Telegram?.WebApp;if(tg&&url){if(url.startsWith('https://t.me/'))tg.openTelegramLink(url);else tg.openLink(url);}else if(url)location.href=url;}

async function renderReferrals(){setActive('referrals');if(!me.authenticated){root.innerHTML=`<div class="page-head"><div><h1>Рефералы</h1></div></div>${loginCta()}`;return;}try{const data=await api('/api/profile');root.innerHTML=`<div class="page-head"><div><div class="page-kicker">ПРИГЛАШЕНИЯ</div><h1>Рефералы</h1><p>Получайте ${data.referral.percent}% с пополнений приглашённых пользователей.</p></div></div><div class="profile-grid"><section class="panel"><div class="stats"><div class="stat"><strong>${data.referral.invited}</strong><span>Приглашено</span></div><div class="stat"><strong>${escapeHtml(data.referral.earned_text)}</strong><span>Начислено</span></div></div></section><section class="panel"><div class="section-title"><h2>Ваша ссылка</h2></div><div class="referral-box"><code>${escapeHtml(data.referral.link)}</code></div><div class="copy-row"><button class="button button--primary" id="copy-ref">Скопировать</button><a class="button" target="_blank" href="https://t.me/share/url?url=${encodeURIComponent(data.referral.link)}">Поделиться</a></div></section></div>`;document.getElementById('copy-ref').addEventListener('click',async event=>{await navigator.clipboard.writeText(data.referral.link);event.currentTarget.textContent='Скопировано';});}catch(error){root.innerHTML=`<div class="empty-state-app">${escapeHtml(error.message)}</div>`;}}

async function renderLeaders(){setActive('leaders');root.innerHTML='<div class="loading-state">Загрузка рейтинга</div>';try{const data=await api('/api/leaders');root.innerHTML=`<div class="page-head"><div><div class="page-kicker">РЕЙТИНГ</div><h1>Лидеры</h1><p>Покупатели с наибольшим количеством успешных заказов.</p></div>${data.my_position?`<div class="balance-chip">Ваше место · ${data.my_position}</div>`:''}</div><section class="panel">${data.items.length?data.items.map(item=>`<div class="leader-row ${item.is_me?'is-me':''}"><span class="rank">${item.position}</span><div><strong>${escapeHtml(item.name)}</strong><span>${item.orders} покупок</span></div><strong>${escapeHtml(item.spent_text)}</strong></div>`).join(''):'<div class="empty-state-app">Рейтинг пока пуст.</div>'}</section>`;}catch(error){root.innerHTML=`<div class="empty-state-app">${escapeHtml(error.message)}</div>`;}}

async function renderPromos(){setActive('promos');if(!me.authenticated){root.innerHTML=`<div class="page-head"><div><h1>Промокоды</h1></div></div>${loginCta()}`;return;}root.innerHTML='<div class="loading-state">Загрузка промокодов</div>';try{const data=await api('/api/promos');root.innerHTML=`<div class="page-head"><div><div class="page-kicker">БОНУСЫ</div><h1>Промокоды</h1><p>Введите код и получите бонус на баланс.</p></div></div><div class="profile-grid"><section class="panel"><form class="form-grid" id="promo-form"><label for="promo-code">Промокод</label><input id="promo-code" name="code" maxlength="32" placeholder="ANTARCTIC" autocomplete="off" required><button class="button button--primary" type="submit">Активировать</button><div class="inline-message" id="promo-status"></div></form></section><section class="panel"><div class="section-title"><h2>Активированные</h2><span class="muted">${data.items.length}</span></div>${data.items.length?data.items.map(item=>`<div class="promo-row"><span class="rank">✓</span><div><strong>${escapeHtml(item.code)}</strong><span>${escapeHtml(item.title||shortDate(item.created_at))}</span></div><strong>+${escapeHtml(item.bonus_text)}</strong></div>`).join(''):'<div class="empty-state-app">Вы ещё не активировали промокоды.</div>'}</section></div>`;document.getElementById('promo-form').addEventListener('submit',async event=>{event.preventDefault();const form=event.currentTarget;const button=form.querySelector('button');const status=document.getElementById('promo-status');button.disabled=true;try{const result=await api('/api/promos/redeem',{method:'POST',body:JSON.stringify({code:form.elements.code.value})});me.balance=result.balance;me.balance_text=result.balance_text;setIdentity();status.textContent=`Начислено ${result.bonus_text}.`;setTimeout(renderPromos,700);}catch(error){status.textContent=error.message;button.disabled=false;}});}catch(error){root.innerHTML=`<div class="empty-state-app">${escapeHtml(error.message)}</div>`;}}

async function navigate(view){closeDrawer();window.scrollTo({top:0,behavior:'smooth'});if(view==='profile')await renderProfile();else if(view==='support')await renderSupport();else if(view==='topup')await renderTopup();else if(view==='referrals')await renderReferrals();else if(view==='leaders')await renderLeaders();else if(view==='promos')await renderPromos();else await renderShop();}

document.querySelectorAll('[data-view]').forEach(button=>button.addEventListener('click',()=>navigate(button.dataset.view)));
document.querySelectorAll('[data-menu-view]').forEach(button=>button.addEventListener('click',()=>navigate(button.dataset.menuView)));
document.getElementById('drawer-auth-action')?.addEventListener('click', async () => {
  if (!me.authenticated) { location.href = '/login'; return; }
  const result = await api('/auth/logout', {method:'POST'});
  location.href = result.redirect || '/';
});
window.addEventListener('load', refreshIcons);

async function init(){
  if(mode==='miniapp'){
    const tg=window.Telegram?.WebApp;
    if(tg){tg.ready();tg.expand();tg.setHeaderColor('#240202');tg.setBackgroundColor('#240202');}
    if(tg?.initData){try{await api('/api/auth/miniapp',{method:'POST',body:JSON.stringify({init_data:tg.initData})});}catch(error){root.innerHTML=`<div class="empty-state-app">${escapeHtml(error.message)}</div>`;return;}}
    else{root.innerHTML='<div class="empty-state-app">Откройте Mini App из меню Telegram-бота.</div>';return;}
  }
  me=await api('/api/me');setIdentity();
  const initial=mode==='miniapp'?(location.hash.slice(1)||'shop'):(location.pathname.split('/')[1]||'shop');
  await navigate(['shop','support','topup','profile','referrals','leaders','promos'].includes(initial)?initial:'shop');
}

init().catch(error=>{root.innerHTML=`<div class="empty-state-app">${escapeHtml(error.message)}</div>`;});
