#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微信小游戏畅销榜 Top100 采集器。
数据源：GameLook 月度《微信小游戏畅销榜Top100》文章（制榜=引力引擎，基于微信官方每日畅销榜积分）。
榜单以图片发布 → 下载原图（自动去 -WxH 缩略后缀取全分辨率）→ 行带检测+左右分栏+逐行OCR(tesseract chi_sim)
→ 结构化入库。每行带文章/图片溯源 URL；OCR 置信不足标 needs_review=1。
文章自校验：读 h1 标题，只收「微信」平台（抖音系列自动跳过）；榜单月份从标题 N月+发布年推断。
"""
import argparse, io, json, re, sqlite3, subprocess, sys, time, urllib.request, ssl, gzip
from datetime import datetime
from pathlib import Path

DB = Path('/home/ubuntu/game_site/games.db')
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0',
      'Accept-Language': 'zh-CN,zh;q=0.9', 'Referer': 'http://www.gamelook.com.cn/'}
CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE

# 候选文章（含抖音系列——运行时按 h1 自动过滤只留微信）
CANDIDATES = [
    'http://www.gamelook.com.cn/2026/08/599222/',
    'http://www.gamelook.com.cn/2026/11/606134/',
    'http://www.gamelook.com.cn/2026/09/601451/',
    'http://www.gamelook.com.cn/2026/11/605538/',
    'http://www.gamelook.com.cn/2026/10/603357/',
    'http://www.gamelook.com.cn/2026/09/601375/',
    'http://www.gamelook.com.cn/2026/08/599666/',
    'http://www.gamelook.com.cn/2026/01/585849/',
    'http://www.gamelook.com.cn/2026/01/585361/',
    'http://www.gamelook.com.cn/2025/12/583289/',
    'http://www.gamelook.com.cn/2025/04/567548/',
    'http://www.gamelook.com.cn/2025/03/565707/',
    'http://www.gamelook.com.cn/2024/12/559326/',
    'http://www.gamelook.com.cn/2024/09/553854/',
    'http://www.gamelook.com.cn/2024/09/552905/',
    'http://www.gamelook.com.cn/2024/08/550791/',
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS wx_minigame_rank (
    month TEXT NOT NULL, rank INTEGER NOT NULL,
    game_name TEXT, publisher TEXT, cat1 TEXT, cat2 TEXT, change TEXT,
    needs_review INTEGER DEFAULT 0, article_url TEXT, image_url TEXT, fetched_at TEXT,
    PRIMARY KEY (month, rank)
);
"""

def http_get(url, timeout=25):
    h = dict(UA); h['Accept-Encoding'] = 'gzip'
    r = urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout, context=CTX)
    raw = r.read()
    if r.headers.get('Content-Encoding') == 'gzip' or raw[:2] == b'\x1f\x8b':
        raw = gzip.decompress(raw)
    return raw


def verify_article(html, url):
    """h1 → (platform_ok, month_str, title)。平台必须微信；月份=N月+年份推断（N>发布月→上一年）。"""
    m = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.S)
    title = re.sub(r'<[^>]+>', '', m.group(1)).strip() if m else ''
    if '畅销榜' not in title:
        return False, None, title
    if '抖音' in title:
        return False, None, title
    is_wx = ('微信' in title) or ('小游戏' in title and '微信' in html[:60000])
    if not is_wx:
        return False, None, title
    pm = re.search(r'/(\d{4})/(\d{2})/\d+/$', url)
    mm = re.search(r'(\d{1,2})月', title)
    if not (pm and mm):
        return False, None, title
    pub_y, pub_m = int(pm.group(1)), int(pm.group(2))
    n = int(mm.group(1))
    year = pub_y if n <= pub_m else pub_y - 1
    return True, f'{year}-{n:02d}', title


def find_rank_image(html):
    """找 Top100 榜单大图：候选先试去缩略后缀的全分辨率原图；标准=高度>=850 且宽高比 0.6~1.9。"""
    from PIL import Image
    cands = []
    for u in re.findall(r'data-original="(http://img\.gamelook\.com\.cn/[^"]+)"', html):
        cands.append(u)
        stripped = re.sub(r'-\d{3,4}x\d{3,4}(\.\w+)$', r'\1', u)
        if stripped != u:
            cands.insert(len(cands) - 1, stripped)  # 原图优先
    seen = set()
    for u in cands:
        if u in seen: continue
        seen.add(u)
        try:
            raw = http_get(u)
            im = Image.open(io.BytesIO(raw))
            if im.height >= 850 and 0.6 <= im.width / im.height <= 1.9:
                return u, raw
        except Exception:
            continue
    return None, None


