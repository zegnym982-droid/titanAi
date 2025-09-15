# Fitness Logger Telegram Bot (SQL backend)

A comprehensive Telegram bot for tracking workouts with an **SQL database backend**, automated scheduling, and intelligent, health‑aware training recommendations.

## What changed vs Google Sheets

* ✅ Replaced Google Sheets with **SQL storage** (SQLite by default; PostgreSQL/MySQL via `DATABASE_URL`).
* ✅ Added **schema & migrations**.
* ✅ Built‑in **backups** (SQL dumps + CSV exports).
* ✅ Sample **SQL views/queries** for weekly reports and per‑muscle‑group tonnage.

---

## Features

* 🏋️ **Workout Tracking**: `/train` → add sets (flexible parsing) → `/finish` with sRPE/mood/sleep.
* 🧠 **Health‑Aware Recommendations**: adapts to injuries/limitations (e.g., shoulder issues → remove/modify OHP/bench variants).
* 📈 **Progress Analysis**: weekly stats, **tonnage per muscle group**, overload detection.
* 📅 **Automated Scheduling**: Weekly report every Sunday 20:00 (Europe/Amsterdam) and daily backups at 02:30 UTC.
* 🎤 **Voice Notes**: stores transcriptions.
* 🗄️ **SQL Backend**: SQLite for quick start; bring your own Postgres/MySQL with `DATABASE_URL`.

---

## Quick Start on Replit

### 1) Secrets / Environment

Required:

* `TELEGRAM_BOT_TOKEN` — your bot token from @BotFather.

Optional (for external DBs):

* `DATABASE_URL` — e.g. `postgresql+psycopg://user:pass@host:5432/dbname` or `mysql+pymysql://...`

  * If **not** set, the bot uses local **SQLite** at `fitness_bot.db`.
* `BACKUP_DIR` — defaults to `backups/`.

### 2) Run the Bot

Click **▶️ Run** in Replit. The bot will:

* Initialize the database (create tables/migrations if missing)
* Start a small keep‑alive web server
* Begin Telegram polling
* Schedule cron jobs (weekly report, daily backups)

### 3) Test Workflow

```
/start
/train
/add Bench 60x5x3
/finish
/week
```

---

## Commands

| Command   | Description                             |
| --------- | --------------------------------------- |
| `/start`  | Register user and initialize DB profile |
| `/train`  | Start a workout session                 |
| `/add`    | Add sets (e.g., `Squat 100x5x3@7`)      |
| `/finish` | Finish workout with sRPE / mood / sleep |
| `/week`   | Weekly report (incl. per‑group tonnage) |

### Set Format Examples

```
Bench 60x5x3        # weight × reps × sets
Жим 80x3@8          # Russian names + RPE
Deadlift 120x5      # single set
Squat 100x3x5@7     # multiple sets + RPE
```

Supported exercises (EN/RU):

* Жим / Bench → Bench Press
* Присед / Squat → Squat
* Тяга / Deadlift → Deadlift
* Жим стоя / OHP → Overhead Press

---

## Database

### Engines

* **SQLite** (default): no config needed, dev‑friendly.
* **PostgreSQL/MySQL**: set `DATABASE_URL`.

### Schema (simplified)

```sql
-- users
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,           -- UUID if using Postgres
  tg_user_id TEXT UNIQUE NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  age INTEGER, height_cm INTEGER, weight_kg REAL,
  training_time TEXT,                             -- preferred HH:MM (optional)
  health_limits TEXT                              -- free text: e.g., "shoulder pain"
);

-- workouts
CREATE TABLE IF NOT EXISTS workouts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  started_at TIMESTAMP NOT NULL,
  finished_at TIMESTAMP,
  program TEXT,
  srpe REAL, mood INTEGER, sleep_hours REAL
);

-- sets
CREATE TABLE IF NOT EXISTS sets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workout_id INTEGER NOT NULL REFERENCES workouts(id) ON DELETE CASCADE,
  exercise TEXT NOT NULL,
  weight REAL NOT NULL,
  reps INTEGER NOT NULL,
  rpe REAL,
  muscle_group TEXT NOT NULL,        -- derived mapping, e.g., "chest", "legs", "back", "shoulders"
  tonnage AS (weight * reps) STORED  -- if engine supports generated columns; else compute in queries
);

-- prs
CREATE TABLE IF NOT EXISTS prs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  exercise TEXT NOT NULL,
  weight REAL NOT NULL,
  reps INTEGER NOT NULL,
  achieved_at TIMESTAMP NOT NULL
);

-- notes (voice/text)
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  workout_id INTEGER REFERENCES workouts(id) ON DELETE SET NULL,
  kind TEXT CHECK (kind IN ('voice','text')) NOT NULL,
  content TEXT NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### Indices

```sql
CREATE INDEX IF NOT EXISTS idx_sets_workout_id ON sets(workout_id);
CREATE INDEX IF NOT EXISTS idx_workouts_user_started ON workouts(user_id, started_at);
CREATE INDEX IF NOT EXISTS idx_prs_user_exercise ON prs(user_id, exercise);
```

### Muscle Group Mapping

Mapping occurs in code when parsing `/add`, e.g.:

* Bench/Жим → `chest`
* Squat/Присед → `legs`
* Deadlift/Тяга → `back`
* OHP/Жим стоя → `shoulders`

You can extend this dictionary for more exercises.

### Views & Reports (examples)

**Weekly tonnage per muscle group**:

```sql
-- for a given user and ISO week
CREATE VIEW IF NOT EXISTS v_weekly_group_tonnage AS
SELECT
  u.id AS user_id,
  strftime('%Y-%W', w.started_at) AS iso_week,      -- SQLite; use DATE_TRUNC('week', ...) on Postgres
  s.muscle_group,
  SUM(s.weight * s.reps) AS tonnage
