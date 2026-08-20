/* 看板渲染的真实 DOM 测试。
 *
 * 原来验收里对网页只做源码字符串匹配（"nearbar" in appjs），
 * 那证明不了渲染是对的——改个变量名就失效，页面坏了也照样通过。
 * 这里用 jsdom 把 app.js 真跑起来，喂各种 data.json，断言渲染结果。
 */
const fs = require('fs');
const path = require('path');
const ROOT = path.join(__dirname, '..');

let JSDOM;
try { ({ JSDOM } = require('jsdom')); }
catch (e) { console.log('SKIP jsdom 未安装'); process.exit(2); }

const APP = fs.readFileSync(path.join(ROOT, 'docs', 'app.js'), 'utf8');
let pass = 0, fail = 0;
function check(name, ok, detail) {
  (ok ? pass++ : fail++);
  console.log(`  ${ok ? 'PASS' : 'FAIL'} ${name}${detail ? '  ' + detail : ''}`);
}

function render(data) {
  const dom = new JSDOM('<div id="app"></div>', { runScripts: 'outside-only' });
  const w = dom.window;
  const store = {};
  Object.defineProperty(w, 'localStorage', {
    value: {
      getItem: k => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
      removeItem: k => { delete store[k]; },
    }, configurable: true
  });
  w.fetch = () => new Promise(() => {});          // 阻断自动加载
  w.eval(APP + '\n;window.__render = render;');
  w.__render(data);
  return w.document;
}

const BASE = {
  data_updated: '2026-08-19',
  generated_at: new Date().toISOString().slice(0, 19).replace('T', ' ') + '+08:00',
  stale_days: 0, fetch_ok: true, acknowledged: {},
  config: { total_capital: 1850000, p1_code: 'sh513050', p2_code: 'sh512890',
            p3_code: 'sh515180', sig_code: '000922', exit_ratio: 1.03,
            buy_tiers: [0.96, 0.93, 0.90] },
  state: { p1: { units: 134700, cash: 150000, cost: 149786, tier: 0, armed: false, exited: false },
           p2: { units: 670400, cash: 0, cost: 774982 },
           p3: { units: 0, cash: 775000, cost: 0, tier: 0 },
           switched: false, switch_date: null },
  metrics: { equity: 1863142, total_return: 0.0071, cagr: null, max_drawdown: 0 },
  last_prices: { p1_px: 1.114, p2_px: 1.161, p3_px: 1.427, sig_px: 5534.83, pb_pct: 0.7348 },
  pending: [], triggers: [], recent_events: [], notices: [], health: [],
  ledger_warns: [], ma_warns: [], curve: []
};
const D = (o) => Object.assign({}, BASE, o);
const txt = (doc) => doc.getElementById('app').textContent;

console.log('看板 DOM 渲染测试');

// 1 · 无事时
let doc = render(D({}));
check('无待办时显示「不操作继续持有」', /不操作/.test(txt(doc)));
check('无待办时不出现确认按钮', doc.querySelectorAll('button.ack').length === 0);

// 2 · 单条指令
doc = render(D({ pending: [{ action: 'P1_DCA', exec_date: '2026-08-26', part: 1,
  label: '中概互联 · 定投买入', detail: '第 1/26 次定投，买入 中概互联ETF(513050) 约 5769 元',
  precheck: '下单前必看折溢价率：>3% 今日暂缓' }] }));
check('指令横幅用 label 作大标题', /中概互联 · 定投买入/.test(txt(doc)));
check('副标题带金额', /5769 元/.test(txt(doc)));
check('渲染「买卖前必查」折溢价提示', /折溢价率/.test(txt(doc)),
      doc.querySelector('.pre') ? doc.querySelector('.pre').textContent.slice(0, 22) : '缺失');
check('委托时段与执行文档一致（9:15–9:35 / 14:55–15:00）',
      /9:15–9:35/.test(txt(doc)) && /14:55–15:00/.test(txt(doc)));
check('有确认按钮', doc.querySelectorAll('button.ack').length >= 1);

