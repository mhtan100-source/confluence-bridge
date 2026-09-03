"""
VCP × SMC Confluence 橋接器
不改動、不依賴 scannerrailway.py / smc_scanner.py 的原始碼——只讀取這兩個已經部署好的
Railway服務目前對外的HTML頁面（一般使用者打開瀏覽器會看到的畫面），解析出：
  - VCP scanner：哪些幣種在哪個時框出現「已就緒」(🎯) 的C訊號，連同該時框對應的日線Stage/收斂%/進場區間/相對強度分數
  - SMC scanner：同一個幣種的4H HTF方向、Killzone時段狀態、Equal High/Low提示
然後合併成一張表，每一列直接給一個「帶入Confluence →」連結，把能自動判斷的欄位都透過網址參數
帶進 VCP Confluence 儀表（checklist），使用者只需要手動補C的實際高低點價位、量縮、突破量能倍數、ATR。

部署方式：獨立成一個新的Railway服務（新的repo或新的service，都可以），跟原本兩個scanner完全分開，
不會動到、也不依賴它們的原始碼——只要它們兩個原本的網址還能打開，這個橋接器就能運作。
"""

import os
import re
import time
import threading
import json
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response
from urllib.parse import urlencode

app = Flask(__name__)

# ============================================================
# 設定（可透過Railway環境變數覆蓋，預設值取自MH目前的兩個scanner）
# ============================================================
VCP_URL        = os.environ.get('VCP_URL', 'https://web-production-f3d46.up.railway.app')
SMC_URL        = os.environ.get('SMC_URL', 'https://smc-production.up.railway.app')
CONFLUENCE_URL = os.environ.get('CONFLUENCE_URL', 'https://claude.ai/code/artifact/232449ef-6415-4cb4-bfa2-0b282d2c2e77')
POLL_SECONDS   = int(os.environ.get('POLL_SECONDS', 5 * 60))
REQUEST_TIMEOUT = 25
TF_LABELS = ['5M', '15M', '30M', '1H', '4H', '1D']
STAGE_MAP = {'1': 'S1', '2': 'S2', '3': 'S3', '4': 'S4'}

CELL_RE = re.compile(
    r'(?P<cnt>\d+)C'
    r'(?P<bear>\(空\))?'
    r'\((?P<pct>[\d.]+)%\)'
    r'(?P<ready>🎯)?'
    r'(?:\s*B(?P<base>\d+))?'
    r'(?:\s*(?P<zone>Pivot|Cheat|LCheat))?'
)

state = {
    'status': 'idle',
    'last_html': '',
    'last_update': None,
    'last_error': None,
    'vcp_ok': False,
    'smc_ok': False,
    'lock': threading.Lock(),
}

