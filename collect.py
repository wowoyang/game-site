#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
游戏趋势数据采集器 —— 只存真实观测值，抓不到就留空，绝不估算。

数据源（全部官方/权威 API）:
  Steam:
    1. api.steampowered.com/ISteamChartsService/GetMostPlayedGames  (Valve 官方)
       rank / last_week_rank / peak_in_game
    2. store.steampowered.com/api/appdetails  (Valve 官方, 仅新游戏补元数据)
       name / genres / release_date / price / recommendations.total
    3. store.steampowered.com/appreviews  (Valve 官方, 近30天评论)
    4. steamspy.com top100in2weeks + appdetails  (SteamSpy)
       tags(玩法标签) / average_2weeks / average_forever / owners 区间 / ccu
  Roblox:
    5. apis.roblox.com/explore-api/v1/get-sorts  (官方四榜单+实时 playerCount)
    6. games.roblox.com/v1/games   (playing/visits/favoritedCount/genre)
    7. thumbnails.roblox.com/v1/games/icons  (图标)
  Epic:
    8. store-site-backend-static.ak.epicgames.com/freeGamesPromotions (官方)
"""
import json
import os
import sqlite3
import ssl
import time
import urllib.request
import urllib.error
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "games.db")
LOG_PATH = os.path.join(BASE, "collect.log")

UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    date TEXT PRIMARY KEY,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS games (
    platform TEXT NOT NULL,
    game_id TEXT NOT NULL,
    name TEXT,
    name_cn TEXT,
    developer TEXT,
    publisher TEXT,
    genres TEXT,
    tags TEXT,
    release_date TEXT,
    first_seen TEXT,
    last_seen TEXT,
    meta_json TEXT,
    PRIMARY KEY (platform, game_id)
);
CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT NOT NULL,
    platform TEXT NOT NULL,
    game_id TEXT NOT NULL,
    ccu INTEGER,
    peak_in_game INTEGER,
    rk INTEGER,
    last_week_rank INTEGER,
    reviews_total INTEGER,
    reviews_delta_30d INTEGER,
    avg_playtime_2w INTEGER,
    avg_playtime_forever INTEGER,
    owners TEXT,
    players INTEGER,
    visits INTEGER,
    favorites INTEGER,
    extra_json TEXT,
    PRIMARY KEY (date, platform, game_id)
);
CREATE INDEX IF NOT EXISTS idx_ds_plat ON daily_stats(platform, date);
CREATE INDEX IF NOT EXISTS idx_games_name ON games(name);
"""

# 老库迁移：幂等加列
def _migrate(con):
    cols = {r[1] for r in con.execute("PRAGMA table_info(games)").fetchall()}
    if "name_cn" not in cols:
        con.execute("ALTER TABLE games ADD COLUMN name_cn TEXT")
        log("迁移: games.name_cn 已加列")
    if "desc_cn" not in cols:
        con.execute("ALTER TABLE games ADD COLUMN desc_cn TEXT")
        log("迁移: games.desc_cn 已加列")
    con.commit()


def today():
    return datetime.now().strftime("%Y-%m-%d")


def log(msg):
    line = f"[{datetime.now().strftime('%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def http_get(url, timeout=25, retries=2):
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers=UA)
        try:
            r = urllib.request.urlopen(req, timeout=timeout, context=CTX)
            return r.status, r.read()
        except urllib.error.HTTPError as e:
            body = None
            try:
                body = e.read()
            except Exception:
                pass
            if e.code in (429, 500, 502, 503) and attempt < retries:
                time.sleep(3 * (attempt + 1))
                continue
            return e.code, body
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
    return -1, None


def http_json(url, timeout=25):
    status, body = http_get(url, timeout)
    if status == 200 and body:
        try:
            return json.loads(body)
        except Exception:
            if b"Too many connections" in body:
                raise RuntimeError("steamspy 连接数限制")  # 让上层重试
            return None
    return None


def ensure_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    con.commit()
    return con


def has_cjk(s):
    """是否含 CJK 字符（判定官方是否提供了真实中文名）"""
    return bool(s) and any('\u4e00' <= c <= '\u9fff' for c in s)


def clean_cn_name(s):
    """清洗官方中文名：去《》包装；不含 CJK 视为无中文名，返回 None"""
    if not has_cjk(s):
        return None
    s = s.strip()
    if s.startswith("《") and s.endswith("》"):
        s = s[1:-1].strip()
    return s or None


def upsert_game(con, platform, game_id, name=None, name_cn=None, developer=None,
                publisher=None, genres=None, tags=None, release_date=None, meta=None,
                desc_cn=None):
    d = today()
    if meta:  # 键级合并：新值只补/覆盖自己的键，不整体清掉旧 meta
        old = con.execute(
            "SELECT meta_json FROM games WHERE platform=? AND game_id=?",
            (platform, game_id)).fetchone()
        if old and old[0]:
            try:
                om = json.loads(old[0])
                om.update(meta)
                meta = om
            except Exception:
                pass
    con.execute(
        """INSERT INTO games (platform, game_id, name, name_cn, developer, publisher, genres,
             tags, release_date, first_seen, last_seen, meta_json, desc_cn)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(platform, game_id) DO UPDATE SET
             name=COALESCE(excluded.name, games.name),
             name_cn=COALESCE(excluded.name_cn, games.name_cn),
             developer=COALESCE(excluded.developer, games.developer),
             publisher=COALESCE(excluded.publisher, games.publisher),
             genres=COALESCE(excluded.genres, games.genres),
             tags=COALESCE(excluded.tags, games.tags),
             release_date=COALESCE(excluded.release_date, games.release_date),
             meta_json=COALESCE(excluded.meta_json, games.meta_json),
             desc_cn=COALESCE(excluded.desc_cn, games.desc_cn),
             last_seen=excluded.last_seen""",
        (platform, game_id, name, name_cn, developer, publisher,
         json.dumps(genres, ensure_ascii=False) if genres else None,
         json.dumps(tags, ensure_ascii=False) if tags else None,
         release_date, d, d,
         json.dumps(meta, ensure_ascii=False) if meta else None,
         desc_cn),
    )