// 3 · 多条指令不互相遮盖
doc = render(D({ pending: [
  { action: 'P1_DCA', exec_date: '2026-08-26', part: 1, label: 'A指令', detail: 'd1' },
  { action: 'P3_TIER', exec_date: '2026-08-26', part: 3, tier: 1, label: 'B指令', detail: 'd2' }] }));
check('两条指令渲染成两条独立横幅', doc.querySelectorAll('.banner.act').length === 2);
check('两条指令各有确认按钮', doc.querySelectorAll('button.ack[data-ack]').length === 2);
check('多待办时有计数条', /今日 2 项待办/.test(txt(doc)));
check('每条带序号徽标', doc.querySelectorAll('.banner .seq').length === 2);

// 4 · 故障置顶且带步骤
doc = render(D({
  health: [{ level: 'warn', title: '黄框', what: 'w', todo: ['a', 'b'] },
           { level: 'critical', title: '红框', what: 'c', todo: ['x', 'y', 'z'] }],
  pending: [{ action: 'P1_DCA', exec_date: '2026-08-26', part: 1, label: '指令', detail: 'd' }] }));
const first = doc.querySelector('#app > *');
check('故障排在指令之前（置顶）', first.classList.contains('prob'), first.className);
check('critical 排在 warn 之前',
      doc.querySelectorAll('.prob')[0].classList.contains('crit'));
check('故障渲染出可执行步骤', doc.querySelectorAll('.prob ol li').length === 5);

// 5 · 客户端自查：generated_at 过旧
doc = render(D({ generated_at: '2020-01-01 21:14:00+08:00' }));
check('页面数据过旧时报「系统已停止运行」', /系统已停止运行/.test(txt(doc)));
check('并给出排查步骤', doc.querySelectorAll('.prob.crit ol li').length >= 3);

// 6 · 临近触发
doc = render(D({ triggers: [
  { label: '红利网格 · 第 1 档买入', cond: '跌破 5365', short: '还需跌 1.2%', dist: 1.2, unit: '%', near: true, progress: 0.7 },
  { label: '切换', cond: 'PB<=20%', short: '当前 73%', dist: 53, unit: 'pp', near: false, progress: 0.2 }] }));
check('临近项渲染独立横幅', doc.querySelectorAll('.nearbar').length === 1);
check('临近横幅写明「无需操作」', /尚未触发，无需操作/.test(txt(doc)));
check('观察点里临近项被标出', doc.querySelectorAll('.trig.near').length === 1);
check('非临近项不误标', doc.querySelectorAll('.trig').length === 2);

// 7 · 通知横幅
doc = render(D({ notices: [{ key: 'note:2026-08-19:x', date: '2026-08-19', part: 1,
  label: '中概互联 · 已开启止盈保护', detail: '从今天起开始盯 MA250', extra: '市值达 40 万' }] }));
check('状态通知渲染为独立样式横幅', doc.querySelectorAll('.banner.note').length === 1);
check('通知有「知道了」按钮', /知道了/.test(txt(doc)));

// 8 · 切换后
doc = render(D({ state: Object.assign({}, BASE.state, { switched: true, switch_date: '2027-01-01' }) }));
check('切换后显示停机', /停止策略A/.test(txt(doc)));

// 9 · 脏数据不崩
try {
  render(D({ pending: [{ action: 'UNKNOWN', exec_date: '2026-01-01', part: 9, detail: null }],
             triggers: null, state: null, metrics: {} }));
  check('缺字段/未知动作不导致渲染崩溃', true);
} catch (e) { check('缺字段/未知动作不导致渲染崩溃', false, String(e).slice(0, 60)); }

// 10 · XSS
doc = render(D({ health: [{ level: 'critical', title: '<img src=x onerror=alert(1)>',
                            what: '<script>bad()</script>', todo: ['<b>x</b>'] }] }));
check('故障文本被转义，不注入标签',
      doc.querySelectorAll('#app img, #app script').length === 0
      && /<img src=x/.test(txt(doc)));

console.log(`\n看板渲染：通过 ${pass} 项，失败 ${fail} 项`);
process.exit(fail ? 1 : 0);
