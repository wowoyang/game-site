#!/usr/bin/env python3
"""每日 Steam 滚动富化（活跃优先）。

策略（87K 全量宇宙下的现实预算）：
- P0: 当日活跃游戏（有 ccu/时长信号，~3K 款）——研究价值密度最高，
  每日重刷 appdetails（中文名/中文简介/发行日/genres/好评率）
- P1: 未富化的非活跃游戏（按拥有量降序）——每日预算 N 款滚动补全，
  数量级 80K，以月为单位逐步覆盖
enrich_steam(con, priority_active=date) 内部实现优先级。
"""
import sys
sys.path.insert(0, "/home/ubuntu/game_site")
import collect

con = collect.ensure_db()
collect.enrich_steam(con)
con.close()