def upsert_stat(con, date, platform, game_id, ccu=None, peak_in_game=None, rk=None,
                last_week_rank=None, reviews_total=None, reviews_delta_30d=None,
                avg_playtime_2w=None, avg_playtime_forever=None, owners=None,
                players=None, visits=None, favorites=None, extra=None):
    con.execute(
        """INSERT INTO daily_stats (date, platform, game_id, ccu, peak_in_game, rk,
             last_week_rank, reviews_total, reviews_delta_30d, avg_playtime_2w,
             avg_playtime_forever, owners, players, visits, favorites, extra_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(date, platform, game_id) DO UPDATE SET
             ccu=COALESCE(excluded.ccu, daily_stats.ccu),
             peak_in_game=COALESCE(excluded.peak_in_game, daily_stats.peak_in_game),
             rk=COALESCE(excluded.rk, daily_stats.rk),
             last_week_rank=COALESCE(excluded.last_week_rank, daily_stats.last_week_rank),
             reviews_total=COALESCE(excluded.reviews_total, daily_stats.reviews_total),
             reviews_delta_30d=COALESCE(excluded.reviews_delta_30d, daily_stats.reviews_delta_30d),
             avg_playtime_2w=COALESCE(excluded.avg_playtime_2w, daily_stats.avg_playtime_2w),
             avg_playtime_forever=COALESCE(excluded.avg_playtime_forever, daily_stats.avg_playtime_forever),
             owners=COALESCE(excluded.owners, daily_stats.owners),
             players=COALESCE(excluded.players, daily_stats.players),
             visits=COALESCE(excluded.visits, daily_stats.visits),
             favorites=COALESCE(excluded.favorites, daily_stats.favorites),
             extra_json=COALESCE(excluded.extra_json, daily_stats.extra_json)""",
        (date, platform, game_id, ccu, peak_in_game, rk, last_week_rank,
         reviews_total, reviews_delta_30d, avg_playtime_2w, avg_playtime_forever,
         owners, players, visits, favorites,
         json.dumps(extra, ensure_ascii=False) if extra else None),
    )


# ================================================================ Steam ====