def ocr_rank_rows(raw_img):
    from PIL import Image, ImageOps
    import numpy as np
    im = Image.open(io.BytesIO(raw_img)).convert('L')
    W, H = im.size
    a = np.array(im)
    dark = (a < 140).sum(axis=1)
    blank = dark < dark.max() * 0.02
    rows = []; in_row = False; start = 0
    for y, b in enumerate(blank):
        if not b and not in_row: in_row = True; start = y
        elif b and in_row:
            in_row = False
            if y - start > 8: rows.append((start, y))
    if in_row: rows.append((start, H))
    colsplit = int(W * 0.465)
    recs = {}
    for ri, (y0, y1) in enumerate(rows):
        if ri == 0: continue
        band = im.crop((0, max(0, y0 - 3), W, min(H, y1 + 3)))
        h = band.height
        for side, rng in [('L', (1, 50)), ('R', (51, 100))]:
            crop = band.crop((0, 0, colsplit, h)) if side == 'L' else band.crop((colsplit, 0, W, h))
            p = crop.resize((crop.width * 3, crop.height * 3), Image.LANCZOS)
            p = ImageOps.autocontrast(p)
            p = p.point(lambda v: 255 if v > 150 else 0)
            p.save('/tmp/_wxr.png')
            try:
                t = subprocess.run(['tesseract', '/tmp/_wxr.png', '-', '-l', 'chi_sim', '--psm', '7'],
                                   capture_output=True, text=True, timeout=90).stdout.strip()
            except Exception:
                continue
            toks = t.split()
            for i, tk in enumerate(toks):
                m2 = re.fullmatch(r'(\d{1,3})', tk.strip('|。,，'))
                if m2 and rng[0] <= int(m2.group(1)) <= rng[1]:
                    r = int(m2.group(1))
                    if r not in recs or len(toks[i + 1:]) > len(recs[r]):
                        recs[r] = toks[i + 1:]
                    break
    return recs

CATS = {'休闲', '角色', '策略', '棋牌'}

def structure(recs):
    out = {}
    for r, rest in recs.items():
        ci = None
        for i, t in enumerate(rest):
            if t.strip('”"\'、') in CATS: ci = i; break
        if ci is None or ci == 0:
            out[r] = {'name': ' '.join(rest[:2]) if rest else '', 'publisher': '',
                      'cat1': '', 'cat2': '', 'change': '', 'needs_review': 1}
        else:
            head = rest[:ci]; tail = rest[ci:]
            out[r] = {'name': ''.join(head[:-1]) if len(head) > 1 else head[0],
                      'publisher': head[-1] if len(head) > 1 else '',
                      'cat1': tail[0], 'cat2': tail[1] if len(tail) > 1 else '',
                      'change': ' '.join(tail[2:]) if len(tail) > 2 else '',
                      'needs_review': 0}
    return out

def collect_one(con, url):
    html = http_get(url).decode('utf-8', 'replace')
    ok, month, title = verify_article(html, url)
    if not ok:
        print(f'[skip] {title[:40]} | {url}', flush=True)
        return 0
    img_url, raw = find_rank_image(html)
    if not img_url:
        print(f'[{month}] 未找到榜单图: {url}', flush=True)
        return 0
    data = structure(ocr_rank_rows(raw))
    now = datetime.now().isoformat(timespec='seconds')
    n = 0
    for r in range(1, 101):
        d = data.get(r)
        if not d: continue
        con.execute("""INSERT OR REPLACE INTO wx_minigame_rank
            (month, rank, game_name, publisher, cat1, cat2, change, needs_review, article_url, image_url, fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (month, r, d['name'], d['publisher'], d['cat1'], d['cat2'], d['change'],
             d['needs_review'], url, img_url, now))
        n += 1
    con.commit()
    okn = sum(1 for d in data.values() if not d['needs_review'])
    print(f'[{month}] 入库 {n} 行（高置信 {okn}） ← {title[:36]}', flush=True)
    return n

def ingest_json(con, path, month, url):
    """导入已人工校验的 JSON（如 2026-07 手工修正版）。"""
    data = json.load(open(path))
    now = datetime.now().isoformat(timespec='seconds')
    for r, d in data.items():
        con.execute("""INSERT OR REPLACE INTO wx_minigame_rank
            (month, rank, game_name, publisher, cat1, cat2, change, needs_review, article_url, image_url, fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (month, int(r), d['name'], d.get('publisher',''), d.get('cat',''), d.get('sub',''),
             d.get('change',''), 0, url, '', now))
    con.commit()
    print(f'[{month}] JSON 导入 {len(data)} 行', flush=True)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--url'); ap.add_argument('--month')
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--ingest-json', help='path month url')
    args = ap.parse_args()
    con = sqlite3.connect(DB, timeout=600)
    con.execute('PRAGMA busy_timeout=600000')
    con.executescript(SCHEMA)
    if args.ingest_json:
        p, m, u = args.ingest_json.split(' ', 2)
        ingest_json(con, p, m, u)
    elif args.url and args.month:
        collect_one(con, args.url)
    elif args.all:
        total = 0
        for url in CANDIDATES:
            try:
                total += collect_one(con, url)
            except Exception as e:
                print(f'FAIL {url} {e}', flush=True)
            time.sleep(2)
        print(f'== 回填完成 共 {total} 行 ==', flush=True)
    else:
        ap.print_help()
    con.close()

if __name__ == '__main__':
    main()
