"""Generate src/data/giants-schedule.json: Giants home games at Oracle Park.

Source: the capstone MLB bronze (officialDate-based dates, the UTC-shift bug is
already fixed upstream). Regular season plus spring/exhibition rows AT the park,
2023-01-01 onward. Future games carry attendance null; the site substitutes the
day/night median of played 2023+ games (meta.medianAttendance).

Run:
  cd /Users/Steve3/Projects/hyperfocus/venue-economics && \
  /Users/Steve3/Projects/personal/capstone/notebooks/.venv/bin/python scripts/build-schedule.py
"""
import json
import os
from pathlib import Path

import duckdb

CSV = os.environ.get(
    "MLB_CSV",
    "/Users/Steve3/Projects/personal/capstone/S3/mlb_giants_schedule/mlb_giants_home_games.csv",
)
OUT = Path(__file__).resolve().parents[1] / "src" / "data" / "giants-schedule.json"

con = duckdb.connect()
# One row per DATE: a doubleheader is a single treatment day. Earliest first
# pitch sets the day/night flag; attendance is the max across the games.
rows = con.execute(
    f"""
    WITH g AS (
        SELECT "date"::VARCHAR AS d, day_night AS dn, first_pitch_hour AS h,
               opponent AS opp, attendance AS att,
               row_number() OVER (PARTITION BY "date"
                                  ORDER BY first_pitch_hour NULLS LAST) AS rn,
               count(*) OVER (PARTITION BY "date") AS n_games,
               max(attendance) OVER (PARTITION BY "date") AS att_max
        FROM read_csv_auto('{CSV}')
        WHERE "date" >= '2023-01-01' AND status IN ('Final', 'Scheduled')
          AND game_type NOT IN ('S', 'E')
    )
    SELECT d, dn, h, opp, att_max AS att,
           CASE WHEN n_games > 1 THEN 'DH' ELSE 'R' END AS gt
    FROM g WHERE rn = 1
    ORDER BY d
    """
).fetchall()

med = dict(
    con.execute(
        f"""
        SELECT day_night, CAST(median(attendance) AS INT)
        FROM read_csv_auto('{CSV}')
        WHERE attendance > 0 AND "date" >= '2023-01-01'
          AND game_type NOT IN ('S', 'E')
        GROUP BY 1
        """
    ).fetchall()
)

games = [
    {"d": d, "dn": dn, "h": h, "opp": opp, "att": att if att and att > 0 else None, "gt": gt}
    for d, dn, h, opp, att, gt in rows
]

assert len(games) > 250, f"only {len(games)} games; expected ~330"
assert set(med) == {"day", "night"}

OUT.write_text(json.dumps({
    "meta": {
        "source": "capstone MLB bronze (officialDate), Oracle Park home games 2023+",
        "minDate": games[0]["d"],
        "maxDate": games[-1]["d"],
        "medianAttendance": {"day": med["day"], "night": med["night"]},
    },
    "games": games,
}, separators=(",", ":")) + "\n")
print(f"wrote {len(games)} games {games[0]['d']}..{games[-1]['d']} -> {OUT}")
print(f"median attendance day={med['day']} night={med['night']}")
