# Orisa, a simple Discord bot with good intentions
# Copyright (C) 2018, 2019 Dennis Brakhane
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, version 3 only
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
import logging
import os
import traceback
import yaml


import multio
import trio

import raven
from curious.dataclasses.presence import Game, GameType, Status
from curious.commands.exc import ConversionFailedError

from . import web
from .config import SENTRY_DSN, BOT_TOKEN, GLADOS_TOKEN, MASHERY_API_KEY, DEVELOPMENT
from .i18n import I18NCommandsManager
from .models import Database
from .orisa import Orisa, OrisaClient


multio.init("trio")

with open("logging.yaml") as logfile:
    logging.config.dictConfig(yaml.safe_load(logfile))

logger = logging.getLogger("orisa.main")

if SENTRY_DSN:
    logger.info("USING SENTRY")
    raven_client = raven.Client(
        dsn=SENTRY_DSN, release=raven.fetch_git_sha(os.path.dirname(__file__))
    )

    @client.event("command_error")
    async def command_error(ev_ctx, ctx, err):
        exc_info = (type(err), err, err.__traceback__)
        raven_client.captureException(exc_info)
        fmtted = "".join(traceback.format_exception(*exc_info))
        logger.error(f"Error in command!\n{fmtted}")


else:
    raven_client = None
    logger.info("NOT USING SENTRY")


client = OrisaClient(BOT_TOKEN)

database = Database()

manager = I18NCommandsManager.with_client(
    client, command_prefix="!" if not DEVELOPMENT else ","
)

already_loaded = False


@client.event("ready")
async def ready(ctx):
    global already_loaded

    if already_loaded:
        logger.info("Ignoring second call to ready")
    else:
        already_loaded = True
        await manager.load_plugin(Orisa, database, raven_client)

    logger.debug(f"I'm in {len(ctx.bot.guilds)} guilds, shard id is {ctx.shard_id}")

    msg = "!ow help" if not DEVELOPMENT else ",ow help"
    await ctx.bot.change_status(game=Game(name=msg, type=GameType.LISTENING_TO))

    class Logger:
        def before_task_step(self, task):
            logger.debug(f">>> task step {task.name}")

        def task_exited(self, task):
            logger.debug(f"<<< task end {task.name}")

    # trio.hazmat.add_instrument(Logger())

@client.event("command_error")
async def command_error(ev_ctx, ctx, err):
    if isinstance(err, ConversionFailedError):
        await ctx.channel.messages.send(str(err))
    else:
        fmtted = ''.join(traceback.format_exception(type(err), err, err.__traceback__))
        logger.error(f"Error in command!\n{fmtted}")

client.run(autoshard=False)
