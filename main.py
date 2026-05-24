import os
import json
import hashlib
import hmac
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

import asyncpg
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
# Render uses PORT environment variable
PORT = int(os.getenv("PORT", 8080))
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://shoaibrza9999-dino.hf.space")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# Global variables
app_state = {}
tg_app = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    app_state["pool"] = await asyncpg.create_pool(DATABASE_URL)
    
    # Initialize DB
    async with app_state["pool"].acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS scores (
                user_id BIGINT PRIMARY KEY,
                username VARCHAR(255),
                first_name VARCHAR(255),
                best_score INT DEFAULT 0,
                coins INT DEFAULT 0
            )
        ''')
        # In case the table exists without coins
        try:
            await conn.execute('ALTER TABLE scores ADD COLUMN coins INT DEFAULT 0')
        except asyncpg.exceptions.DuplicateColumnError:
            pass
            
    # Init Telegram Bot
    global tg_app
    tg_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Handlers
    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CommandHandler("leaderboard", leaderboard))
    
    await tg_app.initialize()
    if WEBHOOK_URL:
        await tg_app.bot.set_webhook(url=f"{WEBHOOK_URL}/webhook")
    await tg_app.start()

    yield

    # Shutdown
    await tg_app.stop()
    await tg_app.shutdown()
    await app_state["pool"].close()

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Adjust in production if needed
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    # Pass chat_id dynamically so the game knows where to send the score
    app_url = f"{FRONTEND_URL}?chat_id={chat_id}"
    
    keyboard = [
        [InlineKeyboardButton("Play Dino", web_app=WebAppInfo(url=app_url))]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.effective_chat.type in ['group', 'supergroup']:
        msg = "Click the button below to play the Dino game in this group!"
    else:
        msg = "Click the button below to play the Dino game!"
        
    await update.message.reply_text(msg, reply_markup=reply_markup)

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with app_state["pool"].acquire() as conn:
        records = await conn.fetch("SELECT first_name, best_score, coins FROM scores ORDER BY best_score DESC LIMIT 10")
    
    if not records:
        await update.message.reply_text("No scores yet. Be the first to play!")
        return
        
    msg = "🏆 *Dino Leaderboard* 🏆\n\n"
    for i, r in enumerate(records, 1):
        name = r['first_name'] or "Anonymous"
        coins = r['coins'] or 0
        msg += f"{i}. {name} - {r['best_score']} pts | 🪙 {coins} coins\n"
        
    await update.message.reply_text(msg, parse_mode="Markdown")

@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"status": "ok"}

class ScorePayload(BaseModel):
    initData: str
    score: int
    chat_id: str = None

def validate_telegram_data(init_data: str, token: str) -> dict:
    parsed_data = dict(parse_qsl(init_data))
    if 'hash' not in parsed_data:
        return None
        
    hash_val = parsed_data.pop('hash')
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
    secret_key = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    
    if calculated_hash == hash_val:
        return json.loads(parsed_data.get('user', '{}'))
    return None

async def notify_score(user_id: int, chat_id: int, first_name: str, score: int, coins_earned: int, is_new_best: bool):
    msg = f"🎮 {first_name} scored {score} points!"
    if coins_earned > 0:
        msg += f"\n🪙 Earned {coins_earned} coins."
    if is_new_best:
        msg += "\n🎉 New High Score!"
        
    # Send to the group where the game was started, or the user's private chat
    target_chat = chat_id if chat_id else user_id
    try:
        await tg_app.bot.send_message(chat_id=target_chat, text=msg)
    except Exception as e:
        print(f"Failed to send message: {e}")

@app.post("/api/score")
async def submit_score(payload: ScorePayload, background_tasks: BackgroundTasks):
    user_data = validate_telegram_data(payload.initData, TELEGRAM_BOT_TOKEN)
    if not user_data:
        raise HTTPException(status_code=401, detail="Invalid initData")
        
    # The user who is ACTUALLY playing the game right now
    user_id = user_data.get('id')
    username = user_data.get('username')
    first_name = user_data.get('first_name')
    score = payload.score
    
    coins_earned = score // 100
    is_new_best = False
    
    async with app_state["pool"].acquire() as conn:
        record = await conn.fetchrow("SELECT best_score FROM scores WHERE user_id = $1", user_id)
        if not record:
            await conn.execute(
                "INSERT INTO scores (user_id, username, first_name, best_score, coins) VALUES ($1, $2, $3, $4, $5)",
                user_id, username, first_name, score, coins_earned
            )
            is_new_best = True
        else:
            await conn.execute(
                "UPDATE scores SET coins = coins + $1 WHERE user_id = $2",
                coins_earned, user_id
            )
            if score > record['best_score']:
                await conn.execute(
                    "UPDATE scores SET best_score = $1, username = $2, first_name = $3 WHERE user_id = $4",
                    score, username, first_name, user_id
                )
                is_new_best = True

    try:
        chat_id = int(payload.chat_id) if payload.chat_id else None
    except:
        chat_id = None

    background_tasks.add_task(notify_score, user_id, chat_id, first_name, score, coins_earned, is_new_best)
    
    return {"status": "success", "is_new_best": is_new_best, "coins_earned": coins_earned}

@app.get("/")
def health_check():
    return {"status": "ok"}
