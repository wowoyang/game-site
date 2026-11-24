#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
游戏趋势分析站 /game — Flask API
所有数字均直接来自 daily_stats/games 表的真实观测值，来源随数据返回标注。
"""
import json
import os
import sqlite3
import time
import urllib.request
from datetime import datetime, timedelta

from flask import Flask, jsonify, request, render_template, send_from_directory, Response

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "games.db")

app = Flask(__name__, static_folder=os.path.join(BASE, "static"),
            template_folder=os.path.join(BASE, "templates"))


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def latest_date(con):
    r = con.execute("SELECT MAX(date) AS d FROM snapshots").fetchone()
    return r["d"] if r else None


def prev_date(con, date, n=1):
    """往前第 n 个快照日"""
    r = con.execute(
        "SELECT DISTINCT date FROM daily_stats WHERE date < ? ORDER BY date DESC "
        "LIMIT 1 OFFSET ?", (date, n - 1)).fetchone()
    return r["date"] if r else None


def jload(s, default=None):
    try:
        return json.loads(s) if s else (default if default is not None else None)
    except Exception:
        return default


# ------------------------------------------------------------------ 首页 ---

@app.route("/")
def index():
    return render_template("index.html")


# -------------------------------------------------------------- 概览 API ---

@app.route("/api/overview")
def api_overview():
    con = db()
    date = latest_date(con)
    if not date:
        return jsonify({"ok": False, "msg": "无快照数据"})
    counts = {p: n for p, n in con.execute(
        "SELECT platform, COUNT(*) FROM daily_stats WHERE date=? GROUP BY platform",
        (date,))}
    hist = con.execute(
        "SELECT date, SUM(CASE WHEN platform='steam' THEN 1 ELSE 0 END) AS steam, "
        "SUM(CASE WHEN platform='roblox' THEN 1 ELSE 0 END) AS roblox, "
        "SUM(CASE WHEN platform='epic' THEN 1 ELSE 0 END) AS epic, "
        "SUM(CASE WHEN platform='taptap' THEN 1 ELSE 0 END) AS taptap "
        "FROM daily_stats GROUP BY date ORDER BY date").fetchall()
    prev = prev_date(con, date)
    con.close()
    return jsonify({
        "ok": True,
        "latest_date": date,
        "prev_date": prev,
        "counts": counts,
        "snapshots": [
            {"date": h["date"], "steam": h["steam"], "roblox": h["roblox"],
             "epic": h["epic"], "taptap": h["taptap"]} for h in hist],
        "sources": {
            "steam": ["Valve官方 ISteamChartsService", "Valve官方 appdetails", "SteamSpy"],
            "roblox": ["Roblox官方 explore-api", "Roblox官方 games-api", "Roblox官方 thumbnails"],
            "epic": ["Epic官方 freeGamesPromotions"],
            "taptap": ["TapTap国际版官方榜 webapiv2 + SSR ld+json"],
        },
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })


# ------------------------------------------------------------ 爆火 API -----

@app.route("/api/new-releases/cal")
def api_new_releases_cal():
    """新发售月历数据：某月逐日新发售计数（真实观测，已收录口径）。
    month=YYYY-MM，默认当月。返回整月天数 + 首日星期（周一=0）。"""
    con = db()
    import datetime as dt
    month = (request.args.get("month") or "").strip()
    try:
        y, m = map(int, month.split("-"))
        first = dt.date(y, m, 1)
    except Exception:
        first = dt.date.today().replace(day=1)
        y, m = first.year, first.month
    nxt = dt.date(y + 1, 1, 1) if m == 12 else dt.date(y, m + 1, 1)
    rows = con.execute(
        "SELECT release_date, COUNT(*) n FROM games WHERE platform='steam' "
        "AND release_date IS NOT NULL GROUP BY release_date").fetchall()
    counts = {}
    for rdate, n in rows:
        try:
            d = dt.datetime.strptime(rdate, "%b %d, %Y").date()
        except ValueError:
            continue
        counts[d.isoformat()] = counts.get(d.isoformat(), 0) + n
    days = []
    d = first
    while d < nxt:
        days.append({"date": d.isoformat(), "count": counts.get(d.isoformat(), 0)})
        d += dt.timedelta(days=1)
    # 有真实发售观测的年份（供年下拉）
    years = [r[0] for r in con.execute(
        "SELECT DISTINCT substr(release_date,-4) y FROM games "
        "WHERE platform='steam' AND release_date IS NOT NULL "
        "AND substr(release_date,-4) GLOB '[12][0-9][0-9][0-9]' ORDER BY y").fetchall()]
    cur_year = str(dt.date.today().year)
    if cur_year not in years:
        years.append(cur_year)
        years.sort()
    con.close()
    return jsonify({"ok": True, "month": f"{y:04d}-{m:02d}",
                    "first_weekday": first.weekday(), "days": days,
                    "month_total": sum(x["count"] for x in days),
                    "years": years})


@app.route("/api/new-releases")
def api_new_releases():
    """Steam 每日新发售（官方搜索接口 Released_DESC，真实发售日观测）。
    days: 回看窗口（默认7，日历热力用 60）；
    date: 精确筛选某一天（YYYY-MM-DD，选填）。
    返回 by_day 按发售日倒序 + calendar（60 天逐日计数，供日历渲染）。"""
    con = db()
    days = int(request.args.get("days", 7) or 7)
    date_filter = (request.args.get("date") or "").strip()
    import datetime as dt
    from collections import defaultdict
    # 日历计数：SQL 聚合全部历史（快）
    cal_rows = con.execute(
        "SELECT release_date, COUNT(*) n FROM games WHERE platform='steam' "
        "AND release_date IS NOT NULL GROUP BY release_date").fetchall()
    counts = {}
    for rdate, n in cal_rows:
        try:
            d = dt.datetime.strptime(rdate, "%b %d, %Y").date()
        except ValueError:
            continue
        counts[d.isoformat()] = counts.get(d.isoformat(), 0) + n
    calendar = [{"date": (dt.date.today() - dt.timedelta(days=i)).isoformat(),
                 "count": counts.get((dt.date.today() - dt.timedelta(days=i)).isoformat(), 0)}
                for i in range(59, -1, -1)]
    # 游戏明细：精确日期 or 窗口（SQL 侧先过滤到目标日集合，避免全表解析）
    def rdate_variants(d):
        # 库中日期来自 Valve/搜索接口，日号可能带/不带前导零（Mar 1 / Mar 01）
        return {d.strftime("%b %d, %Y"), f"{d.strftime('%b')} {d.day}, {d.year}"}
    if date_filter:
        try:
            target = dt.datetime.strptime(date_filter, "%Y-%m-%d").date()
            rdates = sorted(rdate_variants(target))
        except ValueError:
            rdates = []
    else:
        cutoff = dt.date.today() - dt.timedelta(days=days)
        vs = set()
        for i in range(days + 1):
            vs |= rdate_variants(cutoff + dt.timedelta(days=i))
        rdates = sorted(vs)
    rows = []
    if rdates:
        qmarks = ",".join("?" * len(rdates))
        rows = con.execute(
            f"""SELECT game_id, name, name_cn, release_date, developer, genres, meta_json
                FROM games WHERE platform='steam' AND release_date IN ({qmarks})""",
            rdates).fetchall()
    byday = defaultdict(list)
    for r in rows:
        try:
            d = dt.datetime.strptime(r["release_date"], "%b %d, %Y").date()
        except ValueError:
            continue
        meta = jload(r["meta_json"], {}) or {}
        byday[d.isoformat()].append({
            "game_id": r["game_id"], "name": r["name"], "name_cn": r["name_cn"],
            "developer": r["developer"], "genres": jload(r["genres"], []),
            "price_usd": meta.get("price_usd"),
            "reviews_total": meta.get("reviews_total_store"),
            "source": "Steam官方搜索接口",
        })
    # 天列表：精确日期筛选 or 窗口
    if date_filter:
        days_out = [{"date": date_filter,
                     "count": len(byday.get(date_filter, [])),
                     "games": byday.get(date_filter, [])}]
    else:
        cutoff = dt.date.today() - dt.timedelta(days=days)
        sel = [(d, v) for d, v in byday.items() if d >= cutoff.isoformat()]
        days_out = [{"date": d, "count": len(v), "games": v}
                    for d, v in sorted(sel, reverse=True)]
    con.close()
    return jsonify({"ok": True, "days": days, "date": date_filter or None,
                    "calendar": calendar, "by_day": days_out})


@app.route("/api/hot")
def api_hot():
    """近期爆火：真实动量信号（全部为源 API 直接观测，非估算）
    Steam:
      - Valve 官方榜 rank vs last_week_rank（周排名跃升）
      - SteamSpy avg_2weeks vs avg_forever（近期游玩强度高于历史 = 回潮/爆火）
      - 累计评论日差（需 ≥2 天快照）
    Roblox:
      - 官方 Up-and-Coming 榜（Roblox 自己的成长性算法）
      - 官方 Top Trending 榜
      - players 日差（需 ≥2 天快照）
    """
    con = db()
    date = latest_date(con)
    if not date:
        return jsonify({"ok": False, "msg": "无数据"})
    prev = prev_date(con, date)
    out = {"ok": True, "date": date, "prev": prev, "steam": [], "roblox": []}

    # --- Steam：官方榜周跃升 + 近期强度
    rows = con.execute(
        """SELECT s.*, g.name, g.name_cn, g.developer, g.genres, g.tags, g.release_date, g.meta_json
           FROM daily_stats s LEFT JOIN games g
             ON g.platform=s.platform AND g.game_id=s.game_id
           WHERE s.date=? AND s.platform='steam' AND s.last_week_rank IS NOT NULL
           ORDER BY s.rk""", (date,)).fetchall()
    prev_reviews = {}
    if prev:
        for r in con.execute(
                "SELECT game_id, reviews_total FROM daily_stats "
                "WHERE date=? AND platform='steam'", (prev,)):
            if r["reviews_total"] is not None:
                prev_reviews[r["game_id"]] = r["reviews_total"]
    for r in rows:
        jump = (r["last_week_rank"] or 0) - (r["rk"] or 0)  # >0 = 上升
        new_entry = r["last_week_rank"] in (None, 0) and r["rk"] is not None
        meta = jload(r["meta_json"], {}) or {}
        intensity = None
        if r["avg_playtime_forever"] and r["avg_playtime_2w"] is not None \
                and r["avg_playtime_forever"] > 0:
            intensity = round(r["avg_playtime_2w"] / r["avg_playtime_forever"], 2)
        rev_growth = None
        if prev and r["reviews_total"] is not None and \
                r["game_id"] in prev_reviews:
            rev_growth = r["reviews_total"] - prev_reviews[r["game_id"]]
        # 爆火判定：新上榜 / 周跃升≥3 / 近期强度≥1.5 / 日评论增长显著
        hot = new_entry or (jump >= 3) or (intensity or 0) >= 1.5 or \
              (rev_growth or 0) >= 30
        if not hot and r["rk"] and r["rk"] > 50:
            continue
        out["steam"].append({
            "game_id": r["game_id"], "name": r["name"], "name_cn": r["name_cn"],
            "rank": r["rk"], "last_week_rank": r["last_week_rank"],
            "rank_jump": jump if not new_entry else None,
            "new_entry": new_entry,
            "peak_in_game": r["peak_in_game"], "ccu": r["ccu"],
            "intensity_2w_vs_forever": intensity,
            "reviews_total": r["reviews_total"],
            "reviews_growth_1d": rev_growth,
            "genres": jload(r["genres"], []),
            "tags": jload(r["tags"], {}),
            "release_date": r["release_date"],
            "developer": r["developer"],
            "price_usd": meta.get("price_usd"),
            "source": "Valve官方榜" if r["last_week_rank"] is not None else "SteamSpy",
        })
    out["steam"].sort(key=lambda x: (
        -(x["rank_jump"] or 99 if not x["new_entry"] else 999),
        -(x["reviews_growth_1d"] or 0), -(x["intensity_2w_vs_forever"] or 0)))

    # --- Roblox：官方 Up-and-Coming / Trending 榜 + players 日差
    rrows = con.execute(
        """SELECT s.*, g.name, g.name_cn, g.developer, g.genres, g.release_date, g.meta_json
           FROM daily_stats s LEFT JOIN games g
             ON g.platform=s.platform AND g.game_id=s.game_id
           WHERE s.date=? AND s.platform='roblox'""", (date,)).fetchall()
    prev_players = {}
    if prev:
        for r in con.execute(
                "SELECT game_id, players FROM daily_stats "
                "WHERE date=? AND platform='roblox'", (prev,)):
            if r["players"] is not None:
                prev_players[r["game_id"]] = r["players"]
    for r in rrows:
        extra = jload(r["extra_json"], {}) or {}
        sorts = (extra.get("sorts") or {})
        in_up = "upcoming" in sorts
        in_tr = "trending" in sorts
        if not (in_up or in_tr):
            continue
        pchg = None
        if prev and r["players"] is not None and r["game_id"] in prev_players \
                and prev_players[r["game_id"]] > 0:
            pchg = round(
                (r["players"] - prev_players[r["game_id"]]) /
                prev_players[r["game_id"]] * 100, 1)
        meta = jload(r["meta_json"], {}) or {}
        out["roblox"].append({
            "game_id": r["game_id"], "name": r["name"], "name_cn": r["name_cn"],
            "players": r["players"], "players_chg_1d_pct": pchg,
            "visits": r["visits"], "favorites": r["favorites"],
            "up_votes": meta.get("upVotes"), "down_votes": meta.get("downVotes"),
            "genres": jload(r["genres"], []),
            "release_date": r["release_date"], "developer": r["developer"],
            "icon": meta.get("icon"),
            "sort_ranks": sorts,
            "source": "Roblox官方榜",
        })
    out["roblox"].sort(key=lambda x: (
        0 if "upcoming" in x["sort_ranks"] else 1,
        x["sort_ranks"].get("upcoming", x["sort_ranks"].get("trending", 999))))
    con.close()
    return jsonify(out)


# ------------------------------------------------------------ 热门 API -----

@app.route("/api/popular")
def api_popular():
    """饱受欢迎：当下在线/游玩人数排行（真实观测）"""
    con = db()
    date = latest_date(con)
    if not date:
        return jsonify({"ok": False, "msg": "无数据"})
    prev = prev_date(con, date)
    out = {"ok": True, "date": date, "prev": prev, "steam": [], "roblox": []}

    rows = con.execute(
        """SELECT s.*, g.name, g.name_cn, g.developer, g.genres, g.tags, g.release_date, g.meta_json
           FROM daily_stats s JOIN games g
             ON g.platform=s.platform AND g.game_id=s.game_id
           WHERE s.date=? AND s.platform='steam'
           ORDER BY COALESCE(s.peak_in_game, s.ccu) DESC LIMIT 50""", (date,)).fetchall()
    prev_ccu = {}
    if prev:
        for r in con.execute("SELECT game_id, peak_in_game, ccu FROM daily_stats "
                             "WHERE date=? AND platform='steam'", (prev,)):
            v = r["peak_in_game"] or r["ccu"]
            if v:
                prev_ccu[r["game_id"]] = v
    for r in rows:
        meta = jload(r["meta_json"], {}) or {}
        cur = r["peak_in_game"] or r["ccu"]
        wchg = None
        if r["game_id"] in prev_ccu and prev_ccu[r["game_id"]]:
            # 峰值在线日环比（同为官方榜观测）
            wchg = round((cur - prev_ccu[r["game_id"]]) /
                         prev_ccu[r["game_id"]] * 100, 1) if cur else None
        out["steam"].append({
            "game_id": r["game_id"], "name": r["name"], "name_cn": r["name_cn"],
            "rank": r["rk"], "peak_in_game": r["peak_in_game"], "ccu": r["ccu"],
            "ccu_chg_1d_pct": wchg,
            "owners": r["owners"],
            "avg_playtime_2w": r["avg_playtime_2w"],
            "avg_playtime_forever": r["avg_playtime_forever"],
            "reviews_total": r["reviews_total"],
            "userscore": meta.get("userscore"),
            "genres": jload(r["genres"], []),
            "tags": jload(r["tags"], {}),
            "release_date": r["release_date"], "developer": r["developer"],
            "price_usd": meta.get("price_usd"),
            "source": "Valve官方榜+SteamSpy",
        })

    rrows = con.execute(
        """SELECT s.*, g.name, g.name_cn, g.developer, g.genres, g.release_date, g.meta_json
           FROM daily_stats s JOIN games g
             ON g.platform=s.platform AND g.game_id=s.game_id
           WHERE s.date=? AND s.platform='roblox'
           ORDER BY COALESCE(s.players, 0) DESC LIMIT 50""", (date,)).fetchall()
    prev_pl = {}
    if prev:
        for r in con.execute("SELECT game_id, players FROM daily_stats "
                             "WHERE date=? AND platform='roblox'", (prev,)):
            if r["players"]:
                prev_pl[r["game_id"]] = r["players"]
    for r in rrows:
        extra = jload(r["extra_json"], {}) or {}
        meta = jload(r["meta_json"], {}) or {}
        pchg = None
        if prev and r["players"] and r["game_id"] in prev_pl:
            pchg = round((r["players"] - prev_pl[r["game_id"]]) /
                         prev_pl[r["game_id"]] * 100, 1)
        out["roblox"].append({
            "game_id": r["game_id"], "name": r["name"], "name_cn": r["name_cn"],
            "players": r["players"], "players_chg_1d_pct": pchg,
            "visits": r["visits"], "favorites": r["favorites"],
            "up_votes": meta.get("upVotes"), "down_votes": meta.get("downVotes"),
            "genres": jload(r["genres"], []),
            "release_date": r["release_date"], "developer": r["developer"],
            "icon": meta.get("icon"),
            "sort_ranks": extra.get("sorts") or {},
            "source": "Roblox官方",
        })

    # --- TapTap：官方 Top Charts 榜（国际版，评分/评分人数为真实观测）
    trows = con.execute(
        """SELECT s.*, g.name, g.name_cn, g.developer, g.genres, g.release_date, g.meta_json
           FROM daily_stats s JOIN games g
             ON g.platform=s.platform AND g.game_id=s.game_id
           WHERE s.date=? AND s.platform='taptap'
           ORDER BY s.rk LIMIT 50""", (date,)).fetchall()
    out["taptap"] = []
    for r in trows:
        meta = jload(r["meta_json"], {}) or {}
        extra = jload(r["extra_json"], {}) or {}
        out["taptap"].append({
            "game_id": r["game_id"], "name": r["name"], "name_cn": r["name_cn"],
            "rank": r["rk"], "score": extra.get("score") or meta.get("score"),
            "rating_count": extra.get("rating_count") or meta.get("rating_count"),
            "genres": jload(r["genres"], []),
            "release_date": r["release_date"], "developer": r["developer"],
            "icon": meta.get("icon"), "os": meta.get("os"),
            "tt_url": meta.get("tt_url"),
            "desc_en": meta.get("desc_en"),
            "source": "TapTap国际版官方榜",
        })
    con.close()
    return jsonify(out)


@app.route("/api/wx-minigame/rank")
def api_wx_minigame_rank():
    """微信小游戏畅销榜 Top100（GameLook 月度文章 OCR，制榜=引力引擎/微信官方日榜积分）。"""
    con = db()
    months = [r[0] for r in con.execute(
        "SELECT DISTINCT month FROM wx_minigame_rank ORDER BY month DESC").fetchall()]
    rows = [dict(r) for r in con.execute(
        """SELECT month, rank, game_name, publisher, cat1, cat2, change, needs_review, article_url
           FROM wx_minigame_rank ORDER BY month DESC, rank ASC""").fetchall()]
    con.close()
    return jsonify({"ok": True, "months": months, "rows": rows,
                    "note": "数据源=GameLook月度《微信小游戏畅销榜Top100》文章(制榜:引力引擎,基于微信官方每日"
                            "畅销榜名次积分); 榜单以图片发布, OCR识别入库, needs_review=1为OCR低置信行待人工复核"})

@app.route("/steam")
def steam_research_page():
    return render_template("steam.html")


@app.route("/wximg")
def wximg_proxy():
    """GameLook 图床有防盗链（无 Referer 403）→ 服务器代理转发，附正确 Referer。"""
    u = request.args.get("u", "")
    if not u.startswith(("http://img.gamelook.com.cn/", "https://img.gamelook.com.cn/")):
        return jsonify({"ok": False}), 400
    req = urllib.request.Request(u, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0",
        "Referer": "http://www.gamelook.com.cn/"})
    try:
        r = urllib.request.urlopen(req, timeout=15)
        data = r.read()
        return Response(data, mimetype=r.headers.get("Content-Type", "image/jpeg"),
                        headers={"Cache-Control": "public, max-age=86400"})
    except Exception:
        return jsonify({"ok": False}), 502


@app.route("/api/wx-minigame/game")
def api_wx_minigame_game():
    """单款游戏详情：跨期名次轨迹 + 相关 GameLook 文章（含截图/摘要）+ 常青/爆款标签。"""
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"ok": False, "err": "name required"}), 400
    con = db()
    traj = [dict(rank=r[0], month=r[1]) for r in con.execute(
        """SELECT rank, month FROM wx_minigame_rank WHERE game_name=? ORDER BY month""",
        (name,)).fetchall()]
    base = con.execute(
        """SELECT publisher, cat1, cat2 FROM wx_minigame_rank WHERE game_name=? LIMIT 1""",
        (name,)).fetchone()
    arts = [dict(r) for r in con.execute(
        """SELECT url, title, pub_date, snippet, images_json FROM wx_game_articles
           WHERE game_name=? ORDER BY pub_date DESC LIMIT 4""", (name,)).fetchall()]
    for a in arts:
        try:
            a["images"] = json.loads(a.pop("images_json") or "[]")
        except Exception:
            a["images"] = []
    con.close()
    best = min((t["rank"] for t in traj), default=None)
    # 爆款判定（真实数据规则，页内标注）
    tags = []
    if traj:
        if best and best <= 3:
            tags.append("登顶款")
        if len(traj) >= 4:
            tags.append("常青款")
        if len(traj) == 1 and best and best <= 20:
            tags.append("空降爆款")
    return jsonify({"ok": True, "name": name,
                    "publisher": base[0] if base else "", "cat1": base[1] if base else "",
                    "cat2": base[2] if base else "",
                    "traj": traj, "best": best, "articles": arts, "tags": tags})


@app.route("/wxgame")
def wxgame_research_page():
    return render_template("wxgame.html")


# Steam 研究排序键白名单
# 3A 口径：publisher 命中公认大厂发行商（含子公司，LIKE 匹配）。
# 名单为行业常识公开事实（发行商主体名称），非估算数据——口径透明标注。
AAA_PUBLISHERS = [
    "Electronic Arts", "EA Sports", "Activision", "Blizzard", "Ubisoft",
    "Take-Two", "Rockstar Games", "2K", "Private Division",
    "Sony Interactive", "PlayStation", "Warner Bros", "WB Games",
    "Square Enix", "Capcom", "Bandai Namco", "Konami", "Sega", "Atlus",
    "Devolver", "Bethesda", "Microsoft", "Xbox Game Studios", "THQ Nordic",
    "Deep Silver", "Focus Entertainment", "Paradox Interactive",
    "Paradox Arc", "Frontier", "505 Games", "Team17", "NIS America",
    "Koei Tecmo", "SNK", "Marvelous", "Spike Chunsoft", "Arc System Works",
    "Nacon", "Microids", "Aspyr", "Saber Interactive", "Embracer",
    "PLAION", "Raw Fury", "Annapurna Interactive", "Humble Games",
    "Netflix Games", "Tencent", "Level Infinite", "NetEase",
]

SR_SORTS = {
    "peak_in_game": "s.peak_in_game",
    "ccu": "s.ccu",
    "reviews_total": "s.reviews_total",
    "avg_playtime_2w": "s.avg_playtime_2w",
    "avg_playtime_forever": "s.avg_playtime_forever",
    "rank": "s.rk",
    "name": "g.name",
    "price_usd": "CAST(json_extract(g.meta_json,'$.price_usd') AS REAL)",
    "userscore": "CAST(json_extract(g.meta_json,'$.userscore') AS REAL)",
    "positive": "CAST(json_extract(g.meta_json,'$.positive') AS INTEGER)",
}


@app.route("/api/steam-universe")
def api_steam_universe():
    """全量 Steam 研究查询：games 表全量宇宙 + 当日活跃观测。
    服务端过滤+排序+分页（87K 行不下发浏览器）。
    支持 q/price/score/year/tag/genre/dev/active/ranked 过滤。"""
    con = db()
    date = latest_date(con)
    if not date:
        con.close()
        return jsonify({"ok": False, "msg": "无快照数据"})
    q = (request.args.get("q") or "").strip()
    where = ["g.platform='steam'", "g.name IS NOT NULL", "g.name != ''"]
    params = [date]
    if request.args.get("active") == "1":
        where.append("s.game_id IS NOT NULL")
    if request.args.get("ranked") == "1":
        where.append("s.rk IS NOT NULL")
    # ── 研究范围口径（透明标注，全部真实观测字段）──
    # indie: Valve 官方 genres 自标 "Indie"（开发者自选分类）
    # aaa: publisher 命中公认大厂发行商（行业常识名单，LIKE 容错子公司）
    scope = request.args.get("scope")
    if scope == "indie":
        where.append("g.genres LIKE '%\"Indie\"%'")
    elif scope == "aaa":
        conds = []
        for major in AAA_PUBLISHERS:
            conds.append("g.publisher LIKE ?")
            params.append(f"%{major}%")
        where.append("(" + " OR ".join(conds) + ")")
    if q:
        where.append("(g.name LIKE ? OR g.name_cn LIKE ? OR g.developer LIKE ?)")
        params += [f"%{q}%"] * 3
    price = request.args.get("price")
    pj = "CAST(json_extract(g.meta_json,'$.price_usd') AS REAL)"
    if price == "free":
        where.append(f"{pj}=0")
    elif price == "paid":
        where.append(f"{pj}>0")
    elif price == "p20":
        where.append(f"{pj}>0 AND {pj}<=20")
    elif price == "p40":
        where.append(f"{pj}>20 AND {pj}<=40")
    elif price == "p40+":
        where.append(f"{pj}>40")
    score = request.args.get("score")
    if score:
        where.append("CAST(json_extract(g.meta_json,'$.userscore') AS REAL) >= ?")
        params.append(float(score))
    year = request.args.get("year")
    if year:
        where.append("substr(g.release_date, -4) = ?")
        params.append(year)
    tag = request.args.get("tag")
    if tag:
        tags = [t for t in tag.split(",") if t]
        if len(tags) == 1:
            where.append("g.tags LIKE ?")
            params.append(f'%"{tag}"%')
        elif tags:
            where.append("(" + " OR ".join(
                ["g.tags LIKE ?"] * len(tags)) + ")")
            params += [f'%"{t}"%' for t in tags]
    genre = request.args.get("genre")
    if genre:
        gs = [t for t in genre.split(",") if t]
        if len(gs) == 1:
            where.append("g.genres LIKE ?")
            params.append(f'%"{genre}"%')
        elif gs:
            where.append("(" + " OR ".join(
                ["g.genres LIKE ?"] * len(gs)) + ")")
            params += [f'%"{g}"%' for g in gs]
    dev = request.args.get("dev")
    if dev:
        ds = [t for t in dev.split(",") if t]
        where.append("g.developer IN (%s)" % ",".join(
            "?" * len(ds)))
        params += ds
    W = " AND ".join(where)
    total = con.execute(
        f"""SELECT COUNT(*) FROM games g
            LEFT JOIN daily_stats s
              ON s.platform=g.platform AND s.game_id=g.game_id AND s.date=?
            WHERE {W}""", params).fetchone()[0]
    sk = request.args.get("sort") or "peak_in_game"
    sort_col = SR_SORTS.get(sk, "s.peak_in_game")
    sdir = "ASC" if request.args.get("dir") == "asc" else "DESC"
    order = f"({sort_col} IS NULL), {sort_col} {sdir}"
    if sk == "name":
        order = f"g.name COLLATE NOCASE {sdir}"
    try:
        page = max(0, int(request.args.get("page", "0")))
    except ValueError:
        page = 0
    try:
        size = min(100, max(10, int(request.args.get("size", "50"))))
    except ValueError:
        size = 50
    rows = con.execute(
        f"""SELECT g.game_id, g.name, g.name_cn, g.developer, g.publisher,
                   g.genres, g.tags, g.release_date, g.meta_json, g.desc_cn,
                   s.rk, s.peak_in_game, s.last_week_rank, s.ccu,
                   s.avg_playtime_2w, s.avg_playtime_forever, s.owners,
                   s.reviews_total
            FROM games g
            LEFT JOIN daily_stats s
              ON s.platform=g.platform AND s.game_id=g.game_id AND s.date=?
            WHERE {W}
            ORDER BY {order}
            LIMIT ? OFFSET ?""",
        params + [size, page * size]).fetchall()
    games = []
    for r in rows:
        m = jload(r["meta_json"], {}) or {}
        tags = jload(r["tags"], {}) or {}
        rev = r["reviews_total"] or m.get("reviews_total_store")
        owners_num = parse_owners(r["owners"])
        games.append({
            "game_id": r["game_id"], "name": r["name"], "name_cn": r["name_cn"],
            "developer": r["developer"], "publisher": r["publisher"],
            "genres": jload(r["genres"], []) or [],
            "tag_list": sorted(tags, key=tags.get, reverse=True)[:8],
            "release_date": r["release_date"], "desc_cn": r["desc_cn"],
            "rank": r["rk"], "peak_in_game": r["peak_in_game"],
            "last_week_rank": r["last_week_rank"], "ccu": r["ccu"],
            "avg_playtime_2w": r["avg_playtime_2w"],
            "avg_playtime_forever": r["avg_playtime_forever"],
            "owners": r["owners"], "owners_num": owners_num,
            "price_usd": m.get("price_usd"), "userscore": m.get("userscore"),
            "positive": m.get("positive"), "negative": m.get("negative"),
            "reviews_total": rev,
            "intensity": round(r["avg_playtime_2w"] / r["avg_playtime_forever"], 2)
            if (r["avg_playtime_2w"] and r["avg_playtime_forever"]) else None,
            "rank_change": (r["last_week_rank"] - r["rk"])
            if (r["rk"] and r["last_week_rank"]) else None,
        })
    con.close()
    return jsonify({"ok": True, "date": date, "total": total,
                    "page": page, "size": size, "games": games})


@app.route("/api/steam-why")
def api_steam_why():
    """热门/冷门原因分析（证据链式归因，无臆测）。
    信号源（全部直接观测）：
    - rank/rank_change: Valve 官方榜名次与周变化（官方 API）
    - ccu/peak: 当前/峰值在线（官方+SteamSpy）
    - userscore/positive/negative: 好评率与口碑（SteamSpy）
    - news: 官方更新节奏 30 天内更新次数（Valve ISteamNews）
    - hn: HN 全网提及（近 30 天数/总分）
    - reviews_velocity: 评论累计数日差（自建历史差值）
    - intensity: 两周均时÷历史均时（SteamSpy 真实观测衍生比率）
    - discount: 当前折扣（SteamSpy）
    - release_date: 发行年龄（appdetails）
    规则：每条归因输出 {factor, direction(+/−), evidence, source}。"""
    con = db()
    gid = request.args.get("id")
    if not gid:
        con.close()
        return jsonify({"ok": False, "msg": "缺 id"})
    g = con.execute(
        """SELECT g.name, g.name_cn, g.developer, g.release_date, g.genres,
                  g.meta_json, s.rk, s.last_week_rank, s.ccu, s.peak_in_game,
                  s.owners, s.reviews_total
           FROM games g LEFT JOIN daily_stats s
             ON s.platform=g.platform AND s.game_id=g.game_id AND s.date=?
           WHERE g.platform='steam' AND g.game_id=?""",
        (latest_date(con), gid)).fetchone()
    if not g:
        con.close()
        return jsonify({"ok": False, "msg": "游戏不存在"})
    (name, name_cn, dev, rel, genres, mj, rank, lwr,
     ccu, peak, owners, reviews) = g
    rank_chg = (lwr - rank) if (rank and lwr) else None  # 正=上升
    m = json.loads(mj or "{}")
    ins = m.get("insights") or {}
    why = []
    # ── 热度层（官方榜+在线数）──
    if rank:
        why.append({"factor": "官方榜名次", "direction": "+",
                    "evidence": f"Valve 最热榜 #{rank}"
                    + (f"，周变化 {rank_chg:+d} 位" if rank_chg else ""),
                    "source": "Valve ISteamChartsService"})
    if peak:
        why.append({"factor": "玩家规模", "direction": "+",
                    "evidence": f"峰值在线 {peak:,}",
                    "source": "Valve 官方榜"})
    if ccu:
        why.append({"factor": "当前热度", "direction": "+",
                    "evidence": f"当前在线 {ccu:,}",
                    "source": "SteamSpy 实时"})
    # ── 口碑层 ──
    us = m.get("userscore")
    pos, neg = m.get("positive"), m.get("negative")
    if us is not None:
        d = "+" if us >= 70 else ("−" if us < 40 else "0")
        ev = f"好评率 {us}%"
        if pos is not None and neg is not None:
            ev += f"（👍{pos:,} / 👎{neg:,}）"
        why.append({"factor": "口碑", "direction": d, "evidence": ev,
                    "source": "SteamSpy"})
    # ── 驱动层（更新/全网注意力）──
    news = ins.get("news")
    if news:
        d = "+" if news.get("recent_30d", 0) >= 2 else "0"
        why.append({"factor": "官方更新节奏", "direction": d,
                    "evidence": f"近 30 天 {news.get('recent_30d', 0)} 次更新"
                    f"（最新「{news.get('latest_title')}」"
                    f"{news.get('latest_age_days')} 天前）",
                    "source": "Valve ISteamNews"})
    hn = ins.get("hn")
    if hn:
        d = "+" if hn.get("recent_30d", 0) >= 1 else "0"
        why.append({"factor": "全网注意力", "direction": d,
                    "evidence": f"HN 累计 {hn.get('total')} 次提及"
                    f"，近 30 天 {hn.get('recent_30d', 0)} 次"
                    f"（最高分 {hn.get('top_points')}）",
                    "source": "Hacker News Algolia"})
    rv = ins.get("reviews_velocity")
    if rv and rv.get("delta"):
        d = "+" if rv["delta"] > 0 else "−"
        why.append({"factor": "评论增速", "direction": d,
                    "evidence": f"评论数 {rv['yesterday']:,} → {rv['today']:,}"
                    f"（日 +{rv['delta']:,}）",
                    "source": "自建 daily_stats 历史差值"})
    # ── 衍生信号 ──
    s2w = m.get("avg_2w")
    if peak and rank_chg:
        if rank_chg >= 20:
            why.append({"factor": "跃升信号", "direction": "+",
                        "evidence": f"官方榜周跃升 {rank_chg} 位",
                        "source": "Valve 官方榜 last_week_rank"})
        elif rank_chg <= -20:
            why.append({"factor": "回落信号", "direction": "−",
                        "evidence": f"官方榜周下跌 {abs(rank_chg)} 位",
                        "source": "Valve 官方榜 last_week_rank"})
    if m.get("discount") and str(m["discount"]) not in ("0", "None"):
        why.append({"factor": "促销驱动", "direction": "+",
                    "evidence": f"当前 -{m['discount']}%",
                    "source": "SteamSpy"})
    if rel:
        import re as _re
        ym = _re.search(r"\d{4}", str(rel))
        if ym and reviews:
            yrs = max(0.1, (time.time() - time.mktime(
                time.strptime(ym.group(0), "%Y"))) / 3.156e7)
            per_yr = reviews / yrs
            if per_yr > 200000:
                why.append({"factor": "长青热度", "direction": "+",
                            "evidence": f"年均评论 {per_yr/10000:.0f} 万条"
                            f"（{ym.group(0)} 年发行至今）",
                            "source": "发行日÷累计评论（衍生比率）"})
    con.close()
    return jsonify({"ok": True, "game": name, "name_cn": name_cn,
                    "factors": why,
                    "note": "全部因子来自官方/权威 API 直接观测的数值，"
                            "归因方向按阈值判定，无主观臆测"})


# ------------------------------------------------ 微信小游戏 IAA -----

WX_AD_SPECS = [
    {"slot": "激励视频广告", "code": "createRewardedVideoAd",
     "desc": "玩家主动观看完整视频获得游戏奖励（复活/道具/金币）",
     "specs": "单用户曝光频控；奖励回调服务端校验",
     "ecpm": "IAA 小游戏核心收益位（官方多次公开课强调为主力变现位）",
     "doc": "https://developers.weixin.qq.com/minigame/dev/guide/open-ability/ad/rewarded-video-ad.html"},
    {"slot": "Banner 广告", "code": "createBannerAd",
     "desc": "底部/顶部横幅常驻",
     "specs": "宽度 300~屏宽等比缩放；自动刷新间隔 ≥30 秒（官方文档实测）",
     "ecpm": "低价高频，作为激励视频补充",
     "doc": "https://developers.weixin.qq.com/minigame/dev/guide/open-ability/ad/banner-ad.html"},
    {"slot": "插屏广告", "code": "createInterstitialAd",
     "desc": "场景切换/关卡结束弹出",
     "specs": "官方推荐在自然停顿处触发",
     "ecpm": "中段价格，频次需克制",
     "doc": "https://developers.weixin.qq.com/minigame/dev/guide/open-ability/ad/"},
    {"slot": "原生模板广告", "code": "createCustomAd",
     "desc": "融入 UI 风格的自定义样式",
     "specs": "官方模板渲染",
     "ecpm": "体验与收益平衡位",
     "doc": "https://developers.weixin.qq.com/minigame/dev/guide/open-ability/ad/"},
    {"slot": "视频贴片广告", "code": "createGridAd 等",
     "desc": "视频内容前/中插入",
     "specs": "依托视频能力",
     "ecpm": "—",
     "doc": "https://developers.weixin.qq.com/minigame/dev/guide/open-ability/ad/"},
    {"slot": "IAA 预测组件", "code": "官方组件",
     "desc": "买量前预测 LTV/回本周期",
     "specs": "官方商业化工具箱 Pro 组成（2026-01 上线，官方资讯实测）",
     "ecpm": "工具非广告位",
     "doc": "https://ad.weixin.qq.com/"},
    {"slot": "直玩广告", "code": "官方投放形态",
     "desc": "无需素材、广告即试玩、直达小游戏",
     "specs": "2026-04 上线（官方资讯实测）",
     "ecpm": "买量侧形态，降低 IAA 获客成本",
     "doc": "https://ad.weixin.qq.com/"},
]


@app.route("/api/wx-minigame")
def api_wx_minigame():
    """微信小游戏 IAA 研究数据。
    news: 官方资讯时间线（微信广告 news_v2 真实观测入库）；
    specs: 官方文档广告位规格（附文档链接可溯源）。"""
    con = db()
    news = [dict(r) for r in con.execute(
        """SELECT page_id, title, category, classify, scene, pub_date, url, summary, related_game
           FROM wx_minigame_news ORDER BY pub_date DESC""").fetchall()]
    con.close()
    return jsonify({"ok": True, "news": news, "specs": WX_AD_SPECS,
                    "note": "资讯源=微信广告官方资讯流(designapi news_v2, 公开接口, 只收IAA相关); "
                            "规格源=微信开放文档(minigame dev guide); "
                            "榜单类(阿拉丁/七麦)出口不可达, 不做展示, 宁缺毋假"})


@app.route("/api/steam-events")
def api_steam_events():
    """Steam 本年度事件表（官方公告流真实观测）。
    kind: seasonal大促 / nextfest新品节 / themed主题节 / awards大奖。
    announced_date=公告发布日；end_date=官方正文提取的结束日（可空）。"""
    con = db()
    year = request.args.get("year") or str(datetime.now().year)
    rows = con.execute(
        """SELECT event_id, kind, title, announced_date, end_date, url
           FROM steam_events WHERE announced_date LIKE ?
           ORDER BY announced_date DESC""", (year + "%",)).fetchall()
    con.close()
    kinds = {"seasonal": "季末大促", "nextfest": "新品节", "themed": "主题节", "awards": "Steam 大奖"}
    return jsonify({"ok": True, "year": year, "events": [
        {"event_id": r["event_id"], "kind": r["kind"], "kind_cn": kinds.get(r["kind"], r["kind"]),
         "title": r["title"], "announced_date": r["announced_date"],
         "end_date": r["end_date"], "url": r["url"]} for r in rows]})


@app.route("/api/steam-relations")
def api_steam_relations():
    con = db()
    try:
        top = min(200, max(5, int(request.args.get("top", "60"))))
    except ValueError:
        top = 60
    # 核心发行商（按合作款数）
    pubs = con.execute(
        """SELECT publisher, COUNT(*) n FROM games
           WHERE platform='steam' AND developer IS NOT NULL
             AND publisher IS NOT NULL AND developer != publisher
           GROUP BY publisher ORDER BY n DESC LIMIT ?""", (top,)).fetchall()
    pub_names = [p[0] for p in pubs]
    if not pub_names:
        con.close()
        return jsonify({"ok": False, "msg": "无合作关系数据"})
    ph = ",".join("?" * len(pub_names))
    edges = con.execute(
        f"""SELECT developer, publisher, COUNT(*) n FROM games
            WHERE platform='steam' AND developer != publisher
              AND developer IS NOT NULL AND publisher IS NOT NULL
              AND publisher IN ({ph})
            GROUP BY developer, publisher ORDER BY n DESC LIMIT 800""",
        pub_names).fetchall()
    devs = {}
    for d, p, n in edges:
        devs[d] = devs.get(d, 0) + n
    nodes, seen = [], set()
    # 发行商节点（大小=合作款数）
    for p, n in pubs:
        nodes.append({"name": p, "type": "pub", "value": n,
                      "own": con.execute(
                          "SELECT COUNT(*) FROM games WHERE platform='steam' "
                          "AND publisher=? AND developer=publisher",
                          (p,)).fetchone()[0]})
        seen.add(p)
    # 开发商节点（大小=与核心发行商合作款数）
    for d, n in sorted(devs.items(), key=lambda x: -x[1])[:400]:
        if d not in seen:
            nodes.append({"name": d, "type": "dev", "value": n})
            seen.add(d)
    con.close()
    return jsonify({
        "ok": True,
        "nodes": nodes,
        "edges": [{"source": d, "target": p, "value": n}
                  for d, p, n in edges if d in seen and p in seen],
        "note": "节点=厂商(大小:发行商=合作款数/开发商=合作款数) 边宽=合作游戏数 "
                "来源: SteamSpy /all developer×publisher 直接观测",
    })


@app.route("/api/steam-facets")
def api_steam_facets():
    """研究页筛选面板聚合（全量口径）：宇宙规模/活跃数/年份/类型/标签/厂商。"""
    con = db()
    date = latest_date(con)
    if not date:
        con.close()
        return jsonify({"ok": False, "msg": "无快照数据"})
    act_sql = "SELECT DISTINCT game_id FROM daily_stats WHERE platform='steam' AND date=?"
    n_all = con.execute(
        "SELECT COUNT(*) FROM games WHERE platform='steam'").fetchone()[0]
    n_active = con.execute(
        f"SELECT COUNT(*) FROM games WHERE platform='steam' AND game_id IN ({act_sql})",
        (date,)).fetchone()[0]
    years = con.execute(
        """SELECT substr(g.release_date, -4) y, COUNT(*) n
           FROM games g WHERE g.platform='steam'
             AND g.release_date GLOB '*[0-9][0-9][0-9][0-9]'
           GROUP BY y ORDER BY y DESC""").fetchall()
    genres = con.execute(
        """SELECT j.value g, COUNT(*) n FROM games g, json_each(g.genres) j
           WHERE g.platform='steam' AND g.genres IS NOT NULL
           GROUP BY j.value ORDER BY n DESC LIMIT 24""").fetchall()
    tags = con.execute(
        f"""SELECT j.value t, COUNT(*) n
            FROM games g, json_each(g.tags) j
            WHERE g.platform='steam' AND g.tags IS NOT NULL
              AND g.game_id IN ({act_sql})
            GROUP BY j.value ORDER BY n DESC LIMIT 24""",
        (date,)).fetchall()
    devs = con.execute(
        f"""SELECT developer, COUNT(*) n FROM games
            WHERE platform='steam' AND developer IS NOT NULL
              AND game_id IN ({act_sql})
            GROUP BY developer ORDER BY n DESC LIMIT 20""",
        (date,)).fetchall()
    out = {
        "ok": True, "date": date,
        "universe": n_all, "active": n_active,
        "years": [{"v": r[0], "n": r[1]} for r in years],
        "genres": [{"v": r[0], "n": r[1]} for r in genres],
        "tags": [{"v": q[0], "n": q[1]} for q in tags],
        "devs": [{"v": r[0], "n": r[1]} for r in devs],
        "scopes": {
            "indie": {"n": con.execute(
                "SELECT COUNT(*) FROM games WHERE platform='steam' "
                "AND genres LIKE '%\"Indie\"%'").fetchone()[0],
                      "note": "Valve 官方 genres 自标 Indie"},
            "aaa": {"n": con.execute(
                "SELECT COUNT(*) FROM games WHERE platform='steam' AND ("
                + " OR ".join("publisher LIKE ?" for _ in AAA_PUBLISHERS)
                + ")", [f"%{p}%" for p in AAA_PUBLISHERS]).fetchone()[0],
                    "note": "publisher 命中公认大厂名单（含子公司）"},
        },
    }
    con.close()
    return jsonify(out)


def parse_owners(s):
    """'20,000,000 .. 30,000,000' → 25000000（区间中值）"""
    if not s:
        return None
    try:
        nums = [int(x.replace(",", "")) for x in s.split("..")]
        return sum(nums) // len(nums)
    except Exception:
        return None


def age_days(rel, date):
    """发行日至快照日天数（release_date 格式多样，尽力解析）"""
    if not rel:
        return None
    from datetime import datetime
    for fmt in ("%d %b, %Y", "%Y-%m-%d", "%b %d, %Y", "%d/%m/%Y"):
        try:
            d0 = datetime.strptime(rel.strip(), fmt)
            d1 = datetime.strptime(date, "%Y-%m-%d")
            return (d1 - d0).days
        except Exception:
            continue
    return None


# ------------------------------------------------------------ 玩法 API -----

@app.route("/api/tags")
def api_tags():
    """玩法关联分析：
    1. 玩法热度榜 —— 标签在榜上游戏中的加权出现频次（权重=在线人数，log 压缩）
    2. 玩法关联 —— 标签共现矩阵（同游戏同现的标签对）
    3. 玩法趋势 —— 标签覆盖人数的快照间变化（需历史积累）
    """
    con = db()
    date = latest_date(con)
    if not date:
        return jsonify({"ok": False, "msg": "无数据"})
    platform = request.args.get("platform", "steam")
    prev = prev_date(con, date)

    if platform == "roblox":
        # Roblox 用 genreL1（官方玩法分类）
        rows = con.execute(
            """SELECT s.players, s.visits, s.favorites, g.genres, g.name, g.name_cn,
                      g.game_id, g.meta_json
               FROM daily_stats s JOIN games g ON g.platform=s.platform
                 AND g.game_id=s.game_id
               WHERE s.date=? AND s.platform='roblox'""", (date,)).fetchall()
        tag_games = {}
        for r in rows:
            for tg in (jload(r["genres"], []) or []):
                tag_games.setdefault(tg, []).append({
                    "name": r["name"], "name_cn": r["name_cn"],
                    "game_id": r["game_id"],
                    "players": r["players"], "visits": r["visits"],
                    "favorites": r["favorites"]})
        tags = []
        for tg, gs in tag_games.items():
            total_pl = sum(g["players"] or 0 for g in gs)
            tags.append({
                "tag": tg, "tag_cn": tg, "games": len(gs), "total_players": total_pl,
                "top_games": sorted(gs, key=lambda x: -(x["players"] or 0))[:6],
                "source": "Roblox官方 genreL1",
            })
        tags.sort(key=lambda x: -x["total_players"])
        out = {"ok": True, "date": date, "platform": "roblox",
               "tags": tags, "pairs": [], "trend": [], "source": "Roblox官方 genreL1"}
        con.close()
        return jsonify(out)

    # Steam: SteamSpy tags（社区投票的玩法标签）
    rows = con.execute(
        """SELECT s.peak_in_game, s.ccu, g.tags, g.name, g.name_cn, g.game_id, g.genres
           FROM daily_stats s JOIN games g ON g.platform=s.platform
             AND g.game_id=s.game_id
           WHERE s.date=? AND s.platform='steam' AND g.tags IS NOT NULL""",
        (date,)).fetchall()
    tag_games = {}
    for r in rows:
        weight = r["peak_in_game"] or r["ccu"] or 0
        for tg, votes in (jload(r["tags"], {}) or {}).items():
            if votes < 20:   # 噪声标签过滤（SteamSpy 投票数下限，仍是真实值）
                continue
            tag_games.setdefault(tg, []).append(
                {"name": r["name"], "name_cn": r["name_cn"],
                 "game_id": r["game_id"], "weight": weight})
    tags = []
    for tg, gs in tag_games.items():
        total_w = sum(g["weight"] for g in gs)
        tags.append({
            "tag": tg, "games": len(gs),
            "total_ccu_weight": total_w,
            "top_games": sorted(gs, key=lambda x: -x["weight"])[:6],
        })
    tags.sort(key=lambda x: -x["total_ccu_weight"])
    tags = tags[:60]

    # 共现对（玩法关联核心）
    from itertools import combinations
    top_set = {t["tag"] for t in tags}
    pairs = {}
    for r in rows:
        tgs = [t for t, v in (jload(r["tags"], {}) or {}).items()
               if v >= 20 and t in top_set]
        weight = r["peak_in_game"] or r["ccu"] or 0
        for a, b in combinations(sorted(tgs), 2):
            k = (a, b)
            pairs[k] = pairs.get(k, 0) + weight
    pair_list = [{"a": a, "b": b, "weight": w}
                 for (a, b), w in pairs.items() if w > 2000]
    pair_list.sort(key=lambda x: -x["weight"])
    pair_list = pair_list[:80]

    # 玩法趋势（需 ≥2 个快照）
    trend = []
    if prev:
        prows = con.execute(
            """SELECT s.peak_in_game, s.ccu, g.tags FROM daily_stats s
               JOIN games g ON g.platform=s.platform AND g.game_id=s.game_id
               WHERE s.date=? AND s.platform='steam' AND g.tags IS NOT NULL""",
            (prev,)).fetchall()
        prev_w = {}
        for r in prows:
            weight = r["peak_in_game"] or r["ccu"] or 0
            for tg, votes in (jload(r["tags"], {}) or {}).items():
                if votes >= 20:
                    prev_w[tg] = prev_w.get(tg, 0) + weight
        for t in tags:
            pw = prev_w.get(t["tag"])
            if pw and pw > 0:
                trend.append({
                    "tag": t["tag"], "now": t["total_ccu_weight"], "prev": pw,
                    "chg_pct": round((t["total_ccu_weight"] - pw) / pw * 100, 1)})
        trend.sort(key=lambda x: -abs(x["chg_pct"]))
    con.close()
    return jsonify({
        "ok": True, "date": date, "platform": "steam",
        "tags": tags, "pairs": pair_list, "trend": trend,
        "source": "SteamSpy tags + Valve官方在线人数加权",
    })


# ---------------------------------------------------------- 游戏详情 API ---

@app.route("/api/game/<platform>/<gid>")
def api_game(platform, gid):
    con = db()
    g = con.execute(
        "SELECT * FROM games WHERE platform=? AND game_id=?", (platform, gid)).fetchone()
    if not g:
        return jsonify({"ok": False, "msg": "未收录"}), 404
    hist = con.execute(
        """SELECT date, ccu, peak_in_game, rk, last_week_rank, reviews_total,
                  avg_playtime_2w, avg_playtime_forever, players, visits, favorites,
                  extra_json
           FROM daily_stats WHERE platform=? AND game_id=? ORDER BY date""",
        (platform, gid)).fetchall()
    # 官方商店链接（各平台一手入口）
    meta = jload(g["meta_json"], {}) or {}
    if platform == "steam":
        link = f"https://store.steampowered.com/app/{gid}"
    elif platform == "taptap":
        link = meta.get("tt_url") or f"https://www.taptap.io/app/{gid}"
    elif platform == "epic":
        link = (f"https://store.epicgames.com/p/{meta['productSlug']}"
                if meta.get("productSlug") else None)
    else:   # roblox
        link = (f"https://www.roblox.com/games/{meta['rootPlaceId']}"
                if meta.get("rootPlaceId") else None)
    # 相似游戏：标签 Jaccard（Steam）
    similar = []
    if platform == "steam" and g["tags"]:
        my_tags = set((jload(g["tags"], {}) or {}).keys())
        if my_tags:
            others = con.execute(
                """SELECT game_id, name, name_cn, tags FROM games
                   WHERE platform='steam' AND game_id != ? AND tags IS NOT NULL
                   LIMIT 4000""", (gid,)).fetchall()
            sims = []
            for o in others:
                ot = set((jload(o["tags"], {}) or {}).keys())
                inter = len(my_tags & ot)
                if inter == 0:
                    continue
                jac = inter / len(my_tags | ot)
                sims.append((jac, o["game_id"], o["name"], o["name_cn"]))
            sims.sort(reverse=True)
            similar = [{"game_id": i, "name": n, "name_cn": c, "jaccard": round(j, 3)}
                       for j, i, n, c in sims[:12]]
    date = latest_date(con)
    extra_cur = None
    if hist:
        extra_cur = jload(hist[-1]["extra_json"], {})
    con.close()
    return jsonify({
        "ok": True,
        "game": {
            "platform": platform, "game_id": gid, "name": g["name"],
            "name_cn": g["name_cn"], "desc_cn": g["desc_cn"],
            "link": link,
            "developer": g["developer"], "publisher": g["publisher"],
            "genres": jload(g["genres"], []), "tags": jload(g["tags"], {}),
            "release_date": g["release_date"], "meta": meta,
        },
        "history": [
            {"date": h["date"], "ccu": h["ccu"], "peak_in_game": h["peak_in_game"],
             "rank": h["rk"], "last_week_rank": h["last_week_rank"],
             "reviews_total": h["reviews_total"],
             "avg_playtime_2w": h["avg_playtime_2w"],
             "avg_playtime_forever": h["avg_playtime_forever"],
             "players": h["players"], "visits": h["visits"],
             "favorites": h["favorites"],
             "sorts": (jload(h["extra_json"], {}) or {}).get("sorts")}
            for h in hist],
        "similar": similar,
        "latest_date": date,
    })


# ------------------------------------------------------------ Epic API -----

@app.route("/api/epic")
def api_epic():
    con = db()
    date = latest_date(con)
    if not date:
        return jsonify({"ok": False, "msg": "无数据"})
    rows = con.execute(
        """SELECT s.*, g.name, g.name_cn, g.developer, g.genres, g.release_date, g.meta_json
           FROM daily_stats s JOIN games g ON g.platform=s.platform
             AND g.game_id=s.game_id
           WHERE s.date=? AND s.platform='epic'""", (date,)).fetchall()
    out = []
    for r in rows:
        extra = jload(r["extra_json"], {}) or {}
        meta = jload(r["meta_json"], {}) or {}
        out.append({
            "game_id": r["game_id"], "name": r["name"], "name_cn": r["name_cn"],
            "name_cn": r["name_cn"],
            "developer": r["developer"],
            "free_now": (extra.get("freeNow") or False),
            "free_upcoming": (extra.get("freeUpcoming") or False),
            "promo": extra.get("promoDetail"),
            "original_price_usd": meta.get("originalPrice_usd"),
            "slug": meta.get("productSlug"),
            "icon": meta.get("icon"),
            "source": "Epic官方",
        })
    out.sort(key=lambda x: (not x["free_now"], not x["free_upcoming"]))
    con.close()
    return jsonify({"ok": True, "date": date, "games": out})


# ------------------------------------------------------------ 静态资源 -----

@app.route("/static/<path:fn>")
def static_files(fn):
    return send_from_directory(os.path.join(BASE, "static"), fn,
                               max_age=86400)


if __name__ == "__main__":
    # WSGI 中间件：兼容 /game 前缀（生产 Caddy 已 strip；本地直连 5002 时可用前缀复现）
    class _GamePrefix:
        def __init__(self, wsgi):
            self.wsgi = wsgi

        def __call__(self, environ, start_resp):
            p = environ.get("PATH_INFO", "")
            if p.startswith("/game"):
                environ["PATH_INFO"] = p[5:] or "/"
            return self.wsgi(environ, start_resp)

    from werkzeug.serving import run_simple
    run_simple("127.0.0.1", 5002, _GamePrefix(app), threaded=True)
