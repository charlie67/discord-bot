import asyncio
import logging
import sys

import discord
from discord.ext import commands
from discord import FFmpegPCMAudio
import re
import os
import random
import youtube_dl
from voice.voice_helpers import get_video_id, get_youtube_details, search_for_video, get_playlist_id, Video, get_videos_on_playlist
from voice.YTDLSource import YTDLSource

FFMPEG_PATH = '/usr/bin/ffmpeg'

ytdl_format_options = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0'  # bind to ipv4 since ipv6 addresses cause issues sometimes
}

ytdl = youtube_dl.YoutubeDL(ytdl_format_options)


async def get_or_create_audio_source(ctx):
    guild = ctx.guild
    author: discord.Member = ctx.author
    voice_client: discord.VoiceClient = guild.voice_client
    if voice_client is None:
        if author.voice is None:
            await ctx.send("You need to be in a voice channel")
            return None
        else:
            await discord.utils.get(guild.voice_channels, name=author.voice.channel.name).connect()
            voice_client: discord.VoiceClient = guild.voice_client
    return voice_client


def setup(bot):
    bot.add_cog(Voice(bot))


class Voice(commands.Cog):
    video_queue_map = {}
    currently_playing_map = {}

    def __init__(self, bot):
        self.bot = bot
        self.logger = logging.getLogger('discord')
        self.logger.setLevel(logging.DEBUG)
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s'))
        self.logger.addHandler(handler)

    @commands.command(aliases=['summon'])
    async def join(self, ctx):
        await get_or_create_audio_source(ctx)

    @commands.command()
    async def leave(self, ctx):
        guild: discord.Guild = ctx.guild
        voice_client: discord.VoiceClient = guild.voice_client
        if voice_client is not None:
            await voice_client.disconnect()
            del self.currently_playing_map[ctx.guild.id]
            del self.video_queue_map[ctx.guild.id]
        else:
            await ctx.send("... What are you actually expecting me to do??")

    async def audio_player_task(self, server_id):
        video_queue = self.video_queue_map.get(server_id)
        current = video_queue.__getitem__(0)
        video_queue.__delitem__(0)
        video = current[0]
        player = await YTDLSource.from_url(video.video_url, loop=self.bot.loop, stream=True)
        voice_client = current[1]
        ctx = current[2]
        await ctx.send('Now playing: {}'.format(video.video_title))
        await ctx.send(embed=discord.Embed(title=video.video_title, url=video.video_url))
        self.currently_playing_map[ctx.guild.id] = video
        voice_client.play(player, after=lambda e: self.toggle_next(server_id=server_id, ctx=ctx, error=e))

    def toggle_next(self, server_id, ctx, error=None):
        if error is not None:
            asyncio.run_coroutine_threadsafe(ctx.send("Error playing that video"), self.bot.loop)
            self.logger.error("error playing back video" + error)

        if self.currently_playing_map.keys().__contains__(server_id):
            del self.currently_playing_map[server_id]
        self.logger.debug("toggling next for", server_id)

        video_queue = self.video_queue_map.get(server_id)
        if video_queue.__len__() > 0:
            asyncio.run_coroutine_threadsafe(self.audio_player_task(server_id=server_id), self.bot.loop)
        else:
            del self.video_queue_map[server_id]
            asyncio.run_coroutine_threadsafe(ctx.guild.voice_client.disconnect(), self.bot.loop)

    @commands.command()
    async def play(self, ctx, *, search_or_url: str):
        if search_or_url is None:
            await ctx.send("Need to provide something to play")
            return

        voice_client = await get_or_create_audio_source(ctx)
        if voice_client is None:
            return

        video_check_pattern = "^(?:https?:\\/\\/)?(?:www\\.)?(?:youtu\\.be\\/|youtube\\.com\\/(" \
                              "?:embed\\/|v\\/|watch\\?v=|watch\\?.+&v=))((\\w|-){11})?(&?.*)?$"
        valid_video_url = re.search(video_check_pattern, search_or_url)

        playlist_check_pattern = "^https?:\\/\\/(www.youtube.com|youtube.com)\\/playlist(.*)$"
        valid_playlist_url = re.search(pattern=playlist_check_pattern, string=search_or_url)

        if valid_video_url:
            video_id = get_video_id(search_or_url)
            video_title, video_length = get_youtube_details(video_id)
            video_url = search_or_url
        elif valid_playlist_url:
            await ctx.send("Queuing items on playlist")
            playlist_id = get_playlist_id(search_or_url)
            if playlist_id is None:
                return await ctx.send("Can't get videos from the playlist")
            playlist_videos: list = get_videos_on_playlist(url=search_or_url)
            for video in playlist_videos:
                # video: Video = playlist_videos.__getitem__(i)
                pair = (video, voice_client, ctx)
                server_id = ctx.guild.id
                video_queue = self.video_queue_map.get(server_id)
                if video_queue is None:
                    video_queue = list()
                    self.video_queue_map[server_id] = video_queue
                video_queue.append(pair)

            await ctx.send("Queued {} items".format(playlist_videos.__len__().__str__()))
            # await self.queue(ctx=ctx)

            if not voice_client.is_playing():
                self.toggle_next(server_id=ctx.guild.id, ctx=ctx)

            return

        else:
            await ctx.send("Searching for " + search_or_url)
            video_id, video_url, = search_for_video(search_or_url)
            video_title, video_length = get_youtube_details(video_id)

        video = Video(video_url=video_url, video_id=video_id, thumbnail_url=None, video_title=video_title,
                      video_length=video_length)
        pair = (video, voice_client, ctx)
        server_id = ctx.guild.id
        video_queue = self.video_queue_map.get(server_id)
        if video_queue is None:
            video_queue = list()
            self.video_queue_map[server_id] = video_queue
        video_queue.append(pair)

        if not voice_client.is_playing():
            self.toggle_next(server_id=ctx.guild.id, ctx=ctx)
        else:
            await ctx.send('Queuing: {}'.format(video_title))
            await ctx.send(embed=discord.Embed(title=video_title, url=video_url))

    @commands.command()
    async def skip(self, ctx):
        guild = ctx.guild

        voice_client: discord.VoiceClient = guild.voice_client
        if voice_client is not None:
            if voice_client.is_playing() or voice_client.is_paused():
                voice_client.stop()
                self.toggle_next(server_id=guild.id)

                await ctx.send("Skipping")
            else:
                return await ctx.send("Not currently playing")
        else:
            return await ctx.send("You need to be in a voice channel")

    @commands.command()
    async def playfile(self, ctx, file_name: str = None):
        return
        # voice_client = await get_or_create_audio_source(ctx)
        # if voice_client is None:
        #     return
        #
        # if voice_client.is_playing():
        #     await ctx.send("I'm already playing be patient will you")
        #     return
        #
        # if file_name is None:
        #     file_list = os.listdir("/bot/assets/audio")
        #     file_name = random.choice(file_list)
        #
        # if not file_name.endswith(".mp3"):
        #     file_name = file_name + ".mp3"
        #
        # audio_source = FFmpegPCMAudio("/bot/assets/audio/" + file_name,
        #                               executable=FFMPEG_PATH)
        # voice_client.play(audio_source)

    @commands.command(aliases=['stopplaying'])
    async def stop(self, ctx):
        guild = ctx.guild

        voice_client: discord.VoiceClient = guild.voice_client
        if voice_client is not None:
            if voice_client.is_playing() or voice_client.is_paused():
                voice_client.stop()
                del self.currently_playing_map[ctx.server.id]
                await ctx.send("Stopping")
        else:
            await ctx.send("Nothing to stop")

    @commands.command()
    async def pause(self, ctx):
        guild = ctx.guild

        voice_client: discord.VoiceClient = guild.voice_client
        if voice_client is not None and voice_client.is_playing():
            voice_client.pause()
            await ctx.send("Pausing")
        else:
            await ctx.send("Nothing to pause")

    @commands.command()
    async def resume(self, ctx):
        guild = ctx.guild

        voice_client: discord.VoiceClient = guild.voice_client
        if voice_client is not None and voice_client.is_paused():
            voice_client.resume()
            await ctx.send("Resuming")
        else:
            await ctx.send("Nothing to resume")

    @commands.command()
    async def queue(self, ctx):
        server_id = ctx.guild.id
        if not self.video_queue_map.keys().__contains__(server_id):
            return await ctx.send("Queue is empty")
        else:
            video_list = self.video_queue_map[server_id]
            counter = 0
            while counter < video_list.__len__():
                if counter >= 5:
                    await ctx.send("And {} other songs".format(video_list.__len__() - 5))
                    break
                item = video_list.__getitem__(counter)
                video = item[0]
                item_counter = counter + 1
                await ctx.send(item_counter.__str__() + ". " + video.video_title)
                counter += 1

    @commands.command(aliases=['np'])
    async def nowplaying(self, ctx):
        if not self.currently_playing_map.keys().__contains__(ctx.guild.id):
            return await ctx.send("Not playing anything")
        currently_playing = self.currently_playing_map[ctx.guild.id]
        await ctx.send('Now playing: {}'.format(currently_playing.video_title))
        await ctx.send(
            embed=discord.Embed(title=currently_playing.video_title, url=currently_playing.video_url))

    @commands.command()
    async def dishwasher(self, ctx):
        voice_client = await get_or_create_audio_source(ctx)
        if voice_client is None:
            return

        if voice_client.is_playing():
            await ctx.send("I'm already playing be patient will you")
            return

        audio_source = FFmpegPCMAudio("/bot/assets/audio/dishwasher.mp3",
                                      executable=FFMPEG_PATH)
        voice_client.play(audio_source)

