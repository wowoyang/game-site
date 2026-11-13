import sys
sys.path.insert(0, "/home/ubuntu/game_site")
import collect
con = collect.ensure_db()
while True:
    r = collect.spy_backfill(con, max_games=100000)
    if r == 0:
        break
con.close()
