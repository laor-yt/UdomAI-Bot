import os
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv
from pyrogram import Client
from utils import get_temp_dir, start_auto_cleanup_routine

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

from web_dashboard import run_dashboard_server
from self_improver import improver

def main():
    """Start the bot."""
    load_dotenv()
    
    # Start web management dashboard server immediately so Render health checks succeed
    threading.Thread(target=run_dashboard_server, daemon=True).start()
    
    # Start autonomous self-improvement background loop (runs analysis & optimization every 10 minutes)
    improver.start_background_loop(interval_seconds=600)
    
    # Start auto background temp file purging routine
    start_auto_cleanup_routine()

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    api_id = os.environ.get("API_ID")
    api_hash = os.environ.get("API_HASH")
    
    if not token or token == "your_bot_token_here":
        logger.error("No valid Telegram Bot Token found. Please update .env")
        return
        
    if not api_id or not api_hash:
        logger.error("API_ID and API_HASH are required for Pyrogram. Please add them to your environment variables.")
        return

    # Ensure temp dir exists
    get_temp_dir()

    logger.info("Bot started...")
    
    app = Client(
        "my_bot",
        bot_token=token,
        api_id=int(api_id),
        api_hash=api_hash,
        plugins=dict(root="plugins")
    )
    
    app.start()
    
    async def set_commands():
        from pyrogram.types import BotCommand
        try:
            await app.set_bot_commands([
                BotCommand("start", "Start the bot"),
                BotCommand("download", "Download media from a link"),
                BotCommand("convert", "Convert a media file"),
                BotCommand("image", "Generate an image with AI"),
                BotCommand("ask", "Ask the AI a question"),
                BotCommand("search", "Search the web"),
                BotCommand("howtouse", "How to Use guide (Khmer & English)"),
                BotCommand("help", "Show help info")
            ])
            logger.info("Bot commands set successfully!")
        except Exception as e:
            logger.warning(f"Skipped setting bot commands: {e}")
            
    app.loop.run_until_complete(set_commands())
    
    import pyrogram
    pyrogram.idle()
    
    app.stop()

if __name__ == "__main__":
    main()
