#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微信小游戏榜单游戏 → GameLook 相关文章采集（详情页数据源）。
按 wx_minigame_rank 中的游戏名搜索 GameLook，取标题匹配的文章（≤2篇/游戏），
抽取：发表日期、正文开头摘要（爆款分析素材）、文章内游戏截图（data-original，过滤 logo/二维码）。
入库 wx_game_articles 表。幂等：已有该游戏行则跳过。礼貌限速 1.2s。
"""
import re, sqlite3, time, json, urllib.request, urllib.parse, ssl, gzip
from datetime import datetime
from pathlib import Path

DB = Path('/home/ubuntu/game_site/games.db')
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0',
      'Accept-Language': 'zh-CN,zh;q=0.9'}
CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE

SCHEMA = """
CREATE TABLE IF NOT EXISTS wx_game_articles (
    game_name TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT, pub_date TEXT, snippet TEXT, images_json TEXT, fetched_at TEXT,
    PRIMARY KEY (game_name, url)
);
"""

def get(url, tmo=25):
    h = dict(UA); h['Accept-Encoding'] = 'gzip'; h['Referer'] = 'http://www.gamelook.com.cn/'
    r = urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=tmo, context=CTX)
    raw = r.read()
    if r.headers.get('Content-Encoding') == 'gzip' or raw[:2] == b'\x1f\x8b':
        raw = gzip.decompress(raw)
    return raw.decode('utf-8', 'replace')

def norm(s):
    return re.sub(r'[《》:：;；,，.。\s|·\-—…！？!?()"（）\[\]]', '', s)

def main():
    con = sqlite3.connect(DB, timeout=600)
    con.execute('PRAGMA busy_timeout=600000')
    con.executescript(SCHEMA)
    games = [r[0] for r in con.execute(
        """SELECT DISTINCT game_name FROM wx_minigame_rank
           WHERE game_name IS NOT NULL AND game_name != '' AND needs_review=0
           ORDER BY month DESC, rank ASC""").fetchall()]
    done = set(r[0] for r in con.execute('SELECT DISTINCT game_name FROM wx_game_articles').fetchall())
    todo = [g for g in games if g not in done]
    print(f'游戏 {len(games)} 款，已采 {len(done)}，待采 {len(todo)}', flush=True)
    now_s = datetime.now().isoformat(timespec='seconds')
    n_art = 0
    for gi, g in enumerate(todo):
        if len(norm(g)) < 2:
            continue
        try:
            html = get('http://www.gamelook.com.cn/?s=' + urllib.parse.quote(g))
        except Exception as e:
            print(f'[{gi+1}/{len(todo)}] {g} 搜索失败 {str(e)[:40]}', flush=True)
            time.sleep(1.5); continue
        arts = re.findall(
            r'href="(http://www\.gamelook\.com\.cn/\d{4}/\d{2}/\d+/)"[^>]*>([^<]{6,90})</a>', html)
        gn = norm(g)
        matched = []
        seen = set()
        for u, t in arts:
            if u in seen: continue
            seen.add(u)
            if gn in norm(t):
                matched.append((u, t.strip()))
            if len(matched) >= 2: break
        for u, t in matched:
            try:
                ah = get(u)
            except Exception:
                continue
            imgs = [i for i in re.findall(r'data-original="(http://img\.gamelook\.com\.cn/[^"]+)"', ah)
                    if not re.search(r'(logo|qcode|best\d|lazy|banner|\.gif)$', i, re.I)
                    and re.search(r'\.(jpg|jpeg|png)$', i, re.I)]
            txt = re.sub(r'<script.*?</script>|<style.*?</style>', '', ah, flags=re.S)
            txt = re.sub(r'<[^>]+>', ' ', txt); txt = re.sub(r'\s+', ' ', txt)
            i = txt.find('GameLook报道')
            snippet = txt[i:i+300] if i >= 0 else ''
            if not snippet:
                i = txt.find(norm(t)[:8] if norm(t) else g[:4])
                snippet = txt[i:i+300] if i >= 0 else ''
            dm = re.search(r'(\d{4}-\d{2}-\d{2})', txt)
            con.execute(
                """INSERT OR REPLACE INTO wx_game_articles
                   (game_name, url, title, pub_date, snippet, images_json, fetched_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (g, u, t, dm.group(1) if dm else '', snippet.strip(), json.dumps(imgs[:14], ensure_ascii=False), now_s))
            n_art += 1
            time.sleep(1.2)
        if (gi + 1) % 20 == 0:
            con.commit()
            print(f'[{gi+1}/{len(todo)}] 累计文章 {n_art}', flush=True)
        time.sleep(1.2)
    con.commit()
    print(f'== 完成：本轮采集文章 {n_art} 篇 ==', flush=True)
    con.close()

if __name__ == '__main__':
    main()