def collect_steam(con, date):
    ranks = http_json(
        "https://api.steampowered.com/ISteamChartsService/GetMostPlayedGames/v1/?mine=0")
    if not (ranks and ranks.get("response", {}).get("ranks")):
        log("steam charts FAIL — 跳过 steam")
        return
    ranks = ranks["response"]["ranks"]
    log(f"steam charts OK: {len(ranks)} games (Valve 官方最热榜)")

    spy_top = http_json("https://steamspy.com/api.php?request=top100in2weeks") or {}
    log(f"steamspy top100in2weeks OK: {len(spy_top)} games")

    # ---- SteamSpy appdetails（tags / playtime / owners），限速 1.1s
    wanted = [str(r["appid"]) for r in ranks]
    for aid in list(spy_top.keys()):
        if aid not in wanted and len(wanted) < 110:
            wanted.append(aid)
    spy_detail = {}
    for i, aid in enumerate(wanted):
        d = http_json(f"https://steamspy.com/api.php?request=appdetails&appid={aid}")
        if d and "tags" in d:
            spy_detail[aid] = d
        if i % 25 == 24:
            log(f"  steamspy detail {i + 1}/{len(wanted)} ...")
        time.sleep(1.1)
    log(f"steamspy details OK: {len(spy_detail)}/{len(wanted)}")

    # ---- Valve appdetails：top30 榜首游戏每日观测累计评论数（后续按历史差值算增速）
    #     + 为库里缺 release_date 的游戏补元数据
    cur = con.execute(
        "SELECT game_id FROM games WHERE platform='steam' AND release_date IS NOT NULL")
    known = {r[0] for r in cur.fetchall()}
    need_meta = [aid for aid in wanted if aid not in known][:40]
    fetch_list = list(dict.fromkeys([str(r["appid"]) for r in ranks[:30]] + need_meta))
    valve_meta = {}
    for aid in fetch_list:
        j = http_json(
            f"https://store.steampowered.com/api/appdetails?appids={aid}&cc=us&l=en")
        node = (j or {}).get(aid) or {}
        if node.get("success") and node.get("data"):
            valve_meta[aid] = node["data"]
        time.sleep(0.5)
    log(f"valve appdetails OK: {len(valve_meta)}/{len(fetch_list)} "
        f"(含 top30 评论数观测 + {len(need_meta)} 新游戏元数据)")

    # ---- Valve appdetails l=schinese：官方中文店名 + 中文简介
    #     只有含 CJK 的才算真实中文，其余保持 None（不猜测、不机翻）
    cur = con.execute(
        "SELECT game_id FROM games WHERE platform='steam' AND name_cn IS NOT NULL")
    have_cn = {r[0] for r in cur.fetchall()}
    cn_need = [aid for aid in wanted if aid not in have_cn]
    cn_names = {}
    desc_map = {}
    # 中文简介：对 top30 榜首游戏用完整 appdetails(无 filters)拿 short_description
    for aid in [str(r["appid"]) for r in ranks[:30]]:
        j = http_json(
            f"https://store.steampowered.com/api/appdetails?appids={aid}"
            f"&l=schinese&cc=cn")
        node = (j or {}).get(aid) or {}
        dat = node.get("data") or {}
        if dat.get("short_description") and has_cjk(dat["short_description"]):
            desc_map[aid] = dat["short_description"].strip()
        nm = clean_cn_name(dat.get("name", ""))
        if nm:
            cn_names[aid] = nm
        time.sleep(0.4)
    log(f"steam 中文简介 OK: {len(desc_map)} 条；中文店名(完整) {len(cn_names)}")
    for aid in cn_need:  # 其余缺名游戏：轻量 filters=basic 仅取名
        if aid in cn_names:
            continue
        j = http_json(
            f"https://store.steampowered.com/api/appdetails?appids={aid}"
            f"&l=schinese&filters=basic")
        node = (j or {}).get(aid) or {}
        cn = clean_cn_name((node.get("data") or {}).get("name", ""))
        if cn:
            cn_names[aid] = cn
        time.sleep(0.4)
    log(f"steam 中文名 OK: {len(cn_names)}/{len(cn_need)} 含 CJK 的官方中文名")

    # ---- 写库
    for r in ranks:
        aid = str(r["appid"])
        sd = spy_detail.get(aid) or spy_top.get(aid) or {}
        vm = valve_meta.get(aid) or {}
        upsert_game(
            con, "steam", aid,
            name=vm.get("name") or sd.get("name"),
            name_cn=cn_names.get(aid),
            desc_cn=desc_map.get(aid),
            developer=sd.get("developer"),
            publisher=sd.get("publisher"),
            genres=vm.get("genres") and [g["description"] for g in vm["genres"]] or
                   (sd.get("genre") and sd["genre"].split(", ")),
            tags=sd.get("tags"),
            release_date=(vm.get("release_date") or {}).get("date"),
            meta={
                **({"price_usd": vm["price_overview"]["final"] / 100}
                   if vm.get("price_overview") else
                   ({"price_usd": 0} if vm.get("is_free") else {})),
                **({"userscore": sd["userscore"], "positive": sd["positive"],
                    "negative": sd["negative"]}
                   if sd.get("userscore") is not None else {}),
            },
        )
        upsert_stat(
            con, date, "steam", aid,
            peak_in_game=r.get("peak_in_game"),
            rk=r.get("rank"),
            last_week_rank=r.get("last_week_rank"),
            ccu=sd.get("ccu"),
            avg_playtime_2w=sd.get("average_2weeks"),
            avg_playtime_forever=sd.get("average_forever"),
            owners=sd.get("owners"),
            reviews_total=(vm.get("recommendations") or {}).get("total"),
        )
    # spy top100 里不在官方榜的也记录（近两周热度观测）
    chart_ids = {str(r["appid"]) for r in ranks}
    for aid, row in spy_top.items():
        if aid in chart_ids:
            continue
        sd = spy_detail.get(aid) or {}
        vm = valve_meta.get(aid) or {}
        upsert_game(
            con, "steam", aid, name=row.get("name") or sd.get("name"),
            name_cn=cn_names.get(aid),
            developer=sd.get("developer") or row.get("developer"),
            genres=sd.get("genre") and sd["genre"].split(", "),
            tags=sd.get("tags"),
            release_date=(vm.get("release_date") or {}).get("date"),
        )
        upsert_stat(
            con, date, "steam", aid,
            ccu=row.get("ccu"),
            avg_playtime_2w=row.get("average_2weeks"),
            avg_playtime_forever=row.get("average_forever"),
            owners=row.get("owners"),
        )
    con.commit()
    log(f"steam 写入完成: 官方榜 {len(ranks)} + spy 独有 "
        f"{max(0, len(spy_top) - len(chart_ids & set(spy_top)))} 行")


def enrich_steam(con, max_games=3000, date=None):
    """滚动富化（活跃优先 + 每日限额）：
    为库内 steam 游戏补 Valve appdetails 元数据（价格/好评率/发行日/
    类型/中文店名/中文简介）。
    87K 全量逐款富化需 ~40h API 时间，不可一次跑完——
    P0 当日活跃游戏优先，P1 冷门按入库序（≈拥有量降序）滚动，每日限额。
    幂等：发行日+类型+价格已齐的跳过。userscore 已有的不再打 SteamSpy。"""
    date = date or today()
    try:
        act = {r[0] for r in con.execute(
            "SELECT game_id FROM daily_stats WHERE platform='steam' AND date=?",
            (date,))}
    except Exception:
        act = set()
    rows = con.execute(
        "SELECT game_id, name, release_date, genres, meta_json FROM games "
        "WHERE platform='steam' ORDER BY rowid").fetchall()
    todo = []
    for aid, name, rel, genres, mj in rows:
        m = json.loads(mj or '{}')
        if rel and genres and m.get("price_usd") is not None:
            continue  # 关键字段已齐
        todo.append((aid, name, rel, genres, m))
    act_todo = [t for t in todo if t[0] in act]
    cold_todo = [t for t in todo if t[0] not in act]
    todo = (act_todo + cold_todo)[:max_games]
    log(f"steam enrich: 待补 {len(act_todo)} 活跃 + {len(cold_todo)} 冷门，"
        f"本轮限额 {max_games} 款")
    done = 0
    for aid, name, rel, genres, m in todo:
        # 英文完整版：价格/发行日/类型/评论数
        j = http_json(
            f"https://store.steampowered.com/api/appdetails?appids={aid}&cc=us&l=en")
        node = (j or {}).get(aid) or {}
        dat = node.get("data") or {}
        meta = {}
        if dat:
            if (dat.get("price_overview") or {}).get("final") is not None:
                meta["price_usd"] = dat["price_overview"]["final"] / 100
            elif dat.get("is_free"):
                meta["price_usd"] = 0  # F2P 官方标记（无 price_overview 的免费游戏）
            if (dat.get("recommendations") or {}).get("total") is not None:
                meta["reviews_total_store"] = dat["recommendations"]["total"]
        cn = None
        if dat:
            # 中文版同游戏再拉一次（官方中文店名+简介）
            jc = http_json(
                f"https://store.steampowered.com/api/appdetails?appids={aid}"
                f"&l=schinese&cc=cn")
            datc = ((jc or {}).get(aid) or {}).get("data") or {}
            nm = clean_cn_name(datc.get("name", ""))
            if nm:
                cn = nm
            if datc.get("short_description") and has_cjk(datc["short_description"]):
                con.execute(
                    "UPDATE games SET desc_cn=? WHERE platform='steam' AND game_id=?",
                    (datc["short_description"].strip(), aid))
        # SteamSpy 口碑数——仅缺 userscore 时才打（省 1 次请求+限速）
        if m.get("userscore") is None:
            spy = http_json(f"https://steamspy.com/api.php?request=appdetails&appid={aid}")
            if spy and spy.get("userscore"):
                meta["userscore"] = spy["userscore"]
                meta["positive"] = spy["positive"]
                meta["negative"] = spy["negative"]
        # 发行日/类型从英文版取
        rel_v = ((dat.get("release_date") or {}).get("date")) or rel
        gen_v = [g["description"] for g in (dat.get("genres") or [])] or None
        if meta or rel_v != rel or gen_v:
            con.execute(
                """UPDATE games SET release_date=?, genres=?, meta_json=?
                   WHERE platform='steam' AND game_id=?""",
                (rel_v,
                 json.dumps(gen_v, ensure_ascii=False) if gen_v else genres,
                 json.dumps({**m, **meta}, ensure_ascii=False),
                 aid))
        if cn:
            con.execute(
                "UPDATE games SET name_cn=? WHERE platform='steam' AND game_id=? "
                "AND (name_cn IS NULL OR name_cn='')",
                (cn, aid))
        done += 1
        if done % 20 == 0:
            log(f"  enrich {done}/{len(todo)} ...")
            con.commit()
        time.sleep(0.4)
    con.commit()
    log(f"steam enrich 完成: 本轮 {done} 款")


