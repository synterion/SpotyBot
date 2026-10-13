import asyncio
import logging
import random
import spotipy
import asqlite
import twitchio
import os 

from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyOAuth
from typing import TYPE_CHECKING
from collections import defaultdict
from twitchio import eventsub
from twitchio.ext import commands

load_dotenv()

if TYPE_CHECKING:
    import sqlite3

sp = spotipy.Spotify(
    auth_manager=SpotifyOAuth(
        client_id=os.getenv("SPOTIFY_CLIENT_ID"),
        client_secret=os.getenv("SPOTIFY_CLIENT_SECRET"),
        redirect_uri="http://127.0.0.1:8888/callback",
        scope="user-modify-playback-state user-read-playback-state",
    )
)


LOGGER: logging.Logger = logging.getLogger("Bot")

# Consider using a .env or another form of Configuration file!
CLIENT_ID: str = os.getenv("TWITCH_CLIENT_ID")  # The CLIENT ID from the Twitch Dev Console
CLIENT_SECRET: str = os.getenv("TWITCH_CLIENT_SECRET")  # The CLIENT SECRET from the Twitch Dev Console
BOT_ID = os.getenv("BOT_ID")  # The Account ID of the bot user...
OWNER_ID = os.getenv("OWNER_ID")  # Your personal User ID..


class Bot(commands.AutoBot):
    def __init__(self, *, token_database: asqlite.Pool, subs: list[eventsub.SubscriptionPayload]) -> None:
        self.token_database = token_database

        super().__init__(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            bot_id=BOT_ID,
            owner_id=OWNER_ID,
            prefix="!",
            subscriptions=subs,
            force_subscribe=True,
        )

    async def setup_hook(self) -> None:
        # Add our component which contains our commands...
        await self.add_component(MyComponent(self))

    async def event_oauth_authorized(self, payload: twitchio.authentication.UserTokenPayload) -> None:
        await self.add_token(payload.access_token, payload.refresh_token)

        if not payload.user_id:
            return

        if payload.user_id == self.bot_id:
            # We usually don't want subscribe to events on the bots channel...
            return

        # A list of subscriptions we would like to make to the newly authorized channel...
        subs: list[eventsub.SubscriptionPayload] = [
            eventsub.ChatMessageSubscription(broadcaster_user_id=payload.user_id, user_id=self.bot_id),
        ]

        resp: twitchio.MultiSubscribePayload = await self.multi_subscribe(subs)
        if resp.errors:
            LOGGER.warning("Failed to subscribe to: %r, for user: %s", resp.errors, payload.user_id)

    async def add_token(self, token: str, refresh: str) -> twitchio.authentication.ValidateTokenPayload:
        # Make sure to call super() as it will add the tokens interally and return us some data...
        resp: twitchio.authentication.ValidateTokenPayload = await super().add_token(token, refresh)

        # Store our tokens in a simple SQLite Database when they are authorized...
        query = """
        INSERT INTO tokens (user_id, token, refresh)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET
            token = excluded.token,
            refresh = excluded.refresh;
        """

        async with self.token_database.acquire() as connection:
            await connection.execute(query, (resp.user_id, token, refresh))

        LOGGER.info("Added token to the database for user: %s", resp.user_id)
        return resp

    async def event_ready(self) -> None:
        LOGGER.info("Successfully logged in as: %s", self.bot_id)


