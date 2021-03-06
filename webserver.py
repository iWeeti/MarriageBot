import asyncio
import os
import argparse
import secrets
import ssl
import warnings
import logging

from aiohttp.web import Application, AppRunner, TCPSite, middleware, HTTPFound
import discord
from discord.ext import commands
from aiohttp_jinja2 import template, setup as jinja_setup
from aiohttp_session import setup as session_setup, SimpleCookieStorage
from aiohttp_session.cookie_storage import EncryptedCookieStorage as ECS
from aiohttp_session.redis_storage import RedisStorage
from jinja2 import FileSystemLoader
import toml

from cogs import utils
import website


# Set up loggers
logging.getLogger().setLevel(logging.INFO)
logging.basicConfig(format='%(name)s:%(levelname)s: %(message)s')
logger = logging.getLogger('marriagebot.web')
logger.setLevel(logging.INFO)


# Filter warnings
warnings.filterwarnings('ignore', category=RuntimeWarning)


# Parse arguments
parser = argparse.ArgumentParser()
parser.add_argument("config_file", help="The configuration for the bot.")
parser.add_argument("gold_config_file", help="The configuration for the Gold version of the bot.")
parser.add_argument("--nossl", action="store_true", default=False, help="Starts the bot with no SSL web server.")
parser.add_argument("--host", type=str, default='0.0.0.0', help="The host IP to run the webserver on.")
parser.add_argument("--port", type=int, default=8080, help="The port to run the webserver on.")
parser.add_argument("--sslport", type=int, default=8443, help="The port to run the SSL webserver on.")
args = parser.parse_args()


# Read config
with open(args.config_file) as a:
    config = toml.load(a)
with open(args.gold_config_file) as a:
    gold_config = toml.load(a)


# Create website object - don't start based on argv
app = Application(loop=asyncio.get_event_loop(), debug=False)
app.add_routes(website.frontend_routes)
app.add_routes(website.backend_routes)
app.router.add_static('/static', os.getcwd() + '/website/static', append_version=True)
app.router.add_static('/trees', config['tree_file_location'], append_version=True)
app['static_root_url'] = '/static'
jinja_setup(app, loader=FileSystemLoader(os.getcwd() + '/website/templates'))

# Add our connections
app['database'] = utils.DatabaseConnection
utils.DatabaseConnection.logger = logger.getChild("db")
app['redis'] = utils.RedisConnection
utils.RedisConnection.logger = logger.getChild("redis")

# Add our configs
app['config'] = config
app['gold_config'] = gold_config

# Add our bots
app['bot'] = utils.CustomBot(config_file=args.config_file, logger=logger.getChild("bot"))
app['gold_bot'] = utils.CustomBot(config_file=args.gold_config_file, logger=logger.getChild("goldbot"))


if __name__ == '__main__':
    """Starts the bot (and webserver if specified) and runs forever"""

    loop = app.loop

    # Connect the bot
    logger.info("Logging in bot")
    loop.run_until_complete(app['bot'].login(app['config']['token']))
    logger.info("Logging in gold")
    loop.run_until_complete(app['gold_bot'].login(app['gold_config']['token']))

    # Connect the database
    logger.info("Creating database pool")
    loop.run_until_complete(utils.DatabaseConnection.create_pool(app['config']['database']))

    # Connect the redis
    logger.info("Creating redis pool")
    loop.run_until_complete(utils.RedisConnection.create_pool(app['config']['redis']))

    # Connect redis to middleware
    logger.info("Connecting Redis to app")
    storage = RedisStorage(utils.RedisConnection.pool)
    session_setup(app, storage)

    # Start the server unless I said otherwise
    webserver = None
    ssl_webserver = None

    # HTTP server
    logger.info("Creating webserver...")
    application = AppRunner(app)
    loop.run_until_complete(application.setup())
    webserver = TCPSite(application, host=args.host, port=args.port)

    # SSL server
    try:
        if not args.nossl:
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_context.load_cert_chain(**app['config']['ssl_context'])
            ssl_webserver = TCPSite(application, host=args.host, port=args.sslport, ssl_context=ssl_context)
    except Exception as e:
        ssl_webserver = None
        logger.exception("Could not make SSL webserver")

    # Start servers
    loop.run_until_complete(webserver.start())
    logger.info(f"Server started - http://{args.host}:{args.port}/")
    if ssl_webserver:
        loop.run_until_complete(ssl_webserver.start())
        logger.info(f"Server started - https://{args.host}:{args.sslport}/")

    # This is the forever loop
    try:
        logger.info("Running webserver")
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    logger.info("Closing webserver")
    loop.run_until_complete(application.cleanup())
    logger.info("Closing database pool")
    loop.run_until_complete(utils.DatabaseConnection.pool.close())
    logger.info("Closing bot")
    loop.run_until_complete(app['bot'].close())
    logger.info("Closing asyncio loop")
    loop.close()
