#!/usr/bin/env python3
"""
Fitness Logger Telegram Bot
A comprehensive fitness tracking bot with SQLite storage
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dateutil import parser as date_parser
from pytz import timezone
from openai import OpenAI

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Constants
DEFAULT_TIMEZONE = "Europe/Moscow"
DEFAULT_BACKUP_DIR = f"/home/runner/{os.environ.get('REPL_SLUG', 'fitness-bot')}/backups"

# Trainer system prompt for ChatGPT integration
TRAINER_SYSTEM_PROMPT = """
You are a professional fitness trainer and coach with extensive experience in strength training, 
powerlifting, and general fitness. You speak Russian and English fluently. You provide evidence-based advice, focus on progressive overload, 
proper form, and injury prevention. You are supportive but firm in your recommendations, 
emphasizing consistency and gradual progress over quick fixes.

When creating workout programs, always:
1. Consider the user's experience level, available equipment, and any injuries
2. Include compound movements as the foundation
3. Provide clear progression schemes
4. Include warm-up and cool-down recommendations
5. Adapt to the user's schedule and goals

When parsing workout logs (in Russian or English), extract:
- Exercise names (normalize to common English names: жим лёжа = bench press, присед = squat, тяга = deadlift)
- Weight in kg, sets, reps, RPE if mentioned
- Parse natural language like "50 кг 6 подходов по 100 раз" or "bench 60x5x3@8"
- Any notes about form or feeling

Common Russian exercise terms:
- жим лёжа/жим = bench press
- присед/приседания = squat  
- тяга/становая = deadlift
- подходы/подходов = sets
- раз/повторы = reps
- кг = kg

Always respond in JSON format when requested.
"""

# Default training rules
LOAD_RULES = {
    "deload_threshold": 1.25,  # 25% increase triggers deload recommendation
    "high_srpe_threshold": 9,   # sRPE >= 9 is very high
    "low_srpe_threshold": 5,    # sRPE <= 5 is low
    "poor_sleep_threshold": 2,  # Sleep quality <= 2 is poor
}

# OpenAI client initialization with configurable model
# Use stable, available models with fallback
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds

try:
    openai_client = OpenAI(
        api_key=os.environ.get("OPENAI_API_KEY"),
        timeout=30.0,
        max_retries=0  # We'll handle retries manually
    )
except Exception as e:
    logger.error(f"Failed to initialize OpenAI client: {e}")
    openai_client = None

# Exercise aliases (Russian -> English)
EXERCISE_ALIASES = {
    'жим': 'bench press',
    'жим лежа': 'bench press',
    'bench': 'bench press',
    'присед': 'squat',
    'приседания': 'squat',
    'squat': 'squat',
    'тяга': 'deadlift',
    'становая': 'deadlift',
    'deadlift': 'deadlift',
    'жим стоя': 'overhead press',
    'армейский жим': 'overhead press',
    'ohp': 'overhead press',
    'подтягивания': 'pull ups',
    'pull ups': 'pull ups',
    'отжимания': 'push ups',
    'push ups': 'push ups',
}

# Exercise classification for tonnage calculations
EXERCISE_GROUPS = {
    'Ноги': [
        'присед', 'приседания', 'приседание', 'squat', 'leg press', 'жим ногами',
        'выпады', 'lunge', 'подъем', 'подъёмы на носки', 'calf raise',
        'румынская тяга', 'мертвая тяга', 'rdl', 'romanian deadlift',
        'гиперэкстензия', 'hyperextension', 'разгибание ног', 'leg extension',
        'сгибание ног', 'leg curl', 'ягодичный мостик', 'hip thrust'
    ],
    'Жимы': [
        'жим', 'bench press', 'press', 'отжимания', 'pushup', 'push up',
        'разводка', 'fly', 'флай', 'жим гантелей', 'dumbbell press',
        'армейский жим', 'overhead press', 'жим стоя', 'standing press',
        'жим сидя', 'seated press', 'махи', 'lateral raise', 'разведение'
    ],
    'Тяги': [
        'тяга', 'row', 'подтягивания', 'pull up', 'pullup', 'chin up',
        'становая', 'deadlift', 'тяга штанги', 'barbell row',
        'тяга гантели', 'dumbbell row', 'тяга блока', 'cable row',
        'шраги', 'shrug', 'face pull', 'обратные разводки'
    ]
}


def classify_exercise(exercise_name: str) -> str:
    """Classify exercise into muscle groups (Ноги, Жимы, Тяги)"""
    exercise_lower = exercise_name.lower().strip()
    
    # Remove common formatting like "3x8-10" from exercise names
    exercise_clean = exercise_lower.split(':')[0].strip()
    
    for group_name, keywords in EXERCISE_GROUPS.items():
        for keyword in keywords:
            if keyword.lower() in exercise_clean:
                return group_name
    
    # Default classification based on common patterns
    if any(word in exercise_clean for word in ['жим', 'press', 'push']):
        return 'Жимы'
    elif any(word in exercise_clean for word in ['тяга', 'pull', 'row', 'подтягивания']):
        return 'Тяги'
    elif any(word in exercise_clean for word in ['присед', 'squat', 'ног']):
        return 'Ноги'
    
    # If no match found, try to guess from exercise structure
    return 'Другие'


def calculate_tonnage_by_groups(db_path: str, tg_user_id: int, weeks_back: int = 0) -> Dict[str, float]:
    """Calculate tonnage by muscle groups for the current calendar week or specified weeks back"""
    import sqlite3
    from datetime import datetime, timedelta
    
    # Calculate calendar week boundaries (Monday to Sunday)
    today = datetime.now()
    # Get Monday of the current week
    current_week_start = today - timedelta(days=today.weekday())
    # Go back the specified number of weeks
    week_start = current_week_start - timedelta(weeks=weeks_back)
    # Week end is Sunday
    week_end = week_start + timedelta(days=6, hours=23, minutes=59, seconds=59)
    
    start_date = week_start
    end_date = week_end
    
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        
        # Get sets with muscle groups for the specified period
        cursor.execute("""
            SELECT s.muscle_group, s.weight, s.reps
            FROM sets s
            JOIN workouts w ON s.workout_id = w.workout_id
            WHERE w.tg_user_id = ? 
            AND datetime(w.dt_start) >= datetime(?)
            AND datetime(w.dt_start) <= datetime(?)
            AND s.muscle_group IS NOT NULL
        """, (tg_user_id, start_date.isoformat(), end_date.isoformat()))
        
        sets_data = cursor.fetchall()
    
    # Calculate tonnage by groups
    tonnage_by_group = {}
    total_tonnage = 0
    
    for muscle_group, weight, reps in sets_data:
        if muscle_group not in tonnage_by_group:
            tonnage_by_group[muscle_group] = 0
        
        set_tonnage = weight * reps
        tonnage_by_group[muscle_group] += set_tonnage
        total_tonnage += set_tonnage
    
    # Add percentages and total
    result = {}
    for group, tonnage in tonnage_by_group.items():
        percentage = (tonnage / total_tonnage * 100) if total_tonnage > 0 else 0
        result[group] = {
            'tonnage': round(tonnage, 1),
            'percentage': round(percentage, 1)
        }
    
    result['total'] = round(total_tonnage, 1)
    return result


def calculate_exercise_tonnage(db_path: str, tg_user_id: int, days_back: int = 7) -> Dict[str, float]:
    """Calculate tonnage by individual exercises for the specified period"""
    import sqlite3
    from datetime import datetime, timedelta
    
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)
    
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        
        # Get sets by exercises for the specified period
        cursor.execute("""
            SELECT s.exercise, s.weight, s.reps, s.muscle_group
            FROM sets s
            JOIN workouts w ON s.workout_id = w.workout_id
            WHERE w.tg_user_id = ? 
            AND datetime(w.dt_start) >= datetime(?)
            AND datetime(w.dt_start) <= datetime(?)
        """, (tg_user_id, start_date.isoformat(), end_date.isoformat()))
        
        sets_data = cursor.fetchall()
    
    # Calculate tonnage by exercises
    exercise_tonnage = {}
    
    for exercise, weight, reps, muscle_group in sets_data:
        if exercise not in exercise_tonnage:
            exercise_tonnage[exercise] = {
                'tonnage': 0,
                'muscle_group': muscle_group or classify_exercise(exercise)
            }
        
        exercise_tonnage[exercise]['tonnage'] += weight * reps
    
    # Round tonnage values
    for exercise_data in exercise_tonnage.values():
        exercise_data['tonnage'] = round(exercise_data['tonnage'], 1)
    
    return exercise_tonnage


class KeepAliveServer:
    """Simple HTTP server to keep Replit alive"""

    def __init__(self, port: Optional[int] = None):
        self.port = port or int(os.environ.get('PORT', '8000'))
        self.server = None

    def start(self):
        """Start the keep-alive server in a separate thread"""

        def run_server():

            class Handler(BaseHTTPRequestHandler):

                def do_GET(self):
                    self.send_response(200)
                    self.send_header('Content-type', 'text/plain')
                    self.end_headers()
                    self.wfile.write(b'ok')

                def log_message(self, format, *args):
                    pass  # Suppress HTTP logs

            self.server = HTTPServer(('0.0.0.0', self.port), Handler)
            logger.info(f"Keep-alive server started on port {self.port}")
            self.server.serve_forever()

        thread = threading.Thread(target=run_server, daemon=True)
        thread.start()


class DatabaseManager:
    """SQLite database manager for the fitness bot"""

    def __init__(self, db_path: str = "fitness_bot.db"):
        self.db_path = db_path
        self.init_database()

    def init_database(self):
        """Initialize the database with required tables"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            # Enhanced Users table with new fields
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    tg_user_id INTEGER PRIMARY KEY,
                    tz TEXT DEFAULT 'Europe/Moscow',
                    goals TEXT,
                    unit TEXT DEFAULT 'kg',
                    notify_conf TEXT,
                    schedule_days TEXT,
                    preferred_time_local TEXT,
                    goal TEXT,
                    experience TEXT,
                    equipment TEXT,
                    injuries TEXT,
                    -- Расширенная анкета пользователя
                    age INTEGER,
                    gender TEXT,
                    height INTEGER,
                    weight REAL,
                    activity_level TEXT,
                    goal_deadline TEXT,
                    priorities TEXT,
                    chronic_diseases TEXT,
                    doctor_approval TEXT,
                    preferred_exercises TEXT,
                    workout_duration TEXT,
                    motivation TEXT,
                    workout_time TEXT,  -- Время начала тренировок для напоминаний
                    health_restrictions TEXT,  -- Проблемные зоны здоровья (плечи, спина, колени)
                training_style TEXT  -- Стиль тренировок (Сплит или Фулбоди)
                )
            """)
            
            # Database migration: Add missing columns to existing users table
            try:
                # Check if the new columns exist and add them if they don't
                missing_columns = [
                    ('schedule_days', 'TEXT'),
                    ('preferred_time_local', 'TEXT'), 
                    ('goal', 'TEXT'),
                    ('experience', 'TEXT'),
                    ('equipment', 'TEXT'),
                    ('injuries', 'TEXT'),
                    # Новые поля для расширенной анкеты
                    ('age', 'INTEGER'),
                    ('gender', 'TEXT'),
                    ('height', 'INTEGER'), # в см
                    ('weight', 'REAL'), # в кг
                    ('activity_level', 'TEXT'), # сидячий, умеренный, активный, спортсмен
                    ('goal_deadline', 'TEXT'), # срок достижения цели
                    ('priorities', 'TEXT'), # приоритеты через запятую
                    ('chronic_diseases', 'TEXT'), # хронические заболевания
                    ('doctor_approval', 'TEXT'), # разрешение врача
                    ('preferred_exercises', 'TEXT'), # любимые виды упражнений
                    ('workout_duration', 'TEXT'), # длительность тренировки
                    ('motivation', 'TEXT'), # мотивация
                    ('workout_time', 'TEXT'), # время начала тренировок
                    ('health_restrictions', 'TEXT'), # проблемные зоны здоровья
                    ('training_style', 'TEXT') # стиль тренировок (Сплит/Фулбоди)
                ]
                
                # Get existing column names
                cursor.execute("PRAGMA table_info(users)")
                existing_columns = {row[1] for row in cursor.fetchall()}
                
                # Add missing columns
                for col_name, col_type in missing_columns:
                    if col_name not in existing_columns:
                        cursor.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}")
                        logger.info(f"Added missing column: {col_name}")
                        
            except sqlite3.Error as e:
                logger.error(f"Error during database migration: {e}")
                # Continue execution as the bot should still work with basic functionality
            
            # Database migration for sets table: Add muscle_group column
            try:
                # Check if muscle_group column exists in sets table
                cursor.execute("PRAGMA table_info(sets)")
                sets_columns = {row[1] for row in cursor.fetchall()}
                
                if 'muscle_group' not in sets_columns:
                    cursor.execute("ALTER TABLE sets ADD COLUMN muscle_group TEXT")
                    logger.info("Added missing column to sets: muscle_group")
                    
                    # Update existing sets with muscle group classification
                    cursor.execute("SELECT id, exercise FROM sets WHERE muscle_group IS NULL")
                    existing_sets = cursor.fetchall()
                    
                    for set_id, exercise in existing_sets:
                        muscle_group = classify_exercise(exercise)
                        cursor.execute("UPDATE sets SET muscle_group = ? WHERE id = ?", (muscle_group, set_id))
                    
                    if existing_sets:
                        logger.info(f"Updated {len(existing_sets)} existing sets with muscle group classification")
                        
            except sqlite3.Error as e:
                logger.error(f"Error during sets table migration: {e}")

            # Enhanced Workouts table with new fields
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS workouts (
                    workout_id TEXT PRIMARY KEY,
                    tg_user_id INTEGER,
                    dt_start TEXT,
                    dt_end TEXT,
                    program TEXT,
                    duration_min INTEGER,
                    sRPE INTEGER,
                    mood INTEGER,
                    sleep INTEGER,
                    training_load INTEGER,
                    note TEXT,
                    fatigue INTEGER,
                    sleep_quality INTEGER,
                    program_id TEXT,
                    FOREIGN KEY (tg_user_id) REFERENCES users (tg_user_id),
                    FOREIGN KEY (program_id) REFERENCES workout_programs (id)
                )
            """)

            # Workout Programs table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS workout_programs (
                    id TEXT PRIMARY KEY,
                    tg_user_id INTEGER,
                    program_data TEXT,
                    created_at TEXT,
                    is_active INTEGER DEFAULT 1,
                    name TEXT,
                    description TEXT,
                    FOREIGN KEY (tg_user_id) REFERENCES users (tg_user_id)
                )
            """)

            # Workout Timer table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS workout_timer (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tg_user_id INTEGER,
                    workout_id TEXT,
                    timer_start TEXT,
                    timer_duration_min INTEGER DEFAULT 60,
                    notification_sent INTEGER DEFAULT 0,
                    FOREIGN KEY (tg_user_id) REFERENCES users (tg_user_id),
                    FOREIGN KEY (workout_id) REFERENCES workouts (workout_id)
                )
            """)

            # Sets table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workout_id TEXT,
                    exercise TEXT,
                    weight REAL,
                    reps INTEGER,
                    set_idx INTEGER,
                    rpe REAL,
                    comment TEXT,
                    muscle_group TEXT,
                    FOREIGN KEY (workout_id) REFERENCES workouts (workout_id)
                )
            """)

            # PRs table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS prs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tg_user_id INTEGER,
                    date TEXT,
                    exercise TEXT,
                    best_weight REAL,
                    reps INTEGER,
                    est_1rm REAL,
                    note TEXT,
                    FOREIGN KEY (tg_user_id) REFERENCES users (tg_user_id)
                )
            """)

            # Weekly statistics table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS weekly_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tg_user_id INTEGER,
                    week_start TEXT,
                    workouts INTEGER,
                    total_tonnage REAL,
                    avg_sRPE REAL,
                    sum_load INTEGER,
                    avg_mood REAL,
                    deload_flag INTEGER DEFAULT 0,
                    note TEXT,
                    FOREIGN KEY (tg_user_id) REFERENCES users (tg_user_id)
                )
            """)

            # Notes table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tg_user_id INTEGER,
                    date_time TEXT,
                    type TEXT,
                    content TEXT,
                    linked_workout_id TEXT,
                    FOREIGN KEY (tg_user_id) REFERENCES users (tg_user_id),
                    FOREIGN KEY (linked_workout_id) REFERENCES workouts (workout_id)
                )
            """)

            conn.commit()

    def get_user(self, tg_user_id: int) -> Optional[Dict]:
        """Get user data by Telegram user ID"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE tg_user_id = ?",
                           (tg_user_id, ))
            row = cursor.fetchone()
            if row:
                return {
                    'tg_user_id': row[0],
                    'tz': row[1],
                    'goals': row[2],
                    'unit': row[3],
                    'notify_conf': row[4]
                }
        return None

    def create_user(self, tg_user_id: int, tz: str = DEFAULT_TIMEZONE):
        """Create a new user"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO users (tg_user_id, tz, unit, notify_conf)
                VALUES (?, ?, 'kg', '{}')
            """, (tg_user_id, tz))
            conn.commit()

    def create_workout(self,
                       workout_id: str,
                       tg_user_id: int,
                       dt_start: str,
                       program: str = ""):
        """Create a new workout"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO workouts (workout_id, tg_user_id, dt_start, program)
                VALUES (?, ?, ?, ?)
            """, (workout_id, tg_user_id, dt_start, program))
            conn.commit()

    def add_set(self,
                workout_id: str,
                exercise: str,
                weight: float,
                reps: int,
                set_idx: int,
                rpe: Optional[float] = None,
                comment: str = ""):
        """Add a set to a workout"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO sets (workout_id, exercise, weight, reps, set_idx, rpe, comment, muscle_group)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (workout_id, exercise, weight, reps, set_idx, rpe, comment, classify_exercise(exercise)))
            conn.commit()

    def finish_workout(self,
                       workout_id: str,
                       dt_end: str,
                       sRPE: int,
                       mood: int,
                       sleep: int,
                       note: str = ""):
        """Finish a workout with feedback data"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            # Calculate duration and training load
            cursor.execute(
                "SELECT dt_start FROM workouts WHERE workout_id = ?",
                (workout_id, ))
            dt_start_str = cursor.fetchone()[0]
            dt_start = datetime.fromisoformat(dt_start_str)
            dt_end_dt = datetime.fromisoformat(dt_end)
            duration_min = max(
                1, int((dt_end_dt - dt_start).total_seconds() / 60))
            training_load = sRPE * duration_min

            cursor.execute(
                """
                UPDATE workouts 
                SET dt_end = ?, duration_min = ?, sRPE = ?, mood = ?, sleep = ?, 
                    training_load = ?, note = ?
                WHERE workout_id = ?
            """, (dt_end, duration_min, sRPE, mood, sleep, training_load, note,
                  workout_id))
            conn.commit()

    def get_active_workout(self, tg_user_id: int) -> Optional[str]:
        """Get active workout ID for user"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT workout_id FROM workouts
                WHERE tg_user_id = ? AND dt_end IS NULL
                ORDER BY dt_start DESC LIMIT 1
                """, (tg_user_id, ))
            row = cursor.fetchone()
            return row[0] if row else None

    def get_workout_sets(self, workout_id: str) -> List[Dict]:
        """Get all sets for a specific workout"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT exercise, weight, reps, set_idx, rpe, comment
                FROM sets
                WHERE workout_id = ?
                ORDER BY set_idx
                """, (workout_id, ))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_weekly_stats(self,
                         tg_user_id: int,
                         weeks_back: int = 4) -> List[Dict]:
        """Get weekly training statistics"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            end_date = datetime.now()
            start_date = end_date - timedelta(weeks=weeks_back)

            cursor.execute(
                """
                SELECT * FROM workouts 
                WHERE tg_user_id = ? AND dt_start >= ? AND dt_end IS NOT NULL
                ORDER BY dt_start
            """, (tg_user_id, start_date.isoformat()))

            workouts = cursor.fetchall()
            return [{
                'workout_id': w[0],
                'tg_user_id': w[1],
                'dt_start': w[2],
                'dt_end': w[3],
                'program': w[4],
                'duration_min': w[5],
                'sRPE': w[6],
                'mood': w[7],
                'sleep': w[8],
                'training_load': w[9],
                'note': w[10]
            } for w in workouts]
    
    def create_workout_program(self, tg_user_id: int, program_data: str, name: str, description: str) -> str:
        """Create a new workout program"""
        program_id = f"prog_{tg_user_id}_{int(datetime.now().timestamp())}"
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO workout_programs (id, tg_user_id, program_data, created_at, name, description)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (program_id, tg_user_id, program_data, datetime.now().isoformat(), name, description))
            conn.commit()
        return program_id
    
    def get_user_programs(self, tg_user_id: int) -> List[Dict]:
        """Get all active programs for user"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM workout_programs 
                WHERE tg_user_id = ? AND is_active = 1
                ORDER BY created_at DESC
            """, (tg_user_id,))
            rows = cursor.fetchall()
            return [{
                'id': r[0], 'tg_user_id': r[1], 'program_data': r[2],
                'created_at': r[3], 'is_active': r[4], 'name': r[5], 'description': r[6]
            } for r in rows]
    
    def start_workout_timer(self, tg_user_id: int, workout_id: str, duration_min: int = 60):
        """Start workout timer"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO workout_timer (tg_user_id, workout_id, timer_start, timer_duration_min)
                VALUES (?, ?, ?, ?)
            """, (tg_user_id, workout_id, datetime.now().isoformat(), duration_min))
            conn.commit()
    
    def check_expired_timers(self) -> List[Dict]:
        """Check for expired workout timers"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM workout_timer 
                WHERE notification_sent = 0 
                AND datetime(timer_start, '+' || timer_duration_min || ' minutes') <= datetime('now')
            """)
            rows = cursor.fetchall()
            return [{
                'id': r[0], 'tg_user_id': r[1], 'workout_id': r[2], 
                'timer_start': r[3], 'timer_duration_min': r[4]
            } for r in rows]
    
    def mark_timer_notified(self, timer_id: int):
        """Mark timer as notified"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE workout_timer SET notification_sent = 1 WHERE id = ?", (timer_id,))
            conn.commit()
    
    def update_user_profile(self, tg_user_id: int, **kwargs):
        """Update user profile with new fields - SECURE: filters None/empty values"""
        # Разрешенные поля для обновления
        allowed_fields = [
            'schedule_days', 'preferred_time_local', 'goal', 'experience', 'equipment', 'injuries',
            'age', 'gender', 'height', 'weight', 'activity_level', 'goal_deadline', 'priorities',
            'chronic_diseases', 'doctor_approval', 'preferred_exercises', 'workout_duration', 'motivation',
            'workout_time', 'health_restrictions', 'training_style'
        ]
        
        fields = []
        values = []
        for key, value in kwargs.items():
            if key in allowed_fields:
                # SECURITY: Only update if value is not None and not empty string
                if value is not None and str(value).strip() != '':
                    # Basic type validation
                    if key == 'age' and not isinstance(value, int):
                        try:
                            value = int(value)
                        except (ValueError, TypeError):
                            logger.warning(f"Invalid age value: {value}, skipping")
                            continue
                    elif key in ['height', 'weight'] and not isinstance(value, (int, float)):
                        try:
                            value = float(value)
                        except (ValueError, TypeError):
                            logger.warning(f"Invalid {key} value: {value}, skipping")
                            continue
                    
                    fields.append(f"{key} = ?")
                    values.append(value)
        
        if fields:
            values.append(tg_user_id)
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(f"UPDATE users SET {', '.join(fields)} WHERE tg_user_id = ?", values)
                conn.commit()
                logger.info(f"Updated user {tg_user_id} profile: {list(kwargs.keys())}")
    
    def delete_user_data(self, tg_user_id: int):
        """Complete user data deletion"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            # Delete in correct order due to foreign keys
            cursor.execute("DELETE FROM workout_timer WHERE tg_user_id = ?", (tg_user_id,))
            cursor.execute("DELETE FROM notes WHERE tg_user_id = ?", (tg_user_id,))
            cursor.execute("DELETE FROM weekly_stats WHERE tg_user_id = ?", (tg_user_id,))
            cursor.execute("DELETE FROM prs WHERE tg_user_id = ?", (tg_user_id,))
            cursor.execute("DELETE FROM sets WHERE workout_id IN (SELECT workout_id FROM workouts WHERE tg_user_id = ?)", (tg_user_id,))
            cursor.execute("DELETE FROM workouts WHERE tg_user_id = ?", (tg_user_id,))
            cursor.execute("DELETE FROM workout_programs WHERE tg_user_id = ?", (tg_user_id,))
            cursor.execute("DELETE FROM users WHERE tg_user_id = ?", (tg_user_id,))
            conn.commit()


