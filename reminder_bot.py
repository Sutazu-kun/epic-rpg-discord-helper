import os
import re
import asyncio
import dotenv
import discord

dotenv.load_dotenv(override=True)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "epic_reminder.settings")

# django.setup() is called as a side-effect
from django.core.wsgi import get_wsgi_application

get_wsgi_application()

from asgiref.sync import sync_to_async

from epic.models import CoolDown, Profile, Server, JoinCode, Gamble, Hunt, GroupActivity
from epic.query import (
    get_instance,
    update_instance,
    upsert_cooldowns,
    bulk_delete,
    get_cooldown_messages,
    get_guild_cooldown_messages,
    set_guild_cd,
    set_guild_membership,
    update_hunt_results,
)
from epic.utils import tokenize

from epic.cmd_chain import handle_rpcd_message
from epic.scrape import log_message


async def process_rpg_messages(client, server, message):
    rpg_cd_rd_cues, cooldown_cue = ["cooldowns", "ready"], "cooldown"
    gambling_cues = set(Gamble.GAME_CUE_MAP.keys())
    # arena is special case since it does not show an icon_url
    group_cues = GroupActivity.ACTIVITY_SET - {"arena"}
    cues = [*rpg_cd_rd_cues, *gambling_cues, *group_cues, cooldown_cue]
    if "found and killed" in message.content:
        hunt_result = Hunt.hunt_result_from_message(message)
        if hunt_result:
            name, *other = hunt_result
            possible_userids = [str(m.id) for m in client.get_all_members() if name == m.name]
            return await update_hunt_results(other, possible_userids)
    profile = None
    for embed in message.embeds:
        # the user mentioned
        profile = await sync_to_async(Profile.from_embed_icon)(client, server, message, embed)
        if profile and any([cue in embed.author.name for cue in cues]):
            # is the cooldowns list
            if any([cue in embed.author.name for cue in rpg_cd_rd_cues]):
                update, delete = CoolDown.from_cd(profile, [field.value for field in embed.fields])
                await upsert_cooldowns(update)
                await bulk_delete(CoolDown, delete)
            elif cooldown_cue in embed.author.name:
                for cue, cooldown_type in CoolDown.COOLDOWN_RESPONSE_CUE_MAP.items():
                    if cue in str(embed.title):
                        cooldowns = CoolDown.from_cooldown_reponse(profile, embed.title, cooldown_type)
                        if cooldowns and cooldown_type == "guild":
                            return await set_guild_cd(profile, cooldowns[0].after)
                        await upsert_cooldowns(cooldowns)
            elif any([cue in embed.author.name for cue in gambling_cues]):
                gamble = Gamble.from_results_screen(profile, embed)
                if gamble:
                    await gamble.asave()
            elif any([cue in embed.author.name for cue in group_cues]):
                group_activity_type = None
                for activity_type in group_cues:
                    if activity_type in embed.author.name:
                        group_activity_type = activity_type
                        break
                else:
                    return
                group_activity = await sync_to_async(GroupActivity.objects.latest_group_activity)(
                    profile.uid, group_activity_type
                )
                if group_activity:
                    confirmed_group_activity = await sync_to_async(group_activity.confirm_activity)(embed)
                    if confirmed_group_activity:
                        await sync_to_async(confirmed_group_activity.save_as_cooldowns)()
        # special case of GroupActivity
        arena_match = GroupActivity.REGEX_MAP["arena"].search(str(embed.description))
        if arena_match:
            name = arena_match.group(1)
            group_activity = await sync_to_async(GroupActivity.objects.latest_group_activity)(name, "arena")
            if group_activity:
                confirmed_group_activity = await sync_to_async(group_activity.confirm_activity)(embed)
                if confirmed_group_activity:
                    await sync_to_async(confirmed_group_activity.save_as_cooldowns)()

    if profile and (profile.server_id != server.id or profile.channel != message.channel.id):
        profile = await update_instance(profile, server_id=server.id, channel=message.channel.id)
    return