def collect_steam_universe(con, date=None):
    """Steam 全量游戏宇宙（用户要求：深研覆盖所有游戏，不只热门）。
    SteamSpy /all 全量清单 87 页 × 1000 款（按拥有量降序）：
    - games 表：全量 upsert（名称/厂商/价格/评分/拥有量区间等直接观测）
    - daily_stats：只写有活跃信号（ccu>0 或 avg_2w>0）的当日观测行
    - 空名游戏跳过（多为下架，不造名）
    每日重刷 ≈ 87 × 1.1s ≈ 2 分钟，字段滚动更新到最新观测。"""
    date = date or today()
    added = 0
    stats = 0
    skipped = 0
    failed_pages = []
    for page in range(0, 90):
        d = None
        for attempt in range(6):  # Too many connections 需长退避
            try:
                d = http_json(
                    f"https://steamspy.com/api.php?request=all&page={page}")
                break
            except Exception as ex:
                wait = min(5 * (attempt + 1), 30)
                if attempt < 5:
                    time.sleep(wait)
                else:
                    log(f"  universe p{page} 6次重试全失败 → 记入回补队列")
                    failed_pages.append(page)
        if not d:
            continue  # 失败页跳过，最后统一回补
        for aid, e in d.items():
            aid = str(aid)
            name = (e.get("name") or "").strip()
            if not name:
                skipped += 1
                continue
            price = e.get("price")
            meta = {
                "price_usd": int(price) / 100 if price not in (None, "") else None,
                "initialprice_usd": int(e.get("initialprice") or 0) / 100 or None,
                "discount": e.get("discount") or None,
                "userscore": e.get("userscore"),
                "positive": e.get("positive"),
                "negative": e.get("negative"),
                "score_rank": e.get("score_rank") or None,
                "median_2w": e.get("median_2weeks"),
                "median_forever": e.get("median_forever"),
            }
            meta = {k: v for k, v in meta.items() if v is not None}
            upsert_game(con, "steam", aid, name=name,
                        developer=e.get("developer") or None,
                        publisher=e.get("publisher") or None,
                        meta=meta)
            added += 1
            ccu = e.get("ccu") or 0
            a2w = e.get("average_2weeks") or 0
            if ccu > 0 or a2w > 0:
                reviews = (e.get("positive") or 0) + (e.get("negative") or 0)
                upsert_stat(con, date, "steam", aid,
                            ccu=ccu or None,
                            avg_playtime_2w=e.get("average_2weeks") or None,
                            avg_playtime_forever=e.get("average_forever") or None,
                            owners=e.get("owners"),
                            reviews_total=reviews or None)
                stats += 1
        if page % 10 == 0:
            con.commit()
            log(f"  universe p{page}: 累计 {added} 款, 活跃 {stats}")
        time.sleep(1.1)
    # 失败页回补（再给 3 轮机会，间歇性 DB 过载通常几十秒后恢复）
    for rnd in range(1, 4):
        if not failed_pages:
            break
        time.sleep(30)
        still = []
        for page in failed_pages:
            try:
                d = http_json(
                    f"https://steamspy.com/api.php?request=all&page={page}")
            except Exception:
                still.append(page)
                continue
            if not d:
                still.append(page)
                continue
            for aid, e in d.items():
                aid = str(aid)
                name = (e.get("name") or "").strip()
                if not name:
                    continue
                meta = _universe_meta(e)
                upsert_game(con, "steam", aid, name=name,
                            developer=e.get("developer") or None,
                            publisher=e.get("publisher") or None,
                            meta=meta)
                added += 1
                if (e.get("ccu") or 0) > 0 or (e.get("average_2weeks") or 0) > 0:
                    reviews = (e.get("positive") or 0) + (e.get("negative") or 0)
                    upsert_stat(con, date, "steam", aid,
                                ccu=e.get("ccu") or None,
                                avg_playtime_2w=e.get("average_2weeks") or None,
                                avg_playtime_forever=e.get("average_forever") or None,
                                owners=e.get("owners"),
                                reviews_total=reviews or None)
                    stats += 1
            time.sleep(2)
        failed_pages = still
        log(f"  universe 回补第{rnd}轮: 剩余失败页 {failed_pages}")
    con.commit()
    log(f"steam universe 完成: 全量 {added} 行(空名跳过 {skipped}), "
        f"活跃快照 {stats} 行, 未恢复页 {failed_pages}")


