/* 中概+红利策略 · 看板
 *
 * 确认按钮：本地即时生效 + 可选同步到仓库。
 * GH_TOKEN 若留空，只用本地存储；填了则可跨设备同步。
 * !! 该 token 会公开可见，务必使用「细粒度 PAT + 仅本仓库 + 仅 Actions:write」!!
 */
var GH_OWNER = '';        // 例：binbin1555
var GH_REPO  = 'zhonggai-hongli';
var GH_TOKEN = '';        // 细粒度 PAT，仅 Actions:write。留空则不同步

// 兜底标题。正常情况下每条 pending 自带 label，这里只在旧数据上生效。
var ACT = {
  P1_DCA:   '中概互联 · 定投买入',
  P1_ACCEL: '中概互联 · 加码买入',
  P1_EXIT:  '中概互联 · 止盈清仓',
  P3_TIER:  '红利网格 · 调整仓位',
  SWITCH:   '三份全部清仓 · 转创业板策略'
};

function money(v){ return (v==null||isNaN(v)) ? '—' : Math.round(v).toLocaleString('zh-CN'); }
function pct(v,d){ if(v==null||isNaN(v)) return '—';
  d = d==null?2:d; var s=(v*100).toFixed(d); return (v>0?'+':'')+s+'%'; }
function cls(v){ return v>0?'up':(v<0?'down':''); }
function esc(s){ return String(s==null?'':s).replace(/[&<>]/g,function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c]; }); }

var LS_OK = (function(){
  try { localStorage.setItem('__t','1'); localStorage.removeItem('__t'); return true; }
  catch(e){ return false; }
})();
function lsGet(k){ try { return LS_OK && localStorage.getItem(k); } catch(e){ return null; } }
function lsSet(k,v){ try { if(LS_OK) localStorage.setItem(k,v); } catch(e){} }

/* 页面自查：流水线一旦停摆，data.json 会永远停在最后一次的值，
   里面的 stale_days 也是那天算好写死的。只有在浏览器里拿当前时间
   去比 generated_at，才能发现「系统已经不跑了」。 */
function selfCheck(d){
  var out = [];
  if(!LS_OK){
    out.push({level:'warn', title:'浏览器禁用了本地存储',
      what:'「我已执行」的确认状态无法保存，每次刷新都会重新出现。系统运行不受影响。',
      todo:['若在无痕/隐私模式下，换普通窗口打开','或在浏览器设置里允许本站存储数据']});
  }
  var g = String(d.generated_at||'').replace(' ','T');
  var t = Date.parse(g);
  if(!isNaN(t)){
    var days = Math.floor((Date.now()-t)/86400000);
    if(days >= 3){
      out.push({level:'critical', title:'系统已停止运行 '+days+' 天',
        what:'页面最后一次更新是 '+esc(d.generated_at)+'。流水线每天都该跑，'
             +'停这么久说明定时触发或 Actions 出了问题。下面所有数字都是那天的旧值。',
        todo:['打开 cron-job.org，看这个任务的执行历史里最近一次是什么时候、返回什么',
              '打开 GitHub 仓库的 Actions 页，看有没有失败的运行记录',
              '若 Actions 有报错，点进去看日志末尾',
              '若 cron-job 没在触发，检查它的 GitHub token 是否过期']});
    }
  }
  return out;
}

function ackKey(d, p){
  return p ? ('ack:' + d.data_updated + ':' + p.action + ':' + p.exec_date) : null;
}
function isAcked(d, p){
  var k = ackKey(d, p); if(!k) return false;
  if(lsGet(k)) return true;
  return !!(d.acknowledged && d.acknowledged[k]);
}

