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
import logging.config

import csv
import functools
import math
import re
import random
import os
import traceback
import unicodedata

from contextlib import contextmanager, nullcontext, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import groupby, count
from io import BytesIO, StringIO
from operator import attrgetter, itemgetter
from string import Template
from typing import Optional

import asks
import dateutil.parser as date_parser
import hypercorn.config
import hypercorn.trio
import html5lib
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import multio
import numpy as np
import pandas as pd
import pendulum
import raven
import seaborn as sns
import tabulate
import trio
import yaml

from curious import event
from curious.commands.context import Context
from curious.commands.conditions import author_has_roles
from curious.commands.decorators import command, condition
from curious.commands.exc import ConversionFailedError
from curious.commands.plugin import Plugin
from curious.core.client import Client
from curious.core.httpclient import HTTPClient
from curious.exc import Forbidden, HierarchyError
from curious.dataclasses.channel import ChannelType
from curious.dataclasses.embed import Embed
from curious.dataclasses.guild import Guild
from curious.dataclasses.member import Member
from curious.dataclasses.presence import Game, Status
from fuzzywuzzy import process, fuzz
from lxml import html
from oauthlib.oauth2 import WebApplicationClient
from sqlalchemy.orm import joinedload, selectinload
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.sql import func, desc, and_
from itsdangerous.url_safe import URLSafeTimedSerializer
from itsdangerous.exc import BadSignature
from wcwidth import wcswidth

from .config import (
    GuildConfig,
    CHANNEL_NAMES,
    GLADOS_TOKEN,
    MASHERY_API_KEY,
    SENTRY_DSN,
    SIGNING_SECRET,
    OAUTH_CLIENT_ID,
    OAUTH_REDIRECT_HOST,
    OAUTH_REDIRECT_PATH,
    PRIVACY_POLICY_PATH,
    WEB_APP_PATH,
)
from .models import Cron, User, BattleTag, SR, Role, GuildConfigJson
from .exceptions import (
    BlizzardError,
    InvalidBattleTag,
    UnableToFindSR,
    NicknameTooLong,
    InvalidFormat,
)
from .utils import (
    get_sr,
    sort_secondaries,
    send_long,
    reply,
    resolve_tag_or_index,
    format_roles,
)
from . import web


logger = logging.getLogger("orisa")


oauth_serializer = URLSafeTimedSerializer(SIGNING_SECRET)

SUPPORT_DISCORD="https://discord.gg/tsNxvFh"
VOTE_LINK="https://discordbots.org/bot/445905377712930817/vote"

RANKS = ("Bronze", "Silver", "Gold", "Platinum", "Diamond", "Master", "Grand Master")
COLORS = (
    0xCD7E32,  # Bronze
    0xC0C0C0,  # Silver
    0xFFD700,  # Gold
    0xE5E4E2,  # Platinum
    0xA2BFD3,  # Diamond
    0xF9CA61,  # Master
    0xF1D592,  # Grand Master
)

# Conditions


def correct_channel(ctx):
    return (
        any(
            ctx.channel.id == guild.listen_channel_id
            for guild in Orisa._instance.guild_config.values()
        )
        or ctx.channel.private
    )


def only_owner(ctx):
    try:
        return (
            ctx.author.id == ctx.bot.application_info.owner.id and ctx.channel.private
        )
    except AttributeError:
        # application_info is None
        return False


def only_owner_all_channels(ctx):
    try:
        return ctx.author.id == ctx.bot.application_info.owner.id
    except AttributeError:
        # application_info is None
        return False