class SetParser:
    """Intelligent set parser for various formats"""

    @staticmethod
    def normalize_exercise_name(exercise: str) -> str:
        """Normalize exercise name using aliases"""
        exercise_lower = exercise.lower().strip()
        return EXERCISE_ALIASES.get(exercise_lower, exercise.lower())
    
    @staticmethod
    def get_exercise_group(exercise_name: str) -> str:
        """Get muscle group for exercise"""
        return classify_exercise(exercise_name)

    @staticmethod
    def parse_sets(text: str) -> List[Dict]:
        """Parse sets from text input"""
        sets = []

        # Regex patterns for different formats (order matters!)
        patterns = [
            # Pattern 1: Exercise 60x5x3@8 (weight x reps x sets @ RPE)
            r'(\w+(?:\s+\w+)*)\s+(\d+(?:[.,]\d+)?)\s*[x×]\s*(\d+)\s*[x×]\s*(\d+)(?:\s*@\s*(\d+(?:[.,]\d+)?))?',
            # Pattern 2: Exercise 3x5x60@8 (sets x reps x weight @ RPE) - but we need to be careful here
            r'(\w+(?:\s+\w+)*)\s+(\d+)\s*[x×]\s*(\d+)\s*[x×]\s*(\d+(?:[.,]\d+)?)(?:\s*@\s*(\d+(?:[.,]\d+)?))?',
            # Pattern 3: Exercise 60x5@8 (weight x reps @ RPE) - this should match single sets
            r'(\w+(?:\s+\w+)*)\s+(\d+(?:[.,]\d+)?)\s*[x×]\s*(\d+)(?:\s*@\s*(\d+(?:[.,]\d+)?))?',
        ]

        lines = text.strip().split('\n')

        for line in lines:
            line = line.strip()
            if not line:
                continue

            parsed = False

            for pattern in patterns:
                match = re.match(pattern, line, re.IGNORECASE)
                if match:
                    groups = match.groups()

                    # Determine pattern type and parse accordingly
                    exercise = SetParser.normalize_exercise_name(groups[0])

                    # Check if we have 3 numbers (weight x reps x sets OR sets x reps x weight)
                    if len(groups) >= 4 and groups[
                            3] is not None and not groups[3].replace(
                                '.', '').replace(',', '').isdigit() == False:
                        # This is format: Exercise 60x5x3@8 (weight x reps x sets)
                        # OR: Exercise 3x5x60@8 (sets x reps x weight)

                        val1 = float(groups[1].replace(',', '.'))
                        val2 = int(groups[2])
                        val3 = float(groups[3].replace(',', '.'))
                        rpe = float(groups[4].replace(
                            ',',
                            '.')) if len(groups) > 4 and groups[4] else None

                        # Heuristic: if val1 is small (< 20) and val3 is large (> 20), it's likely sets x reps x weight
                        # Otherwise, it's weight x reps x sets
                        if val1 <= 20 and val3 > 20:
                            # Format: sets x reps x weight
                            sets_count = int(val1)
                            reps = val2
                            weight = val3
                        else:
                            # Format: weight x reps x sets
                            weight = val1
                            reps = val2
                            sets_count = int(val3)

                        for i in range(sets_count):
                            sets.append({
                                'exercise': exercise,
                                'weight': weight,
                                'reps': reps,
                                'set_idx': i + 1,
                                'rpe': rpe,
                                'comment': ''
                            })
                    else:
                        # Format: Exercise 60x5@8 (weight x reps)
                        weight = float(groups[1].replace(',', '.'))
                        reps = int(groups[2])
                        rpe = float(groups[3].replace(
                            ',',
                            '.')) if len(groups) > 3 and groups[3] else None

                        sets.append({
                            'exercise': exercise,
                            'weight': weight,
                            'reps': reps,
                            'set_idx': 1,
                            'rpe': rpe,
                            'comment': ''
                        })

                    parsed = True
                    break

            if not parsed:
                logger.warning(f"Could not parse line: {line}")

        return sets

    @staticmethod
    def calculate_tonnage(sets: List[Dict]) -> float:
        """Calculate total tonnage from sets"""
        return sum(s['weight'] * s['reps'] for s in sets)