def _universe_meta(e):
    price = e.get("price")
    meta = {
        "price_usd": int(price) / 100 if price not in (None, "") else None,
        "initialprice_usd": int(e.get("initialprice") or 0) / 100 or None,
        "discount": e.get("discount") or None,
        # /all 端点 userscore 对部分游戏恒返 0（Dota2 等明显脏值），
        # 0 不入库——好评率以 appdetails 富化的真实观测为准
        "userscore": e.get("userscore") or None,
        "positive": e.get("positive"),
        "negative": e.get("negative"),
        "score_rank": e.get("score_rank") or None,
        "median_2w": e.get("median_2weeks"),
        "median_forever": e.get("median_forever"),
    }
    return {k: v for k, v in meta.items() if v is not None}


# =============================================================== Roblox ====

SORT_KEYS = {
    "Top Trending": "trending",
    "Up-and-Coming": "upcoming",
    "Top Playing Now": "playing",
    "Top Revisited": "revisited",
}


def collect_roblox(con, date):
    j = http_json(
        "https://apis.roblox.com/explore-api/v1/get-sorts?sessionId=hermes&pageType=home")
    if not (j and j.get("sorts")):
        log("roblox sorts FAIL — 跳过 roblox")
        return
    # 汇总每个 universeId 的榜单位置
    places = {}   # uid -> {sortKey: rank}
    info = {}     # uid -> {name, playerCount, totalUpVotes, genreL1}
    for s in j["sorts"]:
        key = SORT_KEYS.get(s.get("sortDisplayName", ""))
        games = s.get("games") or []
        if not key or not games:
            continue
        for pos, g in enumerate(games, 1):
            uid = str(g.get("universeId") or "")
            if not uid:
                continue
            places.setdefault(uid, {})[key] = pos
            if uid not in info:
                info[uid] = {
                    "name": g.get("name"),
                    "playerCount": g.get("playerCount"),
                    "totalUpVotes": g.get("totalUpVotes"),
                    "totalDownVotes": g.get("totalDownVotes"),
                    "rootPlaceId": g.get("rootPlaceId"),
                    "genreL1": g.get("genreL1"),
                }
    log(f"roblox sorts OK: {len(info)} games "
        f"({', '.join(sorted({k for v in places.values() for k in v}))})")

    # ---- games.roblox.com 批量详情（接口上限 50/批）
    uids = list(info.keys())
    detail = {}
    for i in range(0, len(uids), 50):
        batch = ",".join(uids[i:i + 50])
        d = http_json(f"https://games.roblox.com/v1/games?universeIds={batch}")
        for g in (d or {}).get("data") or []:
            detail[str(g["id"])] = g
        time.sleep(0.5)
    log(f"roblox games detail OK: {len(detail)}/{len(uids)}")

    # ---- 图标
    icons = {}
    for i in range(0, len(uids), 100):
        batch = ",".join(uids[i:i + 100])
        d = http_json(
            "https://thumbnails.roblox.com/v1/games/icons?universeIds="
            f"{batch}&size=256x256&format=Png&isCircular=false")
        for t in (d or {}).get("data") or []:
            if t.get("state") == "Completed" and t.get("imageUrl"):
                icons[str(t["targetId"])] = t["imageUrl"]
        time.sleep(0.5)
    log(f"roblox icons OK: {len(icons)}")

    for uid in uids:
        g = detail.get(uid) or {}
        o = info[uid]
        upsert_game(
            con, "roblox", uid,
            name=g.get("name") or o["name"],
            developer=(g.get("creator") or {}).get("name"),
            genres=[o["genreL1"]] if o.get("genreL1") else
                   ([g["genre"]] if g.get("genre") else None),
            release_date=g.get("created"),
            meta={
                **({"icon": icons[uid]} if uid in icons else {}),
                **({"upVotes": o["totalUpVotes"]} if o["totalUpVotes"] is not None else {}),
                **({"downVotes": o["totalDownVotes"]} if o.get("totalDownVotes") is not None else {}),
                **({"rootPlaceId": o["rootPlaceId"]} if o.get("rootPlaceId") else {}),
                **({"maxPlayers": g["maxPlayers"]} if g.get("maxPlayers") else {}),
            },
        )
        upsert_stat(
            con, date, "roblox", uid,
            players=g.get("playing") or o["playerCount"],
            visits=g.get("visits"),
            favorites=g.get("favoritedCount"),
            extra={"sorts": places[uid], "playerCountInSort": o["playerCount"]},
        )
    con.commit()
    log(f"roblox 写入完成: {len(uids)} 行")


# ================================================================ Epic =====

