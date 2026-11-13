#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""403 解封探测 + 阶段 A 自动续跑。

每小时探测 Valve appdetails（用一款真实游戏）；解封后以 2 线程 + 1.5s 间隔
保守节奏跑完剩余核心字段，全部完成自动退出。
由 systemd game-unban-watch.service 常驻驱动（本脚本单次执行即退）。
"""
import json
import sys
import time
import urllib.request

sys.path.insert(0, "/home/ubuntu/game_site")
import collect

PROBE_URL = ("https://store.steampowered.com/api/appdetails"
             "?appids=570&cc=us&l=en")


def valve_ok():
    """appdetails 是否恢复正常（返回 JSON 且带 success 字段）。"""
    try:
        req = urllib.request.Request(
            PROBE_URL, headers={"User-Agent": "Mozilla/5.0"})
        j = json.loads(urllib.request.urlopen(req, timeout=15).read())
        node = j.get("570") or {}
        return node.get("success") is not None
    except Exception:
        return False


def main():
    while True:
        if not valve_ok():
            collect.log("[unban-watch] Valve 仍 403，1h 后再探")
            time.sleep(3600)
            continue
        break
    collect.log("[unban-watch] Valve 已解封 ✅ 启动阶段 A 续跑（2 线程）")
    con = collect.ensure_db()
    while True:
        try:
            r = collect.enrich_steam_parallel(
                con, max_games=2000, workers=2, skip_spy=True)
        except RuntimeError as e:
            collect.log(f"[unban-watch] {e}，1h 后再试")
            time.sleep(3600)
            continue
        if r == 0:
            collect.log("[unban-watch] 阶段 A 全部完成 ✅ 退出")
            break
    con.close()


if __name__ == "__main__":
    main()