# Main Orisa code
class Orisa(Plugin):

    SYMBOL_DPS = "\N{CROSSED SWORDS}"
    SYMBOL_TANK = "\N{SHIELD}"  # \N{VARIATION SELECTOR-16}'
    SYMBOL_SUPPORT = (
        "\N{VERY HEAVY GREEK CROSS}"
    )  #'\N{HEAVY PLUS SIGN}'   #\N{VERY HEAVY GREEK CROSS}'
    SYMBOL_FLEX = (
        "\N{ANTICLOCKWISE DOWNWARDS AND UPWARDS OPEN CIRCLE ARROWS}"
    )  # '\N{FLEXED BICEPS}'   #'\N{ANTICLOCKWISE DOWNWARDS AND UPWARDS OPEN CIRCLE ARROWS}'

    # dirty hack needed for correct_channel condition
    _instance = None

    def __init__(self, client, database, raven_client):
        super().__init__(client)
        Orisa._instance = self
        self.database = database
        self.dialogues = {}
        self.web_send_ch, self.web_recv_ch = trio.open_memory_channel(0)
        self.raven_client = raven_client

        self.guild_config = {}
        logger.debug("Loading config")
        with database.session() as session:
            for config in session.query(GuildConfigJson).filter(
                GuildConfigJson.id.in_(self.client.guilds.keys())
            ):
                self.guild_config[config.id] = data = GuildConfig.from_json2(
                    config.config
                )
                logger.debug("Configured %d as %s", config.id, data)

    async def load(self):

        await self.spawn(self._message_new_guilds)

        await self.spawn(self._sync_all_tags_task)

        await self.spawn(self._cron_task)

        await self.spawn(self._web_server)

        await self.spawn(self._oauth_result_listener)

    # admin commands

    @command()
    @condition(only_owner, bypass_owner=False)
    # @author_has_roles("Clan Administrator")
    async def shutdown(self, ctx, safety: str = None):
        if safety != "Orisa":
            await reply(
                ctx,
                "If you want me to shut down, you need to issue `!ow shutdown Orisa` exactly as shown",
            )
        logger.critical("***** GOT EMERGENCY SHUTDOWN COMMAND FROM OWNER *****")
        try:
            await reply(ctx, "Shutting down...")
        except:
            pass
        try:
            await self.client.kill()
        except:
            pass
        raise SystemExit(42)

    @command()
    @condition(only_owner, bypass_owner=False)
    async def createallchannels(self, ctx):
        logger.info("creating all channels")

        for gi in self.guild_config.values():
            for vc in gi.managed_voice_categories:
                await self._adjust_voice_channels(
                    self.client.find_channel(vc.category_id), create_all_channels=True
                )

    @command()
    @condition(only_owner, bypass_owner=False)
    async def adjustallchannels(self, ctx):
        for gi in self.guild_config.values():
            for vc in gi.managed_voice_categories:
                await self._adjust_voice_channels(
                    self.client.find_channel(vc.category_id)
                )

    @command()
    @condition(only_owner, bypass_owner=False)
    async def messageall(self, ctx, *, message: str):
        s = self.database.Session()
        try:
            users = s.query(User).all()
            for user in users:
                try:
                    logger.debug(f"Sending messsage to {user.discord_id}")
                    u = await self.client.get_user(user.discord_id)
                    await u.send(message)
                except:
                    logger.exception(f"Error while sending to {user.discord_id}")
            logger.debug("Done sending")
        finally:
            s.close()

    @command()
    @condition(only_owner, bypass_owner=False)
    async def messageserverowners(self, ctx, *, message: str):
        for guild in ctx.bot.guilds.values():
            try:
                logger.info("working on guild %s with owner %s (%s)", guild, guild.owner, guild.owner.name)
                await ctx.author.send(f"sending to {guild.owner.mention} ({guild.owner.name}) of {guild}")
                await guild.owner.send(message)
            except Exception:
                logger.exception("unable to send to owner of guild %s", guild)


    @command()
    @condition(only_owner, bypass_owner=False)
    async def post(self, ctx, channel_id: str, *, message: str):
        await self._post(ctx, channel_id, message, glados=False)

    @command()
    @condition(only_owner, bypass_owner=False)
    async def gpost(self, ctx, channel_id: str, *, message: str):
        await self._post(ctx, channel_id, message, glados=True)

    async def _post(self, ctx, channel_id: str, message: str, glados: bool):
        try:
            channel_id = CHANNEL_NAMES[channel_id]
        except KeyError:
            channel_id = int(channel_id)
        channel = self.client.find_channel(channel_id)
        with self.client.as_glados() if glados else nullcontext():
            msg = await channel.messages.send(message)
        await ctx.channel.messages.send(f"created {msg.id}")

    @command()
    @condition(only_owner, bypass_owner=False)
    async def gdelete(self, ctx, channel_id: str, message_id: int = None):
        with self.client.as_glados():
            await self.delete(ctx, channel_id, message_id)

    @command()
    @condition(only_owner, bypass_owner=False)
    async def delete(self, ctx, channel_id: str, message_id: int = None):
        match = re.match(
            r"https://discordapp.com/channels/[0-9]+/([0-9]+)/([0-9]+)", channel_id
        )
        if match:
            channel_id = int(match.group(1))
            message_id = int(match.group(2))

        try:
            channel_id = CHANNEL_NAMES[channel_id]
        except KeyError:
            channel_id = int(channel_id)
        # low level access, because getting a message requires MESSAGE_HISTORY permission
        await self.client.http.delete_message(channel_id, message_id)
        await ctx.channel.messages.send("deleted")

    #    @command()
    #    @condition(only_owner)
    #    async def updatehelp(self, ctx, channel_id: int, message_id: int):
    #        await self.client.http.edit_message(channel_id, message_id, embed=self._create_help().to_dict())
    #        await ctx.channel.messages.send("done")

    @command()
    @condition(only_owner)
    async def hs(self, ctx, style: str = "psql"):

        prev_date = datetime.utcnow() - timedelta(days=1)

        with self.database.session() as session:
            await self._top_players(session, prev_date, style)

    @command()
    @condition(only_owner)
    async def updatenicks(self, ctx):
        session = self.database.Session()
        for user in session.query(User).all():
            try:
                await self._update_nick(user)
            except Exception:
                if self.raven_client:
                    self.raven_client.captureException()
                logger.exception("something went wrong during updatenicks")
        await ctx.channel.messages.send("Done")

    @command()
    @condition(only_owner, bypass_owner=False)
    async def cleanup(self, ctx, *, doit: str = None):
        member_ids = [
            id for guild in self.client.guilds.values() for id in guild.members.keys()
        ]
        session = self.database.Session()
        try:
            registered_ids = [x[0] for x in session.query(User.discord_id).all()]
            stale_ids = set(registered_ids) - set(member_ids)
            ids = ", ".join(f"<@{id}>" for id in stale_ids)
            await ctx.channel.messages.send(
                f"there are {len(stale_ids)} stale entries: {ids}"
            )
            if doit == "confirm":
                for id in stale_ids:
                    user = self.database.user_by_discord_id(session, id)
                    if not user:
                        await ctx.channel.messages.send(f"{id} not found in DB???")
                    else:
                        session.delete(user)
                        await ctx.channel.messages.send(f"{user} deleted")
                session.commit()
            elif stale_ids:
                await ctx.channel.messages.send("issue `!cleanup confirm` to delete.")

        finally:
            session.close()

    @command()
    @condition(correct_channel)
    async def ping(self, ctx):
        await reply(ctx, "pong")

    # ow commands

    @command()
    @condition(correct_channel)
    async def ow(self, ctx, *, member: Member = None):
        def format_sr(tag):
            if not tag.sr:
                return "—"
            return f"{tag.sr} ({RANKS[tag.rank]})"

        member_given = member is not None
        if not member_given:
            member = ctx.author

        session = self.database.Session()

        content = embed = None
        try:
            user = self.database.user_by_discord_id(session, member.id)
            if user:
                embed = Embed(colour=0x659DBD)  # will be overwritten later if SR is set
                embed.add_field(name="Nick", value=member.name)

                primary, *secondary = user.battle_tags
                tag_value = f"**{primary.tag}**\n"
                tag_value += "\n".join(tag.tag for tag in secondary)

                sr_value = f"**{format_sr(primary)}**\n"
                sr_value += "\n".join(format_sr(tag) for tag in secondary)

                multiple_tags = len(user.battle_tags) > 1

                embed.add_field(
                    name="BattleTags" if multiple_tags else "BattleTag", value=tag_value
                )
                if any(tag.sr for tag in user.battle_tags):
                    embed.add_field(
                        name="SRs" if multiple_tags else "SR", value=sr_value
                    )

                if primary.rank is not None:
                    embed.colour = COLORS[primary.rank]

                if user.roles:
                    embed.add_field(name="Roles", value=format_roles(user.roles))

                embed.add_field(
                    name="Links",
                    inline=False,
                    value=f'[Overwatch profile](https://playoverwatch.com/en-us/career/pc/{primary.tag.replace("#", "-")}) | [Upvote Orisa]({VOTE_LINK}) | [Orisa Support Server]({SUPPORT_DISCORD})'
                )

                if multiple_tags:
                    footer_text = f"The SR of the primary BattleTag was last updated {pendulum.instance(primary.last_update).diff_for_humans()}."
                else:
                    footer_text = f"The SR was last updated {pendulum.instance(primary.last_update).diff_for_humans()}."

                if member == ctx.author and member_given:
                    footer_text += "\nBTW, you do not need to specify your nickname if you want your own BattleTag; just !ow is enough"
                embed.set_footer(text=footer_text)
            else:
                content = f"{member.name} not found in database! *Do you need a hug?*"
                if member == ctx.author:
                    embed = Embed(
                        title="Hint",
                        description="use `!ow register` to register, or `!ow help` for more info",
                    )
        finally:
            session.close()
        await ctx.channel.messages.send(content=content, embed=embed)

    @ow.subcommand()
    @condition(correct_channel)
    async def get(self, ctx, *, member: Member = None):
        r = await self.ow(ctx, member=member)
        return r

    @ow.subcommand()
    @condition(correct_channel)
    async def about(self, ctx):
        embed = Embed(
            title="About Me",
            description=(
                "I am an open source Discord bot to help manage Overwatch Discord communities.\n"
                "I'm written and maintained by Dennis Brakhane (Joghurt#2732 on Discord) and licensed under the "
                "[GNU Affero General Public License 3.0](https://www.gnu.org/licenses/agpl-3.0.en.html); the "
                "[development is done on Github](https://github.com/brakhane/Orisa)"
            ),
        )
        embed.add_field(
            name="Invite me to your own Discord",
            value=(
                "To invite me to your server, simply [click here](https://wur.st/bot/ever/invite), I will post a message with more information in a channel after I have joined your server"
            ),
        )
        embed.add_field(
            name="Join the official Orisa Discord",
            value=(
                f"If you use me in your Discord server, or generally have suggestions, [join the official Orisa Discord]({SUPPORT_DISCORD}). Updates and new features "
                "will be discussed and announced there"
            ),
        )
        await ctx.author.send(content=None, embed=embed)
        if not ctx.channel.private:
            await reply(ctx, "I've sent you a DM")

    @ow.subcommand()
    async def config(self, ctx, guild_id: int = None):
        is_owner = ctx.author == ctx.bot.application_info.owner
        if ctx.channel.private and not is_owner:
            await ctx.channel.messages.send(
                content="The config command must be issued from a channel of the guild to configure. "
                "Don't worry, I will send you the config instructions as a DM, so others can't configure me just by watching you sending this command.",
                embed=Embed(
                    title="Tip",
                    description="`!ow config` works in *any* channel (that I'm allowed to read messages in, of course), so you can also use an admin only channel",
                ),
            )
            return

        if not is_owner and not any(
            role.name.lower() == "orisa admin" for role in ctx.author.roles
        ):
            help_embed=Embed(
                title=":thinking: Need help?",
                description=f"Join the [Support Discord]({SUPPORT_DISCORD})"
            )
            await reply(
                ctx,
                "This command can only be used by members with the `Orisa Admin` role (only the name of the role is important, it doesn't need any permissions)"
            )
            try:
                await ctx.channel.messages.send(content=None, embed=help_embed)
            except Exception:
                logger.exception("unable to send help embed")
            logger.info(
                f"user {ctx.author} tried to issue ow config without being in Orisa Admin"
            )
            return

        if is_owner and guild_id is not None:
            token = web.create_token(guild_id)
        else:
            token = web.create_token(ctx.guild.id)

        embed = Embed(
            title="Click here to configure me", url=f"{WEB_APP_PATH}config/{token}"
        )
        embed.add_field(
            name=":thinking: Need help?",
            value=f"Join the [Support Discord]({SUPPORT_DISCORD})"
        )
        embed.set_footer(text="This link will be valid for 30 minutes")
        try:
            await ctx.author.send(content=None, embed=embed)
            await reply(ctx, "I sent you a DM")
        except Forbidden:
            await reply(ctx,
            "I tried to send you a DM with the link, but you disallow DM from server members. Please allow that and retry. (I can't post the link here because "
            "everybody who knows that link will be able to configure me for the next 30 minutes)")


    @ow.subcommand()
    @condition(correct_channel)
    async def register(self, ctx, *, ignored: str = None):
        user_id = ctx.message.author_id
        client = WebApplicationClient(OAUTH_CLIENT_ID)
        state = oauth_serializer.dumps(user_id)
        url, headers, body = client.prepare_authorization_request(
            "https://eu.battle.net/oauth/authorize",
            scope=[],
            redirect_url=f"{OAUTH_REDIRECT_HOST}{OAUTH_REDIRECT_PATH}",
            state=state,
        )
        embed = Embed(
            url=url,
            title="Click here to register",
            description="To complete your registration, I need your permission to ask Blizzard for your BattleTag. Please click "
            "the link above and give me permission to access your data. I only need this permission once, you can remove it "
            "later in your BattleNet account.",
        )
        embed.add_field(
            name=":information_source: Protip",
            value="If you want to register a secondary/smurf BattleTag, you can open the link in a private/incognito tab (try right clicking the link) and enter the "
            "account data for that account instead.",
        )
        embed.set_footer(
            text="By registering, you agree to Orisa's Privacy Policy; you can read it by entering !ow privacy"
        )

        await ctx.author.send(content=None, embed=embed)
        if not ctx.channel.private:
            await reply(ctx, "I sent you a DM with instructions.")

    @ow.subcommand()
    @condition(correct_channel)
    async def unregister(self, ctx, tag_or_index: str):
        session = self.database.Session()
        try:
            user = self.database.user_by_discord_id(session, ctx.author.id)
            if not user:
                await reply(
                    ctx, "You are not registered, there's nothing for me to do."
                )
                return

            try:
                index = resolve_tag_or_index(user, tag_or_index)
            except ValueError as e:
                await reply(ctx, e.args[0])
                return
            if index == 0:
                await reply(
                    ctx,
                    "You cannot unregister your primary BattleTag. Use `!ow setprimary` to set a different primary first, or "
                    "use `!ow forgetme` to delete all your data.",
                )
                return

            removed = user.battle_tags.pop(index)
            session.commit()
            await reply(ctx, f"Removed **{removed.tag}**")
            await self._update_nick_after_secondary_change(ctx, user)

        finally:
            session.close()

    async def _update_nick_after_secondary_change(self, ctx, user):
        try:
            await self._update_nick(user, force=True)
        except HierarchyError:
            pass
        except NicknameTooLong as e:
            await reply(
                ctx,
                f'However, your new nickname "{e.nickname}" is now longer than 32 characters, which Discord doesn\'t allow. '
                "Please choose a different format, or shorten your nickname and do a `!ow forceupdate` afterwards.",
            )
        except:
            await reply(
                ctx,
                "However, there was an error updating your nickname. I will try that again later.",
            )
        with suppress(HierarchyError):
            await self._update_nick(user)

    @ow.subcommand()
    @condition(correct_channel)
    async def setprimary(self, ctx, tag_or_index: str):
        session = self.database.Session()
        try:
            user = self.database.user_by_discord_id(session, ctx.author.id)
            if not user:
                await reply(ctx, "You are not registered. Use `!ow register` first.")
                return
            try:
                index = resolve_tag_or_index(user, tag_or_index)
            except ValueError as e:
                await reply(ctx, e.args[0])
                return
            if index == 0:
                await reply(
                    ctx,
                    f'"{user.battle_tags[0].tag}" already is your primary BattleTag. *Going back to sleep*',
                )
                return

            p, s = user.battle_tags[0], user.battle_tags[index]
            p.position = index
            s.position = 0
            session.commit()

            for i, t in enumerate(sorted(user.battle_tags[1:], key=attrgetter("tag"))):
                t.position = i + 1

            session.commit()

            await reply(
                ctx,
                f"Done. Your primary BattleTag is now **{user.battle_tags[0].tag}**.",
            )
            await self._update_nick_after_secondary_change(ctx, user)

        finally:
            session.close()

    @ow.subcommand()
    @condition(correct_channel)
    async def format(self, ctx, *, format: str):
        if "]" in format:
            await reply(ctx, "format string may not contain square brackets")
            return
        if not re.search(r"\$((sr|rank)(?!\w))|(\{(sr|rank)(?!\w)})", format):
            await reply(ctx, "format string must contain at least a $sr or $rank")
            return
        if not format:
            await reply(ctx, "format string missing")
            return

        session = self.database.Session()

        try:
            user = self.database.user_by_discord_id(session, ctx.author.id)
            if not user:
                await reply(ctx, "you must register first")
                return
            else:
                user.format = format
                try:
                    new_nick = await self._update_nick(user, force=True)
                except InvalidFormat as e:
                    await reply(
                        ctx, f'Invalid format string: unknown placeholder "{e.key}"'
                    )
                    session.rollback()
                except NicknameTooLong as e:
                    await reply(
                        ctx,
                        f"Sorry, using this format would make your nickname be longer than 32 characters ({len(e.nickname)} to be exact).\n"
                        f"Please choose a shorter format or shorten your nickname",
                    )
                    session.rollback()
                else:
                    titles = [
                        "Smarties Expert",
                        "Bread Scientist",
                        "Eternal Bosom of Hot Love",
                        "Sith Lord of Security",
                        "Namer of Clouds",
                        "Scourge of Beer Cans",
                        "Muse of Jeff Kaplan",
                        "Shredded Cheese Authority",
                        "MILF Commander",
                        "Cunning Linguist",
                        "Pork Rind Expert",
                        "Dinosaur Supervisor",
                        "Galactic Viceroy of C9",
                        "Earl of Bacon",
                        "Dean of Pizza",
                        "Duke of Tacos",
                        "Retail Jedi",
                        "Pornography Historian",
                    ]
                    # reset if SR should not be shown normally
                    await self._update_nick(user)
                    await reply(
                        ctx,
                        f'Done. Henceforth, ye shall be knownst as "{new_nick}, {random.choice(titles)}."',
                    )
        finally:
            session.commit()
            session.close()

    @ow.subcommand(aliases=("alwayshowsr",))
    @condition(correct_channel)
    async def alwaysshowsr(self, ctx, param: str = "on"):
        with self.database.session() as session:
            user = self.database.user_by_discord_id(session, ctx.author.id)
            if not user:
                await reply(ctx, "you are not registered")
                return
            new_setting = param != "off"
            user.always_show_sr = new_setting
            await self._update_nick(user)
            session.commit()

        msg = "Done. "
        if new_setting:
            msg += "Your nick will be updated even when you are not in an OW voice channel. Use `!ow alwaysshowsr off` to turn it off again"
        else:
            msg += "Your nick will only be updated when you are in an OW voice channel. Use `!ow alwaysshowsr on` to always update your nick"
        await reply(ctx, msg)

    @ow.subcommand()
    @condition(correct_channel, bypass_owner=False)
    async def forceupdate(self, ctx):
        session = self.database.Session()
        try:
            logger.info(f"{ctx.author.id} used forceupdate")
            user = self.database.user_by_discord_id(session, ctx.author.id)
            if not user:
                await reply(ctx, "you are not registered")
            else:
                fault = False
                async with ctx.channel.typing:
                    for tag in user.battle_tags:
                        try:
                            await self._sync_tag(session, tag)
                        except Exception as e:
                            if self.raven_client:
                                self.raven_client.captureException()
                            logger.exception(f"exception while syncing {tag}")
                            fault = True

                if fault:
                    await reply(
                        ctx,
                        "There were some problems updating your SR. Try again later.",
                    )
                else:
                    await reply(
                        ctx,
                        f"OK, I have updated your data. Your (primary) SR is now {user.battle_tags[0].sr}. "
                        "If that is not correct, you need to log out of Overwatch once and try again; your "
                        "profile also needs to be public for me to track your SR.",
                    )
        finally:
            session.commit()
            session.close()

    @ow.subcommand()
    async def forgetme(self, ctx):
        session = self.database.Session()
        try:
            user = self.database.user_by_discord_id(session, ctx.author.id)
            if user:
                logger.info(f"{ctx.author.name} ({ctx.author.id}) requested removal")
                user_id = user.discord_id
                try:
                    for guild in self.client.guilds.values():
                        try:
                            nn = str(guild.members[user_id].name)
                        except KeyError:
                            continue
                        new_nn = re.sub(r"\s*\[.*?\]", "", nn, count=1).strip()
                        try:
                            await guild.members[user_id].nickname.set(new_nn)
                        except HierarchyError:
                            pass
                except Exception:
                    logger.exception("Some problems while resetting nicks")
                session.delete(user)
                await reply(ctx, f"OK, deleted {ctx.author.name} from database")
                session.commit()
            else:
                await reply(
                    ctx,
                    "you are not registered anyway, so there's nothing for me to forget...",
                )
        finally:
            session.close()

    @ow.subcommand()
    @condition(correct_channel)
    async def findplayers(self, ctx, diff_or_min_sr: int = None, max_sr: int = None):
        await self._findplayers(ctx, diff_or_min_sr, max_sr, findall=False)

    @ow.subcommand()
    @condition(correct_channel)
    async def findallplayers(self, ctx, diff_or_min_sr: int = None, max_sr: int = None):
        await self._findplayers(ctx, diff_or_min_sr, max_sr, findall=True)

    @ow.subcommand()
    @condition(correct_channel)
    async def newsr(self, ctx, arg1, arg2=None):
        with self.database.session() as session:
            user = self.database.user_by_discord_id(session, ctx.author.id)
            if not user:
                raise reply(ctx, "You are not registered. Do `!ow register` first.")
                return

            if arg2 is None:
                sr_str = arg1
                tag = user.battle_tags[0]
            else:
                tag_str, sr_str = arg1, arg2

                try:
                    _, score, index = process.extractOne(
                        tag_str,
                        {t.position: t.tag for t in user.battle_tags},
                        score_cutoff=50,
                    )
                    tag = user.battle_tags[index]
                except (ValueError, TypeError):
                    tag = None

                if not tag:
                    await reply(
                        ctx,
                        f"I have no idea which of your BattleTags you mean by '{tag_str}'",
                    )
                    return
            # check for fat fingering
            force = False
            if sr_str.strip().endswith("!"):
                force = True
                sr_str = sr_str[:-1]
            try:
                if sr_str.strip().lower() == "none":
                    sr = None
                else:
                    sr = int(sr_str)
            except ValueError:
                await reply(
                    ctx,
                    "I don't know about you, but '{sr_str}' doesn't look like a number to me",
                )
                return

            if sr is not None:
                if not (500 <= sr <= 5000):
                    await reply(ctx, "SR must be between 500 and 5000")
                    return

                # check for fat finger
                if tag.sr and abs(tag.sr - sr) > 200 and not force:
                    await reply(
                        ctx,
                        f"Whoa! {sr} looks like a big change compared to your previous SR of {tag.sr}. To avoid typos, I will only update it if you are sure."
                        f"So, if that is indeed correct, reissue this command with a ! added to the SR, like `!ow newsr 1234!`",
                    )
                    return

            tag.update_sr(sr)
            rank = tag.rank
            image = f"https://d1u1mce87gyfbn.cloudfront.net/game/rank-icons/season-2/rank-{rank+1}.png"

            await self._handle_new_sr(session, tag, sr, image)
            session.commit()
            await reply(ctx, f"Done. The SR for *{tag.tag}* is now *{sr}*")

    @ow.subcommand()
    @condition(correct_channel)
    async def setrole(self, ctx, *, roles_str: str):
        "Alias for setroles"
        return await self.setroles(ctx, roles_str=roles_str)

    @ow.subcommand()
    @condition(correct_channel)
    async def setroles(self, ctx, *, roles_str: str):
        names = {
            "d": Role.DPS,
            "m": Role.MAIN_TANK,
            "o": Role.OFF_TANK,
            "s": Role.SUPPORT,
        }

        roles = Role.NONE

        for role in roles_str.replace(" ", "").lower():
            try:
                roles |= names[role]
            except KeyError:
                await reply(
                    ctx,
                    f"Unknown role identifier '{role}'. Valid role identifiers are: `d` (DPS), `m` (Main Tank), `o` (Off Tank), `s` (Support). They can be combined, eg. `ds` would mean DPS + Support.",
                )
                return

        session = self.database.Session()
        try:
            user = self.database.user_by_discord_id(session, ctx.author.id)
            if not user:
                await reply(ctx, "You are not registered! Do `!ow register` first.")
                return
            user.roles = roles
            session.commit()
            await reply(ctx, f"Done. Your roles are now **{format_roles(roles)}**.")
        finally:
            session.close()

    async def _findplayers(
        self, ctx, diff_or_min_sr: int = None, max_sr: int = None, *, findall
    ):
        logger.info(
            f"{ctx.author.id} issued findplayers {diff_or_min_sr} {max_sr} {findall}"
        )

        session = self.database.Session()
        try:
            asker = self.database.user_by_discord_id(session, ctx.author.id)
            if not asker:
                await reply(ctx, "you are not registered")
                return

            if max_sr is None:
                # we are looking around the askers SR
                sr_diff = diff_or_min_sr

                if sr_diff is not None:
                    if sr_diff <= 0:
                        await reply(ctx, "SR difference must be positive")
                        return

                    if sr_diff > 5000:
                        await reply(
                            ctx, "You just had to try ridiculous values, didn't you?"
                        )
                        return

                base_sr = asker.battle_tags[0].sr
                if not base_sr:
                    await reply(
                        ctx,
                        "You primary BattleTag has no SR, please give a SR range you want to search for instead",
                    )
                    return

                if sr_diff is None:
                    sr_diff = 1000 if base_sr < 3500 else 500

                min_sr, max_sr = base_sr - sr_diff, base_sr + sr_diff

                type_msg = f"within {sr_diff} of {base_sr} SR"

            else:
                # we are looking at a range
                min_sr = diff_or_min_sr

                if not (
                    (500 <= min_sr <= 5000)
                    and (500 <= max_sr <= 5000)
                    and (min_sr <= max_sr)
                ):
                    await reply(
                        ctx,
                        "min and max must be between 500 and 5000, and min must not be larger than max.",
                    )
                    return

                type_msg = f"between {min_sr} and {max_sr} SR"

            candidates = (
                session.query(BattleTag)
                .join(BattleTag.current_sr)
                .options(joinedload(BattleTag.user))
                .filter(SR.value.between(min_sr, max_sr))
                .all()
            )

            users = set(c.user for c in candidates)

            cmap = {u.discord_id: u for u in users}

            online = []
            offline = []

            for guild in self.client.guilds.values():

                if ctx.author.id not in guild.members:
                    continue

                for member in guild.members.values():
                    if member.user.id == ctx.author.id or member.user.id not in cmap:
                        continue
                    if member.status == Status.OFFLINE:
                        offline.append(member)
                    else:
                        online.append(member)

            def format_member(member):
                nonlocal cmap
                markup = "~~" if member.status == Status.DND else ""

                if member.status == Status.IDLE:
                    hint = "(idle)"
                elif member.status == Status.DND:
                    hint = "(DND)"
                else:
                    hint = ""

                return f"{markup}{str(member.name)}\u00a0{member.mention}{markup}\u00a0{hint}\n"

            msg = ""

            if not online:
                msg += f"There are no players currently online {type_msg}\n\n"
            else:
                msg += f"**The following players are currently online and {type_msg}:**\n\n"
                msg += "\n".join(format_member(m) for m in online)
                msg += "\n"

            if findall:

                if not offline:
                    if online:
                        msg += "There are no offline players within that range."
                    else:
                        msg += "There are also no offline players within that range. :("
                else:
                    msg += "**The following players are within that range, but currently offline:**\n\n"
                    msg += "\n".join(format_member(m) for m in offline)

            else:
                if offline:
                    msg += f"\nThere are also {len(offline)} offline players within that range. Use the `findallplayers` "
                    msg += "command to show them as well."

            await send_long(ctx.author.send, msg)
            if not ctx.channel.private:
                await reply(ctx, "I sent you a DM with the results.")

        finally:
            session.close()

    @ow.subcommand()
    async def help(self, ctx):
        if not ctx.channel.private:
            if (
                ctx.channel.guild_id not in self.guild_config
                or self.guild_config[ctx.channel.guild_id] == GuildConfig.default()
            ):
                await reply(
                    ctx,
                    "I'm not configured yet! Somebody with the role `Orisa Admin` needs to issue `!ow config` to configure me first!",
                )
                try:
                    await ctx.channel.messages.send(content=None, embed=Embed(
                        title=":thinking: Need help?",
                        description=f"Join the [Support Discord]({SUPPORT_DISCORD})"
                    ))
                except Exception:
                    logger.exception("Unable to send help embed")

                return
        forbidden = False
        for embed in self._create_help(ctx):
            try:
                await ctx.author.send(content=None, embed=embed)
            except Forbidden:
                forbidden = True
                break

        if forbidden:
            await reply(
                ctx,
                "I tried to send you a DM with help, but you don't allow DM from server members. "
                "I can't post it here, because it's rather long. Please allow DMs and try again.",
            )
        elif not ctx.channel.private:
            await reply(ctx, "I sent you a DM with instructions.")

    def _create_help(self, ctx):
        channel_id = None

        for guild in self.client.guilds.values():
            if ctx.author.id in guild.members:
                channel_id = self.guild_config[guild.id].listen_channel_id
                break

        embed = Embed(
            title="Orisa's purpose",
            description=(
                "When joining a QP or Comp channel, you need to know the BattleTag of a channel member, or they need "
                "yours to add you. In competitive channels it also helps to know which SR the channel members have. "
                "To avoid having to ask for this information again and again when joining a channel, this bot was created. "
                "When you register with your BattleTag, your nick will automatically be updated to show your "
                "current SR and it will be kept up to date. You can also ask for other member's BattleTag, or request "
                "your own so others can easily add you in OW.\n"
                "It will also send a short message to the chat when you ranked up.\n"
                f"*Like Overwatch's Orisa, this bot is quite young and still new at this. Report issues to <@!{self.client.application_info.owner.id}>*\n"
                f"\n**The commands only work in the <#{channel_id}> channel or by sending me a DM**\n"
                "If you are new to Orisa, you are probably looking for `!ow register`\n"
                "If you want to use Orisa on your own server or help developing it, enter `!ow about`"
            ),
        )
        embed.add_field(
            name="!ow [nick]",
            value=(
                "Shows the BattleTag for the given nickname, or your BattleTag "
                "if no nickname is given. `nick` can contain spaces. A fuzzy search for the nickname is performed.\n"
                "*Examples:*\n"
                "`!ow` will show your BattleTag\n"
                '`!ow the chosen one` will show the BattleTag of "tHE ChOSeN ONe"\n'
                '`!ow orisa` will show the BattleTag of "SG | Orisa", "Orisa", or "Orisad"\n'
                '`!ow oirsa` and `!ow ori` will probably also show the BattleTag of "Orisa"'
            ),
        )
        embed.add_field(
            name="!ow about",
            value="Shows information about Orisa, and how you can add her to your own Discord server, or help supporting her",
        )
        embed.add_field(
            name="!ow alwaysshowsr [on/off]",
            value="On some servers, Orisa will only show your SR or rank in your nick when you are in an OW voice channel. If you want your nick to always show your SR or rank, "
            "set this to on.\n"
            "*Example:*\n"
            "`!ow alwaysshowsr on`",
        )
        embed.add_field(
            name="!ow config",
            value='This command can only be used by members with the "Orisa Admin" role and allows them to configure Orisa for the specific Discord server',
        )
        embed.add_field(
            name="!ow findplayers [max diff] *or* !ow findplayers min max",
            value="*This command is still in beta and may change at any time!*\n"
            "This command is intended to find partners for your Competitive team and shows you all registered and online users within the specified range.\n"
            "If `max diff` is not given, the maximum range that allows you to queue with them is used, so 1000 below 3500 SR, and 500 otherwise. "
            "If `max diff` is given, it is used instead. `findplayers` then searches for all online players that around that range of your own SR.\n"
            "Alternatively, you can give two parameters, `!ow findplayers min max`. In this mode, `findplayers` will search for all online players that are between "
            "min and max.\n"
            "Note that `findplayers` will take all registered BattleTags of players into account, not just their primary.\n"
            "*Examples:*\n"
            "`!ow findplayers`: finds all players that you could start a competitive queue with\n"
            "`!ow findplayers 123`: finds all players that are within 123 SR of your SR\n"
            "`!ow findplayers 1500 2300`: finds all players between 1500 and 2300 SR\n",
        )
        embed.add_field(
            name="!ow findallplayers [max diff] *or* !ow findplayers min max",
            value="Same as `findplayers`, but also includes offline players",
        )
        embed.add_field(
            name="!ow forceupdate",
            value="Immediately checks your account data and updates your nick accordingly.\n"
            "*Checks and updates are done automatically, use this command only if "
            "you want your nick to be up to date immediately!*",
        )
        embed.add_field(
            name="!ow forgetme",
            value="All your BattleTags will be removed from the database and your nick "
            "will not be updated anymore. You can re-register at any time.",
        )

        embed.add_field(
            name="!ow format *format*",
            value="Lets you specify how your SR or rank is displayed. It will always be shown in [square\u00a0brackets] appended to your name.\n"
            "In the *format*, you can specify placeholders with `$placeholder` or `${placeholder}`. The second form is useful when there are no spaces "
            "between the placeholder name and the text. For example, to get `[2000 SR]`, you *can* use just `$sr SR`, however, to get `[2000SR]`, you need "
            "to use `${sr}SR`, because `$srSR` would refer to a nonexistant placeholder `srSR`.\n"
            "Your format string needs to use at least either `$sr` or `$rank`.\n",
        )
        embed.add_field(
            name="\N{BLACK STAR} *ow format placeholders (prepend a $)*",
            value="*The following placeholders are defined:*\n"
            f"`dps`, `tank`, `support`, `flex`\nSymbols for the respective roles: `{self.SYMBOL_DPS}`, `{self.SYMBOL_TANK}`, `{self.SYMBOL_SUPPORT}`, `{self.SYMBOL_FLEX}`\n\n"
            "`sr`\nyour SR; if you have secondary accounts, an asterisk (\*) is added at the end.\n\n"
            "`rank`\nyour Rank; if you have secondary accounts, an asterisk (\*) is added at the end.\n\n"
            "`secondary_sr`\nThe SR of your secondary account, if you have registered one.\nIf you have more than one secondary account (you really like to "
            "give Blizzard money, don't you), the first secondary account (sorted alphabetically) will be used; in that case, consider using `$sr_range` instead.\n\n"
            "`secondary_rank`\nLike `secondary_sr`, but shows the rank instead.\n\n"
            "`lowest_sr`, `highest_sr`\nthe lowest/highest SR of all your accounts, including your primary. Only useful if you have more than one secondary.\n\n"
            "`lowest_rank`, `highest_rank`\nthe same, just for rank.\n\n"
            "`sr_range`\nThe same as `${lowest_sr}–${highest_sr}`.\n\n"
            "`rank_range`\nDito, but for rank.\n",
        )
        embed.add_field(
            name="\N{BLACK STAR} *ow format examples*",
            value="`!ow format test $sr SR` will result in [test 2345 SR]\n"
            "`!ow format Potato/$rank` in [Potato/Gold].\n"
            "`!ow format $sr (alt: $secondary_sr)` in [1234* (alt: 2345)]\n"
            "`!ow format $sr ($sr_range)` in [1234* (600-4200)]\n"
            "`!ow format $sr ($rank_range)` in [1234* (Bronze-Grand Master)]\n\n"
            "*By default, the format is `$sr`*",
        )

        embeds = [embed]
        embed = Embed(title="help cont'd")
        embeds.append(embed)

        embed.add_field(
            name="!ow get nick",
            value=(
                "Same as `!ow [nick]`, (only) useful when the nick is the same as a command.\n"
                "*Example:*\n"
                '`!ow get register` will search for the nick "register"'
            ),
        )
        embed.add_field(
            name="!ow register",
            value="Create a link to your BattleNet account, or adds a secondary BattleTag to your account. "
            "Your OW account will be checked periodically and your nick will be "
            "automatically updated to show your SR or rank (see the *format* command for more info). ",
        )
        embed.add_field(name="!ow privacy", value="Show Orisa's Privacy Policy")
        embed.add_field(
            name="!ow setprimary *battletag*",
            value="Makes the given secondary BattleTag your primary BattleTag. Your primary BattleTag is the one you are currently using, the its SR is shown in your nick\n"
            "Unlike `register`, the search is performed fuzzy and case-insensitve, so you normally only need to give the first (few) letters.\n"
            "The given BattleTag must already be registered as one of your BattleTags.\n"
            "*Example:*\n"
            "`!ow setprimary jjonak`",
        )
        embed.add_field(
            name="!ow setprimary *index*",
            value="Like `!ow setprimary battletag`, but uses numbers, 1 is your first secondary, 2 your seconds etc. The order is shown by `!ow` (alphabetical)\n"
            "Normally, you should not need to use this alternate form, it's available in case Orisa gets confused on what BattleTag you mean (which shouldn't happen)\n"
            "*Example:*\n"
            "`!ow setprimary 1`",
        )
        embed.add_field(
            name="!ow setroles *roles*",
            value="Sets the role you can/want to play. It will be shown in `!ow` and will also be used to update the number of roles "
            "in voice channels you join.\n"
            '*roles* is a single "word" consisting of one or more of the following identifiers (both upper and lower case work):\n'
            "`d` for DPS, `m` for Main Tank, `o` for Off Tank, `s` for Support\n"
            "*Examples:*\n"
            "`!ow setroles d`: you only play DPS\n"
            "`!ow setroles so`: you play Support and Off Tanks\n"
            "`!ow setroles dmos`: you are a true Flex and play everything.",
        )
        embed.add_field(
            name="!ow srgraph [from_date]",
            value="*This command is in beta and can change at any time; it might also have bugs, report them please*\n"
            "Shows a graph of your SR. If from_date (as DD.MM.YY or YYYY-MM-DD) is given, the graph starts at that date, otherwise it starts "
            "as early as Orisa has data.",
        )
        embed.add_field(
            name="!ow unregister *battletag*",
            value="If you have secondary BattleTags, you can remove the given BattleTag from the list. Unlike register, the search is performed fuzzy, so "
            "you normally only have to specify the first few letters of the BattleTag to remove.\n"
            "You cannot remove your primary BattleTag, you have to choose a different primary BattleTag first.\n"
            "*Example:*\n"
            "`!ow unregister foo`",
        )
        embed.add_field(
            name="!ow unregister *index*",
            value="Like `unregister battletag`, but removes the battletag by number. Your first secondary is 1, your second 2, etc.\n"
            "The order is shown by the `!ow` command (it's alphabetical).\n"
            "Normally, you should not need to use this alternate form, it's available in case Orisa gets confused on what BattleTag you mean (which shouldn't happen)\n"
            "*Example:*\n"
            "`!ow unregister 1`",
        )

        return embeds

    @ow.subcommand()
    @condition(correct_channel)
    async def srgraph(self, ctx, date: str = None):

        with self.database.session() as session:
            user = self.database.user_by_discord_id(session, ctx.author.id)
            if not user:
                await reply(ctx, "You are not registered. Do `!ow register` first.")
                return
            else:
                await self._srgraph(ctx, user, ctx.author.name, date)

    @ow.subcommand()
    @author_has_roles("Admin")
    async def usersrgraph(self, ctx, member: Member, date: str = None):
        with self.database.session() as session:
            user = self.database.user_by_discord_id(session, member.id)
            if not user:
                await reply(ctx, f"{member.name} not registered")
                return
            else:
                await self._srgraph(ctx, user, member.name, date)

    @ow.subcommand()
    async def privacy(self, ctx):
        with open(PRIVACY_POLICY_PATH) as f:
            text = f.read()
        text = text.replace("OWNER_ID", f"<@!{self.client.application_info.owner.id}>")
        await send_long(ctx.author.send, text)
        if not ctx.channel.private:
            await reply(ctx, "I sent you the privacy policy as DM.")

    async def _srgraph(self, ctx, user, name, date: str = None):
        sns.set()

        tag = user.battle_tags[0]

        data = [(sr.timestamp, sr.value) for sr in tag.sr_history]

        if not data:
            await ctx.channel.messages.send(
                f"There is no data yet for {tag.tag}, try again later"
            )
            return

        data = pd.DataFrame.from_records(reversed(data), columns=["timestamp", "sr"])

        if date:
            try:
                date = date_parser.parse(
                    date, parserinfo=date_parser.parserinfo(dayfirst=True)
                )
            except ValueError:
                await reply(
                    ctx,
                    f"I don't know what date {date} is supposed to mean. Please use "
                    "the format DD.MM.YY or YYYY-MM-DD",
                )

            data = data[data.timestamp >= date].reset_index(drop=True)

        fig, ax = plt.subplots()

        ax.xaxis_date()

        data.set_index("timestamp").sr.plot(style="C0", ax=ax, drawstyle="steps-post")


        for is_max, ix in enumerate([data.sr.idxmin(), data.sr.idxmax()]):
            col = "C2" if is_max else "C1"

            val = data.iloc[ix].sr
            ax.axhline(y=val, color=col, linestyle="--")

            ax.annotate(
                int(val),
                xy=(1, val),
                xycoords=("axes fraction", "data"),
                xytext=(5, -3),
                textcoords="offset points",
                color=col,
            )

        # data.set_index("timestamp").sr.plot(style="C0", ax=ax, drawstyle="steps-post")

        if False:
            for ix in data.sr[pd.isna].index:
                x = data.iloc[ix - 1 : ix]
                x = x.append(data.iloc[ix + 1 : ix + 2])
                x.loc[0, "timestamp"] = data.iloc[ix].timestamp
                x.set_index("timestamp").sr.plot(
                    style="C0:", ax=ax
                )  # drawstyle="steps-post")

        ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%d.%m."))
        # ax.xaxis.set_major_locator(matplotlib.dates.HourLocator(byhour=(0, 12)))
        plt.xlabel("Date")
        plt.ylabel("SR")

        image = BytesIO()
        plt.savefig(format="png", fname=image, transparent=False)
        image.seek(0)
        embed = Embed(
            title=f"SR History For {name}",
            description=f"Here is your SR history starting from "
            f"{'when you registered' if not date else pendulum.instance(date).to_formatted_date_string()}.\n"
            "A dotted line means that you had no SR during that time (probably due to off-season)",
        )
        embed.set_image(image_url="attachment://graph.png")
        await ctx.channel.messages.upload(
            image, filename="graph.png", message_embed=embed
        )

    # Events
    @event("member_update")
    async def _member_update(self, ctx, old_member: Member, new_member: Member):
        def plays_overwatch(m):
            try:
                return m.game.name == "Overwatch"
            except AttributeError:
                return False

        async def wait_and_fire(ids_to_sync):
            logger.debug(
                f"sleeping for 20s before syncing after OW close of {new_member.name}"
            )
            await trio.sleep(20)
            await self._sync_tags(ids_to_sync)
            logger.debug(f"done syncing tags for {new_member.name} after OW close")

        if plays_overwatch(old_member) and (not plays_overwatch(new_member)):
            session = self.database.Session()
            try:
                user = self.database.user_by_discord_id(session, new_member.user.id)
                if not user:
                    logger.debug(
                        f"{new_member.name} stopped playing OW but is not registered, nothing to do."
                    )
                    return

                ids_to_sync = [t.id for t in user.battle_tags]
                logger.info(
                    f"{new_member.name} stopped playing OW and has {len(ids_to_sync)} BattleTags that need to be checked"
                )
            finally:
                session.close()

            await self.spawn(wait_and_fire, ids_to_sync)

    @event("voice_state_update")
    async def _voice_state_update(self, ctx, member, old_voice_state, new_voice_state):
        parent = None
        if old_voice_state:
            parent = old_voice_state.channel.parent
            if parent:
                await self._adjust_voice_channels(parent)

        if new_voice_state:
            if new_voice_state.channel.parent != parent:
                if new_voice_state.channel.parent:
                    await self._adjust_voice_channels(new_voice_state.channel.parent)

        with self.database.session() as session:
            user = self.database.user_by_discord_id(session, member.id)
            if user:
                formatted = self._format_nick(user)
                try:
                    await self._update_nick_for_member(member, formatted)
                except Exception:
                    logger.exception("Unable to update nick for member %s", member)

    @event("message_create")
    async def _message_create(self, ctx, msg):
        # logger.debug(f"got message {msg.author} {msg.channel} {msg.content} {msg.snowflake_timestamp}")
        if msg.content.startswith("!ow"):
            logger.info(
                f"{msg.author.name} in {msg.channel} issued {msg.content}"
            )
        if msg.content.startswith("!"):
            return
        if msg.channel.private and re.match(r"^[0-9]{3,4}!?$", msg.content.strip()):
            # single number, special case for newsr
            await self.newsr(Context(msg, ctx), msg.content.strip())

    @event("guild_member_remove")
    async def _guild_member_remove(self, ctx: Context, member: Member):
        logger.debug(
            f"Member {member.name}({member.id}) left the guild ({member.guild})"
        )
        if member.id == ctx.bot.user.id:
            logger.info(f"Seems like I was kicked from guild {member.guild}")
            with self.database.session() as session:
                gc = (
                    session.query(GuildConfigJson)
                    .filter_by(id=member.guild.id)
                    .one_or_none()
                )
                if gc:
                    logger.info("That guild was configured")
                    session.delete(gc)
                with suppress(KeyError):
                    del self.guild_config[member.guild.id]
                session.commit()
        else:
            with self.database.session() as session:
                user = self.database.user_by_discord_id(session, member.id)
                if user:
                    in_other_guild = False
                    for guild in self.client.guilds.values():
                        if guild.id != member.guild.id and member.id in guild.members:
                            in_other_guild = True
                            logger.debug(f"{member.name} is still in guild {guild.id}")
                            break
                    if not in_other_guild:
                        logger.info(
                            f"deleting {user} from database because {member.name} left the guild and has no other guilds"
                        )
                        session.delete(user)
                        session.commit()

    @event("guild_join")
    async def _guild_joined(self, ctx: Context, guild: Guild):
        logger.info("Joined guild %r", guild)
        await self._handle_new_guild(guild)

    @event("guild_streamed")
    async def _guild_streamed(self, ctx, guild):
        logger.info("Streamed guild %r", guild)
        if guild.id not in self.guild_config:
            await self._handle_new_guild(guild)

    async def _handle_new_guild(self, guild):
        logger.info("We have a new guild %s \o/", guild)
        self.guild_config[guild.id] = GuildConfig.default()

        msg = (
            "*Greetings*! I am excited to be here :smiley:\n"
            "To get started, create a new role named `Orisa Admin` (only the name is important, it doesn't need any special permissions) and add yourself "
            "and everybody that should be allowed to configure me.\n"
            "Then, write `!ow config` in this channel and I will send you a link to configure me via DM.\n"
            "*I will ignore all commands except `!ow help` and `!ow config` until I'm configured for this Discord!*"
        )

        # try to find a channel to post the first hello message to
        channels = sorted(guild.channels.values(), key=attrgetter("position"))

        # if there is a welcome channel, try that first
        logger.debug("system channel is %r", guild.system_channel)
        if guild.system_channel:
            channels = [guild.system_channel] + list(channels)
        for channel in channels:
            if (
                channel.type == ChannelType.TEXT
                # when read_messages is false, no messages can be sent even if send_messages is true
                and channel.effective_permissions(guild.me).send_messages
                and channel.effective_permissions(guild.me).read_messages
            ):
                logger.debug("found hello channel %s", channel)
                try:
                    await channel.messages.send(msg)
                    logger.debug("message successfully sent")
                except Exception:
                    logger.exception(
                        "Got exception when trying to send to channel %s, checking another one",
                        channel
                    )
                    continue
                else:
                    # found one
                    try:
                        await channel.messages.send(content=None, embed=Embed(
                            title=":thinking: Need help?",
                            description=f"Join the [Support Discord]({SUPPORT_DISCORD})"))
                    except Exception:
                        logger.exception("Unable to send support embed")

                    break
        else:
            logger.debug(
                "no valid hello channel found. Falling back to DM to owner for %s",
                guild,
            )
            await guild.owner.send(
                msg
                + f"\n\n*Somebody (hopefully you) invited me to your server {guild.name}, but I couldn't find a "
                f"text channel I am allowed to send messages to, so I have to message you directly)*"
            )
            try:
                await channel.messages.send(content=None, embed=Embed(
                    title=":thinking: Need help?",
                    description=f"Join the [Support Discord]({SUPPORT_DISCORD})"))
            except Exception:
                logger.exception("Unable to send support embed")


    # Util

    async def _adjust_voice_channels(
        self, parent, *, create_all_channels=False, adjust_user_limits=False
    ):
        logger.debug("adjusting parent %s", parent)
        guild = parent.guild
        if not guild:
            logger.debug("channel doesn't belong to a guild")
            return

        for cat in self.guild_config[guild.id].managed_voice_categories:
            if cat.category_id == parent.id:
                prefix_map = {prefix.name: prefix for prefix in cat.prefixes}
                break
        else:
            logger.debug("channel is not managed")
            return

        def chan_name_no_sr(chan):
            return re.sub(r" \[.*?\]$", "", chan.name)

        def prefixkey(chan):
            return chan_name_no_sr(chan).rsplit("#", 1)[0].strip()

        def numberkey(chan):
            return int(chan_name_no_sr(chan).rsplit("#", 1)[1])

        async def delete_channel(chan):
            nonlocal made_changes

            id = chan.id
            logger.debug("deleting channel %s", chan)
            async with self.client.events.wait_for_manager(
                "channel_delete", lambda chan: chan.id == id
            ):
                await chan.delete()
            made_changes = True

        async def add_a_channel():
            nonlocal made_changes, chans, cat, guild

            name = f"{prefix} #{len(chans)+1}"
            logger.debug("creating a new channel %s", name)

            limit = prefix_map[prefix].limit

            async with self.client.events.wait_for_manager(
                "channel_create", lambda chan: chan.name == name
            ):
                await guild.channels.create(
                    type_=ChannelType.VOICE, name=name, parent=parent, user_limit=limit
                )

            made_changes = True

        voice_channels = [
            chan for chan in parent.children if chan.type == ChannelType.VOICE
        ]

        sorted_channels = sorted(
            filter(lambda chan: "#" in chan.name, parent.children),
            key=attrgetter("name"),
        )

        grouped = list(
            (prefix, list(sorted(group, key=numberkey)))
            for prefix, group in groupby(sorted_channels, key=prefixkey)
        )

        made_changes = False

        found_prefixes = frozenset(prefix for prefix, _ in grouped)

        for wanted_prefix in prefix_map.keys():
            if wanted_prefix not in found_prefixes:
                grouped.append((wanted_prefix, []))

        for prefix, chans in grouped:
            logger.debug("working on prefix %s, chans %s", prefix, chans)
            if prefix not in prefix_map.keys():
                logger.debug("%s is not in prefixes", prefix)
                if cat.remove_unknown:
                    for chan in chans:
                        # deleting a used channel is not cool
                        if not chan.voice_members:
                            await delete_channel(chan)
                continue
            logger.debug("voicemembers %s", [chan.voice_members for chan in chans])
            empty_channels = [chan for chan in chans if not chan.voice_members]
            logger.debug("empty channels %s", empty_channels)

            if create_all_channels:
                while len(chans) < cat.channel_limit:
                    await add_a_channel()
                    chans.append("dummy")  # value doesn't matter

            elif not empty_channels:
                if len(chans) < cat.channel_limit:
                    await add_a_channel()

            elif len(empty_channels) == 1:
                # how we want it
                continue

            else:
                # more than one empty channel, delete the ones with the highest numbers
                for chan in empty_channels[1:]:
                    await delete_channel(chan)

        if True or made_changes:
            managed_channels = []
            unmanaged_channels = []

            # parent.children should be updated by now to contain newly created channels and without deleted ones

            for chan in (
                chan for chan in parent.children if chan.type == ChannelType.VOICE
            ):
                if "#" in chan.name and prefixkey(chan) in prefix_map.keys():
                    managed_channels.append(chan)
                else:
                    unmanaged_channels.append(chan)

            managed_group = {}
            for prefix, group in groupby(
                sorted(managed_channels, key=prefixkey), key=prefixkey
            ):
                managed_group[prefix] = sorted(list(group), key=numberkey)

            final_list = []

            def channel_suffix(session, chan):
                min, max = self.database.get_min_max_sr(
                    session, [member.id for member in chan.voice_members]
                )
                if min and max:
                    if min == max:
                        return f" [~{min}]"
                    else:
                        return f" [{min}–{max}]"
                else:
                    return ""

            for prefix, prefix_info in prefix_map.items():
                chans = managed_group[prefix]
                # rename channels if necessary
                with self.database.session() as session:
                    for i, chan in enumerate(chans):
                        if cat.show_sr_in_nicks:
                            new_name = f"{prefix} #{i+1}{channel_suffix(session, chan)}"
                        else:
                            new_name = f"{prefix} #{i+1}"

                        if new_name != chan.name:
                            await chan.edit(name=new_name)
                        if adjust_user_limits:
                            limit = prefix_info.limit
                            await chan.edit(user_limit=limit)

                final_list.extend(chans)

            start_pos = (
                max(chan.position for chan in unmanaged_channels) + 1
                if unmanaged_channels
                else 1
            )

            for i, chan in enumerate(final_list):
                pos = start_pos + i
                if chan.position != pos:
                    await chan.edit(position=pos)

    def _format_nick(self, user):
        primary = user.battle_tags[0]

        rankno = primary.rank
        rank = RANKS[rankno] if rankno is not None else "Unranked"
        if primary.sr:
            sr = primary.sr
        else:
            sr = "noSR"
            # normally, we only save different values for SR, so if there is
            # a non null value, it should be the second or third, but just
            # to be sure, check the first 10...
            for old_sr in primary.sr_history[:10]:
                if old_sr.value:
                    sr = f"{old_sr.value}?"
                    rank = f"{RANKS[old_sr.rank]}?"
                    break

        try:
            secondary_sr = user.battle_tags[1].sr
        except IndexError:
            # no secondary accounts
            secondary_sr = None
        else:
            # secondary accounts, mark SR
            sr = f"{sr}*"
            rank = f"{rank}*"

        if secondary_sr is None:
            secondary_sr = "noSR"
            secondary_rank = "Unranked"
        else:
            secondary_rank = RANKS[user.battle_tags[1].rank]

        srs = list(sorted(t.sr or -1 for t in user.battle_tags))

        while srs and srs[0] == -1:
            srs.pop(0)

        if srs:
            lowest_sr, highest_sr = srs[0], srs[-1]
            # FIXME: SR().rank is hacky
            lowest_rank, highest_rank = (
                sr and RANKS[SR(value=sr).rank] for sr in (srs[0], srs[-1])
            )
        else:
            lowest_sr = highest_sr = "noSR"
            lowest_rank = highest_rank = "Unranked"

        t = Template(user.format)
        try:
            return t.substitute(
                sr=sr,
                rank=rank,
                lowest_sr=lowest_sr,
                highest_sr=highest_sr,
                sr_range=f"{lowest_sr}–{highest_sr}",
                rank_range=f"{lowest_rank}–{highest_rank}",
                secondary_sr=secondary_sr,
                secondary_rank=secondary_rank,
                dps=self.SYMBOL_DPS,
                tank=self.SYMBOL_TANK,
                support=self.SYMBOL_SUPPORT,
                flex=self.SYMBOL_FLEX,
            )
        except KeyError as e:
            raise InvalidFormat(e.args[0]) from e

    async def _update_nick(self, user, *, force=False, raise_hierachy_error=False):
        user_id = user.discord_id
        exception = new_nn = None

        for guild in self.client.guilds.values():
            try:
                member = guild.members[user_id]
            except KeyError:
                continue
            try:
                formatted = self._format_nick(user)
                new_nn = await self._update_nick_for_member(
                    member,
                    formatted,
                    user,
                    force=force,
                    raise_hierachy_error=raise_hierachy_error,
                )
            except Exception as e:
                exception = e
                continue

        if exception:
            raise exception

        return new_nn

    async def _update_nick_for_member(
        self,
        member,
        formatted: str,
        user=None,
        *,
        force=False,
        raise_hierachy_error=False,
    ):
        nn = str(member.name)

        if force or self._show_sr_in_nick(member, user):
            if re.search(r"\[.*?\]", str(nn)):
                new_nn = re.sub(r"\[.*?\]", f"[{formatted}]", nn)
            else:
                new_nn = f"{nn} [{formatted}]"
        else:
            if re.search(r"\[.*?\]", str(nn)):
                new_nn = re.sub(r"\[.*?\]", "", nn)
            else:
                new_nn = nn

        if len(new_nn) > 32:
            raise NicknameTooLong(new_nn)

        logger.debug("New nick for %s is %s", nn, new_nn)

        if nn != new_nn:
            try:
                await member.nickname.set(new_nn)
            except HierarchyError:
                logger.info(
                    "Cannot update nick %s to %s due to not enough permissions",
                    nn,
                    new_nn,
                )
                if raise_hierachy_error:
                    raise
            except Exception as e:
                logger.exception("error while setting nick", exc_info=True)
                raise

        return new_nn

    def _show_sr_in_nick(self, member, user):
        if self.guild_config[member.guild_id].show_sr_in_nicks_by_default:
            return True

        if not user:
            with self.database.session() as session:
                user = self.database.user_by_discord_id(session, member.id)

        if user.always_show_sr:
            return True

        if member.voice:
            logger.debug("user %s is currently in voice", member)
            gi = self.guild_config[member.guild.id]
            logger.debug(
                "user is in %s with parent %s",
                member.voice.channel,
                member.voice.channel.parent,
            )
            for vc in gi.managed_voice_categories:
                if vc.category_id == member.voice.channel.parent.id:
                    logger.debug("that parent is managed")
                    return vc.show_sr_in_nicks

        return False

    async def _send_congrats(self, user, rank, image):
        for guild in self.client.guilds.values():
            try:
                if user.discord_id not in guild.members:
                    continue
                embed = Embed(
                    title=f"For your own safety, get behind the barrier!",
                    description=f"**{str(guild.members[user.discord_id].name)}** just advanced to **{RANKS[rank]}**. Congratulations!",
                    colour=COLORS[rank],
                )

                embed.set_thumbnail(url=image)

                await self.client.find_channel(
                    self.guild_config[guild.id].congrats_channel_id
                ).messages.send(
                    content=f"Let's hear it for <@!{user.discord_id}>!", embed=embed
                )
            except Exception:
                logger.exception(f"Cannot send congrats for guild {guild}")

    async def _top_players(self, session, prev_date, style="psql"):
        def prev_sr(tag):
            for sr in tag.sr_history[:30]:
                prev_sr = sr
                if sr.timestamp < prev_date:
                    break
            return prev_sr

        tags = (
            session.query(BattleTag)
            .options(joinedload(BattleTag.user))
            .join(BattleTag.current_sr)
            .order_by(desc(SR.value))
            .filter(SR.value != None)
            .all()
        )

        tags_and_prev = [(tag, prev_sr(tag)) for tag in tags]

        top_per_guild = {}

        guilds = self.client.guilds.values()

        users_seen = set()

        # Not the best runtime performance, we'll worry about that when we have
        # hundreds of guilds with hundreds of members
        for tag, prev_sr in tags_and_prev:
            if tag.user.id in users_seen:
                continue
            else:
                users_seen.add(tag.user.id)
            found = False

            for guild in guilds:
                try:
                    member = guild.members[tag.user.discord_id]
                except KeyError:
                    continue

                top_per_guild.setdefault(guild.id, []).append(
                    (member, tag, prev_sr.value)
                )
                found = True

            @dataclass
            class dummy:
                name: str

            if not found:
                # top_per_guild.setdefault(tag.user.discord_id%1, []).append((dummy(name=f"X{tag.user.discord_id}"), tag, prev_sr.value))
                logger.warning(
                    "User %i not found in any of the guilds", tag.user.discord_id
                )

        def member_name(member):
            name = str(member.name)
            name = re.sub(r"\[.*?\]", "", name)
            name = re.sub(r"\{.*?\}", "", name)
            name = re.sub(r"\s{2,}", " ", name)

            return "".join(
                ch if ord(ch) < 256 or unicodedata.category(ch)[0] != "S" else ""
                for ch in name
            )

        for guild_id, tops in top_per_guild.items():

            # FIXME: wrong if there is a tie
            prev_top_tags = [
                top[1] for top in sorted(tops, key=lambda x: x[2] or 0, reverse=True)
            ]

            def prev_str(pos, tag, prev_sr):
                if not prev_sr:
                    return "  (——)"

                old_pos = prev_top_tags.index(tag) + 1
                if pos == old_pos:
                    sym = " "
                elif pos > old_pos:
                    sym = "↓"
                else:
                    sym = "↑"

                return f"{sym} ({old_pos:2})"

            def delta_fmt(curr, prev):
                if not curr or not prev or curr == prev:
                    return ""
                else:
                    return f"{curr-prev:+4}"

            table_prev_sr = None
            data = []
            for ix, (member, tag, prev_sr) in enumerate(tops):
                if tag.sr != table_prev_sr:
                    pos = ix + 1
                table_prev_sr = tag.sr
                data.append(
                    (
                        pos,
                        prev_str(ix + 1, tag, prev_sr),
                        member_name(member),
                        tag.sr,
                        delta_fmt(tag.sr, prev_sr),
                    )
                )

            headers = ["#", "prev", "Member", "SR", "ΔSR"]
            csv_file = StringIO()
            csv_writer = csv.writer(csv_file)
            csv_writer.writerow(headers)
            csv_writer.writerows(data)

            csv_file = BytesIO(csv_file.getvalue().encode("utf-8"))
            csv_file.seek(0)

            tabulate.PRESERVE_WHITESPACE = True
            table_lines = tabulate.tabulate(
                data, headers=headers, tablefmt=style
            ).split("\n")

            table_lines = [f"`{line}`" for line in table_lines]

            # Split table into submessages, because a short "line" is visible after each message
            # we want it to be in "nice" multiples

            ix = 0
            lines = 20

            table_lines.insert(
                0,
                "Hello! Here are the current SR highscores. If a member has more than one "
                "BattleTag, only the tag with the highest SR is considered. Players with  "
                "private profiles, or those that didn't do their placements this season yet "
                "are not shown.\n",
            )
            try:
                chan = self.client.find_channel(
                    self.guild_config[guild_id].listen_channel_id
                )
                send = chan.messages.send
                # send = self.client.application_info.owner.send
                while ix < len(table_lines):
                    # prefer splits at every "step" entry, but if it turns out too long, send a shorter message
                    step = lines if ix else lines + 3
                    await send_long(send, "\n".join(table_lines[ix : ix + step]))
                    ix += step

                await chan.messages.upload(
                    csv_file,
                    filename=f"ranking {pendulum.now().to_iso8601_string()[:10]}.csv",
                )
            except Exception:
                logger.exception("unable to send top players to guild %i", guild_id)

    async def _message_new_guilds(self):
        for guild_id, guild in self.client.guilds.items():
            if guild_id not in self.guild_config:
                await self._handle_new_guild(guild)

    async def _sync_tag(self, session, tag):
        try:
            sr, image = await get_sr(tag.tag)
        except UnableToFindSR:
            logger.debug(f"No SR for {tag.tag}, oh well...")
            sr = rank = image = None
        except Exception:
            tag.error_count += 1
            # we need to update the last_update pseudo-column
            tag.update_sr(tag.sr)
            if self.raven_client:
                self.raven_client.captureException()
            logger.exception(f"Got exception while requesting {tag.tag}")
            raise
        tag.error_count = 0
        tag.update_sr(sr)
        await self._handle_new_sr(session, tag, sr, image)

    async def _handle_new_sr(self, session, tag, sr, image):
        try:
            await self._update_nick(tag.user)
        except HierarchyError:
            # not much we can do, just ignore
            pass
        except NicknameTooLong as e:
            if tag.user.last_problematic_nickname_warning is None or tag.user.last_problematic_nickname_warning < datetime.utcnow() - timedelta(
                days=7
            ):
                tag.user.last_problematic_nickname_warning = datetime.utcnow()
                msg = "*To avoid spamming you, I will only send out this warning once per week*\n"
                msg += f"Hi! I just tried to update your nickname, but the result '{e.nickname}' would be longer than 32 characters."
                if tag.user.format == "%s":
                    msg += "\nPlease shorten your nickname."
                else:
                    msg += "\nTry to use the %s format (you can type `!ow format %s` into this DM channel, or shorten your nickname."
                msg += "\nYour nickname cannot be updated until this is done. I'm sorry for the inconvenience."
                discord_user = await self.client.get_user(tag.user.discord_id)
                await discord_user.send(msg)

            # we can still do the rest, no need to return here
        rank = tag.rank
        if rank is not None:
            user = tag.user

            # get highest SR, but exclude current_sr
            session.flush()
            prev_highest_sr_value = session.query(func.max(SR.value)).filter(
                SR.battle_tag == tag, SR.id != tag.current_sr_id
            )
            prev_highest_sr = (
                session.query(SR)
                .filter(SR.value == prev_highest_sr_value)
                .order_by(desc(SR.timestamp))
                .first()
            )

            logger.debug(f"prev_sr {prev_highest_sr} {tag.current_sr.value}")
            if prev_highest_sr and rank > prev_highest_sr.rank:
                logger.debug(
                    f"user {user} old rank {prev_highest_sr.rank}, new rank {rank}, sending congrats..."
                )
                await self._send_congrats(user, rank, image)
                user.highest_rank = rank

    async def _sync_tags_from_channel(self, channel):
        first = True
        async with channel:
            async for tag_id in channel:
                logger.debug("got %s from channel %r", tag_id, channel)
                if not first:
                    delay = random.random() * 5.0
                    logger.debug(f"rate limiting: sleeping for {delay:.02}s")
                    await trio.sleep(delay)
                else:
                    first = False
                session = self.database.Session()
                try:
                    tag = self.database.tag_by_id(session, tag_id)
                    if tag:
                        await self._sync_tag(session, tag)
                    else:
                        logger.warn(f"No tag for id {tag_id} found, probably deleted")
                    session.commit()
                except Exception:
                    logger.exception(
                        f"exception while syncing {tag.tag} for {tag.user.discord_id}"
                    )
                finally:
                    session.commit()
                    session.close()
        logger.debug("channel %r closed, done", channel)

    async def _sync_check(self):
        session = self.database.Session()
        try:
            ids_to_sync = self.database.get_tags_to_be_synced(session)
        finally:
            session.close()
        if ids_to_sync:
            logger.info(f"{len(ids_to_sync)} tags need to be synced")
            await self._sync_tags(ids_to_sync)
        else:
            logger.debug("No tags need to be synced")

    async def _sync_tags(self, ids_to_sync):
        send_ch, receive_ch = trio.open_memory_channel(len(ids_to_sync))
        logger.debug("preparing to sync ids: %s into channel %r", ids_to_sync, send_ch)

        async with send_ch:
            for tag_id in ids_to_sync:
                await send_ch.send(tag_id)

        async with trio.open_nursery() as nursery:
            async with receive_ch:
                for _ in range(min(len(ids_to_sync), 5)):
                    nursery.start_soon(self._sync_tags_from_channel, receive_ch.clone())
        logger.info("done syncing")

    async def _sync_all_tags_task(self):
        await trio.sleep(10)
        logger.debug("started waiting...")
        while True:
            try:
                await self._sync_check()
            except Exception as e:
                logger.exception(f"something went wrong during _sync_check")
            await trio.sleep(60)

    async def _cron_task(self):
        "poor man's cron, hardcode all the things"

        while True:
            try:
                logger.debug("checking cron...")
                with self.database.session() as s:
                    try:
                        hs = s.query(Cron).filter_by(id="highscore").one()
                    except NoResultFound:
                        hs = Cron(id="highscore", last_run=datetime.utcnow())
                        s.add(hs)
                    next_run = datetime.today().replace(
                        hour=9, minute=0, second=0, microsecond=0
                    )
                    logger.debug(
                        "next_run %s, now %s, last_run %s",
                        next_run,
                        datetime.utcnow(),
                        hs.last_run,
                    )
                    if next_run < datetime.utcnow() and hs.last_run < next_run:
                        logger.debug("running highscore...")
                        await self._cron_run_highscore()
                        logger.debug("done running hiscore")
                        hs.last_run = datetime.utcnow()
                    s.commit()
            except Exception:
                logger.exception("Error during cron")
            await trio.sleep(1 * 60)

    async def _cron_run_highscore(self):
        prev_date = datetime.utcnow() - timedelta(days=1)

        with self.database.session() as session:
            await self._top_players(session, prev_date)

    async def _web_server(self):
        config = hypercorn.config.Config()
        config.access_logger = config.error_logger = logger

        web.send_ch = self.web_send_ch
        web.client = self.client
        web.orisa = self

        logger.info("Starting web server")
        await hypercorn.trio.serve(web.app, config)

    async def _oauth_result_listener(self):
        async for uid, data in self.web_recv_ch:
            logger.debug(f"got OAuth response data {data} for uid {uid}")
            try:
                await self._handle_registration(uid, data.get("battletag"), data["id"])
            except Exception:
                logger.error(
                    "Something went wrong when working with data %s", exc_info=True
                )

    async def _handle_registration(self, user_id, battle_tag, blizzard_id):
        session = self.database.Session()
        try:
            user_obj = await self.client.get_user(user_id)
            user_channel = await user_obj.open_private_channel()

            if battle_tag is None:
                await user_channel.messages.send(
                    "I'm sorry, it seems like you don't have a BattleTag. Orisa currently only works for PC accounts."
                )
                return

            user = self.database.user_by_discord_id(session, user_id)
            resp = None
            new_tag = BattleTag(tag=battle_tag, blizzard_id=blizzard_id)

            if user is None:
                user = User(discord_id=user_id, battle_tags=[new_tag], format="$sr")
                session.add(user)

                extra_text = ""
                for guild in self.client.guilds.values():
                    if user_id in guild.members:
                        extra_text = (
                            self.guild_config[guild.id].extra_register_text or ""
                        )
                        break
                embed = Embed(
                    color=0x6DB76D,
                    title="Registration successful",
                    description=f"OK. People can now ask me for your BattleTag **{battle_tag}**, and I will keep track of your SR.",
                )
                embed.add_field(
                    name=":information_source: Pro Tips",
                    value="On some servers, I will only update your nick if you join a OW voice channel. If you want your nick to always show your SR, "
                    "use the `!ow alwaysshowsr` command. If you want me to show your rank instead of your SR, use `!ow format $rank`.\n"
                    "If you have more than one account, simply issue `!ow register` again.\n",
                )
                if extra_text:
                    embed.add_field(
                        name=f":envelope: A message from the *{guild.name}* staff",
                        value=extra_text,
                    )
            else:
                existing_tag = None
                for tag in user.battle_tags:
                    if tag.blizzard_id == blizzard_id:
                        existing_tag = tag
                        break
                if existing_tag and existing_tag.tag != battle_tag:
                    embed = Embed(
                        color=0x6DB76D,
                        title="BattleTag updated",
                        description=f"It seems like your BattleTag changed from *{existing_tag.tag}* to *{battle_tag}*. I have updated my database.",
                    )
                    existing_tag.tag = battle_tag
                elif any(tag.tag == battle_tag for tag in user.battle_tags):
                    embed = Embed(
                        title="BattleTag already registered",
                        color=0x6F0808,
                        description=f"You already registered the BattleTag *{battle_tag}*, so there's nothing for me to do. *Sleep mode reactivated.*\n",
                    )
                    embed.add_field(
                        name=":information_source: Tip",
                        value="Open the URL in a private/incognito tab next time, so you can enter the credentials of the account you want.",
                    )
                    await user_obj.send(content=None, embed=embed)
                    return
                else:
                    user.battle_tags.append(new_tag)
                    embed = Embed(
                        color=0x6DB76D,
                        title="BattleTag added",
                        description=f"OK. I've added **{battle_tag}** to the list of your BattleTags. **Your primary BattleTag remains {user.battle_tags[0].tag}**. "
                        f"To change your primary tag, use `!ow setprimary yourbattletag`, see help for more details.",
                    )

            try:
                async with user_channel.typing:
                    sr, image = await get_sr(battle_tag)
            except InvalidBattleTag as e:
                await user_channel.messages.send(
                    f"Invalid BattleTag: {e.message}??? I got yours directly from Blizzard, but they claim it doesn't exist... Try again later, Blizzard have fucked up."
                )
                return
            except BlizzardError as e:
                await user_channel.messages.send(
                    f"Sorry, but it seems like Blizzard's site has some problems currently ({e}), please try again later"
                )
                raise
            except UnableToFindSR:
                embed.add_field(
                    name=":warning: No SR",
                    value="You don't have an SR though, your profile needs to be public for SR tracking to work... I still saved your BattleTag.",
                )
                sr = None

            new_tag.update_sr(sr)
            rank = new_tag.rank

            sort_secondaries(user)

            session.commit()

            try:
                await self._update_nick(user, force=True, raise_hierachy_error=True)
            except NicknameTooLong as e:
                embed.add_field(
                    name=":warning: Nickname too long!",
                    value=f"Adding your SR to your nickname would result in '{e.nickname}' and with {len(e.nickname)} characters, be longer than Discord's maximum of 32."
                    f"Please shorten your nick to be no longer than 28 characters. I will regularly try to update it.",
                )
            except HierarchyError as e:
                embed.add_field(
                    name=":warning: Cannot update nickname",
                    value='I do not have enough permissions to update your nickname. The owner needs to move the "Orisa" role higher '
                    "so that is higher than your highest role. If you are the owner of this server, there is no way for me to update your nickname, sorry.",
                )
            except Exception as e:
                logger.exception(f"unable to update nick for user {user}")
                embed.add_field(
                    name=":warning: Cannot update nickname",
                    value="Right now I couldn't update your nickname, will try that again later."
                    "People will still be able to ask for your BattleTag, though.",
                )
            finally:
                with suppress(Exception):
                    await self._update_nick(user)

            embed.add_field(
                name=":thumbsup: Vote for me on Discord Bot List",
                value=f"If you find me helpful, consider voting for me [by clicking here]({VOTE_LINK})",
            )

            await user_channel.messages.send(content=None, embed=embed)

        finally:
            session.close()