def collect_epic(con, date):
    j = http_json(
        "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
        "?locale=en-US&countryCode=US&allowCountries=US")
    elements = ((j or {}).get("data") or {}).get("Catalog", {}).get(
        "searchStore", {}).get("elements") or []
    if not elements:
        log("epic freeGames FAIL — 跳过 epic")
        return
    # ---- Epic 官方 zh-CN 目录：真实中文标题（无 CJK 则无中文名）
    jcn = http_json(
        "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
        "?locale=zh-CN&countryCode=US&allowCountries=US")
    cn_map = {}
    for el in ((jcn or {}).get("data") or {}).get("Catalog", {}).get(
            "searchStore", {}).get("elements") or []:
        if el.get("id"):
            cn = clean_cn_name(el.get("title") or "")
            if cn:
                cn_map[el["id"]] = cn
    log(f"epic 中文名 OK: {len(cn_map)}/{len(elements)} 含 CJK 的官方中文名")
    # ---- Epic 官方 zh-CN 目录同步取中文简介（同一响应的 description 字段）
    desc_map = {}
    for el in ((jcn or {}).get("data") or {}).get("Catalog", {}).get(
            "searchStore", {}).get("elements") or []:
        d = (el.get("description") or "").strip()
        if el.get("id") and d and has_cjk(d):
            desc_map[el["id"]] = d
    log(f"epic 中文简介 OK: {len(desc_map)} 条")
    n = 0
    for e in elements:
        gid = e.get("id")
        if not gid:
            continue
        promos = e.get("promotions") or {}
        cur = (promos.get("promotionalOffers") or [])
        upc = (promos.get("upcomingPromotionalOffers") or [])
        img = None
        for k in e.get("keyImages") or []:
            if k.get("type") in ("DieselGameBox", "OfferImageWide") and k.get("url"):
                img = k["url"]
                break
        price = ((e.get("price") or {}).get("totalPrice") or {})
        # 商店页 slug：优先 productSlug，空则用 catalogNs.mappings(productHome)
        slug = e.get("productSlug")
        if not slug:
            for mp in ((e.get("catalogNs") or {}).get("mappings") or []):
                if mp.get("pageType") == "productHome" and mp.get("pageSlug"):
                    slug = mp["pageSlug"]
                    break
        upsert_game(
            con, "epic", gid,
            name=e.get("title"),
            name_cn=cn_map.get(gid),
            desc_cn=desc_map.get(gid),
            developer=((e.get("seller") or {}).get("name")),
            meta={
                **({"icon": img} if img else {}),
                "productSlug": slug,
                "originalPrice_usd": price.get("originalPrice") / 100
                if price.get("originalPrice") is not None else None,
            },
        )
        upsert_stat(
            con, date, "epic", gid,
            extra={
                "freeNow": bool(cur),
                "freeUpcoming": bool(upc),
                "promoDetail": (cur[:1] + upc[:1]),
            },
        )
        n += 1
    con.commit()
    log(f"epic 写入完成: {n} 行 (freeNow="
        f"{sum(1 for e in elements if (e.get('promotions') or {}).get('promotionalOffers'))})")


# ================================================================ main =====

TT_XUA = ("V=1&PN=WebAppIntl2&LANG=en_US&VN_CODE=116&LOC=CN&PLT=PC&DS=Android"
          "&UID=c4334cf3-124b-471f-8ea2-126d93d2e229&VN=0.1.0&OS=Linux&OSV=x86_64&DT=PC")


def collect_taptap(con, date):
    """TapTap 国际版（taptap.io 官方，数据中心 IP 可达；中文版 taptap.cn 被封无法访问）。
    通道1: webapiv2/i/library/v1/list 榜单（X-UA 为 query 参数，分页 9/页）
    通道2: /app/{id} SSR 页 ld+json（评分/评分人数/中文简介不适用——内容全英文，无 CJK 只留空）
    """
    import urllib.parse
    import re as _re

    def http_html(url):
        req = urllib.request.Request(url, headers={"User-Agent": UA["User-Agent"]})
        with urllib.request.urlopen(req, timeout=20, context=CTX) as r:
            return r.read().decode("utf-8", "ignore")

    apps = []       # 榜单顺序 [{id, title, icon, devs, tags}]
    from0 = 0
    for _ in range(6):   # 最多 6 页 ≈ 54 款
        u = ("https://www.taptap.io/webapiv2/i/library/v1/list"
             f"?from={from0}&limit=9&platform=android&show=9&show_type=1"
             "&X-UA=" + urllib.parse.quote(TT_XUA, safe=""))
        j = http_json(u)
        lst = ((j or {}).get("data") or {}).get("list") or []
        if not lst:
            break
        for it in lst:
            a = it.get("app") or {}
            if not a.get("id"):
                continue
            apps.append({
                "id": str(a["id"]),
                "title": a.get("title"),
                "icon": ((a.get("icon") or {}).get("url")),
                "devs": [d.get("name") for d in (a.get("developers") or []) if d.get("name")],
                "tags": [t.get("value") for t in (a.get("tags") or []) if t.get("value")],
            })
        if len(lst) < 9:
            break
        from0 += 9
        time.sleep(0.5)
    log(f"taptap 榜单 OK: {len(apps)} 款")
    if not apps:
        return

    # 通道2: SSR ld+json 详情（评分/评分人数/简介/发布日期）— 只对 top20 拉详情
    details = {}
    for a in apps[:20]:
        try:
            html = http_html(f"https://www.taptap.io/app/{a['id']}")
            for m in _re.finditer(
                    r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html, _re.S):
                try:
                    j = json.loads(m.group(1))
                except Exception:
                    continue
                if j.get("@type") != "MobileApplication":
                    continue
                rating = j.get("aggregateRating") or {}
                details[a["id"]] = {
                    "score": rating.get("ratingValue"),
                    "rating_count": rating.get("ratingCount"),
                    "desc_en": (j.get("description") or "").strip()[:600] or None,
                    "released": (j.get("datePublished") or "")[:10] or None,
                    "os": j.get("operatingSystem"),
                }
                break
        except Exception as e:
            log(f"taptap detail {a['id']} FAIL: {type(e).__name__}")
        time.sleep(0.6)
    log(f"taptap 详情 OK: {len(details)}/{min(20, len(apps))}")

    n = 0
    for i, a in enumerate(apps):
        det = details.get(a["id"]) or {}
        upsert_game(
            con, "taptap", a["id"],
            name=a["title"],
            developer="、".join(a["devs"]) if a["devs"] else None,
            genres=a["tags"] or None,
            release_date=det.get("released"),
            desc_cn=None,   # 国际版无中文内容，按铁律留空（cn 版被封锁）
            meta={
                "icon": a["icon"],
                "score": det.get("score"),
                "rating_count": det.get("rating_count"),
                "desc_en": det.get("desc_en"),
                "os": det.get("os"),
                "tt_url": f"https://www.taptap.io/app/{a['id']}",
            },
        )
        upsert_stat(
            con, date, "taptap", a["id"],
            rk=i + 1,
            extra={"score": det.get("score"), "rating_count": det.get("rating_count")},
        )
        n += 1
    con.commit()
    log(f"taptap 写入完成: {n} 行")


