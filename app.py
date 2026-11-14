# -*- coding: utf-8 -*-
"""
新闻联播 · A股情报看板  (单文件桌面应用后端)
- 双击 exe -> 启动本地 HTTP 服务 -> 自动打开浏览器看板
- 数据目录: exe 所在文件夹 (桌面\新闻联播)
- 数据源目录: C:\\Users\\CFZ\\WorkBuddy\\Claw\\新闻联播 (每日 22:00 定时任务产出)
- 自动采集: 后台线程定时把数据源里新增/更新的 md 同步到数据目录
- 三个 Tab: 文件清单 / 新闻查看 / 跟踪标的
"""
import os
import re
import io
import sys
import json
import time
import shutil
import threading
import webbrowser
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

# ---------------------------------------------------------------- 配置
def app_dir():
    """exe 所在目录（源码运行时为脚本所在目录）"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def resource(rel):
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)

DATA_DIR = os.environ.get('NEWS_DATA_DIR') or app_dir()   # 桌面\新闻联播
SOURCE_DIR = r'C:\Users\CFZ\WorkBuddy\Claw\新闻联播'   # 定时任务产出目录
CONFIG_PATH = os.path.join(DATA_DIR, '看板配置.json')
LOG_PATH = os.path.join(DATA_DIR, '看板运行日志.txt')
PORT_START = 8765
SYNC_INTERVAL = 60      # 秒

DEFAULT_CONFIG = {
    "source_dir": SOURCE_DIR,
    "data_dir": DATA_DIR,
    "sync_interval": SYNC_INTERVAL,
    "autostart": False,
}

_state = {
    "last_sync": "",
    "sync_result": {"added": [], "updated": []},
    "server": None,
    "port": PORT_START,
}
_wd = ['一', '二', '三', '四', '五', '六', '日']


def log(msg):
    line = '[%s] %s' % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg)
    try:
        with io.open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass
    try:
        print(line)
    except Exception:
        pass


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with io.open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    cfg['data_dir'] = DATA_DIR
    return cfg


def save_config(cfg):
    cfg = dict(cfg)
    cfg.pop('data_dir', None)
    try:
        with io.open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            f.write(json.dumps(cfg, ensure_ascii=False, indent=2))
        return True
    except Exception as e:
        log('save_config failed: %s' % e)
        return False


# ---------------------------------------------------------------- 数据解析
DATE_RE = re.compile(r'(\d{4})-(\d{2})-(\d{2})')
CODE_RE = re.compile(r'^\d{6}(?:\.(?:SH|SZ|BJ))?$', re.I)


def clean_cell(s):
    s = s.strip()
    s = re.sub(r'\*\*|`|__', '', s)
    return s.strip()


def parse_md(path, name):
    """解析单个 md: 返回条目数、标的列表、主题"""
    try:
        with io.open(path, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
    except Exception as e:
        log('read fail %s: %s' % (name, e))
        return {'items': 0, 'stocks': [], 'topic': ''}

    lines = text.split('\n')
    stocks = []
    seen = set()
    for ln in lines:
        s = ln.strip()
        if not s.startswith('|'):
            continue
        cells = [clean_cell(c) for c in s.strip('|').split('|')]
        if len(cells) < 3:
            continue
        code = cells[0]
        if not CODE_RE.match(code):
            continue
        sname = cells[1]
        if sname in ('', '—', '-', '代码', '名称') or sname.startswith('—'):
            continue
        code6 = code.split('.')[0]
        if code6 in seen:
            continue
        seen.add(code6)
        sector = cells[2] if len(cells) > 2 else ''
        reason = cells[3] if len(cells) > 3 else ''
        if reason in ('—', '-'):
            reason = ''
        stocks.append({'code': code6, 'name': sname, 'sector': sector, 'reason': reason})

    # 条目数：### 条目N / 数字. 开头的要闻行
    items = len(re.findall(r'^###\s*条目\s*\d+', text, re.M))
    if not items:
        items = len(re.findall(r'^\s*\d+[\.、]\s*\*{0,2}\S', text, re.M))
    if not items:
        items = len(re.findall(r'^###\s+', text, re.M))

    # 主题：优先 "一句话主题"，其次摘要章节第一条要点
    topic = ''
    m = re.search(r'一句话主题[：:]\s*(.+)', text)
    if m:
        topic = m.group(1)
    if not topic:
        m = re.search(r'##\s*[二2][、.．]?\s*[^\n]*内容摘要[^\n]*\n([\s\S]{0,600})', text)
        if m:
            for ln in m.group(1).split('\n'):
                t = re.sub(r'^\s*[-*>\d\.、\s]+', '', ln).strip()
                t = re.sub(r'\*\*|`', '', t)
                if len(t) > 8:
                    topic = t
                    break
    if not topic:
        for ln in lines[:12]:
            if ln.startswith('>'):
                topic = re.sub(r'^\s*>\s*', '', ln).strip()
                topic = re.sub(r'\*\*|`', '', topic)
                break
    topic = re.sub(r'\s+', ' ', topic)
    if len(topic) > 52:
        topic = topic[:52] + '…'
    return {'items': items, 'stocks': stocks, 'topic': topic}


def scan_files():
    """扫描数据目录 md 文件"""
    out = []
    try:
        names = os.listdir(DATA_DIR)
    except Exception as e:
        log('listdir fail: %s' % e)
        return out
    for name in names:
        if not name.lower().endswith('.md'):
            continue
        m = DATE_RE.search(name)
        if not m:
            continue
        d = '%s-%s-%s' % (m.group(1), m.group(2), m.group(3))
        try:
            wd = _wd[date(int(m.group(1)), int(m.group(2)), int(m.group(3))).weekday()]
        except Exception:
            wd = ''
        path = os.path.join(DATA_DIR, name)
        try:
            st = os.stat(path)
            size, mtime = st.st_size, st.st_mtime
        except Exception:
            size, mtime = 0, 0
        info = parse_md(path, name)
        out.append({
            'date': d, 'weekday': wd, 'name': name, 'size': size,
            'mtime': datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M'),
            'items': info['items'], 'stock_count': len(info['stocks']),
            'topic': info['topic'],
        })
    out.sort(key=lambda x: x['date'], reverse=True)
    today = datetime.now().strftime('%Y-%m-%d')
    for i, r in enumerate(out):
        r['is_today'] = (r['date'] == today)
        r['is_latest'] = (i == 0)
    return out


# ---------------------------------------------------------------- 行情与涨跌计算
# 数据源：腾讯 gtimg 前复权日K（按用户约定禁用东财）。结果本地缓存，首次拉取稍慢。
PRICE_CACHE_DIR = os.path.join(app_dir(), '.price_cache')
PRICE_CACHE_FILE = os.path.join(PRICE_CACHE_DIR, 'kline.json')
_price_cache = None
_price_cache_loaded = False


def market_prefix(code):
    if code.startswith('6'):
        return 'sh' + code
    if code.startswith(('0', '3')):
        return 'sz' + code
    if code.startswith(('8', '4')):
        return 'bj' + code
    return 'sh' + code


def load_price_cache():
    global _price_cache, _price_cache_loaded
    if _price_cache_loaded:
        return _price_cache
    try:
        with io.open(PRICE_CACHE_FILE, 'r', encoding='utf-8') as f:
            _price_cache = json.load(f)
    except Exception:
        _price_cache = {}
    _price_cache_loaded = True
    return _price_cache


def save_price_cache():
    try:
        os.makedirs(PRICE_CACHE_DIR, exist_ok=True)
        with io.open(PRICE_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(load_price_cache(), f, ensure_ascii=False)
    except Exception as e:
        log('save_price_cache failed: %s' % e)


def fetch_kline(code):
    """腾讯 gtimg 前复权日K；返回 {日期: 收盘价}"""
    pref = market_prefix(code)
    start = '2026-01-01'
    end = datetime.now().strftime('%Y-%m-%d')
    url = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,%s,%s,400,qfq' % (pref, start, end)
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        node = (data.get('data') or {}).get(pref) or {}
        rows = node.get('qfqday') or node.get('day') or []
        out = {}
        for r in rows:
            if len(r) >= 3 and r[0] >= start:
                try:
                    out[r[0]] = float(r[2])
                except Exception:
                    pass
        return out
    except Exception as e:
        return {}


def get_kline(code):
    cache = load_price_cache()
    if code in cache:
        return cache[code]
    kl = fetch_kline(code)
    if kl:
        cache[code] = kl
        save_price_cache()
    return kl


def fwd_return(klines, from_date, n):
    """from_date 之后第 n 个交易日的累计涨幅（收盘对收盘）"""
    dates = sorted(klines.keys())
    idx = None
    for i, d in enumerate(dates):
        if d >= from_date:
            idx = i
            break
    if idx is None:
        return None
    j = idx + n
    if j >= len(dates):
        return None
    try:
        return klines[dates[j]] / klines[dates[idx]] - 1.0
    except Exception:
        return None


def compute_perf(code, dates, last_date):
    klines = get_kline(code)
    if not klines:
        return None
    last_ret = {}
    for n in (1, 2, 3, 4, 5, 10, 30):
        last_ret[n] = fwd_return(klines, last_date, n)
    avg_ret = {}
    for n in (3, 5, 10):
        vals = []
        for d in dates:
            r = fwd_return(klines, d, n)
            if r is not None:
                vals.append(r)
        avg_ret[n] = (sum(vals) / len(vals)) if vals else None
    return {'last_ret': last_ret, 'avg_ret': avg_ret}


def build_stocks(with_perf=True):
    """汇总所有 md 里出现过的标的，可选附带涨跌幅表现"""
    idx = {}
    for r in scan_files():
        info = parse_md(os.path.join(DATA_DIR, r['name']), r['name'])
        for s in info['stocks']:
            code = s['code']
            if code not in idx:
                idx[code] = {
                    'code': code, 'name': s['name'], 'sector': s['sector'],
                    'count': 0, 'dates': [], 'last_reason': '', 'sectors': {},
                }
            e = idx[code]
            e['count'] += 1
            e['dates'].append(r['date'])
            if not e['name'] and s['name']:
                e['name'] = s['name']
            if s['sector']:
                e['sectors'][s['sector']] = e['sectors'].get(s['sector'], 0) + 1
            if r['date'] >= max(e['dates']) and s['reason']:
                e['last_reason'] = s['reason']
    for e in idx.values():
        e['dates'].sort(reverse=True)
        e['sector'] = max(e['sectors'].items(), key=lambda kv: kv[1])[0] if e['sectors'] else e['sector']
        e['first_date'] = e['dates'][-1]
        e['last_date'] = e['dates'][0]
        e.pop('sectors', None)
    if with_perf:
        cache = load_price_cache()
        miss = [c for c in idx if c not in cache]
        if miss:
            def _f(c):
                kl = fetch_kline(c)
                if kl:
                    cache[c] = kl
            try:
                with ThreadPoolExecutor(max_workers=20) as ex:
                    list(ex.map(_f, miss))
                save_price_cache()
            except Exception as e:
                log('prefetch kline error: %s' % e)
        for code, e in idx.items():
            perf = compute_perf(code, e['dates'], e['last_date'])
            if perf:
                e['last_ret'] = perf['last_ret']
                e['avg_ret'] = perf['avg_ret']
    out = []
    for e in idx.values():
        out.append(e)
    out.sort(key=lambda x: (-x['count'], x['code']))
    return out


# ---------------------------------------------------------------- 自动采集 / 同步
def sync_from_source():
    """把数据源目录里新增/更新的 md 采集到数据目录"""
    cfg = load_config()
    src = cfg.get('source_dir') or SOURCE_DIR
    added, updated = [], []
    if not os.path.isdir(src):
        log('source dir missing: %s' % src)
        return {'added': added, 'updated': updated, 'error': '数据源目录不存在'}
    if os.path.abspath(src) == os.path.abspath(DATA_DIR):
        return {'added': added, 'updated': updated, 'note': '同目录无需同步'}
    try:
        for name in os.listdir(src):
            if not name.lower().endswith('.md'):
                continue
            sp = os.path.join(src, name)
            dp = os.path.join(DATA_DIR, name)
            try:
                if not os.path.isfile(sp):
                    continue
                need = False
                if not os.path.exists(dp):
                    need, tag = True, 'added'
                elif os.path.getsize(dp) != os.path.getsize(sp) or \
                     os.path.getmtime(dp) < os.path.getmtime(sp) - 1:
                    need, tag = True, 'updated'
                if need:
                    shutil.copy2(sp, dp)
                    (added if tag == 'added' else updated).append(name)
            except Exception as e:
                log('copy fail %s: %s' % (name, e))
    except Exception as e:
        log('sync fail: %s' % e)
    _state['last_sync'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    _state['sync_result'] = {'added': added, 'updated': updated}
    if added or updated:
        log('sync: +%d new, %d updated' % (len(added), len(updated)))
    return {'added': added, 'updated': updated}


def sync_worker():
    while True:
        try:
            cfg = load_config()
            iv = max(20, int(cfg.get('sync_interval', SYNC_INTERVAL)))
        except Exception:
            iv = SYNC_INTERVAL
        time.sleep(iv)
        try:
            sync_from_source()
        except Exception as e:
            log('sync_worker error: %s' % e)


# ---------------------------------------------------------------- 开机自启
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
RUN_NAME = '新闻联播看板'


def get_autostart():
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ)
        try:
            v, _ = winreg.QueryValueEx(k, RUN_NAME)
            return bool(v)
        finally:
            winreg.CloseKey(k)
    except Exception:
        return False


def set_autostart(enable):
    try:
        import winreg
        exe = sys.executable if getattr(sys, 'frozen', False) else \
              os.path.abspath(sys.argv[0])
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_ALL_ACCESS)
        try:
            if enable:
                winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, '"%s"' % exe)
            else:
                try:
                    winreg.DeleteValue(k, RUN_NAME)
                except FileNotFoundError:
                    pass
        finally:
            winreg.CloseKey(k)
        cfg = load_config()
        cfg['autostart'] = bool(enable)
        save_config(cfg)
        return True
    except Exception as e:
        log('set_autostart failed: %s' % e)
        return False


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj):
        self._send(200, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        q = parse_qs(u.query)
        try:
            if p in ('/', '/index.html'):
                with io.open(resource('web/index.html'), 'r', encoding='utf-8') as f:
                    self._send(200, f.read(), 'text/html; charset=utf-8')
                return
            if p == '/api/files':
                self._json({'files': scan_files()})
                return
            if p == '/api/all':
                out = []
                for f in scan_files():
                    path = os.path.join(DATA_DIR, f['name'])
                    try:
                        with io.open(path, 'r', encoding='utf-8', errors='replace') as fh:
                            md = fh.read()
                    except Exception:
                        md = ''
                    out.append({'date': f['date'], 'weekday': f['weekday'],
                                'topic': f['topic'], 'md': md})
                self._json({'files': out})
                return
            if p == '/api/stocks':
                self._json({'stocks': build_stocks()})
                return
            if p == '/api/file':
                name = unquote(q.get('name', [''])[0])
                if not name.lower().endswith('.md') or '/' in name or '\\' in name or '..' in name:
                    self._json({'error': 'invalid name'})
                    return
                path = os.path.join(DATA_DIR, name)
                if not os.path.isfile(path):
                    self._json({'error': 'not found'})
                    return
                with io.open(path, 'r', encoding='utf-8', errors='replace') as f:
                    self._json({'name': name, 'content': f.read()})
                return
            if p == '/api/status':
                files = scan_files()
                cfg = load_config()
                today = datetime.now().strftime('%Y-%m-%d')
                self._json({
                    'total_files': len(files),
                    'total_stocks': len(build_stocks(with_perf=False)),
                    'date_min': files[-1]['date'] if files else '',
                    'date_max': files[0]['date'] if files else '',
                    'today': today,
                    'today_ready': any(f['date'] == today for f in files),
                    'last_sync': _state['last_sync'],
                    'sync_result': _state['sync_result'],
                    'source_dir': cfg.get('source_dir'),
                    'data_dir': DATA_DIR,
                    'autostart': get_autostart(),
                    'port': _state['port'],
                })
                return
            self._send(404, 'not found', 'text/plain; charset=utf-8')
        except Exception as e:
            log('GET %s error: %s' % (p, e))
            self._json({'error': str(e)})

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        try:
            n = int(self.headers.get('Content-Length', 0) or 0)
            raw = self.rfile.read(n).decode('utf-8') if n else '{}'
            try:
                data = json.loads(raw)
            except Exception:
                data = {}
            if p == '/api/sync':
                self._json(sync_from_source())
                return
            if p == '/api/open':
                name = data.get('name', '')
                if name.lower().endswith('.md') and '..' not in name and '/' not in name and '\\' not in name:
                    path = os.path.join(DATA_DIR, name)
                    if os.path.isfile(path):
                        os.startfile(path)
                        self._json({'ok': True})
                        return
                self._json({'ok': False})
                return
            if p == '/api/reveal':
                os.startfile(DATA_DIR)
                self._json({'ok': True})
                return
            if p == '/api/opensource':
                cfg = load_config()
                src = cfg.get('source_dir') or SOURCE_DIR
                if os.path.isdir(src):
                    os.startfile(src)
                self._json({'ok': True})
                return
            if p == '/api/autostart':
                ok = set_autostart(bool(data.get('enable')))
                self._json({'ok': ok, 'autostart': get_autostart()})
                return
            if p == '/api/config':
                cfg = load_config()
                if data.get('source_dir'):
                    cfg['source_dir'] = data['source_dir']
                if data.get('sync_interval'):
                    try:
                        cfg['sync_interval'] = max(20, int(data['sync_interval']))
                    except Exception:
                        pass
                self._json({'ok': save_config(cfg), 'config': cfg})
                return
            if p == '/api/shutdown':
                self._json({'ok': True})
                threading.Thread(target=lambda: (time.sleep(0.4), _state['server'].shutdown()), daemon=True).start()
                return
            self._send(404, 'not found', 'text/plain; charset=utf-8')
        except Exception as e:
            log('POST %s error: %s' % (p, e))
            self._json({'error': str(e)})


LOCK_PATH = os.path.join(DATA_DIR, '.看板端口.lock')


def pick_port():
    import socket
    for p in range(PORT_START, PORT_START + 20):
        s = socket.socket()
        try:
            s.bind(('127.0.0.1', p))
            s.close()
            return p
        except Exception:
            s.close()
            continue
    return PORT_START


def running_instance():
    """若已有看板在运行，返回其端口，避免重复双击开出多个进程"""
    try:
        with io.open(LOCK_PATH, 'r', encoding='utf-8') as f:
            p = int(f.read().strip())
        import urllib.request
        urllib.request.urlopen('http://127.0.0.1:%d/api/status' % p, timeout=1).read()
        return p
    except Exception:
        return None


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    old = running_instance()
    if old:
        webbrowser.open('http://127.0.0.1:%d/' % old)   # 已有实例：直接打开它
        return
    log('start, data_dir=%s' % DATA_DIR)
    sync_from_source()
    port = pick_port()
    _state['port'] = port
    try:
        with io.open(LOCK_PATH, 'w', encoding='utf-8') as f:
            f.write(str(port))
    except Exception:
        pass
    srv = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    _state['server'] = srv
    url = 'http://127.0.0.1:%d/' % port
    threading.Thread(target=sync_worker, daemon=True).start()
    if not os.environ.get('NEWS_NO_BROWSER'):
        threading.Thread(target=lambda: (time.sleep(0.8), webbrowser.open(url)), daemon=True).start()
    log('serving %s' % url)
    try:
        srv.serve_forever()
    except Exception as e:
        log('server stopped: %s' % e)
    try:
        os.remove(LOCK_PATH)
    except Exception:
        pass
    log('exit')


if __name__ == '__main__':
    main()