FROM sets s
JOIN workouts w ON w.id = s.workout_id
JOIN users u ON u.id = w.user_id
GROUP BY 1,2,3;
```

**Overload detection (rolling 4‑week avg vs current week)**:

```sql
-- Example query (SQLite). Adapt for Postgres window functions for efficiency.
-- current week tonnage per group
WITH current AS (
  SELECT user_id, iso_week, muscle_group, tonnage
  FROM v_weekly_group_tonnage
  WHERE iso_week = strftime('%Y-%W', 'now')
),
last4 AS (
  SELECT a.user_id, a.muscle_group,
         AVG(b.tonnage) AS avg_4w
  FROM v_weekly_group_tonnage a
  JOIN v_weekly_group_tonnage b
    ON a.user_id = b.user_id AND a.muscle_group = b.muscle_group
  WHERE b.iso_week >= strftime('%Y-%W', date('now','-28 day'))
    AND b.iso_week <  strftime('%Y-%W', 'now')
  GROUP BY 1,2
)
SELECT c.user_id, c.muscle_group, c.tonnage AS current_week,
       l.avg_4w,
       CASE WHEN c.tonnage > l.avg_4w * 1.2 THEN 1 ELSE 0 END AS overload_flag
FROM current c
LEFT JOIN last4 l USING (user_id, muscle_group);
```

---

## Backups & Maintenance

* **Daily backups (02:30 UTC)**:

  * SQLite: copy `fitness_bot.db` to `backups/fitness_bot_YYYYMMDD.db` and export CSVs (`users.csv`, `workouts.csv`, `sets.csv`, `prs.csv`, `notes.csv`).
  * External DB: run `pg_dump` / `mysqldump` (if available in environment), plus CSV exports via code.
* **Weekly reports (Sun 20:00 Europe/Amsterdam)**: bot computes stats and sends a summary.
* **Migrations**: simple `schema.sql` applied at startup; optional migration runner for future changes.

---

## Health‑Aware Logic (summary)

* If a user reports `shoulder` issues → OHP/bench variants are replaced by safer alternatives (e.g., incline DB press light, machine press, neutral‑grip). Loads are reduced and RPE caps are applied.
* Sleep/mood/sRPE influence load progression and deload suggestions.

---

## Troubleshooting

**Bot doesn’t respond**

1. Verify `TELEGRAM_BOT_TOKEN`.
2. Check Replit logs for exceptions.

**Database issues**

* SQLite lock errors → avoid concurrent writes; ensure one bot instance.
* Postgres/MySQL → verify `DATABASE_URL` and network access.
* Run a DB connectivity self‑test: `python test_db.py` (provided).

**Backups**

* Ensure `backups/` exists or set `BACKUP_DIR`.
* For Postgres/MySQL, confirm dump utilities are available or rely on CSV exports.

---

## File Structure

```
fitness-bot/
├── main.py                 # Bot entrypoint (aiogram)
├── db/
│   ├── schema.sql          # Canonical schema
│   ├── migrations/         # (optional) future migrations
│   └── utils.py            # DB helpers/queries
├── test_db.py              # Connectivity & schema smoke tests
├── requirements.txt        # Python deps
├── README.md               # This file
├── fitness_bot.db          # SQLite database (auto‑created if no DATABASE_URL)
└── backups/                # SQL dumps / CSV exports (auto‑created)
```

## Tech Stack

* **aiogram 3.x** — Telegram framework
* **SQLAlchemy** (recommended) or `sqlite3/psycopg/pymysql` — DB access
* **APScheduler** — scheduling
* **pandas** — analytics & CSV export

---

## Migration from Google Sheets (optional)

If you have existing CSV exports from Sheets:

1. Place `users.csv`, `workouts.csv`, `sets.csv`, `prs.csv`, `notes.csv` into `backups/import/`.
2. Run `python tools/import_csv.py` (script maps columns to schema and inserts rows idempotently).
3. Verify counts with `python test_db.py`.