class Client(discord.Client):
    async def on_ready(self):
        print("Logged on as {0}!".format(self.user))

    async def on_message(self, message):
        if message.author == self.user:
            return

        server = await get_instance(Server, id=message.channel.guild.id)
        if server and not server.active:
            return

        content = message.content[:150].lower()

        if content.startswith("rpgcd") or content.startswith("rcd") or content.startswith("rrd"):
            if content.startswith("rcd"):
                tokens = tokenize(message.content[3:])
                # if tokens and tokens[0] == "scrape":
                #     limit = None
                #     if len(tokens) == 2 and tokens[1].isdigit():
                #         limit = int(tokens[1])
                #     async for m in message.channel.history(limit=limit):
                #         logger = await log_message(m)
                #     return await logger.shutdown()
            elif content.startswith("rrd"):
                tokens = ["rd", *tokenize(message.content[3:])]
            else:
                tokens = tokenize(message.content[5:])
            msg = await handle_rpcd_message(self, tokens, message, server, None, None)
            embed = msg.to_embed()
            await message.channel.send(embed=embed)

        if not server:
            return

        # we want to pull the results of Epic RPG's cooldown message
        if str(message.author) == "EPIC RPG#4117":
            return await process_rpg_messages(self, server, message)

        if content.startswith("rpg"):
            cooldown_type, after = CoolDown.cd_from_command(message.content[3:])
            if not cooldown_type:
                return
            profile, _ = await get_instance(
                Profile,
                uid=message.author.id,
                defaults={
                    "last_known_nickname": message.author.name,
                    "server": server,
                    "channel": message.channel.id,
                },
            )
            if profile.server_id != server.id or profile.channel != message.channel.id:
                profile = await update_instance(profile, server_id=server.id, channel=message.channel.id)
            if cooldown_type == "guild":
                return await set_guild_cd(profile)
            elif cooldown_type in {"hunt", "adventure"}:
                _, _ = await get_instance(Hunt, profile_id=profile.uid, target=None, defaults={"target": None})
            elif cooldown_type in GroupActivity.ACTIVITY_SET:
                # need to know the difference between dungeon and miniboss here
                cooldown_type = "miniboss" if tokenize(message.content[3:])[0] == "miniboss" else cooldown_type
                return await sync_to_async(GroupActivity.create_from_tokens)(
                    cooldown_type, self, profile, server, message
                )
            await upsert_cooldowns([CoolDown(profile=profile, type=cooldown_type, after=after)])

    async def on_message_edit(self, before, after):
        guild_name_regex = re.compile(r"\*\*(?P<guild_name>[^\*]+)\*\* members")
        player_name_regex = re.compile(r"\*\*(?P<player_name>[^\*]+)\*\*")
        guild_membership = {}
        guild_id_map = {}
        for embed in after.embeds:
            for field in embed.fields:
                name_match = guild_name_regex.match(field.name)
                if name_match:
                    guild_membership[name_match.group(1)] = player_name_regex.findall(field.value)
                break
            break
        for guild, membership_set in guild_membership.items():
            guild_id_map[guild] = []
            for member in membership_set:
                # careful in case name contains multiple #
                split_name = member.split("#")
                name, discriminator = "#".join(split_name[:-1]), split_name[-1]
                user = discord.utils.get(self.get_all_members(), name=name, discriminator=discriminator)
                if user:
                    guild_id_map[guild].append(user.id)
        await set_guild_membership(guild_id_map)


if __name__ == "__main__":
    from django.conf import settings

    intents = discord.Intents.default()
    intents.members = True

    bot = Client(intents=intents)

    async def notify():
        await bot.wait_until_ready()
        while not bot.is_closed():
            await sync_to_async(GroupActivity.objects.delete_stale)()
            cooldown_messages = [
                *await get_cooldown_messages(),
                *await get_guild_cooldown_messages(),
            ]
            for message, channel in cooldown_messages:
                _channel = await bot.fetch_channel(channel)
                await _channel.send(message)
            await asyncio.sleep(5)  # task runs every 5 seconds

    bot.loop.create_task(notify())
    bot.run(settings.DISCORD_TOKEN)
