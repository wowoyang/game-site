#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Steam 全量富化专用任务 v2 — 双通道流水线。

通道 A（快，8 线程 Valve）：发行日/类型/价格/中文名/中文简介/评论数
通道 B（慢，1 线程 SteamSpy）：userscore/positive/negative（限速 1.15s/款）

A 先跑完全量（~5h），B 随后独立跑完（~20h）。全部完成自动退出。
"""
import json
import subprocess
import sys
import time

sys.path.insert(0, "/home/ubuntu/game_site")
import collect


def run_stage(name, fn):
    while True:
        try:
            r = fn()
            if not r:
                collect.log(f"[fullenrich] {name} 无剩余，跳过")
                return
            return
        except Exception as e:
            collect.log(f"[fullenrich] {name} 异常 {type(e).__name__}: {e}，60s 续跑")
            time.sleep(60)


def pending_core(con):
    n = 0
    for (mj, rel, genres) in con.execute(
            "SELECT meta_json, release_date, genres FROM games "
            "WHERE platform='steam'").fetchall():
        m = json.loads(mj or "{}")
        if not (rel and genres and m.get("price_usd") is not None):
            n += 1
    return n


def pending_spy(con):
    n = 0
    for (mj,) in con.execute(
            "SELECT meta_json FROM games WHERE platform='steam'").fetchall():
        m = json.loads(mj or "{}")
        if m.get("userscore") is None:
            n += 1
    return n


def main():
    con = collect.ensure_db()
    collect.log("[fullenrich] === 阶段A: Valve 核心字段 8 线程 ===")
    while pending_core(con) > 0:
        run_stage("core",
                  lambda: collect.enrich_steam_parallel(
                      con, max_games=5000, workers=8, skip_spy=True))
    collect.log("[fullenrich] === 阶段A 完成 ✅ ===")
    collect.log("[fullenrich] === 阶段B: SteamSpy 口碑回填 1 线程 ===")
    while pending_spy(con) > 0:
        run_stage("spy", lambda: collect.spy_backfill(con, max_games=100000))
    collect.log("[fullenrich] === 阶段B 完成，全部收官 ✅ ===")
    con.close()


if __name__ == "__main__":
    main()