function render(d){
  var app = document.getElementById('app');
  var h = [];

  /* ── 故障：永远置顶，且必须写清楚该做什么 ── */
  var probs = selfCheck(d).concat(d.health || []);
  probs.sort(function(a,b){ return (a.level==='critical'?0:1) - (b.level==='critical'?0:1); });
  probs.forEach(function(x){
    h.push('<div class="prob ' + (x.level==='critical'?'crit':'warn') + '">' +
      '<div class="p-h">' + (x.level==='critical'?'⛔ 需要你处理':'⚠️ 请留意') + '</div>' +
      '<div class="p-t">' + esc(x.title) + '</div>' +
      '<div class="p-w">' + esc(x.what) + '</div>' +
      '<div class="p-s">该怎么办</div><ol>' +
      (x.todo||[]).map(function(s){ return '<li>' + esc(s) + '</li>'; }).join('') +
      '</ol></div>');
  });

  var st = d.state, met = d.metrics || {}, px = d.last_prices || {};
  var pends = (d.pending || []).slice();
  var todo = pends.filter(function(p){ return !isAcked(d, p); });

  /* ── 横幅：每个待办一条，绝不合并、绝不遮盖 ── */
  if(st && st.switched){
    h.push('<div class="banner idle"><div class="kicker">策略已切换</div>' +
      '<div class="big">停止策略A<br>执行创业板手册</div>' +
      '<div class="sub">切换日 ' + esc(st.switch_date) + '</div></div>');
  }else if(pends.length === 0){
    h.push('<div class="banner idle"><div class="kicker">今日指令</div>' +
      '<div class="big">不操作<br>继续持有</div>' +
      '<div class="sub">下一个触发点见下方观察区</div></div>');
  }else{
    if(pends.length > 1){
      h.push('<div class="tally' + (todo.length ? '' : ' clear') + '">' +
        (todo.length
          ? ('今日 <b>' + pends.length + '</b> 项待办，还有 <b>' + todo.length + '</b> 项未确认')
          : ('今日 ' + pends.length + ' 项待办 · 已全部确认')) +
        '</div>');
    }
    pends.forEach(function(p, i){
      var done = isAcked(d, p);
      var seq = pends.length > 1
        ? '<span class="seq">' + (i + 1) + ' / ' + pends.length + '</span>' : '';
      h.push('<div class="banner ' + (done ? 'done' : 'act') + '" data-i="' + i + '">' +
        '<div class="kicker">' + seq +
          (done ? '已确认执行' : ('今日指令 · ' + esc(p.exec_date) + ' 收盘执行')) + '</div>' +
        '<div class="big">' + esc(p.label || ACT[p.action] || p.action) + '</div>' +
        '<div class="sub">' + esc(p.detail || '') + '</div>' +
        (done ? '' :
          (p.precheck ? '<div class="pre">⚠️ ' + esc(p.precheck) + '</div>' : '') +
          '<div class="why">限价委托 · 避开 9:15–9:35 与 14:55–15:00</div>' +
          '<button class="ack" data-ack="' + i + '">我已执行</button>' +
          (GH_TOKEN ? '<button class="ack sync" data-sync="' + i + '">同步到仓库（跨设备）</button>' : '')
        ) + '</div>');
    });
  }

  /* ── 临近触发：还没到，但快到了 ── */
  var near = (d.triggers || []).filter(function(t){ return t.near; });
  if (near.length && !(st && st.switched)) {
    h.push('<div class="nearbar"><div class="nb-h">⚠️ 临近触发 · ' +
      near.length + ' 项</div>' +
      near.map(function(t){
        return '<div class="nb-i"><b>' + esc(t.label) + '</b>' +
               '<span>' + esc(t.short) + '</span>' +
               '<i>' + esc(t.cond) + '</i></div>';
      }).join('') +
      '<div class="nb-f">尚未触发，无需操作。到点当天会推送指令。</div></div>');
  }

  /* ── 通知横幅：不需下单，但改变了系统行为 ── */
  (d.notices || []).forEach(function(nt, i){
    if (lsGet(nt.key)) return;
    h.push('<div class="banner note" data-n="' + i + '">' +
      '<div class="kicker">' + esc(nt.date) + ' · 状态变化</div>' +
      '<div class="big">' + esc(nt.label) + '</div>' +
      '<div class="sub">' + esc(nt.detail) + '</div>' +
      (nt.extra ? '<div class="why">' + esc(nt.extra) + '</div>' : '') +
      '<button class="ack" data-note="' + i + '">知道了</button></div>');
  });

  /* ── 收益 ── */
  h.push('<div class="card"><h2>收益</h2>' +
    kv('总资产', money(met.equity) + ' 元') +
    kv('总收益', money(met.equity - d.config.total_capital) + ' 元', cls(met.total_return)) +
    kv('总收益率', pct(met.total_return), cls(met.total_return)) +
    kv('年化收益', met.cagr==null ? '— （运行未满一月）' : pct(met.cagr), cls(met.cagr)) +
    kv('最大回撤', pct(met.max_drawdown), met.max_drawdown<0?'down':'') +
    '</div>');

  /* ── 观察点（放在资产分布之前：先看下一步会发生什么） ── */
  if(d.triggers && d.triggers.length){
    var t = d.triggers.map(function(x){
      return '<div class="trig' + (x.near ? ' near' : '') + '">' +
        '<div class="tl">' + (x.near ? '⚠️ ' : '') + '离「' + esc(x.label) + '」</div>' +
        '<div class="tn">' + esc(x.need || x.short || '') + '</div>' +
        '<div class="tw">' + esc(x.now || x.cond || '') + '</div>' +
        '</div>';
    }).join('');
    h.push('<div class="card"><h2>观察点 · 越靠前越接近触发</h2>' + t + '</div>');
  }

  /* ── 资产分布 ── */
  if(st){
    var v1 = st.p1.units*px.p1_px + st.p1.cash;
    var v2 = st.p2.units*px.p2_px + st.p2.cash;
    var v3 = st.p3.units*px.p3_px + st.p3.cash;
    var tot = v1+v2+v3;
    var w = function(v){ return tot>0 ? Math.max(1, Math.round(v/tot*100)) : 0; };
    h.push('<div class="card"><h2>资产分布</h2>' +
      '<div class="stack">' +
        '<i class="s1" style="width:' + w(v1) + '%"></i>' +
        '<i class="s2" style="width:' + w(v2) + '%"></i>' +
        '<i class="s3" style="width:' + w(v3) + '%"></i>' +
      '</div>' +
      '<div class="legend">' +
        '<span><b class="s1"></b>中概互联 ' + w(v1) + '%</span>' +
        '<span><b class="s2"></b>红利低波 ' + w(v2) + '%</span>' +
        '<span><b class="s3"></b>红利网格 ' + w(v3) + '%</span>' +
      '</div>' +
      part('① 中概互联 ' + d.config.p1_code.replace('sh',''), v1,
           '持仓 ' + money(st.p1.units*px.p1_px) + ' ／ 待投现金 ' + money(st.p1.cash) +
           (st.p1.exited ? ' ／ 已止盈退出' : (st.p1.armed ? ' ／ 止盈保护中' : ''))) +
      part('② 红利低波 ' + d.config.p2_code.replace('sh',''), v2, '满仓持有，不操作') +
      part('③ 红利网格 ' + d.config.p3_code.replace('sh',''), v3,
           st.p3.tier + '/3 仓 ／ 待投现金 ' + money(st.p3.cash)) +
      '</div>');
  }

  /* ── 近期事件 ── */
  if(d.recent_events && d.recent_events.length){
    var e = d.recent_events.slice().reverse().slice(0,8).map(function(x){
      // 动作名已自带标的（如「中概定投买入」），不再重复前缀份名
      return '<div class="ev"><b>' + esc(x.date) + '</b> · ' +
        esc(x.action) + '<br>' + esc(x.detail) + '</div>';
    }).join('');
    h.push('<div class="card"><h2>近期事件</h2>' + e + '</div>');
  }

  h.push('<div class="foot">数据截至 ' + esc(d.data_updated) +
    '<br>更新于 ' + esc(d.generated_at) +
    '<br>确认状态仅作提示，不影响系统运行</div>');

  app.innerHTML = h.join('');

  Array.prototype.forEach.call(app.querySelectorAll('[data-ack]'), function(btn){
    btn.onclick = function(){
      var p = (d.pending || [])[+btn.getAttribute('data-ack')];
      var k = ackKey(d, p); if(!k) return;
      lsSet(k, new Date().toISOString());
      render(d);
    };
  });
  Array.prototype.forEach.call(app.querySelectorAll('[data-note]'), function(btn){
    btn.onclick = function(){
      var nt = (d.notices || [])[+btn.getAttribute('data-note')];
      if (!nt) return;
      lsSet(nt.key, new Date().toISOString());
      render(d);
    };
  });
  Array.prototype.forEach.call(app.querySelectorAll('[data-sync]'), function(btn){
    btn.onclick = function(){
      var p = (d.pending || [])[+btn.getAttribute('data-sync')];
      syncToRepo(d, p, btn);
    };
  });
}

