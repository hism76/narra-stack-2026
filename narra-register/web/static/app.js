// NarraNexus 控制台核心脚本 (轻量现代化 · 极速响应 · 自动落盘 · 零丢失)
(function(){
  'use strict';
  var $ = function(id){ return document.getElementById(id); };
  var NL = String.fromCharCode(10);
  var DQ = String.fromCharCode(34);
  var AMP = String.fromCharCode(38);

  function esc(s){
    s = String(s == null ? '' : s);
    return s.split(AMP).join(AMP + 'amp;')
            .split('<').join(AMP + 'lt;')
            .split('>').join(AMP + 'gt;')
            .split(DQ).join(AMP + 'quot;');
  }

  function _n(v, dft){ var n = parseInt(v, 10); return isNaN(n) ? dft : n; }

  function safeSetVal(id, val){
    var el = $(id);
    if (!el) return;
    if (document.activeElement === el) return;
    if (val !== undefined && val !== null) {
      el.value = val;
    }
  }

  var ICONS = {
    copy: '<svg class="icon sm" viewBox="0 0 24 24"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>',
    trash: '<svg class="icon sm" viewBox="0 0 24 24"><path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/></svg>'
  };

  function toast(msg, type){
    var el = $('toast');
    if (!el) return;
    el.className = 'toast ' + (type || 'ok');
    el.textContent = msg;
    el.style.display = 'block';
    setTimeout(function(){ el.style.display = 'none'; }, 2800);
  }

  window.copyText = function(text, label){
    if (!text) return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function(){
        toast((label || '内容') + ' 已复制到剪贴板', 'ok');
      }).catch(function(){
        fallbackCopy(text, label);
      });
    } else {
      fallbackCopy(text, label);
    }
  };

  function fallbackCopy(text, label){
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand('copy');
      toast((label || '内容') + ' 已复制', 'ok');
    } catch(e) {
      toast('复制失败，请手动选取', 'err');
    }
    document.body.removeChild(ta);
  }

  async function api(path, opts){
    opts = opts || {};
    opts.headers = opts.headers || {};
    opts.headers['Content-Type'] = 'application/json';
    try {
      var res = await fetch(path, opts);
      if (res.status === 401) {
        toast('未授权，请检查管理密码', 'err');
        return {};
      }
      return await res.json();
    } catch(e) {
      console.error('[API]', path, e);
      return {};
    }
  }

  // ---------------- 标签页切换 ----------------
  var tabs = document.querySelectorAll('.tab');
  tabs.forEach(function(btn){
    btn.onclick = function(){
      tabs.forEach(function(t){ t.classList.remove('active'); });
      document.querySelectorAll('.panel').forEach(function(p){ p.classList.remove('active'); });
      btn.classList.add('active');
      var pId = btn.getAttribute('data-p');
      if ($(pId)) $(pId).classList.add('active');
      if (pId === 'p-accounts') { loadAccounts(); loadOverview(); }
      if (pId === 'p-keys') loadKeys();
      if (pId === 'p-settings') loadSettings();
      if (pId === 'p-overview') loadOverview();
    };
  });

  // ---------------- 概览与统计 ----------------
  async function loadOverview(){
    try {
      var res = await api('/api/status');
      if (!res || !res.data) {
        res = await api('/api/pool/status');
      }
      var d = (res && res.data) || {};
      var total = d.total_accounts != null ? d.total_accounts : (d.total != null ? d.total : 0);
      var active = d.active_accounts != null ? d.active_accounts : (d.active != null ? d.active : 0);
      var cooling = d.cooling_accounts != null ? d.cooling_accounts : (d.cooling != null ? d.cooling : 0);

      if ($('st-total')) $('st-total').textContent = total;
      if ($('st-active')) $('st-active').textContent = active;
      if ($('st-cooling')) $('st-cooling').textContent = cooling;
    } catch(e){
      console.error('loadOverview error:', e);
    }
  }

  // ---------------- 账号管理 ----------------
  async function loadAccounts(){
    try {
      var res = await api('/api/accounts?page=1&size=100');
      var list = (res.data && res.data.accounts) || [];
      var tbody = $('accounts-tbody');
      if (!tbody) return;

      if (list.length > 0) {
        if ($('st-total')) $('st-total').textContent = list.length;
        var actCnt = list.filter(function(a){ return a.status === 'active'; }).length;
        var coolCnt = list.filter(function(a){ return a.status === 'cooling'; }).length;
        if ($('st-active')) $('st-active').textContent = actCnt;
        if ($('st-cooling')) $('st-cooling').textContent = coolCnt;
      }

      if (!list.length) {
        tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:24px">暂无账号，请前往「批量自动注册」快速生成</td></tr>';
        return;
      }
      var rows = '';
      list.forEach(function(acc){
        var stClass = acc.status === 'active' ? 'ok' : (acc.status === 'cooling' ? 'cooling' : 'invalid');
        var stText = acc.status === 'active' ? '活跃可用' : (acc.status === 'cooling' ? '冷却中' : '失效');
        var quota = (acc.balance_quota != null && acc.balance_quota >= 0) ? ('$' + acc.balance_quota) : '$3.00';
        var timeStr = acc.updated_at ? acc.updated_at.replace('T', ' ').slice(0, 19) : '-';

        rows += '<tr>' +
          '<td class="sub">' + acc.id + '</td>' +
          '<td class="mono"><b>' + esc(acc.email) + '</b></td>' +
          '<td><span class="badge ' + stClass + '">' + stText + '</span></td>' +
          '<td><b style="color:var(--ok)">' + quota + '</b></td>' +
          '<td class="sub">' + (acc.success_count || 0) + ' / ' + (acc.fail_count || 0) + '</td>' +
          '<td class="sub">' + timeStr + '</td>' +
          '<td>' +
            '<button class="btn sm" onclick="testAccount(\'' + esc(acc.email) + '\')">测试</button> ' +
            '<button class="btn sm danger" onclick="deleteAccount(\'' + esc(acc.email) + '\')">' + ICONS.trash + '</button>' +
          '</td>' +
        '</tr>';
      });
      tbody.innerHTML = rows;
    } catch(e){}
  }

  window.deleteAccount = async function(email){
    if (!confirm('确定删除账号 ' + email + ' 吗？')) return;
    var res = await api('/api/accounts/delete', {
      method: 'POST',
      body: JSON.stringify({ email: email })
    });
    toast(res.message || '已删除', res.status === 'success' ? 'ok' : 'err');
    loadAccounts();
    loadOverview();
  };

  window.testAccount = async function(email){
    toast('正在测试账号连通性...', 'ok');
    var res = await api('/api/accounts/test-single', {
      method: 'POST',
      body: JSON.stringify({ email: email })
    });
    toast(res.message || '测试完成', res.status === 'success' ? 'ok' : 'err');
    loadAccounts();
    loadOverview();
  };

  // ---------------- 批量自动注册 ----------------
  if ($('btn-task-start')) {
    $('btn-task-start').onclick = async function(){
      var cnt = _n($('tk-count').value, 5);
      var iv = _n($('tk-interval').value, 2);
      var dom = $('tk-domain') ? $('tk-domain').value : 'smart';
      var tout = _n($('tk-timeout') ? $('tk-timeout').value : 60, 60);

      var j = await api('/api/task/start', {
        method: 'POST',
        body: JSON.stringify({
          count: cnt,
          interval_seconds: iv,
          domain_strategy: dom,
          code_timeout: tout
        })
      });
      toast(j.message || '注册任务已启动', j.status === 'success' ? 'ok' : 'warn');
      loadTaskStatus();
    };
  }

  if ($('btn-task-stop')) {
    $('btn-task-stop').onclick = async function(){
      var j = await api('/api/task/stop', {method: 'POST'});
      toast(j.message || '停止信号已发送', 'ok');
      loadTaskStatus();
    };
  }

  if ($('btn-resume')) {
    $('btn-resume').onclick = async function(){
      var tout = _n($('tk-timeout') ? $('tk-timeout').value : 60, 60);
      var j = await api('/api/task/start', {
        method: 'POST',
        body: JSON.stringify({ is_resume: true, code_timeout: tout })
      });
      toast(j.message || '恢复任务已触发', j.status === 'success' ? 'ok' : 'warn');
      loadTaskStatus();
    };
  }

  if ($('btn-task-clear')) {
    $('btn-task-clear').onclick = async function(){
      await api('/api/task/clear-logs', {method: 'POST'});
      if ($('logs')) $('logs').textContent = '';
    };
  }

  if ($('btn-test-yyds')) {
    $('btn-test-yyds').onclick = async function(){
      var key = $('st-yyds') ? $('st-yyds').value.trim() : '';
      var j = await api('/api/settings/test-yyds', {
        method: 'POST',
        body: JSON.stringify({ api_key: key })
      });
      toast(j.message || '测试完成', j.status === 'success' ? 'ok' : 'err');
    };
  }

  async function loadTaskStatus(){
    try {
      var res = await api('/api/task/status');
      var d = res.data || {};
      var st = d.status || 'idle';
      if ($('task-state')) $('task-state').textContent = st === 'running' ? '进行中' : (st === 'paused' ? '已暂停' : '就绪');
      
      var total = d.target_count || d.total || 0;
      var cur = d.completed_count || d.current || 0;
      var pct = total > 0 ? Math.min(100, Math.round((cur / total) * 100)) : 0;
      if ($('task-prog')) $('task-prog').style.width = pct + '%';
      if ($('task-text')) $('task-text').textContent = '进度: ' + cur + ' / ' + total + ' (' + pct + '%) · 成功: ' + (d.success_count || 0) + ', 失败: ' + (d.fail_count || 0);

      var logsBox = $('logs');
      if (logsBox && d.logs) {
        logsBox.textContent = d.logs.join(NL);
        if ($('autoscroll') && $('autoscroll').checked) {
          logsBox.scrollTop = logsBox.scrollHeight;
        }
      }
    } catch(e){}
  }

  // ---------------- API Keys 管理 (自动即时持久化) ----------------
  var keysList = [];
  async function loadKeys(){
    try {
      var res = await api('/api/keys');
      keysList = res.keys || res.data || [];
      renderKeys();
    } catch(e){}
  }

  function renderKeys(){
    var tbody = $('keys-tbody');
    if (!tbody) return;
    if (!keysList.length) {
      tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:24px">暂无 Key，可在上方添加</td></tr>';
      return;
    }
    var rows = '';
    keysList.forEach(function(k, idx){
      var full = k.key || '';
      var mask = full.length > 12 ? (full.slice(0, 8) + '••••' + full.slice(-4)) : full;
      rows += '<tr>' +
        '<td><input class="input" value="' + esc(k.name || '') + '" onchange="updateKey(' + idx + ',\'name\',this.value)" style="width:140px"></td>' +
        '<td class="mono"><code style="cursor:pointer" onclick="copyText(\'' + esc(full) + '\',\'API Key\')">' + esc(mask) + '</code></td>' +
        '<td><input class="input" type="number" value="' + (k.rpm || 0) + '" onchange="updateKey(' + idx + ',\'rpm\',this.value)" style="width:80px"></td>' +
        '<td class="sub">' + (k.requests || 0) + ' / ' + (k.success || 0) + ' / ' + (k.fail || 0) + '</td>' +
        '<td class="sub">' + (k.last_used ? new Date(k.last_used*1000).toLocaleString() : '-') + '</td>' +
        '<td>' +
          '<button class="btn sm" onclick="copyText(\'' + esc(full) + '\',\'API Key\')">' + ICONS.copy + '</button> ' +
          '<button class="btn sm danger" onclick="deleteKey(' + idx + ')">' + ICONS.trash + '</button>' +
        '</td>' +
      '</tr>';
    });
    tbody.innerHTML = rows;
  }

  window.updateKey = async function(idx, field, val){
    if (!keysList[idx]) return;
    keysList[idx][field] = field === 'rpm' ? _n(val, 0) : val;
    // 立即自动保存入库
    await api('/api/keys', {
      method: 'POST',
      body: JSON.stringify({ keys: keysList })
    });
  };

  window.deleteKey = async function(idx){
    if (!confirm('确定删除该 API Key 吗？')) return;
    keysList.splice(idx, 1);
    renderKeys();
    // 立即自动保存入库
    var res = await api('/api/keys', {
      method: 'POST',
      body: JSON.stringify({ keys: keysList })
    });
    toast('API Key 已删除并保存', res.status === 'success' ? 'ok' : 'err');
  };

  if ($('btn-key-add')) {
    $('btn-key-add').onclick = async function(){
      var name = $('nk-name').value.trim() || 'Client';
      var key = $('nk-key').value.trim();
      if (!key) {
        key = 'sk-narra-' + Math.random().toString(36).substring(2, 10) + Math.random().toString(36).substring(2, 10);
      }
      var rpm = _n($('nk-rpm').value, 0);
      keysList.push({ name: name, key: key, rpm: rpm, requests: 0, success: 0, fail: 0 });
      renderKeys();
      $('nk-name').value = '';
      $('nk-key').value = '';
      $('nk-rpm').value = '';

      // 立即自动发送保存请求入库，彻底防止刷新后丢失！
      var res = await api('/api/keys', {
        method: 'POST',
        body: JSON.stringify({ keys: keysList })
      });
      toast((res.message || 'API Key 已创建并自动保存') + ' 🔑', res.status === 'success' ? 'ok' : 'err');
    };
  }

  if ($('btn-keys-save')) {
    $('btn-keys-save').onclick = async function(){
      var res = await api('/api/keys', {
        method: 'POST',
        body: JSON.stringify({ keys: keysList })
      });
      toast(res.message || 'Key 列表已保存生效', res.status === 'success' ? 'ok' : 'err');
    };
  }

  // ---------------- 系统核心设置 ----------------
  async function loadSettings(){
    try {
      var res = await api('/api/settings');
      var d = res.data || {};
      safeSetVal('st-adminuser', d.admin_username || 'admin');
      safeSetVal('st-adminpass', '');
      safeSetVal('st-yyds', d.yyds_mail_api_key || '');
      safeSetVal('st-proxy', d.proxy_url || 'http://clash-proxy:7890');
      safeSetVal('st-timeout', d.verification_code_timeout || '60');
      safeSetVal('st-effort', d.default_reasoning_effort || 'medium');
      safeSetVal('st-fastmode', (d.fast_mode_enabled !== undefined && d.fast_mode_enabled !== null) ? String(d.fast_mode_enabled) : 'true');
      if ($('tk-timeout') && !$('tk-timeout').value) {
        safeSetVal('tk-timeout', d.verification_code_timeout || '60');
      }
    } catch(e){}
  }

  if ($('btn-settings-save')) {
    $('btn-settings-save').onclick = async function(){
      var payload = {
        admin_username: $('st-adminuser').value.trim() || 'admin',
        yyds_mail_api_key: $('st-yyds').value.trim(),
        proxy_url: $('st-proxy').value.trim(),
        verification_code_timeout: $('st-timeout').value.trim() || '60',
        default_reasoning_effort: $('st-effort').value || 'medium',
        fast_mode_enabled: $('st-fastmode') ? $('st-fastmode').value : 'true'
      };
      if ($('st-adminpass').value) {
        payload.admin_password = $('st-adminpass').value;
      }
      var res = await api('/api/settings', {
        method: 'POST',
        body: JSON.stringify(payload)
      });
      toast(res.message || '核心系统设置已保存生效', res.status === 'success' ? 'ok' : 'err');
      loadSettings();
    };
  }

  if ($('btn-refresh')) {
    $('btn-refresh').onclick = function(){
      loadOverview();
      loadAccounts();
      loadKeys();
      loadTaskStatus();
      toast('数据已刷新', 'ok');
    };
  }

  // 页面启动与轮询
  loadOverview();
  loadAccounts();
  loadKeys();
  loadSettings();
  loadTaskStatus();
  setInterval(function(){
    var activePanel = document.querySelector('.panel.active');
    if (activePanel && activePanel.id === 'p-task') {
      loadTaskStatus();
    }
  }, 3000);

})();