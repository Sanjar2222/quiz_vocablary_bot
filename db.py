import aiosqlite
import json
import os
from datetime import datetime, timedelta

DB_NAME = "voc_quiz.db"

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS words (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                word TEXT UNIQUE NOT NULL,
                translation TEXT,
                example_en TEXT,
                example_uz TEXT,
                grammar_note TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                word_id INTEGER NOT NULL,
                is_learned INTEGER DEFAULT 0,
                review_count INTEGER DEFAULT 0,
                next_review_at TIMESTAMP,
                last_reviewed_at TIMESTAMP,
                UNIQUE(user_id, word_id)
            )
        """)
        await db.commit()

async def add_or_update_word(word: str, translation: str = "", example_en: str = "", example_uz: str = "", grammar_note: str = ""):
    word = word.strip().lower()
    if not word:
        return None
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            INSERT INTO words (word, translation, example_en, example_uz, grammar_note)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(word) DO UPDATE SET
                translation = CASE WHEN ? != '' THEN ? ELSE translation END,
                example_en = CASE WHEN ? != '' THEN ? ELSE example_en END,
                example_uz = CASE WHEN ? != '' THEN ? ELSE example_uz END,
                grammar_note = CASE WHEN ? != '' THEN ? ELSE grammar_note END
        """, (word, translation, example_en, example_uz, grammar_note,
              translation, translation, example_en, example_en, example_uz, example_uz, grammar_note, grammar_note))
        await db.commit()
        
        cursor = await db.execute("SELECT id FROM words WHERE word = ?", (word,))
        row = await cursor.fetchone()
        word_id = row[0] if row else None

    return word_id

async def delete_word_by_id(word_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM words WHERE id = ?", (word_id,))
        await db.execute("DELETE FROM user_progress WHERE word_id = ?", (word_id,))
        await db.commit()

async def get_all_words():
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM words ORDER BY id ASC")
        return [dict(row) for row in await cursor.fetchall()]

async def get_word_by_index(index: int):
    words = await get_all_words()
    if 0 <= index < len(words):
        return words[index], len(words)
    return None, len(words)

async def get_quiz_words(user_id: int):
    """
    Returns words that are:
    1. Not yet reviewed by this user (no record in user_progress)
    2. Marked as unlearned (is_learned = 0)
    3. Learned, but next_review_at <= now (ready for SRS review)
    """
    now = datetime.now()
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        query = """
            SELECT w.*, p.is_learned, p.review_count, p.next_review_at
            FROM words w
            LEFT JOIN user_progress p ON w.id = p.word_id AND p.user_id = ?
            WHERE p.word_id IS NULL 
               OR p.is_learned = 0 
               OR p.next_review_at <= ?
            ORDER BY 
                CASE 
                    WHEN p.word_id IS NULL THEN 0
                    WHEN p.is_learned = 0 THEN 1
                    ELSE 2
                END,
                w.id ASC
        """
        cursor = await db.execute(query, (user_id, now))
        return [dict(row) for row in await cursor.fetchall()]

async def get_unlearned_words(user_id: int):
    """Returns words that the user explicitly marked as 'do not know' or are still unlearned"""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        query = """
            SELECT w.*, p.is_learned, p.review_count
            FROM words w
            JOIN user_progress p ON w.id = p.word_id AND p.user_id = ?
            WHERE p.is_learned = 0
            ORDER BY p.last_reviewed_at DESC
        """
        cursor = await db.execute(query, (user_id,))
        return [dict(row) for row in await cursor.fetchall()]

async def mark_word_learned(user_id: int, word_id: int, days: int = 3):
    """When user knows the word: sets is_learned = 1 and schedules next review after 3+ days"""
    now = datetime.now()
    next_review = now + timedelta(days=days)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            INSERT INTO user_progress (user_id, word_id, is_learned, review_count, next_review_at, last_reviewed_at)
            VALUES (?, ?, 1, 1, ?, ?)
            ON CONFLICT(user_id, word_id) DO UPDATE SET
                is_learned = 1,
                review_count = review_count + 1,
                next_review_at = ?,
                last_reviewed_at = ?
        """, (user_id, word_id, next_review, now, next_review, now))
        await db.commit()

async def mark_word_unlearned(user_id: int, word_id: int):
    """When user does not know: sets is_learned = 0 and schedules review immediately"""
    now = datetime.now()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            INSERT INTO user_progress (user_id, word_id, is_learned, review_count, next_review_at, last_reviewed_at)
            VALUES (?, ?, 0, 1, ?, ?)
            ON CONFLICT(user_id, word_id) DO UPDATE SET
                is_learned = 0,
                review_count = review_count + 1,
                next_review_at = ?,
                last_reviewed_at = ?
        """, (user_id, word_id, now, now, now, now))
        await db.commit()

async def get_user_stats(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        # Total words
        cursor = await db.execute("SELECT COUNT(*) FROM words")
        total_words = (await cursor.fetchone())[0]

        # Learned words (is_learned = 1)
        cursor = await db.execute("SELECT COUNT(*) FROM user_progress WHERE user_id = ? AND is_learned = 1", (user_id,))
        learned_words = (await cursor.fetchone())[0]

        # Unlearned words (is_learned = 0)
        cursor = await db.execute("SELECT COUNT(*) FROM user_progress WHERE user_id = ? AND is_learned = 0", (user_id,))
        unlearned_words = (await cursor.fetchone())[0]

        # Not yet started
        new_words = max(0, total_words - (learned_words + unlearned_words))

        return {
            "total": total_words,
            "learned": learned_words,
            "unlearned": unlearned_words,
            "new": new_words
        }