# ============================================================
# 解析 VCP scanner（scannerrailway.py）目前對外的頁面
# ============================================================
def fetch_vcp_signals():
    """回傳 list[dict]：每個「已就緒(🎯)」的(幣種, 時框)組合各一筆"""
    resp = requests.get(f'{VCP_URL}/result', timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')

    signals = []
    for tr in soup.select('tr[data-stage]'):
        tds = tr.find_all('td')
        if len(tds) < 4 + len(TF_LABELS):
            continue
        sym_a = tds[0].find('a')
        if not sym_a:
            continue
        symbol = sym_a.get_text(strip=True)
        stage_val = tr.get('data-stage', '0')
        stage_label = STAGE_MAP.get(stage_val, '')

        rs_text = tds[3].get_text(strip=True)
        rs_score = None
        if rs_text not in ('--', ''):
            try:
                rs_score = int(rs_text.replace('+', ''))
            except ValueError:
                rs_score = None

        for i, tf in enumerate(TF_LABELS):
            td = tds[4 + i]
            a = td.find('a')
            text = (a.get_text(strip=True) if a else td.get_text(strip=True))
            m = CELL_RE.search(text)
            if not m or not m.group('ready'):
                continue
            signals.append({
                'symbol': symbol,
                'tf': tf,
                'direction': 'short' if m.group('bear') else 'long',
                'stage': stage_label,
                'c_count': m.group('cnt'),
                'last_pct': m.group('pct'),
                'zone': (m.group('zone') or '').lower(),
                'rs_score': rs_score,
            })
    return signals

# ============================================================
# 解析 SMC scanner（smc_scanner.py）目前對外的頁面
# ============================================================
def fetch_smc_context():
    """回傳 (in_killzone: bool, symbol -> {trend, eq_high, eq_low})"""
    resp = requests.get(SMC_URL, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')

    in_kz = soup.select_one('.kz-on') is not None

    ctx = {}
    for a in soup.select('a.sym'):
        tr = a.find_parent('tr')
        if not tr:
            continue
        tds = tr.find_all('td')
        if len(tds) < 7:
            continue
        symbol = a.get_text(strip=True)
        trend_text = tds[1].get_text(strip=True)
        badges_text = tds[6].get_text(' ', strip=True)
        trend = 'bullish' if '多頭' in trend_text else 'bearish' if '空頭' in trend_text else None
        ctx[symbol] = {
            'trend': trend,
            'eq_high': 'EQ High' in badges_text,
            'eq_low': 'EQ Low' in badges_text,
        }
    return in_kz, ctx

# ============================================================
# 合併 + 產生帶入連結
# ============================================================
def build_rows():
    vcp_signals = fetch_vcp_signals()
    in_kz, smc_ctx = fetch_smc_context()

    rows = []
    for sig in vcp_signals:
        smc = smc_ctx.get(sig['symbol'])
        direction = sig['direction']
        if smc:
            trend = smc['trend']
            smc_htf_ok = 1 if (direction == 'long' and trend != 'bearish') or (direction == 'short' and trend != 'bullish') else 0
            eq_flag = 1 if (direction == 'long' and smc['eq_high']) or (direction == 'short' and smc['eq_low']) else 0
            smc_found = True
        else:
            smc_htf_ok, eq_flag, smc_found = 0, 0, False

        params = {
            'symbol': sig['symbol'] + 'USDT',
            'direction': direction,
        }
        if sig['stage']:
            params['stage'] = sig['stage']
        if sig['c_count']:
            params['cCount'] = sig['c_count']
        if sig['last_pct']:
            params['lastCPct'] = sig['last_pct']
        if sig['zone']:
            params['zone'] = sig['zone']
        if sig['rs_score'] is not None:
            params['rsScore'] = sig['rs_score']
        params['smcHtf'] = 1 if smc_found and smc_htf_ok else 0
        params['smcKz'] = 1 if in_kz else 0
        params['eqFlag'] = eq_flag

        conf_url = f'{CONFLUENCE_URL}?{urlencode(params)}'
        rows.append({**sig, 'smc_found': smc_found, 'in_kz': in_kz, 'eq_flag': eq_flag, 'conf_url': conf_url})

    rows.sort(key=lambda r: (r['symbol'], TF_LABELS.index(r['tf'])))
    return rows, in_kz

# ============================================================
# HTML
# ============================================================
def generate_html(rows, in_kz, err=None, vcp_ok=True, smc_ok=True):
    kz_html = '🟢 Killzone 進行中' if in_kz else '🔴 非Killzone時段'
    err_html = f'<div class="err">⚠️ {err}</div>' if err else ''
    src_html = (
        f'<span class="{"src-ok" if vcp_ok else "src-bad"}">VCP scanner {"✓" if vcp_ok else "✗"}</span>'
        f'<span class="{"src-ok" if smc_ok else "src-bad"}">SMC scanner {"✓" if smc_ok else "✗"}</span>'
    )

    body_rows = ''
    if not rows:
        body_rows = '<tr><td colspan="9" class="no-data">目前沒有已就緒(🎯)的訊號</td></tr>'
    for r in rows:
        dir_html = '<span class="long">▲ LONG</span>' if r['direction'] == 'long' else '<span class="short">▼ SHORT</span>'
        smc_html = '✅ 有SMC資料' if r['smc_found'] else '<span class="dim">SMC無此幣資料</span>'
        eq_html = '<span class="warn">⚠ EQ</span>' if r['eq_flag'] else ''
        body_rows += f'''<tr>
          <td class="sym">{r['symbol']}</td>
          <td>{r['tf']}</td>
          <td>{dir_html}</td>
          <td>{r['stage'] or '—'}</td>
          <td>{r['c_count']}C ({r['last_pct']}%)</td>
          <td>{(r['zone'] or '—').capitalize()}</td>
          <td>{r['rs_score'] if r['rs_score'] is not None else '—'}</td>
          <td>{smc_html} {eq_html}</td>
          <td><a class="conf-btn" href="{r['conf_url']}" target="_blank">帶入Confluence →</a></td>
        </tr>'''

    now = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S') + ' (MYT)'

    return f'''<!DOCTYPE html>
<html lang="zh-TW"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VCP × SMC Confluence 橋接器</title>
<style>
  body {{ font-family: Arial, sans-serif; background:#0d1117; color:#e6edf3; margin:0; padding:10px; }}
  h1 {{ color:#bc8cff; font-size:1.35em; margin:0 0 6px; }}
  .header {{ background:#161b22; padding:12px; border-radius:8px; margin-bottom:12px; border:1px solid #30363d; }}
  .info {{ display:flex; gap:14px; flex-wrap:wrap; font-size:0.85em; color:#8b949e; margin-top:6px; }}
  .src-ok {{ color:#3fb950; margin-right:10px; }}
  .src-bad {{ color:#f85149; margin-right:10px; }}
  .err {{ background:#3d1f1f; color:#f85149; padding:8px 12px; border-radius:6px; margin-top:8px; font-size:0.85em; }}
  table {{ width:100%; border-collapse:collapse; font-size:0.85em; background:#161b22; border-radius:8px; overflow:hidden; }}
  th {{ background:#21262d; color:#8b949e; padding:8px 10px; text-align:left; font-weight:normal; }}
  td {{ padding:8px 10px; border-bottom:1px solid #21262d; }}
  tr:hover td {{ background:#1c2128; }}
  .sym {{ font-weight:bold; color:#58a6ff; }}
  .long {{ color:#3fb950; font-weight:bold; }}
  .short {{ color:#f85149; font-weight:bold; }}
  .warn {{ color:#d29922; }}
  .dim {{ color:#5c6577; }}
  .no-data {{ text-align:center; color:#8b949e; padding:20px; font-style:italic; }}
  .conf-btn {{ display:inline-block; background:#4d2d66; color:#bc8cff; padding:5px 12px; border-radius:6px; text-decoration:none; font-size:0.92em; white-space:nowrap; }}
  .conf-btn:hover {{ background:#663d85; }}
  .refresh-btn {{ background:#5a3d7a; color:#fff; border:none; padding:8px 16px; border-radius:6px; cursor:pointer; font-size:0.9em; margin-top:10px; }}
  .refresh-btn:hover {{ background:#6f4a97; }}
</style></head>
<body>
<div class="header">
  <h1>🧭 VCP × SMC Confluence 橋接器</h1>
  <div class="info">
    <span>更新：{now}</span>
    <span>{kz_html}</span>
    <span>就緒訊號：{len(rows)} 筆</span>
  </div>
  <div class="info">{src_html}</div>
  {err_html}
  <button class="refresh-btn" onclick="fetch('/refresh').then(()=>setTimeout(()=>location.reload(),3000))">🔄 重新讀取兩個scanner</button>
</div>
<div style="overflow-x:auto;">
<table>
  <tr>
    <th>幣種</th><th>時框</th><th>方向</th><th>日線Stage</th><th>C訊號</th><th>進場區間</th><th>力量分數</th><th>SMC狀態</th><th>確認</th>
  </tr>
  {body_rows}
</table>
</div>
<p style="color:#5c6577;font-size:0.78em;margin-top:14px;">
  這個頁面本身不進行任何交易判斷、也不呼叫Bybit——只讀取VCP scanner與SMC scanner目前對外顯示的頁面並解析成連結。
  兩個原始scanner的邏輯完全沒有被更動；如果SMC scanner目前沒有把某個幣種列在牠自己的表格裡（分數不夠高），
  這裡就沒有那個幣種的SMC資料，「SMC狀態」會顯示「SMC無此幣資料」，代表HTF/EqualHigh-Low這兩項請自行到SMC scanner確認。
</p>
</body></html>'''

# ============================================================
# 背景輪詢
# ============================================================
def run_bridge_scan():
    with state['lock']:
        if state['status'] == 'scanning':
            return
        state['status'] = 'scanning'
    vcp_ok, smc_ok, err = True, True, None
    rows, in_kz = [], False
    try:
        rows, in_kz = build_rows()
    except requests.RequestException as e:
        err = f'讀取來源scanner失敗：{e}'
        vcp_ok = smc_ok = False
    except Exception as e:
        err = f'解析失敗：{e}'
    html = generate_html(rows, in_kz, err=err, vcp_ok=vcp_ok, smc_ok=smc_ok)
    with state['lock']:
        state['last_html'] = html
        state['last_update'] = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
        state['last_error'] = err
        state['vcp_ok'], state['smc_ok'] = vcp_ok, smc_ok
        state['status'] = 'idle'

def auto_loop():
    time.sleep(5)
    while True:
        run_bridge_scan()
        time.sleep(POLL_SECONDS)

# ============================================================
# Flask 路由
# ============================================================
@app.route('/')
def index():
    with state['lock']:
        html = state['last_html']
        status = state['status']
    if not html:
        return Response('<html><body style="background:#0d1117;color:#eee;font-family:Arial;text-align:center;padding-top:100px;"><h2>⏳ 正在讀取VCP/SMC scanner...</h2><meta http-equiv="refresh" content="5"></body></html>', mimetype='text/html')
    if status == 'scanning':
        return Response(html.replace('</head>', '<meta http-equiv="refresh" content="8"></head>'), mimetype='text/html')
    return Response(html, mimetype='text/html')

@app.route('/refresh')
def refresh():
    threading.Thread(target=run_bridge_scan, daemon=True).start()
    return Response('ok', mimetype='text/plain')

@app.route('/status')
def status():
    with state['lock']:
        s = dict(state)
    s.pop('lock', None)
    s.pop('last_html', None)
    return Response(json.dumps(s, ensure_ascii=False), mimetype='application/json')

if __name__ == '__main__':
    threading.Thread(target=auto_loop, daemon=True).start()
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