class AICoach:
    """AI-powered fitness coach using ChatGPT"""
    
    def __init__(self):
        self.client = openai_client
    
    async def _make_openai_request(self, messages: List[Dict], temperature: float = 0.7, 
                                   json_mode: bool = True) -> Optional[Dict]:
        """Make OpenAI API request with retry logic and error handling"""
        if not self.client:
            logger.error("OpenAI client not initialized")
            return None
            
        for attempt in range(MAX_RETRIES):
            try:
                request_params = {
                    "model": OPENAI_MODEL,
                    "messages": messages,
                    "temperature": temperature,
                    "timeout": 30.0
                }
                
                if json_mode:
                    request_params["response_format"] = {"type": "json_object"}
                
                response = self.client.chat.completions.create(**request_params)
                
                if not response.choices or not response.choices[0].message.content:
                    raise ValueError("Empty response from OpenAI")
                
                content = response.choices[0].message.content
                
                if json_mode:
                    try:
                        return json.loads(content)
                    except json.JSONDecodeError as e:
                        logger.warning(f"Invalid JSON response: {e}")
                        return {"error": "Invalid JSON response", "raw_content": content}
                else:
                    return {"content": content}
                    
            except Exception as e:
                logger.warning(f"OpenAI request attempt {attempt + 1} failed: {e}")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY * (2 ** attempt))  # Exponential backoff
                else:
                    logger.error(f"All OpenAI request attempts failed: {e}")
                    return None
        
        return None
    
    async def generate_workout_program(self, user_data: Dict) -> Dict:
        """Generate a personalized workout program using ChatGPT"""
        try:
            prompt = f"""
            Create a personalized workout program for this user profile:
            
            Goal: {user_data.get('goal', 'general fitness')}
            Experience: {user_data.get('experience', 'beginner')}
            Equipment: {user_data.get('equipment', 'gym')}
            Injuries: {user_data.get('injuries', 'none')}
            Training days: {user_data.get('schedule_days', '3 days per week')}
            Training style: {user_data.get('training_style', 'not specified')}
            Weight: {user_data.get('weight', 'not specified')}
            Age: {user_data.get('age', 'not specified')}
            Height: {user_data.get('height', 'not specified')}
            Medical restrictions: {user_data.get('chronic_diseases', 'none')}
            Problem areas: {user_data.get('health_restrictions', 'none')}
            Workout time: {user_data.get('workout_time', 'not specified')}
            Motivation: {user_data.get('motivation', 'general health')}
            
            TRAINING STYLE REQUIREMENTS:
            - Сплит: Каждая мышечная группа 1 раз в неделю, высокий объём за тренировку, 4-6 дней тренировок
            - Фулбоди: Все основные мышцы 3 раза в неделю, умеренный объём, 3-4 дня тренировок
            
            CRITICAL EXERCISE ADAPTATIONS for problem areas:
            - Плечи/плечевые суставы: избегать подъёмы рук выше головы, предпочитать нейтральный хват, малые амплитуды
            - Спина/позвоночник: избегать наклоны с весом, предпочитать упражнения с поддержкой, нейтральное положение позвоночника
            - Колени/коленные суставы: избегать глубокие приседания, предпочитать частичные амплитуды, поддержка коленей
            
            MANDATORY RUSSIAN EXERCISE FORMAT: All exercise names MUST be in Russian with format "Название упражнения: подходы x повторы"
            Examples: "Жим лёжа: 3x8-10", "Приседания: 4x6-8", "Тяга штанги в наклоне: 3x10-12"
            
            IMPORTANT: Respond in Russian language for all user-facing content.
            
            Create a program based on TRAINING STYLE:
            
            СПЛИТ STRUCTURE (if training_style = 'Сплит'):
            - 4-6 дней в неделю, каждая группа мышц 1 раз
            - Высокий объём за тренировку (12-20 подходов на группу)
            - Разделение: Грудь/Трицепс, Спина/Бицепс, Ноги, Плечи, и т.д.
            
            ФУЛБОДИ STRUCTURE (if training_style = 'Фулбоди'):
            - 3-4 дня в неделю, все основные группы каждый день
            - Умеренный объём (6-12 подходов на группу за тренировку)
            - Базовые движения: приседы, жим, тяги в каждой тренировке
            
            MANDATORY REQUIREMENTS:
            1. Exercise names ONLY in Russian: "Название упражнения: подходы x повторы"
            2. Adapt for problem areas if specified
            3. Конкретные веса в килограммах для каждого упражнения
            4. Structure according to selected training style
            
            Respond in this JSON format with Russian exercise names:
            {{
                "name": "Программа тренировок (Сплит/Фулбоди)",
                "description": "Персональная программа с учётом стиля тренировок и ограничений здоровья",
                "duration_weeks": 8,
                "workouts": [
                    {{
                        "day": "Понедельник",
                        "name": "Грудь и трицепс",
                        "exercises": [
                            {{
                                "name": "Жим лёжа: 3x8-10",
                                "sets": 3,
                                "reps": "8-10",
                                "weight_kg": "40-50",
                                "notes": "Сосредоточьтесь на технике. При проблемах с плечами - уменьшите амплитуду"
                            }},
                            {{
                                "name": "Приседания: 4x6-8",
                                "sets": 4,
                                "reps": "6-8",
                                "weight_kg": "60-70",
                                "notes": "При проблемах с коленями - частичная амплитуда"
                            }}
                        ]
                    }}
                ]
            }}
            """
            
            messages = [
                {"role": "system", "content": TRAINER_SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ]
            
            program_data = await self._make_openai_request(messages, temperature=0.7, json_mode=True)
            
            if program_data is None:
                raise ValueError("Failed to get response from OpenAI")
            
            # Validate required fields
            required_fields = ["name", "description"]
            if not all(field in program_data for field in required_fields):
                logger.warning("Generated program missing required fields")
                program_data.setdefault("name", "Generated Program")
                program_data.setdefault("description", "AI-generated workout program")
            
            return program_data
            
        except Exception as e:
            logger.error(f"Error generating workout program: {e}")
            return {
                "name": "Basic Program",
                "description": "Basic strength training program",
                "error": str(e)
            }
    
    async def parse_workout_text(self, text: str) -> Dict:
        """Parse workout text using ChatGPT to extract structured data"""
        try:
            prompt = f"""
            Parse this workout log and extract structured data:
            
            "{text}"
            
            Extract all exercises with their sets, reps, weight, and any RPE mentioned.
            Normalize exercise names to common English terms.
            
            Respond in this JSON format:
            {{
                "exercises": [
                    {{
                        "name": "bench press",
                        "sets": [
                            {{
                                "weight": 60.0,
                                "reps": 8,
                                "rpe": 7.5,
                                "notes": ""
                            }}
                        ]
                    }}
                ],
                "notes": "General workout notes",
                "confidence": 0.95
            }}
            """
            
            messages = [
                {"role": "system", "content": TRAINER_SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ]
            
            parsed_data = await self._make_openai_request(messages, temperature=0.3, json_mode=True)
            
            if parsed_data is None:
                raise ValueError("Failed to parse workout text")
            
            # Validate response structure
            if "exercises" not in parsed_data:
                logger.warning("AI response missing 'exercises' field")
                parsed_data["exercises"] = []
                parsed_data["confidence"] = 0.0
            
            return parsed_data
            
        except Exception as e:
            logger.error(f"Error parsing workout text: {e}")
            return {"exercises": [], "error": str(e)}
    
    async def get_workout_recommendations(self, workout_data: Dict, user_history: List[Dict]) -> str:
        """Get AI-powered workout recommendations"""
        try:
            prompt = f"""
            Analyze this completed workout and provide recommendations:
            
            Current workout:
            - Duration: {workout_data.get('duration_min', 0)} minutes
            - sRPE: {workout_data.get('sRPE', 5)}/10
            - Fatigue: {workout_data.get('fatigue', 5)}/10
            - Sleep quality: {workout_data.get('sleep_quality', 3)}/5
            - Training load: {workout_data.get('training_load', 0)}
            
            Recent workout history (last 4 weeks):
            {json.dumps(user_history[-12:] if user_history else [], indent=2)}
            
            Provide specific recommendations for:
            1. Next workout adjustments (weight, volume, intensity)
            2. Recovery suggestions
            3. Progressive overload guidance
            4. Any deload recommendations if needed
            
            Keep response under 200 words, be specific and actionable.
            """
            
            messages = [
                {"role": "system", "content": TRAINER_SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ]
            
            response_data = await self._make_openai_request(messages, temperature=0.5, json_mode=False)
            
            if response_data is None:
                raise ValueError("Failed to get recommendations")
            
            return response_data.get("content", "Unable to generate recommendations at this time.")
            
        except Exception as e:
            logger.error(f"Error getting recommendations: {e}")
            return "Unable to generate recommendations at this time."
    
    async def transcribe_voice_message(self, audio_file_path: str) -> str:
        """Transcribe voice message using Whisper (future enhancement)"""
        if not self.client:
            logger.error("OpenAI client not initialized")
            return ""
            
        try:
            with open(audio_file_path, "rb") as audio_file:
                response = self.client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file
                )
            return response.text
        except Exception as e:
            logger.error(f"Error transcribing voice: {e}")
            return ""


class TrainingAnalyzer:
    """Analyze training data and provide recommendations"""

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager

    def analyze_workout(self, tg_user_id: int, sRPE: int, sleep: int,
                        current_week_load: int) -> str:
        """Analyze workout and provide recommendations"""
        recommendations = []

        # sRPE-based recommendations
        if sRPE >= 9:
            recommendations.append(
                "⚠️ Очень высокая нагрузка! Снизьте вес на 5-7.5% или уберите 1-2 подхода."
            )
        elif sRPE >= 7:
            recommendations.append(
                "✅ Хорошая интенсивность. Продолжайте в том же духе!")
            # Check for plateau (simplified)
            recommendations.append(
                "💡 Если прогресс застрял более 2 недель, добавьте +2.5% в первом подходе."
            )
        elif sRPE <= 5:
            recommendations.append(
                "📈 Низкая нагрузка. Можете добавить +2.5-5% веса или +1 подход."
            )

        # Weekly overload check
        weekly_stats = self.db.get_weekly_stats(tg_user_id, 4)
        if len(weekly_stats) >= 4:
            avg_load = sum(w['training_load'] for w in weekly_stats[-4:]) / 4
            if current_week_load > avg_load * 1.25:
                recommendations.append(
                    "🔄 Недельная нагрузка выросла >25%. Рекомендуется разгрузка (-20-30% объема)."
                )

        # Sleep-based recommendations
        if sleep <= 2 and sRPE >= 8:
            recommendations.append(
                "😴 Плохой сон + высокая нагрузка. Сосредоточьтесь на восстановлении!"
            )

        return "\n".join(
            recommendations
        ) if recommendations else "Хорошая тренировка! Продолжайте!"


class FitnessBot:
    """Main Telegram bot class"""

    def __init__(self):
        self.token = os.environ.get('TELEGRAM_BOT_TOKEN')
        if not self.token:
            raise ValueError("TELEGRAM_BOT_TOKEN not found in environment")

        self.bot = Bot(token=self.token,
                       default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.dp = Dispatcher()
        self.db = DatabaseManager()
        self.analyzer = TrainingAnalyzer(self.db)
        self.ai_coach = AICoach()
        self.scheduler = AsyncIOScheduler(timezone=timezone(DEFAULT_TIMEZONE))

        # User state tracking
        self.user_states = {}

        self.setup_handlers()
        self.setup_scheduler()
        self.setup_existing_user_notifications()
    
    def calculate_workout_tonnage(self, workout_id: str) -> Dict[str, float]:
        """Calculate tonnage by muscle groups for a specific workout"""
        with sqlite3.connect(self.db.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT muscle_group, weight, reps
                FROM sets 
                WHERE workout_id = ?
                AND muscle_group IS NOT NULL
            """, (workout_id,))
            
            sets_data = cursor.fetchall()
        
        # Calculate tonnage by groups for this workout
        tonnage_by_group = {}
        total_tonnage = 0
        
        for muscle_group, weight, reps in sets_data:
            if muscle_group not in tonnage_by_group:
                tonnage_by_group[muscle_group] = 0
            
            set_tonnage = weight * reps
            tonnage_by_group[muscle_group] += set_tonnage
            total_tonnage += set_tonnage
        
        # Round values
        for group in tonnage_by_group:
            tonnage_by_group[group] = round(tonnage_by_group[group], 1)
        
        tonnage_by_group['total'] = round(total_tonnage, 1)
        return tonnage_by_group
    
    def format_tonnage_report(self, workout_tonnage: Dict[str, float], weekly_tonnage: Dict) -> str:
        """Format tonnage report for post-workout message"""
        if not workout_tonnage or workout_tonnage.get('total', 0) == 0:
            return "💪 Тоннаж тренировки: данных нет"
        
        # Format workout tonnage
        workout_total = workout_tonnage.get('total', 0)
        report = f"💪 Тоннаж тренировки: {workout_total} кг\n"
        
        # Group breakdown for workout
        for group, tonnage in workout_tonnage.items():
            if group != 'total' and tonnage > 0:
                report += f"  • {group}: {tonnage} кг\n"
        
        # Weekly comparison if available
        if weekly_tonnage and 'total' in weekly_tonnage:
            weekly_total = weekly_tonnage['total']
            report += f"\n📊 Неделя (7 дней): {weekly_total} кг"
            
            # Show weekly breakdown by groups
            for group_name in ['Ноги', 'Жимы', 'Тяги']:
                if group_name in weekly_tonnage:
                    group_data = weekly_tonnage[group_name]
                    tonnage = group_data.get('tonnage', 0)
                    percentage = group_data.get('percentage', 0)
                    if tonnage > 0:
                        report += f"\n  • {group_name}: {tonnage} кг ({percentage} процентов)"
        
        return report
    
    def get_weekly_prs(self, tg_user_id: int) -> List[Dict]:
        """Get personal records achieved this calendar week"""
        with sqlite3.connect(self.db.db_path) as conn:
            cursor = conn.cursor()
            
            # Get this calendar week's sets (Monday to Sunday)
            today = datetime.now()
            week_start = today - timedelta(days=today.weekday())
            cursor.execute("""
                SELECT s.exercise, s.weight, s.reps, w.dt_start
                FROM sets s
                JOIN workouts w ON s.workout_id = w.workout_id
                WHERE w.tg_user_id = ?
                AND datetime(w.dt_start) >= datetime(?)
                ORDER BY s.exercise, s.weight DESC
            """, (tg_user_id, week_start.isoformat()))
            
            weekly_sets = cursor.fetchall()
            
            # Get all-time records before this week with exercise name normalization
            cursor.execute("""
                SELECT s.exercise, MAX(s.weight) as max_weight
                FROM sets s
                JOIN workouts w ON s.workout_id = w.workout_id
                WHERE w.tg_user_id = ?
                AND datetime(w.dt_start) < datetime(?)
                GROUP BY s.exercise
            """, (tg_user_id, week_start.isoformat()))
            
            # Normalize exercise names and build previous records dict
            prev_records = {}
            for exercise, max_weight in cursor.fetchall():
                normalized_name = EXERCISE_ALIASES.get(exercise.lower(), exercise)
                if normalized_name not in prev_records or max_weight > prev_records[normalized_name]:
                    prev_records[normalized_name] = max_weight
        
        # Find new PRs with exercise name normalization
        weekly_prs = []
        checked_exercises = set()
        
        for exercise, weight, reps, dt_start in weekly_sets:
            # Normalize exercise name for comparison
            normalized_name = EXERCISE_ALIASES.get(exercise.lower(), exercise)
            
            if normalized_name in checked_exercises:
                continue
            
            prev_max = prev_records.get(normalized_name, 0)
            if weight > prev_max:
                weekly_prs.append({
                    'exercise': exercise,  # Keep original name for display
                    'weight': weight,
                    'reps': reps,
                    'previous': prev_max,
                    'date': dt_start
                })
                checked_exercises.add(normalized_name)
        
        return weekly_prs
    
    def get_health_flags(self, tg_user_id: int, weekly_stats: List[Dict], total_load: int) -> List[str]:
        """Analyze health indicators and return warning flags"""
        flags = []
        
        # Fixed overload check - compare current week's total load with average of previous 3 complete weeks
        try:
            with sqlite3.connect(self.db.db_path) as conn:
                cursor = conn.cursor()
                
                # Get current week start (Monday)
                today = datetime.now()
                current_week_start = today - timedelta(days=today.weekday())
                
                # Get sum of training_load for previous 3 complete weeks
                cursor.execute("""
                    SELECT COALESCE(SUM(sum_load), 0) as total_load
                    FROM weekly_stats 
                    WHERE tg_user_id = ?
                    AND date(week_start) < date(?)
                    ORDER BY week_start DESC
                    LIMIT 3
                """, (tg_user_id, current_week_start.strftime('%Y-%m-%d')))
                
                result = cursor.fetchone()
                prev_3_weeks_total = result[0] if result and result[0] else 0
                
                # Calculate average weekly load from previous 3 weeks
                if prev_3_weeks_total > 0:
                    avg_prev_weekly_load = prev_3_weeks_total / 3
                    if total_load > avg_prev_weekly_load * 1.25:
                        flags.append("⚠️ Перегрузка: нагрузка выросла >25%")
        except Exception as e:
            logger.error(f"Error in overload check: {e}")
            # Fallback to old logic if database query fails
            if len(self.db.get_weekly_stats(tg_user_id, 4)) >= 4:
                prev_stats = self.db.get_weekly_stats(tg_user_id, 4)[:-len(weekly_stats)]
                if prev_stats:
                    avg_prev_load = sum(w['training_load'] for w in prev_stats if w['training_load']) / len(prev_stats)
                    if total_load > avg_prev_load * 1.25:
                        flags.append("⚠️ Перегрузка: нагрузка выросла >25%")
        
        # Sleep quality check
        if weekly_stats:
            avg_sleep = sum(w['sleep'] for w in weekly_stats if w['sleep']) / len([w for w in weekly_stats if w['sleep']]) if weekly_stats else 0
            if avg_sleep < 3.0:
                flags.append("😴 Плохой сон: среднее качество <3/5")
        
        # Mood check
        if weekly_stats:
            avg_mood = sum(w['mood'] for w in weekly_stats if w['mood']) / len([w for w in weekly_stats if w['mood']]) if weekly_stats else 0
            if avg_mood < 3.0:
                flags.append("😔 Низкое настроение: среднее <3/5")
        
        # High sRPE check
        if weekly_stats:
            avg_srpe = sum(w['sRPE'] for w in weekly_stats if w['sRPE']) / len([w for w in weekly_stats if w['sRPE']]) if weekly_stats else 0
            if avg_srpe > 7.5:
                flags.append("🔥 Высокая интенсивность: sRPE >7.5")
        
        # Training frequency check
        if len(weekly_stats) > 6:
            flags.append("⏰ Частые тренировки: >6 за неделю")
        elif len(weekly_stats) < 2:
            flags.append("📉 Низкая активность: <2 тренировок")
        
        return flags
    
    def generate_weekly_recommendations(self, weekly_tonnage: Dict, health_flags: List[str], weekly_prs: List[Dict], total_workouts: int) -> str:
        """Generate personalized weekly recommendations based on analysis"""
        recommendations = []
        
        # Training balance recommendations with correct thresholds
        if weekly_tonnage and 'total' in weekly_tonnage and weekly_tonnage['total'] > 0:
            group_percentages = {}
            for group in ['Ноги', 'Жимы', 'Тяги']:
                if group in weekly_tonnage:
                    group_percentages[group] = weekly_tonnage[group].get('percentage', 0)
            
            # Check for imbalances using correct thresholds (<25% or >45%)
            if group_percentages:
                for group, percentage in group_percentages.items():
                    if percentage < 25 and percentage > 0:  # Too low percentage
                        recommendations.append(f"• Увеличить долю упражнений на {group} (сейчас {percentage:.1f} процентов, нужно больше 25 процентов)")
                    elif percentage > 45:  # Too high percentage
                        recommendations.append(f"• Снизить долю упражнений на {group} (сейчас {percentage:.1f} процентов, нужно меньше 45 процентов)")
                
                # Additional check for overall balance
                max_group = max(group_percentages, key=group_percentages.get)
                min_group = min(group_percentages, key=group_percentages.get)
                
                if group_percentages[max_group] - group_percentages[min_group] > 30:
                    recommendations.append(f"• Добавить больше упражнений на {min_group} для баланса")
        
        # Health-based recommendations
        for flag in health_flags:
            if "Перегрузка" in flag:
                recommendations.append("• Рассмотрите деload неделю с -20% нагрузки")
            elif "Плохой сон" in flag:
                recommendations.append("• Улучшите гигиену сна: режим, темнота, прохлада")
            elif "Низкое настроение" in flag:
                recommendations.append("• Добавьте активности на свежем воздухе")
            elif "Высокая интенсивность" in flag:
                recommendations.append("• Включите больше легких тренировок (sRPE 5-6)")
            elif "Частые тренировки" in flag:
                recommendations.append("• Обязательно планируйте дни отдыха")
            elif "Низкая активность" in flag:
                recommendations.append("• Увеличьте частоту тренировок до 3-4 раз в неделю")
        
        # PR-based motivation
        if weekly_prs:
            recommendations.append("• Отличная работа с рекордами! Продолжайте прогрессию")
        elif total_workouts >= 3:
            recommendations.append("• Попробуйте увеличить веса на 2.5-5 кг в основных упражнениях")
        
        # General recommendations
        if total_workouts >= 4 and not any("Перегрузка" in flag for flag in health_flags):
            recommendations.append("• Стабильный режим тренировок - продолжайте в том же духе!")
        
        return '\n'.join(recommendations) if recommendations else "Продолжайте тренироваться регулярно и следите за восстановлением!"

    def setup_handlers(self):
        """Setup bot command and message handlers"""
        self.dp.message.register(self.cmd_start, CommandStart())
        self.dp.message.register(self.cmd_train, Command("train"))
        self.dp.message.register(self.cmd_add, Command("add"))
        self.dp.message.register(self.cmd_finish, Command("finish"))
        self.dp.message.register(self.cmd_week, Command("week"))
        self.dp.message.register(self.cmd_export, Command("export"))
        self.dp.message.register(self.cmd_delete_me, Command("delete_me"))
        self.dp.message.register(self.handle_voice,
                                 lambda msg: msg.voice is not None)
        self.dp.message.register(
            self.handle_text,
            lambda msg: msg.text and not msg.text.startswith('/'))
        self.dp.callback_query.register(self.handle_callback)

    def setup_scheduler(self):
        """Setup scheduled tasks and notifications"""
        # Weekly reports every Sunday at 20:00 Moscow time
        self.scheduler.add_job(
            self.send_weekly_reports,
            'cron',
            day_of_week=6,  # Sunday
            hour=20,
            minute=0,
            timezone=timezone(DEFAULT_TIMEZONE))

        # Daily database backups at 02:30 UTC
        self.scheduler.add_job(self.backup_database,
                               'cron',
                               hour=2,
                               minute=30,
                               timezone=timezone('UTC'))
        
        # Daily notification scheduler - checks every hour for pending notifications
        self.scheduler.add_job(
            self.check_workout_notifications,
            'cron',
            minute=0,  # Every hour at minute 0
            timezone=timezone(DEFAULT_TIMEZONE))
        
        # Workout timer checker - runs every 5 minutes
        self.scheduler.add_job(
            self.check_workout_timers,
            'interval',
            minutes=5)

    async def cmd_start(self, message: Message):
        """Enhanced /start command with program creation wizard"""
        if not message.from_user:
            await message.answer("❌ Не удалось определить пользователя.")
            return
            
        tg_user_id = message.from_user.id
        username = message.from_user.first_name or message.from_user.username or str(
            tg_user_id)

        try:
            # Check if user already exists
            user = self.db.get_user(tg_user_id)
            if user:
                # Show enhanced welcome for existing users
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🏋️ Начать тренировку", callback_data="start_workout")],
                    [InlineKeyboardButton(text="👀 Моя тренировка", callback_data="view_workout")],
                    [InlineKeyboardButton(text="📊 Недельный отчет", callback_data="weekly_report")],
                    [InlineKeyboardButton(text="🎯 Создать новую программу", callback_data="create_program")],
                    [InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings")]
                ])
                
                await message.answer(
                    f"Добро пожаловать обратно, {username}! 💪\n\n"
                    "🤖 <b>AI Fitness Coach готов к работе!</b>\n\n"
                    "Доступные команды:\n"
                    "• /train - начать тренировку\n"
                    "• /add - добавить подходы\n"
                    "• /finish - завершить тренировку\n"
                    "• /week - недельный отчет\n"
                    "• /export - экспорт данных\n"
                    "• /delete_me - удалить аккаунт\n\n"
                    "Или используйте кнопки ниже:",
                    reply_markup=keyboard
                )
                return

            # Start setup wizard for new users
            await message.answer("Настраиваю ваш аккаунт... ⏳")

            # Create basic user account
            self.db.create_user(tg_user_id, tz="Europe/Moscow")

            # Start program creation wizard
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🎯 Создать программу с ИИ", callback_data="wizard_create")],
                [InlineKeyboardButton(text="📤 Загрузить свою программу", callback_data="wizard_upload")],
                [InlineKeyboardButton(text="⏭️ Пропустить (пока без программы)", callback_data="wizard_skip")]
            ])

            await message.answer(
                f"🎉 Добро пожаловать в AI Fitness Coach, {username}!\n\n"
                f"🤖 <b>Ваш персональный тренер с ИИ готов!</b>\n\n"
                "✨ <b>Что умеет ваш AI-тренер:</b>\n"
                "• Создает персональные программы тренировок\n"
                "• Анализирует ваши тренировки в реальном времени\n"
                "• Дает рекомендации по прогрессии\n"
                "• Отправляет напоминания о тренировках\n"
                "• Экспортирует детальную аналитику\n\n"
                "Давайте настроим вашу программу тренировок:",
                reply_markup=keyboard
            )

            # Set user state for wizard
            self.user_states[tg_user_id] = {
                'step': 'program_choice',
                'wizard_data': {}
            }

        except Exception as e:
            logger.error(f"Error in start command: {e}")
            await message.answer(
                "❌ Ошибка при настройке аккаунта. Попробуйте еще раз.")

    async def cmd_train(self, message: Message):
        """Handle /train command"""
        if not message.from_user:
            await message.answer("❌ Не удалось определить пользователя.")
            return
            
        tg_user_id = message.from_user.id
        logger.info(f"User {tg_user_id} starting workout")

        try:
            # Check if user exists
            user = self.db.get_user(tg_user_id)
            logger.info(f"User check result: {user is not None}")
            
            if not user:
                await message.answer(
                    "🚀 Сначала используйте команду /start для настройки аккаунта.")
                return

            # Check for active workout
            active_workout = self.db.get_active_workout(tg_user_id)
            logger.info(f"Active workout check: {active_workout}")
            
            if active_workout:
                await message.answer(
                    f"⚠️ У вас уже есть активная тренировка: {active_workout}\n"
                    "Завершите её командой /finish или добавляйте подходы командой /add"
                )
                return

            # Create new workout
            workout_id = f"w_{tg_user_id}_{int(datetime.now().timestamp())}"
            dt_start = datetime.now().isoformat()
            logger.info(f"Creating workout: {workout_id}")

            self.db.create_workout(workout_id, tg_user_id, dt_start)
            logger.info(f"Workout created successfully: {workout_id}")

            await message.answer(
                f"💪 Тренировка начата!\n"
                f"🆔 ID: {workout_id}\n"
                f"⏰ Время начала: {datetime.now().strftime('%H:%M')}\n\n"
                "📝 Добавляйте подходы командой /add или просто отправьте сообщение:\n"
                "• Жим лёжа 60 кг 5 подходов по 8 раз\n"
                "• Bench 60x5x3@8\n"
                "• Или опишите тренировку своими словами")
                
        except Exception as e:
            logger.error(f"Error in cmd_train for user {tg_user_id}: {e}")
            await message.answer(f"❌ Произошла ошибка при создании тренировки: {e}")

    async def cmd_add(self, message: Message):
        """Handle /add command"""
        if not message.from_user:
            return
        tg_user_id = message.from_user.id

        # Get active workout
        workout_id = self.db.get_active_workout(tg_user_id)
        if not workout_id:
            await message.answer(
                "Нет активной тренировки. Начните с команды /train")
            return

        # Get text after /add
        text = (message.text or '').replace('/add', '').strip()
        if not text:
            await message.answer("Добавьте подходы после команды /add\n"
                                 "Например: /add Жим 60x5x3\n"
                                 "Или: /add Bench 60x5@8")
            return

        await self.process_sets(message, text, workout_id)

    async def process_sets(self, message: Message, text: str, workout_id: str):
        """Process and add sets to workout with AI fallback"""
        try:
            # First try regex parsing
            sets = SetParser.parse_sets(text)

            # If regex parsing fails, try AI parsing
            if not sets:
                logger.info(f"Regex parsing failed for: {text}, trying AI parsing")
                ai_result = await self.ai_coach.parse_workout_text(text)
                
                if ai_result and 'exercises' in ai_result and ai_result['exercises']:
                    # Convert AI result to our format
                    sets = []
                    set_idx = 1
                    for exercise in ai_result['exercises']:
                        for set_data in exercise.get('sets', []):
                            # Safe conversion with fallback for None values
                            weight = set_data.get('weight') or 0
                            reps = set_data.get('reps') or 0
                            
                            # Ensure we have valid numeric values
                            try:
                                weight = float(weight) if weight is not None else 0.0
                                reps = int(reps) if reps is not None else 0
                            except (ValueError, TypeError):
                                logger.warning(f"Invalid weight/reps in AI response: weight={weight}, reps={reps}")
                                weight = 0.0
                                reps = 0
                            
                            sets.append({
                                'exercise': exercise['name'],
                                'weight': weight,
                                'reps': reps,
                                'set_idx': set_idx,
                                'rpe': float(set_data.get('rpe')) if set_data.get('rpe') is not None else None,
                                'comment': set_data.get('notes', '')
                            })
                            set_idx += 1
                    
                    if sets:
                        await message.answer(f"🤖 AI распознал: {len(sets)} подход(ов)")

            if not sets:
                await message.answer(
                    "❌ Не удалось распознать подходы. Попробуйте:\n"
                    "• Жим лёжа 60 кг 5 подходов по 8 раз\n"
                    "• Bench press 60x5x3@8\n"
                    "• Или опишите тренировку своими словами")
                return

            # Add sets to database
            user = self.db.get_user(message.from_user.id) if message.from_user else None
            for set_data in sets:
                set_data['tg_user_id'] = message.from_user.id if message.from_user else 0
                self.db.add_set(workout_id, set_data['exercise'],
                                set_data['weight'], set_data['reps'],
                                set_data['set_idx'], set_data.get('rpe'),
                                set_data.get('comment', ''))

            # Sets are already stored in SQLite database

            # Format response
            tonnage = SetParser.calculate_tonnage(sets)
            sets_summary = []

            current_exercise = None
            exercise_sets = []

            for set_data in sets:
                if current_exercise != set_data['exercise']:
                    if exercise_sets:
                        sets_summary.append(
                            f"{current_exercise}: {', '.join(exercise_sets)}")
                        exercise_sets = []
                    current_exercise = set_data['exercise']

                set_str = f"{set_data['weight']}{user.get('unit', 'kg') if user else 'kg'}×{set_data['reps']}"
                if set_data['rpe']:
                    set_str += f"@{set_data['rpe']}"
                exercise_sets.append(set_str)

            if exercise_sets:
                sets_summary.append(
                    f"{current_exercise}: {', '.join(exercise_sets)}")

            await message.answer(
                f"✅ Подходы добавлены!\n\n"
                f"{chr(10).join(sets_summary)}\n\n"
                f"📊 Тоннаж: {tonnage:.1f} {user.get('unit', 'kg') if user else 'kg'}\n"
                f"Продолжайте добавлять подходы или используйте /finish для завершения."
            )

        except Exception as e:
            logger.error(f"Error processing sets: {e}")
            await message.answer(
                "❌ Ошибка при добавлении подходов. Попробуйте еще раз.")

    async def cmd_finish(self, message: Message):
        """Handle /finish command"""
        if not message.from_user:
            return
        tg_user_id = message.from_user.id

        workout_id = self.db.get_active_workout(tg_user_id)
        if not workout_id:
            await message.answer("Нет активной тренировки для завершения.")
            return

        # Start finish workflow
        self.user_states[tg_user_id] = {
            'step': 'srpe',
            'workout_id': workout_id
        }

        # Create sRPE keyboard
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(text=str(i), callback_data=f"srpe_{i}")
                for i in range(1, 6)
            ],
                             [
                                 InlineKeyboardButton(
                                     text=str(i), callback_data=f"srpe_{i}")
                                 for i in range(6, 11)
                             ]])

        await message.answer(
            "🎯 Оцените субъективную нагрузку тренировки (sRPE) от 1 до 10:\n"
            "1-2: Очень легко\n"
            "3-4: Легко\n"
            "5-6: Умеренно\n"
            "7-8: Тяжело\n"
            "9-10: Максимально тяжело",
            reply_markup=keyboard)

    async def handle_callback(self, callback_query: types.CallbackQuery):
        """Handle inline keyboard callbacks"""
        tg_user_id = callback_query.from_user.id
        data = callback_query.data
        logger.info(f"Callback received from user {tg_user_id}: {data}")

        # Answer callback immediately to prevent timeout
        try:
            await callback_query.answer()
        except Exception as e:
            logger.warning(f"Failed to answer callback (may already be expired): {e}")

        try:
            # Handle main menu callbacks
            if data == "start_workout":
                logger.info(f"Starting workout via callback for user {tg_user_id}")
                await self.start_workout_callback(callback_query)
                return
            elif data == "view_workout":
                logger.info(f"Showing current workout via callback for user {tg_user_id}")
                await self.show_current_workout_callback(callback_query)
                return
            elif data == "weekly_report":
                logger.info(f"Showing weekly report via callback for user {tg_user_id}")
                await self.show_weekly_report_callback(callback_query)
                return
            elif data == "create_program":
                logger.info(f"Starting program creation via callback for user {tg_user_id}")
                await self.start_program_wizard(callback_query)
                return
            elif data == "settings":
                logger.info(f"Showing settings via callback for user {tg_user_id}")
                await self.show_settings_callback(callback_query)
                return
                
            # Handle program wizard callbacks
            elif data == "wizard_create":
                logger.info(f"Starting program wizard for user {tg_user_id}")
                await self.start_program_wizard(callback_query)
                return
            elif data == "wizard_upload":
                logger.info(f"Starting program upload for user {tg_user_id}")
                await self.start_program_upload(callback_query)
                return
            elif data == "wizard_skip":
                logger.info(f"Skipping program setup for user {tg_user_id}")
                await self.skip_program_setup(callback_query)
                return
        except Exception as e:
            logger.error(f"Error in callback handler: {e}")
            try:
                await callback_query.message.edit_text(f"❌ Ошибка: {e}")
            except:
                pass

        if tg_user_id not in self.user_states:
            try:
                await callback_query.message.edit_text(
                    "⏰ Сессия истекла. Начните заново с /start")
            except:
                pass
            return

        state = self.user_states[tg_user_id]

        if data and data.startswith('srpe_') and state['step'] == 'srpe':
            srpe = int(data.split('_')[1])
            state['sRPE'] = srpe
            state['step'] = 'mood'

            # Create mood keyboard
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=str(i), callback_data=f"mood_{i}")
                for i in range(1, 6)
            ]])

            if callback_query.message and hasattr(callback_query.message, 'edit_text'):
                try:
                    await callback_query.message.edit_text(
                        f"✅ sRPE: {srpe}\n\n"
                        "😊 Оцените настроение после тренировки от 1 до 5:\n"
                        "1: Ужасно\n"
                        "2: Плохо\n"
                        "3: Нормально\n"
                        "4: Хорошо\n"
                        "5: Отлично",
                        reply_markup=keyboard)
                except Exception as e:
                    logger.warning(f"Failed to edit message: {e}")
                    await callback_query.message.answer(
                        f"✅ sRPE: {srpe}\n\n"
                        "😊 Оцените настроение после тренировки от 1 до 5:\n"
                        "1: Ужасно\n2: Плохо\n3: Нормально\n4: Хорошо\n5: Отлично",
                        reply_markup=keyboard)

        elif data and data.startswith('mood_') and state['step'] == 'mood':
            mood = int(data.split('_')[1])
            state['mood'] = mood
            state['step'] = 'sleep'

            # Create sleep keyboard
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=str(i), callback_data=f"sleep_{i}")
                for i in range(1, 6)
            ]])

            if callback_query.message and hasattr(callback_query.message, 'edit_text'):
                try:
                    await callback_query.message.edit_text(
                        f"✅ sRPE: {state['sRPE']}\n"
                        f"✅ Настроение: {mood}\n\n"
                        "😴 Оцените качество сна прошлой ночью от 1 до 5:\n"
                        "1: Ужасный\n"
                        "2: Плохой\n"
                        "3: Нормальный\n"
                        "4: Хороший\n"
                        "5: Отличный",
                        reply_markup=keyboard)
                except Exception as e:
                    logger.warning(f"Failed to edit message: {e}")
                    await callback_query.message.answer(
                        f"✅ sRPE: {state['sRPE']}\n✅ Настроение: {mood}\n\n"
                        "😴 Оцените качество сна прошлой ночью от 1 до 5:\n"
                        "1: Ужасный\n2: Плохой\n3: Нормальный\n4: Хороший\n5: Отличный",
                        reply_markup=keyboard)

        elif data and data.startswith('sleep_') and state['step'] == 'sleep':
            sleep = int(data.split('_')[1])
            await self.finish_workout_process(callback_query, state, sleep)
            
        # Handle wizard goal selection
        # NEW EXTENDED WIZARD HANDLERS
        # Handle age selection
        elif data and data.startswith('age_') and state.get('step') == 'age':
            state['wizard_data']['age'] = data.replace('age_', '')
            await self.wizard_step_gender(callback_query, state)
            
        # Handle gender selection  
        elif data and data.startswith('gender_') and state.get('step') == 'gender':
            genders = {'gender_male': 'мужской', 'gender_female': 'женский'}
            state['wizard_data']['gender'] = genders.get(data, 'мужской')
            # Direct text input prompt for height - no fake message needed
            await callback_query.message.edit_text(
                f"📋 **СОЗДАНИЕ ПЕРСОНАЛЬНОЙ ПРОГРАММЫ**\n"
                f"Раздел 2/8: Физические параметры\n\n"
                f"✅ Возраст: {state['wizard_data'].get('age', '')}\n"
                f"✅ Пол: {state['wizard_data'].get('gender', '')}\n\n"
                f"📏 Укажите ваш рост в сантиметрах\n\n"
                f"📝 Введите рост числом (например: 175):",
                parse_mode=ParseMode.MARKDOWN
            )
            state['step'] = 'height'
            state['current_section'] = 2
            
        # Handle goal selection (updated)
        elif data and data.startswith('goal_') and state.get('step') == 'goal':
            goals = {
                'goal_mass': 'набор мышечной массы',
                'goal_weight_loss': 'похудение', 
                'goal_strength': 'увеличение силы',
                'goal_fitness': 'общая физподготовка',
                'goal_endurance': 'улучшение выносливости',
                'goal_health': 'здоровье спины и суставов'
            }
            state['wizard_data']['goal'] = goals.get(data or '', 'общая физподготовка')
            await self.wizard_step_experience(callback_query, state)
            
        # Handle experience selection (updated)
        elif data and data.startswith('exp_') and state.get('step') == 'experience':
            experiences = {
                'exp_beginner': 'новичок',
                'exp_intermediate': 'средний',
                'exp_advanced': 'продвинутый'
            }
            state['wizard_data']['experience'] = experiences.get(data or '', 'новичок')
            await self.wizard_step_schedule(callback_query, state)
            
        # Handle training style selection
        elif data and data.startswith('style_') and state.get('step') == 'training_style':
            if data == 'style_split':
                state['wizard_data']['training_style'] = 'Сплит'
                # Move to workout time prompt
                await callback_query.message.edit_text(
                    f"📋 **СОЗДАНИЕ ПЕРСОНАЛЬНОЙ ПРОГРАММЫ**\n"
                    f"Раздел 8/9: Время тренировок\n\n"
                    f"✅ Стиль: Сплит (высокая нагрузка)\n\n"
                    f"🕒 В какое время вы планируете тренироваться?\n"
                    f"(Это поможет настроить напоминания)\n\n"
                    f"📝 Введите время в формате ЧЧ:ММ (например: 18:30):",
                    parse_mode=ParseMode.MARKDOWN
                )
                state['step'] = 'workout_time'
                state['current_section'] = 8
            elif data == 'style_fullbody':
                state['wizard_data']['training_style'] = 'Фулбоди'
                # Move to workout time prompt
                await callback_query.message.edit_text(
                    f"📋 **СОЗДАНИЕ ПЕРСОНАЛЬНОЙ ПРОГРАММЫ**\n"
                    f"Раздел 8/9: Время тренировок\n\n"
                    f"✅ Стиль: Фулбоди (умеренная нагрузка)\n\n"
                    f"🕒 В какое время вы планируете тренироваться?\n"
                    f"(Это поможет настроить напоминания)\n\n"
                    f"📝 Введите время в формате ЧЧ:ММ (например: 18:30):",
                    parse_mode=ParseMode.MARKDOWN
                )
                state['step'] = 'workout_time'
                state['current_section'] = 8
        
        # Handle health restrictions selection
        elif data and data.startswith('health_') and state.get('step') == 'health_restrictions':
            if data == 'health_done':
                # Move to training style selection
                await self.wizard_step_training_style(callback_query, state)
            elif data == 'health_none':
                # Clear all restrictions and mark as no problems
                state['wizard_data']['health_restrictions'] = ['нет проблем']
                await self.wizard_step_medical(callback_query, state)
            elif data == 'health_other':
                # Set state for text input of other restrictions
                state['step'] = 'health_text_input'
                await callback_query.message.edit_text(
                    f"📋 **СОЗДАНИЕ ПЕРСОНАЛЬНОЙ ПРОГРАММЫ**\n"
                    f"Раздел 6/8: Дополнительные ограничения\n\n"
                    f"📝 Опишите ваши медицинские ограничения:\n"
                    f"(Например: травма запястья, операция на колене)\n\n"
                    f"Введите описание или 'отмена' для возврата:",
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                # Add/remove specific restriction
                restrictions = state['wizard_data']['health_restrictions']
                restriction_map = {
                    'health_shoulder': 'проблемы с плечами',
                    'health_back': 'проблемы со спиной', 
                    'health_knee': 'проблемы с коленями'
                }
                
                restriction_name = restriction_map.get(data, '')
                if restriction_name:
                    # Remove 'нет проблем' if adding real restriction
                    if 'нет проблем' in restrictions:
                        restrictions.remove('нет проблем')
                    
                    # Toggle restriction
                    if restriction_name in restrictions:
                        restrictions.remove(restriction_name)
                    else:
                        restrictions.append(restriction_name)
                
                # Update display
                await self.wizard_step_medical(callback_query, state)

        # Handle schedule days selection
        elif data and data.startswith('schedule_') and state.get('step') == 'schedule':
            schedules = {
                'schedule_2': '2 дня в неделю',
                'schedule_3': '3 дня в неделю', 
                'schedule_4': '4 дня в неделю',
                'schedule_5': '5 дней в неделю',
                'schedule_6': '6 дней в неделю'
            }
            state['wizard_data']['schedule_days'] = schedules.get(data or '', '3 дня в неделю')
            
            # Next step - ask for medical restrictions (text input)
            await self.wizard_step_medical(callback_query, state)
            
        # Weight selection is now handled by text input in handle_text
            
        # Workout time selection is now handled by text input in handle_text
            
        # Handle motivation selection (final step)
        elif data and data.startswith('motivation_') and state.get('step') == 'motivation':
            motivations = {
                'motivation_health': 'здоровье и хорошее самочувствие',
                'motivation_appearance': 'улучшение внешнего вида',
                'motivation_strength': 'стать сильнее',
                'motivation_confidence': 'повышение уверенности в себе',
                'motivation_sport': 'подготовка к спорту',
                'motivation_stress': 'снятие стресса'
            }
            state['wizard_data']['motivation'] = motivations.get(data, 'здоровье и хорошее самочувствие')
            
            # Final step - generate program with all collected data
            await self.generate_ai_program(callback_query, state)

    async def start_program_wizard(self, callback_query: types.CallbackQuery):
        """Start the comprehensive AI program creation wizard"""
        tg_user_id = callback_query.from_user.id
        
        # Set user state for wizard
        self.user_states[tg_user_id] = {
            'step': 'age',
            'wizard_data': {},
            'current_section': 1,
            'total_sections': 8
        }
        
        # Start with general information - age (text input)
        await callback_query.message.edit_text(
            "📋 **СОЗДАНИЕ ПЕРСОНАЛЬНОЙ ПРОГРАММЫ**\n"
            "Раздел 1/8: Общая информация\n\n"
            "👤 Сколько вам лет?\n\n"
            "📝 Введите ваш возраст числом (например: 25):",
            parse_mode=ParseMode.MARKDOWN
        )

    async def start_program_upload(self, callback_query: types.CallbackQuery):
        """Start custom program upload"""
        tg_user_id = callback_query.from_user.id
        
        self.user_states[tg_user_id] = {
            'step': 'upload_program',
            'wizard_data': {}
        }
        
        await callback_query.message.edit_text(
            "📄 Отправьте вашу программу тренировок текстом или файлом.\n\n"
            "Например:\n"
            "День 1 - Верх тела:\n"
            "• Жим лёжа 4x8-10\n"
            "• Подтягивания 3x8-12\n"
            "• Жим плечами 3x10-12\n\n"
            "Или просто опишите как тренируетесь обычно."
        )

    async def skip_program_setup(self, callback_query: types.CallbackQuery):
        """Skip program setup"""
        await callback_query.message.edit_text(
            "✅ Настройка завершена!\n\n"
            "Вы можете создать программу позже командой /start\n\n"
            "Начните тренировку: /train\n"
            "Посмотрите статистику: /week"
        )

    async def generate_ai_program(self, callback_query: types.CallbackQuery, state: Dict):
        """Generate AI workout program based on user data"""
        try:
            await callback_query.message.edit_text(
                "🤖 Создаю персональную программу тренировок...\n"
                "⏳ Это может занять несколько секунд"
            )
            
            # Prepare user data for AI - only include collected data
            user_data = {}
            wizard_data = state['wizard_data']
            
            # Add only data that was actually collected from user
            if 'age' in wizard_data and wizard_data['age']:
                user_data['age'] = wizard_data['age']
            if 'gender' in wizard_data and wizard_data['gender']:
                user_data['gender'] = wizard_data['gender']
            if 'weight' in wizard_data and wizard_data['weight']:
                user_data['weight'] = f"{wizard_data['weight']} кг"
            if 'goal' in wizard_data and wizard_data['goal']:
                user_data['goal'] = wizard_data['goal']
            if 'experience' in wizard_data and wizard_data['experience']:
                user_data['experience'] = wizard_data['experience']
            if 'schedule_days' in wizard_data and wizard_data['schedule_days']:
                user_data['schedule_days'] = wizard_data['schedule_days']
            if 'chronic_diseases' in wizard_data and wizard_data['chronic_diseases']:
                user_data['chronic_diseases'] = wizard_data['chronic_diseases']
            if 'health_restrictions' in wizard_data and wizard_data['health_restrictions']:
                user_data['health_restrictions'] = wizard_data['health_restrictions']
            if 'training_style' in wizard_data and wizard_data['training_style']:
                user_data['training_style'] = wizard_data['training_style']
            if 'workout_time' in wizard_data and wizard_data['workout_time']:
                user_data['workout_time'] = wizard_data['workout_time']
            if 'motivation' in wizard_data and wizard_data['motivation']:
                user_data['motivation'] = wizard_data['motivation']
            
            # SECURITY: Do NOT add defaults for AI - it must work with real user data only
            # AI will handle missing data gracefully and ask for clarification if needed
            
            # Load additional user data from DB if not in wizard_data
            if 'health_restrictions' not in user_data:
                with sqlite3.connect(self.db.db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT health_restrictions FROM users WHERE tg_user_id = ?", (tg_user_id,))
                    result = cursor.fetchone()
                    if result and result[0]:
                        user_data['health_restrictions'] = result[0]
            
            # Generate program with AI
            program_data = await self.ai_coach.generate_workout_program(user_data)
            
            if program_data and 'name' in program_data:
                # Save program to database
                tg_user_id = callback_query.from_user.id
                program_id = self.db.create_workout_program(
                    tg_user_id, 
                    json.dumps(program_data, ensure_ascii=False),
                    program_data['name'],
                    program_data.get('description', f"AI-сгенерированная программа для {user_data.get('goal', 'фитнеса')}")
                )
                
                # Update user profile ONLY with data collected by wizard (no defaults)
                update_data = {}
                wizard_data = state['wizard_data']
                
                # Only update fields that user explicitly provided
                if 'age' in wizard_data and wizard_data['age']:
                    # Convert age range to middle value for storage
                    age_map = {'16-20': 18, '21-30': 25, '31-40': 35, '41-50': 45, '51-60': 55, '60+': 65}
                    # SECURITY: Only save age if it's a valid range from wizard - no defaults
                    if wizard_data['age'] in age_map:
                        update_data['age'] = age_map[wizard_data['age']]
                
                if 'gender' in wizard_data and wizard_data['gender']:
                    update_data['gender'] = wizard_data['gender']
                    
                if 'goal' in wizard_data and wizard_data['goal']:
                    update_data['goal'] = wizard_data['goal']
                    
                if 'experience' in wizard_data and wizard_data['experience']:
                    update_data['experience'] = wizard_data['experience']
                    
                if 'schedule_days' in wizard_data and wizard_data['schedule_days']:
                    update_data['schedule_days'] = wizard_data['schedule_days']
                    
                if 'weight' in wizard_data and wizard_data['weight']:
                    update_data['weight'] = wizard_data['weight']
                    
                if 'chronic_diseases' in wizard_data and wizard_data['chronic_diseases']:
                    update_data['chronic_diseases'] = wizard_data['chronic_diseases']
                    
                if 'workout_time' in wizard_data and wizard_data['workout_time']:
                    update_data['workout_time'] = wizard_data['workout_time']
                    
                if 'motivation' in wizard_data and wizard_data['motivation']:
                    update_data['motivation'] = wizard_data['motivation']
                if 'training_style' in wizard_data and wizard_data['training_style']:
                    update_data['training_style'] = wizard_data['training_style']
                
                # Only update if we have data to update
                if update_data:
                    self.db.update_user_profile(tg_user_id, **update_data)
                    
                    # Setup notifications if workout time and schedule were provided
                    if 'workout_time' in update_data and 'schedule_days' in update_data:
                        user_profile = self.db.get_user(tg_user_id)
                        user_tz = user_profile.get('tz', DEFAULT_TIMEZONE) if user_profile else DEFAULT_TIMEZONE
                        self.setup_user_notifications(
                            tg_user_id, 
                            update_data['schedule_days'], 
                            update_data['workout_time'], 
                            user_tz
                        )
                
                # Format and send full program
                program_text = self.format_program_text(program_data, user_data)
                
                # Send program in chunks if too long (Telegram limit is 4096 characters)
                if len(program_text) > 4000:
                    # Send header first
                    await callback_query.message.edit_text(
                        f"✅ Программа создана!\n\n"
                        f"📋 **{program_data['name']}**\n"
                        f"🎯 {user_data['goal']} | 📊 {user_data['experience']}\n\n"
                        "📱 Отправляю полную программу следующим сообщением...",
                        parse_mode=ParseMode.MARKDOWN
                    )
                    
                    # Send full program as new message
                    await callback_query.message.answer(program_text, parse_mode=ParseMode.MARKDOWN)
                else:
                    # Send everything in one message
                    await callback_query.message.edit_text(program_text, parse_mode=ParseMode.MARKDOWN)
                
                # Clear user state
                if tg_user_id in self.user_states:
                    del self.user_states[tg_user_id]
                    
            else:
                raise ValueError("Не удалось создать программу")
                
        except Exception as e:
            logger.error(f"Error generating AI program: {e}")
            await callback_query.message.edit_text(
                "❌ Произошла ошибка при создании программы.\n\n"
                "Попробуйте еще раз позже или выберите 'Загрузить свою программу'.\n\n"
                "Для начала тренировки используйте: /train"
            )

    async def generate_ai_program_message(self, message, state: Dict):
        """Generate AI workout program based on user data (message version)"""
        try:
            await message.answer(
                "🤖 Создаю персональную программу тренировок...\n"
                "⏳ Это может занять несколько секунд"
            )
            
            # Prepare user data for AI - only include collected data
            user_data = {}
            wizard_data = state['wizard_data']
            
            # Add only data that was actually collected from user
            if 'age' in wizard_data and wizard_data['age']:
                user_data['age'] = wizard_data['age']
            if 'gender' in wizard_data and wizard_data['gender']:
                user_data['gender'] = wizard_data['gender']
            if 'height' in wizard_data and wizard_data['height']:
                user_data['height'] = f"{wizard_data['height']} см"
            if 'weight' in wizard_data and wizard_data['weight']:
                user_data['weight'] = f"{wizard_data['weight']} кг"
            if 'goal' in wizard_data and wizard_data['goal']:
                user_data['goal'] = wizard_data['goal']
            if 'experience' in wizard_data and wizard_data['experience']:
                user_data['experience'] = wizard_data['experience']
            if 'schedule_days' in wizard_data and wizard_data['schedule_days']:
                user_data['schedule_days'] = wizard_data['schedule_days']
            if 'chronic_diseases' in wizard_data and wizard_data['chronic_diseases']:
                user_data['chronic_diseases'] = wizard_data['chronic_diseases']
            if 'health_restrictions' in wizard_data and wizard_data['health_restrictions']:
                user_data['health_restrictions'] = wizard_data['health_restrictions']
            if 'training_style' in wizard_data and wizard_data['training_style']:
                user_data['training_style'] = wizard_data['training_style']
            if 'workout_time' in wizard_data and wizard_data['workout_time']:
                user_data['workout_time'] = wizard_data['workout_time']
            if 'motivation' in wizard_data and wizard_data['motivation']:
                user_data['motivation'] = wizard_data['motivation']
            
            # Load additional user data from DB if not in wizard_data
            if 'health_restrictions' not in user_data:
                with sqlite3.connect(self.db.db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT health_restrictions FROM users WHERE tg_user_id = ?", (tg_user_id,))
                    result = cursor.fetchone()
                    if result and result[0]:
                        user_data['health_restrictions'] = result[0]
            
            # Generate program with AI
            program_data = await self.ai_coach.generate_workout_program(user_data)
            
            if program_data and 'name' in program_data:
                # Save program to database
                tg_user_id = message.from_user.id
                program_id = self.db.create_workout_program(
                    tg_user_id, 
                    json.dumps(program_data, ensure_ascii=False),
                    program_data['name'],
                    program_data.get('description', f"AI-сгенерированная программа для {user_data.get('goal', 'фитнеса')}")
                )
                
                # Update user profile with all collected wizard data
                all_updates = {}
                if wizard_data.get('age'): all_updates['age'] = wizard_data['age']
                if wizard_data.get('gender'): all_updates['gender'] = wizard_data['gender']
                if wizard_data.get('height'): all_updates['height'] = wizard_data['height']
                if wizard_data.get('weight'): all_updates['weight'] = wizard_data['weight']
                if wizard_data.get('goal'): all_updates['goal'] = wizard_data['goal']
                if wizard_data.get('experience'): all_updates['experience'] = wizard_data['experience']
                if wizard_data.get('schedule_days'): all_updates['schedule_days'] = wizard_data['schedule_days']
                if wizard_data.get('chronic_diseases'): all_updates['chronic_diseases'] = wizard_data['chronic_diseases']
                if wizard_data.get('health_restrictions'): all_updates['health_restrictions'] = ', '.join(wizard_data['health_restrictions']) if isinstance(wizard_data['health_restrictions'], list) else wizard_data['health_restrictions']
                if wizard_data.get('workout_time'): all_updates['workout_time'] = wizard_data['workout_time']
                if wizard_data.get('motivation'): all_updates['motivation'] = wizard_data['motivation']
                
                if all_updates:
                    updated_fields = self.db.update_user_profile(tg_user_id, **all_updates)
                    logger.info(f"Updated user {tg_user_id} profile: {updated_fields}")
                
                # Clear wizard state
                if tg_user_id in self.user_states:
                    del self.user_states[tg_user_id]
                
                # Setup notifications with collected data
                await self.setup_user_notifications_after_wizard(tg_user_id)
                
                # Format program nicely
                program_text = f"✨ **{program_data['name']}**\n\n"
                if 'description' in program_data:
                    program_text += f"{program_data['description']}\n\n"
                
                program_text += self.format_program_display(program_data)
                
                await message.answer(
                    program_text + "\n\n✅ Программа сохранена и настроены напоминания!\n"
                    "💪 Начинайте тренировку: /train",
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                await message.answer(
                    "❌ Ошибка генерации программы.\n\n"
                    "Попробуйте еще раз позже: /start"
                )
                
        except Exception as e:
            logger.error(f"Error in generate_ai_program_message: {e}")
            await message.answer(
                "❌ Произошла ошибка при создании программы.\n\n"
                "Попробуйте еще раз позже или выберите 'Загрузить свою программу'.\n\n"
                "Для начала тренировки используйте: /train"
            )

    def format_program_text(self, program_data: Dict, user_data: Dict) -> str:
        """Format workout program data into readable text"""
        text = f"✅ **{program_data.get('name', 'Программа тренировок')}**\n\n"
        
        # Program info
        description = program_data.get('description', 'Персональная программа тренировок')
        text += f"📝 {description}\n"
        text += f"🎯 Цель: {user_data.get('goal', 'фитнес')}\n"
        text += f"📊 Уровень: {user_data.get('experience', 'новичок')}\n"
        
        duration = program_data.get('duration_weeks')
        if duration:
            text += f"⏱ Продолжительность: {duration} недель\n"
        
        text += "\n━━━━━━━━━━━━━━━━━━\n\n"
        
        # Workouts
        workouts = program_data.get('workouts', [])
        if workouts:
            text += "🏋️ **ПРОГРАММА ТРЕНИРОВОК:**\n\n"
            
            for i, workout in enumerate(workouts, 1):
                day = workout.get('day', f'День {i}')
                workout_name = workout.get('name', 'Тренировка')
                text += f"**{day} - {workout_name}**\n"
                
                exercises = workout.get('exercises', [])
                for exercise in exercises:
                    name = exercise.get('name', 'Упражнение')
                    sets = exercise.get('sets', 3)
                    reps = exercise.get('reps', '8-10')
                    weight = exercise.get('weight_guidance', '')
                    notes = exercise.get('notes', '')
                    
                    text += f"• {name}: {sets}x{reps}"
                    if weight:
                        text += f" ({weight})"
                    if notes:
                        text += f" - {notes}"
                    text += "\n"
                
                text += "\n"
        
        text += "━━━━━━━━━━━━━━━━━━\n"
        text += "💪 Начните тренировку: /train\n"
        text += "📊 Просмотр статистики: /week"
        
        return text

    # WIZARD STEP FUNCTIONS
    async def wizard_step_gender(self, callback_query: types.CallbackQuery, state: Dict):
        """Step 2: Gender selection"""
        state['step'] = 'gender'
        state['current_section'] = 1
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👨 Мужской", callback_data="gender_male")],
            [InlineKeyboardButton(text="👩 Женский", callback_data="gender_female")]
        ])
        
        await callback_query.message.edit_text(
            f"📋 **СОЗДАНИЕ ПЕРСОНАЛЬНОЙ ПРОГРАММЫ**\n"
            f"Раздел 1/8: Общая информация\n\n"
            f"✅ Возраст: {state['wizard_data'].get('age', '')}\n"
            f"👤 Ваш пол:",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )

    async def wizard_step_gender_message(self, message, state: Dict):
        """Step 2: Gender selection (message version)"""
        state['step'] = 'gender'
        state['current_section'] = 1
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👨 Мужской", callback_data="gender_male")],
            [InlineKeyboardButton(text="👩 Женский", callback_data="gender_female")]
        ])
        
        await message.answer(
            f"📋 **СОЗДАНИЕ ПЕРСОНАЛЬНОЙ ПРОГРАММЫ**\n"
            f"Раздел 1/8: Общая информация\n\n"
            f"✅ Возраст: {state['wizard_data'].get('age', '')}\n"
            f"👤 Ваш пол:",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )

    async def wizard_step_height(self, message, state: Dict):
        """Step 3: Height collection (text input)"""
        state['step'] = 'height'
        state['current_section'] = 2
        
        await message.answer(
            f"📋 **СОЗДАНИЕ ПЕРСОНАЛЬНОЙ ПРОГРАММЫ**\n"
            f"Раздел 2/8: Физические параметры\n\n"
            f"✅ Возраст: {state['wizard_data'].get('age', '')}\n"
            f"✅ Пол: {state['wizard_data'].get('gender', '')}\n\n"
            f"📏 Укажите ваш рост в сантиметрах\n\n"
            f"📝 Введите рост числом (например: 175):",
            parse_mode=ParseMode.MARKDOWN
        )

    async def wizard_step_weight(self, message, state: Dict):
        """Step 4: Weight collection (text input)"""
        state['step'] = 'weight'
        state['current_section'] = 2
        
        await message.answer(
            f"📋 **СОЗДАНИЕ ПЕРСОНАЛЬНОЙ ПРОГРАММЫ**\n"
            f"Раздел 2/8: Физические параметры\n\n"
            f"✅ Возраст: {state['wizard_data'].get('age', '')}\n"
            f"✅ Пол: {state['wizard_data'].get('gender', '')}\n"
            f"✅ Рост: {state['wizard_data'].get('height', '')} см\n\n"
            f"⚖️ Укажите ваш точный вес в килограммах\n\n"
            f"📝 Введите вес числом (например: 70):",
            parse_mode=ParseMode.MARKDOWN
        )

    async def wizard_step_goal(self, callback_query: types.CallbackQuery, state: Dict):
        """Step 4: Goal selection"""
        state['step'] = 'goal'
        state['current_section'] = 3
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💪 Набор мышечной массы", callback_data="goal_mass")],
            [InlineKeyboardButton(text="🔥 Похудение", callback_data="goal_weight_loss")],
            [InlineKeyboardButton(text="⚡ Увеличение силы", callback_data="goal_strength")],
            [InlineKeyboardButton(text="🏃 Общая физподготовка", callback_data="goal_fitness")],
            [InlineKeyboardButton(text="🫁 Улучшение выносливости", callback_data="goal_endurance")],
            [InlineKeyboardButton(text="🏥 Здоровье спины и суставов", callback_data="goal_health")]
        ])
        
        await callback_query.message.edit_text(
            f"📋 **СОЗДАНИЕ ПЕРСОНАЛЬНОЙ ПРОГРАММЫ**\n"
            f"Раздел 3/8: Цели тренировок\n\n"
            f"✅ Возраст: {state['wizard_data'].get('age', '')}\n"
            f"✅ Пол: {state['wizard_data'].get('gender', '')}\n"
            f"✅ Рост: {state['wizard_data'].get('height', '')} см\n"
            f"✅ Вес: {state['wizard_data'].get('weight', '')} кг\n\n"
            f"🎯 Какая ваша основная цель тренировок?",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )

    async def wizard_step_goal_message(self, message, state: Dict):
        """Step 5: Goal selection (message version)"""
        state['step'] = 'goal'
        state['current_section'] = 3
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💪 Набор мышечной массы", callback_data="goal_mass")],
            [InlineKeyboardButton(text="🔥 Похудение", callback_data="goal_weight_loss")],
            [InlineKeyboardButton(text="⚡ Увеличение силы", callback_data="goal_strength")],
            [InlineKeyboardButton(text="🏃 Общая физподготовка", callback_data="goal_fitness")],
            [InlineKeyboardButton(text="🫁 Улучшение выносливости", callback_data="goal_endurance")],
            [InlineKeyboardButton(text="🏥 Здоровье спины и суставов", callback_data="goal_health")]
        ])
        
        await message.answer(
            f"📋 **СОЗДАНИЕ ПЕРСОНАЛЬНОЙ ПРОГРАММЫ**\n"
            f"Раздел 3/8: Цели тренировок\n\n"
            f"✅ Возраст: {state['wizard_data'].get('age', '')}\n"
            f"✅ Пол: {state['wizard_data'].get('gender', '')}\n"
            f"✅ Рост: {state['wizard_data'].get('height', '')} см\n"
            f"✅ Вес: {state['wizard_data'].get('weight', '')} кг\n\n"
            f"🎯 Какая ваша основная цель тренировок?",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )

    async def wizard_step_experience(self, callback_query: types.CallbackQuery, state: Dict):
        """Step 4: Experience level"""
        state['step'] = 'experience'
        state['current_section'] = 3
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔰 Новичок (менее 6 месяцев)", callback_data="exp_beginner")],
            [InlineKeyboardButton(text="📈 Средний (6 месяцев - 2 года)", callback_data="exp_intermediate")],
            [InlineKeyboardButton(text="💪 Продвинутый (более 2 лет)", callback_data="exp_advanced")]
        ])
        
        await callback_query.message.edit_text(
            f"📋 **СОЗДАНИЕ ПЕРСОНАЛЬНОЙ ПРОГРАММЫ**\n"
            f"Раздел 4/8: Уровень подготовки\n\n"
            f"✅ Цель: {state['wizard_data'].get('goal', '')}\n\n"
            f"📊 Каков ваш опыт тренировок?",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )

    async def wizard_step_schedule(self, callback_query: types.CallbackQuery, state: Dict):
        """Step 5: Schedule planning"""
        state['step'] = 'schedule'
        state['current_section'] = 4
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="2 дня в неделю", callback_data="schedule_2")],
            [InlineKeyboardButton(text="3 дня в неделю", callback_data="schedule_3")],
            [InlineKeyboardButton(text="4 дня в неделю", callback_data="schedule_4")],
            [InlineKeyboardButton(text="5 дней в неделю", callback_data="schedule_5")],
            [InlineKeyboardButton(text="6 дней в неделю", callback_data="schedule_6")]
        ])
        
        await callback_query.message.edit_text(
            f"📋 **СОЗДАНИЕ ПЕРСОНАЛЬНОЙ ПРОГРАММЫ**\n"
            f"Раздел 5/8: Организация тренировок\n\n"
            f"✅ Опыт: {state['wizard_data'].get('experience', '')}\n\n"
            f"📅 Сколько дней в неделю готовы тренироваться?",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )

    async def wizard_step_medical(self, callback_query: types.CallbackQuery, state: Dict):
        """Step 6: Health restrictions with structured selection"""
        state['step'] = 'health_restrictions'
        state['current_section'] = 6
        
        # Initialize health restrictions tracking
        if 'health_restrictions' not in state['wizard_data']:
            state['wizard_data']['health_restrictions'] = []
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🤷 Плечи/плечевые суставы", callback_data="health_shoulder")],
            [InlineKeyboardButton(text="🔄 Спина/позвоночник", callback_data="health_back")],
            [InlineKeyboardButton(text="🦵 Колени/коленные суставы", callback_data="health_knee")],
            [InlineKeyboardButton(text="🦗 Другие ограничения", callback_data="health_other")],
            [InlineKeyboardButton(text="✅ Нет проблем", callback_data="health_none")],
            [InlineKeyboardButton(text="▶️ Далее", callback_data="health_done")]
        ])
        
        # Show current selections
        restrictions = state['wizard_data']['health_restrictions']
        selected_text = ""
        if restrictions:
            selected_text = f"\n\n✅ Выбрано: {', '.join(restrictions)}"
        
        await callback_query.message.edit_text(
            f"📋 **СОЗДАНИЕ ПЕРСОНАЛЬНОЙ ПРОГРАММЫ**\n"
            f"Раздел 6/8: Проверка проблемных зон\n\n"
            f"✅ Расписание: {state['wizard_data'].get('schedule_days', '')}\n\n"
            f"🏥 Отметьте проблемные зоны для адаптации упражнений:\n"
            f"(Можно выбрать несколько или указать 'Нет проблем')"
            f"{selected_text}\n\n"
            f"📝 После выбора нажмите 'Далее'",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )

    async def wizard_step_medical_message(self, message, state: Dict):
        """Step 6: Health restrictions message version"""
        state['step'] = 'health_restrictions'
        state['current_section'] = 6
        
        # Initialize health restrictions tracking
        if 'health_restrictions' not in state['wizard_data']:
            state['wizard_data']['health_restrictions'] = []
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🤷 Плечи/плечевые суставы", callback_data="health_shoulder")],
            [InlineKeyboardButton(text="🔄 Спина/позвоночник", callback_data="health_back")],
            [InlineKeyboardButton(text="🦵 Колени/коленные суставы", callback_data="health_knee")],
            [InlineKeyboardButton(text="🦗 Другие ограничения", callback_data="health_other")],
            [InlineKeyboardButton(text="✅ Нет проблем", callback_data="health_none")],
            [InlineKeyboardButton(text="▶️ Далее", callback_data="health_done")]
        ])
        
        # Show current selections
        restrictions = state['wizard_data']['health_restrictions']
        selected_text = ""
        if restrictions:
            selected_text = f"\n\n✅ Выбрано: {', '.join(restrictions)}"
        
        await message.answer(
            f"📋 **СОЗДАНИЕ ПЕРСОНАЛЬНОЙ ПРОГРАММЫ**\n"
            f"Раздел 6/8: Проверка проблемных зон\n\n"
            f"✅ Расписание: {state['wizard_data'].get('schedule_days', '')}\n\n"
            f"🏥 Отметьте проблемные зоны для адаптации упражнений:\n"
            f"(Можно выбрать несколько или указать 'Нет проблем')"
            f"{selected_text}\n\n"
            f"📝 После выбора нажмите 'Далее'",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )

    async def wizard_step_workout_time(self, message, state: Dict):
        """Step 7: Workout time selection (text input)"""
        state['step'] = 'workout_time'
        state['current_section'] = 7
        
        await message.answer(
            f"📋 **СОЗДАНИЕ ПЕРСОНАЛЬНОЙ ПРОГРАММЫ**\n"
            f"Раздел 7/8: Время тренировок\n\n"
            f"✅ Медицинские ограничения: {state['wizard_data'].get('chronic_diseases', 'нет')}\n\n"
            f"🕒 В какое время вы планируете тренироваться?\n"
            f"(Это поможет настроить напоминания)\n\n"
            f"📝 Введите время в формате ЧЧ:ММ (например: 18:30):",
            parse_mode=ParseMode.MARKDOWN
        )

    async def wizard_step_workout_time_message(self, message: Message, state: Dict):
        """Step 7: Workout time selection - transition from text input"""
        state['step'] = 'workout_time'
        state['current_section'] = 7
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🌅 Утром (06:00-09:00)", callback_data="time_morning")],
            [InlineKeyboardButton(text="🌞 Днем (12:00-15:00)", callback_data="time_afternoon")],
            [InlineKeyboardButton(text="🌆 Вечером (18:00-21:00)", callback_data="time_evening")],
            [InlineKeyboardButton(text="🌙 Поздно (21:00-23:00)", callback_data="time_night")]
        ])
        
        await message.answer(
            f"📋 **СОЗДАНИЕ ПЕРСОНАЛЬНОЙ ПРОГРАММЫ**\n"
            f"Раздел 7/8: Время тренировок\n\n"
            f"✅ Медицинские ограничения: {state['wizard_data'].get('chronic_diseases', 'нет')}\n\n"
            f"🕒 В какое время вы планируете тренироваться?\n"
            f"(Это поможет настроить напоминания)",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )

    async def wizard_step_training_style(self, callback_query: types.CallbackQuery, state: Dict):
        """Step 7: Training style selection"""
        state['step'] = 'training_style'
        state['current_section'] = 7
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏋️ Сплит", callback_data="style_split")],
            [InlineKeyboardButton(text="💪 Фулбоди", callback_data="style_fullbody")]
        ])
        
        # Get health restrictions for display
        health_restrictions = state['wizard_data'].get('health_restrictions', [])
        health_text = ', '.join(health_restrictions) if health_restrictions else 'Нет ограничений'
        
        await callback_query.message.edit_text(
            f"📋 **СОЗДАНИЕ ПЕРСОНАЛЬНОЙ ПРОГРАММЫ**\n"
            f"Раздел 7/9: Стиль тренировок\n\n"
            f"✅ Проблемные зоны: {health_text}\n\n"
            f"🏋️ **Сплит** - каждая группа мышц 1 раз в неделю с высокой нагрузкой\n"
            f"🔥 Подходит для: опытных, много времени на тренировку\n"
            f"⏱️ Тренировки: 60-90 минут, 4-6 дней в неделю\n\n"
            f"💪 **Фулбоди** - все мышцы 3 раза в неделю с умеренной нагрузкой\n"
            f"🎯 Подходит для: новичков, ограниченное время\n"
            f"⏱️ Тренировки: 45-60 минут, 3-4 дня в неделю\n\n"
            f"🤔 Какой стиль тренировок предпочитаете?",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )

    async def wizard_step_motivation(self, callback_query: types.CallbackQuery, state: Dict):
        """Step 9: Motivation (final step)"""
        state['step'] = 'motivation'
        state['current_section'] = 9
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💚 Здоровье и самочувствие", callback_data="motivation_health")],
            [InlineKeyboardButton(text="💪 Улучшение внешнего вида", callback_data="motivation_appearance")],
            [InlineKeyboardButton(text="⚡ Стать сильнее", callback_data="motivation_strength")],
            [InlineKeyboardButton(text="🔥 Повысить уверенность", callback_data="motivation_confidence")],
            [InlineKeyboardButton(text="🏃 Подготовка к спорту", callback_data="motivation_sport")],
            [InlineKeyboardButton(text="😌 Снятие стресса", callback_data="motivation_stress")]
        ])
        
        await callback_query.message.edit_text(
            f"📋 **СОЗДАНИЕ ПЕРСОНАЛЬНОЙ ПРОГРАММЫ**\n"
            f"Раздел 9/9: Мотивация\n\n"
            f"✅ Время тренировок: {state['wizard_data'].get('workout_time', '')}\n"
            f"✅ Стиль: {state['wizard_data'].get('training_style', '')}\n\n"
            f"💭 Что мотивировало вас начать тренироваться?\n"
            f"Это поможет создать более персональную программу:\n\n"
            f"🔄 Выберите быстрый вариант или напишите своё:",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )

    async def wizard_step_motivation_message(self, message, state: Dict):
        """Step 9: Motivation (message version with hybrid input)"""
        state['step'] = 'motivation'
        state['current_section'] = 9
        
        # Гибридные кнопки + возможность текстового ввода
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💚 Здоровье и самочувствие", callback_data="motivation_health")],
            [InlineKeyboardButton(text="💪 Улучшение внешнего вида", callback_data="motivation_appearance")],
            [InlineKeyboardButton(text="⚡ Стать сильнее", callback_data="motivation_strength")],
            [InlineKeyboardButton(text="🔥 Повысить уверенность", callback_data="motivation_confidence")],
            [InlineKeyboardButton(text="🏃 Подготовка к спорту", callback_data="motivation_sport")],
            [InlineKeyboardButton(text="😌 Снятие стресса", callback_data="motivation_stress")]
        ])
        
        await message.answer(
            f"📋 **СОЗДАНИЕ ПЕРСОНАЛЬНОЙ ПРОГРАММЫ**\n"
            f"Раздел 9/9: Мотивация\n\n"
            f"✅ Время тренировок: {state['wizard_data'].get('workout_time', '')}\n"
            f"✅ Стиль: {state['wizard_data'].get('training_style', '')}\n\n"
            f"💭 Что мотивировало вас начать тренироваться?\n"
            f"Это поможет создать более персональную программу:\n\n"
            f"🔄 Выберите быстрый вариант либо\n"
            f"📝 Напишите свою мотивацию текстом:",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )

    async def start_workout_callback(self, callback_query: types.CallbackQuery):
        """Start workout via callback (same as /train command)"""
        if not callback_query.from_user:
            await callback_query.answer("❌ Не удалось определить пользователя.")
            return
            
        tg_user_id = callback_query.from_user.id
        logger.info(f"User {tg_user_id} starting workout via callback")

        try:
            # Check if user exists
            user = self.db.get_user(tg_user_id)
            logger.info(f"User check result: {user is not None}")
            
            if not user:
                await callback_query.message.edit_text(
                    "🚀 Сначала используйте команду /start для настройки аккаунта.")
                return

            # Check for active workout
            active_workout = self.db.get_active_workout(tg_user_id)
            logger.info(f"Active workout check: {active_workout}")
            
            if active_workout:
                await callback_query.message.edit_text(
                    f"⚠️ У вас уже есть активная тренировка: {active_workout}\n"
                    "Завершите её командой /finish или добавляйте подходы командой /add")
                return

            # Create new workout
            workout_id = f"w_{tg_user_id}_{int(datetime.now().timestamp())}"
            dt_start = datetime.now().isoformat()
            logger.info(f"Creating workout: {workout_id}")

            self.db.create_workout(workout_id, tg_user_id, dt_start)
            logger.info(f"Workout created successfully: {workout_id}")

            await callback_query.message.edit_text(
                f"💪 Тренировка начата!\n"
                f"🆔 ID: {workout_id}\n"
                f"⏰ Время начала: {datetime.now().strftime('%H:%M')}\n\n"
                "📝 Добавляйте подходы командой /add или просто отправьте сообщение:\n"
                "• Жим лёжа 60 кг 5 подходов по 8 раз\n"
                "• Bench 60x5x3@8\n"
                "• Или опишите тренировку своими словами")
                
        except Exception as e:
            logger.error(f"Error in start_workout_callback for user {tg_user_id}: {e}")
            await callback_query.message.edit_text(f"❌ Произошла ошибка при создании тренировки: {e}")

    async def show_current_workout_callback(self, callback_query: types.CallbackQuery):
        """Show current active workout"""
        if not callback_query.from_user:
            await callback_query.answer("❌ Не удалось определить пользователя.")
            return

        tg_user_id = callback_query.from_user.id
        logger.info(f"User {tg_user_id} requested current workout view")

        try:
            workout_id = self.db.get_active_workout(tg_user_id)
            if not workout_id:
                await callback_query.message.edit_text("⚠️ У вас нет активной тренировки.")
                return

            sets = self.db.get_workout_sets(workout_id)
            if not sets:
                await callback_query.message.edit_text("📭 В текущей тренировке пока нет подходов.")
                return

            user = self.db.get_user(tg_user_id)
            unit = user.get('unit', 'kg') if user else 'kg'

            sets_summary = []
            current_exercise = None
            exercise_sets = []

            for s in sets:
                if current_exercise != s['exercise']:
                    if exercise_sets:
                        sets_summary.append(f"{current_exercise}: {', '.join(exercise_sets)}")
                        exercise_sets = []
                    current_exercise = s['exercise']

                set_str = f"{s['weight']}{unit}×{s['reps']}"
                if s.get('rpe'):
                    set_str += f"@{s['rpe']}"
                exercise_sets.append(set_str)

            if exercise_sets:
                sets_summary.append(f"{current_exercise}: {', '.join(exercise_sets)}")

            await callback_query.message.edit_text(
                "💪 Текущая тренировка:\n\n" + "\n".join(sets_summary)
            )

        except Exception as e:
            logger.error(f"Error in show_current_workout_callback for user {tg_user_id}: {e}")
            await callback_query.message.edit_text(f"❌ Ошибка: {e}")

    async def show_weekly_report_callback(self, callback_query: types.CallbackQuery):
        """Show weekly report via callback"""
        if not callback_query.from_user:
            await callback_query.answer("❌ Не удалось определить пользователя.")
            return
            
        # Create a fake message object to reuse cmd_week logic
        class FakeMessage:
            def __init__(self, user_id, bot_instance):
                self.from_user = types.User(id=user_id, is_bot=False, first_name="User")
                self.bot_instance = bot_instance
                
            async def answer(self, text, **kwargs):
                await callback_query.message.edit_text(text, **kwargs)
        
        fake_message = FakeMessage(callback_query.from_user.id, self.bot)
        await self.cmd_week(fake_message)

    async def show_settings_callback(self, callback_query: types.CallbackQuery):
        """Show settings menu via callback"""
        await callback_query.message.edit_text(
            "⚙️ Настройки\n\n"
            "Доступные команды:\n"
            "• /start - Перенастройка аккаунта\n"
            "• /export - Экспорт данных в CSV\n"
            "• /delete_me - Удаление всех данных\n\n"
            "Для возврата к главному меню используйте /start"
        )

    async def finish_workout_process(self, callback_query: types.CallbackQuery,
                                     state: Dict, sleep: int):
        """Complete the workout finishing process"""
        if not callback_query.from_user:
            return
        tg_user_id = callback_query.from_user.id
        workout_id = state['workout_id']
        srpe = state['sRPE']
        mood = state['mood']

        try:
            # Finish workout in database
            dt_end = datetime.now().isoformat()
            self.db.finish_workout(workout_id, dt_end, srpe, mood, sleep)

            # Get workout data for analysis
            user = self.db.get_user(tg_user_id)
            if user:
                # Calculate training load and duration
                with sqlite3.connect(self.db.db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT * FROM workouts WHERE workout_id = ?",
                        (workout_id, ))
                    workout_row = cursor.fetchone()

                if workout_row:
                    workout_data = {
                        'workout_id': workout_row[0],
                        'tg_user_id': workout_row[1],
                        'dt_start': workout_row[2],
                        'dt_end': workout_row[3],
                        'program': workout_row[4],
                        'duration_min': workout_row[5],
                        'sRPE': workout_row[6],
                        'mood': workout_row[7],
                        'sleep': workout_row[8],
                        'training_load': workout_row[9],
                        'note': workout_row[10] or ''
                    }

                    # Workout data is already stored in SQLite database

                    # Get recommendations
                    recommendations = self.analyzer.analyze_workout(
                        tg_user_id, srpe, sleep, workout_data['training_load'])

                    # Calculate tonnage for this workout
                    workout_tonnage = self.calculate_workout_tonnage(workout_id)
                    
                    # Calculate weekly tonnage by groups (last 7 days)
                    weekly_tonnage = calculate_tonnage_by_groups(self.db.db_path, tg_user_id, 7)

                    # Format final message
                    duration_str = f"{workout_data['duration_min']} мин"
                    training_load = workout_data['training_load']
                    
                    # Format tonnage report
                    tonnage_report = self.format_tonnage_report(workout_tonnage, weekly_tonnage)

                    if callback_query.message and hasattr(callback_query.message, 'edit_text'):
                        try:
                            await callback_query.message.edit_text(
                                f"🎉 Тренировка завершена!\n\n"
                                f"📊 Статистика:\n"
                                f"⏱ Длительность: {duration_str}\n"
                                f"🎯 sRPE: {srpe}/10\n"
                                f"😊 Настроение: {mood}/5\n"
                                f"😴 Сон: {sleep}/5\n"
                                f"⚡ Тренировочная нагрузка: {training_load}\n\n"
                                f"{tonnage_report}\n\n"
                                f"🤖 Рекомендации:\n{recommendations}")
                        except Exception as e:
                            logger.warning(f"Failed to edit workout completion message: {e}")
                            await callback_query.message.answer(
                                f"🎉 Тренировка завершена!\n\n"
                                f"📊 Статистика:\n"
                                f"⏱ Длительность: {duration_str}\n"
                                f"🎯 sRPE: {srpe}/10\n"
                                f"😊 Настроение: {mood}/5\n"
                                f"😴 Сон: {sleep}/5\n"
                                f"⚡ Тренировочная нагрузка: {training_load}\n\n"
                                f"{tonnage_report}\n\n"
                                f"🤖 Рекомендации:\n{recommendations}")

            # Clean up state
            del self.user_states[tg_user_id]

        except Exception as e:
            logger.error(f"Error finishing workout: {e}")
            if callback_query.message and hasattr(callback_query.message, 'edit_text'):
                try:
                    await callback_query.message.edit_text(
                        "❌ Ошибка при завершении тренировки. Попробуйте еще раз.")
                except Exception:
                    await callback_query.message.answer(
                        "❌ Ошибка при завершении тренировки. Попробуйте еще раз.")

    async def cmd_week(self, message: Message):
        """Handle /week command"""
        if not message.from_user:
            return
            
        tg_user_id = message.from_user.id

        try:
            weekly_stats = self.db.get_weekly_stats(tg_user_id, 1)

            if not weekly_stats:
                await message.answer(
                    "📊 Нет данных о тренировках за эту неделю.")
                return

            # Calculate statistics
            total_workouts = len(weekly_stats)
            total_load = sum(w['training_load'] for w in weekly_stats
                             if w['training_load'])
            avg_srpe = sum(w['sRPE'] for w in weekly_stats if w['sRPE']) / len(
                [w for w in weekly_stats if w['sRPE']]) if weekly_stats else 0
            avg_mood = sum(w['mood'] for w in weekly_stats if w['mood']) / len(
                [w for w in weekly_stats if w['mood']]) if weekly_stats else 0

            # Calculate detailed tonnage by muscle groups (current calendar week)
            weekly_tonnage = calculate_tonnage_by_groups(self.db.db_path, tg_user_id, 0)
            total_tonnage = weekly_tonnage.get('total', 0)
            
            # Calculate PRs for the week
            weekly_prs = self.get_weekly_prs(tg_user_id)

            # Get health flags (includes overload and other indicators)
            health_flags = self.get_health_flags(tg_user_id, weekly_stats, total_load)

            # Format enhanced report
            week_start = datetime.now() - timedelta(days=datetime.now().weekday())
            report = f"📊 Недельный отчет ({week_start.strftime('%d.%m')} - {datetime.now().strftime('%d.%m')})\n\n"
            
            # Basic stats
            report += f"🏋️ Тренировок: {total_workouts}\n"
            report += f"⚖️ Общий тоннаж: {total_tonnage:.1f} кг\n"
            
            # Tonnage by muscle groups
            if weekly_tonnage and any(group in weekly_tonnage for group in ['Ноги', 'Жимы', 'Тяги']):
                report += "\n💪 Тоннаж по группам:\n"
                for group_name in ['Ноги', 'Жимы', 'Тяги']:
                    if group_name in weekly_tonnage:
                        group_data = weekly_tonnage[group_name]
                        tonnage = group_data.get('tonnage', 0)
                        percentage = group_data.get('percentage', 0)
                        if tonnage > 0:
                            report += f"  • {group_name}: {tonnage} кг ({percentage} процентов)\n"
            
            # Performance metrics
            report += f"\n🎯 Средний sRPE: {avg_srpe:.1f}/10\n"
            report += f"😊 Среднее настроение: {avg_mood:.1f}/5\n"
            report += f"⚡ Общая нагрузка: {total_load}\n"
            
            # Personal Records
            if weekly_prs:
                report += f"\n🏆 Новые рекорды ({len(weekly_prs)}):\n"
                for pr in weekly_prs:
                    prev_text = f" (было: {pr['previous']} кг)" if pr['previous'] > 0 else " (первый раз!)"
                    report += f"  • {pr['exercise']}: {pr['weight']} кг{prev_text}\n"
            
            # Health flags
            if health_flags:
                report += f"\n🚨 Важные индикаторы:\n"
                for flag in health_flags:
                    report += f"  {flag}\n"
            
            # Recommendations based on analysis
            recommendations = self.generate_weekly_recommendations(weekly_tonnage, health_flags, weekly_prs, total_workouts)
            if recommendations:
                report += f"\n💡 Рекомендации:\n{recommendations}"

            await message.answer(report)

            # Store weekly statistics in database
            try:
                with sqlite3.connect(self.db.db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        INSERT OR REPLACE INTO weekly_stats 
                        (tg_user_id, week_start, workouts, total_tonnage, avg_sRPE, sum_load, avg_mood, deload_flag, note)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            tg_user_id,
                            week_start.strftime('%Y-%m-%d'),
                            total_workouts,
                            total_tonnage,
                            round(avg_srpe, 1),
                            total_load,
                            round(avg_mood, 1),
                            1 if any("Перегрузка" in flag for flag in health_flags) else 0,
                            ''  # note
                        ))
                    conn.commit()
            except Exception as e:
                logger.error(f"Error saving weekly data: {e}")

        except Exception as e:
            logger.error(f"Error in week command: {e}")
            await message.answer("❌ Ошибка при формировании отчета.")

    async def handle_voice(self, message: Message):
        """Handle voice messages"""
        if not message.from_user or not message.voice:
            return
            
        tg_user_id = message.from_user.id

        # Check if user exists
        user = self.db.get_user(tg_user_id)
        if not user:
            await message.answer(
                "Сначала используйте команду /start для настройки аккаунта.")
            return

        try:
            # Get voice message details
            voice = message.voice
            voice_id = voice.file_id
            duration = voice.duration
            file_size = getattr(voice, 'file_size', 0)

            # Get current active workout if any
            active_workout = self.db.get_active_workout(tg_user_id)
            workout_link = active_workout if active_workout else ""

            # Store voice note in database
            dt_now = datetime.now().isoformat()
            voice_content = f"Voice note: {duration}s, {file_size} bytes, ID: {voice_id}"

            # Save to database
            try:
                with sqlite3.connect(self.db.db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        INSERT INTO notes (tg_user_id, date_time, type, content, linked_workout_id)
                        VALUES (?, ?, ?, ?, ?)
                    """, (tg_user_id, dt_now, "voice_note", voice_content,
                          workout_link))
                    conn.commit()

                response_msg = (
                    f"🎤 Голосовое сообщение сохранено! (длительность: {duration}с)\n"
                    f"✅ Сохранено в базу данных.")

                if active_workout:
                    response_msg += f"\n🏋️ Связано с активной тренировкой: {active_workout}"
                else:
                    response_msg += "\n💡 Нет активной тренировки. Запустите /train чтобы начать."

                response_msg += "\n\n⚠️ Транскрипция пока недоступна - добавляйте подходы текстом через /add"

            except Exception as db_error:
                logger.error(
                    f"Failed to save voice note to database: {db_error}")
                response_msg = (
                    f"🎤 Голосовое сообщение получено! (длительность: {duration}с)\n"
                    "⚠️ Не удалось сохранить в базу данных.\n"
                    "💡 Пока что добавляйте подходы текстом через /add")

            await message.answer(response_msg)

        except Exception as e:
            logger.error(f"Error handling voice message: {e}")
            await message.answer(
                "❌ Ошибка обработки голосового сообщения.\n"
                "Попробуйте добавить подходы текстом через /add")

    async def handle_text(self, message: Message):
        """Handle text messages that might contain sets or wizard input"""
        if not message.from_user:
            return
            
        tg_user_id = message.from_user.id

        # Check if user is in wizard state for different input steps
        if tg_user_id in self.user_states:
            state = self.user_states[tg_user_id]
            
            # Handle age input
            if state.get('step') == 'age':
                try:
                    age = int(message.text.strip())
                    if 10 <= age <= 100:
                        state['wizard_data']['age'] = str(age)
                        await self.wizard_step_gender_message(message, state)
                    else:
                        await message.answer("⚠️ Пожалуйста, введите корректный возраст (от 10 до 100 лет)")
                except ValueError:
                    await message.answer("⚠️ Пожалуйста, введите возраст числом (например: 25)")
                return
            
            # Handle height input  
            elif state.get('step') == 'height':
                try:
                    height = int(message.text.strip())
                    if 120 <= height <= 250:
                        state['wizard_data']['height'] = height
                        await self.wizard_step_weight(message, state)
                    else:
                        await message.answer("⚠️ Пожалуйста, введите корректный рост (от 120 до 250 см)")
                except ValueError:
                    await message.answer("⚠️ Пожалуйста, введите рост числом (например: 175)")
                return
            
            # Handle weight input
            elif state.get('step') == 'weight':
                try:
                    weight = float(message.text.strip().replace(',', '.'))
                    if 30 <= weight <= 300:
                        state['wizard_data']['weight'] = int(weight)
                        await self.wizard_step_goal_message(message, state)
                    else:
                        await message.answer("⚠️ Пожалуйста, введите корректный вес (от 30 до 300 кг)")
                except ValueError:
                    await message.answer("⚠️ Пожалуйста, введите вес числом (например: 70)")
                return
            
            # Handle workout time input
            elif state.get('step') == 'workout_time':
                import re
                time_pattern = r'^([01]?[0-9]|2[0-3]):([0-5][0-9])$'
                if re.match(time_pattern, message.text.strip()):
                    state['wizard_data']['workout_time'] = message.text.strip()
                    await self.wizard_step_motivation_message(message, state)
                else:
                    await message.answer("⚠️ Пожалуйста, введите время в формате ЧЧ:ММ (например: 18:30)")
                return
            
            # Handle motivation text input (hybrid input)
            elif state.get('step') == 'motivation':
                # Сохраняем произвольную мотивацию от пользователя
                state['wizard_data']['motivation'] = message.text.strip()
                # Завершаем мастер и генерируем программу
                await self.generate_ai_program_message(message, state)
                return
            
            # Handle health text input (custom restrictions)
            elif state.get('step') == 'health_text_input':
                input_text = message.text.strip()
                
                if input_text.lower() == 'отмена':
                    # Return to health restrictions selection
                    state['step'] = 'health_restrictions'
                    await self.wizard_step_medical_message(message, state)
                else:
                    # Add custom restriction
                    restrictions = state['wizard_data']['health_restrictions']
                    if 'нет проблем' in restrictions:
                        restrictions.remove('нет проблем')
                    restrictions.append(f"другое: {input_text}")
                    
                    # Return to selection menu with confirmation
                    state['step'] = 'health_restrictions'
                    await self.wizard_step_medical_message(message, state)
                return

            # Handle medical restrictions input
            elif state.get('step') == 'medical':
                # Save medical restrictions text input
                medical_text = message.text or ""
                if medical_text.lower() in ['нет', 'no', 'отсутствуют', 'нету']:
                    state['wizard_data']['chronic_diseases'] = 'нет'
                else:
                    state['wizard_data']['chronic_diseases'] = medical_text
                
                # Move to next step - workout time
                await self.wizard_step_workout_time(message, state)
                return

        # Check if user has active workout
        workout_id = self.db.get_active_workout(tg_user_id)
        if workout_id:
            # Try to parse as sets
            text = message.text or ""
            await self.process_sets(message, text, workout_id)
        else:
            await message.answer(
                "Начните тренировку командой /train, чтобы добавлять подходы.\n"
                "Или используйте /start для настройки аккаунта.")
    
    async def cmd_export(self, message: Message):
        """Handle /export command - export user data"""
        if not message.from_user:
            return
            
        tg_user_id = message.from_user.id
        
        try:
            # Check if user exists
            user = self.db.get_user(tg_user_id)
            if not user:
                await message.answer(
                    "Сначала используйте команду /start для настройки аккаунта.")
                return
            
            await message.answer("📤 Экспорт данных...\nПожалуйста, подождите...")
            
            # Get all user data
            with sqlite3.connect(self.db.db_path) as conn:
                # Export workouts
                workouts_df = pd.read_sql_query(
                    "SELECT * FROM workouts WHERE tg_user_id = ?", 
                    conn, params=[tg_user_id])
                
                # Export sets
                sets_df = pd.read_sql_query("""
                    SELECT s.* FROM sets s 
                    JOIN workouts w ON s.workout_id = w.workout_id 
                    WHERE w.tg_user_id = ?
                """, conn, params=[tg_user_id])
                
                # Export PRs
                prs_df = pd.read_sql_query(
                    "SELECT * FROM prs WHERE tg_user_id = ?", 
                    conn, params=[tg_user_id])
            
            # Create summary
            total_workouts = len(workouts_df)
            total_sets = len(sets_df)
            total_tonnage = (sets_df['weight'] * sets_df['reps']).sum() if not sets_df.empty else 0
            
            summary = (
                f"📊 <b>Экспорт данных завершен!</b>\n\n"
                f"🏋️ Всего тренировок: {total_workouts}\n"
                f"💪 Всего подходов: {total_sets}\n"
                f"⚖️ Общий тоннаж: {total_tonnage:.1f} кг\n\n"
                f"📝 Данные сохранены в CSV файлы.\n"
                f"Для получения файлов обратитесь к администратору бота."
            )
            
            await message.answer(summary)
            
        except Exception as e:
            logger.error(f"Error in export command: {e}")
            await message.answer("❌ Ошибка при экспорте данных.")
    
    async def cmd_delete_me(self, message: Message):
        """Handle /delete_me command - delete user account"""
        if not message.from_user:
            return
            
        tg_user_id = message.from_user.id
        
        try:
            # Check if user exists
            user = self.db.get_user(tg_user_id)
            if not user:
                await message.answer("Аккаунт не найден.")
                return
            
            # Create confirmation keyboard
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Да, удалить ВСЕ данные", callback_data="confirm_delete")],
                [InlineKeyboardButton(text="✅ Отмена", callback_data="cancel_delete")]
            ])
            
            await message.answer(
                "⚠️ <b>ВНИМАНИЕ!</b> ⚠️\n\n"
                "Вы действительно хотите удалить свой аккаунт?\n\n"
                "Это действие удалит:\n"
                "• Все ваши тренировки\n"
                "• Всю статистику\n"
                "• Все подходы и упражнения\n"
                "• Личные рекорды\n"
                "• Все заметки\n\n"
                "⚠️ <b>Это действие НЕОБРАТИМО!</b>",
                reply_markup=keyboard
            )
            
        except Exception as e:
            logger.error(f"Error in delete_me command: {e}")
            await message.answer("❌ Ошибка при удалении аккаунта.")
    
    async def check_workout_notifications(self):
        """Legacy notification check - maintained for backward compatibility"""
        # This method is kept for backward compatibility but core notifications
        # are now handled by individual user scheduler jobs in setup_user_notifications
        logger.info("Legacy notification check - individual user jobs handle notifications now")
    
    def setup_user_notifications(self, tg_user_id: int, schedule_days: str, preferred_time_local: str, user_tz: str = DEFAULT_TIMEZONE):
        """Setup individual notification jobs for a specific user"""
        try:
            # Remove existing notifications for this user first
            self.remove_user_notifications(tg_user_id)
            
            if not schedule_days or not preferred_time_local:
                logger.info(f"User {tg_user_id} has no schedule configured")
                return
            
            # Parse preferred time (format: "18:00" or "18:00-21:00")
            if ':' not in preferred_time_local:
                logger.warning(f"Invalid time format for user {tg_user_id}: {preferred_time_local}")
                return
            
            # Handle time ranges like "18:00-21:00" - use the start time
            time_part = preferred_time_local.split('-')[0]  # Take start of range
            time_parts = time_part.split(':')
            workout_hour = int(time_parts[0])
            workout_minute = int(time_parts[1]) if len(time_parts) > 1 else 0
            
            # Parse schedule days
            days_mapping = {
                'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
                'friday': 4, 'saturday': 5, 'sunday': 6
            }
            
            workout_days = []
            for day in schedule_days.lower().split(','):
                day = day.strip()
                if day in days_mapping:
                    workout_days.append(days_mapping[day])
            
            if not workout_days:
                logger.warning(f"No valid days found for user {tg_user_id}: {schedule_days}")
                return
            
            user_timezone = timezone(user_tz)
            
            # Schedule notifications for each workout day
            for day_of_week in workout_days:
                # Food/Water reminder - 2 hours before workout
                food_hour = workout_hour - 2
                food_day_of_week = day_of_week
                if food_hour < 0:
                    food_hour += 24
                    # Shift to previous day when crossing midnight
                    food_day_of_week = (day_of_week - 1) % 7
                
                self.scheduler.add_job(
                    func=self.send_food_water_reminder,
                    trigger='cron',
                    args=[tg_user_id, preferred_time_local],
                    day_of_week=food_day_of_week,
                    hour=food_hour,
                    minute=workout_minute,
                    timezone=user_timezone,
                    id=f'food_reminder_{tg_user_id}_{day_of_week}',
                    replace_existing=True
                )
                
                # Session plan reminder - 20 minutes before workout
                plan_hour = workout_hour
                plan_minute = workout_minute - 20
                plan_day_of_week = day_of_week
                if plan_minute < 0:
                    plan_minute += 60
                    plan_hour -= 1
                    if plan_hour < 0:
                        plan_hour += 24
                        # Shift to previous day when crossing midnight
                        plan_day_of_week = (day_of_week - 1) % 7
                
                self.scheduler.add_job(
                    func=self.send_session_plan_reminder,
                    trigger='cron',
                    args=[tg_user_id],
                    day_of_week=plan_day_of_week,
                    hour=plan_hour,
                    minute=plan_minute,
                    timezone=user_timezone,
                    id=f'plan_reminder_{tg_user_id}_{day_of_week}',
                    replace_existing=True
                )
                
                # Main workout reminder - at workout time
                self.scheduler.add_job(
                    func=self.send_workout_reminder,
                    trigger='cron',
                    args=[tg_user_id, preferred_time_local],
                    day_of_week=day_of_week,
                    hour=workout_hour,
                    minute=workout_minute,
                    timezone=user_timezone,
                    id=f'workout_reminder_{tg_user_id}_{day_of_week}',
                    replace_existing=True
                )
            
            logger.info(f"Scheduled notifications for user {tg_user_id} on {len(workout_days)} days")
            
        except Exception as e:
            logger.error(f"Error setting up notifications for user {tg_user_id}: {e}")
    
    def remove_user_notifications(self, tg_user_id: int):
        """Remove all notification jobs for a specific user"""
        try:
            # Remove all types of notifications for all days
            for day in range(7):
                for notification_type in ['food_reminder', 'plan_reminder', 'workout_reminder']:
                    job_id = f'{notification_type}_{tg_user_id}_{day}'
                    try:
                        self.scheduler.remove_job(job_id)
                        logger.debug(f"Removed job {job_id}")
                    except Exception:
                        pass  # Job might not exist
            
            logger.info(f"Removed all notifications for user {tg_user_id}")
        except Exception as e:
            logger.error(f"Error removing notifications for user {tg_user_id}: {e}")
    
    async def send_food_water_reminder(self, tg_user_id: int, workout_time: str):
        """Send food and water reminder 2 hours before workout"""
        try:
            await self.bot.send_message(
                tg_user_id,
                f"🍎💧 <b>Предтренировочное напоминание!</b>\n\n"
                f"Тренировка начинается в {workout_time} (через 2 часа)\n\n"
                f"💡 <b>Рекомендации:</b>\n"
                f"• Поешьте за 1.5-2 часа до тренировки\n"
                f"• Выпейте 300-500мл воды\n"
                f"• Избегайте тяжелой пищи\n"
                f"• Можно съесть банан или энергетический батончик\n\n"
                f"⏰ Следующее напоминание: за 20 минут до тренировки"
            )
            logger.info(f"Sent food/water reminder to user {tg_user_id}")
        except Exception as e:
            logger.error(f"Error sending food/water reminder to user {tg_user_id}: {e}")
    
    async def send_session_plan_reminder(self, tg_user_id: int):
        """Send session plan reminder 20 minutes before workout"""
        try:
            # Get user's active program if available
            programs = self.db.get_user_programs(tg_user_id)
            today_plan = "Готовьтесь к тренировке!"
            
            if programs:
                # Get today's plan from the most recent active program
                # This is a simplified version - can be enhanced with AI
                today_plan = "🏋️ Сегодня: Силовая тренировка\n• Разминка 10 мин\n• Основные упражнения\n• Заминка 5 мин"
            
            await self.bot.send_message(
                tg_user_id,
                f"📋 <b>План на сегодня!</b>\n\n"
                f"{today_plan}\n\n"
                f"💡 <b>Не забудьте:</b>\n"
                f"• Размяться перед тренировкой\n"
                f"• Подготовить воду\n"
                f"• Настроить музыку\n"
                f"• Начать тренировку командой /train\n\n"
                f"🕙 Время тренировки: через 20 минут!"
            )
            logger.info(f"Sent session plan reminder to user {tg_user_id}")
        except Exception as e:
            logger.error(f"Error sending session plan reminder to user {tg_user_id}: {e}")
    
    async def send_workout_reminder(self, tg_user_id: int, workout_time: str):
        """Send main workout reminder at workout time"""
        try:
            # Check if user doesn't have an active workout already
            active_workout = self.db.get_active_workout(tg_user_id)
            if active_workout:
                logger.info(f"User {tg_user_id} already has active workout, skipping reminder")
                return
            
            current_time = datetime.now()
            current_day = current_time.strftime('%A')
            
            await self.bot.send_message(
                tg_user_id,
                f"🏋️ <b>Время тренировки!</b> 💪\n\n"
                f"📅 {current_day}, {workout_time}\n"
                f"⏰ Пора начинать!\n\n"
                f"🚀 <b>Начните тренировку:</b>\n"
                f"Используйте команду /train\n\n"
                f"💡 <b>Помните:</b>\n"
                f"• Следите за техникой\n"
                f"• Пейте воду между подходами\n"
                f"• Записывайте результаты"
            )
            logger.info(f"Sent workout reminder to user {tg_user_id}")
        except Exception as e:
            logger.error(f"Error sending workout reminder to user {tg_user_id}: {e}")
    
    def setup_existing_user_notifications(self):
        """Set up notifications for all existing users on bot startup"""
        try:
            with sqlite3.connect(self.db.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT tg_user_id, schedule_days, preferred_time_local, tz
                    FROM users 
                    WHERE schedule_days IS NOT NULL AND preferred_time_local IS NOT NULL
                    AND schedule_days != '' AND preferred_time_local != ''
                """)
                users_with_schedules = cursor.fetchall()
            
            setup_count = 0
            for user_id, schedule_days, preferred_time, user_tz in users_with_schedules:
                try:
                    if schedule_days and preferred_time:
                        tz = user_tz or DEFAULT_TIMEZONE
                        self.setup_user_notifications(user_id, schedule_days, preferred_time, tz)
                        setup_count += 1
                except Exception as user_error:
                    logger.error(f"Error setting up notifications for user {user_id}: {user_error}")
            
            logger.info(f"Set up notifications for {setup_count} users on startup")
            
        except Exception as e:
            logger.error(f"Error setting up existing user notifications: {e}")
    
    async def check_workout_timers(self):
        """Check for expired workout timers and send notifications"""
        try:
            expired_timers = self.db.check_expired_timers()
            
            for timer in expired_timers:
                try:
                    user_id = timer['tg_user_id']
                    workout_id = timer['workout_id']
                    duration = timer['timer_duration_min']
                    
                    await self.bot.send_message(
                        user_id,
                        f"⏰ <b>Напоминание о тренировке!</b>\n\n"
                        f"Прошло {duration} минут с начала тренировки.\n"
                        f"Тренировка: {workout_id}\n\n"
                        f"💡 Не забудьте:\n"
                        f"• Пить воду\n"
                        f"• Следить за техникой\n"
                        f"• Завершить тренировку командой /finish"
                    )
                    
                    # Mark timer as notified
                    self.db.mark_timer_notified(timer['id'])
                    
                except Exception as timer_error:
                    logger.error(f"Error sending timer notification: {timer_error}")
                    
        except Exception as e:
            logger.error(f"Error checking workout timers: {e}")

    async def send_weekly_reports(self):
        """Send weekly reports to all users"""
        try:
            with sqlite3.connect(self.db.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT DISTINCT tg_user_id FROM users")
                users = cursor.fetchall()

            for user_row in users:
                tg_user_id = user_row[0]
                try:
                    # Create a fake message for the week command
                    class FakeMessage:

                        def __init__(self, user_id, bot_instance):
                            # Create a proper user-like object with proper typing
                            class FakeUser:
                                def __init__(self, user_id):
                                    self.id = user_id
                            self.from_user = FakeUser(user_id)
                            self.bot_instance = bot_instance

                        async def answer(self, text, **kwargs):
                            await self.bot_instance.send_message(
                                self.from_user.id, text, **kwargs)

                    fake_message = FakeMessage(tg_user_id, self.bot)
                    # Use duck typing - FakeMessage has the same interface as Message for our needs
                    await self.cmd_week(fake_message)  # type: ignore

                except Exception as e:
                    logger.error(
                        f"Error sending weekly report to user {tg_user_id}: {e}"
                    )

        except Exception as e:
            logger.error(f"Error in weekly reports job: {e}")

    async def backup_database(self):
        """Backup SQLite database to CSV files"""
        backup_dir = os.environ.get('BACKUP_DIR', DEFAULT_BACKUP_DIR)

        try:
            backup_path = Path(backup_dir) / datetime.now().strftime(
                '%Y-%m-%d')
            backup_path.mkdir(parents=True, exist_ok=True)

            with sqlite3.connect(self.db.db_path) as conn:
                # Backup all tables to CSV
                tables = [
                    'users', 'workouts', 'sets', 'prs', 'weekly_stats', 'notes'
                ]

                for table in tables:
                    try:
                        df = pd.read_sql_query(f"SELECT * FROM {table}", conn)
                        if not df.empty:
                            csv_path = backup_path / f"{table}.csv"
                            df.to_csv(csv_path, index=False)
                            logger.info(
                                f"Backed up {table} table to {csv_path}")
                    except Exception as e:
                        logger.error(f"Error backing up {table} table: {e}")

                # Also create a full database backup
                db_backup_path = backup_path / "fitness_bot_backup.db"
                with open(db_backup_path, 'wb') as backup_file:
                    for line in conn.iterdump():
                        backup_file.write(f'{line}\n'.encode('utf-8'))

                logger.info(f"Database backup completed to {backup_path}")

        except Exception as e:
            logger.error(f"Error in database backup job: {e}")

    async def start_polling(self):
        """Start the bot polling"""
        logger.info("Starting Fitness Logger Bot...")
        self.scheduler.start()
        await self.dp.start_polling(self.bot)


async def main():
    """Main function"""
    try:
        # Start keep-alive server
        keep_alive = KeepAliveServer()
        keep_alive.start()

        # Start bot
        bot = FitnessBot()
        await bot.start_polling()

    except Exception as e:
        logger.error(f"Error starting bot: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