class MyComponent(commands.Component):

    def __init__(self, bot: Bot) -> None:
        self.bot = bot 
        self.music_lock = asyncio.Lock()

    @staticmethod
    def _track_key(track: dict) -> tuple[str,str]:
        artist = track["artists"][0]["name"] if track.get("artists") else ""
        return (track["name"].lower(), artist.lower)

    async def _already_queued(self, track: dict) -> bool:
        data = await asyncio.to_thread(sp.queue) or {}

        known = list(data.get("queue", []))
        if data.get("currently_playing"):
            known.append(data["currently_playing"])

        uris = {t["uri"] for t in known}
        keys = {self._track_key(t) for t in known}

        return track["uri"] in uris  or self._track_key(track) in keys


    @commands.command()
    async def music(self, ctx: commands.Context, *, song: str) -> None:
        """!music song name"""
        try:
            result = await asyncio.to_thread(sp.search, q=song, type="track", limit=1)
            items = result["tracks"]["items"]
            if not items:
                await ctx.reply(f"Aucun resultat pout {song}.")
                return

            track = items[0]
            artists = ", ".join(a["name"] for a in track["artists"])
            label = f"{artists} - {track['name']}"

            async with self.music_lock:

                if await self._already_queued(track):
                    await ctx.reply(f"« {label} » est déjà dans la file ou en cours de lecture.")
                    return
                await asyncio.to_thread(sp.add_to_queue, track["uri"])

        
        except spotipy.SpotifyException as e:
            if e.http_status == 404:
                await ctx.reply("Aucune lecture Spotify active : ouvre Spotify et lance un morceau.")
            elif e.http_status == 403:
                await ctx.reply("Spotify refuse l'action (compte Premium requis).")
            else:
                await ctx.reply("Erreur Spotify, réessaie dans un instant.")
            return


        await ctx.reply(f"« {artists} - {track['name']} » ajoutée à la file !")

        channel_id = ctx.broadcaster.id
        viewer = ctx.chatter.name

        print(f"[{ctx.broadcaster.name}] {viewer} demande : {song}")
        

    @commands.command()
    async def queue(self, ctx: commands.Context) -> None:
        """!queue : affiche les prochaines demandes"""
        try: 
            data = await asyncio.to_thread(sp.queue) or {}
        except spotipy.SpotifyException:
            await ctx.reply("Impossible de lire la file ")
            return

        upcoming = data.get("queue", [])[:10]
        if not upcoming:
            await ctx.reply("La file d'attente est vide")
            return
        
        parts = []
        for i, t in enumerate(upcoming,1):
            artist = t["artists"][0]["name"] if t.get("artists") else "?"
            parts.append(f"{i}. {artist} - {t['name']}")

        await ctx.reply("Prochains morceaux : " + " | ".join(parts))


async def setup_database(db: asqlite.Pool) -> tuple[list[tuple[str, str]], list[eventsub.SubscriptionPayload]]:
    # Create our token table, if it doesn't exist..
    # You should add the created files to .gitignore or potentially store them somewhere safer
    # This is just for example purposes...

    query = """CREATE TABLE IF NOT EXISTS tokens(user_id TEXT PRIMARY KEY, token TEXT NOT NULL, refresh TEXT NOT NULL)"""
    async with db.acquire() as connection:
        await connection.execute(query)

        # Fetch any existing tokens...
        rows: list[sqlite3.Row] = await connection.fetchall("""SELECT * from tokens""")

        tokens: list[tuple[str, str]] = []
        subs: list[eventsub.SubscriptionPayload] = []

        for row in rows:
            tokens.append((row["token"], row["refresh"]))

            if row["user_id"] == BOT_ID:
                continue

            subs.extend([eventsub.ChatMessageSubscription(broadcaster_user_id=row["user_id"], user_id=BOT_ID)])

    return tokens, subs


# Our main entry point for our Bot
# Best to setup_logging here, before anything starts
def main() -> None:
    twitchio.utils.setup_logging(level=logging.INFO)

    sp.current_user()

    async def runner() -> None:
        async with asqlite.create_pool("tokens.db") as tdb:
            tokens, subs = await setup_database(tdb)

            async with Bot(token_database=tdb, subs=subs) as bot:
                for pair in tokens:
                    await bot.add_token(*pair)

                await bot.start(load_tokens=False)

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        LOGGER.warning("Shutting down due to KeyboardInterrupt")


if __name__ == "__main__":
    main()