# ================================================================ 洞察 =====
# 热门/冷门原因分析信号源（全部官方/权威 API 直接观测）：
# 1. Valve ISteamNews: 官方更新节奏（标题+日期）→ "更新驱动" 证据
# 2. HN Algolia: 全网技术社区提及 → "全网注意力" 证据
# 3. 评论趋势: reviews_total 历史差值 → "评论加速" 证据（appreviews 端点
#    对本服务器 IP 返商店首页 HTML，判死刑；改用累计值日差——仍是真实观测）
# 归因规则不做臆测：每条结论都带证据链（数值+来源+口径）。


def collect_insights(con, date=None, max_games=120):
    """每日洞察采集：当日活跃 Top N 游戏（按 CCU）的三路信号。
    存 games.meta_json.insights 键（键级合并保护其他富化字段）。"""
    date = date or today()
    rows = con.execute(
        """SELECT s.game_id, g.name FROM daily_stats s
           JOIN games g ON g.platform=s.platform AND g.game_id=s.game_id
           WHERE s.platform='steam' AND s.date=?
           ORDER BY (s.ccu + COALESCE(s.peak_in_game,0)) DESC LIMIT ?""",
        (date, max_games)).fetchall()
    log(f"insights: 采集 {len(rows)} 款 Top 活跃游戏的三路信号")
    for i, (aid, name) in enumerate(rows):
        ins = {}
        # 1) 官方更新节奏（Valve ISteamNews，无墙）
        try:
            j = http_json(
                "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
                f"?appid={aid}&count=10&maxlength=0")
            items = ((j or {}).get("appnews") or {}).get("newsitems") or []
            if items:
                latest = items[0]
                age_days = max(0, (time.time() - latest["date"]) / 86400)
                ins["news"] = {
                    "count_10": len(items),
                    "latest_title": latest["title"][:80],
                    "latest_age_days": round(age_days, 1),
                    "recent_30d": sum(
                        1 for it in items if time.time() - it["date"] < 30*86400),
                }
        except Exception:
            pass
        # 2) HN 提及（全网注意力）
        try:
            import urllib.parse as _up
            q = _up.quote(name.split(":")[0].strip()[:40])
            j = http_json(
                f"https://hn.algolia.com/api/v1/search?query={q}"
                f"&tags=story&hitsPerPage=30")
            hits = (j or {}).get("hits") or []
            if hits:
                m30 = sum(1 for h in hits if h.get("created_at_i", 0) > time.time() - 30*86400)
                ins["hn"] = {
                    "total": j.get("nbHits") or len(hits),
                    "recent_30d": m30,
                    "top_points": max((h.get("points") or 0) for h in hits),
                    "top_title": max(
                        hits, key=lambda h: h.get("points") or 0
                    ).get("title", "")[:80],
                }
        except Exception:
            pass
        # 3) 评论趋势（累计值日差——今日首次记录基线，明日起有速度）
        try:
            r = con.execute(
                "SELECT reviews_total FROM daily_stats WHERE platform='steam' "
                "AND game_id=? AND date < ? ORDER BY date DESC LIMIT 1",
                (aid, date)).fetchone()
            cur = con.execute(
                "SELECT reviews_total FROM daily_stats WHERE platform='steam' "
                "AND game_id=? AND date=?", (aid, date)).fetchone()
            if r and r[0] and cur and cur[0]:
                delta = (cur[0] or 0) - (r[0] or 0)
                ins["reviews_velocity"] = {
                    "yesterday": r[0], "today": cur[0],
                    "delta": delta,
                }
        except Exception:
            pass
        if ins:
            mj = con.execute(
                "SELECT meta_json FROM games WHERE platform='steam' AND game_id=?",
                (aid,)).fetchone()
            m = json.loads(mj[0] or "{}") if mj else {}
            m["insights"] = {**(m.get("insights") or {}), **ins,
                             "updated": date}
            con.execute(
                "UPDATE games SET meta_json=? WHERE platform='steam' AND game_id=?",
                (json.dumps(m, ensure_ascii=False), aid))
        if (i + 1) % 20 == 0:
            con.commit()
            log(f"  insights {i+1}/{len(rows)}")
        time.sleep(0.3)
    con.commit()
    log(f"insights 完成: {len(rows)} 款已采集")


def main():
    started = time.time()
    date = today()
    con = ensure_db()
    con.execute("INSERT OR IGNORE INTO snapshots VALUES (?,?)",
                (date, datetime.now().isoformat(timespec="seconds")))
    con.commit()

    for fn in (collect_steam, collect_steam_universe, collect_roblox,
               collect_epic, collect_taptap, collect_insights):
        name = fn.__name__
        try:
            fn(con, date)
        except Exception as e:
            log(f"{name} EXCEPTION: {type(e).__name__}: {e}")
        con.commit()

    cur = con.execute(
        "SELECT platform, COUNT(*) FROM daily_stats WHERE date=? GROUP BY platform", (date,))
    parts = ", ".join(f"{p}={n}" for p, n in cur.fetchall()) or "无数据"
    log(f"== 快照 {date} 完成 [{parts}] 耗时 {time.time() - started:.0}s ==")
    con.close()


if __name__ == "__main__":
    main()


