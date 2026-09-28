"""
VCP × SMC Confluence 橋接器
不改動、不依賴 scannerrailway.py / smc_scanner.py 的原始碼——只讀取這兩個已經部署好的
Railway服務目前對外的HTML頁面（一般使用者打開瀏覽器會看到的畫面），解析出：
  - VCP scanner：哪些幣種在哪個時框出現「已就緒」(🎯) 的C訊號，連同該時框對應的日線Stage/收斂%/進場區間/相對強度分數
  - SMC scanner：同一個幣種的4H HTF方向、Killzone時段狀態、Equal High/Low提示
然後合併成一張表，每一列直接給一個「帶入Confluence →」連結，把能自動判斷的欄位都透過網址參數
帶進 VCP Confluence 儀表（checklist）。按連結時會經過 /enrich 自動補算C高低點價位、ATR14、量縮比、
最後3根收盤位置、上方空間、進場點等高點（只讀取VCP scanner的/debug_c與Bybit公開行情），使用者只需手動核對止損側流動性。

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
from flask import Flask, Response, request
from urllib.parse import urlencode

app = Flask(__name__)

# ============================================================
# 設定（可透過Railway環境變數覆蓋，預設值取自MH目前的兩個scanner）
# ============================================================
VCP_URL        = os.environ.get('VCP_URL', 'https://web-production-f3d46.up.railway.app')
SMC_URL        = os.environ.get('SMC_URL', 'https://smc-scanner-production.up.railway.app')
# 預設改成同一個服務裡的 /checklist（相對路徑）——claude.ai上的Artifact頁面因為安全沙盒機制，
# 收不到網址上的?symbol=...等參數，帶入功能一定會失效；改成自己Railway上的頁面就沒有這個限制。
CONFLUENCE_URL = os.environ.get('CONFLUENCE_URL', '/checklist')
POLL_SECONDS   = int(os.environ.get('POLL_SECONDS', 5 * 60))
REQUEST_TIMEOUT = 25
CHECKLIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'checklist.html')
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
# 掛單前自動補算（按「帶入Confluence →」時才計算，不會在背景一直打API）
# 資料來源：
#   - C的高低點：VCP scanner自己的 /debug_c 診斷頁（只讀取，不改動原掃描器）
#   - K線/成交量：Bybit公開行情API（不需要API key）
# 算出來的東西：C高低點價位、ATR14、量縮比、最後3根收盤位置、上方空間、進場點等高點
# 所有門檻都可以用Railway環境變數覆蓋。
# ============================================================
# Bybit的主網域對部分地區/機房IP會回403，依序嘗試這幾個官方網域（api.bytick.com是Bybit官方備用網域）
BYBIT_HOSTS        = [h.strip() for h in os.environ.get('BYBIT_API', 'https://api.bybit.com,https://api.bytick.com').split(',') if h.strip()]
BYBIT_HEADERS      = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'}
VOL_DRY_MAX        = float(os.environ.get('VOL_DRY_MAX', 0.7))        # 最後一C均量 ÷ C之前MA50均量 < 此值＝量縮
CLOSE_POS_MIN      = float(os.environ.get('CLOSE_POS_MIN', 0.6))      # 最後3根平均收盤位置（在C區間內）≥ 此值
CLOSE_BARS         = int(os.environ.get('CLOSE_BARS', 3))             # 看最後幾根已收K線
WICK_RATIO         = float(os.environ.get('WICK_RATIO', 0.5))         # 逆向影線 ≥ K線全長此比例＝長影線
MAX_LONG_WICKS     = int(os.environ.get('MAX_LONG_WICKS', 1))         # 最後幾根裡最多容許幾根長影線
HEADROOM_MIN_PCT   = float(os.environ.get('HEADROOM_MIN_PCT', 4.0))   # 進場價到最近日線前高的距離 ≥ 此%
HEADROOM_PIVOT_LEN = int(os.environ.get('HEADROOM_PIVOT_LEN', 5))     # 日線擺動高/低點左右各幾根
EQ_TOL_PCT         = float(os.environ.get('EQ_TOL_PCT', 0.3))         # 其他擺動高點跟C高點差距在±此%內＝等高點
EQ_LOOKBACK_BARS   = int(os.environ.get('EQ_LOOKBACK_BARS', 150))     # 在訊號時框往前找幾根
EQ_PIVOT_LEN       = int(os.environ.get('EQ_PIVOT_LEN', 3))
DEBUG_C_TIMEOUT    = int(os.environ.get('DEBUG_C_TIMEOUT', 60))

BYBIT_INTERVAL = {'5M': '5', '15M': '15', '30M': '30', '1H': '60', '4H': '240', '1D': 'D'}

def fetch_bybit_kline(symbol, tf_label, limit=1000):
    """回傳由舊到新的list[dict]：t(ms)/o/h/l/c/v；最後一根是還沒收完的K線"""
    params = {'category': 'linear', 'symbol': symbol, 'interval': BYBIT_INTERVAL[tf_label], 'limit': limit}
    errors = []
    data = None
    for host in BYBIT_HOSTS:
        try:
            r = requests.get(f'{host}/v5/market/kline', params=params, headers=BYBIT_HEADERS, timeout=REQUEST_TIMEOUT)
            if r.status_code != 200:
                errors.append(f'{host.split("//")[-1]} HTTP {r.status_code}')
                continue
            data = r.json()
            if data.get('retCode') != 0:
                errors.append(f'{host.split("//")[-1]} {data.get("retMsg")}')
                data = None
                continue
            break
        except requests.RequestException as e:
            errors.append(f'{host.split("//")[-1]} {type(e).__name__}')
    if data is None:
        raise RuntimeError('Bybit行情讀取失敗：' + '、'.join(errors))
    rows = data['result']['list'][::-1]   # Bybit是新→舊，反轉成舊→新
    return [{'t': int(x[0]), 'o': float(x[1]), 'h': float(x[2]), 'l': float(x[3]),
             'c': float(x[4]), 'v': float(x[5])} for x in rows]

def fetch_last_c(symbol, tf_label, direction):
    """從VCP scanner的/debug_c讀出最後一個C的高低點與開始時間(ms)。方向對不上或讀不到就回傳None"""
    r = requests.get(f'{VCP_URL}/debug_c', params={'symbol': symbol, 'tf': tf_label.lower()},
                     timeout=DEBUG_C_TIMEOUT)
    r.raise_for_status()
    d = r.json()
    want = 'bull' if direction == 'long' else 'bear'
    if d.get('direction') != want or not d.get('c_list'):
        return None
    last = d['c_list'][-1]
    def to_ms(tinfo):
        if not tinfo:
            return None
        dt = datetime.strptime(tinfo['utc'][:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    h_ms, l_ms = to_ms(last.get('h_time')), to_ms(last.get('l_time'))
    # 多頭C＝先高後低，C從高點開始；空頭C＝先低後高，C從低點開始
    start_ms = h_ms if direction == 'long' else l_ms
    return {'label': last.get('label'), 'hv': float(last['hv']), 'lv': float(last['lv']), 'start_ms': start_ms}

def calc_atr14(bars):
    if len(bars) < 15:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]['h'], bars[i]['l'], bars[i - 1]['c']
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[:14]) / 14
    for tr in trs[14:]:
        atr = (atr * 13 + tr) / 14          # Wilder平滑，跟TradingView的ATR一致
    return atr

def pivot_idx(bars, key, n, is_high):
    out = []
    for i in range(n, len(bars) - n):
        v = bars[i][key]
        window = [bars[j][key] for j in range(i - n, i + n + 1) if j != i]
        if (is_high and all(v >= w for w in window)) or (not is_high and all(v <= w for w in window)):
            out.append(i)
    return out

def enrich_signal(symbol, tf_label, direction):
    """回傳 (params dict, 說明文字list)。任何一項算不出來就略過該項，不影響其他項。"""
    notes, p = [], {}
    is_long = direction == 'long'

    # C高低點只需要VCP掃描器，就算Bybit行情讀不到也先帶入
    try:
        c = fetch_last_c(symbol, tf_label, direction)
    except Exception as e:
        c = None
        notes.append(f'讀取C高低點失敗（{type(e).__name__}）')
    if c:
        p['cHigh'] = f"{c['hv']:.8g}"
        p['cLow'] = f"{c['lv']:.8g}"
        p['cLabel'] = c.get('label') or ''

    try:
        bars_all = fetch_bybit_kline(symbol, tf_label)
    except Exception as e:
        notes.append(f'{e}——ATR/量縮/收盤位置/上方空間/等高點需手動判斷')
        return p, notes
    closed = bars_all[:-1]                       # 最後一根還沒收完，不列入計算
    atr = calc_atr14(closed)
    if atr:
        p['atr14'] = f'{atr:.8g}'

    if not c:
        notes.append('VCP掃描器目前沒有這個方向的C資料，C高低點/量縮/收盤位置/上方空間/等高點需要手動判斷')
        return p, notes

    c_high, c_low = c['hv'], c['lv']
    rng = c_high - c_low
    entry = c_high * 1.005 if is_long else c_low * 0.995

    # ① 量縮比：最後一C期間的均量 ÷ C開始前50根的均量
    if c['start_ms'] is not None:
        start_i = next((i for i, b in enumerate(closed) if b['t'] >= c['start_ms']), None)
        if start_i is not None and start_i >= 50 and start_i < len(closed):
            c_vol = sum(b['v'] for b in closed[start_i:]) / (len(closed) - start_i)
            base = sum(b['v'] for b in closed[start_i - 50:start_i]) / 50
            if base > 0:
                p['volDry'] = f'{c_vol / base:.2f}'

    # ② 最後N根收盤位置（在C區間內，1＝貼著有利的一端）＋長影線根數
    if rng > 0 and len(closed) >= CLOSE_BARS:
        last = closed[-CLOSE_BARS:]
        pos, wicks = [], 0
        for b in last:
            x = (b['c'] - c_low) / rng if is_long else (c_high - b['c']) / rng
            pos.append(max(0.0, min(1.0, x)))
            full = b['h'] - b['l']
            wick = (b['h'] - max(b['o'], b['c'])) if is_long else (min(b['o'], b['c']) - b['l'])
            if full > 0 and wick / full >= WICK_RATIO:
                wicks += 1
        p['closePos'] = f'{sum(pos) / len(pos):.2f}'
        p['wicks'] = wicks

    # ③ 上方空間：進場價到最近一個日線擺動高點（做空＝下方擺動低點）的距離
    try:
        daily = fetch_bybit_kline(symbol, '1D', limit=500)[:-1]
        n = HEADROOM_PIVOT_LEN
        if is_long:
            lv = [daily[i]['h'] for i in pivot_idx(daily, 'h', n, True) if daily[i]['h'] > entry]
            # 最近幾根還沒辦法確認成擺動點，但它們的高點一樣是壓力
            lv += [b['h'] for b in daily[-n:] if b['h'] > entry]
            room = ((min(lv) - entry) / entry * 100) if lv else None
        else:
            lv = [daily[i]['l'] for i in pivot_idx(daily, 'l', n, False) if daily[i]['l'] < entry]
            lv += [b['l'] for b in daily[-n:] if b['l'] < entry]
            room = ((entry - max(lv)) / entry * 100) if lv else None
        p['headroom'] = 'none' if room is None else f'{room:.1f}'
    except Exception as e:
        notes.append(f'日線資料讀取失敗，上方空間需手動判斷（{type(e).__name__}）')

    # ④ 進場點等高點：訊號時框往前找，有沒有其他擺動高點（做空＝低點）跟C高點差距在±EQ_TOL_PCT%內
    look = closed[-EQ_LOOKBACK_BARS:]
    offset = len(closed) - len(look)
    start_i = next((i for i, b in enumerate(closed) if c['start_ms'] and b['t'] >= c['start_ms']), len(closed))
    ref = c_high if is_long else c_low
    eq_count, last_hit = 0, -10**9
    for i in pivot_idx(look, 'h' if is_long else 'l', EQ_PIVOT_LEN, is_long):
        gi = i + offset
        if gi >= start_i - EQ_PIVOT_LEN:      # 排除C自己那個高/低點
            continue
        v = look[i]['h'] if is_long else look[i]['l']
        if abs(v - ref) / ref * 100 <= EQ_TOL_PCT:
            if i - last_hit > EQ_PIVOT_LEN:   # 平頂連續幾根只算一次
                eq_count += 1
            last_hit = i
    p['eqEntry'] = eq_count
    # 門檻也一起帶過去，checklist用同一組數字判斷（改Railway環境變數兩邊會一致）
    p['vdMax'] = VOL_DRY_MAX
    p['cpMin'] = CLOSE_POS_MIN
    p['maxWicks'] = MAX_LONG_WICKS
    p['hrMin'] = HEADROOM_MIN_PCT
    return p, notes

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

        # 先經過 /enrich 補算掛單前的數據，再自動轉到 checklist
        conf_url = f'/enrich?{urlencode({**params, "tf": sig["tf"]})}'
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
  這個頁面只讀取VCP scanner與SMC scanner目前對外顯示的頁面並解析成連結；按下「帶入Confluence →」時，才會另外讀取該訊號的C高低點（VCP scanner的/debug_c）
  與Bybit公開行情，補算ATR14、量縮比、收盤位置、上方空間、等高點，再自動帶入checklist（需要幾秒鐘）。
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

@app.route('/enrich')
def enrich():
    """按「帶入Confluence →」時進來：補算C高低點/ATR/量縮/收盤位置/上方空間/等高點，
    再帶著全部參數轉到checklist。補算失敗也照樣轉過去，只是少帶那幾項。"""
    params = {k: v for k, v in request.args.items()}
    tf = params.pop('tf', '')
    symbol = params.get('symbol', '')
    direction = params.get('direction', 'long')
    extra, notes = {}, []
    if symbol and tf in BYBIT_INTERVAL:
        try:
            extra, notes = enrich_signal(symbol, tf, direction)
        except Exception as e:
            notes = [f'自動補算失敗（{type(e).__name__}: {e}）']
    params.update(extra)
    if tf:
        params['tf'] = tf
    if notes:
        params['enrichNote'] = '；'.join(notes)[:400]
    return Response('', status=302, headers={'Location': f'{CONFLUENCE_URL}?{urlencode(params)}'})

@app.route('/debug_enrich')
def debug_enrich():
    """診斷用：/debug_enrich?symbol=BTCUSDT&tf=1H&direction=long → 直接看補算結果（JSON）"""
    symbol = request.args.get('symbol', 'BTCUSDT').upper()
    tf = request.args.get('tf', '1H').upper()
    direction = request.args.get('direction', 'long')
    try:
        extra, notes = enrich_signal(symbol, tf, direction)
        out = {'symbol': symbol, 'tf': tf, 'direction': direction, 'params': extra, 'notes': notes}
    except Exception as e:
        out = {'error': f'{type(e).__name__}: {e}'}
    return Response(json.dumps(out, ensure_ascii=False, indent=2), mimetype='application/json')

@app.route('/checklist')
def checklist():
    """VCP Confluence 儀表——原本是claude.ai的Artifact，因為Artifact的沙盒機制讀不到網址參數，
    改成由這個服務直接提供同一份頁面，這樣「帶入Confluence →」的自動帶入才會真的生效。"""
    try:
        with open(CHECKLIST_FILE, 'r', encoding='utf-8') as f:
            body = f.read()
    except FileNotFoundError:
        return Response('checklist.html 沒有找到，請確認它跟 confluence_scanner.py 放在同一個資料夾一起上傳。', status=500, mimetype='text/plain')
    # checklist.html 本身沒有 <!DOCTYPE>/<html>/<head> 包裝（原本是給claude.ai Artifact用，那邊會自動包），
    # 這裡自己補上，順便明確宣告UTF-8，避免中文字顯示亂碼。
    html = (
        '<!doctype html><html lang="zh-TW"><head>'
        '<meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '</head><body>' + body + '</body></html>'
    )
    return Response(html, mimetype='text/html')

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