def fuzzy_nick_match(ann, ctx: Context, name: str):
    def strip_tags(name):
        return re.sub(r"^(.*?\|)?([^[{]*)((\[|\{).*)?", r"\2", str(name)).strip()

    member = member_id = None
    if ctx.guild:
        guilds = [ctx.guild]
    else:
        guilds = [
            guild for guild in ctx.bot.guilds.values() if ctx.author.id in guild.members
        ]

    if name.startswith("<@") and name.endswith(">"):
        id = name[2:-1]
        if id[0] == "!":  # strip nicknames
            id = id[1:]
        try:
            member_id = int(id)
        except ValueError:
            raise ConversionFailedError(ctx, name, Member, "Invalid member ID")
    else:

        def scorer(s1, s2, force_ascii=True, full_process=True):
            if s1.lower() == s2.lower():
                return 200
            else:
                score = fuzz.WRatio(s1, s2, force_ascii, full_process)
                if s2.startswith(s1):
                    score *= 2
                return score

        candidates = process.extractBests(
            name,
            {
                id: strip_tags(mem.name)
                for guild in guilds
                for id, mem in guild.members.items()
            },
            scorer=scorer,
        )
        logger.debug(f"candidates are {candidates}")
        if candidates:
            member_name, score, member_id = candidates[0]

    if member_id is not None:
        for guild in guilds:
            member = guild.members.get(member_id)
            if member:
                break

    if member is None:
        raise ConversionFailedError(
            ctx, name, Member, "Cannot find member with that name"
        )
    else:
        return member


Context.add_converter(Member, fuzzy_nick_match)

multio.init("trio")

GLaDOS: ContextVar[bool] = ContextVar("GLaDOS", default=False)


class OrisaClient(Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__GLaDOS_http = HTTPClient(GLADOS_TOKEN, bot=True)

    @contextmanager
    def as_glados(self):
        token = GLaDOS.set(True)
        try:
            yield
        finally:
            GLaDOS.reset(token)

    def _http_get(self):
        return self.__GLaDOS_http if GLaDOS.get() else self.__http

    def _http_set(self, http):
        self.__http = http

    http = property(_http_get, _http_set)