# ============================================================ 并行富化 =====
def enrich_steam_parallel(con, max_games=5000, workers=5, skip_spy=False):
    """多线程并行版全量富化（专用任务用，cron 的串行版保持不变）。
    HTTP 阶段并行（Valve appdetails en+cn + SteamSpy），写库集中串行
    （SQLite 写安全 + 服务器读不受锁）。
    幂等判据与串行版一致：发行日+类型+price_usd 已齐则跳过。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import queue as _q
    date = today()
    try:
        act = {r[0] for r in con.execute(
            "SELECT game_id FROM daily_stats WHERE platform='steam' AND date=?",
            (date,))}
    except Exception:
        act = set()
    rows = con.execute(
        "SELECT game_id, name, release_date, genres, meta_json FROM games "
        "WHERE platform='steam' ORDER BY rowid").fetchall()
    todo = []
    for aid, name, rel, genres, mj in rows:
        m = json.loads(mj or '{}')
        if rel and genres and m.get("price_usd") is not None:
            continue
        todo.append((aid, name, rel, genres, m))
    act_todo = [t for t in todo if t[0] in act]
    cold_todo = [t for t in todo if t[0] not in act]
    todo = (act_todo + cold_todo)[:max_games]
    log(f"parallel enrich: 待补 {len(act_todo)} 活跃 + {len(cold_todo)} 冷门, "
        f"{workers} workers, 本轮限额 {max_games}")
    if not todo:
        return 0
    done = 0
    results = _q.Queue()

    def fetch_one(item):
        aid, name, rel, genres, m = item
        meta = {}
        cn = None
        desc_cn = None
        j = http_json(
            f"https://store.steampowered.com/api/appdetails?appids={aid}&cc=us&l=en")
        dat = ((j or {}).get(aid) or {}).get("data") or {}
        if dat:
            if (dat.get("price_overview") or {}).get("final") is not None:
                meta["price_usd"] = dat["price_overview"]["final"] / 100
            elif dat.get("is_free"):
                meta["price_usd"] = 0
            if (dat.get("recommendations") or {}).get("total") is not None:
                meta["reviews_total_store"] = dat["recommendations"]["total"]
            jc = http_json(
                f"https://store.steampowered.com/api/appdetails?appids={aid}"
                f"&l=schinese&cc=cn")
            datc = ((jc or {}).get(aid) or {}).get("data") or {}
            nm = clean_cn_name(datc.get("name", ""))
            if nm:
                cn = nm
            sd = datc.get("short_description") or ""
            if sd and has_cjk(sd):
                desc_cn = sd.strip()
        if m.get("userscore") is None and not skip_spy:
            spy = http_json(
                f"https://steamspy.com/api.php?request=appdetails&appid={aid}")
            if spy and spy.get("userscore"):
                meta["userscore"] = spy["userscore"]
                meta["positive"] = spy["positive"]
                meta["negative"] = spy["negative"]
        rel_v = ((dat.get("release_date") or {}).get("date")) or rel
        gen_v = [g["description"] for g in (dat.get("genres") or [])] or None
        return (aid, rel_v, gen_v, genres, {**m, **meta}, cn, desc_cn)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(fetch_one, item) for item in todo]
        for fut in as_completed(futs):
            try:
                aid, rel_v, gen_v, genres, m2, cn, desc_cn = fut.result()
            except Exception:
                continue
            try:
                if cn:
                    con.execute(
                        "UPDATE games SET name_cn=? WHERE platform='steam' "
                        "AND game_id=? AND (name_cn IS NULL OR name_cn='')",
                        (cn, aid))
                if desc_cn:
                    con.execute(
                        "UPDATE games SET desc_cn=? WHERE platform='steam' "
                        "AND game_id=?",
                        (desc_cn, aid))
                con.execute(
                    """UPDATE games SET release_date=?, genres=?, meta_json=?
                       WHERE platform='steam' AND game_id=?""",
                    (rel_v,
                     json.dumps(gen_v, ensure_ascii=False) if gen_v else genres,
                     json.dumps(m2, ensure_ascii=False),
                     aid))
                done += 1
                if done % 100 == 0:
                    log(f"  parallel enrich {done}/{len(todo)} ...")
                    con.commit()
            except sqlite3.OperationalError as e:
                log(f"  write skip {aid}: {e}")
    con.commit()
    log(f"parallel enrich 完成: 本轮 {done}/{len(todo)} 款")
    return done


def spy_backfill(con, max_games=100000):
    """SteamSpy userscore 单线程回填（限速 1.15s/次，独立慢速通道）。
    核心字段（发行日/类型/价格/中文名）已由 enrich_steam_parallel 高速补齐，
    本函数只补口碑三件套：userscore/positive/negative。幂等。"""
    rows = con.execute(
        "SELECT game_id, meta_json FROM games WHERE platform='steam' "
        "ORDER BY rowid").fetchall()
    todo = []
    for aid, mj in rows:
        m = json.loads(mj or '{}')
        if m.get("userscore") is None:
            todo.append((aid, m))
    todo = todo[:max_games]
    log(f"spy backfill: 待补 userscore {len(todo)} 款")
    done = 0
    for aid, m in todo:
        spy = http_json(
            f"https://steamspy.com/api.php?request=appdetails&appid={aid}")
        if spy and spy.get("userscore"):
            m2 = {**m, "userscore": spy["userscore"],
                  "positive": spy["positive"], "negative": spy["negative"]}
            con.execute(
                "UPDATE games SET meta_json=? WHERE platform='steam' AND game_id=?",
                (json.dumps(m2, ensure_ascii=False), aid))
        done += 1
        if done % 100 == 0:
            log(f"  spy backfill {done}/{len(todo)} ...")
            con.commit()
        time.sleep(1.15)
    con.commit()
    log(f"spy backfill 完成: 本轮 {done} 款")
    return done
