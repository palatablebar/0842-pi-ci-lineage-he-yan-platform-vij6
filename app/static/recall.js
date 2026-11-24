(function () {
  var $ = function (id) { return document.getElementById(id); };
  function esc(value) {
    return String(value === null || value === undefined ? '' : value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }
  function setNotice(text, ok) {
    var node = $('notice');
    node.textContent = text || '';
    node.style.color = ok === false ? '#a33' : '';
  }
  async function request(url, options) {
    var response = await fetch(url, Object.assign({ headers: { 'Content-Type': 'application/json' } }, options || {}));
    var data = await response.json().catch(function () { return {}; });
    if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
    return data;
  }
  function parentsText(parents) {
    var keys = Object.keys(parents || {});
    if (!keys.length) return '—';
    return keys.map(function (k) { return esc(k) + ':' + esc(parents[k]); }).join('; ');
  }
  function renderHolds(data) {
    $('hold-count').textContent = data.holds.length + ' 条';
    $('hold-body').innerHTML = data.holds.length ? data.holds.map(function (row) {
      return '<tr><td>' + esc(row.lot_id) + '</td><td>' + esc(row.recall_id) + '</td>' +
        '<td><span class="state-chip" data-s="' + esc(row.state) + '">' + esc(row.state) + '</span></td>' +
        '<td><code>' + esc(row.reason_code) + '</code></td><td>' + esc(row.detail) + '</td></tr>';
    }).join('') : '<tr><td class="empty" colspan="5">当前没有停发批次</td></tr>';
  }
  function renderPending(data) {
    var items = data.pending_items || [];
    $('pending-count').textContent = items.length + ' 条';
    $('pending-body').innerHTML = items.length ? items.map(function (item) {
      return '<li><span>' + esc(item.recall_id) + '</span><code>' + esc(item.reason_code) + '</code><span>' + esc(item.detail) + '</span></li>';
    }).join('') : '<li class="empty">暂无待核事项</li>';
  }
  function renderLots(data) {
    $('lot-body').innerHTML = data.lots.length ? data.lots.map(function (lot) {
      var constraints = lot.holds.length ? '<ul class="constraint-list">' + lot.holds.map(function (h) {
        return '<li>' + esc(h.recall_id) + ' · ' + esc(h.state) + ' · ' + esc(h.reason_code) + '</li>';
      }).join('') + '</ul>' : '<span class="closed-tag">无约束</span>';
      return '<tr><td>' + esc(lot.lot_id) + '</td><td class="num">v' + esc(lot.version) + '</td>' +
        '<td>' + parentsText(lot.parents) + '</td>' +
        '<td class="num">' + esc(lot.qty_in) + ' / ' + esc(lot.qty_out) + '</td>' +
        '<td><span class="ship-tag" data-s="' + esc(lot.shipping) + '">' +
        ({ HOLD: '停发', PENDING: '待核停发', SHIPPABLE: '照常出货' }[lot.shipping] || esc(lot.shipping)) +
        '</span></td><td>' + constraints + '</td></tr>';
    }).join('') : '<tr><td class="empty" colspan="6">尚未登记批次</td></tr>';
  }
  function renderRecalls(data) {
    $('recall-body').innerHTML = data.recalls.length ? data.recalls.map(function (item) {
      var closed = item.kind === 'CLOSED';
      return '<li class="recall-line"><span class="' + (closed ? 'closed' : '') + '">' +
        esc(item.recall_id) + ' · 问题批次 ' + esc(item.problem_lot) +
        (item.reason ? ' · ' + esc(item.reason) : '') + '</span>' +
        (closed ? '<span class="closed-tag">已解除</span>'
          : '<button type="button" data-close="' + esc(item.recall_id) + '">解除召回</button>') + '</li>';
    }).join('') : '<li class="empty">暂无召回事件</li>';
    Array.from(document.querySelectorAll('[data-close]')).forEach(function (button) {
      button.addEventListener('click', function () { return closeRecall(button.getAttribute('data-close')); });
    });
  }
  function render(data) {
    $('k-lots').textContent = data.lots.length;
    $('k-holds').textContent = data.hold_count;
    $('k-pending').textContent = data.pending_count;
    $('k-recalls').textContent = data.recalls.filter(function (r) { return r.kind === 'OPEN'; }).length;
    renderHolds(data);
    renderPending(data);
    renderLots(data);
    renderRecalls(data);
    $('updated-at').textContent = '已同步 · ' + new Date().toLocaleTimeString();
  }
  async function refresh() {
    render(await request('/api/recall/board'));
  }
  function formValues(form) {
    var out = {};
    new FormData(form).forEach(function (value, key) { out[key] = String(value).trim(); });
    return out;
  }
  async function submitLot(event) {
    event.preventDefault();
    var values = formValues(event.target);
    try {
      setNotice('');
      var payload = {
        lot_id: values.lot_id, parent_lot: values.parent_lot,
        qty_in: Number(values.qty_in), qty_out: Number(values.qty_out),
        loss_qty: Number(values.loss_qty || 0),
      };
      var result = await request('/api/recall/lots', { method: 'POST', body: JSON.stringify(payload) });
      render(result.recompute);
      setNotice(result.changed ? '批次已登记，停发名单已重算' : '内容未变化，沿用既有记录', true);
      event.target.reset();
    } catch (error) { setNotice(error.message, false); }
  }
  async function submitRecall(event) {
    event.preventDefault();
    var values = formValues(event.target);
    try {
      setNotice('');
      var payload = { recall_id: values.recall_id, problem_lot: values.problem_lot, reason: values.reason };
      if (values.event_id) payload.event_id = values.event_id;
      var result = await request('/api/recall/recalls', { method: 'POST', body: JSON.stringify(payload) });
      render(result.recompute);
      setNotice(result.created ? '召回已登记，闭包停发已生成' : '同一起召回重试，未新增停发记录', true);
      event.target.reset();
    } catch (error) { setNotice(error.message, false); }
  }
  async function closeRecall(recallId) {
    try {
      setNotice('');
      var result = await request('/api/recall/recalls/' + encodeURIComponent(recallId) + '/close', { method: 'POST' });
      render(result.recompute);
      setNotice('召回 ' + recallId + ' 已解除，仅释放其对应停发', true);
    } catch (error) { setNotice(error.message, false); }
  }
  document.addEventListener('DOMContentLoaded', function () {
    $('lot-form').addEventListener('submit', submitLot);
    $('recall-form').addEventListener('submit', submitRecall);
    refresh().catch(function (error) { setNotice(error.message, false); });
  });
})();
