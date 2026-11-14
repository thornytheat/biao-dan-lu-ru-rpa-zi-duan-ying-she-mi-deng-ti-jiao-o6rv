// 原生 JS：dry-run 走 fetch、冲突建议一键采纳、提交失败就地显示 409/422 原因。
// 所有表单在禁用 JS 时仍能提交（服务端渲染兜底），JS 只是增强。
(function () {
  function qs(sel, root) { return (root || document).querySelector(sel); }
  function qsa(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  async function postJSON(url, payload) {
    var res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload || {})
    });
    var data = null;
    try { data = await res.json(); } catch (e) { data = { detail: '响应不是 JSON' }; }
    if (!res.ok) {
      var err = new Error((data && (data.detail || data.message)) || ('请求失败：' + res.status));
      err.payload = data;
      err.status = res.status;
      throw err;
    }
    return data;
  }

  function showError(err) {
    var host = qs('#form-error') || qs('#run-error');
    if (!host) { alert(err && err.message ? err.message : String(err)); return; }
    var extra = (err && err.payload && err.payload.code) ? ' [' + err.payload.code + ']' : '';
    host.hidden = false;
    host.className = 'alert alert-error';
    host.textContent = '执行失败：' + (err && err.message ? err.message : String(err)) + extra;
  }

  function versionOf(scope) {
    var el = qs('input[name="version"]', scope) || qs('#version');
    if (!el || !el.value) return null;
    var n = Number(el.value);
    return Number.isFinite(n) && n > 0 ? n : null;
  }

  // 1) 工作台选单据表单 → POST /api/dry-run → 跳 /result/{run_id}
  var runForm = qs('#run-form');
  if (runForm) {
    runForm.addEventListener('submit', async function (event) {
      event.preventDefault();
      var btn = qs('button[type="submit"]', runForm);
      if (btn) btn.disabled = true;
      try {
        var payload = { doc_no: (qs('#doc') || {}).value };
        var v = versionOf(runForm);
        if (v) payload.version = v;
        var data = await postJSON('/api/dry-run', payload);
        window.location.href = data.result_url;
      } catch (err) {
        showError(err);
        if (btn) btn.disabled = false;
      }
    });
  }

  // 2) 提交表单 → POST /api/submit → 成功跳回执；409/422 就地提示
  qsa('form[data-json-submit]').forEach(function (form) {
    form.addEventListener('submit', async function (event) {
      event.preventDefault();
      var btn = qs('button[type="submit"]', form);
      if (btn) btn.disabled = true;
      try {
        var payload = { doc_no: form.getAttribute('data-doc-no') };
        var v = versionOf(form);
        if (v) payload.version = v;
        var data = await postJSON('/api/submit', payload);
        var suffix = data.replayed ? '&replayed=1' : '';
        window.location.href = '/review?receipt=' + encodeURIComponent(data.receipt.receipt_no) + suffix;
      } catch (err) {
        showError(err);
        if (btn) btn.disabled = false;
      }
    });
  });

  // 3) 冲突建议：把规则优先级输入框改成建议值并提交规则表单重跑
  qsa('[data-apply-rule]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var ruleId = btn.getAttribute('data-apply-rule');
      var priority = btn.getAttribute('data-apply-priority');
      var input = qs('input[name="priority_' + ruleId + '"]');
      if (!input) { showError(new Error('页面缺少规则 ' + ruleId + ' 的优先级输入框')); return; }
      input.value = priority;
      var form = qs('#rules-form');
      if (form) form.submit();
    });
  });

  // 4) 结果页分段切换：同组按钮 active，同一 .panel 内 .tab-panel 只显示目标
  qsa('[data-tab-target]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var group = btn.closest('.segmented, .tabs');
      if (group) {
        qsa('button', group).forEach(function (b) { b.classList.remove('active'); });
      }
      btn.classList.add('active');
      var host = btn.closest('.panel') || document;
      qsa('.tab-panel', host).forEach(function (panel) {
        panel.hidden = panel.id !== btn.dataset.tabTarget;
      });
    });
  });

  // 5) 复核页：客户端搜索/筛选 + 统计 + 导出 CSV（纯前端增强，服务端数据不变）
  (function reviewTools() {
    var table = qs('#submissions-table');
    if (!table) return;
    var searchInput = qs('#review-search');
    var docSelect = qs('#review-doc');
    var replaySelect = qs('#review-replay');
    var stat = qs('#review-stat');

    function dataRows() {
      return qsa('tbody tr', table).filter(function (tr) {
        return !tr.hasAttribute('data-placeholder');
      });
    }

    function visibleRows() {
      return dataRows().filter(function (tr) { return !tr.hidden; });
    }

    function refreshStat() {
      if (!stat) return;
      var total = dataRows().length;
      var shown = visibleRows().length;
      stat.textContent = shown === total
        ? '共 ' + total + ' 条提交'
        : '筛选命中 ' + shown + ' / ' + total + ' 条提交';
    }

    function applyFilter() {
      var q = searchInput ? searchInput.value.trim().toLowerCase() : '';
      var doc = docSelect ? docSelect.value : '';
      var replay = replaySelect ? replaySelect.value : '';
      dataRows().forEach(function (tr) {
        var text = tr.textContent.toLowerCase();
        var ok = (!q || text.indexOf(q) !== -1)
          && (!doc || tr.getAttribute('data-doc') === doc)
          && (replay === '' || tr.getAttribute('data-replay') === replay);
        tr.hidden = !ok;
      });
      refreshStat();
    }

    [searchInput, docSelect, replaySelect].forEach(function (el) {
      if (!el) return;
      el.addEventListener('input', applyFilter);
      el.addEventListener('change', applyFilter);
    });
    refreshStat();

    function cellText(td) {
      var text = (td.textContent || '').replace(/\s+/g, ' ').trim();
      return '"' + text.replace(/"/g, '""') + '"';
    }

    function exportCsv() {
      var headers = qsa('thead th', table).map(function (th) {
        return '"' + (th.textContent || '').replace(/\s+/g, ' ').trim().replace(/"/g, '""') + '"';
      });
      var lines = [headers.join(',')];
      visibleRows().forEach(function (tr) {
        lines.push(qsa('td', tr).map(cellText).join(','));
      });
      var blob = new Blob(['\ufeff' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' });
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url;
      a.download = 'formforge-submissions.csv';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    }

    qsa('[data-export-csv]').forEach(function (btn) {
      btn.addEventListener('click', exportCsv);
    });
  })();
})();