function kv(k, v, c){
  return '<div class="kv"><span class="k">' + k + '</span>' +
    '<span class="v ' + (c||'') + '">' + v + '</span></div>';
}
function part(name, v, meta){
  return '<div class="part"><div class="top"><span class="nm">' + name +
    '</span><span class="amt">' + money(v) + '</span></div>' +
    '<div class="meta">' + meta + '</div></div>';
}

function syncToRepo(d, p, btn){
  var k = ackKey(d, p);
  if(!k || !GH_TOKEN || !GH_OWNER){ btn.textContent = '未配置同步'; return; }
  btn.disabled = true; btn.textContent = '同步中…';
  fetch('https://api.github.com/repos/' + GH_OWNER + '/' + GH_REPO +
        '/actions/workflows/confirm.yml/dispatches', {
    method: 'POST',
    headers: { 'Authorization': 'Bearer ' + GH_TOKEN,
               'Accept': 'application/vnd.github+json',
               'X-GitHub-Api-Version': '2022-11-28',
               'Content-Type': 'application/json' },
    body: JSON.stringify({ ref: 'main', inputs: { ack_key: k } })
  }).then(function(r){
    btn.textContent = r.ok ? '已同步（约1分钟后生效）' : ('同步失败 ' + r.status);
  }).catch(function(){ btn.textContent = '同步失败：网络错误'; });
}

fetch('data.json?t=' + Date.now())
  .then(function(r){ if(!r.ok) throw new Error(r.status); return r.json(); })
  .then(render)
  .catch(function(e){
    document.getElementById('app').innerHTML =
      '<div class="warnbar">读取 data.json 失败：' + esc(e.message) +
      '<br>若刚部署，请等 GitHub Actions 首次运行完成。</div>';
  });
