import os
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv
from pyrogram import Client
from utils import get_temp_dir, start_auto_cleanup_routine

# Global Pyrogram app instance for Telegram client
app = None

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Suppress Hugging Face Hub unauthenticated warning logs
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
logging.getLogger("huggingface_hub.utils._http").setLevel(logging.ERROR)

from web_dashboard import run_dashboard_server
from self_improver import improver
import scheduler

def main():
    """Start the bot."""
    load_dotenv()
    
    # Enable optional async IO mode via environment variable
    ENABLE_ASYNC_IO = os.getenv('ENABLE_ASYNC_IO', 'false').lower() == 'true'
    
    # Start web management dashboard server immediately so Render health checks succeed
    threading.Thread(target=run_dashboard_server, daemon=False).start()
    
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
    
    global app
    app = Client(
        "my_bot",
        bot_token=token,
        api_id=int(api_id),
        api_hash=api_hash,
        plugins=dict(root="plugins")
    )
    
    # Start the scheduler loop
    scheduler.run_scheduler_loop(app)
    
    import time
    from pyrogram.errors import FloodWait
    
    import asyncio
    import concurrent.futures
    loop = asyncio.get_event_loop()
    # Adjust thread pool size based on async mode and CPU cores
    if ENABLE_ASYNC_IO:
        cpu = os.cpu_count() or 2
        max_workers = cpu * 2
    else:
        max_workers = 64
    loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=max_workers))
    
    server_id = os.environ.get("SERVER_ID", "main")
    
    if server_id == "main":
        # Startup with exponential backoff on FloodWait
        max_retries = 5
        retry = 0
        while True:
            try:
                app.start()
                break
            except FloodWait as e:
                wait_time = getattr(e, "value", 60)
                backoff = min(wait_time * (2 ** retry), 300)
                logger.warning(f"⚠️ Telegram FloodWait during startup! Sleeping {backoff}s before retrying...")
                time.sleep(backoff + 2)
                retry += 1
                if retry >= max_retries:
                    logger.error("Maximum FloodWait retries reached. Exiting.")
                    raise
    else:
        logger.info(f"Skipping Main Bot startup (SERVER_ID is '{server_id}', not 'main').")
    
    async def set_commands():
        from pyrogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault
        from plugins.core import start_shared_user_client
        try:
            base_commands = [
                BotCommand("start", "Start the bot"),
                BotCommand("download", "Download media from a link"),
                BotCommand("convert", "Convert a media file"),
                BotCommand("image", "Generate an image with AI"),
                BotCommand("ask", "Ask the AI a question"),
                BotCommand("search", "Search the web"),
                BotCommand("publish", "📤 Publish file/text to a group or channel"),
                BotCommand("mychats", "📋 List groups/channels where I'm admin"),
                BotCommand("howtouse", "How to Use guide (Khmer & English)"),
                BotCommand("help", "Show help info")
            ]
            
            admin_commands = base_commands + [
                BotCommand("dashboard", "⚙️ Open Web Dashboard"),
                BotCommand("setfb", "Configure Facebook Page"),
                BotCommand("checkfb", "Check Facebook config"),
                BotCommand("postfb", "Post to Facebook"),
                BotCommand("allfb", "List all users with custom FB pages (Super Admin)")
            ]
            
            # Set default commands for everyone
            await app.set_bot_commands(base_commands, scope=BotCommandScopeDefault())
            
            # Set extended commands for SUPER_ADMINs
            if server_id == "main":
                try:
                    from user_manager import load_users
                    users = load_users()
                    for uid_str, udata in users.items():
                        if uid_str != "__system_config__" and udata.get("role") == "SUPER_ADMIN":
                            try:
                                await app.set_bot_commands(admin_commands, scope=BotCommandScopeChat(int(uid_str)))
                            except Exception as inner_e:
                                logger.warning(f"Failed to set admin commands for {uid_str}: {inner_e}")
                except Exception as e:
                    logger.warning(f"Error iterating users for admin commands: {e}")
                logger.info("Bot commands set successfully!")
            else:
                logger.info(f"Skipped setting main bot commands because SERVER_ID is '{server_id}'")
        except Exception as e:
            logger.warning(f"Skipped setting bot commands: {e}")

        # Start shared user client for private channel downloads (if USER_SESSION set)
        user_session = os.environ.get("USER_SESSION") or os.environ.get("STRING_SESSION")
        if user_session:
            await start_shared_user_client(api_id, api_hash, user_session)
            logger.info("Shared user client started for private channel downloads.")
        else:
            logger.info("USER_SESSION not set — private channel downloads disabled (bot must be a member).")

        # Pre-load admin chats list on startup
        try:
            from plugins.core import refresh_admin_chats
            await refresh_admin_chats(app)
            logger.info("Admin chats refreshed on startup.")
        except Exception as e:
            logger.warning(f"Could not refresh admin chats on startup: {e}")
            
        # Start all custom bots
        import custom_bots_manager
        custom_bots_manager.main_loop = app.loop
        await custom_bots_manager.start_all_approved_bots()
            
    # set_commands initializes other clients, so we still run it on all servers.
    # The actual bot commands for 'my_bot' will only be set if SERVER_ID == 'main'.
    app.loop.run_until_complete(set_commands())
    
    import pyrogram
    try:
        pyrogram.idle()
    finally:
        # Clean up custom bots
        import custom_bots_manager
        app.loop.run_until_complete(custom_bots_manager.stop_all_bots())
        
        # Clean up shared user client on shutdown
        from plugins.core import stop_shared_user_client
        app.loop.run_until_complete(stop_shared_user_client())
    
    app.stop()

if __name__ == "__main__":
    main()
